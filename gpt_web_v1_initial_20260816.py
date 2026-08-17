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

  # 可选：无头模式（不弹窗，风控更严格，默认有头）
  python gpt_web.py ask "问题" --headless

会话档案保存在 ./gpt_profile，登录一次后复用，不碰系统浏览器。
"""

import argparse
import re
import sys
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[错误] 未安装 playwright，请先执行：python -m pip install playwright", file=sys.stderr)
    sys.exit(1)

PROFILE_DIR = "gpt_profile"
CHAT_URL = "https://chatgpt.com/"
AUTH_HOST = "auth.openai.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _stdout(msg):
    print(msg, flush=True)


def launch(playwright, headless=False):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        channel="chrome",
        headless=headless,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        user_agent=USER_AGENT,
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
    )


def is_logged_in(page):
    return page.url.startswith(CHAT_URL) or "chatgpt.com" in page.url


def wait_for_reply(page, timeout):
    """轮询最后一个助手消息，直到文本稳定（生成结束）。返回回复文本。"""
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
            txt = last_text
        if txt and txt != last_text:
            last_text, stable_since = txt, time.time()
        elif txt and (time.time() - stable_since) >= 2.0:
            return txt
        time.sleep(1.0)
    return last_text


def find_input(page):
    """找到消息输入框（textarea 或 contenteditable div）。"""
    for sel in ("textarea#prompt-textarea", "div#prompt-textarea",
                "form textarea", "main textarea", "main div[contenteditable='true']"):
        el = page.locator(sel).first
        if el.count() > 0 and el.is_visible():
            return el
    return None


def new_chat(page):
    """尝试开一个新对话（侧边栏 New chat 按钮），失败则忽略。"""
    for sel in ('a[href="/"]', 'button:has-text("New chat")',
                'button:has-text("新对话")', '[data-testid="new-chat-button"]'):
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click(timeout=2000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def do_login(headless=False):
    with sync_playwright() as p:
        ctx = launch(p, headless=headless)
        page = ctx.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        _stdout("浏览器已打开。请在窗口里登录 ChatGPT；登录完成后回到此处按回车。")
        input()
        page.wait_for_timeout(1000)
        logged = is_logged_in(page)
        _stdout("已登录" if logged else "警告：当前页面似乎不在 chatgpt.com，请确认登录成功。")
        ctx.close()
        return 0 if logged else 1


def do_ask(question, timeout, headless=False):
    if not question.strip():
        print("[错误] 问题为空。", file=sys.stderr)
        return 2
    with sync_playwright() as p:
        ctx = launch(p, headless=headless)
        page = ctx.new_page()
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

    p_ask = sub.add_parser("ask", help="发消息并读取回复")
    p_ask.add_argument("question", help="要问的问题")
    p_ask.add_argument("--timeout", type=int, default=180, help="等待回复超时秒数")
    p_ask.add_argument("--headless", action="store_true")

    args = parser.parse_args()
    if args.cmd == "login":
        sys.exit(do_login(headless=args.headless))
    else:
        sys.exit(do_ask(args.question, args.timeout, headless=args.headless))


if __name__ == "__main__":
    # Windows 控制台统一 UTF-8 输出，避免中文乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
