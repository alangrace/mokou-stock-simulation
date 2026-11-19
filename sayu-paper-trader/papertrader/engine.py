"""决策心跳：读计划 -> 取行情/指标 -> 先卖后买 -> 落库/播报。

一次 ``run_once`` 对应 SayuStock 的"每 30 分钟看一次盘"：
1. 交易时段守卫（非交易时段只写挂起决策，不成交）
2. 持仓退出（计划止损/止盈/技术破坏/T+1）
3. 候选入场（计划阈值 + 趋势过滤 + 仓位预算 + 宏观/止损硬闸）
4. 成交后写流水/现金/持仓，并输出成交冒泡；每条决策（含 hold）都落库
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import ledger, notify, planner, rules
from .calendar import SessionState, ensure_tz, session_state
from .config import TZ
from .indicators import compute
from .market import Bar, MarketClient, MarketError, Quote
from .plan import validate_buy

# 宏观档位是仓位上限：个股逻辑再好也不能越档（复刻原项目三档规则）
MACRO_CAPS: dict[str, tuple[float, float]] = {
    "进攻": (0.80, 0.15),
    "中性": (0.60, 0.10),
    "防御": (0.30, 0.05),
}


@dataclass
class RunResult:
    account_id: int
    account_name: str
    plan_id: str
    data_source: str
    as_of: str
    session: dict
    index: dict
    decisions: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    account: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "plan_id": self.plan_id,
            "data_source": self.data_source,
            "as_of": self.as_of,
            "session": self.session,
            "index": self.index,
            "decisions": self.decisions,
            "trades": self.trades,
            "errors": self.errors,
            "account": self.account,
        }


def _quote_dict(q: Quote) -> dict:
    return {
        "code": q.code,
        "name": q.name,
        "price": q.price,
        "prev_close": q.prev_close,
        "open": q.open,
        "high": q.high,
        "low": q.low,
        "change_pct": q.change_pct,
        "volume": q.volume,
        "amount": q.amount,
        "turnover": q.turnover,
        "ts": q.ts,
    }


def _bar_quote(code: str, name: str, bar: Bar, prev_close: float) -> dict:
    return {
        "code": code,
        "name": name,
        "price": bar.close,
        "prev_close": prev_close,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "change_pct": round((bar.close - prev_close) / prev_close * 100, 4) if prev_close else 0.0,
        "volume": bar.volume,
        "amount": bar.amount,
        "turnover": bar.turnover,
        "ts": bar.date,
    }


def _bars_for(market: MarketClient, code: str, as_of: datetime | None, replay: bool) -> list[Bar]:
    bars = market.kline(code, klt=101, limit=180)
    if replay and as_of is not None:
        cutoff = as_of.date().isoformat()
        bars = [b for b in bars if b.date < cutoff]
    return bars


def _price_map(
    market: MarketClient,
    codes: list[str],
    names: dict[str, str],
    as_of: datetime | None,
    replay: bool,
) -> dict[str, dict]:
    if not replay:
        quotes = market.batch_quotes(codes)
        return {code: _quote_dict(q) for code, q in quotes.items()}
    out: dict[str, dict] = {}
    for code in codes:
        try:
            bars = market.kline(code, klt=101, limit=30)
            prev_close = bars[-2].close if len(bars) >= 2 else 0.0
            bar = market.bar_at(code, as_of) if as_of else None
        except MarketError:
            continue
        if bar is not None:
            out[code] = _bar_quote(code, names.get(code, ""), bar, prev_close)
    return out


def _index_context(market: MarketClient, as_of: datetime | None, replay: bool) -> tuple[dict, str]:
    try:
        bars = _bars_for(market, "1.000001", as_of, replay)
        ind = compute(bars)
        if replay and as_of is not None:
            # 回放必须用 as-of 当时的指数分钟价；日 K 只用于算 MA/RSI
            bar = market.bar_at("1.000001", as_of)
            if bar is not None:
                prev_close = bars[-1].close if bars else bar.open
                price = bar.close
                chg = round((bar.close - prev_close) / prev_close * 100, 4) if prev_close else 0.0
            else:
                price = bars[-1].close
                chg = bars[-1].pct_chg
        else:
            q = market.index_quote()
            price = q.price
            chg = q.change_pct
        if ind.get("above_ma20") and (ind.get("ma20") or 0) > (ind.get("ma60") or 0):
            regime = "进攻"
        elif not ind.get("above_ma20") and (ind.get("ma20") or 0) < (ind.get("ma60") or 0):
            regime = "防御"
        else:
            regime = "中性"
        return {
            "price": price,
            "change_pct": chg,
            "above_ma20": ind.get("above_ma20"),
            "ma20": ind.get("ma20"),
            "ma60": ind.get("ma60"),
            "regime": regime,
        }, regime
    except (MarketError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "regime": "未知"}, "中性"


def _account_view(account: dict, positions: list[dict], quotes: dict[str, dict]) -> dict:
    cash = float(account["cash"])
    position_value = 0.0
    unrealized = 0.0
    rows: list[dict] = []
    for p in positions:
        q = quotes.get(p["code"]) or {}
        price = float(q.get("price") or p.get("last_price") or p["avg_cost"])
        mv = price * int(p["qty"])
        pnl = (price - float(p["avg_cost"])) * int(p["qty"])
        position_value += mv
        unrealized += pnl
        rows.append(
            {
                **p,
                "current_price": round(price, 3),
                "market_value": round(mv, 2),
                "unrealized_pnl": round(pnl, 2),
                "unrealized_pnl_pct": round(pnl / (float(p["avg_cost"]) * int(p["qty"])) * 100, 2)
                if p["avg_cost"] and p["qty"]
                else 0.0,
            }
        )
    equity = cash + position_value
    return {
        "cash": round(cash, 2),
        "position_value": round(position_value, 2),
        "total_equity": round(equity, 2),
        "unrealized_pnl": round(unrealized, 2),
        "realized_pnl": round(float(account["principal"]) - float(account["initial_cash"]), 2),
        "position_count": len(positions),
        "positions": rows,
    }


def run_once(
    *,
    account_name: str,
    plan_id: str | None = None,
    as_of: datetime | None = None,
    data_source: str = "live",
    execute: bool = True,
    market: MarketClient | None = None,
    verbose: bool = True,
) -> RunResult:
    """执行一轮决策。``execute=False`` 为预览：照常评估/写决策，但不成交。"""
    account = ledger.get_account(account_name)
    if account is None:
        raise RuntimeError(f"账户不存在：{account_name}（先执行 init）")
    record = ledger.get_plan(int(account["id"]), plan_id)
    if record is None:
        raise RuntimeError(f"账户 {account_name} 没有启用的入场计划（先执行 plan-load）")
    plan = record["payload"]
    account_id = int(account["id"])
    market = market or MarketClient()
    replay = data_source == "replay"
    when = ensure_tz(as_of) if as_of else datetime.now(TZ)
    session = session_state(when)
    result = RunResult(
        account_id=account_id,
        account_name=account_name,
        plan_id=str(plan["plan_id"]),
        data_source=data_source,
        as_of=when.isoformat(timespec="seconds"),
        session=session.as_dict(),
        index={},
    )

    codes = [str(c["code"]) for c in plan["universe"]]
    names = {str(c["code"]): str(c["name"]) for c in plan["universe"]}
    positions_before = ledger.positions(account_id)
    for p in positions_before:
        codes.append(str(p["code"]))
        names.setdefault(str(p["code"]), str(p.get("name", "")))
    codes = list(dict.fromkeys(codes))

    quotes = _price_map(market, codes, names, when if replay else None, replay)
    index_ctx, index_regime = _index_context(market, when if replay else None, replay)
    result.index = index_ctx

    # 指标：每天每票算一次；失败的票记入 errors，不阻塞其他票
    indicators: dict[str, dict] = {}
    for code in codes:
        try:
            bars = _bars_for(market, code, when if replay else None, replay)
            indicators[code] = compute(bars)
        except (MarketError, ValueError) as exc:
            result.errors.append(f"{code} 指标失败: {type(exc).__name__}: {exc}")

    today = when.date()
    day = today.isoformat()
    buys_today = ledger.buys_today(account_id, day)
    daily_count = ledger.daily_trade_count(account_id, day)
    risk = plan["risk"]

    def emit(text: str) -> None:
        prefix = "[回放] " if replay else ""
        ledger.broadcast(account_id, prefix + text)
        if verbose:
            print(prefix + text)

    def decide(
        *,
        code: str | None,
        name: str | None,
        action: str,
        score: float,
        reason: str,
        snapshot: dict | None = None,
        blocked_by: str = "",
        trade_id: int | None = None,
    ) -> None:
        did = ledger.append_decision(
            account_id=account_id,
            code=code,
            name=name,
            action=action,
            score=score,
            reason=reason,
            snapshot=snapshot,
            blocked_by=blocked_by,
            trade_id=trade_id,
            data_source=data_source,
            created_at=when,
        )
        result.decisions.append(
            {
                "id": did,
                "action": action,
                "code": code,
                "name": name,
                "score": score,
                "reason": reason,
                "blocked_by": blocked_by,
                "trade_id": trade_id,
            }
        )

    # ============ 1) 先处理持仓退出 ============
    positions = ledger.positions(account_id)
    for pos in positions:
        code = str(pos["code"])
        quote = quotes.get(code)
        ind = indicators.get(code)
        if quote is None or ind is None:
            decide(
                code=code, name=pos.get("name"), action="hold", score=0.0,
                reason="行情/指标不可达，本轮无法复核持仓，保持不动",
                blocked_by="no_data",
            )
            continue
        candidate = next((c for c in plan["universe"] if str(c["code"]) == code), {"code": code, "name": pos.get("name")})
        score, _reasons = planner.score_candidate(ind, quote, candidate)
        signal = planner.exit_signal(
            position=pos, quote=quote, score=score, today=today,
            buys_today=buys_today, plan=plan,
        )
        if signal["action"] != "sell":
            decide(
                code=code, name=pos.get("name"), action="hold", score=score,
                reason=signal["reason"], blocked_by=signal["blocked_by"],
                snapshot={"indicators": ind, "quote": quote},
            )
            continue
        if not session.is_open:
            decide(
                code=code, name=pos.get("name"), action="sell", score=score,
                reason=f"{signal['reason']}；但{session.label}，卖单挂起",
                blocked_by="market_closed",
                snapshot={"indicators": ind, "quote": quote, "trigger": signal["trigger"]},
            )
            continue
        if not execute:
            decide(
                code=code, name=pos.get("name"), action="sell", score=score,
                reason=f"{signal['reason']}；预览模式不成交",
                blocked_by="preview", snapshot={"indicators": ind, "quote": quote},
            )
            continue
        if daily_count >= int(risk.get("max_daily_trades", 12)):
            decide(
                code=code, name=pos.get("name"), action="sell", score=score,
                reason=f"{signal['reason']}；但已达单日交易上限 {daily_count}",
                blocked_by="max_daily_trades",
            )
            continue
        try:
            fill = ledger.record_fill(
                account_id=account_id, code=code, name=str(pos.get("name") or ""),
                side="sell", price=float(quote["price"]), qty=int(pos["qty"]),
                reason=signal["reason"],
                snapshot={"plan_stop_price": pos.get("plan_stop_price"), "plan_stop_pct": pos.get("plan_stop_pct"),
                          "plan_take_price": pos.get("plan_take_price"), "trigger": signal["trigger"],
                          "indicators": ind, "quote": quote},
                mode=str(account["mode"]), executed_at=when, trade_date=day,
            )
        except (ValueError, RuntimeError) as exc:
            decide(code=code, name=pos.get("name"), action="sell", score=score,
                   reason=f"{signal['reason']}；成交被拒: {exc}", blocked_by="fill_rejected")
            continue
        daily_count += 1
        decide(
            code=code, name=pos.get("name"), action="sell", score=score,
            reason=signal["reason"], snapshot={"indicators": ind, "quote": quote},
            trade_id=fill["trade_id"],
        )
        result.trades.append(fill)
        sign = "+" if fill["realized_pnl"] >= 0 else ""
        emit(
            f"🔴 卖出 {pos.get('name')}({code}) {fill['qty']} 股 @¥{fill['price']:.2f}"
            f"（{sign}¥{fill['realized_pnl']:.2f}）｜原因：{signal['reason']}"
        )
        buys_today = ledger.buys_today(account_id, day)

    # ============ 2) 候选入场 ============
    held = {str(p["code"]) for p in ledger.positions(account_id)}
    sector_of = {str(c["code"]): str(c.get("sector", "")) for c in plan["universe"]}
    held_sectors: dict[str, int] = {}
    for held_code in held:
        sector = sector_of.get(held_code, "")
        if sector:
            held_sectors[sector] = held_sectors.get(sector, 0) + 1
    macro_regime = str(plan["macro"]["regime"])
    cap_total, cap_single = MACRO_CAPS.get(macro_regime, (1.0, 1.0))
    eff_total = min(float(risk.get("max_total_exposure_pct", 1.0)), cap_total)
    eff_single = min(float(risk.get("max_single_pct", 1.0)), cap_single)
    equity = float(ledger.get_account_by_id(account_id)["cash"])
    positions_now = ledger.positions(account_id)
    for p in positions_now:
        q = quotes.get(str(p["code"])) or {}
        equity += float(q.get("price") or p.get("last_price") or p["avg_cost"]) * int(p["qty"])

    max_entries = int(plan["entry"].get("max_candidates_per_round", 3))
    entries_this_round = 0
    ranked: list[tuple[float, dict]] = []
    for candidate in plan["universe"]:
        code = str(candidate["code"])
        quote = quotes.get(code)
        ind = indicators.get(code)
        if quote is None or ind is None:
            decide(code=code, name=candidate.get("name"), action="hold", score=0.0,
                   reason="行情/指标不可达，跳过", blocked_by="no_data")
            continue
        score, reasons = planner.score_candidate(ind, quote, candidate)
        ranked.append((score, candidate))
        blockers = planner.entry_blockers(plan, candidate, ind, quote, held=code in held)
        if code in held:
            continue
        if blockers:
            decide(code=code, name=candidate.get("name"), action="hold", score=score,
                   reason="；".join(blockers) + "｜" + "、".join(reasons[:4]),
                   blocked_by=(";".join(blockers))[:200],
                   snapshot={"indicators": ind, "quote": quote, "planned": False})
            continue
        if macro_regime == "防御" and not bool(candidate.get("defensive", False)):
            decide(code=code, name=candidate.get("name"), action="hold", score=score,
                   reason=f"宏观防御档只买高股息/公用/必需消费/上游资源；{candidate.get('name')}不在计划防御名单",
                   blocked_by="macro_defensive_only")
            continue
        if score < float(plan["entry"].get("min_score", 0.2)):
            decide(code=code, name=candidate.get("name"), action="hold", score=score,
                   reason=f"评分 {score:.2f} < 计划阈值 {plan['entry']['min_score']:.2f}｜" + "、".join(reasons[:4]),
                   blocked_by="score_below_threshold",
                   snapshot={"indicators": ind, "quote": quote, "planned": False})
            continue
        if entries_this_round >= max_entries:
            decide(code=code, name=candidate.get("name"), action="hold", score=score,
                   reason=f"信号达标但本轮入场名额已满（{max_entries} 只）", blocked_by="round_quota")
            continue
        if daily_count >= int(risk.get("max_daily_trades", 12)):
            decide(code=code, name=candidate.get("name"), action="buy", score=score,
                   reason=f"信号达标但已达单日交易上限 {daily_count}", blocked_by="max_daily_trades")
            continue
        if len(ledger.positions(account_id)) >= int(risk.get("max_positions", 6)):
            decide(code=code, name=candidate.get("name"), action="buy", score=score,
                   reason=f"信号达标但已达最大持仓数 {risk['max_positions']}", blocked_by="max_holdings")
            continue
        sector = str(candidate.get("sector", ""))
        max_per_sector = int(plan["entry"].get("max_per_sector", 99))
        if sector and held_sectors.get(sector, 0) >= max_per_sector:
            decide(code=code, name=candidate.get("name"), action="hold", score=score,
                   reason=f"信号达标但行业「{sector}」已达计划上限 {max_per_sector} 只",
                   blocked_by="sector_cap")
            continue
        qty, budget, note = planner.position_budget(
            plan, candidate, equity, float(quote["price"]),
            float(ledger.get_account_by_id(account_id)["cash"]),
            max_total_pct=eff_total, max_single_pct=eff_single,
        )
        if qty <= 0:
            decide(code=code, name=candidate.get("name"), action="hold", score=score,
                   reason=f"信号达标但{note}", blocked_by="budget")
            continue
        snapshot = planner.snapshot_for_buy(plan, candidate, ind, quote, qty, score)
        gate = validate_buy(plan, snapshot)
        if gate:
            decide(code=code, name=candidate.get("name"), action="buy", score=score,
                   reason=gate, blocked_by="strategy_gate")
            continue
        if not session.is_open:
            decide(code=code, name=candidate.get("name"), action="buy", score=score,
                   reason=(
                       f"{session.label}，入场计划挂起：拟买 {qty} 股 @¥{quote['price']:.2f}；"
                       f"宏观:{plan['macro']['regime']}｜" + "、".join(reasons[:4])
                   ),
                   blocked_by="market_closed", snapshot=snapshot)
            entries_this_round += 1
            if sector:
                held_sectors[sector] = held_sectors.get(sector, 0) + 1
            continue
        if not execute:
            decide(code=code, name=candidate.get("name"), action="buy", score=score,
                   reason=f"预览模式不成交：拟买 {qty} 股 @¥{quote['price']:.2f}",
                   blocked_by="preview", snapshot=snapshot)
            entries_this_round += 1
            if sector:
                held_sectors[sector] = held_sectors.get(sector, 0) + 1
            continue
        try:
            fill = ledger.record_fill(
                account_id=account_id, code=code, name=str(candidate.get("name") or ""),
                side="buy", price=float(quote["price"]), qty=qty,
                reason=f"入场计划：{candidate.get('thesis', '')}",
                snapshot=snapshot, mode=str(account["mode"]), executed_at=when, trade_date=day,
            )
        except (ValueError, RuntimeError) as exc:
            decide(code=code, name=candidate.get("name"), action="buy", score=score,
                   reason=f"成交被拒: {exc}", blocked_by="fill_rejected")
            continue
        daily_count += 1
        entries_this_round += 1
        held.add(code)
        sector = str(candidate.get("sector", ""))
        if sector:
            held_sectors[sector] = held_sectors.get(sector, 0) + 1
        decide(code=code, name=candidate.get("name"), action="buy", score=score,
               reason=(
                   f"宏观:{plan['macro']['regime']}｜计划入场：{candidate.get('thesis', '')}"
                   f"｜计划止损 {snapshot['plan_stop_pct']:.2%} / 止盈 {snapshot['plan_take_pct']:.2%}"
               ),
               snapshot=snapshot, trade_id=fill["trade_id"])
        result.trades.append(fill)
        emit(
            f"🟢 买入 {candidate.get('name')}({code}) {fill['qty']} 股 @¥{fill['price']:.2f}"
            f"｜原因：{candidate.get('thesis', '')}"
        )
        if len(held) >= int(risk.get("max_positions", 6)):
            break

    fresh_account = ledger.get_account_by_id(account_id)
    assert fresh_account is not None
    result.account = _account_view(fresh_account, ledger.positions(account_id), quotes)
    result.account["index_regime"] = index_regime
    if result.trades and execute and not replay:
        notify.notify_fills(result.trades, account_name)
    return result


def write_daily_snapshot(account_name: str, market: MarketClient | None = None) -> dict:
    account = ledger.get_account(account_name)
    if account is None:
        raise RuntimeError(f"账户不存在：{account_name}")
    market = market or MarketClient()
    positions = ledger.positions(int(account["id"]))
    quotes: dict[str, dict] = {}
    if positions:
        try:
            raw = market.batch_quotes([str(p["code"]) for p in positions])
            quotes = {code: _quote_dict(q) for code, q in raw.items()}
        except MarketError:
            quotes = {}
    view = _account_view(account, positions, quotes)
    day = datetime.now(TZ).date().isoformat()
    prev = ledger.previous_snapshot(int(account["id"]), day)
    prev_equity = float(prev["total_equity"]) if prev else float(account["initial_cash"])
    day_pnl = float(view["total_equity"]) - prev_equity
    total_pnl = float(view["total_equity"]) - float(account["initial_cash"])
    return ledger.write_snapshot(
        account_id=int(account["id"]),
        trade_date=day,
        cash=float(view["cash"]),
        position_value=float(view["position_value"]),
        total_equity=float(view["total_equity"]),
        day_pnl=round(day_pnl, 2),
        total_pnl=round(total_pnl, 2),
    )
