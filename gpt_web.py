#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gpt_web.py — 操作网页端已登录的 ChatGPT：发消息、读取回复（Playwright + 系统 Chrome）

用法：
  # 第一次：登录（会弹出浏览器窗口，手动登录 ChatGPT 后回到脚本按回车）
  python gpt_web.py login

  # 之后随时提问（agent 也可在任务中调用）
  python gpt_web.py ask "你的问题"
  python gpt_web.py ask "问题" --timeout 300     # 自定义等待秒数

  # 指定会话：在名为 <名称> 的会话里沟通（不存在则报错并列出可见会话，绝不新建）
  python gpt_web.py ask "问题" --conversation "项目讨论"

  # 可选：无头模式（不弹窗，风控更严格，默认有头）
  python gpt_web.py ask "问题" --headless

会话档案保存在 ./gpt_profile，登录一次后复用，不碰系统浏览器。
"""

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[错误] 未安装 playwright，请先执行：python -m pip install playwright", file=sys.stderr)
    sys.exit(1)

# ---- Win32（零闪现最小化：SetWindowPlacement 修改最小化窗口的恢复位置）----
user32 = ctypes.WinDLL("user32", use_last_error=True)

SW_SHOWMINIMIZED = 2
GW_OWNER = 4


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
        ("rcDevice", wintypes.RECT),
    ]


user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.SetWindowPlacement.restype = wintypes.BOOL
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND

PROFILE_DIR = "gpt_profile"
CHAT_URL = "https://chatgpt.com/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _stdout(msg):
    print(msg, flush=True)


def _kill_profile_chrome():
    """清理残留的 gpt_profile Chrome 进程（只杀带 gpt_profile 的，不碰其他浏览器）。

    上次调用异常退出可能留下 Chrome 占着 profile 单实例锁，导致后续启动被
    "转发"后立即关闭（TargetClosedError）。仅在本进程启动失败时调用。
    """
    try:
        script = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
                  "| Where-Object { $_.CommandLine -match 'gpt_profile' } "
                  "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        # CREATE_NO_WINDOW：禁止弹出 PowerShell 控制台窗口（否则每次清理都闪现）
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def launch(playwright, headless=False, minimized=False):
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
    ]
    # "后台静默"：窗口在屏幕外创建（--window-position 为负坐标，完全不可见、
    # 无闪现），随后 Win32 保持最小化并设置恢复位置到屏幕内——用户点任务栏
    # 图标时窗口才首次进入屏幕。注意不加 --start-minimized（会被 Playwright
    # 覆盖成 normal；且偶发"最小化启动"会让 GetWindowRect 返回 0，干扰 HWND
    # 指纹匹配）。headless 无窗口，忽略 minimized。
    if minimized and not headless:
        args.append("--window-position=-32000,-32000")
    last = None
    for attempt in range(3):
        try:
            return playwright.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                channel="chrome",
                headless=headless,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                user_agent=USER_AGENT,
                args=args,
            )
        except Exception as exc:
            last = exc
            print(f"[警告] Chrome 启动失败（{exc}），清理残留进程后重试 {attempt + 1}/3…", file=sys.stderr)
            _kill_profile_chrome()
            time.sleep(2)
    raise last


def _minimize_window(page):
    """完全静默最小化（零闪现，Win32 SetWindowPlacement 方案）。

    流程（GPT 确认的最优路线）：
      1) 先轮询等 Chrome 主窗口 HWND 出现（冷启动时窗口创建有延迟；
         窗口自始至终在屏幕外创建，用户不可见）；
      2) CDP 直接把窗口最小化——此时窗口仍在屏幕外 (-14564 等负坐标)，
         从未以 normal 状态出现在屏幕内，没有任何一帧可见；
      3) Win32 SetWindowPlacement：保持 minimized（showCmd=SW_SHOWMINIMIZED），
         只修改"未来恢复位置" rcNormalPosition 为屏幕内坐标 (100,100)；
      4) 用户点任务栏图标时，窗口才第一次进入屏幕（恢复在屏幕中间）。

    整个生命周期不存在 normal+屏幕内 的中间态，所以彻底消除闪现。
    若 Win32 部分失败，窗口仍已最小化（恢复位置停留在屏幕外，属罕见兜底）。
    """
    cdp = page.context.new_cdp_session(page)
    target = cdp.send("Browser.getWindowForTarget")
    window_id = target["windowId"]
    # 1) 先找 HWND（轮询等窗口创建），再最小化——minimized 后 rect 可能失效
    hwnd = _find_offscreen_chrome_hwnd(timeout=8.0)
    try:
        # 2) 直接最小化（屏幕外 → 最小化，无屏幕内中间态）
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "minimized"},
        })
    except Exception as exc:
        print(f"[警告] CDP 最小化失败：{exc}", file=sys.stderr)
        return
    if hwnd is not None:
        try:
            # 3) Win32 保持最小化，仅修改恢复位置（零闪现主路径）
            if not _set_minimized_restore_bounds(hwnd, 100, 100, 1280, 900):
                print("[警告] SetWindowPlacement 设置恢复位置失败，改用 CDP 兜底", file=sys.stderr)
                _cdp_restore_bounds_fallback(cdp, window_id)
        except Exception as exc:
            print(f"[警告] 设置恢复位置失败：{exc}，改用 CDP 兜底", file=sys.stderr)
            _cdp_restore_bounds_fallback(cdp, window_id)
    else:
        print("[警告] 未找到 Chrome 主窗口句柄，改用 CDP 兜底设置恢复位置", file=sys.stderr)
        _cdp_restore_bounds_fallback(cdp, window_id)


def _cdp_restore_bounds_fallback(cdp, window_id):
    """CDP 兜底：normal 设到屏幕内再立即最小化——恢复位置落在屏幕内。

    主路径（Win32 SetWindowPlacement）拿不到 HWND 时才走这里。窗口可能
    在"移到屏幕内"瞬间闪现一帧（最坏情况），但保证用户点任务栏能查看。
    """
    try:
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"left": 100, "top": 100, "width": 1280, "height": 900, "windowState": "normal"},
        })
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "minimized"},
        })
    except Exception as exc:
        print(f"[警告] CDP 兜底设置恢复位置失败：{exc}", file=sys.stderr)


def _find_offscreen_chrome_hwnd(timeout=10.0):
    """轮询枚举顶层窗口，找本次 Playwright 启动的 Chrome 主窗口句柄。

    筛选：ownerless 顶层窗口 + 类名 Chrome_WidgetWin_1 + 左上角在屏幕外
    （负坐标启动指纹）+ 主窗口尺寸（宽度>800）。Chrome 冷启动时窗口创建
    有延迟，轮询最多 timeout 秒，每 0.5s 一次。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            if user32.GetWindow(hwnd, GW_OWNER) != 0:
                return True  # 有 owner 的窗口不是顶层主窗口
            buf = ctypes.create_unicode_buffer(64)
            if user32.GetClassNameW(hwnd, buf, 64) == 0 or buf.value != "Chrome_WidgetWin_1":
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            if (rect.left < 0 and rect.top < 0        # 左上角在屏幕外（启动指纹）
                    and rect.right - rect.left > 800):  # 主窗口尺寸（排除残留小窗）
                found.append((hwnd, rect.left, rect.top))
            return True

        user32.EnumWindows(_cb, 0)
        if found:
            found.sort(key=lambda t: abs(t[1] + 32000) + abs(t[2] + 32000))
            return found[0][0]
        time.sleep(0.5)
    return None


