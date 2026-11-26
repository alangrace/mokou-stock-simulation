"""运行开关（默认关闭）。

优先级：

1. 环境变量 ``SAYU_AGENT_TRADING``（``1`` / ``0`` / true / false）——显式设置时以它为准；
2. ``data/agent_trading.flag`` 文件——未设环境变量时读它。

为什么要落盘一个文件：DSH 定时任务的每次心跳都是全新 shell，不会继承
用户终端里 ``export`` 的变量。只靠环境变量，定时任务永远看到"关闭"，
开关等于没开。
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import DB_PATH

FLAG_NAME = "agent_trading.flag"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def flag_path() -> Path:
    """与账本放同一目录：测试改 SAYU_PAPER_DB 时开关也跟着隔离。"""
    return DB_PATH.parent / FLAG_NAME


def _parse(raw: str) -> bool | None:
    value = str(raw).strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def agent_trading_enabled() -> bool:
    """开关状态。环境变量优先，其次看 flag 文件。"""
    env = os.environ.get("SAYU_AGENT_TRADING")
    if env is not None:
        parsed = _parse(env)
        if parsed is not None:
            return parsed
    path = flag_path()
    if not path.exists():
        return False
    return _parse(path.read_text(encoding="utf-8")) is True


def set_agent_trading(enabled: bool) -> Path:
    """写/删 flag 文件，返回 flag 路径。"""
    path = flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        path.write_text("1\n", encoding="utf-8")
    elif path.exists():
        path.unlink()
    return path


def switch_status() -> dict:
    path = flag_path()
    env = os.environ.get("SAYU_AGENT_TRADING")
    return {
        "enabled": agent_trading_enabled(),
        "env": env if env is not None else "",
        "env_effective": _parse(env) if env is not None else None,
        "flag_path": str(path),
        "flag_exists": path.exists(),
    }
