"""宏观事件表与宏观快照。

P1 只读骨架：Agent 通过 CLI 写入事件/快照，交易引擎暂不读取。
"""

from __future__ import annotations

import json
from typing import Any

from . import ledger

REGIMES = ("进攻", "中性", "防御")
PHASES = ("成长相", "价值相", "平衡")
DIRECTIONS = ("risk_off", "risk_on", "mixed")
STATUSES = ("open", "settled")


def _str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return default
    return str(value).strip()


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def upsert_event(payload: dict) -> dict:
    ledger.init_db()
    slug = _str(payload.get("slug"))
    title = _str(payload.get("title"))
    if not slug or not title:
        raise ValueError("macro_event 需要 slug 和 title")
    try:
        severity = int(payload.get("severity", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("severity 必须是 1~5 的整数") from exc
    if not 1 <= severity <= 5:
        raise ValueError("severity 必须是 1~5")
    direction = _str(payload.get("direction", "mixed")).lower()
    if direction not in DIRECTIONS:
        raise ValueError(f"direction 必须是 {DIRECTIONS}")
    status = _str(payload.get("status", "open")).lower()
    if status not in STATUSES:
        raise ValueError(f"status 必须是 {STATUSES}")
    stale = 1 if bool(payload.get("stale", True)) else 0
    result = _str(payload.get("result"))
    stance = _str(payload.get("stance"))
    now = ledger.now_iso()
    with ledger.tx() as conn:
        existing = conn.execute("SELECT id FROM macro_event WHERE slug = ?", (slug,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE macro_event SET title=?, severity=?, direction=?, status=?, result=?, "
                "stance=?, stale=?, updated_at=? WHERE slug=?",
                (title, severity, direction, status, result, stance, stale, now, slug),
            )
        else:
            conn.execute(
                "INSERT INTO macro_event(slug, title, severity, direction, status, result, stance, "
                "stale, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (slug, title, severity, direction, status, result, stance, stale, now, now),
            )
        row = conn.execute("SELECT * FROM macro_event WHERE slug = ?", (slug,)).fetchone()
        assert row is not None
        return dict(row)


def list_events(status: str | None = None, limit: int = 100) -> list[dict]:
    ledger.init_db()
    sql = "SELECT * FROM macro_event"
    args: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY severity DESC, updated_at DESC LIMIT ?"
    args.append(limit)
    with ledger.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def get_event(slug: str) -> dict | None:
    ledger.init_db()
    with ledger.connect() as conn:
        row = conn.execute("SELECT * FROM macro_event WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None


def save_snapshot(payload: dict) -> dict:
    ledger.init_db()
    regime = _str(payload.get("regime", "中性"))
    if regime not in REGIMES:
        raise ValueError(f"regime 必须是 {REGIMES}")
    phase = _str(payload.get("phase", "平衡"))
    if phase not in PHASES:
        raise ValueError(f"phase 必须是 {PHASES}")
    sources = payload.get("sources") or []
    if not isinstance(sources, list):
        sources = [str(sources)]
    snapshot = {
        "as_of": _str(payload.get("as_of")) or ledger.now_iso(),
        "regime": regime,
        "phase": phase,
        "fed_prob_25bp": _num(payload.get("fed_prob_25bp")),
        "breadth_down": _str(payload.get("breadth_down")),
        "gold_pct": _num(payload.get("gold_pct")),
        "silver_pct": _num(payload.get("silver_pct")),
        "copper_pct": _num(payload.get("copper_pct")),
        "oil_pct": _num(payload.get("oil_pct")),
        "dollar": _num(payload.get("dollar")),
        "usdcnh": _num(payload.get("usdcnh")),
        "note": _str(payload.get("note")),
        "sources_json": json.dumps([str(s) for s in sources], ensure_ascii=False),
        "created_at": ledger.now_iso(),
    }
    with ledger.tx() as conn:
        conn.execute(
            "INSERT INTO macro_snapshot(as_of, regime, phase, fed_prob_25bp, breadth_down, gold_pct, "
            "silver_pct, copper_pct, oil_pct, dollar, usdcnh, note, sources_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot["as_of"], snapshot["regime"], snapshot["phase"], snapshot["fed_prob_25bp"],
                snapshot["breadth_down"], snapshot["gold_pct"], snapshot["silver_pct"],
                snapshot["copper_pct"], snapshot["oil_pct"], snapshot["dollar"], snapshot["usdcnh"],
                snapshot["note"], snapshot["sources_json"], snapshot["created_at"],
            ),
        )
        row = conn.execute("SELECT * FROM macro_snapshot WHERE id = last_insert_rowid()").fetchone()
        assert row is not None
        return dict(row)


def latest_snapshot() -> dict | None:
    ledger.init_db()
    with ledger.connect() as conn:
        row = conn.execute("SELECT * FROM macro_snapshot ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def log_agent_run(
    account_id: int,
    *,
    model: str = "",
    prompt_hash: str = "",
    decision: dict | None = None,
    token_in: int = 0,
    token_out: int = 0,
    status: str = "ok",
    started_at: str | None = None,
) -> int:
    with ledger.tx() as conn:
        conn.execute(
            "INSERT INTO agent_run(account_id, started_at, model, prompt_hash, decision_json, "
            "token_in, token_out, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id, started_at or ledger.now_iso(), model, prompt_hash,
                json.dumps(decision or {}, ensure_ascii=False), int(token_in), int(token_out), status,
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def list_agent_runs(limit: int = 50) -> list[dict]:
    ledger.init_db()
    with ledger.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM agent_run ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        ]
