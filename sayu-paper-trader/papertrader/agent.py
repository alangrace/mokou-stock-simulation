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
