"""状态报告：账户 / 持仓 / 流水 / 决策 / 计划的文本与 Markdown 输出。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import ledger
from .config import TZ
from .engine import MACRO_CAPS, _account_view, _quote_dict
from .market import MarketClient, MarketError


def collect(account_name: str, market: MarketClient | None = None) -> dict[str, Any]:
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
    plan = ledger.get_plan(int(account["id"]))
    return {
        "as_of": datetime.now(TZ).isoformat(timespec="seconds"),
        "account": account,
        "view": view,
        "plan": plan,
        "trades": ledger.recent_trades(int(account["id"]), limit=20),
        "decisions": ledger.recent_decisions(int(account["id"]), limit=25),
        "broadcasts": ledger.recent_broadcasts(int(account["id"]), limit=20),
        "snapshots": ledger.snapshots(int(account["id"]), limit=10),
    }


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def render_text(data: dict[str, Any]) -> str:
    acc = data["account"]
    view = data["view"]
    lines = [
        f"【模拟盘】{acc['name']}  id={acc['id']}  策略={acc['strategy']}  模式={acc['mode']}",
        f"数据时点：{data['as_of']}",
        f"现金 ¥{_fmt_money(view['cash'])} ｜ 持仓市值 ¥{_fmt_money(view['position_value'])} ｜ "
        f"总资产 ¥{_fmt_money(view['total_equity'])}",
        f"浮盈 ¥{_fmt_money(view['unrealized_pnl'])} ｜ 已实现 ¥{_fmt_money(view['realized_pnl'])} ｜ "
        f"持仓 {view['position_count']} 只",
    ]
    plan = data["plan"]
    if plan:
        p = plan["payload"]
        lines.append(
            f"入场计划：{p['name']}（{p['plan_id']}）｜宏观 {p['macro']['regime']}｜"
            f"候选 {len(p['universe'])} 只｜评分阈值 {p['entry']['min_score']}"
        )
    if view["positions"]:
        lines.append("")
        lines.append("持仓：")
        for row in view["positions"]:
            chg = row.get("change_pct")
            day_pnl = row.get("day_pnl")
            chg_text = f"{chg:+.2f}%" if chg is not None else "—"
            day_text = f"{day_pnl:+,.2f}" if day_pnl is not None else "—"
            lines.append(
                f"  {row['name']}({row['code']}) {row['qty']} 股 成本 {row['avg_cost']:.3f} "
                f"现价 {row['current_price']:.3f} 今日 {chg_text}（{day_text}） "
                f"市值 {_fmt_money(row['market_value'])} "
                f"浮盈 {row['unrealized_pnl']:+.2f}（{row['unrealized_pnl_pct']:+.2f}%）"
                f" 止损 {row.get('plan_stop_price') or '—'} 止盈 {row.get('plan_take_price') or '—'}"
            )
    else:
        lines.append("持仓：空仓")

    lines.append("")
    lines.append("最近成交：")
    if data["trades"]:
        for t in data["trades"][:10]:
            sign = "🟢买入" if t["side"] == "buy" else "🔴卖出"
            lines.append(
                f"  #{t['id']} {t['executed_at']} {sign} {t['name']}({t['code']}) "
                f"{t['qty']}股 @{t['price']:.3f} 费{t['fee']:.2f} 盈亏{t['realized_pnl']:+.2f}"
            )
    else:
        lines.append("  （无）")

    lines.append("")
    lines.append("最近决策（含挂起/未成交）：")
    if data["decisions"]:
        for d in data["decisions"][:12]:
            tag = d["blocked_by"] or "-"
            lines.append(
                f"  #{d['id']} {d['created_at']} {d['action']} {d['code'] or '-'} "
                f"score={d['score']:+.2f} [{tag}] {d['reason'][:110]}"
            )
    else:
        lines.append("  （无）")
    return "\n".join(lines)


def render_markdown(data: dict[str, Any]) -> str:
    acc = data["account"]
    view = data["view"]
    plan = data["plan"]
    lines = [
        f"# 模拟盘状态 · {acc['name']}",
        "",
        f"- 数据时点：{data['as_of']}",
        f"- 账户 id：{acc['id']}｜策略：{acc['strategy']}｜模式：{acc['mode']}",
        "",
        "## 账户总览",
        "",
        "| 现金 | 持仓市值 | 总资产 | 持仓浮盈 | 已实现盈亏 | 持仓数 |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {_fmt_money(view['cash'])} | {_fmt_money(view['position_value'])} | "
        f"{_fmt_money(view['total_equity'])} | {_fmt_money(view['unrealized_pnl'])} | "
        f"{_fmt_money(view['realized_pnl'])} | {view['position_count']} |",
        "",
        "## 每日净值快照",
        "",
    ]
    if data.get("snapshots"):
        lines += [
            "| 日期 | 现金 | 持仓市值 | 总资产 | 当日盈亏 | 累计盈亏 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for snap in data["snapshots"]:
            lines.append(
                f"| {snap['trade_date']} | {_fmt_money(snap['cash'])} | {_fmt_money(snap['position_value'])} | "
                f"{_fmt_money(snap['total_equity'])} | {snap['day_pnl']:+.2f} | {snap['total_pnl']:+.2f} |"
            )
    else:
        lines.append("（无）")

    lines += [
        "",
        "## 持仓",
        "",
    ]
    if view["positions"]:
        lines += [
            "| 代码 | 名称 | 股数 | 成本 | 现价 | 市值 | 浮盈 | 收益率 | 计划止损 | 计划止盈 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in view["positions"]:
            lines.append(
                f"| {row['code']} | {row['name']} | {row['qty']} | {row['avg_cost']:.3f} | "
                f"{row['current_price']:.3f} | {_fmt_money(row['market_value'])} | "
                f"{row['unrealized_pnl']:+.2f} | {row['unrealized_pnl_pct']:+.2f}% | "
                f"{row.get('plan_stop_price') or '—'} | {row.get('plan_take_price') or '—'} |"
            )
    else:
        lines.append("（空仓）")

    lines += ["", "## 入场计划", ""]
    if plan:
        p = plan["payload"]
        lines += [
            f"- 计划：**{p['name']}**（{p['plan_id']}，状态 {plan['status']}）",
            f"- 宏观档位：**{p['macro']['regime']}** — {p['macro']['note']}",
            f"- 风险预算：总仓 ≤ {p['risk']['max_total_exposure_pct']:.0%}，"
            f"单票 ≤ {p['risk']['max_single_pct']:.0%}，最多 {p['risk']['max_positions']} 只，"
            f"最低现金 {p['risk']['min_cash_pct']:.0%}",
            f"- 宏观档位实际上限：总仓 ≤ {MACRO_CAPS.get(p['macro']['regime'], (1, 1))[0]:.0%}，"
            f"单票 ≤ {MACRO_CAPS.get(p['macro']['regime'], (1, 1))[1]:.0%}（与计划上限取更小值）",
            f"- 入场阈值：score ≥ {p['entry']['min_score']}，须站上 MA20，"
            f"RSI6 {p['entry']['min_rsi6']:.0f}~{p['entry']['max_rsi6']:.0f}",
            "",
            "| 代码 | 名称 | 行业 | 计划权重 | 止损 | 止盈 | 入场逻辑 |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        for c in p["universe"]:
            lines.append(
                f"| {c['code']} | {c['name']} | {c.get('sector', '')} | {c.get('weight', 1.0):.2f} | "
                f"{c.get('stop_pct', p['entry']['stop_pct']):.1%} | "
                f"{c.get('take_pct', p['entry']['take_pct']):.1%} | {c['thesis']} |"
            )

    lines += ["", "## 最近成交", ""]
    if data["trades"]:
        lines += [
            "| id | 时间 | 方向 | 代码 | 名称 | 股数 | 价格 | 金额 | 费用 | 已实现盈亏 | 原因 |",
            "|---:|---|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
        for t in data["trades"]:
            side = "买入" if t["side"] == "buy" else "卖出"
            lines.append(
                f"| {t['id']} | {t['executed_at']} | {side} | {t['code']} | {t['name']} | {t['qty']} | "
                f"{t['price']:.3f} | {_fmt_money(t['amount'])} | {t['fee']:.2f} | "
                f"{t['realized_pnl']:+.2f} | {t['reason'][:80]} |"
            )
    else:
        lines.append("（无）")

    lines += ["", "## 最近决策（含 hold / 挂起）", ""]
    if data["decisions"]:
        lines += [
            "| id | 时间 | 动作 | 代码 | score | 拦截 | 理由 |",
            "|---:|---|---|---|---:|---|---|",
        ]
        for d in data["decisions"]:
            lines.append(
                f"| {d['id']} | {d['created_at']} | {d['action']} | {d['code'] or '—'} | "
                f"{d['score']:+.2f} | {d['blocked_by'] or '—'} | {d['reason'][:120]} |"
            )
    else:
        lines.append("（无）")

    lines += ["", "## 成交播报记录", ""]
    if data["broadcasts"]:
        for b in data["broadcasts"]:
            lines.append(f"- {b['created_at']} {b['text']}")
    else:
        lines.append("（无）")

    lines += ["", "---", "", "> 本表由独立复刻程序生成，仅用于模拟盘研究，不构成投资建议。"]
    return "\n".join(lines)
