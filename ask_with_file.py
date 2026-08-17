#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从文件读取问题，在指定 GPT 会话中提问，回复保存到文件并 UTF-8 打印。
支持 GPT 以代码围栏输出的 markdown（如任务卡）：代码块解析后写入
<reply_file> 旁的 _codeblocks.txt，供 extract_task_card.py 提取为 .md。"""
import sys
import io
import os
import re

sys.path.insert(0, ".")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from gpt_web_session import do_ask  # noqa: E402


def parse_blocks(out):
    """从 do_ask 的 stdout 中解析 --- BLOCK N --- ... --- END BLOCK N ---"""
    blocks = []
    parts = re.split(r"--- BLOCK \d+ ---\n", out)
    for seg in parts[1:]:
        end = seg.find("--- END BLOCK")
        if end >= 0:
            blocks.append(seg[:end].rstrip("\n"))
    return blocks


def main():
    if len(sys.argv) < 5:
        print("用法: python ask_with_file.py <title> <question_file> <timeout_sec> <reply_file>")
        return 2
    title = sys.argv[1]
    qfile = sys.argv[2]
    timeout = int(sys.argv[3])
    reply_file = sys.argv[4]
    with open(qfile, "r", encoding="utf-8") as f:
        question = f.read()

    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, buf
    try:
        code = do_ask(title, question, timeout)
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    out = buf.getvalue()
    with open(reply_file, "w", encoding="utf-8") as f:
        f.write(out)

    blocks = parse_blocks(out)
    if blocks:
        cb_file = os.path.splitext(reply_file)[0] + "_codeblocks.txt"
        with open(cb_file, "w", encoding="utf-8") as f:
            f.write("\n\n===== BLOCK SEPARATOR =====\n\n".join(blocks))
        print(f"[代码块] 已保存 {len(blocks)} 个到 {cb_file}", flush=True)

    print(out, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
