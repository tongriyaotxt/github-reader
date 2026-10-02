"""控制台兼容工具。

Windows 上 Python 的 stdout 默认跟随系统代码页（中文系统是 GBK），
打印 ⚠ 或某些 Unicode 字符会直接抛 UnicodeEncodeError 让脚本崩溃。
实测踩到过，所以所有入口都先调用一次。

同时管一个可选的 ANSI 颜色开关——重定向到文件时自动关闭，
避免把转义序列写进日报里。
"""

from __future__ import annotations

import os
import sys

_COLOR_ENABLED: bool | None = None


def setup_console() -> None:
    """把 stdout/stderr 切到 UTF-8，并容忍无法编码的字符。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # errors="replace" 是关键：宁可显示成 ? 也不要让整个脚本崩掉
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    if os.name == "nt":
        # 让 Windows 终端理解 ANSI 转义序列
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def color_enabled() -> bool:
    global _COLOR_ENABLED
    if _COLOR_ENABLED is None:
        _COLOR_ENABLED = (
            sys.stdout.isatty()
            and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb"
        )
    return _COLOR_ENABLED


def paint(text: str, code: str) -> str:
    """给文本上色；不支持颜色时原样返回。"""
    if not color_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return paint(text, "2")


def bold(text: str) -> str:
    return paint(text, "1")


def cyan(text: str) -> str:
    return paint(text, "36")


def yellow(text: str) -> str:
    return paint(text, "33")


def green(text: str) -> str:
    return paint(text, "32")
