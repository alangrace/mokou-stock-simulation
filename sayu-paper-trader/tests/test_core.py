"""核心规则/账本/指标的最小回归测试（unittest，无第三方依赖）。

运行：
  cd sayu-paper-trader
  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime

_TMP = tempfile.mkdtemp(prefix="sayu-paper-test-")
os.environ["SAYU_PAPER_DB"] = os.path.join(_TMP, "test.db")

from papertrader import ledger, macro, proposal, rules  # noqa: E402
from papertrader.calendar import session_state  # noqa: E402
from papertrader.config import TZ  # noqa: E402
from papertrader.indicators import compute  # noqa: E402
from papertrader.market import Bar  # noqa: E402
from papertrader.plan import PlanError, validate_buy, validate_plan  # noqa: E402


def make_bars(n: int = 80) -> list[Bar]:
    bars: list[Bar] = []
    price = 10.0
    for i in range(n):
        price *= 1.004
        bars.append(
            Bar(
                date=f"2026-01-{i + 1:02d}",
                open=price * 0.995,
                close=price,
                high=price * 1.01,
                low=price * 0.99,
                volume=1_000_000 + i * 1000,
                amount=price * 1_000_000,
                pct_chg=0.4,
                turnover=1.0,
            )
        )
    return bars


class RuleTests(unittest.TestCase):
    def test_fee_buy_and_sell(self) -> None:
        buy = rules.calc_fee("buy", 10_000)
        self.assertEqual(buy.commission, 5.0)
        self.assertEqual(buy.stamp_tax, 0.0)
        self.assertEqual(buy.total, 5.0)
        sell = rules.calc_fee("sell", 100_000)
        self.assertEqual(sell.commission, 25.0)
        self.assertEqual(sell.stamp_tax, 50.0)
        self.assertEqual(sell.total, 75.0)

    def test_round_lot(self) -> None:
        self.assertEqual(rules.round_lot(99), 0)
        self.assertEqual(rules.round_lot(100), 100)
        self.assertEqual(rules.round_lot(1234), 1200)

    def test_limit_threshold_by_board(self) -> None:
        self.assertEqual(rules.limit_threshold("600519"), 10.0)
        self.assertEqual(rules.limit_threshold("688981"), 20.0)
        self.assertEqual(rules.limit_threshold("300750"), 20.0)
        self.assertEqual(rules.limit_threshold("830799"), 30.0)
        self.assertEqual(rules.limit_threshold("600519", "ST 某某"), 5.0)

    def test_price_limit_block(self) -> None:
        self.assertTrue(rules.check_price_limit("buy", "600519", "贵州茅台", 109.5, 100.0))
        self.assertEqual(rules.check_price_limit("buy", "600519", "贵州茅台", 105.0, 100.0), "")
        self.assertTrue(rules.check_price_limit("sell", "600519", "贵州茅台", 90.5, 100.0))

    def test_apply_fill(self) -> None:
        qty, avg = rules.apply_fill(0, 0.0, "buy", 100, 10.0, 5.0)
        self.assertEqual(qty, 100)
        self.assertAlmostEqual(avg, 10.05)
        qty2, avg2 = rules.apply_fill(100, 10.05, "sell", 40, 11.0, 5.0)
        self.assertEqual(qty2, 60)
        self.assertAlmostEqual(avg2, 10.05)


class CalendarTests(unittest.TestCase):
    def test_open_lunch_holiday(self) -> None:
        self.assertTrue(session_state(datetime(2026, 11, 18, 10, 30, tzinfo=TZ)).is_open)
        self.assertFalse(session_state(datetime(2026, 11, 18, 12, 0, tzinfo=TZ)).is_open)
        self.assertFalse(session_state(datetime(2026, 10, 1, 10, 30, tzinfo=TZ)).trading_day)
        self.assertFalse(session_state(datetime(2026, 11, 21, 10, 30, tzinfo=TZ)).trading_day)


class IndicatorTests(unittest.TestCase):
    def test_compute_keys(self) -> None:
        ind = compute(make_bars())
        for key in ("ma5", "ma20", "ma60", "dif", "dea", "rsi6", "atr_pct", "vol_ratio", "pos_20d"):
            self.assertIn(key, ind)
        self.assertGreater(ind["close"], ind["ma20"])

    def test_insufficient_bars(self) -> None:
        with self.assertRaises(ValueError):
            compute(make_bars(10))


class PlanTests(unittest.TestCase):
    def _plan(self) -> dict:
        return {
            "plan_id": "t",
            "name": "t",
            "account": "acc",
            "macro": {"regime": "中性", "note": "测试用宏观依据八字以上"},
            "risk": {
                "max_total_exposure_pct": 0.6,
                "max_single_pct": 0.1,
                "max_positions": 3,
                "min_cash_pct": 0.1,
            },
            "entry": {"stop_pct": -0.06, "take_pct": 0.12, "min_score": 0.2},
            "universe": [{"code": "600519", "name": "贵州茅台", "thesis": "测试逻辑"}],
        }

    def test_validate_plan_ok(self) -> None:
        plan = validate_plan(self._plan())
        self.assertEqual(plan["universe"][0]["stop_pct"], -0.06)

    def test_validate_plan_rejects_missing_thesis(self) -> None:
        raw = self._plan()
        raw["universe"][0]["thesis"] = ""
        with self.assertRaises(PlanError):
            validate_plan(raw)

    def test_buy_gate(self) -> None:
        plan = validate_plan(self._plan())
        bad = {"macro_regime": "中性", "macro_note": "测试用宏观依据八字以上"}
        self.assertTrue(validate_buy(plan, bad))
        stop_only = dict(bad, plan_stop_pct=-0.06)
        self.assertIn("止盈", validate_buy(plan, stop_only))
        good = dict(bad, plan_stop_pct=-0.06, plan_take_pct=0.12)
        self.assertEqual(validate_buy(plan, good), "")

    def test_buy_gate_normalizes_percent(self) -> None:
        plan = validate_plan(self._plan())
        snap = {
            "macro_regime": "中性",
            "macro_note": "测试用宏观依据八字以上",
            "plan_stop_pct": -6,
            "plan_take_pct": 12,
        }
        self.assertEqual(validate_buy(plan, snap), "")
        self.assertTrue(validate_buy(plan, dict(snap, plan_stop_pct=-600)))
        self.assertTrue(validate_buy(plan, dict(snap, plan_take_pct=600)))

    def test_defense_macro_gate(self) -> None:
        raw = self._plan()
        raw["macro"] = {
            "regime": "进攻",
            "note": "测试用宏观依据八字以上",
            "risk_off_severe": True,
            "risk_off_titles": "测试事件",
        }
        plan = validate_plan(raw)
        snap = {
            "macro_regime": "进攻",
            "macro_note": "测试用宏观依据八字以上",
            "plan_stop_pct": -0.06,
            "plan_take_pct": 0.12,
        }
        self.assertIn("不得标 macro_regime=进攻", validate_buy(plan, snap))


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger.init_db()
        # 每个用例独立账户，避免前一个用例的持仓污染
        self.account = ledger.create_account(f"测试盘-{self._testMethodName}", cash=100_000)

    def test_buy_writes_position_and_cash(self) -> None:
        fill = ledger.record_fill(
            account_id=self.account["id"], code="600519", name="贵州茅台",
            side="buy", price=10.0, qty=100,
            reason="测试买入", snapshot={"plan_stop_pct": -0.06, "plan_stop_price": 9.4},
            trade_date="2026-11-18",
        )
        self.assertEqual(fill["position_qty"], 100)
        self.assertAlmostEqual(fill["cash_after"], 100_000 - 1000 - 5)
        pos = ledger.get_position(self.account["id"], "600519")
        assert pos is not None
        self.assertEqual(pos["qty"], 100)
        self.assertAlmostEqual(pos["plan_stop_pct"], -0.06)

    def test_t_plus_1_blocks_same_day_sell(self) -> None:
        ledger.record_fill(
            account_id=self.account["id"], code="600519", name="贵州茅台",
            side="buy", price=10.0, qty=100, reason="t", trade_date="2026-11-18",
        )
        with self.assertRaises(ValueError) as ctx:
            ledger.record_fill(
                account_id=self.account["id"], code="600519", name="贵州茅台",
                side="sell", price=11.0, qty=100, reason="t", trade_date="2026-11-18",
            )
        self.assertIn("T+1", str(ctx.exception))
        sell = ledger.record_fill(
            account_id=self.account["id"], code="600519", name="贵州茅台",
            side="sell", price=11.0, qty=100, reason="t", trade_date="2026-11-19",
        )
        self.assertEqual(sell["position_qty"], 0)
        self.assertIsNone(ledger.get_position(self.account["id"], "600519"))

    def test_sell_rejects_more_than_position(self) -> None:
        ledger.record_fill(
            account_id=self.account["id"], code="600519", name="贵州茅台",
            side="buy", price=10.0, qty=100, reason="t", trade_date="2026-11-18",
        )
        with self.assertRaises(ValueError):
            ledger.record_fill(
                account_id=self.account["id"], code="600519", name="贵州茅台",
                side="sell", price=11.0, qty=200, reason="t", trade_date="2026-11-19",
            )


class PlannerTests(unittest.TestCase):
    def _plan(self) -> dict:
        return {
            "macro": {"regime": "防御"},
            "risk": {"max_total_exposure_pct": 0.6, "max_single_pct": 0.15, "min_cash_pct": 0.1},
            "entry": {"stop_pct": -0.06, "take_pct": 0.12},
            "exit": {"score_exit": -0.45},
        }

    def test_stop_and_take(self) -> None:
        from papertrader.planner import exit_signal

        pos = {"code": "600519", "avg_cost": 10.0, "plan_stop_price": 9.4,
               "plan_stop_pct": -0.06, "plan_take_price": 11.2, "qty": 100}
        stop = exit_signal(position=pos, quote={"price": 9.3}, score=0.0,
                           today=date(2026, 11, 18), buys_today=[], plan=self._plan())
        self.assertEqual(stop["action"], "sell")
        self.assertEqual(stop["trigger"], "stop_price")
        take = exit_signal(position=pos, quote={"price": 11.3}, score=0.0,
                           today=date(2026, 11, 18), buys_today=[], plan=self._plan())
        self.assertEqual(take["action"], "sell")
        self.assertEqual(take["trigger"], "take_price")

    def test_t_plus_1_blocks_exit(self) -> None:
        from papertrader.planner import exit_signal

        pos = {"code": "600519", "avg_cost": 10.0, "plan_stop_price": 9.4,
               "plan_stop_pct": -0.06, "plan_take_price": 11.2, "qty": 100}
        sig = exit_signal(position=pos, quote={"price": 9.0}, score=-0.9,
                          today=date(2026, 11, 18), buys_today=[("600519", 100)],
                          plan=self._plan())
        self.assertEqual(sig["action"], "hold")
        self.assertEqual(sig["blocked_by"], "t_plus_1")

    def test_macro_caps_lower_single_position(self) -> None:
        from papertrader.planner import position_budget

        qty, budget, note = position_budget(
            self._plan(), {"weight": 1.0}, equity=1_000_000, price=20.0,
            cash=1_000_000, max_total_pct=0.30, max_single_pct=0.05,
        )
        self.assertEqual(qty, 2500)
        self.assertAlmostEqual(budget, 50_000.0)
        self.assertEqual(note, "")


class MacroTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger.init_db()

    def test_upsert_and_list_event(self) -> None:
        event = macro.upsert_event(
            {
                "slug": "fed_dec_2026",
                "title": "美联储12月加息预期",
                "severity": 4,
                "direction": "risk_off",
                "status": "open",
                "result": "CME定价25bp概率92%",
                "stance": "压制成长/贵金属",
            }
        )
        self.assertEqual(event["severity"], 4)
        rows = macro.list_events(status="open")
        self.assertTrue(any(r["slug"] == "fed_dec_2026" for r in rows))
        updated = macro.upsert_event({"slug": "fed_dec_2026", "title": "更新", "severity": 5})
        self.assertEqual(updated["title"], "更新")
        self.assertEqual(updated["severity"], 5)

    def test_invalid_severity(self) -> None:
        with self.assertRaises(ValueError):
            macro.upsert_event({"slug": "bad", "title": "bad", "severity": 9})

    def test_snapshot(self) -> None:
        snap = macro.save_snapshot(
            {
                "regime": "防御",
                "phase": "价值相",
                "fed_prob_25bp": 0.92,
                "breadth_down": "超5300只下跌",
                "gold_pct": -3.4,
                "sources": ["https://example.com"],
            }
        )
        self.assertEqual(snap["regime"], "防御")
        latest = macro.latest_snapshot()
        assert latest is not None
        self.assertEqual(latest["fed_prob_25bp"], 0.92)


class ProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        ledger.init_db()
        self.account = ledger.create_account(f"提案盘-{self._testMethodName}", cash=100_000)

    def test_log_hold_decision(self) -> None:
        result = proposal.log_decision(
            self.account["name"],
            {"action": "hold", "code": "600028", "score": 0.1, "reason": "测试持有"},
        )
        self.assertTrue(result["ok"])
        runs = macro.list_agent_runs(limit=5)
        self.assertTrue(any(r["status"] == "dry_run_hold" for r in runs))

    def test_log_rejects_bad_action(self) -> None:
        with self.assertRaises(ValueError):
            proposal.log_decision(self.account["name"], {"action": "short"})


if __name__ == "__main__":
    unittest.main()

