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
import subprocess
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[错误] 未安装 playwright，请先执行：python -m pip install playwright", file=sys.stderr)
    sys.exit(1)

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
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def launch(playwright, headless=False, minimized=False):
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
    ]
    # "后台最小化"：窗口先在屏幕外创建（--window-position 为负坐标，
    # 完全不可见、无闪现），随后 CDP 一次"移到屏幕中间并立即最小化"，
    # 使任务栏恢复位置在屏幕内可见。headless 无窗口，忽略 minimized。
    if minimized and not headless:
        args.append("--start-minimized")
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
    """通过 page 级 CDP 把浏览器窗口最小化（无 OS 依赖）。

    必须用 page 的 CDP session：browser 级 session 在尚无页面时调
    Browser.getWindowForTarget 会报 "No web contents in the target"。

    顺序必须是"先设位置再最小化"（实测反向会让恢复位置回到启动坐标）：
    窗口在屏幕外启动，背靠背两条命令把它移到屏幕中间并立即最小化，
    毫秒级无渲染无感知；用户点任务栏图标恢复时窗口出现在屏幕中间。
    """
    try:
        cdp = page.context.new_cdp_session(page)
        target = cdp.send("Browser.getWindowForTarget")
        window_id = target["windowId"]
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"left": 100, "top": 100, "width": 1280, "height": 900, "windowState": "normal"},
        })
        cdp.send("Browser.setWindowBounds", {
            "windowId": window_id,
            "bounds": {"windowState": "minimized"},
        })
    except Exception as exc:
        # 最小化失败不致命：窗口照常弹出，功能不受影响
        print(f"[警告] 窗口最小化失败：{exc}", file=sys.stderr)


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


def wait_for_reply(page, timeout):
    """轮询最后一个助手消息，直到生成结束且文本稳定。返回回复文本。

    完成条件 = 没有"生成中"指示（Stop 按钮消失）且文本连续 2 秒不变，
    避免把流式停顿误判成完成。读取失败时不推进稳定计时，绝不因 DOM
    抖动提前返回。
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
        if el.count() > 0 and el.is_visible():
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
    """打开指定名称的会话：扫描侧边栏（title 子串匹配）+ href 精确定位点击。

    回归初始版本的策略——不依赖搜索框（UI 版本差异/时序都不稳定）：
    直接遍历侧边栏 `a[href*="/c/"]`，找到匹配项后用 href 二次定位再点击，
    并验证 URL 进入 /c/。找不到返回 False（由调用方决定新建/报错）。
    """
    name = name.strip()
    if not name:
        return False
    page.wait_for_timeout(2500)  # 等侧边栏渲染
    deadline = time.time() + 20
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


def do_ask(question, timeout, headless=False, conversation=None, minimized=False):
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

        opened_existing = False
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
        else:
            new_chat(page)

        inp = find_input(page)
        if inp is None:
            print(f"[错误] 找不到输入框。当前 URL: {page.url}，标题: {page.title()}", file=sys.stderr)
            ctx.close()
            return 4

        inp.click()
        inp.fill(question)
        page.keyboard.press("Enter")
        _stdout("[已发送，等待回复…]")
        reply = wait_for_reply(page, timeout)
        if not reply:
            print("[警告] 超时未获取到回复。", file=sys.stderr)
            ctx.close()
            return 5
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
    p_ask.add_argument("--minimized", action="store_true", help="浏览器窗口最小化在后台工作，不弹出干扰（与 --headless 互斥，headless 优先）")

    args = parser.parse_args()
    if args.cmd == "login":
        sys.exit(do_login(headless=args.headless, wait=args.wait))
    else:
        sys.exit(do_ask(args.question, args.timeout, headless=args.headless,
                        conversation=args.conversation, minimized=args.minimized))


if __name__ == "__main__":
    # Windows 控制台统一 UTF-8 输出，避免中文乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
