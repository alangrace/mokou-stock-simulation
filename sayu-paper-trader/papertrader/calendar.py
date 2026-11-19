"""A 股交易日历与交易时段（Asia/Shanghai）。

复刻 SayuStock 的交易日守卫语义：非交易日 / 非 09:30-11:30 / 13:00-15:00
一律不允许撮合。这里用内置节假日表（与仓库 trading_calendar.py 的 2026 表一致），
离线可用；生产版应像原项目一样用上证指数行情自证。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import TZ

# 2026 年 A 股休市日（与 SayuStock trading_calendar.py 静态兜底表一致）
HOLIDAYS_2026: frozenset[str] = frozenset(
    {
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
        "2026-02-20", "2026-02-23",
        "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    }
)

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

PHASE_LABELS = {
    "holiday": "休市日",
    "weekend": "周末",
    "pre_open": "开盘前",
    "morning": "上午盘",
    "lunch": "午休",
    "afternoon": "下午盘",
    "post_close": "收盘后",
}


@dataclass(frozen=True, slots=True)
class SessionState:
    now: datetime
    trading_day: bool
    is_open: bool
    phase: str
    label: str
    next_open: datetime | None

    def as_dict(self) -> dict:
        return {
            "now": self.now.isoformat(),
            "trading_day": self.trading_day,
            "is_open": self.is_open,
            "phase": self.phase,
            "label": self.label,
            "next_open": self.next_open.isoformat() if self.next_open else None,
        }


def ensure_tz(dt: datetime) -> datetime:
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def holiday_name(d: date) -> str | None:
    key = d.isoformat()
    return "法定休市" if key in HOLIDAYS_2026 else None


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS_2026


def phase_of(dt: datetime) -> str:
    dt = ensure_tz(dt)
    if not is_trading_day(dt.date()):
        return "holiday" if dt.weekday() < 5 else "weekend"
    t = dt.time()
    if t < MORNING_OPEN:
        return "pre_open"
    if t <= MORNING_CLOSE:
        return "morning"
    if t < AFTERNOON_OPEN:
        return "lunch"
    if t <= AFTERNOON_CLOSE:
        return "afternoon"
    return "post_close"


def is_open(dt: datetime | None = None) -> bool:
    dt = ensure_tz(dt or datetime.now(TZ))
    return phase_of(dt) in ("morning", "afternoon")


def next_open(dt: datetime | None = None) -> datetime | None:
    dt = ensure_tz(dt or datetime.now(TZ))
    cursor = dt
    for _ in range(40):
        day = cursor.date()
        if is_trading_day(day):
            for hh, mm in ((9, 30), (13, 0)):
                candidate = datetime.combine(day, time(hh, mm), tzinfo=TZ)
                if candidate > dt:
                    return candidate
        cursor = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=TZ)
    return None


def session_state(dt: datetime | None = None) -> SessionState:
    now = ensure_tz(dt or datetime.now(TZ))
    phase = phase_of(now)
    trading = is_trading_day(now.date())
    return SessionState(
        now=now,
        trading_day=trading,
        is_open=phase in ("morning", "afternoon"),
        phase=phase,
        label=PHASE_LABELS.get(phase, phase),
        next_open=next_open(now),
    )
