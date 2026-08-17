#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 GPT 回复中提取任务卡并保存为 .md 文件。

输入：GPT 回复文件（或 _codeblocks.txt）。
提取优先级：
  1) ===TASK_CARD_START=== ... ===TASK_CARD_END=== 标记之间的内容（含代码围栏）
  2) ```markdown 围栏中标题含"任务卡"的代码块
  3) 从 "NEXT TASK" / "任务卡" 标题截取到文件尾（fallback，沿用旧方案）

用法: python extract_task_card.py <reply_file> [out_dir]
输出: out_dir/<标题>.md  （默认 out_dir = C:\\Users\\cruelworld\\Desktop\\DeepSeek\\类DND游戏\\任务卡）
"""
import os
import re
import sys

DEFAULT_OUT = r"C:\Users\cruelworld\Desktop\DeepSeek\类DND游戏\任务卡"


def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\r\n]', "", name).strip()


def extract_marked(text):
    """===TASK_CARD_START=== 与 ===TASK_CARD_END=== 之间"""
    m = re.search(r"===TASK_CARD_START===\s*(.*?)\s*===TASK_CARD_END===", text, re.S)
    if m:
        return m.group(1).strip()
    return None


def extract_fence(text):
    """```markdown ... ``` 或 ``` ... ``` 围栏中标题含 '任务卡' 的块"""
    blocks = re.findall(r"```(?:markdown|md)?\s*\n(.*?)```", text, re.S)
    for b in blocks:
        first = b.strip().splitlines()[0] if b.strip() else ""
        if "任务卡" in first:
            return b.strip()
    return None


def extract_fallback(text):
    """旧方案：从 'NEXT TASK' 或含 '任务卡' 的标题截取到文件尾"""
    for marker in ["NEXT TASK", "## 任务卡", "# V0.", "TASK — V0"]:
        i = text.find(marker)
        if i >= 0:
            return text[i:].strip()
    return None


def main():
    if len(sys.argv) < 2:
        print("用法: python extract_task_card.py <reply_file> [out_dir]")
        return 2
    src = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    card = extract_marked(text) or extract_fence(text) or extract_fallback(text)
    if not card:
        print("[未找到] 回复中没有任务卡标记/代码围栏/任务卡标题。", file=sys.stderr)
        return 1

    # 文件名取第一个 # 标题（去掉 markdown 符号），否则用日期
    title = None
    for line in card.splitlines():
        if line.startswith("#"):
            title = line.lstrip("# ").strip()
            break
    if not title:
        import datetime
        title = f"任务卡-{datetime.date.today().isoformat()}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{sanitize(title)}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(card)
    print(f"[已保存] {out_path} ({len(card)} 字符)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