def _set_minimized_restore_bounds(hwnd, x, y, w, h):
    """保持窗口最小化，仅把"恢复位置"改为 (x, y, w, h)（workspace 坐标）。"""
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
        return False
    wp.showCmd = SW_SHOWMINIMIZED
    wp.rcNormalPosition.left = x
    wp.rcNormalPosition.top = y
    wp.rcNormalPosition.right = x + w
    wp.rcNormalPosition.bottom = y + h
    return bool(user32.SetWindowPlacement(hwnd, ctypes.byref(wp)))


def is_logged_in(page):
    return page.url.startswith(CHAT_URL) or "chatgpt.com" in page.url


def _is_generating(page):
    """ChatGPT 正在生成回复时页面上会出现 Stop 按钮；按钮消失 = 生成结束。

    这是"是否完成"的权威信号：流式回复中途可能停顿数秒，单纯按文本稳定
    判定会把停顿误判成"已经答完"。选择器随 UI 改版可能失效，失效时返回
    False（退化为纯文本稳定判定，不比之前差）。
    """
    for sel in [
        '[data-testid="stop-button"]',
        '[data-testid="stop-generating-button"]',
        'button:has-text("Stop generating")',
        'button:has-text("停止生成")',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                return True
        except Exception:
            continue
    return False


def _is_thinking(txt):
    """GPT 思考阶段的占位文本：不是最终回复，继续等。"""
    t = (txt or "").strip().lower()
    return t in ("thinking", "正在思考", "思考中", "思考中…", "thinking…", "thinking...") \
        or t.startswith("thinking") and len(t) < 30


def wait_for_reply(page, timeout):
    """轮询最后一个助手消息，直到生成结束且文本稳定。返回回复文本。

    完成条件 = 没有"生成中"指示（Stop 按钮消失）且文本连续 2 秒不变，
    避免把流式停顿误判成完成。读取失败时不推进稳定计时，绝不因 DOM
    抖动提前返回。GPT 思考阶段（Thinking 占位文本）不视为完成。
    """
    deadline = time.time() + timeout
    last_text, stable_since = "", time.time()

    def current_text():
        msgs = page.locator('[data-message-author-role="assistant"]')
        n = msgs.count()
        if n == 0:
            return ""
        # 最后一条助手消息里的纯文本
        return msgs.nth(n - 1).inner_text(timeout=3000)

    while time.time() < deadline:
        try:
            txt = current_text()
        except Exception:
            # 读取失败（DOM 抖动/加载中）：不推进稳定计时，继续等
            time.sleep(0.8)
            continue
        if _is_thinking(txt):
            # 思考阶段：不推进完成判定，继续等真实回复
            last_text = ""
            time.sleep(0.8)
            continue
        if txt and txt != last_text:
            last_text, stable_since = txt, time.time()
        elif txt and (time.time() - stable_since) >= 2.0 and not _is_generating(page):
            return txt
        time.sleep(0.8)
    return last_text


def find_input(page):
    """找到消息输入框（textarea 或 contenteditable div）。"""
    for sel in ["textarea#prompt-textarea", "div#prompt-textarea",
                "form textarea", "main textarea", "main div[contenteditable='true']"]:
        el = page.locator(sel).first
        # 跳过 disabled 输入框（ChatGPT 新 UI 的 fallback textarea 常为 disabled）
        if el.count() > 0 and el.is_visible() and not el.is_disabled():
            return el
    return None


def new_chat(page):
    """尝试开一个新对话（侧边栏 New chat 按钮）。失败则忽略。"""
    for sel in ['a[href="/"]', 'button:has-text("New chat")',
                'button:has-text("新对话")', '[data-testid="new-chat-button"]']:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                # force：会话项可能遮挡，绕过命中检测；点击后等新会话加载
                el.click(timeout=2000, force=True)
                page.wait_for_timeout(2200)
                return True
        except Exception:
            continue
    return False


def session_links(page, limit=100):
    """返回侧边栏会话 [(title, href), ...]（回归初始版本的稳定做法）。"""
    out = []
    links = page.locator('a[href*="/c/"]')
    n = links.count()
    for i in range(min(n, limit)):
        try:
            href = links.nth(i).get_attribute("href")
            text = links.nth(i).inner_text(timeout=1200) or ""
            title = text.strip().splitlines()[0] if text.strip() else ""
        except Exception:
            continue
        out.append((title, href or ""))
    return out


def open_conversation(page, name):
    """打开指定名称/URL 的会话。优先级：① 参数是会话 URL 直接导航 →
    ② 名称缓存 URL 直接导航（跳过侧边栏）→ ③ 侧边栏按名称查找
    （title 子串匹配 + href 精确定位点击 + 滚动加载 30s）。成功后把
    名称→URL 写入缓存，下次直开。找不到返回 False（调用方决定新建/报错）。
    """
    name = name.strip()
    if not name:
        return False
    # ① 参数本身是会话 URL / /c/<uuid>：直接导航，不依赖侧边栏
    if "/c/" in name or name.startswith("http"):
        m = re.search(r"/c/([0-9a-fA-F-]+)", name)
        if m:
            url = "https://chatgpt.com/c/" + m.group(1)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
                _conv_map_set(name, url)
                _stdout(f"[已打开会话 URL] {url}")
                return True
            except Exception:
                return False
    # ② 名称缓存命中：直接 URL 导航（最稳，跳过侧边栏）
    cached = _conv_map_get(name)
    if cached and "/c/" in cached:
        try:
            page.goto(cached, wait_until="domcontentloaded")
            page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
            _stdout(f"[已打开缓存会话] {name} -> {cached}")
            return True
        except Exception:
            pass  # 缓存失效（会话被删/归档）→ 回退侧边栏查找
    # ③ 侧边栏按名称查找（虚拟列表：滚动加载 + 多次扫描）
    page.wait_for_timeout(3000)  # 等侧边栏渲染
    deadline = time.time() + 30
    while time.time() < deadline:
        sessions = session_links(page)
        match = None
        for t, href in sessions:
            if t and name.lower() in t.lower():
                match = (t, href)
                break
        if match is not None:
            # href 精确定位点击（不依赖索引/文本，杜绝误点）
            links = page.locator('a[href*="/c/"]')
            for i in range(links.count()):
                try:
                    if (links.nth(i).get_attribute("href") or "") == match[1]:
                        links.nth(i).click(timeout=5000)
                        break
                except Exception:
                    continue
            try:
                page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
            except Exception:
                pass
            if match[1]:
                _conv_map_set(name, match[1])
            _stdout(f"[已定位会话] {match[0]} {match[1]}")
            return True
        # 侧边栏是虚拟列表：悬停侧边栏后滚动，再扫下一轮
        try:
            page.locator('nav').first.hover()
            page.mouse.wheel(0, 2500)
        except Exception:
            try:
                page.mouse.wheel(0, 2500)
            except Exception:
                pass
        page.wait_for_timeout(1200)
    return False


SESSION_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_map.json")
CONVERSATION_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversation_map.json")


def _conv_map_get(name):
    """名称 -> 会话 URL 缓存查询（conversation 名称 → /c/<uuid>）。"""
    try:
        with open(CONVERSATION_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(name) if isinstance(data, dict) else None
    except Exception:
        return None


def _conv_map_set(name, url):
    """记录 名称 → 会话 URL，下次直接 URL 导航（跳过侧边栏查找）。"""
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


def _load_session_map():
    """读取会话映射 {key: "/c/<uuid>"}（DSH 会话键 → GPT 会话 URL）。"""
    try:
        with open(SESSION_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_session_map(mapping):
    """原子写会话映射（临时文件 + rename）。"""
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


def _norm_conv_url(u):
    """归一化会话 URL：完整 URL 与侧边栏相对路径（/c/<uuid>）互转比较。"""
    u = (u or "").strip()
    if u.startswith("https://chatgpt.com"):
        u = u[len("https://chatgpt.com"):]
    return u


def open_conversation_by_url(page, url):
    """按会话 URL（/c/<uuid>）精确打开侧边栏会话（不依赖标题）。

    侧边栏是虚拟列表：新会话在顶部，先滚回顶部再扫描；href 归一化后
    精确匹配（完整 URL 与相对路径等效），杜绝误点。
    """
    url = url.strip()
    if not url or "/c/" not in url:
        return False
    page.wait_for_timeout(2500)
    deadline = time.time() + 20
    rolled_top = False
    while time.time() < deadline:
        for _t, href in session_links(page):
            if _norm_conv_url(href) == _norm_conv_url(url):
                links = page.locator('a[href*="/c/"]')
                for i in range(links.count()):
                    try:
                        if _norm_conv_url(links.nth(i).get_attribute("href") or "") == _norm_conv_url(url):
                            links.nth(i).click(timeout=5000)
                            break
                    except Exception:
                        continue
                try:
                    page.wait_for_url(lambda u: "/c/" in u, timeout=15000)
                except Exception:
                    pass
                _stdout(f"[已打开会话] {url}")
                return True
        if not rolled_top:
            # 第一次没找到先滚回顶部（新会话在列表顶部），再扫一轮
            rolled_top = True
            try:
                page.locator('nav').first.hover()
                page.mouse.wheel(0, -5000)
            except Exception:
                try:
                    page.mouse.wheel(0, -5000)
                except Exception:
                    pass
        else:
            try:
                page.locator('nav').first.hover()
                page.mouse.wheel(0, 2500)
            except Exception:
                try:
                    page.mouse.wheel(0, 2500)
                except Exception:
                    pass
        page.wait_for_timeout(1200)
    return False


def do_login(headless=False, wait=300):
    with sync_playwright() as p:
        ctx = launch(p, headless=headless)
        page = ctx.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        _stdout(f"浏览器已打开，请在窗口里登录 ChatGPT（最多等待 {wait} 秒）。")
        deadline = time.time() + wait
        logged = False
        while time.time() < deadline:
            if is_logged_in(page) and "auth.openai.com" not in page.url:
                logged = True
                break
            page.wait_for_timeout(2000)
        if logged:
            _stdout("检测到已登录，会话已保存到 ./gpt_profile。")
        else:
            _stdout("警告：等待超时，未确认登录状态。可重试 python gpt_web.py login。")
        ctx.close()
        return 0 if logged else 1


def do_ask(question, timeout, headless=False, conversation=None, minimized=False, session_key=None):
    if not question.strip():
        print("[错误] 问题为空。", file=sys.stderr)
        return 2
    with sync_playwright() as p:
        ctx = launch(p, headless=headless, minimized=minimized)
        page = ctx.new_page()
        # "后台最小化"：页面建好后用 CDP 再最小化一次（双保险；窗口在
        # 启动时已被 --start-minimized 最小化，正常不会闪现）。
        if minimized and not headless:
            _minimize_window(page)
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        # 等待可能的跳转/登录页
        try:
            page.wait_for_url(lambda u: "chatgpt.com" in u, timeout=20000)
        except Exception:
            pass
        if not is_logged_in(page):
            print("[错误] 未登录。请先运行：python gpt_web.py login", file=sys.stderr)
            ctx.close()
            return 3

        # 会话选择：指定名称 > 本 DSH 会话的固定 GPT 会话（session-key 映射）> 新建
        opened_existing = False
        session_url = None
        if conversation and conversation.strip():
            opened_existing = open_conversation(page, conversation)
            if opened_existing:
                _stdout(f"[已打开指定会话：{conversation}]")
            else:
                # 保守模式：找不到指定会话就报错退出，绝不新建、绝不误发
                print(f"[错误] 未找到会话「{conversation}」。可见会话如下：", file=sys.stderr)
                try:
                    for t, href in session_links(page, limit=40):
                        print(f"  {t}\t{href}", file=sys.stderr)
                except Exception:
                    pass
                ctx.close()
                return 6
        elif session_key:
            # 本 DSH 会话固定 GPT 会话：第一次新建并记录 URL，之后复用
            session_url = _session_map_get(session_key)
            if session_url:
                opened_existing = open_conversation_by_url(page, session_url)
                if opened_existing:
                    _stdout(f"[已打开本会话的 GPT 会话：{session_url}]")
                else:
                    # 记录失效（会话被删/移出侧边栏）→ 新建并重新记录
                    print(f"[警告] 记录的会话 {session_url} 未找到，重新新建", file=sys.stderr)
                    _session_map_del(session_key)
                    new_chat(page)
            else:
                new_chat(page)
        else:
            new_chat(page)

        # 会话页加载后输入框可能稍后才渲染，轮询等待最多 25 秒
        inp = None
        find_deadline = time.time() + 25
        while time.time() < find_deadline and inp is None:
            inp = find_input(page)
            if inp is None:
                page.wait_for_timeout(1500)
        if inp is None:
            print(f"[错误] 找不到输入框。当前 URL: {page.url}，标题: {page.title()}", file=sys.stderr)
            ctx.close()
            return 4

        # 长会话页面可能持续轻微抖动，force 点击仅用于聚焦输入框
        inp.click(force=True)
        inp.fill(question)
        page.keyboard.press("Enter")
        _stdout("[已发送，等待回复…]")
        reply = wait_for_reply(page, timeout)
        if not reply:
            print("[警告] 超时未获取到回复。", file=sys.stderr)
            ctx.close()
            return 5
        # 记录本 DSH 会话的 GPT 会话 URL（新会话发送后 URL 才出现 /c/<uuid>）
        if session_key and not opened_existing:
            cur = page.url
            if "/c/" in cur:
                _session_map_set(session_key, cur)
                _stdout(f"[已记录会话] {cur}")
        _stdout(reply)
        ctx.close()
        return 0


def main():
    parser = argparse.ArgumentParser(description="网页端 ChatGPT 收发消息工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="打开浏览器手动登录 ChatGPT")
    p_login.add_argument("--headless", action="store_true")
    p_login.add_argument("--wait", type=int, default=300, help="等待登录的秒数")

    p_ask = sub.add_parser("ask", help="发消息并读取回复")
    p_ask.add_argument("question", help="要问的问题")
    p_ask.add_argument("--timeout", type=int, default=180, help="等待回复超时秒数")
    p_ask.add_argument("--headless", action="store_true")
    p_ask.add_argument("--conversation", default="", help="在指定名称的 GPT 会话里沟通（不存在则报错退出，绝不新建）")
    p_ask.add_argument("--session-key", default="", help="DSH 会话键：本 DSH 会话第一次询问时新建 GPT 会话并记录，之后复用同一会话")
    p_ask.add_argument("--minimized", action="store_true", help="浏览器窗口最小化在后台工作，不弹出干扰（与 --headless 互斥，headless 优先）")

    args = parser.parse_args()
    if args.cmd == "login":
        sys.exit(do_login(headless=args.headless, wait=args.wait))
    else:
        sys.exit(do_ask(args.question, args.timeout, headless=args.headless,
                        conversation=args.conversation, minimized=args.minimized,
                        session_key=args.session_key))


if __name__ == "__main__":
    # Windows 控制台统一 UTF-8 输出，避免中文乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
