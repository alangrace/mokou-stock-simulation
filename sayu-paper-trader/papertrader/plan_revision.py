"""持仓计划修订（P1a：只做 dry-run 校验，不写库）。

对应上游 e513b64d 的 papertrade_position_plan_update：
- backfill：老仓缺止损/止盈时补齐；
- trail_stop：浮盈后单向上移止损；
- macro_tighten：宏观降档/新 risk_off 时收紧；
- take_adjust：突破后调整止盈。

P1a 只返回校验结果并写 agent_run 审计，不修改 position。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from . import ledger, macro, plan as plan_mod
from .config import TZ
from .market import MarketClient, MarketError

REVISION_TYPES = ("backfill", "trail_stop", "macro_tighten", "take_adjust")
MODE_STOPS = {"conservative": -0.05, "balanced": -0.08, "aggressive": -0.12}
COOLDOWN_HOURS = 20
MIN_STEP = 0.02
TAKE_NEAR = 0.02
TAKE_MAX_RAISE = 0.20


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed


def _plan_view(position: dict, avg_cost: float) -> dict:
    stop_price = _num(position.get("plan_stop_price"))
    stop_pct = _num(position.get("plan_stop_pct"))
    if stop_pct is None and stop_price is not None and avg_cost > 0:
        stop_pct = stop_price / avg_cost - 1.0
    take_price = _num(position.get("plan_take_price"))
    take_pct = take_price / avg_cost - 1.0 if take_price is not None and avg_cost > 0 else None
    return {
        "plan_stop_price": round(stop_price, 3) if stop_price is not None else None,
        "plan_stop_pct": round(stop_pct, 4) if stop_pct is not None else None,
        "plan_take_price": round(take_price, 3) if take_price is not None else None,
        "plan_take_pct": round(take_pct, 4) if take_pct is not None else None,
        "plan_thesis": position.get("plan_thesis") or "",
        "plan_updated_at": position.get("plan_updated_at"),
        "plan_rev_count": int(position.get("plan_rev_count") or 0),
    }


def _bad(code: str, name: str, rtype: str, price: float, reason: str, blocked_by: str, **extra: Any) -> dict:
    return {
        "ok": False,
        "dry_run": True,
        "code": code,
        "name": name,
        "revision_type": rtype,
        "price": round(float(price), 3) if price else 0.0,
        "blocked_by": blocked_by,
        "reason": reason,
        "warnings": [],
        **extra,
    }


def evaluate(
    position: dict,
    plan: dict,
    payload: dict,
    price: float,
    now: datetime,
    mode: str = "balanced",
) -> dict:
    """纯校验函数，方便测试；不写数据库。"""
    code = str(position.get("code") or payload.get("code") or "")
    name = str(position.get("name") or "")
    avg = float(position.get("avg_cost") or 0.0)
    rtype = str(payload.get("revision_type", "")).strip().lower()
    if rtype not in REVISION_TYPES:
        return _bad(code, name, rtype or "unknown", price, f"revision_type 必须是 {'/'.join(REVISION_TYPES)}", "bad_type")
    if avg <= 0:
        return _bad(code, name, rtype, price, "持仓成本异常", "bad_cost")
    reason = str(payload.get("reason", "")).strip()
    if len(reason) < 6:
        return _bad(code, name, rtype, price, "必须写清楚修订理由（≥6 字）", "bad_reason")

    old = _plan_view(position, avg)
    old_stop = _num(old["plan_stop_price"])
    old_take = _num(old["plan_take_price"])

    raw_stop_pct = payload.get("plan_stop_pct")
    stop_pct = plan_mod.normalize_stop_pct(raw_stop_pct)
    raw_take_pct = payload.get("plan_take_pct")
    take_pct = plan_mod.normalize_take_pct(raw_take_pct)
    if raw_stop_pct is not None and stop_pct is None:
        return _bad(code, name, rtype, price, "plan_stop_pct 非法：必须是 (-1,0) 小数", "bad_stop_pct", old_plan=old)
    if raw_take_pct is not None and take_pct is None:
        return _bad(code, name, rtype, price, "plan_take_pct 非法：必须是 (0,5] 小数", "bad_take_pct", old_plan=old)

    new_stop = _num(payload.get("plan_stop_price"))
    if new_stop is None and stop_pct is not None:
        new_stop = avg * (1.0 + stop_pct)
    if new_stop is None:
        new_stop = old_stop
    new_take = _num(payload.get("plan_take_price"))
    if new_take is None and take_pct is not None:
        new_take = avg * (1.0 + take_pct)
    if new_take is None:
        new_take = old_take

    updated_at = _dt(position.get("plan_updated_at"))
    cooldown_ok = not (updated_at and now - updated_at < timedelta(hours=COOLDOWN_HOURS))
    rev_count = int(position.get("plan_rev_count") or 0)
    warnings: list[str] = []
    if rev_count >= 2:
        warnings.append(f"plan_rev_count={rev_count}，该仓已多次修订，应更克制")

    if rtype == "backfill":
        if old_stop is not None and old_take is not None:
            return _bad(code, name, rtype, price, "该仓止损/止盈都已存在，不需要 backfill", "not_missing", old_plan=old)
        if old_stop is None and _num(payload.get("plan_stop_price")) is None and stop_pct is None:
            return _bad(code, name, rtype, price, "backfill 缺少缺失的止损", "missing_stop", old_plan=old)
        if old_take is None and _num(payload.get("plan_take_price")) is None and take_pct is None:
            return _bad(code, name, rtype, price, "backfill 缺少缺失的止盈", "missing_take", old_plan=old)
        if old_stop is not None and new_stop is not None and abs(new_stop - old_stop) > 1e-6:
            return _bad(code, name, rtype, price, "已有止损不能用 backfill 改；请用 trail_stop/macro_tighten", "backfill_change", old_plan=old)
        if old_take is not None and new_take is not None and abs(new_take - old_take) > 1e-6:
            return _bad(code, name, rtype, price, "已有止盈不能用 backfill 改；请用 take_adjust", "backfill_change", old_plan=old)
    else:
        if not cooldown_ok:
            return _bad(code, name, rtype, price, f"计划冷却中：{COOLDOWN_HOURS} 小时内只能修订一次", "cooldown", old_plan=old)
        if old_stop is None or old_take is None:
            return _bad(code, name, rtype, price, "老仓缺少止损/止盈，请先用 backfill 补齐", "need_backfill", old_plan=old)

    if new_stop is None or new_stop <= 0:
        return _bad(code, name, rtype, price, "修订后止损价无效", "bad_stop", old_plan=old)
    if new_take is None or new_take <= 0:
        return _bad(code, name, rtype, price, "修订后止盈价无效", "bad_take", old_plan=old)
    if new_stop >= price:
        return _bad(code, name, rtype, price, "新止损不得高于/等于现价，否则会立即触发", "stop_above_price", old_plan=old)
    if new_take <= price:
        return _bad(code, name, rtype, price, "新止盈必须高于现价，避免立即触发", "take_below_price", old_plan=old)
    hard_stop = avg * (1.0 + MODE_STOPS.get(mode, -0.10))
    if new_stop < hard_stop - 1e-6:
        return _bad(code, name, rtype, price, f"新止损比模式硬止损更宽（{hard_stop:.3f}）", "wider_than_mode_stop", old_plan=old)

    if rtype == "trail_stop":
        if old_stop is None:
            return _bad(code, name, rtype, price, "没有原止损，不能 trail_stop", "no_old_stop", old_plan=old)
        if new_stop <= old_stop:
            return _bad(code, name, rtype, price, "trail_stop 只能上移止损，不能放宽", "stop_widen", old_plan=old)
        if (new_stop - old_stop) / old_stop < MIN_STEP:
            return _bad(code, name, rtype, price, f"止损变化不足 {MIN_STEP:.0%}", "step_too_small", old_plan=old)
        profit = price / avg - 1.0
        if profit < 0.08:
            return _bad(code, name, rtype, price, f"浮盈 {profit:.1%} 未达 +8%，不建议 trail_stop", "not_enough_profit", old_plan=old)
    elif rtype == "macro_tighten":
        regime = str(payload.get("macro_regime", "")).strip()
        if regime not in ("中性", "防御"):
            return _bad(code, name, rtype, price, "macro_tighten 须写 macro_regime=中性/防御", "macro_regime", old_plan=old)
        stop_changed = old_stop is not None and abs(new_stop - old_stop) > 1e-6
        take_changed = old_take is not None and abs(new_take - old_take) > 1e-6
        if not stop_changed and not take_changed:
            return _bad(code, name, rtype, price, "macro_tighten 至少改止损或止盈一项", "no_change", old_plan=old)
        if stop_changed:
            if new_stop < old_stop:
                return _bad(code, name, rtype, price, "macro_tighten 不能放宽止损", "stop_widen", old_plan=old)
            if (new_stop - old_stop) / old_stop < MIN_STEP:
                return _bad(code, name, rtype, price, f"止损变化不足 {MIN_STEP:.0%}", "step_too_small", old_plan=old)
        if take_changed:
            if new_take > old_take:
                return _bad(code, name, rtype, price, "macro_tighten 不能上调止盈", "take_raise", old_plan=old)
            if (old_take - new_take) / old_take < MIN_STEP:
                return _bad(code, name, rtype, price, f"止盈变化不足 {MIN_STEP:.0%}", "step_too_small", old_plan=old)
    elif rtype == "take_adjust":
        if new_take <= old_take:
            return _bad(code, name, rtype, price, "take_adjust 只能上调止盈", "take_down", old_plan=old)
        if (new_take - old_take) / old_take < MIN_STEP:
            return _bad(code, name, rtype, price, f"止盈变化不足 {MIN_STEP:.0%}", "step_too_small", old_plan=old)
        if (new_take - old_take) / old_take > TAKE_MAX_RAISE:
            return _bad(code, name, rtype, price, f"单次止盈上调不得超过 {TAKE_MAX_RAISE:.0%}", "take_too_high", old_plan=old)
        if old_take / price - 1.0 < TAKE_NEAR:
            return _bad(code, name, rtype, price, "现价距原止盈不足 2%，禁止上调", "take_near", old_plan=old)
        if not str(payload.get("evidence", "")).strip():
            return _bad(code, name, rtype, price, "take_adjust 须写 evidence（突破/催化依据）", "no_evidence", old_plan=old)

    new = {
        "plan_stop_price": round(new_stop, 3),
        "plan_stop_pct": round(new_stop / avg - 1.0, 4),
        "plan_take_price": round(new_take, 3),
        "plan_take_pct": round(new_take / avg - 1.0, 4),
        "plan_thesis": str(payload.get("plan_thesis") or old.get("plan_thesis") or ""),
        "plan_updated_at": now.isoformat(timespec="seconds"),
        "plan_rev_count": rev_count + 1,
    }
    return {
        "ok": True,
        "dry_run": True,
        "code": code,
        "name": name,
        "revision_type": rtype,
        "price": round(price, 3),
        "old_plan": old,
        "new_plan": new,
        "blocked_by": "",
        "reason": f"would {rtype}：止损 {old.get('plan_stop_price')} → {new['plan_stop_price']}，止盈 {old.get('plan_take_price')} → {new['plan_take_price']}（dry-run，不写库）",
        "warnings": warnings,
        "cooldown_ok": cooldown_ok,
        "plan_rev_count": rev_count,
    }


def agent_trading_enabled() -> bool:
    """P1b/P3 写库开关：默认关闭，需显式设置 SAYU_AGENT_TRADING=1。"""
    return str(os.environ.get("SAYU_AGENT_TRADING", "0")).strip().lower() in ("1", "true", "yes", "on")


def apply_revision(account_id: int, position: dict, result: dict, note: str = "") -> dict:
    """把 evaluate 通过的新计划写入 position，并写一条 hold 决策做审计。"""
    new = result["new_plan"]
    old = result["old_plan"]
    updated = ledger.update_position_plan(
        account_id,
        str(position["code"]),
        plan_stop_price=new["plan_stop_price"],
        plan_stop_pct=new["plan_stop_pct"],
        plan_take_price=new["plan_take_price"],
        plan_thesis=new["plan_thesis"],
        plan_updated_at=new["plan_updated_at"],
        plan_rev_count=new["plan_rev_count"],
    )
    reason = (
        f"计划修订[{result['revision_type']}] 止损 {old.get('plan_stop_price')} → {new['plan_stop_price']}，"
        f"止盈 {old.get('plan_take_price')} → {new['plan_take_price']}｜原因：{note or '未填写'}"
    )
    decision_id = ledger.append_decision(
        account_id=account_id,
        code=str(position["code"]),
        name=str(position.get("name") or ""),
        action="hold",
        score=0.0,
        reason=reason,
        snapshot={"old_plan": old, "new_plan": new, "revision_type": result["revision_type"]},
        blocked_by="",
    )
    return {
        "decision_id": decision_id,
        "position": updated,
        "applied": True,
    }


def validate(
    account_name: str,
    payload: dict,
    market: MarketClient | None = None,
    now: datetime | None = None,
    dry_run: bool = True,
) -> dict:
    account = ledger.get_account(account_name)
    if account is None:
        raise RuntimeError(f"账户不存在：{account_name}")
    plan_record = ledger.get_plan(int(account["id"]))
    if plan_record is None:
        raise RuntimeError("没有启用的入场计划")
    code = str(payload.get("code", "")).strip()
    if not code:
        raise RuntimeError("缺少 code")
    position = ledger.get_position(int(account["id"]), code)
    if position is None:
        result = _bad(code, "", str(payload.get("revision_type", "")), 0.0, "没有该持仓", "no_position")
        macro.log_agent_run(int(account["id"]), decision={"proposal": payload, "result": result}, status="plan_dry_run_blocked")
        return result
    market = market or MarketClient()
    try:
        quote = market.quote(code)
    except MarketError as exc:
        result = _bad(code, str(position.get("name") or ""), str(payload.get("revision_type", "")), 0.0, f"行情不可达：{type(exc).__name__}: {exc}", "no_quote", old_plan=_plan_view(position, float(position["avg_cost"])))
        macro.log_agent_run(int(account["id"]), decision={"proposal": payload, "result": result}, status="plan_dry_run_blocked")
        return result
    result = evaluate(
        position,
        plan_record["payload"],
        payload,
        float(quote.price),
        now or datetime.now(TZ),
        mode=str(account.get("mode", "balanced")),
    )
    status = "plan_dry_run_ok" if result["ok"] else "plan_dry_run_blocked"
    if result["ok"] and dry_run is False:
        if not agent_trading_enabled():
            blocked = dict(result)
            blocked["ok"] = False
            blocked["blocked_by"] = "agent_trading_disabled"
            blocked["reason"] = "SAYU_AGENT_TRADING 未开启，拒绝写入计划"
            macro.log_agent_run(
                int(account["id"]), decision={"proposal": payload, "result": blocked},
                status="plan_write_blocked",
            )
            return blocked
        applied = apply_revision(
            int(account["id"]), position, result, note=str(payload.get("reason", "")).strip()
        )
        result = dict(result, dry_run=False, **applied)
        macro.log_agent_run(
            int(account["id"]),
            decision={"proposal": payload, "result": result},
            status="plan_applied",
        )
        return result
    macro.log_agent_run(int(account["id"]), decision={"proposal": payload, "result": result}, status=status)
    return result
