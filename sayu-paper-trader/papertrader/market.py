"""东方财富行情客户端（仅用标准库）。

复刻 SayuStock 的 ``get_market()`` 最小子集：
- 实时快照 push2（现价 / 昨收 / 涨跌幅 / 名称）
- 日 K / 分钟 K push2his
- 批量快照 ulist.np

价格为 ``fltt=2`` 放大后的浮点，避免各股 decimal 不同导致 /100 错误。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .config import TZ

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_LIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_TENCENT_QUOTE = "https://qt.gtimg.cn/q="
_TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_TENCENT_HEADERS = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}


def _tencent_symbol(code: str) -> str:
    """A 股代码 -> 腾讯行情 symbol。1.000001 -> sh000001。"""
    if "." in code:
        market, raw = code.split(".", 1)
        return ("sh" if market == "1" else "sz") + raw
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def _http_text(url: str, encoding: str = "utf-8", timeout: float = 12.0) -> str:
    req = urllib.request.Request(url, headers=_TENCENT_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, "replace")


class MarketError(RuntimeError):
    """行情不可达 / 返回结构异常。"""


@dataclass(frozen=True, slots=True)
class Quote:
    code: str
    name: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    change_pct: float
    volume: float
    amount: float
    turnover: float | None
    volume_ratio: float | None
    ts: str

    @property
    def change(self) -> float:
        return round(self.price - self.prev_close, 4) if self.prev_close else 0.0


@dataclass(frozen=True, slots=True)
class Bar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    pct_chg: float
    turnover: float

    @property
    def is_up(self) -> bool:
        return self.close >= self.open


def secid(code: str) -> str:
    """A 股代码 -> 东财 secid。已带市场前缀（如 1.000001）则原样返回。"""
    code = code.strip()
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def _get_json(url: str, params: dict[str, str], timeout: float = 12.0) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"})
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise MarketError("响应不是 JSON 对象")
            return payload
        except Exception as exc:  # noqa: BLE001 - 外部不可信输入，统一重试
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise MarketError(f"行情请求失败: {type(last).__name__}: {last}")


class MarketClient:
    """带 TTL 内存缓存的行情客户端。单轮决策内复用，避免重复打接口。"""

    def __init__(self, quote_ttl: float = 30.0, kline_ttl: float = 300.0) -> None:
        self.quote_ttl = quote_ttl
        self.kline_ttl = kline_ttl
        self._quote_cache: dict[str, tuple[float, Quote]] = {}
        self._kline_cache: dict[tuple[str, int, int], tuple[float, list[Bar]]] = {}
        # 东财在开盘/限流时会直接 RemoteDisconnected；连续失败两次后
        # 短时间内切到腾讯行情，避免每个代码都白等 3 次重试。
        self._em_failures: int = 0
        self._em_down_until: float = 0.0

    def _em_ok(self) -> bool:
        return time.time() >= self._em_down_until

    def _em_fail(self) -> None:
        self._em_failures += 1
        if self._em_failures >= 2:
            self._em_down_until = time.time() + 120.0

    def _em_success(self) -> None:
        self._em_failures = 0

    # ---------------- 快照 ----------------
    def quote(self, code: str) -> Quote:
        now = time.time()
        hit = self._quote_cache.get(code)
        if hit and now - hit[0] < self.quote_ttl:
            return hit[1]
        if self._em_ok():
            try:
                payload = _get_json(
                    _QUOTE_URL,
                    {
                        "secid": secid(code),
                        "fltt": "2",
                        "fields": "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f116,f170",
                        "ut": _UT,
                    },
                )
                data = payload.get("data")
                if not isinstance(data, dict) or not data.get("f57"):
                    raise MarketError(f"快照为空: {code}")
                q = self._quote_from_dict(data)
                self._em_success()
                self._quote_cache[code] = (now, q)
                return q
            except MarketError:
                self._em_fail()
        q = self._tencent_quote(code)
        self._quote_cache[code] = (now, q)
        return q

    def batch_quotes(self, codes: Iterable[str]) -> dict[str, Quote]:
        codes = list(dict.fromkeys(codes))
        out: dict[str, Quote] = {}
        missing: list[str] = []
        now = time.time()
        for code in codes:
            hit = self._quote_cache.get(code)
            if hit and now - hit[0] < self.quote_ttl:
                out[code] = hit[1]
            else:
                missing.append(code)
        if missing:
            if self._em_ok():
                rows: object = None
                try:
                    payload = _get_json(
                        _LIST_URL,
                        {
                            "secids": ",".join(secid(c) for c in missing),
                            "fltt": "2",
                            "fields": "f2,f3,f4,f12,f13,f14,f15,f16,f17,f18,f5,f6",
                            "ut": _UT,
                        },
                    )
                    data = payload.get("data")
                    rows = data.get("diff") if isinstance(data, dict) else None
                    self._em_success()
                except MarketError:
                    # 开盘/限流时 ulist 可能直接 RemoteDisconnected；切腾讯
                    self._em_fail()
                    rows = None
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict) or not row.get("f12"):
                            continue
                        code = str(row["f12"])
                        try:
                            q = self._quote_from_list_row(code, row)
                        except (TypeError, ValueError):
                            continue
                        out[code] = q
                        self._quote_cache[code] = (now, q)
            missing = [c for c in missing if c not in out]
            if missing:
                try:
                    tencent = self._tencent_batch(missing)
                except MarketError:
                    tencent = {}
                for code, q in tencent.items():
                    out[code] = q
                    self._quote_cache[code] = (now, q)
            for code in [c for c in missing if c not in out]:
                try:
                    out[code] = self.quote(code)
                except MarketError:
                    continue
        return out

    def index_quote(self) -> Quote:
        return self.quote("1.000001")

    # ---------------- 腾讯回退 ----------------
    def _tencent_quote(self, code: str) -> Quote:
        batch = self._tencent_batch([code])
        if code not in batch:
            raise MarketError(f"腾讯快照为空: {code}")
        return batch[code]

    def _tencent_batch(self, codes: list[str]) -> dict[str, Quote]:
        symbols = ",".join(_tencent_symbol(c) for c in codes)
        try:
            text = _http_text(_TENCENT_QUOTE + symbols, encoding="gbk")
        except Exception as exc:  # noqa: BLE001 - 外部 HTTP 统一转为行情错误
            raise MarketError(f"腾讯行情不可达: {type(exc).__name__}: {exc}") from exc
        sym_to_code = {_tencent_symbol(c): c for c in codes}
        out: dict[str, Quote] = {}
        for chunk in text.split(";"):
            chunk = chunk.strip()
            if not chunk.startswith("v_") or "=" not in chunk:
                continue
            key, _, raw = chunk.partition("=")
            raw = raw.strip().strip('"')
            parts = raw.split("~")
            if len(parts) < 35:
                continue
            sym = key[2:]
            code = sym_to_code.get(sym, parts[2] if len(parts) > 2 else "")
            if not code:
                continue
            price = self._f(parts, 3)
            if price <= 0:
                continue
            prev = self._f(parts, 4)
            pct = self._f(parts, 32)
            if pct == 0 and prev > 0:
                pct = round((price - prev) / prev * 100, 4)
            amount = 0.0
            if len(parts) > 35:
                pieces = parts[35].split("/")
                if len(pieces) >= 3:
                    try:
                        amount = float(pieces[2])
                    except ValueError:
                        amount = 0.0
            ts_raw = parts[30] if len(parts) > 30 else ""
            ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
            if len(ts_raw) == 14 and ts_raw.isdigit():
                ts = f"{ts_raw[0:4]}-{ts_raw[4:6]}-{ts_raw[6:8]} {ts_raw[8:10]}:{ts_raw[10:12]}:{ts_raw[12:14]}"
            out[code] = Quote(
                code=code,
                name=parts[1].strip(),
                price=price,
                prev_close=prev,
                open=self._f(parts, 5),
                high=self._f(parts, 33) or price,
                low=self._f(parts, 34) or price,
                change_pct=pct,
                volume=self._f(parts, 36) or self._f(parts, 6),
                amount=amount,
                turnover=self._f(parts, 38) or None,
                volume_ratio=None,
                ts=ts,
            )
        return out

    def _tencent_kline(self, code: str, limit: int) -> list[Bar]:
        symbol = _tencent_symbol(code)
        url = f"{_TENCENT_KLINE}?param={symbol},day,,,{limit},qfq"
        try:
            text = _http_text(url, encoding="utf-8")
            payload = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise MarketError(f"腾讯 K 线不可达: {type(exc).__name__}: {exc}") from exc
        data = payload.get("data")
        node = data.get(symbol) if isinstance(data, dict) else None
        rows = None
        if isinstance(node, dict):
            for key in ("qfqday", "day", "hfqday"):
                candidate = node.get(key)
                if isinstance(candidate, list) and candidate:
                    rows = candidate
                    break
        if not isinstance(rows, list) or not rows:
            raise MarketError(f"腾讯 K 线为空: {code}")
        bars: list[Bar] = []
        prev_close: float | None = None
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                date_str = str(row[0])
                open_p = float(row[1])
                close_p = float(row[2])
                high_p = float(row[3])
                low_p = float(row[4])
                volume = float(row[5])
            except (TypeError, ValueError):
                continue
            pct = 0.0
            if prev_close and prev_close > 0:
                pct = round((close_p - prev_close) / prev_close * 100, 4)
            bars.append(
                Bar(
                    date=date_str,
                    open=open_p,
                    close=close_p,
                    high=high_p,
                    low=low_p,
                    volume=volume,
                    amount=0.0,
                    pct_chg=pct,
                    turnover=0.0,
                )
            )
            prev_close = close_p
        return bars

    @staticmethod
    def _f(parts: list[str], idx: int) -> float:
        try:
            return float(parts[idx])
        except (IndexError, TypeError, ValueError):
            return 0.0

    # ---------------- K 线 ----------------
    def kline(self, code: str, klt: int = 101, limit: int = 160) -> list[Bar]:
        key = (code, klt, limit)
        now = time.time()
        hit = self._kline_cache.get(key)
        if hit and now - hit[0] < self.kline_ttl:
            return hit[1]
        if self._em_ok():
            try:
                payload = _get_json(
                    _KLINE_URL,
                    {
                        "secid": secid(code),
                        "klt": str(klt),
                        "fqt": "1",
                        "lmt": str(limit),
                        "end": "20500101",
                        "fields1": "f1,f2,f3,f4,f5,f6",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                        "ut": _UT,
                    },
                )
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
                    raise MarketError(f"K 线为空: {code}")
                bars = [self._bar_from_line(line) for line in data["klines"] if isinstance(line, str)]
                self._em_success()
                self._kline_cache[key] = (now, bars)
                return bars
            except MarketError:
                self._em_fail()
        if klt != 101:
            # 腾讯回退目前只实现日 K；分钟线（回放）保持东财
            raise MarketError(f"东财日 K 不可达，腾讯回退不支持 klt={klt}")
        bars = self._tencent_kline(code, limit)
        self._kline_cache[key] = (now, bars)
        return bars

    def bar_at(self, code: str, when: datetime) -> Bar | None:
        """回放用：取 ``when`` 当时（或之前最近）的 1 分钟 K 线。"""
        bars = self.kline(code, klt=1, limit=500)
        target = when.strftime("%Y-%m-%d %H:%M")
        candidates = [b for b in bars if b.date <= target]
        return candidates[-1] if candidates else None

    # ---------------- 解析 ----------------
    @staticmethod
    def _quote_from_dict(d: dict) -> Quote:
        def f(key: str) -> float:
            value = d.get(key)
            if value in (None, "-", ""):
                return 0.0
            return float(value)

        ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        price = f("f43")
        prev = f("f60")
        pct = f("f170")
        if pct == 0 and prev > 0 and price > 0:
            pct = round((price - prev) / prev * 100, 4)
        return Quote(
            code=str(d.get("f57", "")),
            name=str(d.get("f58", "")),
            price=price,
            prev_close=prev,
            open=f("f46"),
            high=f("f44"),
            low=f("f45"),
            change_pct=pct,
            volume=f("f47"),
            amount=f("f48"),
            turnover=f("f50") or None,
            volume_ratio=None,
            ts=ts,
        )

    @staticmethod
    def _quote_from_list_row(code: str, d: dict) -> Quote:
        def f(key: str) -> float:
            value = d.get(key)
            if value in (None, "-", ""):
                return 0.0
            return float(value)

        ts = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
        price = f("f2")
        prev = f("f18")
        pct = f("f3")
        if pct == 0 and prev > 0 and price > 0:
            pct = round((price - prev) / prev * 100, 4)
        return Quote(
            code=code,
            name=str(d.get("f14", "")),
            price=price,
            prev_close=prev,
            open=f("f17"),
            high=f("f15"),
            low=f("f16"),
            change_pct=pct,
            volume=f("f5"),
            amount=f("f6"),
            turnover=None,
            volume_ratio=None,
            ts=ts,
        )

    @staticmethod
    def _bar_from_line(line: str) -> Bar:
        parts = line.split(",")
        if len(parts) < 11:
            raise MarketError(f"K 线字段不足: {line!r}")

        def f(i: int) -> float:
            try:
                return float(parts[i])
            except (TypeError, ValueError):
                return 0.0

        return Bar(
            date=parts[0],
            open=f(1),
            close=f(2),
            high=f(3),
            low=f(4),
            volume=f(5),
            amount=f(6),
            pct_chg=f(8),
            turnover=f(10),
        )
