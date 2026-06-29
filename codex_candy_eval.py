#!/usr/bin/env python3
"""用 Krill 的 OpenAI-compatible 接口测试糖果问题，统计 reasoning tokens 并判分。
    python codex_candy_eval.py -m gpt-5.5 -r high -n 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata

try:
    from openai import OpenAI
except ImportError as exc:  # ponytail: 直接报依赖缺失，避免再猜 HTTP 细节
    raise SystemExit("缺少 openai 包，请先运行: pip install openai") from exc

AUTH_FILE = os.path.expanduser(r"~/.codex/auth.json")
API_BASE_URL = "https://api.krill-ai.com/codex/v1"

CODEX_PROMPT = """不使用任何外部工具回答以下问题：

在一个黑色的袋子里放有三种口味的糖果，每种糖果有两种不同的形状（圆形和五角星形，不同的形状靠手感可以分辨）。现已知不同口味的糖和不同形状的数量统计如下表。参赛者需要在活动前决定摸出的糖果数目，那么，最少取出多少个糖果才能保证手中同时拥有不同形状的苹果味和桃子味的糖？（同时手中有圆形苹果味匹配五角星桃子味糖果，或者有圆形桃子味匹配五角星苹果味糖果都满足要求）

        苹果味  桃子味  西瓜味
圆形       7      9      8
五角星形   7      6      4
"""

# 正确答案为 21：只要回答中出现独立的 "21"（前后非数字）即判为正确。
ANSWER_PATTERN = re.compile(r"(?<!\d)21(?!\d)")


def _load_api_key() -> str:
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            auth = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到鉴权文件：{AUTH_FILE}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"鉴权文件不是合法 JSON：{AUTH_FILE}") from exc

    api_key = auth.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"鉴权文件缺少 OPENAI_API_KEY：{AUTH_FILE}")
    return api_key


def _make_client() -> OpenAI:
    return OpenAI(base_url=API_BASE_URL, api_key=_load_api_key())


def _extract_reasoning_tokens(usage) -> int | None:
    details = getattr(usage, "completion_tokens_details", None) or getattr(usage, "output_tokens_details", None)
    return getattr(details, "reasoning_tokens", None)


def run_codex(model: str | None, effort: str):
    client = _make_client()
    completion = client.chat.completions.create(
        model=model or "gpt-5.5",
        messages=[{"role": "user", "content": CODEX_PROMPT}],
        reasoning_effort=effort,
    )
    usage = completion.usage
    return (
        completion.choices[0].message.content or "",
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
        _extract_reasoning_tokens(usage),
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
    """统一输出为 UTF-8，并探测是否可用 ANSI 光标控制做表格原地刷新。"""
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
    parser.add_argument("-m", "--model", help="Model name; omit for gpt-5.5.")
    parser.add_argument(
        "-r", "--reasoning-effort", default="medium",
        choices=["low", "medium", "high", "xhigh"],
    )
    parser.add_argument("-n", "--runs", type=int, default=5, help="Number of repeated runs")
    args = parser.parse_args()

    headers = ["Run", "Answer", "Correct", "In Tok", "Out Tok", "Reason Tok", "Time(s)"]
    aligns = ["right", "left", "center", "right", "right", "right", "right"]

    def run_one(i: int) -> tuple[list, bool]:
        try:
            start = time.perf_counter()
            text, in_tok, out_tok, rea_tok = run_codex(args.model, args.reasoning_effort)
            elapsed = time.perf_counter() - start
            ok = bool(ANSWER_PATTERN.search(text))
            return [i, preview(text), "✓" if ok else "✗", in_tok, out_tok, rea_tok, f"{elapsed:.1f}"], ok
        except Exception as exc:
            return [i, f"ERROR: {preview(str(exc))}", "-", "-", "-", "-", "-"], False

    rows = []
    correct = 0
    if use_ansi:
        print("\033[s", end="", flush=True)
    for i in range(1, args.runs + 1):
        row, ok = run_one(i)
        rows.append(row)
        correct += int(ok)
        if use_ansi:
            print("\033[u\033[J", end="")
            print(render_table(headers, rows, aligns), flush=True)
    if not use_ansi:
        print(render_table(headers, rows, aligns), flush=True)

    print(f"\nScore: {correct}/{args.runs}")


if __name__ == "__main__":
    main()
