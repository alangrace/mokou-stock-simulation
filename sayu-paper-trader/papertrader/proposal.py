"""Agent 提议校验（P2 dry-run / P3 受控执行）。

默认只读：不写 trade / position / cash；只把提议写入 agent_run 审计。
加 execute=True 且 SAYU_AGENT_TRADING=1 时，才按 P3 限额真正成交：

- 单日 Agent 成交 ≤ 3 笔；
- 与规则 tick 共用计划里的 max_daily_trades（默认 12）；
- 单日 Agent 成交金额 ≤ 10% 权益；
- 当日回撤 ≥ 2% 时自动停用 Agent 成交。
"""

from __future__ import annotations

import json
from typing import Any

from . import ledger, macro, plan as plan_mod, planner, rules, switches
from .calendar import session_state
from .engine import MACRO_CAPS, _quote_dict
from .market import MarketClient, MarketError

AGENT_MAX_DAILY_TRADES = 3
AGENT_MAX_DAILY_NOTIONAL_PCT = 0.10
AGENT_MAX_DAILY_DRAWDOWN_PCT = 0.02


def agent_trading_enabled() -> bool:
    """P3 写库开关：默认关闭，需环境变量 SAYU_AGENT_TRADING=1 或 data/agent_trading.flag。"""
    return switches.agent_trading_enabled()


def _agent_limits(
    account_id: int,
    account: dict,
    plan: dict,
    equity: float,
    day: str,
) -> tuple[str, str]:
    """Agent 独立限额检查；返回 (blocked_by, 理由)，放行返回 ("", "")。"""
    used = ledger.daily_agent_trade_count(account_id, day)
    if used >= AGENT_MAX_DAILY_TRADES:
        return "agent_daily_quota", f"Agent 今日已成交 {used}/{AGENT_MAX_DAILY_TRADES} 笔"
    notional = sum(
        float(t["amount"])
        for t in ledger.recent_trades(account_id, 200)
        if str(t.get("trade_date")) == day and str(t.get("source") or "rule") == "agent"
    )
    if equity > 0 and notional >= equity * AGENT_MAX_DAILY_NOTIONAL_PCT:
        return (
            "agent_daily_notional",
            f"Agent 今日成交金额 {notional:.0f} 已达权益 {AGENT_MAX_DAILY_NOTIONAL_PCT:.0%} 上限",
        )
    baseline = float(account.get("initial_cash") or 0.0)
    if baseline > 0 and equity < baseline * (1 - AGENT_MAX_DAILY_DRAWDOWN_PCT):
        return (
            "agent_drawdown_kill",
            f"账户权益 {equity:.0f} 低于初始 {baseline:.0f} 的 "
            f"{(1 - AGENT_MAX_DAILY_DRAWDOWN_PCT):.0%}，Agent 成交已停用",
        )
    return "", ""


def _money(value: float | None) -> float:
    return round(float(value or 0.0), 2)


def _block(
    *,
    account_id: int | None,
    payload: dict,
    code: str,
    name: str,
    side: str,
    reason: str,
    blocked_by: str,
    quote: Any = None,
    qty: int = 0,
    price: float = 0.0,
    amount: float = 0.0,
    fee: float = 0.0,
    warnings: list[str] | None = None,
    position_before: int = 0,
) -> dict:
    result = {
        "ok": False,
        "dry_run": True,
        "action": side,
        "code": code,
        "name": name,
        "qty": qty,
        "price": _money(price),
        "amount": _money(amount),
        "fee": _money(fee),
        "blocked_by": blocked_by,
        "reason": reason,
        "warnings": warnings or [],
        "position_before": position_before,
        "position_after": position_before,
    }
    if account_id:
        macro.log_agent_run(
            account_id,
            decision={"proposal": payload, "result": result},
            status="dry_run_blocked",
        )
    return result


def _equity_and_quotes(
    market: MarketClient,
    account: dict,
    positions: list[dict],
    codes: list[str],
) -> tuple[float, float, dict]:
    all_codes = list(dict.fromkeys([*codes, *[str(p["code"]) for p in positions]]))
    try:
        quotes = market.batch_quotes(all_codes)
    except MarketError:
        quotes = {}
    position_value = 0.0
    for p in positions:
        q = quotes.get(str(p["code"]))
        price = float(q.price) if q is not None else float(p.get("last_price") or p.get("avg_cost") or 0.0)
        position_value += price * int(p["qty"])
    equity = float(account["cash"]) + position_value
    return equity, position_value, quotes


