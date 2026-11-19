"""项目路径与常量。"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
LOG_DIR = ROOT / "logs"
PLAN_DIR = ROOT / "plans"
for _d in (DATA_DIR, REPORT_DIR, LOG_DIR, PLAN_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("SAYU_PAPER_DB", DATA_DIR / "paper.db"))
TZ = ZoneInfo("Asia/Shanghai")

# 与 SayuStock 默认盘一致
DEFAULT_ACCOUNT = "默认模拟盘"
DEFAULT_CASH = 1_000_000.0
DEFAULT_STRATEGY = "multi_factor"

# 交易成本（A 股）
COMMISSION_RATE = 0.00025
COMMISSION_MIN = 5.0
STAMP_TAX_RATE = 0.0005
LOT_SIZE = 100
