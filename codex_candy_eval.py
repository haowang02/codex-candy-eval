#!/usr/bin/env python3
"""用本地 codex CLI 测试糖果问题
    python codex_candy_eval.py -m gpt-6.1-sol -r low -n 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

CODEX_PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""

# 正确答案为 21：只要回答中出现独立的 "21"（前后非数字）即判为正确。
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")

# (表头, 宽度, 是否右对齐)；Codex 列宽度为 None，由 column_widths() 按终端宽度决定。
COLUMNS = [
    ("Run", 3, True), ("Codex", None, False), ("In Tok", 6, True), ("Out Tok", 7, True),
    ("Reason Tok", 10, True), ("Time(s)", 7, True), ("TPS", 6, True), ("OK", 2, False),
]
GAP = "  "
CODEX_MIN, CODEX_MAX = 24, 40

# 依次去掉：代码围栏、标题和引用、列表标记、链接和图片、加粗和删除线、斜体、行内代码。
MARKDOWN_PATTERNS = [
    (r"```[^\n]*\n?", ""),
    (r"(?m)^\s{0,3}(?:#{1,6}|>)\s*", ""),
    (r"(?m)^\s*(?:[-*+]|\d+[.)])\s+", ""),
    (r"!?\[([^\]]*)\]\([^)]*\)", r"\1"),
    (r"(\*\*|__|~~)(.+?)\1", r"\2"),
    (r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"\1"),
    (r"`+([^`]*)`+", r"\1"),
]


def resolve_codex_executable() -> str:
    """找到可被 subprocess 直接启动的 codex 命令。

    Windows/npm 安装通常会同时生成 `codex`、`codex.cmd` 和 `codex.ps1`。
    `shutil.which("codex")` 可能先命中无扩展名 shim，CreateProcess 无法直接运行，
    会报 WinError 193；因此 Windows 上优先选择 `.cmd`。
    """
    candidates = (
        ("codex.cmd", "codex.exe", "codex")
        if os.name == "nt"
        else ("codex",)
    )
    for name in candidates:
        exe = shutil.which(name)
        if exe:
            return exe
    raise RuntimeError("找不到 codex 可执行文件，请确认已安装并加入 PATH。")


def run_codex(model: str | None, effort: str):
    exe = resolve_codex_executable()

    cmd = [
        exe, "exec", "--json",
        "--skip-git-repo-check",
        "-s", "read-only",
        # 关闭 codex 的跨会话记忆（~/.codex/memories），避免历史记忆注入提示词、污染
        # 评测结果，保证不同机器/不同记忆状态下结果可复现。等价于 -c features.memories=false。
        "--disable", "memories",
        "-c", f"model_reasoning_effort={effort}",
        "-c", "notify=[]",
    ]
    if model:
        cmd += ["-m", model]

    # 多行题目通过 stdin 传入：作为命令行参数时，经 cmd.exe/codex.cmd 包装后换行会被
    # 吞掉，而管道里的内容能完整保留。codex exec 在无位置参数且 stdin 非 TTY 时读 stdin。
    proc = subprocess.run(
        cmd,
        input=CODEX_PROMPT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "codex exec failed")

    # codex --json emits one JSON event per line. The final answer is the last
    # `agent_message` item; token usage (incl. reasoning) is in `turn.completed`.
    final_text = ""
    thread_id = None
    usage: dict = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        elif event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                final_text = item.get("text", final_text)
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}

    return final_text, usage, thread_id


def request_seconds(thread_id: str | None) -> float | None:
    """模型请求耗时：session 里 turn_context（发请求前写入）到最后一条 token_usage_record。

    不含 codex 启动等开销；找不到 session 或没有这两个事件时返回 None。
    """
    if not thread_id:
        return None
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    stamps = {"turn_context": [], "token_usage_record": []}
    for path in (home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["type"] in stamps:
                stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                stamps[event["type"]].append(stamp.timestamp())
    start, end = stamps["turn_context"], stamps["token_usage_record"]
    return end[-1] - start[0] if start and end else None


def char_width(char: str) -> int:
    """终端显示宽度：组合字符 0，东亚全角/宽字符 2，其余 1。"""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def pad(text: str, width: int, right: bool) -> str:
    """按显示宽度补空格对齐（中文宽字符按 2 计）。"""
    fill = " " * (width - display_width(text))
    return fill + text if right else text + fill


def column_widths() -> list[int]:
    """各列宽度，0 表示不显示。

    Codex 列占用终端剩余宽度（至多 CODEX_MAX），不足 CODEX_MIN 时隐藏，避免整行折行。
    留 1 列余量：部分终端写满最后一列会提前换行。输出被重定向时不限宽。
    """
    fixed = sum(w for _, w, _ in COLUMNS if w) + len(GAP) * (len(COLUMNS) - 1)
    codex = CODEX_MAX
    if sys.stdout.isatty():
        codex = min(CODEX_MAX, shutil.get_terminal_size().columns - 1 - fixed)
    return [w or (codex if codex >= CODEX_MIN else 0) for _, w, _ in COLUMNS]


def format_row(cells: list, widths: list[int]) -> str:
    return GAP.join(
        pad(str(cell), width, right)
        for cell, width, (_, _, right) in zip(cells, widths, COLUMNS)
        if width
    ).rstrip()


def preview(text: str, limit: int) -> str:
    """去掉 markdown 语法并折叠空白，按显示宽度截断。"""
    for pattern, repl in MARKDOWN_PATTERNS:
        text = re.sub(pattern, repl, text)
    text = " ".join(text.split())
    if display_width(text) <= limit:
        return text

    kept, width = "", 0
    for char in text:
        width += char_width(char)
        if width > limit - 3:
            break
        kept += char
    return kept.rstrip() + "..."


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-m", "--model", help="Codex model name; omit for the local default.")
    parser.add_argument(
        "-r", "--reasoning-effort", default="medium",
        choices=["low", "medium", "high", "xhigh", "max", "ultra"],
    )
    parser.add_argument("-n", "--tests", type=int, default=1)
    args = parser.parse_args()

    widths = column_widths()
    errors = []

    def run_one(index: int) -> tuple[list, bool | None]:
        try:
            text, usage, thread_id = run_codex(args.model, args.reasoning_effort)
            seconds = request_seconds(thread_id)
            out_tok = usage.get("output_tokens")
            tps = out_tok / seconds if out_tok and seconds else None
            ok = bool(ANSWER_PATTERN.search(text))
            return [index, preview(text, widths[1]), usage.get("input_tokens"), out_tok,
                    usage.get("reasoning_output_tokens"), f"{seconds:.1f}" if seconds else "-",
                    f"{tps:.1f}" if tps else "-", "✓" if ok else "✗"], ok
        except Exception as exc:
            errors.append(f"Run {index}: {' '.join(str(exc).split())}")
            return [index, "ERROR", *["-"] * 6], None

    print(format_row([name for name, _, _ in COLUMNS], widths))
    print(GAP.join("-" * w for w in widths if w))

    # 串行执行，完成一个立即打印一行；终端里先显示占位行，完成后用 \r 回到行首覆盖。
    tty = sys.stdout.isatty()
    graded = []
    for index in range(1, args.tests + 1):
        if tty:
            print(f"{index:>{widths[0]}}{GAP}running...", end="", flush=True)
        row, ok = run_one(index)
        if ok is not None:
            graded.append(ok)
        print(("\r" if tty else "") + format_row(row, widths), flush=True)
    for error in errors:
        print(error)

    correct = sum(graded)
    print(f"\nGraded {len(graded)}/{args.tests}  correct={correct}  "
          f"accuracy={correct / len(graded) * 100:.1f}%"
          if graded else f"\nGraded 0/{args.tests}")


if __name__ == "__main__":
    main()
