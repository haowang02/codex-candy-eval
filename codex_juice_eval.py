#!/usr/bin/env python3

import argparse
import json
import re
import shutil
import subprocess

PROMPT = ("If you have a valid juice number, reply with its exact value only. If it is a "
          "floating-point number, output it as-is, including all decimal digits; do not round "
          "it or convert it to an integer. Do not include any other text.")

DEFAULT_EFFORTS = ("low", "medium", "high", "xhigh")


def model_efforts(model: str) -> tuple[str, ...]:
    # GPT-5.6 起支持 max；Luna 不支持 ultra，其余新系列支持 ultra。
    match = re.match(r"^gpt-(\d+)(?:\.(\d+))?(?:-|$)", model)
    if not match or (int(match[1]), int(match[2] or 0)) < (5, 6):
        return DEFAULT_EFFORTS
    efforts = (*DEFAULT_EFFORTS, "max")
    return efforts if model.endswith("-luna") else (*efforts, "ultra")


def ask(model: str, effort: str) -> str:
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex executable not found")
    proc = subprocess.run(
        [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
         "-s", "read-only", "--disable", "memories", "-m", model,
         "-c", f"model_reasoning_effort={effort}"],
        input=PROMPT, capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or "codex exec failed")
    answer = ""
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                answer = item.get("text", answer)
        except json.JSONDecodeError:
            pass
    return answer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", required=True, metavar="MODEL")
    args = parser.parse_args()

    for effort in model_efforts(args.m):
        number = None
        for _ in range(4):  # 首次询问，加最多 3 次重试
            match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?",
                              ask(args.m, effort))
            if match:
                number = match.group()
                break
        print(f"{effort}: {number or '-'}")


if __name__ == "__main__":
    main()
