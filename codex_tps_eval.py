#!/usr/bin/env python3
"""用 Krill 的 OpenAI-compatible 接口跑 5 个问题，测量输出 TPS。

    python codex_tps_eval.py -m gpt-5.5 -r high
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import unicodedata

try:
    from openai import OpenAI
except ImportError as exc:  # ponytail: 直接报依赖缺失，避免再猜 HTTP 细节
    raise SystemExit("缺少 openai 包，请先运行: pip install openai") from exc

AUTH_FILE = os.path.expanduser(r"~/.codex/auth.json")
API_BASE_URL = "https://api.krill-ai.com/codex/v1"

# 统一的前缀：明确禁止读写文件 / 调用工具，要求把完整解答直接写进回答里。
PROMPT_PREFIX = (
    "不使用任何外部工具，也不要读取、创建或修改任何文件，直接在回答中给出完整、"
    "详细的解答（包含完整可运行代码、复杂度分析以及边界情况讨论）：\n\n"
)

# 5 个不同的问题：都偏 coding agent 场景，且解答需要较大的推理 + 输出 token 量，
# 但本身是“纯思考 + 纯输出”，不依赖文件系统或网络。
QUESTIONS = [
    "用 Python 从零实现一个线程安全、支持 TTL 过期和 LRU 淘汰的内存缓存（不依赖第三方库）。"
    "要求：给出完整类实现、各操作的时间复杂度、并发安全性分析，以及过期与淘汰同时发生时的"
    "边界处理。",

    "用 Python 从零实现一个算术表达式求值器，支持 + - * /、括号、一元负号、幂运算 ** 以及"
    "形如 max(a, b)、sin(x) 的函数调用。要求：手写词法分析器、递归下降解析器（生成 AST）"
    "和求值器，给出完整代码、文法定义和针对运算符优先级/结合性的测试用例。",

    "用 Python 设计并实现一个限流器，同时给出令牌桶（token bucket）和滑动窗口日志"
    "（sliding window log）两种算法，支持按用户 ID 独立限流。要求：完整代码、并发安全性"
    "分析，以及两种算法在突发流量、内存占用、精确度上的取舍对比。",

    "用 Python 从零实现一个 JSON 解析器（禁止使用标准库 json 模块），手写递归下降解析器，"
    "支持对象、数组、带转义和 Unicode 的字符串、整数/浮点/科学计数法数字、true/false/null。"
    "要求：完整代码、清晰的错误定位（行列号）以及覆盖各种非法输入的测试。",

    "用 Python 实现一个基于有向图的任务调度器：支持依赖声明（DAG）、拓扑排序、循环依赖检测、"
    "可并行执行的批次划分、以及失败任务的指数退避重试。要求：完整代码、核心算法说明、"
    "复杂度分析，以及对存在环或重复依赖等异常情况的处理。",
]


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


def run_codex(prompt: str, model: str | None, effort: str):
    client = _make_client()
    completion = client.chat.completions.create(
        model=model or "gpt-5.5",
        messages=[{"role": "user", "content": prompt}],
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


def preview(text: str, limit: int = 24) -> str:
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
    args = parser.parse_args()

    headers = ["Run", "Question", "Answer", "In Tok", "Out Tok", "Reason Tok", "Time(s)", "TPS"]
    aligns = ["right", "left", "left", "right", "right", "right", "right", "right"]

    def run_one(index: int, question: str) -> tuple[list, float | None]:
        q_preview = preview(question)
        try:
            start = time.perf_counter()
            text, in_tok, out_tok, rea_tok = run_codex(
                PROMPT_PREFIX + question, args.model, args.reasoning_effort)
            elapsed = time.perf_counter() - start
            tps = out_tok / elapsed if out_tok and elapsed > 0 else None
            return [index, q_preview, preview(text), in_tok, out_tok, rea_tok,
                    f"{elapsed:.1f}", f"{tps:.1f}" if tps else "-"], tps
        except Exception as exc:
            return [index, q_preview, f"ERROR: {preview(str(exc))}", *["-"] * 5], None

    rows = []
    tps_values = []
    if use_ansi:
        print("\033[s", end="", flush=True)
    for index, question in enumerate(QUESTIONS, start=1):
        row, tps = run_one(index, question)
        rows.append(row)
        if tps is not None:
            tps_values.append(tps)
        if use_ansi:
            print("\033[u\033[J", end="")
            print(render_table(headers, rows, aligns), flush=True)
    if not use_ansi:
        print(render_table(headers, rows, aligns), flush=True)

    if tps_values:
        avg = sum(tps_values) / len(tps_values)
        print(f"\nMeasured {len(tps_values)}/{len(QUESTIONS)} runs  avg TPS = {avg:.1f} tok/s")
    else:
        print(f"\nMeasured 0/{len(QUESTIONS)} runs")


if __name__ == "__main__":
    main()
