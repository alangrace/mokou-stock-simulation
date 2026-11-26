"""静态可视化仪表盘：把 SQLite 账本渲染成一个自包含 HTML。

设计目标：
- 不依赖 Web 框架 / 前端 CDN，双击 reports/dashboard.html 即可看；
- 每 30 秒自动刷新页面（meta refresh），数据由 papertrader tick 每 30 分钟重写；
- 账户、净值曲线、持仓、成交、决策、入场计划一页看完。
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from . import ledger, report
from .calendar import session_state
from .config import REPORT_DIR, TZ

CSS = """
:root {
  --bg: #0b1020;
  --panel: #131a2e;
  --panel-2: #18213a;
  --line: #24304d;
  --text: #e6edf7;
  --muted: #93a4c3;
  --accent: #38bdf8;
  --green: #22c55e;
  --red: #ef4444;
  --amber: #f59e0b;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: radial-gradient(1200px 600px at 10% -10%, #16224a 0%, var(--bg) 55%);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro SC", "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 14px;
}
a { color: var(--accent); text-decoration: none; }
.header { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
.title { font-size: 26px; font-weight: 700; letter-spacing: .5px; }
.subtitle { color: var(--muted); margin-top: 6px; }
.badge { display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); color: var(--muted); margin-left: 6px; }
.badge.on { color: var(--green); border-color: rgba(34,197,94,.45); background: rgba(34,197,94,.08); }
.badge.off { color: var(--red); border-color: rgba(239,68,68,.45); background: rgba(239,68,68,.08); }
.badge.warn { color: var(--amber); border-color: rgba(245,158,11,.45); background: rgba(245,158,11,.08); }
.btn { display: inline-block; background: var(--panel-2); color: var(--text); border: 1px solid var(--line); padding: 8px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; text-decoration: none; }
.btn:hover { border-color: var(--accent); color: var(--accent); }
.cards { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
.card { background: linear-gradient(180deg, var(--panel), var(--panel-2)); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.card .label { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
.card .value { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.card .sub { color: var(--muted); font-size: 12px; margin-top: 6px; }
.up { color: var(--green); }
.down { color: var(--red); }
.flat { color: var(--muted); }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin-bottom: 16px; overflow: hidden; }
.panel h2 { font-size: 15px; margin: 0 0 12px; color: var(--text); font-weight: 600; }
.panel h2 .hint { color: var(--muted); font-weight: 400; font-size: 12px; margin-left: 8px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
td.left, th.left { text-align: left; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: rgba(56,189,248,.04); }
.side { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 12px; }
.side.buy { color: var(--green); background: rgba(34,197,94,.1); }
.side.sell { color: var(--red); background: rgba(239,68,68,.1); }
.side.hold { color: var(--muted); background: rgba(147,164,195,.1); }
.tag { display: inline-block; padding: 2px 7px; border-radius: 6px; font-size: 11px; color: var(--muted); background: rgba(147,164,195,.08); border: 1px solid var(--line); }
.empty { color: var(--muted); padding: 18px; text-align: center; border: 1px dashed var(--line); border-radius: 8px; }
.plan-line { color: var(--muted); line-height: 1.8; }
.plan-line b { color: var(--text); }
details { margin-top: 10px; }
summary { cursor: pointer; color: var(--accent); font-size: 13px; }
.footer { color: var(--muted); font-size: 12px; line-height: 1.7; margin-top: 8px; }
.mono { font-variant-numeric: tabular-nums; }
@media (max-width: 1100px) { .cards { grid-template-columns: repeat(3, 1fr); } .grid { grid-template-columns: 1fr; } }
@media (max-width: 640px) { body { padding: 14px; } .cards { grid-template-columns: repeat(2, 1fr); } }
"""


def _esc(value: object) -> str:
    return html.escape(str(value))


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _cls(value: float | None) -> str:
    if value is None or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def _equity_svg(snapshots: list[dict]) -> str:
    rows = sorted(snapshots, key=lambda s: s["trade_date"])
    if not rows:
        return '<div class="empty">暂无净值快照。收盘后 papertrader tick / snapshot 会写入。</div>'
    if len(rows) == 1:
        s = rows[0]
        return (
            '<div class="empty">仅 1 个快照：'
            f'{_esc(s["trade_date"])} · 总资产 {_money(s["total_equity"])} · '
            f'当日 {s["day_pnl"]:+.2f}。明日起积累多个快照后显示曲线。</div>'
        )

    width, height, pad = 900.0, 260.0, 42.0
    values = [float(s["total_equity"]) for s in rows]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1.0
    span = hi - lo
    lo -= span * 0.08
    hi += span * 0.08
    inner_w, inner_h = width - pad * 2, height - pad * 2

    points: list[str] = []
    for i, value in enumerate(values):
        x = pad + (inner_w * i / (len(values) - 1))
        y = pad + inner_h - (value - lo) / (hi - lo) * inner_h
        points.append(f"{x:.1f},{y:.1f}")

    grid = []
    for i in range(5):
        y = pad + inner_h * i / 4
        val = hi - (hi - lo) * i / 4
        grid.append(
            f'<line x1="{pad}" y1="{y:.1f}" x2="{width-pad}" y2="{y:.1f}" stroke="#24304d" stroke-width="1"/>'
            f'<text x="8" y="{y+4:.1f}" fill="#93a4c3" font-size="11">{val:,.0f}</text>'
        )

    first, last = rows[0], rows[-1]
    color = "#22c55e" if values[-1] >= values[0] else "#ef4444"
    dots = "".join(
        f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="3" fill="{color}"/>'
        for p in points
    )
    return f"""
    <svg viewBox="0 0 {width:.0f} {height:.0f}" width="100%" height="260" role="img" aria-label="净值曲线">
      {''.join(grid)}
      <polyline points="{' '.join(points)}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>
      {dots}
      <text x="{pad}" y="{height-12}" fill="#93a4c3" font-size="11">{_esc(first["trade_date"])}</text>
      <text x="{width-pad}" y="{height-12}" fill="#93a4c3" font-size="11" text-anchor="end">{_esc(last["trade_date"])}</text>
      <text x="{width-pad}" y="{pad-14}" fill="#93a4c3" font-size="11" text-anchor="end">单位：元</text>
    </svg>
    """


def _positions_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty">当前空仓</div>'
    body = []
    for p in rows:
        price = float(p["current_price"])
        stop = p.get("plan_stop_price")
        take = p.get("plan_take_price")
        dist = ((price - float(stop)) / price * 100) if stop and price else None
        chg = p.get("change_pct")
        day_pnl = p.get("day_pnl")
        chg_cls = _cls(float(chg)) if chg is not None else "flat"
        chg_text = _pct(float(chg)) if chg is not None else "—"
        if day_pnl is None:
            day_cls, day_text = "flat", "—"
        else:
            day_cls, day_text = _cls(float(day_pnl)), f"{float(day_pnl):+,.2f}"
        body.append(
            "<tr>"
            f'<td class="left">{_esc(p["code"])}</td>'
            f'<td class="left">{_esc(p["name"])}</td>'
            f'<td>{int(p["qty"]):,}</td>'
            f'<td>{float(p["avg_cost"]):.3f}</td>'
            f'<td>{price:.3f}</td>'
            f'<td class="{chg_cls}">{chg_text}</td>'
            f'<td class="{day_cls}">{day_text}</td>'
            f'<td>{_money(float(p["market_value"]))}</td>'
            f'<td class="{_cls(float(p["unrealized_pnl"]))}">{float(p["unrealized_pnl"]):+,.2f}</td>'
            f'<td class="{_cls(float(p["unrealized_pnl_pct"]))}">{_pct(float(p["unrealized_pnl_pct"]))}</td>'
            f'<td>{stop if stop else "—"}</td>'
            f'<td>{take if take else "—"}</td>'
            f'<td class="{"down" if dist is not None and dist < 5 else "flat"}">{_pct(dist) if dist is not None else "—"}</td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        '<th class="left">代码</th><th class="left">名称</th><th>股数</th><th>成本</th><th>现价</th>'
        '<th>今日涨跌</th><th>今日盈亏</th>'
        '<th>市值</th><th>浮盈</th><th>收益率</th><th>计划止损</th><th>计划止盈</th><th>距止损</th>'
        '</tr></thead><tbody>' + "".join(body) + "</tbody></table></div>"
    )


def _trades_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty">暂无成交</div>'
    body = []
    for t in rows:
        side = "buy" if t["side"] == "buy" else "sell"
        side_text = "买入" if side == "buy" else "卖出"
        source = str(t.get("source") or "rule")
        source_text = "Agent" if source == "agent" else "规则"
        body.append(
            "<tr>"
            f'<td class="left">{_esc(t["executed_at"][5:19])}</td>'
            f'<td class="left"><span class="side {side}">{side_text}</span></td>'
            f'<td class="left">{_esc(t["name"])} ({_esc(t["code"])})</td>'
            f'<td class="left">{source_text}</td>'
            f'<td>{int(t["qty"]):,}</td>'
            f'<td>{float(t["price"]):.3f}</td>'
            f'<td>{_money(float(t["amount"]))}</td>'
            f'<td>{float(t["fee"]):.2f}</td>'
            f'<td class="{_cls(float(t["realized_pnl"]))}">{float(t["realized_pnl"]):+,.2f}</td>'
            f'<td class="left">{_esc(t["reason"][:60])}</td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        '<th class="left">时间</th><th class="left">方向</th><th class="left">标的</th>'
        '<th class="left">来源</th><th>股数</th>'
        '<th>价格</th><th>金额</th><th>费用</th><th>已实现盈亏</th><th class="left">原因</th>'
        '</tr></thead><tbody>' + "".join(body) + "</tbody></table></div>"
    )


def _decisions_table(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty">暂无决策</div>'
    body = []
    for d in rows[:24]:
        action = str(d["action"])
        tone = "buy" if action == "buy" else "sell" if action == "sell" else "hold"
        body.append(
            "<tr>"
            f'<td class="left">{_esc(d["created_at"][5:19])}</td>'
            f'<td class="left"><span class="side {tone}">{_esc(action)}</span></td>'
            f'<td class="left">{_esc(d["code"] or "—")}</td>'
            f'<td class="{_cls(float(d["score"]))}">{float(d["score"]):+.2f}</td>'
            f'<td class="left">{_esc(d["blocked_by"] or "—")[:46]}</td>'
            f'<td class="left">{_esc(d["reason"][:120])}</td>'
            "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr>'
        '<th class="left">时间</th><th class="left">动作</th><th class="left">代码</th><th>score</th>'
        '<th class="left">拦截</th><th class="left">理由</th>'
        '</tr></thead><tbody>' + "".join(body) + "</tbody></table></div>"
    )


def _plan_panel(plan: dict | None) -> str:
    if not plan:
        return '<div class="empty">未加载入场计划</div>'
    p = plan["payload"]
    macro = p["macro"]
    risk = p["risk"]
    entry = p["entry"]
    rows = []
    for c in p["universe"]:
        rows.append(
            "<tr>"
            f'<td class="left">{_esc(c["code"])}</td>'
            f'<td class="left">{_esc(c["name"])}</td>'
            f'<td class="left">{_esc(c.get("sector", ""))}</td>'
            f'<td>{float(c.get("weight", 1.0)):.2f}</td>'
            f'<td>{float(c.get("stop_pct", entry["stop_pct"])):.1%}</td>'
            f'<td>{float(c.get("take_pct", entry["take_pct"])):.1%}</td>'
            f'<td class="left">{_esc(c.get("thesis", ""))}</td>'
            "</tr>"
        )
    return f"""
    <div class="plan-line">
      <div><b>{_esc(p["name"])}</b>（{_esc(p["plan_id"])}）· 状态 {_esc(plan["status"])}</div>
      <div>宏观档位：<b>{_esc(macro["regime"])}</b> — {_esc(macro["note"])}</div>
      <div>风险预算：总仓 ≤ {float(risk["max_total_exposure_pct"]):.0%}，单票 ≤ {float(risk["max_single_pct"]):.0%}，
      最多 {int(risk["max_positions"])} 只，最低现金 {float(risk["min_cash_pct"]):.0%}</div>
      <div>入场阈值：score ≥ {float(entry["min_score"]):.2f} · 站上 MA20 · RSI6 {float(entry["min_rsi6"]):.0f}~{float(entry["max_rsi6"]):.0f}
      · 单轮最多 {int(entry.get("max_candidates_per_round", 3))} 只 · 同行业最多 {int(entry.get("max_per_sector", 1))} 只</div>
    </div>
    <details>
      <summary>展开候选池（{len(p["universe"])} 只）</summary>
      <div class="table-wrap"><table><thead><tr>
      <th class="left">代码</th><th class="left">名称</th><th class="left">行业</th><th>权重</th><th>止损</th><th>止盈</th><th class="left">入场逻辑</th>
      </tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    </details>
    """


def build(account_name: str, market=None) -> Path:
    data = report.collect(account_name, market)
    acc = data["account"]
    view = data["view"]
    snapshots = data["snapshots"]
    latest = snapshots[0] if snapshots else None
    state = session_state()
    generated = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    plan = data.get("plan") or {}
    plan_payload = plan.get("payload") or {}
    plan_risk = plan_payload.get("risk") or {}
    min_cash_pct = float(plan_risk.get("min_cash_pct", 0.0))
    max_positions = int(plan_risk.get("max_positions", 0))

    from . import switches

    agent_on = switches.agent_trading_enabled()
    agent_badge_class = "on" if agent_on else "off"
    agent_badge_text = "Agent 交易 开启" if agent_on else "Agent 交易 关闭"
    agent_switch_hint = (
        "agent switch --on 已开启：定时 tick 会自动移动止损，Agent 可受控下单"
        if agent_on
        else "agent switch --on 可开启；当前只做规则交易"
    )

    today = datetime.now(TZ).date().isoformat()
    all_decisions = ledger.recent_decisions(int(acc["id"]), limit=200)
    today_decisions = [d for d in all_decisions if d["created_at"][:10] == today]

    day_pnl = float(latest["day_pnl"]) if latest else 0.0
    day_pnl_pct = (day_pnl / float(acc["initial_cash"]) * 100) if acc["initial_cash"] else 0.0
    total_pnl = float(view["total_equity"]) - float(acc["initial_cash"])
    total_pnl_pct = (total_pnl / float(acc["initial_cash"]) * 100) if acc["initial_cash"] else 0.0
    session_class = "on" if state.is_open else "off"
    session_text = state.label

    cards = f"""
    <div class="cards">
      <div class="card"><div class="label">总资产（实时估值）</div>
        <div class="value">{_money(float(view["total_equity"]))}</div>
        <div class="sub {_cls(total_pnl)}">{total_pnl:+,.2f}（{_pct(total_pnl_pct)}）</div></div>
      <div class="card"><div class="label">最新快照盈亏</div>
        <div class="value {_cls(day_pnl)}">{day_pnl:+,.2f}</div>
        <div class="sub">{_esc(latest["trade_date"] + " · " + latest["created_at"][11:16]) if latest else "暂无快照"}</div></div>
      <div class="card"><div class="label">现金</div>
        <div class="value">{_money(float(view["cash"]))}</div>
        <div class="sub">最低现金 {min_cash_pct:.0%}</div></div>
      <div class="card"><div class="label">持仓市值</div>
        <div class="value">{_money(float(view["position_value"]))}</div>
        <div class="sub">{int(view["position_count"])} 只持仓</div></div>
      <div class="card"><div class="label">持仓浮盈</div>
        <div class="value {_cls(float(view["unrealized_pnl"]))}">{float(view["unrealized_pnl"]):+,.2f}</div>
        <div class="sub">相对成本（含买入费用）</div></div>
      <div class="card"><div class="label">已实现盈亏</div>
        <div class="value {_cls(float(view["realized_pnl"]))}">{float(view["realized_pnl"]):+,.2f}</div>
        <div class="sub">principal − initial_cash</div></div>
    </div>
    """

    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mokou 模拟盘监控 · {_esc(acc["name"])}</title>
<style>{CSS}</style>
</head>
<body>
  <div class="header">
    <div>
      <div class="title">Mokou 模拟盘监控</div>
      <div class="subtitle">
        {_esc(acc["name"])} · id={int(acc["id"])} · 策略 {_esc(acc["strategy"])}
        <span class="badge {session_class}">{_esc(session_text)}</span>
        <span class="badge">{_esc(acc["mode"])}</span>
        <span class="badge {agent_badge_class}" title="agent switch：{_esc(agent_switch_hint)}">{_esc(agent_badge_text)}</span>
      </div>
    </div>
    <div style="text-align:right">
      <div class="subtitle">数据时点 {_esc(data["as_of"])}</div>
      <div class="subtitle">页面生成 {_esc(generated)} · 每 30 秒自动刷新</div>
      <a class="btn" href="" title="只重新加载当前 HTML；行情与账本由 papertrader tick 每 30 分钟重写">重新加载</a>
    </div>
  </div>

  {cards}

  <div class="grid">
    <div class="panel">
      <h2>净值曲线 <span class="hint">来自每日快照</span></h2>
      {_equity_svg(snapshots)}
    </div>
    <div class="panel">
      <h2>今日概览</h2>
      <div class="plan-line">
        <div>今日成交：<b>{sum(1 for t in data["trades"] if t["executed_at"][:10] == datetime.now(TZ).date().isoformat())}</b> 笔</div>
        <div>今日决策：<b>{len(today_decisions)}</b> 条（含 hold）</div>
        <div>持仓数：<b>{int(view["position_count"])}</b> / {max_positions}</div>
        <div>当前仓位：<b>{(float(view["position_value"]) / float(view["total_equity"]) * 100) if view["total_equity"] else 0:.1f}%</b></div>
        <div>最近成交：<b>{_esc(data["trades"][0]["executed_at"][5:19]) if data["trades"] else "—"}</b></div>
        <div>最近决策：<b>{_esc(data["decisions"][0]["created_at"][5:19]) if data["decisions"] else "—"}</b></div>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>持仓 <span class="hint">现价自动取东方财富；计划止损/止盈来自买入快照</span></h2>
    {_positions_table(view["positions"])}
  </div>

  <div class="panel">
    <h2>最近成交</h2>
    {_trades_table(data["trades"][:12])}
  </div>

  <div class="panel">
    <h2>最近决策 <span class="hint">含 hold 与硬闸拦截原因</span></h2>
    {_decisions_table(all_decisions)}
  </div>

  <div class="panel">
    <h2>入场计划</h2>
    {_plan_panel(data["plan"])}
  </div>

  <div class="footer">
    数据源：本地 SQLite（{_esc(acc["name"])}）+ 东方财富公开行情（失败时腾讯快照/日 K 回退）。仅为模拟盘，不构成投资建议。<br>
    刷新逻辑：papertrader tick 每 30 分钟写库并重写本页；浏览器每 30 秒重新加载。若数据时点未变，说明最近一次 tick 还没跑或非交易时段。
  </div>
</body>
</html>
"""
    path = REPORT_DIR / "dashboard.html"
    tmp = path.with_suffix(".html.tmp")
    tmp.write_text(html_doc, encoding="utf-8")
    tmp.replace(path)  # 原子替换，避免浏览器刷新时读到写了一半的页面
    return path
