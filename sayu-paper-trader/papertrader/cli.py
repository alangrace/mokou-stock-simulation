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

from . import dashboard, ledger, report
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
