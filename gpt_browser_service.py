#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 嵌入浏览器常驻服务（多会话版，对标 Codex 内置浏览器）。

架构（依据 Codex 公开文档与调研结论）：
  - 一个懒启动 Chromium（首次使用才启动，屏幕外运行，永不弹出窗口）
  - auth_ctx：持久 BrowserContext（gpt_profile）——登录身份，作为所有会话
    的 cookie 种子（Codex 的"共享 browser identity"类比）
  - sessions[key]：每个 DSH 会话一个非持久 BrowserContext——cookies/
    localStorage/页面完全隔离，会话之间互不干扰；共享登录态
  - 按需：会话首次被访问（/stream?session= 或 /ask 带 session_key）才创建；
    仅当会话有 WS 客户端（面板可见）才推 screencast 帧（15fps 节流）；
    面板卸载（/close）立即回收，空闲 15 分钟自动回收
  - 二进制 WS 帧：header(JSON) + '\\n' + JPEG bytes，队列只留最新帧

接口：
  - GET  /stream?session=<key> : 屏幕流（二进制帧）+ 输入事件（鼠标/键盘）
  - GET  /status              : 服务与登录状态
  - POST /ask                 : 向 ChatGPT 发消息并等回复（session_key 路由）
  - POST /open                : 打开指定名称会话（带 session）
  - POST /new                 : 新建会话（带 session）
  - POST /navigate            : 导航任意 URL（带 session）
  - POST /close               : 显式回收会话（前端面板卸载时调用）

启动：python gpt_browser_service.py [--port 3090]
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time

from aiohttp import web, WSMsgType

CHAT_URL = "https://chatgpt.com/"
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "gpt_profile")
SESSION_MAP_FILE = os.path.join(HERE, "session_map.json")
CONVERSATION_MAP_FILE = os.path.join(HERE, "conversation_map.json")
DEFAULT_PORT = 3090
VIEWPORT = {"width": 1280, "height": 900}
FRAME_FPS = 15                 # 可见会话的目标帧率
SESSION_IDLE_SECONDS = 900     # 空闲回收（15 分钟）


# --------------------------------------------------------------------------
# CORS：前端（http://127.0.0.1:3080 详情面板）跨源调用 HTTP API。
# aiohttp 默认不带 Access-Control-Allow-Origin，浏览器会拒绝读取响应，
# 导致面板工具栏/地址栏全部静默失效。这里放开本地 origin + OPTIONS preflight。
# --------------------------------------------------------------------------
def _is_local_origin(origin):
    if not origin:
        return False
    return (origin.startswith("http://127.0.0.1:")
            or origin.startswith("http://localhost:")
            or origin.startswith("http://[::1]:"))


