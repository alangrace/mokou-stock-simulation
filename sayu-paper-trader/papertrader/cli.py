"""命令行入口。

用法：
  python -m papertrader.cli init --account 默认模拟盘 --cash 1000000
  python -m papertrader.cli plan-load --file plans/entry_20261118.json
  python -m papertrader.cli preview --account 默认模拟盘
  python -m papertrader.cli run --account 默认模拟盘 --replay --as-of "2026-11-18 10:30"
  python -m papertrader.cli daemon --account 默认模拟盘 --until 13:10
  python -m papertrader.cli status --account 默认模拟盘
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from . import agent as agent_mod
from . import dashboard, ledger, macro, plan_revision, proposal, report
from .calendar import ensure_tz, session_state
from .config import LOG_DIR, REPORT_DIR, TZ
from .engine import run_once, write_daily_snapshot
from .market import MarketClient
from .plan import load_plan


def _parse_when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return ensure_tz(datetime.strptime(raw, fmt))
        except ValueError:
            continue
    raise SystemExit(f"无法解析时间：{raw!r}（用 'YYYY-MM-DD HH:MM'）")


def _print_run(result) -> None:
    print("")
    print(f"=== 决策轮次 {result.as_of} / {result.data_source} / {result.account_name} ===")
    print(f"交易时段：{result.session['label']}（open={result.session['is_open']}，"
          f"next_open={result.session['next_open']}）")
    idx = result.index
    print(f"上证指数：{idx.get('price', '—')}（{idx.get('change_pct', 0):+.2f}%）"
          f" 环境档位：{idx.get('regime', '—')}")
    print(f"本轮决策 {len(result.decisions)} 条，成交 {len(result.trades)} 笔")
    for d in result.decisions:
        print(f"  · {d['action']:<4} {d['code'] or '—':<6} score={d['score']:+.2f} "
              f"[{d['blocked_by'] or '—'}] {d['reason']}")
    if result.trades:
        print("成交：")
        for t in result.trades:
            print(f"  · {t['side']} {t['code']} {t['qty']}股 @{t['price']:.3f} "
                  f"fee={t['fee']:.2f} pnl={t['realized_pnl']:+.2f}")
    acc = result.account
    print(f"账户：现金 ¥{acc.get('cash', 0):,.2f} ｜ 持仓 ¥{acc.get('position_value', 0):,.2f} "
          f"｜ 总资产 ¥{acc.get('total_equity', 0):,.2f} ｜ 浮盈 ¥{acc.get('unrealized_pnl', 0):+.2f}")
    if result.errors:
        print("错误：")
        for e in result.errors:
            print("  !", e)


def cmd_init(args) -> int:
    acc = ledger.create_account(args.account, cash=args.cash, strategy=args.strategy)
    print(f"✅ 账户已就绪：{acc['name']} id={acc['id']} 现金 ¥{acc['cash']:,.2f}")
    return 0


def cmd_plan_load(args) -> int:
    plan = load_plan(args.file)
    if args.account:
        plan["account"] = args.account
    acc = ledger.get_account(plan["account"])
    if acc is None:
        print(f"⚠️ 账户不存在：{plan['account']}；先执行 init", file=sys.stderr)
        return 2
    ledger.save_plan(int(acc["id"]), plan)
    print(f"✅ 入场计划已加载：{plan['name']}（{plan['plan_id']}）-> {acc['name']}")
    print(f"   宏观 {plan['macro']['regime']}｜候选 {len(plan['universe'])} 只｜"
          f"阈值 score≥{plan['entry']['min_score']}｜止损 {plan['entry']['stop_pct']:.1%}")
    return 0


def cmd_preview(args) -> int:
    result = run_once(
        account_name=args.account,
        plan_id=args.plan,
        as_of=_parse_when(args.as_of),
        data_source="replay" if args.as_of else "live",
        execute=False,
    )
    _print_run(result)
    return 0


def cmd_run(args) -> int:
    when = _parse_when(args.as_of)
    if args.replay and when is None:
        print("⚠️ --replay 必须配合 --as-of", file=sys.stderr)
        return 2
    result = run_once(
        account_name=args.account,
        plan_id=args.plan,
        as_of=when,
        data_source="replay" if args.replay else "live",
        execute=True,
    )
    _print_run(result)
    if args.snapshot:
        snap = write_daily_snapshot(args.account)
        print(f"净值快照：{snap['trade_date']} total={snap['total_equity']:,.2f} "
              f"day={snap['day_pnl']:+.2f} total_pnl={snap['total_pnl']:+.2f}")
    return 0


def _log(path: Path, text: str) -> None:
    line = f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}] {text}"
    print(line, flush=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def cmd_daemon(args) -> int:
    """等到下一个交易时段，按间隔跑心跳，直到 --until；收盘后写快照。"""
    until_t = dtime.fromisoformat(args.until)
    now = datetime.now(TZ)
    until = datetime.combine(now.date(), until_t, tzinfo=TZ)
    if until <= now:
        until += timedelta(days=1)
    log_path = LOG_DIR / f"daemon_{now.strftime('%Y%m%d')}.log"
    _log(log_path, f"daemon start account={args.account} interval={args.interval}s until={until.isoformat()}")
    ran = 0
    while True:
        now = datetime.now(TZ)
        if now >= until:
            break
        state = session_state(now)
        if state.is_open:
            try:
                result = run_once(
                    account_name=args.account,
                    plan_id=args.plan,
                    data_source="live",
                    execute=True,
                    verbose=True,
                )
                ran += 1
                _log(log_path, f"heartbeat #{ran} decisions={len(result.decisions)} trades={len(result.trades)}")
                if result.trades:
                    _log(log_path, "trades=" + json.dumps(result.trades, ensure_ascii=False))
                if ran == 1:
                    try:
                        snap = write_daily_snapshot(args.account)
                        _log(log_path, f"snapshot total={snap['total_equity']:.2f}")
                    except Exception as exc:  # noqa: BLE001
                        _log(log_path, f"snapshot failed: {type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001
                _log(log_path, f"heartbeat failed: {type(exc).__name__}: {exc}")
                _log(log_path, traceback.format_exc(limit=3))
            sleep_for = min(args.interval, max(1, int((until - datetime.now(TZ)).total_seconds())))
        else:
            nxt = state.next_open
            wait = 60
            if nxt:
                delta = max(1, int((nxt - now).total_seconds()) + 5)
                wait = 5 if delta <= 20 else min(120, max(10, delta))
            _log(log_path, f"waiting: {state.label} next_open={nxt.isoformat() if nxt else '—'} sleep={wait}s")
            sleep_for = min(wait, max(1, int((until - now).total_seconds())))
        time.sleep(sleep_for)

    try:
        snap = write_daily_snapshot(args.account)
        _log(log_path, f"final snapshot total={snap['total_equity']:.2f} day={snap['day_pnl']:+.2f}")
    except Exception as exc:  # noqa: BLE001
        _log(log_path, f"final snapshot failed: {type(exc).__name__}: {exc}")
    _log(log_path, f"daemon exit heartbeats={ran}")
    return 0


def cmd_status(args) -> int:
    data = report.collect(args.account)
    text = report.render_text(data)
    print(text)
    if args.md:
        path = Path(args.md)
        if not path.is_absolute():
            path = REPORT_DIR / path
        path.write_text(report.render_markdown(data), encoding="utf-8")
        print(f"\n📄 Markdown 已写入：{path}")
    return 0


def cmd_snapshot(args) -> int:
    snap = write_daily_snapshot(args.account)
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


def cmd_dashboard(args) -> int:
    path = dashboard.build(args.account)
    print(f"dashboard: {path}")
    return 0


def _json_payload(args) -> dict:
    raw = ""
    if getattr(args, "file", ""):
        raw = Path(args.file).read_text(encoding="utf-8")
    elif getattr(args, "json", ""):
        raw = args.json
    if not raw:
        raise SystemExit("需要 --json '<JSON>' 或 --file <path>")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("JSON 必须是对象")
    return payload


def cmd_agent_context(args) -> int:
    context = agent_mod.build_context(args.account)
    print(json.dumps(context, ensure_ascii=False, indent=2))
    return 0


def cmd_agent_macro_list(args) -> int:
    print(json.dumps(macro.list_events(status=args.status or None), ensure_ascii=False, indent=2))
    return 0


def cmd_agent_macro_upsert(args) -> int:
    print(json.dumps(macro.upsert_event(_json_payload(args)), ensure_ascii=False, indent=2))
    return 0


def cmd_agent_macro_snapshot(args) -> int:
    print(json.dumps(macro.save_snapshot(_json_payload(args)), ensure_ascii=False, indent=2))
    return 0


def cmd_agent_quote(args) -> int:
    codes = [c.strip() for c in str(args.codes).split(",") if c.strip()]
    from .engine import _quote_dict
    from .market import MarketClient

    quotes = MarketClient().batch_quotes(codes)
    print(json.dumps({code: _quote_dict(q) for code, q in quotes.items()}, ensure_ascii=False, indent=2))
    return 0


def cmd_agent_indicators(args) -> int:
    from .indicators import compute
    from .market import MarketClient

    ind = compute(MarketClient().kline(args.code, 101, args.limit))
    print(json.dumps(ind, ensure_ascii=False, indent=2))
    return 0


def cmd_agent_trade(args) -> int:
    if not args.dry_run:
        raise SystemExit("P2 只允许 dry-run；实盘写入在 P3 开启，请加 --dry-run")
    result = proposal.validate(args.account, _json_payload(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_agent_decision(args) -> int:
    result = proposal.log_decision(args.account, _json_payload(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_agent_runs(args) -> int:
    print(json.dumps(macro.list_agent_runs(limit=args.limit), ensure_ascii=False, indent=2))
    return 0


def cmd_agent_plan_update(args) -> int:
    execute = bool(getattr(args, "execute", False)) and not bool(args.dry_run)
    if execute and not plan_revision.agent_trading_enabled():
        raise SystemExit(
            "写计划需要显式开启：export SAYU_AGENT_TRADING=1；"
            "不加 --execute 时默认只做 dry-run"
        )
    result = plan_revision.validate(args.account, _json_payload(args), dry_run=not execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_tick(args) -> int:
    """定时任务专用：交易时段跑一轮；收盘后写快照；其余时段直接跳过。"""
    state = session_state()
    if state.is_open:
        result = run_once(
            account_name=args.account,
            plan_id=args.plan,
            data_source="live",
            execute=True,
            verbose=False,
        )
        print(f"tick: {state.label} 决策 {len(result.decisions)} 条，成交 {len(result.trades)} 笔")
        for t in result.trades:
            side = "买入" if t["side"] == "buy" else "卖出"
            print(
                f"成交 {side} {t['code']} {t['qty']} 股 @{t['price']:.3f} "
                f"费用 {t['fee']:.2f} 已实现 {t['realized_pnl']:+.2f}"
            )
        acc = result.account
        print(
            f"账户: 现金 {acc.get('cash', 0):,.2f} 持仓市值 {acc.get('position_value', 0):,.2f} "
            f"总资产 {acc.get('total_equity', 0):,.2f} 浮盈 {acc.get('unrealized_pnl', 0):+,.2f}"
        )
    elif state.phase == "post_close":
        snap = write_daily_snapshot(args.account)
        print(
            f"tick: 收盘快照 {snap['trade_date']} 总资产 {snap['total_equity']:,.2f} "
            f"当日 {snap['day_pnl']:+,.2f}"
        )
    else:
        print(f"tick: {state.label}，跳过（next_open={state.next_open.isoformat() if state.next_open else '—'}）")
    path = dashboard.build(args.account)
    print(f"dashboard: {path}")
    return 0


def cmd_screen(args) -> int:
    """按计划阈值对候选池做一次离线打分（不下单、不写决策）。"""
    from .indicators import compute
    from .planner import score_candidate

    plan = load_plan(args.file)
    market = MarketClient()
    rows = []
    for candidate in plan["universe"]:
        code = candidate["code"]
        try:
            bars = market.kline(code, 101, 180)
            ind = compute(bars)
            q = market.quote(code)
            score, reasons = score_candidate(ind, {"price": q.price, "change_pct": q.change_pct}, candidate)
            rows.append((score, code, candidate["name"], q.price, q.change_pct, ind, reasons))
        except Exception as exc:  # noqa: BLE001
            rows.append((-9, code, candidate.get("name", ""), 0, 0, {}, [f"失败: {exc}"]))
    rows.sort(key=lambda r: r[0], reverse=True)
    print(f"计划 {plan['plan_id']} 候选打分（阈值 {plan['entry']['min_score']}）")
    for score, code, name, price, chg, ind, reasons in rows:
        print(f"  {score:+.3f} {code} {name:<8} 现价 {price:>8.2f} ({chg:+.2f}%) "
              f"MA20={ind.get('ma20')} RSI6={ind.get('rsi6')} "
              f"量比={ind.get('vol_ratio')}｜" + "、".join(reasons[:5]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SayuStock 模拟盘功能独立复刻")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="创建模拟盘账户")
    init.add_argument("--account", default="默认模拟盘")
    init.add_argument("--cash", type=float, default=1_000_000.0)
    init.add_argument("--strategy", default="multi_factor")
    init.set_defaults(func=cmd_init)

    load = sub.add_parser("plan-load", help="加载入场计划 JSON")
    load.add_argument("--file", required=True)
    load.add_argument("--account", default="")
    load.set_defaults(func=cmd_plan_load)

    prev = sub.add_parser("preview", help="只评估并写挂起决策，不成交")
    prev.add_argument("--account", default="默认模拟盘")
    prev.add_argument("--plan", default=None)
    prev.add_argument("--as-of", default=None)
    prev.set_defaults(func=cmd_preview)

    run = sub.add_parser("run", help="执行一轮（可按历史时点回放）")
    run.add_argument("--account", default="默认模拟盘")
    run.add_argument("--plan", default=None)
    run.add_argument("--as-of", default=None)
    run.add_argument("--replay", action="store_true")
    run.add_argument("--snapshot", action="store_true")
    run.set_defaults(func=cmd_run)

    daemon = sub.add_parser("daemon", help="等到开盘并按间隔跑心跳")
    daemon.add_argument("--account", default="默认模拟盘")
    daemon.add_argument("--plan", default=None)
    daemon.add_argument("--interval", type=int, default=300)
    daemon.add_argument("--until", default="15:05")
    daemon.set_defaults(func=cmd_daemon)

    status = sub.add_parser("status", help="查看账户/持仓/流水/决策")
    status.add_argument("--account", default="默认模拟盘")
    status.add_argument("--md", default="")
    status.set_defaults(func=cmd_status)

    snap = sub.add_parser("snapshot", help="写当日净值快照")
    snap.add_argument("--account", default="默认模拟盘")
    snap.set_defaults(func=cmd_snapshot)

    dash = sub.add_parser("dashboard", help="生成可视化 HTML 仪表盘")
    dash.add_argument("--account", default="默认模拟盘")
    dash.set_defaults(func=cmd_dashboard)

    agent_p = sub.add_parser("agent", help="Agent 模式：只读上下文 / 宏观事件")
    agent_sub = agent_p.add_subparsers(dest="agent_cmd", required=True)

    actx = agent_sub.add_parser("context", help="输出一轮决策所需的 JSON 上下文")
    actx.add_argument("--account", default="默认模拟盘")
    actx.add_argument("--json", action="store_true", help="兼容 Agent 调用的显式 JSON 标志")
    actx.set_defaults(func=cmd_agent_context)

    aquote = agent_sub.add_parser("quote", help="批量查询行情")
    aquote.add_argument("--codes", required=True)
    aquote.add_argument("--json", action="store_true")
    aquote.set_defaults(func=cmd_agent_quote)

    aind = agent_sub.add_parser("indicators", help="查询单只股票技术指标")
    aind.add_argument("--code", required=True)
    aind.add_argument("--limit", type=int, default=180)
    aind.add_argument("--json", action="store_true")
    aind.set_defaults(func=cmd_agent_indicators)

    atrade = agent_sub.add_parser("trade", help="dry-run 校验一笔买/卖提议（不成交）")
    atrade.add_argument("--account", default="默认模拟盘")
    atrade.add_argument("--json", default="")
    atrade.add_argument("--file", default="")
    atrade.add_argument("--dry-run", action="store_true")
    atrade.set_defaults(func=cmd_agent_trade)

    adec = agent_sub.add_parser("decision", help="记录一条 Agent 决策（hold/信号，不成交）")
    adec.add_argument("--account", default="默认模拟盘")
    adec.add_argument("--json", default="")
    adec.add_argument("--file", default="")
    adec.set_defaults(func=cmd_agent_decision)

    aruns = agent_sub.add_parser("runs", help="查看最近 Agent 提议/决策记录")
    aruns.add_argument("--limit", type=int, default=20)
    aruns.add_argument("--json", action="store_true")
    aruns.set_defaults(func=cmd_agent_runs)

    aplan = agent_sub.add_parser("plan-update", help="dry-run 校验持仓止损/止盈修订（P1a 不写库）")
    aplan.add_argument("--account", default="默认模拟盘")
    aplan.add_argument("--json", default="")
    aplan.add_argument("--file", default="")
    aplan.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为）")
    aplan.add_argument("--execute", action="store_true", help="真正写入 position，需 SAYU_AGENT_TRADING=1")
    aplan.set_defaults(func=cmd_agent_plan_update)

    amacro = agent_sub.add_parser("macro", help="宏观事件表 / 快照")
    amacro_sub = amacro.add_subparsers(dest="macro_cmd", required=True)

    amlist = amacro_sub.add_parser("list", help="列出宏观事件")
    amlist.add_argument("--status", default="")
    amlist.add_argument("--json", action="store_true")
    amlist.set_defaults(func=cmd_agent_macro_list)

    amup = amacro_sub.add_parser("upsert", help="新增/更新一条宏观事件")
    amup.add_argument("--json", default="")
    amup.add_argument("--file", default="")
    amup.set_defaults(func=cmd_agent_macro_upsert)

    amsnap = amacro_sub.add_parser("snapshot", help="写一条宏观快照")
    amsnap.add_argument("--json", default="")
    amsnap.add_argument("--file", default="")
    amsnap.set_defaults(func=cmd_agent_macro_snapshot)

    tick = sub.add_parser("tick", help="定时任务：交易时段跑一轮，收盘后写快照")
    tick.add_argument("--account", default="默认模拟盘")
    tick.add_argument("--plan", default=None)
    tick.set_defaults(func=cmd_tick)

    screen = sub.add_parser("screen", help="按计划给候选池打分（不下单）")
    screen.add_argument("--file", required=True)
    screen.set_defaults(func=cmd_screen)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
