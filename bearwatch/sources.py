"""
数据抓取层。

设计原则：
  1. 全部使用免费、无需 API key 的公开端点（Stooq CSV + FRED 公开图表 CSV）。
  2. 任何单一数据源失败都不能让整个流程崩掉 —— 失败的因子会被标记为
     unavailable，打分时按"权重重分配"处理，而不是当成 0 分。
  3. 每次成功抓取都会落盘缓存，下次抓不到时回退到缓存并标注数据陈旧。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests

log = logging.getLogger("bearwatch.sources")

UA = "bear-watch/1.0 (+https://github.com/)"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache")


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
@dataclass
class Series:
    """一条时间序列：日期升序排列的 (date, value) 列表。"""

    name: str
    points: List[Tuple[date, float]] = field(default_factory=list)
    stale: bool = False          # True = 用的是缓存，不是本次抓到的新数据
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return len(self.points) > 0

    @property
    def last(self) -> Optional[float]:
        return self.points[-1][1] if self.points else None

    @property
    def last_date(self) -> Optional[date]:
        return self.points[-1][0] if self.points else None

    def values(self) -> List[float]:
        return [v for _, v in self.points]

    def tail(self, n: int) -> List[float]:
        return self.values()[-n:]

    def value_n_ago(self, n: int) -> Optional[float]:
        """n 个观测值之前的数值（不是 n 个日历日）。"""
        vals = self.values()
        if len(vals) <= n:
            return None
        return vals[-(n + 1)]

    def value_on_or_before(self, target: date) -> Optional[float]:
        best = None
        for d, v in self.points:
            if d <= target:
                best = v
            else:
                break
        return best

    def sma(self, n: int) -> Optional[float]:
        vals = self.values()
        if len(vals) < n:
            return None
        return sum(vals[-n:]) / n

    def max_over(self, n: int) -> Optional[float]:
        vals = self.tail(n)
        return max(vals) if vals else None

    def min_over(self, n: int) -> Optional[float]:
        vals = self.tail(n)
        return min(vals) if vals else None


def _cache_path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(CACHE_DIR, f"{safe}.json")


def _write_cache(key: str, s: Series) -> None:
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(
                {"name": s.name, "points": [[d.isoformat(), v] for d, v in s.points]},
                f,
            )
    except Exception as exc:  # pragma: no cover - 缓存写失败不应影响主流程
        log.warning("cache write failed for %s: %s", key, exc)


def _read_cache(key: str) -> Optional[Series]:
    p = _cache_path(key)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
        pts = [(date.fromisoformat(d), float(v)) for d, v in raw["points"]]
        return Series(name=raw["name"], points=pts, stale=True)
    except Exception as exc:  # pragma: no cover
        log.warning("cache read failed for %s: %s", key, exc)
        return None


def _http_get(url: str, params: Optional[dict], retries: int, backoff: float) -> Optional[str]:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(
                url,
                params=params,
                headers={"User-Agent": UA, "Accept": "text/csv,*/*"},
                timeout=45,
            )
            if r.status_code == 200 and r.text and len(r.text) > 40:
                return r.text
            last_err = f"HTTP {r.status_code} len={len(r.text) if r.text else 0}"
        except Exception as exc:
            last_err = repr(exc)
        if attempt < retries:
            time.sleep(backoff * attempt)
    log.warning("GET failed %s params=%s: %s", url, params, last_err)
    return None


def _parse_csv(text: str, date_col: str, value_col: str, name: str) -> Series:
    """解析 CSV，跳过缺失值（FRED 用 '.' 表示缺失）。"""
    pts: List[Tuple[date, float]] = []
    rdr = csv.DictReader(io.StringIO(text))
    fields = {(fn or "").strip().lower(): (fn or "") for fn in (rdr.fieldnames or [])}
    dk = fields.get(date_col.lower())
    vk = fields.get(value_col.lower())
    if dk is None or vk is None:
        # FRED 有时把值列命名成 series id 之外的名字，退化为"第一列日期 + 最后一列值"
        names = [n for n in (rdr.fieldnames or []) if n]
        if len(names) >= 2:
            dk, vk = names[0], names[-1]
        else:
            return Series(name=name, error=f"unexpected columns: {rdr.fieldnames}")
    for row in rdr:
        raw_d = (row.get(dk) or "").strip()
        raw_v = (row.get(vk) or "").strip()
        if not raw_d or raw_v in ("", ".", "NaN", "null", "N/A"):
            continue
        try:
            d = datetime.strptime(raw_d[:10], "%Y-%m-%d").date()
            pts.append((d, float(raw_v)))
        except ValueError:
            continue
    pts.sort(key=lambda t: t[0])
    return Series(name=name, points=pts)


# --------------------------------------------------------------------------
# Stooq：日线行情
# --------------------------------------------------------------------------
def fetch_stooq(symbol: str, name: str, cfg: dict) -> Series:
    """
    Stooq 日线 CSV：Date,Open,High,Low,Close,Volume
    指数用 ^spx / ^ndx / ^vix，美股 ETF 用 spy.us 这种后缀形式。
    """
    base = cfg["sources"]["stooq"]["base"]
    retries = cfg["sources"].get("retries", 4)
    backoff = cfg["sources"].get("backoff_seconds", 3)
    text = _http_get(base, {"s": symbol, "i": "d"}, retries, backoff)
    if text and not text.lower().startswith("exceeded"):
        s = _parse_csv(text, "date", "close", name)
        if s.ok and len(s.points) > 60:
            _write_cache(f"stooq_{name}", s)
            return s
        log.warning("stooq %s parsed but too short (%d rows)", symbol, len(s.points))
    cached = _read_cache(f"stooq_{name}")
    if cached:
        log.warning("stooq %s -> using STALE cache (last=%s)", symbol, cached.last_date)
        return cached
    return Series(name=name, error=f"stooq fetch failed for {symbol}")


# --------------------------------------------------------------------------
# FRED：宏观序列（公开 CSV 端点，不需要 API key）
# --------------------------------------------------------------------------
def fetch_fred(series_id: str, name: str, cfg: dict, years: int = 30) -> Series:
    base = cfg["sources"]["fred"]["base"]
    retries = cfg["sources"].get("retries", 4)
    backoff = cfg["sources"].get("backoff_seconds", 3)
    start = (date.today() - timedelta(days=365 * years)).isoformat()
    params = {"id": series_id, "cosd": start}
    # 如果用户配置了 FRED API key，走官方 API（更稳），否则走公开 CSV
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if api_key:
        s = _fetch_fred_api(series_id, name, api_key, start, retries, backoff)
        if s.ok:
            _write_cache(f"fred_{name}", s)
            return s
    text = _http_get(base, params, retries, backoff)
    if text:
        s = _parse_csv(text, "observation_date", series_id, name)
        if not s.ok:
            s = _parse_csv(text, "date", series_id, name)
        if s.ok:
            _write_cache(f"fred_{name}", s)
            return s
    cached = _read_cache(f"fred_{name}")
    if cached:
        log.warning("fred %s -> using STALE cache (last=%s)", series_id, cached.last_date)
        return cached
    return Series(name=name, error=f"fred fetch failed for {series_id}")


def _fetch_fred_api(series_id, name, api_key, start, retries, backoff) -> Series:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
    }
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=45, headers={"User-Agent": UA})
            if r.status_code == 200:
                obs = r.json().get("observations", [])
                pts = []
                for o in obs:
                    if o.get("value") in (".", "", None):
                        continue
                    try:
                        pts.append((date.fromisoformat(o["date"]), float(o["value"])))
                    except (ValueError, KeyError):
                        continue
                pts.sort(key=lambda t: t[0])
                return Series(name=name, points=pts)
        except Exception as exc:
            log.debug("fred api attempt %d failed: %s", attempt, exc)
        time.sleep(backoff * attempt)
    return Series(name=name, error="fred api failed")


# --------------------------------------------------------------------------
# CAPE（席勒市盈率）：优先 multpl，失败则用 SPX/10年平均实际盈利的近似回退
# --------------------------------------------------------------------------
def fetch_cape(cfg: dict) -> Series:
    """
    multpl.com 提供席勒 CAPE 的月度表格。这里做轻量解析。
    抓不到就返回空 Series，打分层会把 valuation 因子标为 unavailable。
    """
    retries = cfg["sources"].get("retries", 4)
    backoff = cfg["sources"].get("backoff_seconds", 3)
    text = _http_get("https://www.multpl.com/shiller-pe/table/by-month", None, retries, backoff)
    if text:
        s = _parse_multpl_table(text, "cape")
        if s.ok:
            _write_cache("cape", s)
            return s
    cached = _read_cache("cape")
    if cached:
        return cached
    return Series(name="cape", error="cape fetch failed")


def _parse_multpl_table(html: str, name: str) -> Series:
    """从 multpl 的 HTML 表格里抠出 (日期, 数值)。故意写得很宽容。"""
    import re

    pts: List[Tuple[date, float]] = []
    row_re = re.compile(
        r"<td[^>]*>\s*([A-Z][a-z]{2}\s+\d{1,2},\s*\d{4})\s*</td>\s*<td[^>]*>\s*([0-9.]+)",
        re.S,
    )
    for m in row_re.finditer(html):
        try:
            d = datetime.strptime(m.group(1).replace("  ", " "), "%b %d, %Y").date()
            pts.append((d, float(m.group(2))))
        except ValueError:
            continue
    pts.sort(key=lambda t: t[0])
    return Series(name=name, points=pts)


# --------------------------------------------------------------------------
# 汇总抓取
# --------------------------------------------------------------------------
def fetch_all(cfg: dict) -> Dict[str, Series]:
    out: Dict[str, Series] = {}
    for key, sym in cfg["sources"]["stooq"]["symbols"].items():
        out[key] = fetch_stooq(sym, key, cfg)
        log.info("stooq %-6s %-8s rows=%-6d last=%s stale=%s",
                 key, sym, len(out[key].points), out[key].last_date, out[key].stale)
    for key, sid in cfg["sources"]["fred"]["series"].items():
        out[key] = fetch_fred(sid, key, cfg)
        log.info("fred  %-9s %-14s rows=%-6d last=%s stale=%s",
                 key, sid, len(out[key].points), out[key].last_date, out[key].stale)
    out["cape"] = fetch_cape(cfg)
    log.info("cape  rows=%d last=%s", len(out["cape"].points), out["cape"].last_date)
    return out
