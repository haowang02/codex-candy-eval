#!/usr/bin/env python3
"""用本地 codex CLI 测试糖果问题，统计 reasoning tokens 并判分。
    python codex_candy_eval.py -m gpt-5.5 -r high -n 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

CODEX_PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""

# 正确答案为 21：只要回答中出现独立的 "21"（前后非数字）即判为正确。
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")


def run_codex(model: str | None, effort: str):
    # Windows 上 codex 多是 npm 安装的 codex.cmd 包装脚本，裸名字 CreateProcess 找
    # 不到（PATH 搜索只补 .exe），用 shutil.which 解析出带扩展名的完整路径再执行。
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("找不到 codex 可执行文件，请确认已安装并加入 PATH。")

    cmd = [
        exe, "exec", "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s", "read-only",
        # 关闭 codex 的跨会话记忆（~/.codex/memories），避免历史记忆注入提示词、污染
        # 评测结果，保证不同机器/不同记忆状态下结果可复现。等价于 -c features.memories=false。
        "--disable", "memories",
        "-c", f"model_reasoning_effort={effort}",
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
    usage: dict = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                final_text = item.get("text", final_text)
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}

    return (
        final_text,
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("reasoning_output_tokens"),
    )


def char_width(char: str) -> int:
    """终端显示宽度：组合字符 0，东亚全角/宽字符 2，其余 1。"""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def pad(text: str, width: int, align: str) -> str:
    """按显示宽度补空格对齐（中文宽字符按 2 计）。"""
    gap = width - display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def render_table(headers: list[str], rows: list[list], aligns: list[str]) -> str:
    """原生渲染对齐表格（tabulate "simple" 风格），列宽按显示宽度计算。"""
    str_rows = [[str(c) for c in row] for row in rows]
    widths = [
        max(display_width(headers[i]), *(display_width(r[i]) for r in str_rows)) if str_rows
        else display_width(headers[i])
        for i in range(len(headers))
    ]

    def fmt(cells: list[str]) -> str:
        return "  ".join(pad(cells[i], widths[i], aligns[i]) for i in range(len(headers)))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in str_rows]
    return "\n".join(lines)


def preview(text: str, limit: int = 40) -> str:
    flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")
    if display_width(flat) <= limit:
        return flat

    result = []
    width = 0
    for char in flat:
        next_width = char_width(char)
        if width + next_width > limit - 3:
            break
        result.append(char)
        width += next_width
    return "".join(result) + "..."


def _enable_windows_ansi() -> bool:
    """开启 Windows 控制台的 VT 处理，让 ANSI 转义序列（含光标定位）生效。"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except Exception:
        return False


def setup_console() -> bool:
    """统一输出为 UTF-8（避免 ✓/✗、中文在 GBK 等控制台编码下抛 UnicodeEncodeError），
    并探测是否可用 ANSI 光标控制做表格原地刷新。

    返回 True 表示可原地重绘；否则（如输出被重定向、或旧版 Windows 无法开 VT）退化为
    结束后一次性打印整张表，避免屏幕上出现 `←[s` 之类的转义乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        return _enable_windows_ansi()
    return True


def main() -> None:
    use_ansi = setup_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-m", "--model", help="Codex model name; omit for the local default.")
    parser.add_argument(
        "-r", "--reasoning-effort", default="medium",
        choices=["low", "medium", "high", "xhigh"],
    )
    parser.add_argument("-n", "--tests", type=int, default=1)
    args = parser.parse_args()

    headers = ["Run", "Codex", "In Tok", "Out Tok", "Reason Tok", "Time(s)", "TPS", "OK"]
    aligns = ["right", "left", "right", "right", "right", "right", "right", "center"]

    def run_one(index: int) -> tuple[list, bool | None]:
        try:
            start = time.perf_counter()
            text, in_tok, out_tok, rea_tok = run_codex(args.model, args.reasoning_effort)
            elapsed = time.perf_counter() - start
            tps = out_tok / elapsed if out_tok and elapsed > 0 else None
            ok = bool(ANSWER_PATTERN.search(text))
            return [index, preview(text), in_tok, out_tok, rea_tok, f"{elapsed:.1f}",
                    f"{tps:.1f}" if tps else "-", "✓" if ok else "✗"], ok
        except Exception as exc:
            return [index, f"ERROR: {preview(str(exc))}", *["-"] * 6], None

    # 串行执行：逐个请求，完成一个立即打印该行结果。
    rows = []
    graded = []
    prev_lines = 0  # 上一次绘制的表格占据的屏幕行数，用于原地重绘时上移光标
    for index in range(1, args.tests + 1):
        row, ok = run_one(index)
        rows.append(row)
        if ok is not None:
            graded.append(ok)
        if use_ansi:
            # 用“行数计数 + 光标上移（CSI A）”替代 save/restore（CSI s/u）。
            # macOS Terminal.app 不支持 CSI s/u，会导致表格每轮向下堆叠、表头重复；
            # 光标上移序列所有常见终端都支持，最稳妥。
            if prev_lines > 0:
                sys.stdout.write(f"\033[{prev_lines}A\033[J")
            table = render_table(headers, rows, aligns)
            sys.stdout.write(table + "\n")
            sys.stdout.flush()
            prev_lines = table.count("\n") + 1
    if not use_ansi:
        print(render_table(headers, rows, aligns), flush=True)

    correct = sum(graded)
    print(f"\nGraded {len(graded)}/{args.tests}  correct={correct}  "
          f"accuracy={correct / len(graded) * 100:.1f}%"
          if graded else f"\nGraded 0/{args.tests}")


if __name__ == "__main__":
    main()
