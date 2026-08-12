"""生成 REPORT.md / latest.json / history.jsonl。

REPORT.md 有两个读者：
  1. 你（人）—— 所以要有表格和人话。
  2. Claude（我）—— 周日的 Cowork 定时任务会 WebFetch 这个文件的 raw 链接，
     所以文件里要包含足够的原始数字和历史序列，让我能独立判断，而不是
     只能复述 DeepSeek 的结论。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .model import Assessment, flat_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HISTORY = os.path.join(DATA, "history.jsonl")      # 只记录周报，用于计算环比
DAILY = os.path.join(DATA, "daily.jsonl")          # 每日静默检查的记录
LATEST = os.path.join(DATA, "latest.json")         # 最近一次周报
ALERT_STATE = os.path.join(DATA, "alert_state.json")
REPORT = os.path.join(ROOT, "REPORT.md")


def _bar(score: float, width: int = 20) -> str:
    n = int(round(score / 100 * width))
    return "█" * n + "·" * (width - n)


def _risk_word(p: float) -> str:
    if p < 0.10:
        return "低"
    if p < 0.20:
        return "偏低"
    if p < 0.32:
        return "中等偏高"
    if p < 0.45:
        return "高"
    return "很高"


def load_history(limit: Optional[int] = None) -> List[dict]:
    if not os.path.exists(HISTORY):
        return []
    rows = []
    with open(HISTORY, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:] if limit else rows


def load_latest() -> Optional[dict]:
    if not os.path.exists(LATEST):
        return None
    try:
        with open(LATEST, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def build_record(a: Assessment, alerts: List[dict], ds_brief: str = "") -> dict:
    rec = a.to_dict()
    rec["generated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["alerts"] = alerts
    rec["flat_values"] = flat_values(a)
    rec["ds_brief"] = ds_brief
    return rec


def persist(a: Assessment, alerts: List[dict], ds_brief: str = "",
            kind: str = "weekly") -> dict:
    """kind='weekly' 写 history.jsonl + latest.json；kind='daily' 只写 daily.jsonl。"""
    os.makedirs(DATA, exist_ok=True)
    rec = build_record(a, alerts, ds_brief)
    rec["kind"] = kind
    target = HISTORY if kind == "weekly" else DAILY
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if kind == "weekly":
        with open(LATEST, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    return rec


def load_alert_state() -> dict:
    if not os.path.exists(ALERT_STATE):
        return {}
    try:
        with open(ALERT_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_alert_state(state: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(ALERT_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def last_daily() -> Optional[dict]:
    if not os.path.exists(DAILY):
        return None
    last = None
    with open(DAILY, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


def _trend_table(hist: List[dict], n: int = 12) -> str:
    rows = hist[-n:]
    if not rows:
        return "_（暂无历史）_"
    out = ["| 日期 | 总分 | 熊市概率 | 环比 |", "|---|---|---|---|"]
    prev = None
    for r in rows:
        p = r.get("probability", 0) * 100
        delta = f"{p - prev:+.1f}pp" if prev is not None else "—"
        out.append(f"| {r.get('as_of','?')} | {r.get('score',0):.1f} | {p:.1f}% | {delta} |")
        prev = p
    return "\n".join(out)


def _sparkline(hist: List[dict], n: int = 26) -> str:
    rows = hist[-n:]
    if len(rows) < 2:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    ps = [r.get("probability", 0) for r in rows]
    lo, hi = min(ps), max(ps)
    if hi - lo < 1e-9:
        return blocks[0] * len(ps)
    return "".join(blocks[min(7, int((p - lo) / (hi - lo) * 7.999))] for p in ps)


def render_markdown(a: Assessment, alerts: List[dict], ds_brief: str,
                    cc_brief: str, cfg: dict) -> str:
    hist = load_history()
    p = a.probability
    prev = hist[-2] if len(hist) >= 2 else None
    delta_txt = ""
    if prev:
        dpp = (p - prev.get("probability", p)) * 100
        delta_txt = f"（较上次 {dpp:+.1f} 个百分点）"

    lines: List[str] = []
    A = lines.append

    A("# 🐻 Bear Watch — 美股熊市监控报告")
    A("")
    A(f"> **数据截止：{a.as_of}** ｜ 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    A("")
    A("## 一、结论")
    A("")
    from .model import REGIME_TEXT
    if a.regime == "bear":
        A("> 🐻 **S&P 500 距 52 周高点回撤已达 20%，按定义已经处在熊市中。**")
        A("> 下面的概率是「熊市延续/继续下探」的风险，不是「是否会开始」。")
        A("")
    A(f"### {a.probability_label}：**{p*100:.1f}%** {delta_txt}")
    A("")
    A(f"- 综合风险分：**{a.score:.1f} / 100**  `{_bar(a.score)}`")
    A(f"- 风险等级：**{_risk_word(p)}**")
    A(f"- 当前市场状态：**{REGIME_TEXT.get(a.regime, a.regime)}**"
      + (f"（距 52 周高点 {a.drawdown*100:+.1f}%）" if a.drawdown is not None else ""))
    A(f"- 无条件基准概率（1950 年以来任意季度）：约 8%")
    A("")
    A("### 两个维度分开看（比单一概率更有信息量）")
    A("")
    from .model import level_word
    A("| 维度 | 分数 | 水平 | 它回答的问题 |")
    A("|---|---:|---|---|")
    A(f"| **结构脆弱度** | {a.fragility:.1f} | **{level_word(a.fragility)}** | "
      f"一旦开跌，会不会跌成熊市、跌多深？（估值/曲线/政策/宽度）|"
      if a.fragility is not None else "| 结构脆弱度 | — | 数据不足 | |")
    A(f"| **触发压力** | {a.trigger:.1f} | **{level_word(a.trigger)}** | "
      f"现在有没有真正的下跌动能？（趋势/信用/波动/就业/大宗/季节性）|"
      if a.trigger is not None else "| 触发压力 | — | 数据不足 | |")
    A("")
    A(f"**当前配置：** {a.configuration}")
    sp = _sparkline(hist)
    if sp:
        A(f"- 概率走势（最近 {min(26, len(hist))} 次）：`{sp}`")
    A("")

    if alerts:
        A("### 🚨 本次触发的告警")
        A("")
        for al in alerts:
            act = al.get("actual")
            act_s = f"（当前 {act:.2f}）" if isinstance(act, (int, float)) else ""
            A(f"- **{al['desc']}**{act_s}")
        A("")
    else:
        A("_本次无告警触发。_")
        A("")

    A("## 二、因子明细")
    A("")
    A("| 因子 | 权重 | 风险分 | 图示 | 说明 |")
    A("|---|---:|---:|---|---|")
    for f in sorted(a.factors, key=lambda x: -(x.score or -1)):
        if f.available:
            A(f"| {f.label} | {f.weight:.0f} | **{f.score:.0f}** | `{_bar(f.score, 10)}` | {f.detail} |")
        else:
            A(f"| {f.label} | {f.weight:.0f} | — | `数据缺失` | {f.detail} |")
    A("")
    if a.notes:
        for n in a.notes:
            A(f"> ⚠️ {n}")
        A("")

    A("## 三、原始数值（供独立复核）")
    A("")
    A("```json")
    A(json.dumps(flat_values(a), ensure_ascii=False, indent=2, default=str))
    A("```")
    A("")

    A("## 四、历史轨迹")
    A("")
    A(_trend_table(hist))
    A("")

    A("## 五、DS：DeepSeek 解读")
    A("")
    A(ds_brief.strip() if ds_brief.strip() else "_（本次未生成）_")
    A("")

    A("## 六、CC：Claude 解读")
    A("")
    A(cc_brief.strip() if cc_brief.strip() else
      "_（等待 Cowork 定时任务写入 `cc_brief.md`）_")
    A("")

    A("---")
    A("")
    A("## 七、模型说明（给人和给 Claude 看的）")
    A("")
    A("**概率是怎么算出来的：** 10 个因子各自映射到 0-100 的风险分，按权重加权平均得到总分，")
    A("再用 `logit(p) = 0.056 × (总分 − 50) − 2.442` 映射成概率。校准锚点：总分 50 → 8%（约等于")
    A("1950 年以来任意一个季度开启 -20% 熊市的无条件基准概率），总分 75 → 25%，总分 90 → 45%。")
    A("")
    A("**权重设计的取舍：** 估值只有 12 分权重，趋势 18 + 信用 16 有 34 分。这是刻意的 ——")
    A("估值告诉你「跌下来会跌多深」，趋势和信用才告诉你「什么时候跌」。CAPE 在 40 以上待了")
    A("好几年这件事本身说明估值不是择时工具。")
    A("")
    A("**这个模型不知道什么：** 它看不见新闻、地缘政治、政策突变、单一公司事件（比如某家云厂商")
    A("砍 AI 资本开支指引）。这些恰恰是 DS 段和 CC 段要补的。所以两段解读不是装饰，是模型的")
    A("盲区补丁。")
    A("")
    A("**给 Claude（CC）的指令：** 读完上面第二、三、四节后，请独立判断 ——")
    A("(1) 规则模型的分数有没有明显失真的因子？(2) 有没有模型看不见但当周确实重要的事件？")
    A("(3) 你自己对未来 3 个月熊市概率的独立估计是多少，与规则模型差在哪、为什么？")
    A("(4) 如果和上周相比有实质变化，明确指出是什么变了。不要复述表格，只写模型漏掉的东西。")
    A("")
    return "\n".join(lines)


def write_report(md: str) -> str:
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(md)
    return REPORT
