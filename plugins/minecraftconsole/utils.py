"""辅助工具模块"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecOptions:
    command: str
    wait_ms: int
    explicit_wait: bool


def parse_command_args(event_message_str: str, command: str) -> str | None:
    """解析指令参数，并尽量保留参数原始空白。"""
    if event_message_str is None:
        return None

    s = event_message_str.strip()
    if not s:
        return None

    if s.startswith("／"):
        s = "/" + s[1:]

    cmd = command.strip().lstrip("/")
    cmd_lower = cmd.lower()

    if s[: len(cmd) + 1].lower() == f"/{cmd_lower}":
        rest = s[len(cmd) + 1 :].lstrip()
        return rest if rest else None

    if s[: len(cmd)].lower() == cmd_lower:
        rest = s[len(cmd) :].lstrip()
        return rest if rest else None

    return s


_WAIT_RE = re.compile(r"(?:^|\s)--t=(\d+)(ms|s)?(?=\s|$)", re.IGNORECASE)


def parse_exec_options(raw_args: str) -> ExecOptions:
    """解析执行参数。

    支持：
    - /mc-command list
    - /mc-command lp editor --t=5s
    - /mc-command say hi --t=500ms
    """
    text = (raw_args or "").strip()
    if not text:
        return ExecOptions(command="", wait_ms=0, explicit_wait=False)

    wait_ms = 0
    explicit_wait = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal wait_ms, explicit_wait
        explicit_wait = True
        value = int(match.group(1))
        unit = (match.group(2) or "s").lower()
        wait_ms = value if unit == "ms" else value * 1000
        return " "

    command = _WAIT_RE.sub(_replace, text)
    command = re.sub(r"\s+", " ", command).strip()
    return ExecOptions(command=command, wait_ms=max(0, wait_ms), explicit_wait=explicit_wait)


def truncate_text(text: str, max_len: int) -> str:
    if text is None:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n...（已截断，原长度 {len(text)} 字符）"
