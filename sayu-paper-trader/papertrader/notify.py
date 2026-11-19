"""成交桌面通知（macOS）。

通知通道优先级：
1. terminal-notifier（独立 bundle id；若系统拒绝权限会自动跳过）
2. Tkinter 右上角浮层（无系统通知权限依赖，10 秒自动消失）——默认回退
3. AppleScript display dialog（居中模态框，仅强制 dialog 模式使用）
4. osascript display notification（非模态，但后台进程可能被系统静默丢弃）

环境变量：
- SAYU_PAPER_NOTIFY=0 关闭全部通知
- SAYU_PAPER_NOTIFY_MODE=auto|toast|dialog|notification
  auto（默认）= terminal-notifier -> 右上角 toast
  toast        = 只弹右上角浮层
  dialog       = 只弹居中对话框
  notification = terminal-notifier -> osascript 通知（不弹浮层）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def _mode() -> str:
    return os.environ.get("SAYU_PAPER_NOTIFY_MODE", "auto").strip().lower()


def enabled() -> bool:
    if os.environ.get("SAYU_PAPER_NOTIFY", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return _mode() != "off"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _terminal_notifier(title: str, subtitle: str, body: str) -> bool:
    notifier = shutil.which("terminal-notifier")
    if not notifier:
        return False
    try:
        result = subprocess.run(
            [
                notifier,
                "-title", title,
                "-subtitle", subtitle,
                "-message", body,
                "-sound", "Glass",
                "-group", "sayu-paper",
            ],
            capture_output=True,
            timeout=8,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _notification(title: str, subtitle: str, body: str) -> bool:
    script = (
        f'display notification "{_escape(body)}" '
        f'with title "{_escape(title)}" subtitle "{_escape(subtitle)}" sound name "Glass"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=8,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _toast(title: str, subtitle: str, body: str, seconds: int = 10) -> bool:
    """在右上角弹一个 Tkinter 浮层；独立进程，不阻塞交易主流程。"""
    python = sys.executable or shutil.which("python3.12")
    if not python:
        return False
    root = Path(__file__).resolve().parent.parent
    try:
        subprocess.Popen(
            [python, "-m", "papertrader.toast", title, subtitle, body, str(seconds)],
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def _dialog(title: str, subtitle: str, body: str, seconds: int = 12) -> bool:
    message = f"{subtitle}\n\n{body}"
    script = (
        f'display dialog "{_escape(message)}" with title "{_escape(title)}" '
        f'buttons {{"知道了"}} default button 1 giving up after {int(seconds)}'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=seconds + 5,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def send(title: str, subtitle: str, body: str) -> bool:
    if not enabled() or sys.platform != "darwin":
        return False
    mode = _mode()
    if mode in ("auto", "toast"):
        if mode == "auto" and _terminal_notifier(title, subtitle, body):
            return True
        return _toast(title, subtitle, body)
    if mode == "notification":
        if _terminal_notifier(title, subtitle, body):
            return True
        return _notification(title, subtitle, body)
    if mode == "dialog":
        return _dialog(title, subtitle, body)
    return False


def notify_fills(trades: Sequence[dict], account_name: str) -> None:
    """一轮有多笔成交时合并成一条通知，避免连续弹窗。"""
    if not trades:
        return
    lines: list[str] = []
    for t in trades[:4]:
        side = "买入" if t["side"] == "buy" else "卖出"
        emoji = "🟢" if t["side"] == "buy" else "🔴"
        line = f"{emoji} {side} {t['code']} {t['qty']}股 @{t['price']:.2f}"
        if t.get("realized_pnl"):
            line += f"（{t['realized_pnl']:+.2f}）"
        lines.append(line)
    if len(trades) > 4:
        lines.append(f"…另有 {len(trades) - 4} 笔，详见监控页")
    send(
        title="模拟盘成交",
        subtitle=f"{account_name} · {len(trades)} 笔",
        body="\n".join(lines),
    )
