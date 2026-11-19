"""入场计划：结构、校验、落库前的策略硬闸。

对应 SayuStock 的两层约束：
1. 计划本身声明宏观档位、风险预算、候选池与每只票的 thesis；
2. buy 落库前硬闸（``strategy.gate_buy``）：必须有数值止损 + 宏观档位 + 宏观依据；
   表内存在高危 risk_off 事件时，不许把 ``进攻`` 写进快照。

这是"LLM 自主决策"的可审计替身：规则引擎只能在本计划允许的范围内行动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MACRO_REGIMES = ("进攻", "中性", "防御")
MACRO_NOTE_MIN_LEN = 8


class PlanError(ValueError):
    """计划不合法，拒绝加载 / 拒绝下单。"""


def load_plan(path: str | Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_plan(raw)


def validate_plan(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise PlanError("计划必须是 JSON 对象")
    errors: list[str] = []
    for key in ("plan_id", "name", "account"):
        if not str(raw.get(key, "")).strip():
            errors.append(f"缺少 {key}")
    macro = raw.get("macro") or {}
    regime = str(macro.get("regime", "")).strip()
    if regime not in MACRO_REGIMES:
        errors.append(f"macro.regime 必须是 {'/'.join(MACRO_REGIMES)}")
    note = str(macro.get("note", "")).strip()
    if len(note) < MACRO_NOTE_MIN_LEN:
        errors.append(f"macro.note 至少 {MACRO_NOTE_MIN_LEN} 字")

    risk = raw.get("risk") or {}
    for key in ("max_total_exposure_pct", "max_single_pct", "max_positions", "min_cash_pct"):
        if key not in risk:
            errors.append(f"缺少 risk.{key}")
    if float(risk.get("max_total_exposure_pct", 0)) <= 0 or float(risk.get("max_total_exposure_pct", 0)) > 1:
        errors.append("risk.max_total_exposure_pct 必须在 (0, 1]")
    if float(risk.get("max_single_pct", 0)) <= 0 or float(risk.get("max_single_pct", 0)) > 1:
        errors.append("risk.max_single_pct 必须在 (0, 1]")

    entry = raw.get("entry") or {}
    if float(entry.get("stop_pct", 0)) >= 0:
        errors.append("entry.stop_pct 必须为负（止损幅度）")
    if float(entry.get("take_pct", 0)) <= 0:
        errors.append("entry.take_pct 必须为正（止盈幅度）")
    min_score = float(entry.get("min_score", 0))
    if not (-1 <= min_score <= 1):
        errors.append("entry.min_score 必须在 [-1, 1]")

    universe = raw.get("universe")
    if not isinstance(universe, list) or not universe:
        errors.append("universe 不能为空")
        universe = []
    seen: set[str] = set()
    for i, item in enumerate(universe):
        if not isinstance(item, dict):
            errors.append(f"universe[{i}] 不是对象")
            continue
        code = str(item.get("code", "")).strip()
        if len(code) != 6 or not code.isdigit():
            errors.append(f"universe[{i}].code 非法: {code!r}")
        if code in seen:
            errors.append(f"universe 代码重复: {code}")
        seen.add(code)
        if not str(item.get("name", "")).strip():
            errors.append(f"universe[{i}].name 为空")
        if not str(item.get("thesis", "")).strip():
            errors.append(f"universe[{i}].thesis 为空（计划必须写明入场逻辑）")

    if errors:
        raise PlanError("计划校验失败：\n- " + "\n- ".join(errors))

    raw.setdefault("exit", {})
    raw.setdefault("risk", {})
    raw["risk"].setdefault("initial_cash", 1_000_000.0)
    raw["risk"].setdefault("max_drawdown_pct", -0.20)
    raw["risk"].setdefault("max_daily_trades", 12)
    raw["entry"].setdefault("max_candidates_per_round", 3)
    raw["entry"].setdefault("require_above_ma20", True)
    raw["entry"].setdefault("min_rsi6", 25.0)
    raw["entry"].setdefault("max_rsi6", 72.0)
    raw["entry"].setdefault("max_change_pct", 7.0)
    raw["entry"].setdefault("max_per_sector", 1)
    raw["exit"].setdefault("score_exit", -0.45)
    for item in universe:
        item.setdefault("weight", 1.0)
        item.setdefault("stop_pct", entry["stop_pct"])
        item.setdefault("take_pct", entry["take_pct"])
        item.setdefault("defensive", False)
    return raw


def macro_blocks(plan: dict, regime: str) -> str:
    """复刻宏观硬闸：高危 risk_off 进行中时，不许标"进攻"。"""
    macro = plan.get("macro") or {}
    if bool(macro.get("risk_off_severe")) and regime == "进攻":
        titles = macro.get("risk_off_titles") or "高危事件"
        return (
            f"⚠️ 宏观事件表有进行中的高危 risk_off 事件（{titles}），buy 不得标 macro_regime=进攻；"
            "请改为 中性/防御 并缩小仓位"
        )
    return ""


def validate_buy(plan: dict, snapshot: dict[str, Any]) -> str:
    """buy 落库前硬闸。放行返回空串，拒绝返回中文理由。"""
    stop_pct = _num(snapshot.get("plan_stop_pct"))
    stop_price = _num(snapshot.get("plan_stop_price"))
    if not ((stop_pct is not None and stop_pct < 0) or (stop_price is not None and stop_price > 0)):
        return "⚠️ buy 须在 snapshot 写入 plan_stop_pct(<0) 或 plan_stop_price(>0)（数值止损）；已拒绝落库"
    regime = str(snapshot.get("macro_regime", "")).strip()
    if regime not in MACRO_REGIMES:
        return f"⚠️ buy 须写 macro_regime（{'/'.join(MACRO_REGIMES)} 之一）；已拒绝落库"
    note = str(snapshot.get("macro_note", "")).strip()
    if len(note) < MACRO_NOTE_MIN_LEN:
        return f"⚠️ buy 须写 macro_note（≥{MACRO_NOTE_MIN_LEN} 字宏观依据）；已拒绝落库"
    return macro_blocks(plan, regime)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
