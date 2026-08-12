"""
规则打分模型。

核心思想
--------
把"未来 3 个月开启一轮 -20% 熊市"的概率拆成 10 个可观测因子，每个因子
独立映射到 0-100 的风险分（0 = 完全没有熊市味道，100 = 历史上熊市开始前
的典型读数），再按权重加权平均，最后用一条校准过的 logistic 曲线把总分
翻译成概率。

为什么不让 LLM 直接给概率
-------------------------
LLM 给出的概率不可复现、不可回溯检验，而且会跟着新闻情绪漂移。这里的
分数完全由数字决定，同样的输入永远得到同样的输出，一年后你可以回头检验
每一次读数对不对。LLM 只负责把分数讲成人话。

每个因子都会输出 detail 字段，说明它为什么给这个分 —— 报告和 Telegram
消息里都会带上，方便你自己判断这个分合不合理。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date
from typing import Dict, List, Optional

from .sources import Series


# --------------------------------------------------------------------------
# 工具：分段线性插值。给定若干 (输入, 风险分) 锚点，线性插值 + 两端截断。
# --------------------------------------------------------------------------
def piecewise(x: float, anchors: List[tuple]) -> float:
    anchors = sorted(anchors, key=lambda a: a[0])
    if x <= anchors[0][0]:
        return float(anchors[0][1])
    if x >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y1)
            t = (x - x0) / (x1 - x0)
            return float(y0 + t * (y1 - y0))
    return float(anchors[-1][1])


def pct_rank(value: float, population: List[float]) -> Optional[float]:
    """百分位排名，用 midrank 处理并列，避免大量并列值把分位顶到 100。"""
    if not population:
        return None
    below = sum(1 for v in population if v < value)
    equal = sum(1 for v in population if v == value)
    return 100.0 * (below + 0.5 * equal) / len(population)


@dataclass
class Factor:
    key: str
    label: str
    weight: float
    score: Optional[float]           # 0-100；None = 数据缺失
    detail: str                      # 人话解释
    values: Dict[str, Optional[float]]   # 原始数值，写进 history.jsonl 供回溯

    @property
    def available(self) -> bool:
        return self.score is not None


# ==========================================================================
# 各因子
# ==========================================================================
def f_valuation(d: Dict[str, Series], w: float) -> Factor:
    cape = d.get("cape")
    if not cape or not cape.ok:
        return Factor("valuation", "估值 (CAPE)", w, None, "CAPE 数据缺失", {})
    cur = cape.last
    # 用 1950 年以后的历史做分位（1871-1950 的低估值会让分位失真）
    pop = [v for dt, v in cape.points if dt.year >= 1950]
    rank = pct_rank(cur, pop) or 50.0
    # 分位直接当风险分，但把 90 分位以上的区间拉伸 —— 泡沫尾部才是真风险
    score = piecewise(rank, [(0, 5), (50, 30), (75, 45), (90, 62), (95, 75), (99, 92), (100, 98)])
    detail = f"席勒 CAPE = {cur:.1f}，处于 1950 年以来第 {rank:.0f} 百分位"
    return Factor("valuation", "估值 (CAPE)", w, score, detail,
                  {"cape": cur, "cape_pctile_since1950": rank})


def f_trend(d: Dict[str, Series], w: float) -> Factor:
    spx = d.get("spx")
    if not spx or not spx.ok or len(spx.points) < 260:
        return Factor("trend", "价格趋势", w, None, "SPX 日线数据不足", {})
    px = spx.last
    sma50, sma200 = spx.sma(50), spx.sma(200)
    hi252 = spx.max_over(252)
    dd = (px / hi252 - 1.0) if hi252 else 0.0
    dist200 = (px / sma200 - 1.0) if sma200 else 0.0

    s_dist = piecewise(dist200 * 100, [(-15, 96), (-10, 90), (-5, 78), (-2, 62),
                                       (0, 52), (3, 38), (7, 25), (12, 15), (20, 12)])
    death_cross = bool(sma50 and sma200 and sma50 < sma200)
    s_cross = 78.0 if death_cross else 22.0
    s_dd = piecewise(dd * 100, [(-25, 96), (-20, 92), (-15, 85), (-10, 66),
                                (-5, 38), (-2, 20), (0, 12)])
    score = 0.40 * s_dist + 0.25 * s_cross + 0.35 * s_dd
    detail = (f"SPX {px:,.0f}，距 52 周高点 {dd*100:+.1f}%；"
              f"较 200 日均线 {dist200*100:+.1f}%；"
              f"50/200 日均线{'死叉 ⚠' if death_cross else '金叉'}")
    return Factor("trend", "价格趋势", w, score, detail,
                  {"spx": px, "sma50": sma50, "sma200": sma200,
                   "drawdown_from_52w_high": dd, "dist_to_sma200": dist200,
                   "death_cross": 1.0 if death_cross else 0.0})


def f_credit(d: Dict[str, Series], w: float) -> Factor:
    oas = d.get("hy_oas")
    if not oas or not oas.ok:
        return Factor("credit", "信用利差 (HY OAS)", w, None, "高收益债利差数据缺失", {})
    cur = oas.last                      # 单位：百分点
    prev = oas.value_n_ago(60)          # 约 3 个月前（交易日）
    delta_bp = (cur - prev) * 100 if prev is not None else None

    s_level = piecewise(cur * 100, [(230, 15), (300, 28), (400, 45), (500, 65),
                                    (700, 85), (1000, 97)])
    if delta_bp is None:
        s_delta, dtxt = s_level, "（60 日变化不可得）"
    else:
        s_delta = piecewise(delta_bp, [(-100, 12), (-50, 20), (0, 28), (50, 48),
                                       (100, 70), (200, 90), (350, 98)])
        dtxt = f"，60 个交易日变化 {delta_bp:+.0f}bp"
    score = 0.45 * s_level + 0.55 * s_delta
    detail = f"高收益债 OAS = {cur*100:.0f}bp{dtxt}"
    return Factor("credit", "信用利差 (HY OAS)", w, score, detail,
                  {"hy_oas_bp": cur * 100, "hy_oas_delta_60d_bp": delta_bp})


def f_curve(d: Dict[str, Series], w: float) -> Factor:
    c = d.get("t10y3m")
    if not c or not c.ok:
        return Factor("curve", "收益率曲线 (10y-3m)", w, None, "收益率曲线数据缺失", {})
    cur = c.last
    min24 = min(v for dt, v in c.points if (date.today() - dt).days <= 730) \
        if any((date.today() - dt).days <= 730 for dt, _ in c.points) else cur
    was_inverted = min24 < -0.05

    if cur < -0.5:
        score, why = 62.0, "深度倒挂中 —— 这是领先信号，熊市通常还要等 6-18 个月"
    elif cur < 0:
        score, why = 58.0, "轻度倒挂"
    elif was_inverted and cur < 0.8:
        score, why = 74.0, "刚从倒挂中转正并陡峭化 —— 历史上这个阶段离衰退最近"
    elif was_inverted:
        score, why = 55.0, "过去两年曾倒挂，目前已明显转正"
    else:
        score, why = 20.0, "过去两年未倒挂"
    detail = f"10年-3月 = {cur:+.2f}%（24 个月最低 {min24:+.2f}%）；{why}"
    return Factor("curve", "收益率曲线 (10y-3m)", w, score, detail,
                  {"t10y3m": cur, "t10y3m_min_24m": min24})


def f_volatility(d: Dict[str, Series], w: float) -> Factor:
    vix = d.get("vix")
    if not vix or not vix.ok:
        vix = d.get("vixcl")
    if not vix or not vix.ok:
        return Factor("volatility", "波动率 (VIX)", w, None, "VIX 数据缺失", {})
    cur = vix.last
    prev20 = vix.value_n_ago(20)
    dv = (cur - prev20) if prev20 is not None else None
    s_level = piecewise(cur, [(10, 12), (13, 20), (16, 32), (20, 47),
                              (25, 63), (30, 78), (40, 92), (60, 98)])
    if dv is None:
        s_delta = s_level
        dtxt = ""
    else:
        s_delta = piecewise(dv, [(-8, 12), (-3, 22), (0, 30), (3, 45),
                                 (6, 62), (10, 80), (18, 95)])
        dtxt = f"，20 日变化 {dv:+.1f}"
    score = 0.5 * s_level + 0.5 * s_delta
    detail = f"VIX = {cur:.1f}{dtxt}"
    return Factor("volatility", "波动率 (VIX)", w, score, detail,
                  {"vix": cur, "vix_delta_20d": dv})


def f_labor(d: Dict[str, Series], w: float) -> Factor:
    un = d.get("unrate")
    pay = d.get("payems")
    if (not un or not un.ok) and (not pay or not pay.ok):
        return Factor("labor", "就业", w, None, "就业数据缺失", {})

    sahm = None
    s_sahm = None
    if un and un.ok and len(un.points) >= 15:
        v = un.values()
        ma3 = [sum(v[i - 2:i + 1]) / 3 for i in range(2, len(v))]
        cur3 = ma3[-1]
        min12 = min(ma3[-12:]) if len(ma3) >= 12 else min(ma3)
        sahm = cur3 - min12
        s_sahm = piecewise(sahm, [(-0.2, 10), (0, 18), (0.2, 35), (0.35, 55),
                                  (0.5, 80), (0.8, 93), (1.5, 98)])

    pay3 = None
    s_pay = None
    if pay and pay.ok and len(pay.points) >= 5:
        v = pay.values()                 # 单位：千人
        pay3 = (v[-1] - v[-4]) / 3.0     # 近 3 个月月均新增（千人）
        s_pay = piecewise(pay3, [(-150, 95), (-50, 88), (0, 70), (50, 50),
                                 (100, 32), (150, 20), (250, 12)])

    parts = [(s, wt) for s, wt in ((s_sahm, 0.55), (s_pay, 0.45)) if s is not None]
    if not parts:
        return Factor("labor", "就业", w, None, "就业数据不足以计算", {})
    score = sum(s * wt for s, wt in parts) / sum(wt for _, wt in parts)

    bits = []
    if sahm is not None:
        flag = " ⚠已触发衰退阈值" if sahm >= 0.5 else ""
        bits.append(f"Sahm 规则 = {sahm:+.2f}pp（阈值 0.50）{flag}")
    if pay3 is not None:
        bits.append(f"非农近 3 个月月均 {pay3*1000:+,.0f} 人")
    return Factor("labor", "就业", w, score, "；".join(bits),
                  {"sahm_gap_pp": sahm, "payroll_3m_avg_thousands": pay3})


def f_policy(d: Dict[str, Series], w: float) -> Factor:
    ff = d.get("fedfunds")
    core = d.get("corecpi")
    if not ff or not ff.ok:
        return Factor("policy", "货币政策", w, None, "政策利率数据缺失", {})
    nominal = ff.last
    infl = None
    if core and core.ok and len(core.points) >= 13:
        v = core.values()
        infl = (v[-1] / v[-13] - 1.0) * 100
    if infl is None:
        score = piecewise(nominal, [(0, 20), (2, 35), (4, 55), (6, 78)])
        detail = f"有效联邦基金利率 {nominal:.2f}%（核心通胀不可得）"
        return Factor("policy", "货币政策", w, score, detail,
                      {"fed_funds": nominal, "core_cpi_yoy": None, "real_rate": None})
    real = nominal - infl
    gap = infl - 2.0
    s_real = piecewise(real, [(-3, 15), (-1, 28), (0, 38), (1, 52),
                              (2, 68), (3, 84), (4.5, 94)])
    s_gap = piecewise(gap, [(-1, 18), (0, 26), (0.5, 38), (1, 50),
                            (2, 70), (3, 86), (5, 96)])
    score = 0.5 * s_real + 0.5 * s_gap
    detail = (f"联邦基金利率 {nominal:.2f}%，核心 CPI 同比 {infl:.1f}%，"
              f"实际利率 {real:+.2f}%，通胀缺口 {gap:+.1f}pp")
    return Factor("policy", "货币政策", w, score, detail,
                  {"fed_funds": nominal, "core_cpi_yoy": infl, "real_rate": real})


def f_breadth(d: Dict[str, Series], w: float) -> Factor:
    rsp, spy = d.get("rsp"), d.get("spy")
    if not rsp or not spy or not rsp.ok or not spy.ok:
        return Factor("breadth", "市场宽度 (RSP/SPY)", w, None, "等权重/市值加权数据缺失", {})
    n = 63
    if len(rsp.points) < n + 1 or len(spy.points) < n + 1:
        return Factor("breadth", "市场宽度 (RSP/SPY)", w, None, "历史长度不足", {})
    cur = rsp.last / spy.last
    old = rsp.value_n_ago(n) / spy.value_n_ago(n)
    chg = (cur / old - 1.0) * 100
    score = piecewise(chg, [(-10, 92), (-6, 82), (-3, 66), (-1, 52),
                            (0, 45), (2, 30), (5, 18), (10, 12)])
    detail = (f"等权重/市值加权比价 3 个月变化 {chg:+.1f}%"
              f"（负 = 涨幅集中在少数巨头，结构脆弱）")
    return Factor("breadth", "市场宽度 (RSP/SPY)", w, score, detail,
                  {"rsp_spy_ratio": cur, "rsp_spy_chg_63d_pct": chg})


def f_commodity(d: Dict[str, Series], w: float) -> Factor:
    br = d.get("brent")
    if not br or not br.ok or len(br.points) < 70:
        return Factor("commodity", "能源冲击 (布伦特)", w, None, "油价数据缺失", {})
    cur = br.last
    old = br.value_n_ago(63)
    chg = (cur / old - 1.0) * 100 if old else 0.0
    score = piecewise(chg, [(-30, 15), (-15, 22), (0, 33), (10, 48),
                            (20, 65), (35, 84), (60, 96)])
    detail = (f"布伦特 ${cur:.1f}，3 个月变化 {chg:+.1f}%"
              f"（急涨 = 输入型通胀 → 央行被迫紧缩）")
    return Factor("commodity", "能源冲击 (布伦特)", w, score, detail,
                  {"brent": cur, "brent_chg_63d_pct": chg})


# 1950 年以来 S&P 500 各月平均收益（%），用于计算"未来 3 个月"的季节性逆风
_MONTHLY_AVG = {1: 1.07, 2: -0.01, 3: 1.13, 4: 1.46, 5: 0.30, 6: 0.11,
                7: 1.28, 8: -0.01, 9: -0.72, 10: 0.91, 11: 1.82, 12: 1.49}


def f_seasonality(today: date, w: float) -> Factor:
    fwd = [(today.month + k - 1) % 12 + 1 for k in (1, 2, 3)]
    exp_ret = sum(_MONTHLY_AVG[m] for m in fwd)
    score = piecewise(exp_ret, [(-1.0, 78), (0.0, 66), (1.0, 55), (2.0, 45),
                                (3.5, 33), (5.0, 22)])
    # 总统周期：中期选举年（美国总统任期第 2 年）4-10 月额外加权
    midterm = (today.year % 4) == 2
    bump = 0.0
    if midterm and 4 <= today.month <= 10:
        bump = 12.0
        score = min(100.0, score + bump)
    names = {1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月", 7: "7月",
             8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月"}
    detail = (f"未来 3 个月为 {'/'.join(names[m] for m in fwd)}，"
              f"1950 年以来该组合历史合计平均 {exp_ret:+.2f}%"
              + (f"；且处于中期选举年 4-10 月的历史弱势窗口（+{bump:.0f} 分）" if bump else ""))
    return Factor("seasonality", "季节性 / 总统周期", w, score, detail,
                  {"fwd3m_hist_avg_pct": exp_ret, "midterm_year": 1.0 if midterm else 0.0})


# ==========================================================================
# 汇总
# ==========================================================================
# ==========================================================================
# 结构脆弱度 vs 触发压力
# --------------------------------------------------------------------------
# 这是整个模型最重要的一个设计。单一的"熊市概率"会骗人：市场创新高、
# VIX 15、信用利差极窄的时候，任何以趋势为主的模型都会给出很低的读数 ——
# 它永远不会喊顶。这不是 bug，是这类模型的本质：它测的是"点火"，不是"火药"。
#
# 所以这里把因子分成两类，分别汇总：
#   结构脆弱度 (fragility)：估值、曲线、政策、宽度 —— 慢变量，决定"跌下去会
#       跌多深、能不能跌成熊市"。可以在高位停留数年，没有择时价值。
#   触发压力 (trigger)：趋势、信用、波动、就业、大宗、季节性 —— 快变量，
#       决定"什么时候开始跌"。
#
# 真正危险的配置是 高脆弱度 + 触发压力开始上升。单看任何一个都会误判。
# ==========================================================================
STRUCTURAL_KEYS = {"valuation", "curve", "policy", "breadth"}
TRIGGER_KEYS = {"trend", "credit", "volatility", "labor", "commodity", "seasonality"}


def _subscore(factors: List["Factor"], keys: set) -> Optional[float]:
    sel = [f for f in factors if f.key in keys and f.available]
    if not sel:
        return None
    tw = sum(f.weight for f in sel)
    return sum(f.score * f.weight for f in sel) / tw


def level_word(x: Optional[float]) -> str:
    if x is None:
        return "未知"
    if x < 30:
        return "低"
    if x < 45:
        return "偏低"
    if x < 58:
        return "中等"
    if x < 70:
        return "偏高"
    if x < 82:
        return "高"
    return "极高"


@dataclass
class Assessment:
    as_of: str
    score: float
    probability: float
    factors: List[Factor]
    missing: List[str]
    notes: List[str]
    regime: str = "normal"        # normal / pullback / correction / bear
    drawdown: Optional[float] = None
    fragility: Optional[float] = None   # 结构脆弱度 0-100（慢变量）
    trigger: Optional[float] = None     # 触发压力 0-100（快变量）

    @property
    def configuration(self) -> str:
        """把 (脆弱度, 触发压力) 两维压成一句人能记住的话。"""
        fr, tg = self.fragility, self.trigger
        if fr is None or tg is None:
            return "数据不足以判断结构"
        if fr >= 62 and tg >= 55:
            return "⚠️ 最危险的配置：地基本来就脆，而且已经开始晃 —— 历史上熊市正是从这里开始的"
        if fr >= 62 and tg >= 42:
            return "⚠️ 需要警惕：结构脆弱，触发压力正在积累"
        if fr >= 62:
            return "火药很多，但还没有火星 —— 结构脆弱，但暂时没有下跌动能。这种状态可以持续数年，不构成卖出理由，构成的是「别加杠杆」的理由"
        if tg >= 55:
            return "有下跌动能，但结构不算脆弱 —— 更像一次普通回调而不是熊市起点"
        return "结构和动能都健康"

    @property
    def probability_label(self) -> str:
        """已经在熊市里的时候，"进入熊市的概率"这个问法本身就失效了，换个说法。"""
        if self.regime == "bear":
            return "熊市延续 / 进一步下探的风险"
        return "未来 3 个月进入熊市 (S&P 500 自高点 -20%) 的概率"

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "score": round(self.score, 2),
            "probability": round(self.probability, 4),
            "fragility": round(self.fragility, 2) if self.fragility is not None else None,
            "trigger": round(self.trigger, 2) if self.trigger is not None else None,
            "configuration": self.configuration,
            "regime": self.regime,
            "drawdown": round(self.drawdown, 4) if self.drawdown is not None else None,
            "probability_label": self.probability_label,
            "factors": [asdict(f) for f in self.factors],
            "missing": self.missing,
            "notes": self.notes,
        }


REGIME_TEXT = {
    "normal": "正常（距高点回撤 <5%）",
    "pullback": "小幅回调（回撤 5-10%）",
    "correction": "技术性调整（回撤 10-20%）",
    "bear": "已确认熊市（回撤 ≥20%）",
}


def classify_regime(dd: Optional[float], cfg: dict) -> str:
    if dd is None:
        return "normal"
    d = -dd
    if d >= cfg["meta"].get("bear_drawdown_threshold", 0.20):
        return "bear"
    if d >= 0.10:
        return "correction"
    if d >= 0.05:
        return "pullback"
    return "normal"


def score_to_probability(score: float, cfg: dict) -> float:
    pm = cfg["probability_map"]
    logit = pm["slope"] * (score - 50.0) + pm["intercept"]
    p = 1.0 / (1.0 + math.exp(-logit))
    return max(pm["floor"], min(pm["ceiling"], p))


def assess(data: Dict[str, Series], cfg: dict, today: Optional[date] = None) -> Assessment:
    today = today or date.today()
    W = cfg["weights"]
    factors = [
        f_valuation(data, W["valuation"]),
        f_trend(data, W["trend"]),
        f_credit(data, W["credit"]),
        f_curve(data, W["curve"]),
        f_volatility(data, W["volatility"]),
        f_labor(data, W["labor"]),
        f_policy(data, W["policy"]),
        f_breadth(data, W["breadth"]),
        f_commodity(data, W["commodity"]),
        f_seasonality(today, W["seasonality"]),
    ]
    avail = [f for f in factors if f.available]
    missing = [f.label for f in factors if not f.available]

    # 权重重分配：缺失因子的权重按比例分给可用因子，而不是当 0 分
    tw = sum(f.weight for f in avail)
    min_w = float(cfg.get("min_available_weight", 45))
    if tw < min_w:
        raise RuntimeError(
            f"可用因子权重仅 {tw:.0f}（低于下限 {min_w:.0f}）——"
            f" 数据抓取失败过多，本次拒绝产出结论。缺失：{missing}"
        )
    score = sum(f.score * f.weight for f in avail) / tw
    prob = score_to_probability(score, cfg)

    notes: List[str] = []
    stale = [k for k, s in data.items() if s.stale]
    if stale:
        notes.append(f"以下数据源本次抓取失败，使用了缓存（可能陈旧）：{', '.join(sorted(stale))}")
    if missing:
        lost = 100.0 - tw
        notes.append(f"缺失因子占原权重 {lost:.0f}%，已按比例重分配给其余因子")

    dd = None
    for f in factors:
        if f.key == "trend" and f.values.get("drawdown_from_52w_high") is not None:
            dd = f.values["drawdown_from_52w_high"]
    regime = classify_regime(dd, cfg)
    if regime == "bear":
        notes.append(
            "S&P 500 距 52 周高点回撤已达 20%，按定义已经处在熊市中。"
            "上面的概率数字请理解为「熊市延续/继续下探」的风险，而不是「是否会开始」。"
        )
    fragility = _subscore(factors, STRUCTURAL_KEYS)
    trigger = _subscore(factors, TRIGGER_KEYS)
    if fragility is not None and trigger is not None and fragility - trigger > 25:
        notes.append(
            "结构脆弱度远高于触发压力。这是趋势类模型的已知盲区：市场在高位时"
            "总分会被「趋势良好」压低，模型永远不会提前喊顶。总概率读数偏低时，"
            "请同时看结构脆弱度 —— 它说明的是「一旦开跌会有多糟」。"
        )

    return Assessment(as_of=today.isoformat(), score=score, probability=prob,
                      factors=factors, missing=missing, notes=notes,
                      regime=regime, drawdown=dd,
                      fragility=fragility, trigger=trigger)


# ==========================================================================
# 告警判定
# ==========================================================================
def evaluate_alerts(a: Assessment, data: Dict[str, Series], prev: Optional[dict],
                    cfg: dict) -> List[dict]:
    """返回本次触发的告警列表。prev = 上一次运行的 latest.json（可为 None）。"""
    fired: List[dict] = []
    fv: Dict[str, Optional[float]] = {}
    for f in a.factors:
        fv.update(f.values)

    prev_p = (prev or {}).get("probability")

    for rule in cfg["alerts"]["rules"]:
        t = rule["type"]
        hit = False
        actual = None
        if t == "prob_delta_pp" and prev_p is not None:
            actual = (a.probability - prev_p) * 100
            hit = actual >= rule["threshold"]
        elif t == "prob_level":
            actual = a.probability
            was_below = prev_p is None or prev_p < rule["threshold"]
            hit = a.probability >= rule["threshold"] and was_below
        elif t == "drawdown":
            dd = fv.get("drawdown_from_52w_high")
            if dd is not None:
                actual = -dd
                hit = -dd >= rule["threshold"]
        elif t == "vix_level":
            v = fv.get("vix")
            if v is not None:
                actual = v
                hit = v >= rule["threshold"]
        elif t == "hy_oas_delta_bp":
            dbp = fv.get("hy_oas_delta_60d_bp")
            if dbp is not None:
                actual = dbp
                hit = dbp >= rule["threshold"]
        elif t == "fragile_and_firing":
            fr, tg = a.fragility, a.trigger
            if fr is not None and tg is not None:
                actual = tg
                hit = fr >= rule.get("fragility_min", 62) and tg >= rule.get("trigger_min", 55)
        elif t == "death_cross":
            dc = fv.get("death_cross")
            prev_dc = ((prev or {}).get("flat_values") or {}).get("death_cross")
            actual = dc
            hit = bool(dc) and not bool(prev_dc)   # 只在首次出现时告警
        if hit:
            fired.append({"id": rule["id"], "desc": rule["desc"],
                          "actual": actual, "threshold": rule.get("threshold")})
    return fired


def flat_values(a: Assessment) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {}
    for f in a.factors:
        out.update(f.values)
    return out
