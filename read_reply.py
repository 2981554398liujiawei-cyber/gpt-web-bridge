#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打开指定 GPT 会话，等待最后一条 assistant 回复稳定后输出（只读，不发送消息）"""
import sys
import time

sys.path.insert(0, ".")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402
from gpt_web_session import launch, is_logged_in, session_links, find_input  # noqa: E402

CHAT_URL = "https://chatgpt.com/"


def main():
    if len(sys.argv) < 4:
        print("用法: python read_reply.py <title> <timeout_sec> <reply_file>")
        return 2
    title = sys.argv[1]
    timeout = int(sys.argv[2])
    reply_file = sys.argv[3]

    with sync_playwright() as p:
        ctx = launch(p)
        page = ctx.new_page()
        page.goto(CHAT_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_url(lambda u: "chatgpt.com" in u, timeout=20000)
        except Exception:
            pass
        if not is_logged_in(page):
            print("[错误] 未登录", file=sys.stderr)
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
            print(f"[错误] 未找到会话 {title!r}", file=sys.stderr)
            ctx.close()
            return 6
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
            pass
        page.wait_for_timeout(3000)

        # 等待最后一条 assistant 消息稳定（生成结束）
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
        reply = msgs.nth(n - 1).inner_text(timeout=3000) if n > 0 else ""
        if not reply.strip():
            print("[警告] 无回复", file=sys.stderr)
            ctx.close()
            return 5
        with open(reply_file, "w", encoding="utf-8") as f:
            f.write(reply)
        print(reply, flush=True)
        ctx.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
