"""右上角浮层提示（Tkinter，无系统通知权限依赖）。

用法：
  python3.12 -m papertrader.toast "标题" "副标题" "正文" [秒数]
"""

from __future__ import annotations

import os
import sys
import tkinter as tk


def main(argv: list[str]) -> int:
    title = argv[1] if len(argv) > 1 else "模拟盘"
    subtitle = argv[2] if len(argv) > 2 else ""
    body = argv[3] if len(argv) > 3 else ""
    try:
        seconds = max(3, min(30, int(argv[4]))) if len(argv) > 4 else 10
    except ValueError:
        seconds = 10

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.97)
    root.configure(bg="#0f172a")

    width = 430
    height = min(250, 150 + 22 * max(0, body.count("\n")))
    screen_w = root.winfo_screenwidth()
    x = max(12, screen_w - width - 24)
    y = 44  # 菜单栏下方
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.update_idletasks()

    bg = "#0f172a"
    panel = tk.Frame(root, bg=bg, highlightbackground="#334155", highlightthickness=1)
    panel.pack(fill="both", expand=True)

    tk.Label(
        panel, text="● " + title, bg=bg, fg="#38bdf8",
        font=("PingFang SC", 12, "bold"), anchor="w",
    ).pack(fill="x", padx=14, pady=(12, 0))
    if subtitle:
        tk.Label(
            panel, text=subtitle, bg=bg, fg="#94a3b8",
            font=("PingFang SC", 10), anchor="w",
        ).pack(fill="x", padx=14)
    tk.Label(
        panel, text=body, bg=bg, fg="#e6edf7",
        font=("PingFang SC", 11), anchor="w", justify="left",
        wraplength=width - 28,
    ).pack(fill="both", expand=True, padx=14, pady=(8, 4))
    tk.Label(
        panel, text=f"{seconds}s 后自动关闭 · 点击任意处关闭",
        bg=bg, fg="#64748b", font=("PingFang SC", 9), anchor="e",
    ).pack(fill="x", padx=14, pady=(0, 8))

    for widget in (root, panel):
        widget.bind("<Button-1>", lambda _event: root.destroy())
    root.after(seconds * 1000, root.destroy)
    # macOS 下 pack 之后可能回到默认位置，再设一次并刷新
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.update_idletasks()
    root.attributes("-topmost", True)
    root.lift()
    if os.environ.get("SAYU_TOAST_DEBUG"):
        root.after(
            300,
            lambda: print(
                f"toast mapped={root.winfo_ismapped()} xy=({root.winfo_x()},{root.winfo_y()}) "
                f"wh=({root.winfo_width()},{root.winfo_height()})",
                flush=True,
            ),
        )
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
