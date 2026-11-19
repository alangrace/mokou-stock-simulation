"""规则化"决策代理"：把计划 + 行情 + 指标变成可审计的 buy/sell/hold 意图。

它替代 SayuStock 里的 LLM 决策层，但输出契约与落库字段保持一致：
- score ∈ [-1, 1]
- reason 里写清 宏观 / 技术 / 计划 / 风控
- buy 的 snapshot 带 plan_stop_* / plan_take_* / macro_regime / macro_note
- 非交易时段只产生 ``blocked_by=market_closed`` 的挂起决策，不成交
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import rules
from .calendar import SessionState


def pnl_pct(price: float, avg_cost: float) -> float:
    return (price - avg_cost) / avg_cost if avg_cost > 0 else 0.0


def score_candidate(ind: dict, quote: dict, candidate: dict) -> tuple[float, list[str]]:
    """技术面评分（-1 ~ +1），只用于计划内排序与阈值过滤。"""
    score = 0.0
    reasons: list[str] = []
    close = float(ind.get("close") or 0)
    ma20 = ind.get("ma20")
    ma60 = ind.get("ma60")

    if ind.get("above_ma20"):
        score += 0.08
        reasons.append("站上 MA20")
    else:
        score -= 0.06
        reasons.append("跌破 MA20")
    if ind.get("ma_bull"):
        score += 0.10
        reasons.append("均线多头排列")
    elif ind.get("ma_bear"):
        score -= 0.10
        reasons.append("均线空头排列")
    if ma20 and ma60:
        if ma20 > ma60:
            score += 0.06
            reasons.append("MA20>MA60 中期向上")
        else:
            score -= 0.04
            reasons.append("MA20<MA60 中期偏弱")

    if ind.get("macd_golden_cross_3d"):
        score += 0.08
        reasons.append("MACD 3 日内金叉")
    if ind.get("macd_death_cross_3d"):
        score -= 0.08
        reasons.append("MACD 3 日内死叉")
    if float(ind.get("dif") or 0) > float(ind.get("dea") or 0):
        score += 0.05
        reasons.append("DIF>DEA")
    else:
        score -= 0.05
        reasons.append("DIF<DEA")

    rsi6 = ind.get("rsi6")
    if isinstance(rsi6, (int, float)):
        if 40 <= rsi6 <= 65:
            score += 0.04
            reasons.append(f"RSI6={rsi6:.0f} 健康")
        elif rsi6 > 75:
            score -= 0.10
            reasons.append(f"RSI6={rsi6:.0f} 超买")
        elif rsi6 < 30:
            score += 0.04
            reasons.append(f"RSI6={rsi6:.0f} 超卖")

    vr = ind.get("vol_ratio")
    if isinstance(vr, (int, float)):
        if 1.2 <= vr <= 2.5:
            score += 0.05
            reasons.append(f"量比={vr:.2f} 温和放量")
        elif vr > 3.0:
            score -= 0.02
            reasons.append(f"量比={vr:.2f} 过热")

    pct20 = ind.get("pct_20d")
    if isinstance(pct20, (int, float)):
        if 0 <= pct20 <= 12:
            score += 0.05
            reasons.append(f"20 日涨幅 {pct20:+.1f}% 温和")
        elif pct20 > 25:
            score -= 0.06
            reasons.append(f"20 日涨幅 {pct20:+.1f}% 已高")

    pos20 = ind.get("pos_20d")
    if isinstance(pos20, (int, float)):
        if pos20 > 0.90:
            score -= 0.05
            reasons.append("处于 20 日区间顶部")
        elif 0.25 <= pos20 <= 0.80:
            score += 0.03
            reasons.append("处于 20 日区间中部")

    atr = ind.get("atr_pct")
    if isinstance(atr, (int, float)) and atr > 0.05:
        score -= 0.05
        reasons.append(f"ATR={atr:.1%} 高波动降权")

    change_pct = float(quote.get("change_pct") or 0)
    if change_pct > 5:
        score -= 0.03
        reasons.append(f"当日已涨 {change_pct:.1f}%")

    return max(-1.0, min(1.0, round(score, 4))), reasons


def entry_blockers(
    plan: dict,
    candidate: dict,
    ind: dict,
    quote: dict,
    held: bool,
) -> list[str]:
    blockers: list[str] = []
    price = float(quote.get("price") or 0)
    if price <= 0:
        blockers.append("无有效报价")
    if held:
        blockers.append("已持仓（本轮不重复开仓）")
    limit_reason = rules.check_price_limit(
        "buy", candidate["code"], candidate.get("name", ""), price,
        float(quote.get("prev_close") or 0), quote.get("change_pct"),
    )
    if limit_reason:
        blockers.append(limit_reason)
    entry = plan["entry"]
    if entry.get("require_above_ma20", True) and not ind.get("above_ma20"):
        blockers.append("未站上 MA20（计划要求趋势确认）")
    rsi6 = ind.get("rsi6")
    if isinstance(rsi6, (int, float)):
        if rsi6 > float(entry.get("max_rsi6", 72)):
            blockers.append(f"RSI6={rsi6:.0f} 高于计划上限 {entry['max_rsi6']:.0f}")
        if rsi6 < float(entry.get("min_rsi6", 25)):
            blockers.append(f"RSI6={rsi6:.0f} 低于计划下限 {entry['min_rsi6']:.0f}")
    chg = abs(float(quote.get("change_pct") or 0))
    if chg > float(entry.get("max_change_pct", 7)):
        blockers.append(f"当日振幅 {chg:.1f}% 超过计划上限 {entry['max_change_pct']:.1f}%")
    return blockers


def exit_signal(
    *,
    position: dict,
    quote: dict,
    score: float,
    today: date,
    buys_today: list[tuple[str, int]],
    plan: dict,
) -> dict:
    """返回 {action, reason, trigger, blocked_by}。action 为 sell / hold。"""
    code = position["code"]
    price = float(quote.get("price") or 0)
    avg = float(position["avg_cost"])
    change = pnl_pct(price, avg) if price > 0 else 0.0
    locked = rules.locked_qty_today(buys_today, code)
    stop_price = position.get("plan_stop_price")
    stop_pct = position.get("plan_stop_pct")
    take_price = position.get("plan_take_price")

    if price <= 0:
        return {"action": "hold", "reason": "无有效报价，暂不动作", "trigger": "", "blocked_by": "no_quote"}
    if locked > 0:
        return {
            "action": "hold",
            "reason": f"A 股 T+1：今日买入 {locked} 股锁定，今日不可卖",
            "trigger": "",
            "blocked_by": "t_plus_1",
        }
    if stop_price and price <= float(stop_price):
        return {
            "action": "sell",
            "reason": f"触发计划止损价 {float(stop_price):.2f}（现价 {price:.2f}，浮亏 {change:.2%}）",
            "trigger": "stop_price",
            "blocked_by": "",
        }
    if stop_pct is not None and change <= float(stop_pct):
        return {
            "action": "sell",
            "reason": f"触发计划止损幅度 {float(stop_pct):.2%}（现浮亏 {change:.2%}）",
            "trigger": "stop_pct",
            "blocked_by": "",
        }
    if take_price and price >= float(take_price):
        return {
            "action": "sell",
            "reason": f"触发计划止盈价 {float(take_price):.2f}（现价 {price:.2f}，浮盈 {change:.2%}）",
            "trigger": "take_price",
            "blocked_by": "",
        }
    score_exit = float(plan.get("exit", {}).get("score_exit", -0.45))
    if score <= score_exit:
        return {
            "action": "sell",
            "reason": f"技术评分 {score:.2f} ≤ 退出阈值 {score_exit:.2f}，趋势破坏",
            "trigger": "score_exit",
            "blocked_by": "",
        }
    return {
        "action": "hold",
        "reason": (
            f"持有：现价 {price:.2f} / 成本 {avg:.2f}（{change:+.2%}），"
            f"距止损 {float(stop_price):.2f}、距止盈 {float(take_price):.2f}" if stop_price and take_price
            else f"持有：现价 {price:.2f} / 成本 {avg:.2f}（{change:+.2%}）"
        ),
        "trigger": "",
        "blocked_by": "",
    }


def position_budget(
    plan: dict,
    candidate: dict,
    equity: float,
    price: float,
    cash: float,
    *,
    max_total_pct: float | None = None,
    max_single_pct: float | None = None,
) -> tuple[int, float, str]:
    """按计划权重算目标股数；宏观档位可进一步压低总仓/单票上限。"""
    risk = plan["risk"]
    total_pct = min(float(risk["max_total_exposure_pct"]), max_total_pct) if max_total_pct is not None else float(risk["max_total_exposure_pct"])
    single_pct = min(float(risk["max_single_pct"]), max_single_pct) if max_single_pct is not None else float(risk["max_single_pct"])
    exposure_cap = equity * total_pct
    single_cap = equity * single_pct * float(candidate.get("weight", 1.0))
    current_exposure = equity - cash
    remaining_total = max(0.0, exposure_cap - current_exposure)
    budget = min(single_cap, remaining_total, cash - equity * float(risk.get("min_cash_pct", 0.0)))
    if budget <= 0 or price <= 0:
        return 0, 0.0, "计划仓位/现金预算不足"
    qty = rules.round_lot(int(budget / price))
    if qty < 100:
        return 0, budget, f"预算 {budget:.0f} 元不足 100 股（现价 {price:.2f}）"
    return qty, budget, ""


def snapshot_for_buy(plan: dict, candidate: dict, ind: dict, quote: dict, qty: int, score: float) -> dict:
    entry = plan["entry"]
    price = float(quote["price"])
    stop_pct = float(candidate.get("stop_pct", entry["stop_pct"]))
    take_pct = float(candidate.get("take_pct", entry["take_pct"]))
    return {
        "plan_entry": round(price, 4),
        "plan_stop_pct": round(stop_pct, 4),
        "plan_stop_price": round(price * (1 + stop_pct), 3),
        "plan_take_pct": round(take_pct, 4),
        "plan_take_price": round(price * (1 + take_pct), 3),
        "plan_thesis": candidate.get("thesis", ""),
        "macro_regime": plan["macro"]["regime"],
        "macro_note": plan["macro"]["note"],
        "score": score,
        "planned_qty": qty,
        "indicators": ind,
        "quote": quote,
    }
