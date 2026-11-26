"""核心规则/账本/指标的最小回归测试（unittest，无第三方依赖）。

运行：
  cd sayu-paper-trader
  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="sayu-paper-test-")
os.environ["SAYU_PAPER_DB"] = os.path.join(_TMP, "test.db")

from papertrader import ledger, macro, plan_revision, proposal, rules  # noqa: E402
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

    def _frozen_session(self, when: datetime):
        from unittest import mock

        fake = SimpleNamespace(
            now=when,
            trading_day=True,
            is_open=True,
            phase="morning",
            label="上午盘",
            next_open=None,
        )
        return mock.patch.object(proposal, "session_state", return_value=fake)

    def _stub_market(self, ind: dict, price: float = 20.0):
        q = SimpleNamespace(
            code="600036",
            name="招商银行",
            price=price,
            prev_close=price,
            open=price,
            high=price,
            low=price,
            change_pct=0.0,
            volume=0.0,
            amount=0.0,
            turnover=None,
            ts="2026-11-23 10:00:00",
        )

        class _M:
            def quote(self, code: str):
                return q

            def batch_quotes(self, codes):
                return {c: q for c in codes}

            def kline(self, code: str, klt: int = 101, limit: int = 180):
                return []

        return _M(), ind

    def test_buy_rejects_rsi_over_limit(self) -> None:
        plan = {
            "plan_id": "p-entry",
            "name": "测试计划",
            "account": self.account["name"],
            "macro": {"regime": "中性", "note": "测试用宏观依据八字以上"},
            "risk": {"max_total_exposure_pct": 0.6, "max_single_pct": 0.15,
                     "max_positions": 8, "min_cash_pct": 0.1},
            "entry": {"stop_pct": -0.06, "take_pct": 0.12, "require_above_ma20": True,
                      "max_rsi6": 75, "min_rsi6": 30, "max_change_pct": 7, "min_score": 0.2},
            "universe": [{"code": "600036", "name": "招商银行", "thesis": "测试"},
                         {"code": "601166", "name": "兴业银行", "thesis": "测试"}],
        }
        from papertrader.plan import validate_plan

        ledger.save_plan(self.account["id"], validate_plan(plan))
        ind = {"above_ma20": True, "rsi6": 90.0, "ma5": 21, "ma10": 20, "ma20": 19,
               "ma60": 18, "close": 20.0, "dif": 0.1, "dea": 0.05, "macd_bar": 0.05,
               "macd_golden_cross_3d": False, "macd_death_cross_3d": False,
               "pct_20d": 5.0, "pos_20d": 0.5}
        market, _ = self._stub_market(ind)
        from unittest import mock

        import papertrader.indicators as ind_mod

        with self._frozen_session(datetime(2026, 11, 23, 10, 0, tzinfo=TZ)), \
             mock.patch.object(ind_mod, "compute", return_value=ind):
            result = proposal.validate(
                self.account["name"],
                {"action": "buy", "code": "600036", "qty": 1000,
                 "plan_stop_pct": -0.06, "plan_take_pct": 0.12,
                 "macro_regime": "中性", "macro_note": "测试用宏观依据八字以上"},
                market=market,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked_by"], "entry_blockers")
        self.assertIn("RSI6", result["reason"])

    def _agent_env(self, when: datetime):
        fake = SimpleNamespace(
            now=when, trading_day=True, is_open=True, phase="morning",
            label="上午盘", next_open=None,
        )
        return mock.patch.object(proposal, "session_state", return_value=fake)

    def _agent_plan(self, name: str, codes: list[tuple[str, str]], max_daily: int = 12) -> None:
        from papertrader.plan import validate_plan

        raw = {
            "plan_id": f"p3-{self._testMethodName}",
            "name": "P3测试计划",
            "account": name,
            "macro": {"regime": "中性", "note": "测试用宏观依据八字以上"},
            "risk": {"max_total_exposure_pct": 0.6, "max_single_pct": 0.15,
                     "max_positions": 8, "min_cash_pct": 0.1, "max_daily_trades": max_daily},
            "entry": {"stop_pct": -0.06, "take_pct": 0.12},
            "universe": [{"code": c, "name": n, "thesis": "测试"} for c, n in codes],
        }
        account = ledger.create_account(name, cash=1_000_000)
        ledger.save_plan(account["id"], validate_plan(raw))
        return account

    def _ind_ok(self) -> dict:
        return {"above_ma20": True, "rsi6": 50.0, "ma5": 44, "ma10": 43, "ma20": 42,
                "ma60": 40, "close": 43.0, "dif": 0.5, "dea": 0.4, "macd_bar": 0.1,
                "macd_golden_cross_3d": False, "macd_death_cross_3d": False,
                "pct_20d": 5.0, "pos_20d": 0.5}

    def test_execute_requires_switch(self) -> None:
        account = self._agent_plan(f"P3开关盘-{self._testMethodName}", [("600036", "招商银行")])
        payload = {"action": "buy", "code": "600036", "qty": 1000,
                   "plan_stop_pct": -0.06, "plan_take_pct": 0.12,
                   "macro_regime": "中性", "macro_note": "测试用宏观依据八字以上",
                   "reason": "开关测试", "score": 0.5}
        ind = self._ind_ok()
        market, _ = self._stub_market(ind)
        import papertrader.indicators as ind_mod

        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "0"}), \
             self._agent_env(datetime(2026, 11, 24, 10, 0, tzinfo=TZ)), \
             mock.patch.object(ind_mod, "compute", return_value=ind):
            result = proposal.validate(account["name"], payload, market=market, execute=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked_by"], "agent_trading_disabled")
        self.assertEqual(ledger.recent_trades(account["id"], 10), [])

    def test_execute_writes_agent_trade(self) -> None:
        account = self._agent_plan(f"P3成交盘-{self._testMethodName}", [("600036", "招商银行")])
        payload = {"action": "buy", "code": "600036", "qty": 1000,
                   "plan_stop_pct": -0.06, "plan_take_pct": 0.12,
                   "macro_regime": "中性", "macro_note": "测试用宏观依据八字以上",
                   "reason": "成交测试", "score": 0.5}
        ind = self._ind_ok()
        market, _ = self._stub_market(ind)
        import papertrader.indicators as ind_mod

        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "1"}), \
             self._agent_env(datetime(2026, 11, 24, 10, 0, tzinfo=TZ)), \
             mock.patch.object(ind_mod, "compute", return_value=ind):
            result = proposal.validate(account["name"], payload, market=market, execute=True)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["dry_run"])
        trades = ledger.recent_trades(account["id"], 10)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["source"], "agent")
        self.assertEqual(trades[0]["name"], "招商银行")
        position = ledger.get_position(account["id"], "600036")
        price = float(trades[0]["price"])
        self.assertAlmostEqual(position["plan_stop_price"], price * 0.94, places=2)
        self.assertAlmostEqual(position["plan_take_price"], price * 1.12, places=2)

    def test_agent_daily_quota(self) -> None:
        codes = [("600036", "招商银行"), ("601166", "兴业银行"),
                 ("600028", "中国石化"), ("600887", "伊利股份")]
        account = self._agent_plan(f"P3配额盘-{self._testMethodName}", codes)
        ind = self._ind_ok()
        market, _ = self._stub_market(ind)
        import papertrader.indicators as ind_mod

        results = []
        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "1"}), \
             self._agent_env(datetime(2026, 11, 24, 10, 0, tzinfo=TZ)), \
             mock.patch.object(ind_mod, "compute", return_value=ind):
            for code, _name in codes:
                payload = {"action": "buy", "code": code, "qty": 1000,
                           "plan_stop_pct": -0.06, "plan_take_pct": 0.12,
                           "macro_regime": "中性", "macro_note": "测试用宏观依据八字以上",
                           "reason": "配额测试", "score": 0.5}
                results.append(proposal.validate(account["name"], payload, market=market, execute=True))
        self.assertEqual([r["ok"] for r in results], [True, True, True, False])
        self.assertEqual(results[3]["blocked_by"], "agent_daily_quota")
        self.assertEqual(ledger.daily_agent_trade_count(account["id"], "2026-11-24"), 3)

    def test_shared_daily_cap(self) -> None:
        account = self._agent_plan(
            f"P3共享计数盘-{self._testMethodName}",
            [("600036", "招商银行"), ("601166", "兴业银行")],
            max_daily=2,
        )
        for code, name in [("600036", "招商银行"), ("601166", "兴业银行")]:
            ledger.record_fill(
                account_id=account["id"], code=code, name=name, side="buy",
                price=43.0, qty=1000, reason="规则成交",
                snapshot={"plan_stop_price": 40.42, "plan_stop_pct": -0.06,
                          "plan_take_price": 48.16, "plan_thesis": "测试"},
                source="rule", executed_at=datetime(2026, 11, 24, 9, 35, tzinfo=TZ),
                trade_date="2026-11-24",
            )
        ind = self._ind_ok()
        market, _ = self._stub_market(ind)
        import papertrader.indicators as ind_mod

        payload = {"action": "buy", "code": "600036", "qty": 1000,
                   "plan_stop_pct": -0.06, "plan_take_pct": 0.12,
                   "macro_regime": "中性", "macro_note": "测试用宏观依据八字以上",
                   "reason": "共享计数", "score": 0.5, "allow_add": True}
        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "1"}), \
             self._agent_env(datetime(2026, 11, 24, 10, 0, tzinfo=TZ)), \
             mock.patch.object(ind_mod, "compute", return_value=ind):
            result = proposal.validate(account["name"], payload, market=market, execute=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked_by"], "max_daily_trades")

    def test_buy_rejects_low_score(self) -> None:
        plan = {
            "plan_id": "p-score",
            "name": "测试计划",
            "account": self.account["name"],
            "macro": {"regime": "中性", "note": "测试用宏观依据八字以上"},
            "risk": {"max_total_exposure_pct": 0.6, "max_single_pct": 0.15,
                     "max_positions": 8, "min_cash_pct": 0.1},
            "entry": {"stop_pct": -0.06, "take_pct": 0.12, "require_above_ma20": True,
                      "max_rsi6": 75, "min_rsi6": 30, "max_change_pct": 7, "min_score": 0.2},
            "universe": [{"code": "600036", "name": "招商银行", "thesis": "测试"}],
        }
        from papertrader.plan import validate_plan

        ledger.save_plan(self.account["id"], validate_plan(plan))
        ind = {"above_ma20": True, "rsi6": 50.0, "ma5": 20, "ma10": 20, "ma20": 19,
               "ma60": 18, "close": 20.0, "dif": 0.1, "dea": 0.05, "macd_bar": 0.05,
               "macd_golden_cross_3d": False, "macd_death_cross_3d": False,
               "pct_20d": 5.0, "pos_20d": 0.5}
        market, _ = self._stub_market(ind)
        from unittest import mock
        import papertrader.indicators as ind_mod

        with self._frozen_session(datetime(2026, 11, 23, 10, 0, tzinfo=TZ)), \
             mock.patch.object(ind_mod, "compute", return_value=ind):
            result = proposal.validate(
                self.account["name"],
                {"action": "buy", "code": "600036", "qty": 1000,
                 "plan_stop_pct": -0.06, "plan_take_pct": 0.12, "score": -0.5,
                 "macro_regime": "中性", "macro_note": "测试用宏观依据八字以上"},
                market=market,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked_by"], "score_below_threshold")


class PlanRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 20, 15, 40, tzinfo=TZ)
        self.position = {
            "code": "600887",
            "name": "伊利股份",
            "avg_cost": 32.338,
            "plan_stop_price": 30.39,
            "plan_stop_pct": -0.06,
            "plan_take_price": 36.21,
            "plan_thesis": "测试",
            "plan_updated_at": "2026-11-18T13:14:59+08:00",
            "plan_rev_count": 0,
        }

    def test_trail_stop_only_up_and_step(self) -> None:
        ok = plan_revision.evaluate(
            self.position, {},
            {"revision_type": "trail_stop", "plan_stop_price": 31.5, "reason": "测试修订理由"},
            35.0, self.now,
        )
        self.assertTrue(ok["ok"], ok)
        widen = plan_revision.evaluate(
            self.position, {},
            {"revision_type": "trail_stop", "plan_stop_price": 30.0, "reason": "测试修订理由"},
            35.0, self.now,
        )
        self.assertEqual(widen["blocked_by"], "stop_widen")
        step = plan_revision.evaluate(
            self.position, {},
            {"revision_type": "trail_stop", "plan_stop_price": 30.5, "reason": "测试修订理由"},
            35.0, self.now,
        )
        self.assertEqual(step["blocked_by"], "step_too_small")

    def test_trail_stop_requires_profit(self) -> None:
        result = plan_revision.evaluate(
            self.position, {},
            {"revision_type": "trail_stop", "plan_stop_price": 31.5, "reason": "测试修订理由"},
            33.35, self.now,
        )
        self.assertEqual(result["blocked_by"], "not_enough_profit")

    def test_cooldown(self) -> None:
        position = dict(self.position, plan_updated_at=(self.now - timedelta(hours=1)).isoformat())
        result = plan_revision.evaluate(
            position, {},
            {"revision_type": "trail_stop", "plan_stop_price": 31.5, "reason": "测试修订理由"},
            35.0, self.now,
        )
        self.assertEqual(result["blocked_by"], "cooldown")

    def test_take_adjust_near_old_take(self) -> None:
        result = plan_revision.evaluate(
            self.position, {},
            {
                "revision_type": "take_adjust",
                "plan_take_price": 38.0,
                "reason": "测试修订理由",
                "evidence": "测试突破",
            },
            35.8, self.now,
        )
        self.assertEqual(result["blocked_by"], "take_near")

    def test_backfill(self) -> None:
        position = dict(self.position, plan_stop_price=None, plan_stop_pct=None, plan_take_price=None)
        result = plan_revision.evaluate(
            position, {},
            {
                "revision_type": "backfill",
                "plan_stop_price": 30.0,
                "plan_take_price": 36.0,
                "reason": "测试修订理由",
            },
            33.0, self.now,
        )
        self.assertTrue(result["ok"], result)

    def _write_account(self) -> tuple[dict, dict]:
        from papertrader.plan import validate_plan

        name = f"P1B盘-{self._testMethodName}"
        account = ledger.create_account(name, cash=100_000)
        raw = {
            "plan_id": f"p1b-{self._testMethodName}",
            "name": "P1B测试计划",
            "account": name,
            "macro": {"regime": "中性", "note": "测试用宏观依据八字以上"},
            "risk": {"max_total_exposure_pct": 0.6, "max_single_pct": 0.15,
                     "max_positions": 8, "min_cash_pct": 0.1, "max_daily_trades": 12},
            "entry": {"stop_pct": -0.06, "take_pct": 0.12},
            "universe": [{"code": "600887", "name": "伊利股份", "thesis": "测试"}],
        }
        ledger.save_plan(account["id"], validate_plan(raw))
        ledger.record_fill(
            account_id=account["id"], code="600887", name="伊利股份", side="buy",
            price=32.0, qty=1000, reason="测试",
            snapshot={"plan_stop_price": 30.08, "plan_stop_pct": -0.06,
                      "plan_take_price": 38.0, "plan_thesis": "测试"},
            trade_date="2026-11-19", executed_at=datetime(2026, 11, 19, 10, 0, tzinfo=TZ),
        )
        return account, ledger.get_position(account["id"], "600887")

    def test_apply_revision_writes_position_once(self) -> None:
        account, position = self._write_account()
        payload = {"code": "600887", "revision_type": "trail_stop",
                   "plan_stop_price": 32.2, "reason": "浮盈超8%后上移止损保本"}
        quote = SimpleNamespace(price=36.0)

        class _M:
            def quote(self, code: str):
                return quote

        cash_before = ledger.get_account(account["name"])["cash"]
        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "1"}):
            result = plan_revision.validate(
                account["name"], payload, market=_M(), dry_run=False,
                now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["applied"])
        updated = ledger.get_position(account["id"], "600887")
        self.assertAlmostEqual(updated["plan_stop_price"], 32.2, places=3)
        self.assertEqual(updated["plan_rev_count"], 1)
        self.assertEqual(updated["qty"], position["qty"])
        self.assertEqual(ledger.get_account(account["name"])["cash"], cash_before)

    def test_apply_revision_respects_switch(self) -> None:
        account, _ = self._write_account()
        payload = {"code": "600887", "revision_type": "trail_stop",
                   "plan_stop_price": 32.2, "reason": "浮盈超8%后上移止损保本"}
        quote = SimpleNamespace(price=36.0)

        class _M:
            def quote(self, code: str):
                return quote

        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "0"}):
            result = plan_revision.validate(
                account["name"], payload, market=_M(), dry_run=False,
                now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked_by"], "agent_trading_disabled")
        self.assertAlmostEqual(
            ledger.get_position(account["id"], "600887")["plan_stop_price"], 30.08, places=3
        )

    def test_macro_tighten(self) -> None:
        result = plan_revision.evaluate(
            self.position, {},
            {
                "revision_type": "macro_tighten",
                "plan_stop_price": 31.5,
                "macro_regime": "防御",
                "reason": "测试修订理由",
            },
            33.0, self.now,
        )
        self.assertTrue(result["ok"], result)


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        from papertrader import switches

        self.switches = switches
        switches.set_agent_trading(False)

    def tearDown(self) -> None:
        self.switches.set_agent_trading(False)

    def test_default_off(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAYU_AGENT_TRADING", None)
            self.assertFalse(self.switches.agent_trading_enabled())

    def test_flag_persists(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAYU_AGENT_TRADING", None)
            path = self.switches.set_agent_trading(True)
            self.assertTrue(path.exists())
            self.assertTrue(self.switches.agent_trading_enabled())
            self.assertTrue(self.switches.flag_path().parent.samefile(path.parent))

    def test_env_overrides_flag(self) -> None:
        self.switches.set_agent_trading(True)
        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "0"}):
            self.assertFalse(self.switches.agent_trading_enabled())
        with mock.patch.dict(os.environ, {"SAYU_AGENT_TRADING": "1"}):
            self.switches.set_agent_trading(False)
            self.assertTrue(self.switches.agent_trading_enabled())

    def test_switch_gates_write_path(self) -> None:
        account, _ = PlanRevisionTests()._write_account()
        payload = {"code": "600887", "revision_type": "trail_stop",
                   "plan_stop_price": 32.2, "reason": "浮盈超8%后上移止损保本"}
        quote = SimpleNamespace(price=36.0)

        class _M:
            def quote(self, code: str):
                return quote

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAYU_AGENT_TRADING", None)
            self.switches.set_agent_trading(False)
            blocked = plan_revision.validate(
                account["name"], payload, market=_M(), dry_run=False,
                now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
            )
            self.assertEqual(blocked["blocked_by"], "agent_trading_disabled")

            self.switches.set_agent_trading(True)
            applied = plan_revision.validate(
                account["name"], payload, market=_M(), dry_run=False,
                now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
            )
            self.assertTrue(applied["applied"], applied)


class AutoTrailTests(unittest.TestCase):
    def _account(self, name: str, *, cost: float, stop: float) -> tuple[dict, dict]:
        from papertrader.plan import validate_plan

        account = ledger.create_account(name, cash=100_000)
        raw = {
            "plan_id": f"auto-{name}",
            "name": "自动止损测试计划",
            "account": name,
            "macro": {"regime": "中性", "note": "测试用宏观依据八字以上"},
            "risk": {"max_total_exposure_pct": 0.6, "max_single_pct": 0.15,
                     "max_positions": 8, "min_cash_pct": 0.1, "max_daily_trades": 12},
            "entry": {"stop_pct": -0.06, "take_pct": 0.12},
            "universe": [{"code": "600887", "name": "伊利股份", "thesis": "测试"}],
        }
        ledger.save_plan(account["id"], validate_plan(raw))
        ledger.record_fill(
            account_id=account["id"], code="600887", name="伊利股份", side="buy",
            price=cost, qty=1000, reason="测试",
            snapshot={"plan_stop_price": stop, "plan_stop_pct": -0.06,
                      "plan_take_price": round(cost * 1.4, 3), "plan_thesis": "测试"},
            trade_date="2026-11-19", executed_at=datetime(2026, 11, 19, 10, 0, tzinfo=TZ),
        )
        return account, ledger.get_position(account["id"], "600887")

    def _market(self, price: float):
        quote = SimpleNamespace(price=price)

        class _M:
            def quote(self, code: str):
                return quote

        return _M()

    def test_skips_below_profit_threshold(self) -> None:
        account, _ = self._account(f"自动盘-low-{self._testMethodName}", cost=32.0, stop=30.08)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAYU_AGENT_TRADING", None)
            picks = plan_revision.auto_trail_candidates(
                account["name"], market=self._market(33.0),
                now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
            )
        self.assertEqual(picks, [])

    def test_picks_and_writes_above_threshold(self) -> None:
        account, _ = self._account(f"自动盘-high-{self._testMethodName}", cost=32.0, stop=30.08)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAYU_AGENT_TRADING", None)
            from papertrader import switches

            switches.set_agent_trading(True)
            try:
                results = plan_revision.auto_trail_stops(
                    account["name"], market=self._market(36.0), dry_run=False,
                    now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
                )
            finally:
                switches.set_agent_trading(False)
        self.assertEqual(len(results), 1, results)
        applied = results[0]["result"]
        self.assertTrue(applied["ok"], applied)
        self.assertTrue(applied["applied"])
        updated = ledger.get_position(account["id"], "600887")
        self.assertAlmostEqual(updated["plan_stop_price"], round(36.0 * 0.97, 3), places=3)
        self.assertAlmostEqual(
            updated["plan_stop_pct"], round(36.0 * 0.97 / updated["avg_cost"] - 1.0, 4), places=4
        )
        self.assertEqual(updated["plan_rev_count"], 1)

    def test_dry_run_does_not_write(self) -> None:
        account, position = self._account(f"自动盘-dry-{self._testMethodName}", cost=32.0, stop=30.08)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAYU_AGENT_TRADING", None)
            results = plan_revision.auto_trail_stops(
                account["name"], market=self._market(36.0),
                now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
            )
        self.assertEqual(len(results), 1, results)
        self.assertFalse(results[0]["result"].get("applied"))
        self.assertEqual(ledger.get_position(account["id"], "600887")["plan_stop_price"], 30.08)

    def test_cooldown_blocks_second_trail(self) -> None:
        account, _ = self._account(f"自动盘-cool-{self._testMethodName}", cost=32.0, stop=30.08)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAYU_AGENT_TRADING", None)
            from papertrader import switches

            switches.set_agent_trading(True)
            try:
                first = plan_revision.auto_trail_stops(
                    account["name"], market=self._market(36.0), dry_run=False,
                    now=datetime(2026, 11, 23, 10, 0, tzinfo=TZ),
                )
                second = plan_revision.auto_trail_stops(
                    account["name"], market=self._market(40.0), dry_run=False,
                    now=datetime(2026, 11, 23, 11, 0, tzinfo=TZ),
                )
            finally:
                switches.set_agent_trading(False)
        self.assertTrue(first[0]["result"]["applied"])
        self.assertEqual(second[0]["result"]["blocked_by"], "cooldown")


if __name__ == "__main__":
    unittest.main()

