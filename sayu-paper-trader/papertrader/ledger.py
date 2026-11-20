"""SQLite 账本：账户 / 持仓 / 流水 / 决策 / 净值快照 / 计划。

设计对齐 SayuStock 的 SQLModel 单源账本：
- 流水 append-only；成交时在**同一事务**里写流水 + 改现金 + 改持仓
- 持仓带结构化入场计划（plan_stop_price / plan_stop_pct / plan_take_price / plan_thesis）
- 决策日志 hold 也写，blocked_by 记录"为什么没成交"
- 卖出实现盈亏只累加 principal，不重复进现金
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from .config import DB_PATH, DEFAULT_ACCOUNT, DEFAULT_CASH, DEFAULT_STRATEGY, TZ
from .rules import apply_fill, cash_delta, realized_pnl


SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    strategy TEXT NOT NULL DEFAULT 'multi_factor',
    mode TEXT NOT NULL DEFAULT 'balanced',
    cash REAL NOT NULL,
    initial_cash REAL NOT NULL,
    principal REAL NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position (
    account_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL,
    avg_cost REAL NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_price REAL,
    last_quote_at TEXT,
    plan_stop_price REAL,
    plan_stop_pct REAL,
    plan_take_price REAL,
    plan_thesis TEXT,
    plan_updated_at TEXT,
    plan_rev_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, code)
);

CREATE TABLE IF NOT EXISTS trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    price REAL NOT NULL,
    qty INTEGER NOT NULL,
    amount REAL NOT NULL,
    fee REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'balanced',
    decision_id INTEGER,
    executed_at TEXT NOT NULL,
    trade_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trade_acc_date ON trade(account_id, trade_date);
CREATE INDEX IF NOT EXISTS ix_trade_acc_code ON trade(account_id, code);

CREATE TABLE IF NOT EXISTS decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    code TEXT,
    name TEXT,
    action TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '',
    blocked_by TEXT NOT NULL DEFAULT '',
    trade_id INTEGER,
    data_source TEXT NOT NULL DEFAULT 'live',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decision_acc ON decision(account_id, created_at);

CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    trade_date TEXT NOT NULL,
    cash REAL NOT NULL,
    position_value REAL NOT NULL,
    total_equity REAL NOT NULL,
    day_pnl REAL NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, trade_date)
);

CREATE TABLE IF NOT EXISTS entry_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    plan_id TEXT NOT NULL,
    name TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, plan_id)
);

CREATE TABLE IF NOT EXISTS broadcast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    severity INTEGER NOT NULL DEFAULT 3,
    direction TEXT NOT NULL DEFAULT 'mixed',
    status TEXT NOT NULL DEFAULT 'open',
    result TEXT NOT NULL DEFAULT '',
    stance TEXT NOT NULL DEFAULT '',
    stale INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    regime TEXT NOT NULL DEFAULT '中性',
    phase TEXT NOT NULL DEFAULT '平衡',
    fed_prob_25bp REAL,
    breadth_down TEXT NOT NULL DEFAULT '',
    gold_pct REAL,
    silver_pct REAL,
    copper_pct REAL,
    oil_pct REAL,
    dollar REAL,
    usdcnh REAL,
    note TEXT NOT NULL DEFAULT '',
    sources_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    decision_json TEXT NOT NULL DEFAULT '{}',
    token_in INTEGER NOT NULL DEFAULT 0,
    token_out INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok'
);
"""


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(position)")}
        if "plan_updated_at" not in cols:
            conn.execute("ALTER TABLE position ADD COLUMN plan_updated_at TEXT")
            conn.execute("UPDATE position SET plan_updated_at = opened_at WHERE plan_updated_at IS NULL")
        if "plan_rev_count" not in cols:
            conn.execute("ALTER TABLE position ADD COLUMN plan_rev_count INTEGER NOT NULL DEFAULT 0")


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ============================================================
# 账户
# ============================================================
def create_account(name: str = DEFAULT_ACCOUNT, cash: float = DEFAULT_CASH, strategy: str = DEFAULT_STRATEGY) -> dict:
    init_db()
    with tx() as conn:
        existing = conn.execute("SELECT * FROM account WHERE name = ?", (name,)).fetchone()
        if existing:
            return dict(existing)
        ts = now_iso()
        conn.execute(
            "INSERT INTO account(name, strategy, mode, cash, initial_cash, principal, enabled, created_at) "
            "VALUES (?, ?, 'balanced', ?, ?, ?, 1, ?)",
            (name, strategy, cash, cash, cash, ts),
        )
        row = conn.execute("SELECT * FROM account WHERE name = ?", (name,)).fetchone()
        assert row is not None
        return dict(row)


