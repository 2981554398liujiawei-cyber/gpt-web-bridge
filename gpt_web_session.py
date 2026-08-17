#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gpt_web_session.py — 在既有 ChatGPT 会话（如"李跳跳工具分析"）中提问，绝不新建会话。

用法：
  python gpt_web_session.py list
  python gpt_web_session.py ask --title "李跳跳工具分析" "问题" [--timeout 300]

退出码：
  0 成功；2 空问题；3 未登录；4 找不到输入框；5 超时无回复；6 找不到目标会话（已打印可见会话列表）
"""

import argparse
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


def launch(playwright):
    return playwright.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        channel="chrome",
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        user_agent=USER_AGENT,
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
    )


def is_logged_in(page):
    return "chatgpt.com" in page.url


def session_links(page, limit=80):
    """返回侧边栏会话 [(title, href), ...]"""
    out = []
    links = page.locator('a[href*="/c/"]')
    n = links.count()
    for i in range(min(n, limit)):
        try:
            href = links.nth(i).get_attribute("href")
            text = links.nth(i).inner_text(timeout=1500) or ""
            title = text.strip().splitlines()[0] if text.strip() else ""
        except Exception:
            continue
        out.append((title, href or ""))
    return out


def find_input(page):
    for sel in ["textarea#prompt-textarea", "div#prompt-textarea",
                "form textarea", "main textarea", "main div[contenteditable='true']"]:
        el = page.locator(sel).first
        if el.count() > 0:
            try:
                if el.is_visible() and el.is_enabled():
                    return el
            except Exception:
                continue
    return None


def wait_for_generation(page, timeout):
    """发送后等待生成结束：stop 按钮出现再消失；回退文本稳定轮询。
    返回 (reply_text, code_blocks)：code_blocks 为最后一个 assistant 消息中
    所有 <pre><code> 代码块文本（保留原始 markdown 源码）。"""
    stop = page.locator('button[data-testid="stop-button"]')
    deadline = time.time() + 60
    started = False
    while time.time() < deadline:
        try:
            if stop.count() > 0 and stop.first.is_visible():
                started = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if started:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if stop.count() == 0 or not stop.first.is_visible():
                    break
            except Exception:
                break
            time.sleep(1.0)
        time.sleep(1.5)
    msgs = page.locator('[data-message-author-role="assistant"]')
    n = msgs.count()
    if n == 0:
        return "", []
    last = msgs.nth(n - 1)
    text = last.inner_text(timeout=3000)
    blocks = []
    try:
        codes = last.locator("pre code")
        for i in range(codes.count()):
            try:
                blocks.append(codes.nth(i).inner_text(timeout=1500))
            except Exception:
                continue
    except Exception:
        pass
    return text, blocks


def do_list():
    with sync_playwright() as p:
        ctx = launch(p)
        page = ctx.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_url(lambda u: "chatgpt.com" in u, timeout=20000)
        except Exception:
            pass
        if not is_logged_in(page):
            print("[错误] 未登录。请先运行：python gpt_web.py login", file=sys.stderr)
            ctx.close()
            return 3
        page.wait_for_timeout(4000)
        sessions = session_links(page)
        if not sessions:
            print("[警告] 侧边栏未发现会话链接（可能侧边栏未加载）。", file=sys.stderr)
        for title, href in sessions:
            print(f"{title}\t{href}")
        ctx.close()
        return 0


def do_ask(title, question, timeout):
    if not question.strip():
        print("[错误] 问题为空。", file=sys.stderr)
        return 2
    with sync_playwright() as p:
        ctx = launch(p)
        page = ctx.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_url(lambda u: "chatgpt.com" in u, timeout=20000)
        except Exception:
            pass
        if not is_logged_in(page):
            print("[错误] 未登录。请先运行：python gpt_web.py login", file=sys.stderr)
            ctx.close()
            return 3
        page.wait_for_timeout(4000)
        sessions = session_links(page)
        match = None
        for t, href in sessions:
            if title in t:
                match = (t, href)
                break
        if match is None:
            print(f"[错误] 未找到标题包含 {title!r} 的会话。可见会话如下：", file=sys.stderr)
            for t, href in sessions:
                print(f"{t}\t{href}", file=sys.stderr)
            ctx.close()
            return 6
        print(f"[已定位会话] {match[0]} {match[1]}", flush=True)
        links = page.locator('a[href*="/c/"]')
        for i in range(links.count()):
            try:
                if (links.nth(i).get_attribute("href") or "") == match[1]:
                    links.nth(i).click(timeout=5000)
                    break
            except Exception:
                continue
        try:
            page.wait_for_url(lambda u: "/c/" in u, timeout=30000)
        except Exception:
            print(f"[警告] 进入会话后 URL 未变为 /c/，当前: {page.url}", file=sys.stderr)
        page.wait_for_timeout(3000)
        inp = None
        for attempt in range(3):
            inp = find_input(page)
            if inp is not None:
                break
            print(f"[重试] 输入框不可用（disabled/未加载），刷新页面（{attempt + 1}/3）…", file=sys.stderr)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
        if inp is None:
            print(f"[错误] 找不到输入框。当前 URL: {page.url}", file=sys.stderr)
            ctx.close()
            return 4
        inp.click(timeout=10000)
        inp.fill(question)
        page.keyboard.press("Enter")
        print("[已发送，等待回复…]", flush=True)
        reply, code_blocks = wait_for_generation(page, timeout)
        if not reply.strip():
            print("[警告] 超时未获取到回复。", file=sys.stderr)
            ctx.close()
            return 5
        print(reply, flush=True)
        if code_blocks:
            # 代码块（含 markdown 围栏内容）写入同名 _codeblocks.txt 供脚本保存
            try:
                base = getattr(sys.modules["__main__"], "__file__", None)
            except Exception:
                base = None
            print(f"\n[代码块] 共 {len(code_blocks)} 个", flush=True)
            for i, b in enumerate(code_blocks):
                print(f"--- BLOCK {i + 1} ---\n{b}\n--- END BLOCK {i + 1} ---", flush=True)
        ctx.close()
        return 0


def main():
    parser = argparse.ArgumentParser(description="在既有 ChatGPT 会话中提问（不新建会话）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="列出侧边栏可见会话")
    p_ask = sub.add_parser("ask", help="在指定会话中提问")
    p_ask.add_argument("--title", required=True, help="会话标题子串，如：李跳跳工具分析")
    p_ask.add_argument("question", help="要问的问题")
    p_ask.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.cmd == "list":
        sys.exit(do_list())
    else:
        sys.exit(do_ask(args.title, args.question, args.timeout))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
