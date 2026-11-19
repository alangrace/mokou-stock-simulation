"""技术指标（纯函数，单源）。

对应 SayuStock ``utils/indicators.py`` 的最小子集：MA / MACD / RSI / ATR /
量比 / 20 日分位。所有计算只依赖传入的 K 线，不做任何取数。
"""

from __future__ import annotations

from statistics import fmean

from .market import Bar


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return fmean(values[-n:])


def ema_series(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (n + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, signal)
    bar = 2 * (dif[-1] - dea[-1])
    return round(dif[-1], 6), round(dea[-1], 6), round(bar, 6)


def rsi(closes: list[float], n: int = 6) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-n, 0):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / n
    avg_loss = losses / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def atr_pct(bars: list[Bar], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs: list[float] = []
    for i in range(-n, 0):
        high = bars[i].high
        low = bars[i].low
        prev_close = bars[i - 1].close
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    close = bars[-1].close
    if close <= 0:
        return None
    return round(fmean(trs) / close, 5)


def _cross_in_3d(fast: list[float], slow: list[float]) -> tuple[bool, bool]:
    golden = False
    death = False
    for i in range(1, min(4, len(fast))):
        if fast[-i] > slow[-i] and fast[-i - 1] <= slow[-i - 1]:
            golden = True
        if fast[-i] < slow[-i] and fast[-i - 1] >= slow[-i - 1]:
            death = True
    return golden, death


def compute(bars: list[Bar]) -> dict:
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    if len(closes) < 30:
        raise ValueError(f"K 线不足（{len(closes)} 根），至少 30 根")

    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    dif, dea, bar = macd(closes)
    dif_series = [a - b for a, b in zip(ema_series(closes, 12), ema_series(closes, 26))]
    dea_series = ema_series(dif_series, 9)
    golden, death = _cross_in_3d(dif_series, dea_series)
    rsi6 = rsi(closes, 6)
    rsi12 = rsi(closes, 12)
    atr = atr_pct(bars, 14)

    prev_vols = volumes[-6:-1]
    vol_ratio = round(volumes[-1] / fmean(prev_vols), 2) if prev_vols and fmean(prev_vols) > 0 else None

    high20 = max(b.high for b in bars[-20:])
    low20 = min(b.low for b in bars[-20:])
    pos20 = round((closes[-1] - low20) / (high20 - low20), 4) if high20 > low20 else 0.5
    pct20 = round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) > 21 and closes[-21] else None

    bull = bool(ma5 and ma10 and ma20 and ma5 > ma10 > ma20)
    bear = bool(ma5 and ma10 and ma20 and ma5 < ma10 < ma20)

    return {
        "close": closes[-1],
        "ma5": round(ma5, 3) if ma5 else None,
        "ma10": round(ma10, 3) if ma10 else None,
        "ma20": round(ma20, 3) if ma20 else None,
        "ma60": round(ma60, 3) if ma60 else None,
        "ma_bull": bull,
        "ma_bear": bear,
        "above_ma20": bool(ma20 and closes[-1] > ma20),
        "dif": dif,
        "dea": dea,
        "macd_bar": bar,
        "macd_golden_cross_3d": golden,
        "macd_death_cross_3d": death,
        "rsi6": rsi6,
        "rsi12": rsi12,
        "atr_pct": atr,
        "vol_ratio": vol_ratio,
        "pos_20d": pos20,
        "pct_20d": pct20,
        "turnover": bars[-1].turnover,
        "amount": bars[-1].amount,
        "bars": len(bars),
        "last_date": bars[-1].date,
    }