def get_account(name: str = DEFAULT_ACCOUNT) -> dict | None:
    init_db()
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM account WHERE name = ?", (name,)).fetchone())


def get_account_by_id(account_id: int) -> dict | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone())


def list_accounts() -> list[dict]:
    init_db()
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM account ORDER BY id")]


def update_mode(account_id: int, mode: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE account SET mode = ? WHERE id = ?", (mode, account_id))


# ============================================================
# 持仓
# ============================================================
def positions(account_id: int) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM position WHERE account_id = ? ORDER BY code", (account_id,))]


def get_position(account_id: int, code: str) -> dict | None:
    with connect() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM position WHERE account_id = ? AND code = ?", (account_id, code)
            ).fetchone()
        )


def update_quote(account_id: int, code: str, price: float, at: str) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE position SET last_price = ?, last_quote_at = ? WHERE account_id = ? AND code = ?",
            (price, at, account_id, code),
        )


# ============================================================
# 流水
# ============================================================
def buys_today(account_id: int, day: str) -> list[tuple[str, int]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT code, qty FROM trade WHERE account_id = ? AND trade_date = ? AND side = 'buy'",
            (account_id, day),
        ).fetchall()
    return [(str(r["code"]), int(r["qty"])) for r in rows]


def daily_trade_count(account_id: int, day: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trade WHERE account_id = ? AND trade_date = ?",
            (account_id, day),
        ).fetchone()
    return int(row["n"]) if row else 0