def validate(
    account_name: str,
    payload: dict,
    market: MarketClient | None = None,
    execute: bool = False,
) -> dict:
    account = ledger.get_account(account_name)
    if account is None:
        raise RuntimeError(f"账户不存在：{account_name}")
    plan_record = ledger.get_plan(int(account["id"]))
    if plan_record is None:
        raise RuntimeError("没有启用的入场计划")
    plan = plan_record["payload"]
    account_id = int(account["id"])
    action = str(payload.get("action", "")).strip().lower()
    code = str(payload.get("code", "")).strip()
    if action not in ("buy", "sell"):
        return _block(
            account_id=account_id, payload=payload, code=code, name="",
            side=action or "unknown", reason="action 必须是 buy/sell", blocked_by="bad_action",
        )
    if not code:
        return _block(
            account_id=account_id, payload=payload, code="", name="",
            side=action, reason="缺少 code", blocked_by="bad_code",
        )

    market = market or MarketClient()
    try:
        quote = market.quote(code)
    except MarketError as exc:
        return _block(
            account_id=account_id, payload=payload, code=code, name="",
            side=action, reason=f"行情不可达：{type(exc).__name__}: {exc}", blocked_by="no_quote",
        )

    session = session_state()
    if not session.is_open:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side=action, quote=quote, reason=f"非交易时段（{session.label}）", blocked_by="market_closed",
        )

    positions = ledger.positions(account_id)
    position = next((p for p in positions if str(p["code"]) == code), None)
    held = {str(p["code"]) for p in positions}
    universe = {str(c["code"]): c for c in plan.get("universe", [])}
    candidate = universe.get(code, {"code": code, "name": quote.name, "sector": "", "weight": 1.0})
    sector = str(candidate.get("sector", ""))
    equity, position_value, quotes = _equity_and_quotes(market, account, positions, [code])
    if equity <= 0:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side=action, quote=quote, reason="账户权益异常", blocked_by="bad_equity",
        )

    raw_regime = str(payload.get("macro_regime", "")).strip()
    if action == "buy":
        if raw_regime not in MACRO_CAPS:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, reason="缺少/非法 macro_regime", blocked_by="macro_regime",
            )
        regime = raw_regime
    else:
        # 卖出不强制要求宏观档位；缺省时沿用计划档位做仓位上限计算
        regime = raw_regime if raw_regime in MACRO_CAPS else str(plan["macro"].get("regime", "中性"))
        if regime not in MACRO_CAPS:
            regime = "中性"
    if action == "buy":
        # 与规则引擎同源：先跑入场技术拦截，再做防御名单与评分校验
        from .indicators import compute

        try:
            ind = compute(market.kline(code, 101, 180))
        except MarketError as exc:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote,
                reason=f"K 线不可达，无法复核入场条件：{type(exc).__name__}: {exc}",
                blocked_by="no_data",
            )
        allow_add = bool(payload.get("allow_add", False))
        blockers = planner.entry_blockers(
            plan, candidate, ind, _quote_dict(quote), held=(code in held) and not allow_add
        )
        if blockers:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, reason="；".join(blockers), blocked_by="entry_blockers",
            )
        try:
            score = float(payload.get("score"))
        except (TypeError, ValueError):
            # 未提供评分时按计划规则现算，避免默认 0 误伤
            score, _score_reasons = planner.score_candidate(ind, _quote_dict(quote), candidate)
        min_score = float(plan["entry"].get("min_score", 0.2))
        if score < min_score:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote,
                reason=f"评分 {score:.2f} < 计划阈值 {min_score:.2f}",
                blocked_by="score_below_threshold",
            )
    if action == "buy" and regime == "防御" and not bool(candidate.get("defensive", False)):
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side=action, quote=quote,
            reason=f"宏观防御档只买高股息/公用/必需消费/上游资源；{candidate.get('name', quote.name)}不在计划防御名单",
            blocked_by="macro_defensive_only",
        )
    cap_total, cap_single = MACRO_CAPS[regime]
    eff_total = min(float(plan["risk"]["max_total_exposure_pct"]), cap_total)
    eff_single = min(float(plan["risk"]["max_single_pct"]), cap_single)
    max_positions = int(plan["risk"].get("max_positions", 8))
    max_per_sector = int(plan["entry"].get("max_per_sector", 1))
    max_daily = int(plan["risk"].get("max_daily_trades", 12))
    day = session.now.date().isoformat()
    daily_count = ledger.daily_trade_count(account_id, day)

    # 价格笼子
    limit_reason = rules.check_price_limit(
        action, code, quote.name, quote.price, quote.prev_close, quote.change_pct
    )
    if limit_reason:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side=action, quote=quote, reason=limit_reason, blocked_by="price_limit",
        )

    if daily_count >= max_daily:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side=action, quote=quote,
            reason=f"已达单日交易上限 {daily_count}/{max_daily}（规则与 Agent 共用）",
            blocked_by="max_daily_trades",
        )

    if action == "buy":
        agent_blocked, agent_reason = _agent_limits(account_id, account, plan, equity, day)
        if agent_blocked:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, reason=agent_reason, blocked_by=agent_blocked,
            )

    try:
        qty = int(payload.get("qty", 0) or 0)
    except (TypeError, ValueError):
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side=action, quote=quote, reason="qty 必须是整数", blocked_by="bad_qty",
        )

    if action == "buy":
        if code in held and not bool(payload.get("allow_add", False)):
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, reason="已持仓；如需加仓传 allow_add=true",
                blocked_by="already_held",
            )
        if code not in held and len(positions) >= max_positions:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, reason=f"已达最大持仓数 {len(positions)}/{max_positions}",
                blocked_by="max_holdings",
            )
        if code not in held and sector:
            sector_count = sum(1 for p in positions if universe.get(str(p["code"]), {}).get("sector") == sector)
            if sector_count >= max_per_sector:
                return _block(
                    account_id=account_id, payload=payload, code=code, name=quote.name,
                    side=action, quote=quote, reason=f"行业「{sector}」已达上限 {max_per_sector}",
                    blocked_by="sector_cap",
                )

        existing_value = float(position["qty"]) * quote.price if position else 0.0
        if qty <= 0:
            weight = float(candidate.get("weight", 1.0))
            target = equity * eff_single * weight - existing_value
            cash_room = float(account["cash"]) - equity * float(plan["risk"].get("min_cash_pct", 0.0))
            total_room = equity * eff_total - position_value
            budget = min(target, cash_room, total_room)
            qty = rules.round_lot(int(max(0.0, budget) / quote.price)) if quote.price > 0 else 0
        if qty <= 0 or qty % 100 != 0:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, reason="qty 必须为 100 股整手且 >0", blocked_by="bad_qty",
            )

        amount = qty * quote.price
        fee_bd = rules.calc_fee("buy", amount)
        normalized = plan_mod.normalize_plan_fields(payload)
        stop_pct_val = normalized.get("plan_stop_pct")
        take_pct_val = normalized.get("plan_take_pct")
        stop_price_val = normalized.get("plan_stop_price")
        take_price_val = normalized.get("plan_take_price")
        # 只给幅度时按成交价折算成价格，确保退出检查（止损价/止盈价）能生效
        if stop_price_val is None and stop_pct_val is not None:
            stop_price_val = round(quote.price * (1 + float(stop_pct_val)), 3)
        if take_price_val is None and take_pct_val is not None:
            take_price_val = round(quote.price * (1 + float(take_pct_val)), 3)
        snapshot = {
            "plan_stop_pct": stop_pct_val,
            "plan_stop_price": stop_price_val,
            "plan_take_pct": take_pct_val,
            "plan_take_price": take_price_val,
            "plan_thesis": normalized.get("plan_thesis", ""),
            "macro_regime": regime,
            "macro_note": normalized.get("macro_note", ""),
        }
        gate = plan_mod.validate_buy(plan, snapshot)
        if gate:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, qty=qty, price=quote.price, amount=amount,
                fee=fee_bd.total, reason=gate, blocked_by="strategy_gate",
                position_before=int(position["qty"]) if position else 0,
            )
        if position_value + amount > equity * eff_total + 1e-6:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, qty=qty, price=quote.price, amount=amount,
                fee=fee_bd.total,
                reason=f"总仓超限：{(position_value + amount) / equity:.1%} > {eff_total:.1%}",
                blocked_by="total_exposure",
                position_before=int(position["qty"]) if position else 0,
            )
        single_after = existing_value + amount
        weight = float(candidate.get("weight", 1.0))
        single_cap = equity * eff_single * weight
        if single_after > single_cap + 1e-6:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, qty=qty, price=quote.price, amount=amount,
                fee=fee_bd.total,
                reason=(
                    f"单票超限：{single_after / equity:.1%} > {eff_single * weight:.1%}"
                    f"（宏观上限 {eff_single:.1%} × 权重 {weight:.2f}）"
                ),
                blocked_by="single_exposure",
                position_before=int(position["qty"]) if position else 0,
            )
        cash_room = float(account["cash"]) - equity * float(plan["risk"].get("min_cash_pct", 0.0))
        if amount + fee_bd.total > cash_room + 1e-6:
            return _block(
                account_id=account_id, payload=payload, code=code, name=quote.name,
                side=action, quote=quote, qty=qty, price=quote.price, amount=amount,
                fee=fee_bd.total,
                reason=f"现金不足：需 {amount + fee_bd.total:.2f}，可用 {cash_room:.2f}",
                blocked_by="cash",
                position_before=int(position["qty"]) if position else 0,
            )
        warnings: list[str] = []
        result = {
            "ok": True,
            "dry_run": True,
            "action": "buy",
            "code": code,
            "name": quote.name,
            "qty": qty,
            "price": _money(quote.price),
            "amount": _money(amount),
            "fee": _money(fee_bd.total),
            "commission": _money(fee_bd.commission),
            "stamp_tax": _money(fee_bd.stamp_tax),
            "realized_pnl": 0.0,
            "blocked_by": "",
            "reason": f"would buy {qty} 股 @{quote.price:.3f}（dry-run，不成交）",
            "warnings": warnings,
            "position_before": int(position["qty"]) if position else 0,
            "position_after": (int(position["qty"]) if position else 0) + qty,
            "macro_regime": regime,
            "score": round(score, 4),
            "plan_stop_pct": snapshot.get("plan_stop_pct"),
            "plan_stop_price": snapshot.get("plan_stop_price"),
            "plan_take_pct": snapshot.get("plan_take_pct"),
            "plan_take_price": snapshot.get("plan_take_price"),
            "eff_total_pct": round(eff_total, 4),
            "eff_single_pct": round(eff_single, 4),
        }
        if not execute:
            macro.log_agent_run(
                account_id, decision={"proposal": payload, "result": result}, status="dry_run_ok"
            )
            return result
        if not agent_trading_enabled():
            blocked = dict(result)
            blocked["ok"] = False
            blocked["blocked_by"] = "agent_trading_disabled"
            blocked["reason"] = "SAYU_AGENT_TRADING 未开启，拒绝下单"
            macro.log_agent_run(
                account_id, decision={"proposal": payload, "result": blocked},
                status="agent_trade_blocked",
            )
            return blocked
        try:
            fill = ledger.record_fill(
                account_id=account_id, code=code, name=quote.name, side="buy",
                price=float(quote.price), qty=qty,
                reason=f"Agent 买入：{payload.get('reason', '')}",
                snapshot=snapshot, mode=str(account.get("mode", "balanced")), source="agent",
                executed_at=session.now, trade_date=day,
            )
        except (ValueError, RuntimeError) as exc:
            blocked = dict(result)
            blocked["ok"] = False
            blocked["blocked_by"] = "fill_rejected"
            blocked["reason"] = f"成交被拒：{exc}"
            macro.log_agent_run(
                account_id, decision={"proposal": payload, "result": blocked},
                status="agent_trade_blocked",
            )
            return blocked
        result.update(
            {
                "dry_run": False,
                "trade_id": fill["trade_id"],
                "source": "agent",
                "position_after": fill["position_qty"],
                "cash_after": fill["cash_after"],
                "reason": f"买入 {qty} 股 @{fill['price']:.3f}（Agent 成交）",
            }
        )
        macro.log_agent_run(
            account_id, decision={"proposal": payload, "result": result}, status="agent_trade_ok"
        )
        return result

    # sell
    if position is None:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side="sell", quote=quote, reason="没有该持仓", blocked_by="no_position",
        )
    held_qty = int(position["qty"])
    if qty <= 0:
        qty = held_qty
    if qty <= 0 or qty % 100 != 0:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side="sell", quote=quote, qty=qty, reason="qty 必须为 100 股整手且 >0", blocked_by="bad_qty",
            position_before=held_qty,
        )
    if qty > held_qty:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side="sell", quote=quote, qty=qty, reason=f"卖出超过持仓 {qty}/{held_qty}",
            blocked_by="position_short", position_before=held_qty,
        )
    locked = sum(q for c, q in ledger.buys_today(account_id, day) if c == code)
    if locked > 0:
        return _block(
            account_id=account_id, payload=payload, code=code, name=quote.name,
            side="sell", quote=quote, qty=qty,
            reason=f"A 股 T+1：今日买入 {locked} 股锁定，今日不可卖",
            blocked_by="t_plus_1", position_before=held_qty,
        )
    amount = qty * quote.price
    fee_bd = rules.calc_fee("sell", amount)
    pnl = rules.realized_pnl(float(position["avg_cost"]), qty, quote.price, fee_bd.total)
    result = {
        "ok": True,
        "dry_run": True,
        "action": "sell",
        "code": code,
        "name": quote.name,
        "qty": qty,
        "price": _money(quote.price),
        "amount": _money(amount),
        "fee": _money(fee_bd.total),
        "commission": _money(fee_bd.commission),
        "stamp_tax": _money(fee_bd.stamp_tax),
        "realized_pnl": _money(pnl),
        "blocked_by": "",
        "reason": f"would sell {qty} 股 @{quote.price:.3f}（dry-run，不成交）",
        "warnings": [],
        "position_before": held_qty,
        "position_after": held_qty - qty,
        "macro_regime": regime,
        "eff_total_pct": round(eff_total, 4),
        "eff_single_pct": round(eff_single, 4),
    }
    if not execute:
        macro.log_agent_run(
            account_id, decision={"proposal": payload, "result": result}, status="dry_run_ok"
        )
        return result
    if not agent_trading_enabled():
        blocked = dict(result)
        blocked["ok"] = False
        blocked["blocked_by"] = "agent_trading_disabled"
        blocked["reason"] = "SAYU_AGENT_TRADING 未开启，拒绝下单"
        macro.log_agent_run(
            account_id, decision={"proposal": payload, "result": blocked},
            status="agent_trade_blocked",
        )
        return blocked
    try:
        fill = ledger.record_fill(
            account_id=account_id, code=code, name=quote.name, side="sell",
            price=float(quote.price), qty=qty,
            reason=f"Agent 卖出：{payload.get('reason', '')}",
            snapshot={}, mode=str(account.get("mode", "balanced")), source="agent",
            executed_at=session.now, trade_date=day,
        )
    except (ValueError, RuntimeError) as exc:
        blocked = dict(result)
        blocked["ok"] = False
        blocked["blocked_by"] = "fill_rejected"
        blocked["reason"] = f"成交被拒：{exc}"
        macro.log_agent_run(
            account_id, decision={"proposal": payload, "result": blocked},
            status="agent_trade_blocked",
        )
        return blocked
    result.update(
        {
            "dry_run": False,
            "trade_id": fill["trade_id"],
            "source": "agent",
            "position_after": fill["position_qty"],
            "cash_after": fill["cash_after"],
            "realized_pnl": fill["realized_pnl"],
            "reason": f"卖出 {qty} 股 @{fill['price']:.3f}（Agent 成交）",
        }
    )
    macro.log_agent_run(
        account_id, decision={"proposal": payload, "result": result}, status="agent_trade_ok"
    )
    return result