@web.middleware
async def cors_middleware(request, handler):
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
        if _is_local_origin(origin):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp
    resp = await handler(request)
    if _is_local_origin(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# --------------------------------------------------------------------------
# 模型可读性：页面快照（浏览器端提取文本 + 可交互元素 + 建议选择器）
# --------------------------------------------------------------------------
SNAPSHOT_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const text = (document.body ? document.body.innerText : '') || '';
  const els = document.querySelectorAll(
    'button, a, input, textarea, select, [contenteditable="true"], [role="button"], [role="link"], [role="textbox"], [role="menuitem"], [role="tab"]'
  );
  const out = [];
  for (const el of els) {
    if (out.length >= 30) break;
    const r = el.getBoundingClientRect();
    if (!r || (r.width === 0 && r.height === 0)) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    const tag = el.tagName.toLowerCase();
    const aria = el.getAttribute('aria-label') || '';
    const ph = el.getAttribute('placeholder') || '';
    const inner = clean(el.innerText).slice(0, 80);
    if (!aria && !ph && !inner && tag !== 'input' && tag !== 'textarea') continue;
    let sel = '';
    if (aria) sel = '[aria-label="' + aria.replace(/"/g, '&quot;') + '"]';
    else if (ph) sel = tag + '[placeholder="' + ph.replace(/"/g, '&quot;') + '"]';
    else if (inner && (tag === 'button' || tag === 'a')) {
      const q = inner.slice(0, 40).replace(/"/g, '&quot;');
      sel = tag + ':has-text("' + q + '")';
    } else sel = tag;
    out.push({
      tag,
      role: el.getAttribute('role') || '',
      text: inner,
      aria,
      placeholder: ph,
      href: el.href || '',
      sel,
    });
  }
  return { text: text.slice(0, 6000), interactives: out };
}
"""

AUTH_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--window-position=-32000,-32000",  # 屏幕外，永不弹出
    # 窗口被隐藏后 Chrome 会因遮挡检测暂停渲染——禁用后画面照常输出
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--no-activate",  # 启动时不抢焦点
]


# --------------------------------------------------------------------------
# 会话映射（DSH 会话键 -> GPT 会话 URL），与 gpt_web.py 同文件，可共存
# --------------------------------------------------------------------------
def _load_session_map():
    try:
        with open(SESSION_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_session_map(mapping):
    try:
        tmp = SESSION_MAP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSION_MAP_FILE)
    except Exception as exc:
        print(f"[警告] 保存会话映射失败：{exc}", file=sys.stderr)


def _session_map_get(key):
    return _load_session_map().get(key)


def _session_map_set(key, url):
    m = _load_session_map()
    m[key] = url
    _save_session_map(m)


def _session_map_del(key):
    m = _load_session_map()
    if key in m:
        del m[key]
        _save_session_map(m)


# 名称 -> 会话 URL 缓存（conversation 名称 → /c/<uuid>）：第一次按名称在
# 侧边栏找到后记录，之后直接用 URL 直开，跳过不可靠的侧边栏查找
def _conv_map_get(name):
    try:
        with open(CONVERSATION_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(name) if isinstance(data, dict) else None
    except Exception:
        return None


def _conv_map_set(name, url):
    try:
        m = {}
        try:
            with open(CONVERSATION_MAP_FILE, "r", encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            m = {}
        if not isinstance(m, dict):
            m = {}
        m[name] = url
        tmp = CONVERSATION_MAP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONVERSATION_MAP_FILE)
    except Exception as exc:
        print(f"[警告] 保存会话名映射失败：{exc}", file=sys.stderr)


def _kill_profile_chrome():
    """杀掉占用 gpt_profile 的残留 Chrome（单实例锁）。"""
    try:
        # CREATE_NO_WINDOW：禁止弹出 PowerShell 控制台窗口（否则每次清理都闪现）
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -match 'gpt_profile' } | "
             "ForEach-Object { taskkill /PID $_.ProcessId /T /F }"],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        print(f"[警告] 清理残留 Chrome 失败：{exc}", file=sys.stderr)


def _hide_profile_chrome_windows():
    """Win32：隐藏所有 gpt_profile Chrome 的顶层窗口（任务栏无图标、不弹
    窗口、不抢焦点；画面照常由 CDP screencast 输出）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        pids = set()
        for pid, cmdline in _profile_chrome_pids():
            pids.add(pid)
        if not pids:
            return
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        found = []

        def cb(hwnd, _lp):
            tid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(tid))
            if tid.value in pids:
                found.append(hwnd)
            return True

        user32.EnumWindows(enum_proc(cb), 0)
        for hwnd in found:
            try:
                user32.ShowWindow(ctypes.c_void_p(hwnd), 0)  # SW_HIDE
            except Exception:
                pass
    except Exception as exc:
        print(f"[警告] 隐藏浏览器窗口失败：{exc}", file=sys.stderr)


def _profile_chrome_pids():
    """返回 (pid, cmdline) 列表：命令行含 gpt_profile 的 chrome 进程。"""
    out = []
    try:
        import subprocess
        # CREATE_NO_WINDOW：禁止弹出 PowerShell 控制台窗口（watchdog 每 3s
        # 调一次本函数，无此标志会持续闪 PowerShell 窗口）
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -match 'gpt_profile' } | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].strip().isdigit():
                out.append((int(parts[0].strip()), parts[1]))
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# 单个 DSH 会话的浏览器槽（context + page + cdp + 帧队列 + ws 客户端）
# --------------------------------------------------------------------------
class BrowserSession:
    def __init__(self, key, ctx, page, cdp):
        self.key = key
        self.ctx = ctx          # BrowserContext（非持久，隔离）
        self.page = page        # 当前活动 Page
        self.cdp = cdp          # CDP session（绑定 page）
        self.clients = set()    # 绑定的 WS 客户端
        self.frame_q = asyncio.Queue(maxsize=1)  # 只留最新帧
        self.ask_lock = asyncio.Lock()
        self.last_active_at = time.time()
        self.dead = False
        self._screencast_on = False
        # 页面实际 CSS viewport（innerWidth/innerHeight）——输入坐标基准；
        # 可能被窗口系统 clamp，必须实时读而不是用 launch 时的设置值
        self.css_w = VIEWPORT["width"]
        self.css_h = VIEWPORT["height"]

    def touch(self):
        self.last_active_at = time.time()
    @property
    def page_size(self):
        """CSS viewport（CDP 输入坐标基准）。"""
        return self.css_w, self.css_h

    def logged_in(self):
        try:
            u = self.page.url if self.page else ""
            return u.startswith(CHAT_URL) and "auth.openai.com" not in u
        except Exception:
            return False

    async def close(self):
        """标记死亡并后台关闭（不阻塞调用方；context 关闭可能等待页面）。"""
        self.dead = True
        asyncio.create_task(self._do_close())

    async def _do_close(self):
        try:
            if self._screencast_on and self.cdp is not None:
                await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.ctx.close(), timeout=15)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------
