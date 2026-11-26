"""Agent 模式上下文：把一轮决策所需的数据一次性交给 DSH 模型。

P1 只读：不写流水、不改持仓、不改计划。
"""

from __future__ import annotations

import json
from typing import Any

from . import ledger, macro, planner, report
from .calendar import session_state
from .engine import _index_context, _quote_dict
from .indicators import compute
from .market import MarketClient


def _candidate_context(
    market: MarketClient,
    plan: dict,
    candidate: dict,
    quotes: dict,
    held: bool,
) -> dict:
    code = str(candidate.get("code", ""))
    quote = quotes.get(code)
    item: dict[str, Any] = {
        "code": code,
        "name": candidate.get("name", ""),
        "sector": candidate.get("sector", ""),
        "thesis": candidate.get("thesis", ""),
        "weight": candidate.get("weight", 1.0),
        "held": held,
        "quote": None,
        "indicators": None,
        "score": None,
        "reasons": [],
        "blockers": [],
        "error": "",
    }
    if quote is None:
        item["error"] = "无有效报价"
        return item
    q = _quote_dict(quote)
    item["quote"] = q
    try:
        ind = compute(market.kline(code, 101, 180))
    except Exception as exc:  # noqa: BLE001 - 外部行情/数据不足
        item["error"] = f"{type(exc).__name__}: {exc}"
        return item
    item["indicators"] = ind
    score, reasons = planner.score_candidate(ind, q, candidate)
    item["score"] = score
    item["reasons"] = reasons
    item["blockers"] = planner.entry_blockers(plan, candidate, ind, q, held=False)
    return item


def build_context(account_name: str, market: MarketClient | None = None) -> dict:
    market = market or MarketClient()
    data = report.collect(account_name, market)
    plan_record = data.get("plan")
    plan = plan_record["payload"] if plan_record else {}
    view = data["view"]
    positions = view["positions"]
    held = {str(p["code"]) for p in positions}

    universe = plan.get("universe", []) if plan else []
    codes = [str(c.get("code", "")) for c in universe] + [str(p["code"]) for p in positions]
    codes = [c for c in dict.fromkeys(codes) if c]
    quotes = market.batch_quotes(codes) if codes else {}

    candidates = [
        _candidate_context(market, plan, candidate, quotes, held=str(candidate.get("code", "")) in held)
        for candidate in universe
    ]

    index_ctx, index_regime = _index_context(market, None, False)
    snapshot = macro.latest_snapshot()
    if snapshot and snapshot.get("sources_json"):
        try:
            snapshot["sources"] = json.loads(snapshot["sources_json"])
        except (TypeError, ValueError):
            snapshot["sources"] = []
    else:
        if snapshot:
            snapshot["sources"] = []

    account = dict(data["account"])
    account.update(
        {
            "position_value": view["position_value"],
            "total_equity": view["total_equity"],
            "unrealized_pnl": view["unrealized_pnl"],
            "realized_pnl": view["realized_pnl"],
            "position_count": view["position_count"],
        }
    )

    return {
        "as_of": data["as_of"],
        "session": session_state().as_dict(),
        "index": {**index_ctx, "regime": index_regime},
        "account": account,
        "plan": plan_record,
        "positions": positions,
        "candidates": candidates,
        "macro_events": macro.list_events(status="open"),
        "macro_snapshot": snapshot,
        "recent_trades": data["trades"],
        "recent_decisions": data["decisions"],
    }

_KEEP_INDICATORS = (
    "close", "ma20", "ma60", "above_ma20", "ma_bull", "ma_bear",
    "rsi6", "macd_golden_cross_3d", "macd_death_cross_3d", "vol_ratio",
    "pct_20d", "atr_pct",
)


def compact_context(context: dict, keep_decisions: int = 12, keep_trades: int = 8) -> dict:
    """把上下文裁到决策必需字段，减少 token 消耗。

    - 候选只留 code/name/sector/held/quote/score/reasons/blockers 与少量关键指标；
    - 历史决策/成交只留最近若干条；
    - 计划里去掉 universe 的 thesis 长文（候选里已有）以外的冗余字段不动。
    """
    out = dict(context)
    candidates = []
    for item in context.get("candidates", []):
        ind = item.get("indicators") or {}
        slim = {k: item.get(k) for k in ("code", "name", "sector", "held", "weight", "score", "reasons", "blockers", "error")}
        if item.get("held"):
            slim["thesis"] = item.get("thesis", "")
        quote = item.get("quote") or None
        slim["quote"] = {k: quote.get(k) for k in ("price", "change_pct", "name")} if quote else None
        slim["indicators"] = {k: ind.get(k) for k in _KEEP_INDICATORS if k in ind}
        candidates.append(slim)
    out["candidates"] = candidates
    out["recent_decisions"] = context.get("recent_decisions", [])[:keep_decisions]
    out["recent_trades"] = context.get("recent_trades", [])[:keep_trades]
    plan_record = context.get("plan") or {}
    if plan_record.get("payload"):
        plan = dict(plan_record["payload"])
        plan["universe"] = [
            {k: c.get(k) for k in ("code", "name", "sector", "defensive", "weight")}
            for c in plan.get("universe", [])
        ]
        out["plan"] = dict(plan_record, payload=plan)
    return out