def log_decision(account_name: str, payload: dict) -> dict:
    account = ledger.get_account(account_name)
    if account is None:
        raise RuntimeError(f"账户不存在：{account_name}")
    action = str(payload.get("action", "")).strip().lower()
    if action not in ("hold", "buy", "sell"):
        raise ValueError("action 必须是 hold/buy/sell")
    code = str(payload.get("code", "")).strip()
    name = str(payload.get("name", "")).strip()
    if code and not name:
        # 名称缺失时用持仓或计划里的名称补齐，避免审计里只剩代码
        position = ledger.get_position(int(account["id"]), code)
        if position is not None:
            name = str(position.get("name") or "")
        if not name:
            plan_record = ledger.get_plan(int(account["id"]))
            universe = (plan_record or {}).get("payload", {}).get("universe", [])
            name = next(
                (str(item.get("name") or "") for item in universe if str(item.get("code")) == code),
                "",
            )
    if code and name:
        payload = dict(payload, code=code, name=name)
    status = "dry_run_hold" if action == "hold" else "dry_run_signal"
    run_id = macro.log_agent_run(
        int(account["id"]),
        decision={"decision": payload},
        status=status,
    )
    return {
        "ok": True,
        "dry_run": True,
        "run_id": run_id,
        "status": status,
        "action": action,
        "code": code,
        "name": name,
    }
