"""A 股撮合规则：整手、费率、涨跌停、T+1 与账户风控。

逐条对应 SayuStock ``matcher.py`` / ``trade_executor.py`` 的实现：
- 100 股整手，向下取整
- 佣金 max(成交额 × 0.025%, 5 元)，卖出印花税 0.05%
- 主板 ±10%、ST ±5%、科创/创业 ±20%、北交所 ±30%；
  实际涨跌幅达到名义幅度 90% 即按触及涨跌停拦截（涨停不追、跌停不割）
- T+1：当日买入股数当日不可卖
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import COMMISSION_MIN, COMMISSION_RATE, LOT_SIZE, STAMP_TAX_RATE

LIMIT_MAIN = 10.0
LIMIT_STAR = 20.0
LIMIT_BSE = 30.0
LIMIT_ST = 5.0
LIMIT_BLOCK_RATIO = 0.9


@dataclass(frozen=True, slots=True)
class FeeBreakdown:
    commission: float
    stamp_tax: float
    total: float


@dataclass(frozen=True, slots=True)
class RiskRules:
    max_pos_pct: float = 0.12
    max_daily_trades: int = 12
    stop_loss: float = -0.08
    max_drawdown: float = -0.20
    min_cash_pct: float = 0.10
    max_holdings: int = 6
    reentry_per_day: int = 1


def round_lot(qty: int) -> int:
    if qty < LOT_SIZE:
        return 0
    return (qty // LOT_SIZE) * LOT_SIZE


def calc_fee(side: str, amount: float) -> FeeBreakdown:
    commission = max(amount * COMMISSION_RATE, COMMISSION_MIN)
    stamp = amount * STAMP_TAX_RATE if side == "sell" else 0.0
    return FeeBreakdown(round(commission, 2), round(stamp, 2), round(commission + stamp, 2))


def is_st(name: str | None) -> bool:
    if not name:
        return False
    nm = name.upper().replace(" ", "").replace("　", "")
    return nm.startswith(("ST", "*ST", "SST", "S*ST", "PT"))


def limit_threshold(code: str, name: str | None = None) -> float:
    if not code or len(code) < 6:
        return LIMIT_MAIN
    if code.startswith(("688", "689")) or code[:3] in ("300", "301"):
        return LIMIT_STAR
    if code.startswith(("83", "87", "920")):
        return LIMIT_BSE
    if is_st(name):
        return LIMIT_ST
    return LIMIT_MAIN


def board_label(code: str, name: str | None = None) -> str:
    threshold = limit_threshold(code, name)
    if threshold == LIMIT_STAR:
        return "科创板/创业板"
    if threshold == LIMIT_BSE:
        return "北交所"
    if threshold == LIMIT_ST:
        return "ST 主板"
    return "主板"


def check_price_limit(
    side: str,
    code: str,
    name: str,
    price: float,
    prev_close: float,
    change_pct: float | None = None,
) -> str:
    """触及涨跌停返回拒绝理由；放行返回空串。"""
    threshold = limit_threshold(code, name)
    block_at = threshold * LIMIT_BLOCK_RATIO
    if prev_close and prev_close > 0:
        actual = (price - prev_close) / prev_close * 100.0
    elif change_pct is not None:
        actual = change_pct
    else:
        return ""
    if side == "buy" and actual >= block_at:
        return (
            f"涨停板买入拦截（{board_label(code, name)} 涨幅={actual:.2f}% ≥ {block_at:.1f}%，"
            f"名义涨停 {threshold:.0f}%），不追涨停"
        )
    if side == "sell" and actual <= -block_at:
        return (
            f"跌停板卖出拦截（{board_label(code, name)} 跌幅={actual:.2f}% ≤ -{block_at:.1f}%，"
            f"名义跌停 {threshold:.0f}%），不割跌停"
        )
    return ""


def locked_qty_today(
    buys_today: list[tuple[str, int]],
    code: str,
) -> int:
    """T+1 锁定股数：当日该股买入总量。"""
    return sum(qty for c, qty in buys_today if c == code)


def new_avg_cost(old_qty: int, old_avg: float, buy_qty: int, buy_price: float, buy_fee: float) -> float:
    if old_qty + buy_qty <= 0:
        return 0.0
    return (old_qty * old_avg + buy_qty * buy_price + buy_fee) / (old_qty + buy_qty)


def apply_fill(
    old_qty: int,
    old_avg: float,
    side: str,
    qty: int,
    price: float,
    fee: float,
) -> tuple[int, float]:
    if side == "buy":
        return old_qty + qty, new_avg_cost(old_qty, old_avg, qty, price, fee)
    new_qty = old_qty - qty
    if new_qty <= 0:
        return 0, 0.0
    return new_qty, old_avg


def cash_delta(side: str, amount: float, fee: float) -> float:
    if side == "buy":
        return -(amount + fee)
    if side == "sell":
        return amount - fee
    raise ValueError(f"非法方向: {side!r}")


def realized_pnl(avg_cost: float, qty: int, price: float, fee: float) -> float:
    return (price - avg_cost) * qty - fee


def can_trade_today(today: date, last_trade_date: date | None) -> bool:
    """占位：A 股 T+1 只约束卖出，不限制当天再买。"""
    return True