def record_fill(
    *,
    account_id: int,
    code: str,
    name: str,
    side: str,
    price: float,
    qty: int,
    reason: str,
    snapshot: dict | None = None,
    decision_id: int | None = None,
    mode: str = "balanced",
    executed_at: datetime | None = None,
    trade_date: str | None = None,
) -> dict:
    """原子成交：写流水 + 改现金 + 改持仓。返回 trade_id / 成交后持仓 / 现金。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"非法方向: {side!r}")
    if price <= 0 or qty <= 0:
        raise ValueError("价格/数量必须为正")
    if qty % 100 != 0:
        raise ValueError(f"qty 必须为 100 股整手：收到 {qty}")
    executed = executed_at or datetime.now(TZ)
    day = trade_date or executed.date().isoformat()
    amount = round(price * qty, 2)
    fee = _fee_total(side, amount)

    with tx() as conn:
        acc = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
        if acc is None:
            raise RuntimeError(f"账户不存在: {account_id}")
        pos = conn.execute(
            "SELECT * FROM position WHERE account_id = ? AND code = ?", (account_id, code)
        ).fetchone()
        old_qty = int(pos["qty"]) if pos else 0
        old_avg = float(pos["avg_cost"]) if pos else 0.0

        if side == "sell":
            locked = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) AS n FROM trade "
                "WHERE account_id = ? AND code = ? AND trade_date = ? AND side = 'buy'",
                (account_id, code, day),
            ).fetchone()["n"]
            if int(locked) > 0:
                raise ValueError(f"A 股 T+1 拦截：{code} 今日已买入 {int(locked)} 股，今日不可卖")
            if old_qty < qty:
                raise ValueError(f"持仓不足：{code} 持仓 {old_qty} 股，卖出 {qty} 股")

        cash = float(acc["cash"])
        delta = cash_delta(side, amount, fee)
        if side == "buy" and cash + delta < 0:
            raise ValueError(f"现金不足：需 {amount + fee:.2f}，可用 {cash:.2f}")
        pnl = realized_pnl(old_avg, qty, price, fee) if side == "sell" else 0.0

        conn.execute(
            "UPDATE account SET cash = cash + ?, principal = principal + ? WHERE id = ?",
            (delta, pnl if side == "sell" else 0.0, account_id),
        )
        conn.execute(
            "INSERT INTO trade(account_id, code, name, side, price, qty, amount, fee, realized_pnl, "
            "reason, snapshot, mode, decision_id, executed_at, trade_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id, code, name, side, price, qty, amount, fee, pnl,
                reason, json.dumps(snapshot or {}, ensure_ascii=False), mode,
                decision_id, executed.isoformat(timespec="seconds"), day,
            ),
        )
        trade_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

        new_qty, new_avg = apply_fill(old_qty, old_avg, side, qty, price, fee)
        plan = snapshot or {}
        plan_fields_present = any(
            plan.get(key) is not None
            for key in ("plan_stop_price", "plan_stop_pct", "plan_take_price", "plan_thesis")
        )
        plan_updated = (
            executed.isoformat(timespec="seconds")
            if side == "buy" and plan_fields_present
            else None
        )
        if new_qty <= 0:
            conn.execute("DELETE FROM position WHERE account_id = ? AND code = ?", (account_id, code))
        elif pos:
            conn.execute(
                "UPDATE position SET qty = ?, avg_cost = ?, name = ?, last_price = ?, last_quote_at = ?, "
                "updated_at = ?, plan_stop_price = COALESCE(?, plan_stop_price), "
                "plan_stop_pct = COALESCE(?, plan_stop_pct), "
                "plan_take_price = COALESCE(?, plan_take_price), "
                "plan_thesis = COALESCE(?, plan_thesis), "
                "plan_updated_at = COALESCE(?, plan_updated_at) "
                "WHERE account_id = ? AND code = ?",
                (
                    new_qty, new_avg, name, price, executed.isoformat(timespec="seconds"),
                    executed.isoformat(timespec="seconds"),
                    _num(plan.get("plan_stop_price")), _num(plan.get("plan_stop_pct")),
                    _num(plan.get("plan_take_price")), _str(plan.get("plan_thesis")),
                    plan_updated, account_id, code,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO position(account_id, code, name, qty, avg_cost, opened_at, updated_at, "
                "last_price, last_quote_at, plan_stop_price, plan_stop_pct, plan_take_price, plan_thesis, "
                "plan_updated_at, plan_rev_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id, code, name, new_qty, new_avg,
                    executed.isoformat(timespec="seconds"), executed.isoformat(timespec="seconds"),
                    price, executed.isoformat(timespec="seconds"),
                    _num(plan.get("plan_stop_price")), _num(plan.get("plan_stop_pct")),
                    _num(plan.get("plan_take_price")), _str(plan.get("plan_thesis")),
                    plan_updated, 0,
                ),
            )
        cash_after = float(conn.execute("SELECT cash FROM account WHERE id = ?", (account_id,)).fetchone()["cash"])
    return {
        "trade_id": trade_id,
        "side": side,
        "code": code,
        "price": price,
        "qty": qty,
        "amount": amount,
        "fee": fee,
        "realized_pnl": round(pnl, 2),
        "position_qty": new_qty,
        "avg_cost": round(new_avg, 4),
        "cash_after": round(cash_after, 2),
        "trade_date": day,
        "executed_at": executed.isoformat(timespec="seconds"),
    }


def _fee_total(side: str, amount: float) -> float:
    from .rules import calc_fee

    return calc_fee(side, amount).total


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def recent_trades(account_id: int, limit: int = 20, code: str | None = None) -> list[dict]:
    sql = "SELECT * FROM trade WHERE account_id = ?"
    args: list[Any] = [account_id]
    if code:
        sql += " AND code = ?"
        args.append(code)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


# ============================================================
# 决策日志
# ============================================================
def append_decision(
    *,
    account_id: int,
    code: str | None,
    name: str | None,
    action: str,
    score: float,
    reason: str,
    snapshot: dict | None = None,
    blocked_by: str = "",
    trade_id: int | None = None,
    data_source: str = "live",
    created_at: datetime | None = None,
) -> int:
    ts = (created_at or datetime.now(TZ)).isoformat(timespec="seconds")
    with tx() as conn:
        conn.execute(
            "INSERT INTO decision(account_id, code, name, action, score, reason, snapshot, blocked_by, "
            "trade_id, data_source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account_id, code, name, action, round(float(score), 4), reason,
                json.dumps(snapshot or {}, ensure_ascii=False), blocked_by, trade_id, data_source, ts,
            ),
        )
        return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def recent_decisions(account_id: int, limit: int = 20, code: str | None = None) -> list[dict]:
    sql = "SELECT * FROM decision WHERE account_id = ?"
    args: list[Any] = [account_id]
    if code:
        sql += " AND code = ?"
        args.append(code)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


# ============================================================
# 净值快照
# ============================================================
def write_snapshot(
    *,
    account_id: int,
    trade_date: str,
    cash: float,
    position_value: float,
    total_equity: float,
    day_pnl: float,
    total_pnl: float,
) -> dict:
    with tx() as conn:
        conn.execute(
            "INSERT INTO snapshot(account_id, trade_date, cash, position_value, total_equity, day_pnl, "
            "total_pnl, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, trade_date) DO UPDATE SET cash=excluded.cash, "
            "position_value=excluded.position_value, total_equity=excluded.total_equity, "
            "day_pnl=excluded.day_pnl, total_pnl=excluded.total_pnl, created_at=excluded.created_at",
            (account_id, trade_date, cash, position_value, total_equity, day_pnl, total_pnl, now_iso()),
        )
        row = conn.execute(
            "SELECT * FROM snapshot WHERE account_id = ? AND trade_date = ?", (account_id, trade_date)
        ).fetchone()
        assert row is not None
        return dict(row)


def previous_snapshot(account_id: int, before: str) -> dict | None:
    with connect() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM snapshot WHERE account_id = ? AND trade_date < ? ORDER BY trade_date DESC LIMIT 1",
                (account_id, before),
            ).fetchone()
        )


def snapshots(account_id: int, limit: int = 30) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM snapshot WHERE account_id = ? ORDER BY trade_date DESC LIMIT ?",
                (account_id, limit),
            )
        ]


# ============================================================
# 计划
# ============================================================
def save_plan(account_id: int, payload: dict) -> dict:
    plan_id = str(payload["plan_id"])
    name = str(payload["name"])
    ts = now_iso()
    with tx() as conn:
        conn.execute(
            "INSERT INTO entry_plan(account_id, plan_id, name, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?) "
            "ON CONFLICT(account_id, plan_id) DO UPDATE SET name=excluded.name, payload=excluded.payload, "
            "status='active', updated_at=excluded.updated_at",
            (account_id, plan_id, name, json.dumps(payload, ensure_ascii=False), ts, ts),
        )
        row = conn.execute(
            "SELECT * FROM entry_plan WHERE account_id = ? AND plan_id = ?", (account_id, plan_id)
        ).fetchone()
        assert row is not None
        return dict(row)


def get_plan(account_id: int, plan_id: str | None = None) -> dict | None:
    with connect() as conn:
        if plan_id:
            row = conn.execute(
                "SELECT * FROM entry_plan WHERE account_id = ? AND plan_id = ?", (account_id, plan_id)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM entry_plan WHERE account_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["payload"] = json.loads(out["payload"])
        return out


# ============================================================
# 播报
# ============================================================
def broadcast(account_id: int, text: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO broadcast(account_id, text, created_at) VALUES (?, ?, ?)",
            (account_id, text, now_iso()),
        )


def recent_broadcasts(account_id: int, limit: int = 20) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM broadcast WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                (account_id, limit),
            )
        ]