class BrowserService:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.auth_ctx = None    # 持久 context：登录身份（cookie 种子）
        self.auth_page = None
        self.sessions = {}      # key -> BrowserSession
        self.startup_error = ""
        self._started = False

    # ---------------- 浏览器（懒启动） ----------------
    async def ensure_browser(self):
        """auth context 就绪（持久登录），浏览器进程懒启动。"""
        if self._started and self.browser is not None:
            return
        from playwright.async_api import async_playwright
        for i in range(3):
            try:
                self.pw = await async_playwright().start()
                self.auth_ctx = await self.pw.chromium.launch_persistent_context(
                    user_data_dir=PROFILE, channel="chrome", headless=False,
                    viewport=VIEWPORT, locale="en-US", args=AUTH_BROWSER_ARGS,
                )
                # 有头窗口（避免 Cloudflare 人机验证），但立即隐藏：
                # 任务栏无图标、不弹屏幕中间、不抢焦点
                _hide_profile_chrome_windows()
                self.browser = self.auth_ctx.browser
                self.auth_page = self.auth_ctx.pages[0] if self.auth_ctx.pages \
                    else await self.auth_ctx.new_page()
                await self.auth_page.goto(CHAT_URL, wait_until="domcontentloaded")
                try:
                    await self.auth_page.wait_for_url(
                        lambda u: "chatgpt.com" in u, timeout=20000)
                except Exception:
                    pass
                self._started = True
                print("[服务] 浏览器就绪（懒启动完成，auth 登录态持久）")
                return
            except Exception as exc:
                print(f"[启动失败 {i + 1}/3] {exc}", file=sys.stderr)
                self.startup_error = str(exc)
                _kill_profile_chrome()
                await asyncio.sleep(2)
        raise RuntimeError(f"浏览器启动失败：{self.startup_error}")

    async def get_session(self, key):
        """取（或创建）指定 DSH 会话的浏览器槽。"""
        key = (key or "default").strip() or "default"
        s = self.sessions.get(key)
        if s is not None and not s.dead:
            s.touch()
            return s
        if s is not None:
            del self.sessions[key]
        await self.ensure_browser()
        # 新会话：独立 BrowserContext，从 auth 复制 cookies（共享登录，隔离状态）
        ctx = await self.browser.new_context(
            viewport=VIEWPORT, locale="en-US",
        )
        try:
            cookies = await self.auth_ctx.cookies()
            if cookies:
                await ctx.add_cookies(cookies)
        except Exception as exc:
            print(f"[警告] 复制登录 cookies 失败：{exc}", file=sys.stderr)
        page = await ctx.new_page()
        await page.goto(CHAT_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_url(lambda u: "chatgpt.com" in u, timeout=20000)
        except Exception:
            pass
        cdp = await ctx.new_cdp_session(page)
        s = BrowserSession(key, ctx, page, cdp)
        cdp.on("Page.screencastFrame", self._make_on_frame(s))
        # 弹窗/新标签（window.open、登录跳转等）自动成为活动页
        ctx.on("page", lambda p: asyncio.create_task(self._adopt_page(s, p)))
        self.sessions[key] = s
        await self._refresh_css_viewport(s)
        asyncio.create_task(self._broadcast_loop(s))
        print(f"[会话] 创建浏览器槽 session={key} "
              f"logged_in={s.logged_in()} sessions={len(self.sessions)}")
        return s

    async def _refresh_css_viewport(self, s):
        """读页面实际 CSS viewport（窗口系统可能 clamp 了窗口尺寸）。"""
        try:
            size = await s.page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight})")
            s.css_w = int(size.get("w") or VIEWPORT["width"])
            s.css_h = int(size.get("h") or VIEWPORT["height"])
        except Exception:
            pass

    async def _adopt_page(self, s, page):
        """把新弹出的页面切换为该会话的活动页（旧页停止推帧）。"""
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        if s.dead:
            return
        try:
            await self._stop_screencast(s)
        except Exception:
            pass
        try:
            await s.cdp.detach()
        except Exception:
            pass
        s.page = page
        s.cdp = await s.ctx.new_cdp_session(page)
        s.cdp.on("Page.screencastFrame", self._make_on_frame(s))
        await self._refresh_css_viewport(s)
        if s.clients:
            await self._start_screencast(s)

    # ---------------- 屏幕流 ----------------
    def _make_on_frame(self, s):
        def on_frame(msg):
            # 立即 ACK（fire-and-forget），避免 CDP 停帧
            try:
                sid = msg["sessionId"]
                loop = asyncio.get_running_loop()
                loop.create_task(self._ack(s, sid))
            except Exception:
                pass
            try:
                s.frame_q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # 只留最新帧
        return on_frame

    async def _ack(self, s, session_id):
        try:
            await s.cdp.send("Page.screencastFrameAck",
                             {"sessionId": session_id})
        except Exception:
            pass

    async def _broadcast_loop(self, s):
        """per-session 广播：15fps 节流 + 二进制帧（header + JPEG bytes）。"""
        last_sent = 0.0
        while not s.dead:
            msg = await s.frame_q.get()
            if not s.clients or s.page is None:
                continue
            now = time.time()
            if now - last_sent < 1.0 / FRAME_FPS:
                continue
            last_sent = now
            try:
                meta = msg.get("metadata", {}) or {}
                pw, ph = s.page_size  # CSS viewport（CDP 输入坐标基准）
                fw = int(meta.get("deviceWidth", pw) or pw)
                fh = int(meta.get("deviceHeight", ph) or ph)
                header = json.dumps({
                    "type": "frame",
                    "pageW": pw, "pageH": ph,
                    # 帧物理像素（img 固有尺寸）与缩放因子：前端换算坐标用
                    "frameW": fw, "frameH": fh,
                    "scale": float(meta.get("pageScaleFactor", 1.0) or 1.0),
                }).encode("utf-8") + b"\n"
                payload = base64.b64decode(msg["data"])
                dead = []
                for ws in list(s.clients):
                    try:
                        await ws.send_bytes(header + payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    s.clients.discard(ws)
            except Exception as exc:
                print(f"[广播失败] {exc}", file=sys.stderr)

    async def _start_screencast(self, s):
        if s._screencast_on:
            return
        try:
            await s.cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": 65,
                "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
            })
            s._screencast_on = True
        except Exception as exc:
            print(f"[screencast 启动失败] {exc}", file=sys.stderr)

    async def _stop_screencast(self, s):
        if not s._screencast_on:
            return
        try:
            await s.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        s._screencast_on = False

    # ---------------- WS 流 ----------------
    async def ws_stream(self, request):
        key = request.query.get("session", "") or "default"
        try:
            s = await self.get_session(key)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=503)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        s.clients.add(ws)
        await self._start_screencast(s)
        try:
            hello = {
                "type": "hello",
                "pageW": s.page_size[0], "pageH": s.page_size[1],
                "loggedIn": s.logged_in(),
                "url": s.page.url if s.page else "",
            }
            await ws.send_str(json.dumps(hello))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        ev = json.loads(msg.data)
                    except Exception:
                        continue
                    await self._handle_input(s, ev)
                elif msg.type == WSMsgType.ERROR:
                    break
        except Exception:
            pass
        finally:
            s.clients.discard(ws)
            s.touch()
            if not s.clients:
                await self._stop_screencast(s)  # 不可见 → 停帧（省资源）
        return ws

    async def _handle_input(self, s, ev):
        if s.cdp is None:
            return
        t = ev.get("type")
        try:
            if t == "mouse":
                action = ev.get("action", "moved")
                kind = {"moved": "mouseMoved", "pressed": "mousePressed",
                        "released": "mouseReleased", "wheel": "mouseWheel"}.get(action)
                if kind is None:
                    return
                params = {"type": kind, "x": int(ev.get("x", 0)), "y": int(ev.get("y", 0))}
                if kind in ("mousePressed", "mouseReleased"):
                    params["button"] = ev.get("button", "left")
                    params["clickCount"] = int(ev.get("clickCount", 1))
                if kind == "mouseWheel":
                    params["deltaX"] = int(ev.get("deltaX", 0))
                    params["deltaY"] = int(ev.get("deltaY", 0))
                await s.cdp.send("Input.dispatchMouseEvent", params)
            elif t == "key":
                action = ev.get("action", "down")
                kind = "keyDown" if action == "down" else "keyUp"
                params = {"type": kind, "key": ev.get("key", "")}
                if ev.get("code"):
                    params["code"] = ev["code"]
                if ev.get("vk"):
                    params["windowsVirtualKeyCode"] = int(ev["vk"])
                await s.cdp.send("Input.dispatchKeyEvent", params)
            elif t == "text":
                await s.cdp.send("Input.insertText", {"text": ev.get("text", "")})
        except Exception as exc:
            print(f"[输入转发失败] {exc}", file=sys.stderr)

    # ---------------- HTTP API ----------------
    async def api_status(self, request):
        # 不触发浏览器启动（懒启动）：只报进程与（已启动时的）登录状态。
        info = {"ok": True, "started": self._started}
        if self._started and self.auth_page is not None:
            try:
                info["loggedIn"] = "auth.openai.com" not in (self.auth_page.url or "")
                info["url"] = self.auth_page.url
                info["title"] = await self.auth_page.title()
            except Exception:
                info["loggedIn"] = False
        info["sessions"] = list(self.sessions.keys())
        return web.json_response(info)

    def _session_param(self, request):
        return request.query.get("session", "") or "default"

    async def _with_session(self, request, handler):
        key = self._session_param(request)
        try:
            s = await self.get_session(key)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=503)
        return await handler(s)

    async def api_navigate(self, request):
        async def handler(s):
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "bad json"}, status=400)
            url = (body.get("url") or "").strip()
            if not url:
                return web.json_response({"ok": False, "error": "url required"}, status=400)
            if not url.startswith("http"):
                url = "https://" + url
            try:
                await s.page.goto(url, wait_until="domcontentloaded")
                try:
                    await s.page.wait_for_url(
                        lambda u: url.split("://")[1][:40] in u, timeout=15000)
                except Exception:
                    pass
                await self._refresh_css_viewport(s)
                return web.json_response({"ok": True, "url": s.page.url})
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)})
        return await self._with_session(request, handler)

    async def api_reload(self, request):
        async def handler(s):
            try:
                await s.page.reload(wait_until="domcontentloaded", timeout=20000)
                await self._refresh_css_viewport(s)
                return web.json_response({"ok": True, "url": s.page.url})
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)})
        return await self._with_session(request, handler)

    async def api_back(self, request):
        async def handler(s):
            try:
                await s.page.go_back(wait_until="domcontentloaded", timeout=20000)
                await self._refresh_css_viewport(s)
                return web.json_response({"ok": True, "url": s.page.url})
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)})
        return await self._with_session(request, handler)

    async def api_forward(self, request):
        async def handler(s):
            try:
                await s.page.go_forward(wait_until="domcontentloaded", timeout=20000)
                await self._refresh_css_viewport(s)
                return web.json_response({"ok": True, "url": s.page.url})
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)})
        return await self._with_session(request, handler)

    async def api_external(self, request):
        async def handler(s):
            try:
                import webbrowser
                url = s.page.url
                webbrowser.open(url)
                return web.json_response({"ok": True, "url": url})
            except Exception as exc:
                return web.json_response({"ok": False, "error": str(exc)})
        return await self._with_session(request, handler)

    async def api_open(self, request):
        async def handler(s):
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "bad json"}, status=400)
            name = (body.get("name") or "").strip()
            if not name:
                return web.json_response({"ok": False, "error": "name required"}, status=400)
            async with s.ask_lock:
                ok = await self._open_conversation(s.page, name)
                if not ok:
                    titles = await self._session_links(s.page, limit=40)
                    return web.json_response({
                        "ok": False, "error": "会话不存在", "visible": titles,
                    })
                return web.json_response({"ok": True, "url": s.page.url})
        return await self._with_session(request, handler)

    async def api_new(self, request):
        async def handler(s):
            async with s.ask_lock:
                await self._new_chat(s.page)
                return web.json_response({"ok": True, "url": s.page.url})
        return await self._with_session(request, handler)

    async def api_ask(self, request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        question = (body.get("question") or "").strip()
        if not question:
            return web.json_response({"ok": False, "error": "问题为空"}, status=400)
        conversation = (body.get("conversation") or "").strip()
        session_key = (body.get("session_key") or "").strip()
        timeout = int(body.get("timeout", 180) or 180)
        try:
            s = await self.get_session(session_key)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=503)
        async with s.ask_lock:
            try:
                reply, opened_existing, err = await self._ask(
                    s, question, conversation, session_key, timeout)
            except Exception as exc:
                return web.json_response({"ok": False, "error": f"ask 失败：{exc}"})
        if err:
            return web.json_response({"ok": False, "error": err, "reply": reply})
        return web.json_response({"ok": True, "reply": reply})

    # ---------------- 模型可读性 + 元素级操作 ----------------
    async def api_snapshot(self, request):
        """页面快照：标题/URL/可见文本/可交互元素（含建议选择器）+ 可选截图。

        对标 Codex：让模型"看到"页面状态。截图默认不返回（省流量），
        查询参数 ?shot=1 时附 base64 JPEG。
        """
        async def handler(s):
            try:
                title = await s.page.title()
            except Exception:
                title = ""
            url = s.page.url
            try:
                info = await s.page.evaluate(SNAPSHOT_JS)
            except Exception as exc:
                return web.json_response(
                    {"ok": False, "error": f"快照提取失败：{exc}"})
            resp = {
                "ok": True,
                "title": title,
                "url": url,
                "text": info.get("text", "") if isinstance(info, dict) else "",
                "interactives": info.get("interactives", []) if isinstance(info, dict) else [],
            }
            want = (request.query.get("shot") or request.query.get("screenshot") or "").strip()
            if want and want not in ("0", "false", "no"):
                try:
                    shot = await s.page.screenshot(type="jpeg", quality=70)
                    resp["screenshot_b64"] = base64.b64encode(shot).decode()
                except Exception as exc:
                    resp["screenshot_error"] = str(exc)
            return web.json_response(resp)
        return await self._with_session(request, handler)

    async def api_act(self, request):
        """元素级操作：用 locator 精确点击/输入，替代坐标点击的脆弱性。

        op:
          click:  locator(sel).nth(index).click()
          type:   locator(sel).nth(index).click() 后 fill(text)（失败回退键盘输入）
          press:  page.keyboard.press(key)（如 "Enter"）
          text:   page.keyboard.type(text)（在当前聚焦处输入）
          scroll: 有 sel 则滚动到元素；否则 page.mouse.wheel(0, deltaY)
          wait:   page.wait_for_timeout(ms)
        """
        async def handler(s):
            try:
                body = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "bad json"}, status=400)
            op = (body.get("op") or "").strip()
            sel = (body.get("sel") or "").strip()
            text = body.get("text") or ""
            key = body.get("key") or ""
            try:
                index = int(body.get("index", 0) or 0)
                delta_y = int(body.get("deltaY", 600) or 600)
                ms = int(body.get("ms", 800) or 800)
            except ValueError:
                return web.json_response({"ok": False, "error": "index/deltaY/ms 需为整数"})
            page = s.page
            try:
                if op in ("click", "type") and not sel:
                    return web.json_response({"ok": False, "error": "click/type 需要 sel"})
                if op == "click":
                    loc = page.locator(sel).nth(index)
                    await loc.scroll_into_view_if_needed(timeout=5000)
                    await loc.click(timeout=8000)
                elif op == "type":
                    loc = page.locator(sel).nth(index)
                    await loc.scroll_into_view_if_needed(timeout=5000)
                    await loc.click(timeout=8000)
                    try:
                        await loc.fill(text)
                    except Exception:
                        await page.keyboard.type(text)
                elif op == "press":
                    if not key:
                        return web.json_response({"ok": False, "error": "press 需要 key"})
                    await page.keyboard.press(key)
                elif op == "text":
                    await page.keyboard.type(text)
                elif op == "scroll":
                    if sel:
                        await page.locator(sel).nth(index).scroll_into_view_if_needed(timeout=5000)
                    else:
                        await page.mouse.wheel(0, delta_y)
                elif op == "wait":
                    await page.wait_for_timeout(ms)
                else:
                    return web.json_response({"ok": False, "error": f"未知操作 {op}（click/type/press/text/scroll/wait）"})
            except Exception as exc:
                return web.json_response({"ok": False, "error": f"{op} 失败：{exc}"})
            await self._refresh_css_viewport(s)
            return web.json_response({"ok": True, "url": page.url})
        return await self._with_session(request, handler)

    async def api_close(self, request):
        key = self._session_param(request)
        s = self.sessions.pop(key, None)
        if s is not None:
            await s.close()
            print(f"[会话] 回收浏览器槽 session={key}")
        return web.json_response({"ok": True})

    async def api_debug(self, request):
        async def handler(s):
            sel = request.query.get("sel", "")
            if not sel:
                return web.json_response({"error": "sel required"})
            el = s.page.locator(sel)
            out = {"sel": sel, "count": await el.count()}
            if out["count"] > 0:
                try:
                    out["visible"] = await el.first.is_visible()
                except Exception as e:
                    out["visible"] = f"err {e}"
                try:
                    out["disabled"] = await el.first.is_disabled()
                except Exception as e:
                    out["disabled"] = f"err {e}"
                try:
                    out["tag"] = await el.first.evaluate(
                        "e => e.tagName + '#' + e.id + '.' + (e.className || '')")
                except Exception as e:
                    out["tag"] = f"err {e}"
            return web.json_response(out)
        return await self._with_session(request, handler)

    # ---------------- 页面操作（async 版，逻辑同 gpt_web.py） ----------------
    async def _ensure_chatgpt_ready(self, page, timeout=45):
        """等 Cloudflare 人机验证（'Just a moment...'）自动通过。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if "chatgpt.com" in page.url and "auth.openai.com" not in page.url:
                    title = await page.title()
                    if "just a moment" not in title.lower():
                        return True
            except Exception:
                pass
            await page.wait_for_timeout(1500)
        return False

    async def _session_links(self, page, limit=100):
        out = []
        links = page.locator('a[href*="/c/"]')
        n = min(await links.count(), limit)
        for i in range(n):
            try:
                href = links.nth(i).get_attribute("href")
                text = (await links.nth(i).inner_text(timeout=1200)) or ""
                title = text.strip().splitlines()[0] if text.strip() else ""
            except Exception:
                continue
            out.append((title, href or ""))
        return out

    @staticmethod
    def _norm_url(u):
        u = (u or "").strip()
        if u.startswith("https://chatgpt.com"):
            u = u[len("https://chatgpt.com"):]
        return u

    async def _open_conversation(self, page, name):
        """打开指定会话。优先级：① 参数是 URL 直接导航 → ② 名称缓存 URL
        直接导航（跳过侧边栏）→ ③ 侧边栏按名称查找（增强：等 3s、扫 100
        链接、滚动加载 30s）。成功后把 名称→URL 写入缓存，下次直开。"""
        name = name.strip()
        if not name:
            return False
        # ① 参数本身是会话 URL / /c/<uuid>：直接导航，不依赖侧边栏
        if "/c/" in name or name.startswith("http"):
            import re
            m = re.search(r"/c/([0-9a-fA-F-]+)", name)
            if m:
                url = f"https://chatgpt.com/c/{m.group(1)}"
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
                    _conv_map_set(name, url)
                    return True
                except Exception:
                    return False
        # ② 名称缓存命中：直接 URL 导航（最稳，跳过侧边栏）
        cached = _conv_map_get(name)
        if cached and "/c/" in cached:
            try:
                await page.goto(cached, wait_until="domcontentloaded")
                await page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
                return True
            except Exception:
                pass  # 缓存失效（会话被删/归档）→ 回退侧边栏查找
        # ③ 侧边栏按名称查找（虚拟列表：滚动加载 + 多次扫描）
        await page.wait_for_timeout(3000)
        deadline = time.time() + 30
        while time.time() < deadline:
            sessions = await self._session_links(page, limit=100)
            match = None
            for t, href in sessions:
                if t and name.lower() in t.lower():
                    match = (t, href)
                    break
            if match is not None:
                links = page.locator('a[href*="/c/"]')
                for i in range(await links.count()):
                    try:
                        if (links.nth(i).get_attribute("href") or "") == match[1]:
                            await links.nth(i).click(timeout=5000)
                            break
                    except Exception:
                        continue
                try:
                    await page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
                except Exception:
                    pass
                if match[1]:
                    _conv_map_set(name, match[1])
                return True
            try:
                await page.locator('nav').first.hover()
                await page.mouse.wheel(0, 2500)
            except Exception:
                try:
                    await page.mouse.wheel(0, 2500)
                except Exception:
                    pass
            await page.wait_for_timeout(1200)
        return False

    async def _open_conversation_by_url(self, page, url):
        url = url.strip()
        if not url or "/c/" not in url:
            return False
        await page.wait_for_timeout(2500)
        deadline = time.time() + 20
        rolled_top = False
        while time.time() < deadline:
            for _t, href in await self._session_links(page):
                if self._norm_url(href) == self._norm_url(url):
                    links = page.locator('a[href*="/c/"]')
                    for i in range(await links.count()):
                        try:
                            if self._norm_url(links.nth(i).get_attribute("href") or "") == self._norm_url(url):
                                await links.nth(i).click(timeout=5000)
                                break
                        except Exception:
                            continue
                    try:
                        await page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
                    except Exception:
                        pass
                    return True
            try:
                await page.locator('nav').first.hover()
                await page.mouse.wheel(0, -5000 if not rolled_top else 2500)
            except Exception:
                try:
                    await page.mouse.wheel(0, -5000 if not rolled_top else 2500)
                except Exception:
                    pass
            rolled_top = True
            await page.wait_for_timeout(1200)
        return False

    async def _new_chat(self, page):
        for sel in ['a[href="/"]', 'a[aria-label*="New chat" i]',
                    'a[aria-label*="新建" i]', 'button[aria-label*="New chat" i]']:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    await el.click(timeout=3000)
                    return
            except Exception:
                continue
        try:
            await page.goto(CHAT_URL, wait_until="domcontentloaded")
        except Exception:
            pass

    async def _find_input(self, page):
        for sel in ["textarea#prompt-textarea", "div#prompt-textarea",
                    "form textarea", "main textarea",
                    "main div[contenteditable='true']"]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible() and not await el.is_disabled():
                    return el
            except Exception:
                continue
        return None

    async def _is_generating(self, page):
        for sel in [
            '[data-testid="stop-button"]',
            '[data-testid="stop-generating-button"]',
            'button:has-text("Stop generating")',
            'button:has-text("停止生成")',
        ]:
            try:
                el = page.locator(sel).first
                if await el.count() > 0 and await el.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _is_thinking(txt):
        t = (txt or "").strip().lower()
        return t in ("thinking", "正在思考", "思考中", "思考中…", "thinking…", "thinking...") \
            or (t.startswith("thinking") and len(t) < 30)

    async def _wait_for_reply(self, page, timeout):
        deadline = time.time() + timeout
        last_text, stable_since = "", time.time()

        async def current_text():
            msgs = page.locator('[data-message-author-role="assistant"]')
            n = await msgs.count()
            if n == 0:
                return ""
            return await msgs.nth(n - 1).inner_text(timeout=3000)

        while time.time() < deadline:
            try:
                txt = await current_text()
            except Exception:
                await page.wait_for_timeout(800)
                continue
            if self._is_thinking(txt):
                last_text = ""
                await page.wait_for_timeout(800)
                continue
            if txt and txt != last_text:
                last_text, stable_since = txt, time.time()
            elif txt and (time.time() - stable_since) >= 2.0 and not await self._is_generating(page):
                return txt, None
            await page.wait_for_timeout(800)
        return last_text, ("超时未获取到回复" if not last_text else None)

    async def _ask(self, s, question, conversation, session_key, timeout):
        page = s.page
        if not s.logged_in():
            return "", False, "未登录。请先运行：python gpt_web.py login"
        if not await self._ensure_chatgpt_ready(page):
            return "", False, "ChatGPT 页面被人机验证拦截（Cloudflare），请稍后重试或重启服务"
        opened_existing = False
        err = None
        if conversation:
            opened_existing = await self._open_conversation(page, conversation)
            if not opened_existing:
                return "", False, f"未找到会话「{conversation}」"
        elif session_key:
            session_url = _session_map_get(session_key)
            if session_url:
                opened_existing = await self._open_conversation_by_url(page, session_url)
                if not opened_existing:
                    _session_map_del(session_key)
                    await self._new_chat(page)
            else:
                await self._new_chat(page)
        else:
            await self._new_chat(page)

        inp = None
        find_deadline = time.time() + 25
        while time.time() < find_deadline and inp is None:
            inp = await self._find_input(page)
            if inp is None:
                await page.wait_for_timeout(1500)
        if inp is None:
            return "", False, "找不到输入框"
        try:
            await inp.click(force=True)
            await inp.fill(question)
        except Exception:
            await page.keyboard.type(question)
        await page.keyboard.press("Enter")
        reply, werr = await self._wait_for_reply(page, timeout)
        if session_key and not opened_existing:
            cur = page.url
            if "/c/" in cur:
                _session_map_set(session_key, cur)
        return reply, opened_existing, werr

    # ---------------- 回收 ----------------
    async def _reaper(self):
        """空闲会话自动回收（15 分钟无 ws 客户端且无活动）。"""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for key, s in list(self.sessions.items()):
                if s.dead:
                    del self.sessions[key]
                elif not s.clients and now - s.last_active_at > SESSION_IDLE_SECONDS:
                    print(f"[会话] 空闲回收 session={key}")
                    await s.close()
                    del self.sessions[key]

    async def _watchdog(self):
        """周期隐藏浏览器窗口（新弹窗也隐藏）+ auth 页面死亡时自杀退出。"""
        while True:
            await asyncio.sleep(3)
            if self._started:
                try:
                    _hide_profile_chrome_windows()
                except Exception:
                    pass
                try:
                    if self.auth_page is None or self.auth_page.is_closed():
                        print("[服务] 浏览器已关闭，进程退出", file=sys.stderr)
                        os._exit(1)
                except Exception:
                    os._exit(1)

    # ---------------- 退出 ----------------
    async def shutdown(self):
        for s in list(self.sessions.values()):
            await s.close()
        self.sessions.clear()
        try:
            if self.auth_ctx is not None:
                await self.auth_ctx.close()
        except Exception:
            pass
        try:
            if self.pw is not None:
                await self.pw.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="DSH 嵌入浏览器常驻服务（多会话）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    async def run():
        svc = BrowserService()
        app = web.Application(middlewares=[cors_middleware])
        app.router.add_get("/status", svc.api_status)
        app.router.add_get("/debug", svc.api_debug)
        app.router.add_get("/stream", svc.ws_stream)
        app.router.add_post("/ask", svc.api_ask)
        app.router.add_get("/snapshot", svc.api_snapshot)
        app.router.add_post("/snapshot", svc.api_snapshot)
        app.router.add_post("/act", svc.api_act)
        app.router.add_get("/act", svc.api_act)
        app.router.add_post("/open", svc.api_open)
        app.router.add_post("/new", svc.api_new)
        app.router.add_post("/navigate", svc.api_navigate)
        app.router.add_get("/reload", svc.api_reload)
        app.router.add_post("/reload", svc.api_reload)
        app.router.add_get("/back", svc.api_back)
        app.router.add_post("/back", svc.api_back)
        app.router.add_get("/forward", svc.api_forward)
        app.router.add_post("/forward", svc.api_forward)
        app.router.add_get("/external", svc.api_external)
        app.router.add_post("/external", svc.api_external)
        app.router.add_get("/close", svc.api_close)
        app.router.add_post("/close", svc.api_close)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", args.port)
        await site.start()
        print(f"[服务] 监听 http://127.0.0.1:{args.port}（懒启动，按需建会话，零弹出）")
        asyncio.create_task(svc._reaper())
        asyncio.create_task(svc._watchdog())
        try:
            await asyncio.Event().wait()
        finally:
            await svc.shutdown()
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    main()
