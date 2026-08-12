#!/usr/bin/env python3
"""
离线自测：用合成数据驱动整条链路，验证
  1. 打分与概率映射在各种市况下的行为是否合理（单调、不越界）
  2. 因子缺失时的权重重分配
  3. 告警规则的触发与去重
  4. Telegram 消息渲染 + 4096 分片 + HTML 转义
  5. REPORT.md 渲染不报错

不需要网络。运行：python tests/test_offline.py
"""

from __future__ import annotations

import math
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml

from bearwatch import model, notify, report
from bearwatch.sources import Series

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def mkseries(name, values, freq_days=1, end=None):
    end = end or date.today()
    pts = [(end - timedelta(days=freq_days * (len(values) - 1 - i)), float(v))
           for i, v in enumerate(values)]
    return Series(name=name, points=pts)


def cape_history(current: float):
    """
    构造一条形状接近真实的 CAPE 历史（1950 年以来约 900 个月），
    末尾接上 current。用常数序列做测试会让百分位恒等于 50，
    等于根本没测到估值因子。
    """
    buckets = [(7, 12, 90), (12, 17, 300), (17, 22, 240), (22, 27, 140),
               (27, 32, 80), (32, 38, 35), (38, 45, 15)]
    vals = []
    for lo, hi, cnt in buckets:
        for i in range(cnt):
            vals.append(lo + (hi - lo) * (i + 0.5) / cnt)
    vals.sort()
    vals.append(current)
    return vals


def scenario(kind: str):
    """构造三种市况的合成数据：calm / stress / crisis。"""
    n = 600
    if kind == "calm":
        spx = [3000 * (1 + 0.0004) ** i for i in range(n)]          # 稳步上涨
        vix = [14.0] * n
        oas = [3.0] * n
        curve = [1.2] * 800
        un = [4.0] * 40
        pay = [150000 + 150 * i for i in range(40)]
        cape = cape_history(30.0)
        rsp = [100 * (1 + 0.00040) ** i for i in range(n)]
        spy = [100 * (1 + 0.00040) ** i for i in range(n)]
        brent = [80.0] * n
        ff = [2.0] * 400
        core = [300 * (1.002) ** i for i in range(40)]               # ~2.4% yoy
    elif kind == "stress":
        spx = [3000 * (1 + 0.0004) ** i for i in range(n - 60)]
        spx += [spx[-1] * (1 - 0.0022) ** i for i in range(1, 61)]   # 近 3 个月回撤约 12%
        vix = [15.0] * (n - 40) + [15 + 0.35 * i for i in range(40)]
        oas = [3.2] * (n - 60) + [3.2 + 0.022 * i for i in range(60)]
        curve = [1.0] * 500 + [-0.6] * 200 + [0.3] * 100
        un = [3.9] * 30 + [4.0, 4.1, 4.2, 4.3, 4.3, 4.4, 4.4, 4.5, 4.5, 4.6]
        pay = [150000 + 120 * i for i in range(34)] + [154100, 154120, 154125, 154120, 154110, 154095]
        cape = cape_history(39.0)
        rsp = [100 * (1 + 0.00040) ** i for i in range(n - 63)] + \
              [100 * (1 + 0.00040) ** (n - 63) * (1 - 0.0013) ** i for i in range(1, 64)]
        spy = [100 * (1 + 0.00040) ** i for i in range(n)]
        brent = [78.0] * (n - 63) + [78 * (1 + 0.0042) ** i for i in range(1, 64)]
        ff = [4.5] * 400
        core = [300 * (1.0031) ** i for i in range(40)]              # ~3.8% yoy
    else:  # crisis
        spx = [3000 * (1 + 0.0004) ** i for i in range(n - 120)]
        spx += [spx[-1] * (1 - 0.0028) ** i for i in range(1, 121)]  # 回撤约 28%
        vix = [16.0] * (n - 30) + [16 + 0.9 * i for i in range(30)]
        oas = [3.5] * (n - 60) + [3.5 + 0.06 * i for i in range(60)]
        curve = [1.0] * 500 + [-1.0] * 250 + [0.2] * 50
        un = [3.8] * 26 + [4.0, 4.2, 4.5, 4.8, 5.1, 5.4, 5.7, 6.0, 6.2, 6.4, 6.5, 6.6, 6.7, 6.8]
        pay = [150000 + 120 * i for i in range(34)] + [154000, 153700, 153200, 152500, 151700, 150900]
        cape = cape_history(41.0)
        rsp = [100 * (1 + 0.00040) ** i for i in range(n - 63)] + \
              [100 * (1 + 0.00040) ** (n - 63) * (1 - 0.0022) ** i for i in range(1, 64)]
        spy = [100 * (1 + 0.00040) ** i for i in range(n)]
        brent = [70.0] * (n - 63) + [70 * (1 + 0.0060) ** i for i in range(1, 64)]
        ff = [5.5] * 400
        core = [300 * (1.0040) ** i for i in range(40)]              # ~4.9% yoy

    return {
        "spx": mkseries("spx", spx),
        "vix": mkseries("vix", vix),
        "hy_oas": mkseries("hy_oas", oas),
        "t10y3m": mkseries("t10y3m", curve),
        "unrate": mkseries("unrate", un, freq_days=30),
        "payems": mkseries("payems", pay, freq_days=30),
        "cape": mkseries("cape", cape, freq_days=30),
        "rsp": mkseries("rsp", rsp),
        "spy": mkseries("spy", spy),
        "brent": mkseries("brent", brent),
        "fedfunds": mkseries("fedfunds", ff),
        "corecpi": mkseries("corecpi", core, freq_days=30),
        "ndx": mkseries("ndx", spx),
        "qqq": mkseries("qqq", spy),
        "t10y2y": mkseries("t10y2y", curve),
        "dgs10": mkseries("dgs10", [4.5] * 400),
        "cpi": mkseries("cpi", core, freq_days=30),
    }


print("\n=== 1. 三种市况下的打分单调性 ===")
results = {}
for kind in ("calm", "stress", "crisis"):
    d = scenario(kind)
    a = model.assess(d, CFG, today=date(2026, 8, 10))
    results[kind] = a
    print(f"  [{kind:7s}] 总分 {a.score:5.1f}  概率 {a.probability*100:5.1f}%  缺失={a.missing}")
    for f in sorted(a.factors, key=lambda x: -(x.score or -1)):
        print(f"            {f.label:<22} {('%.0f' % f.score) if f.available else 'N/A':>4}  {f.detail[:78]}")

check("calm < stress < crisis（总分单调）",
      results["calm"].score < results["stress"].score < results["crisis"].score,
      f"{results['calm'].score:.1f} / {results['stress'].score:.1f} / {results['crisis'].score:.1f}")
check("calm 概率 < 20%", results["calm"].probability < 0.20,
      f"{results['calm'].probability:.3f}")
check("crisis 概率 > 40%", results["crisis"].probability > 0.40,
      f"{results['crisis'].probability:.3f}")
check("所有概率在 [floor, ceiling] 内",
      all(CFG["probability_map"]["floor"] <= r.probability <= CFG["probability_map"]["ceiling"]
          for r in results.values()))
check("所有因子分在 [0,100]",
      all(0 <= f.score <= 100 for r in results.values() for f in r.factors if f.available))
check("calm 判定为 normal", results["calm"].regime == "normal", results["calm"].regime)
check("stress 判定为 correction", results["stress"].regime == "correction", results["stress"].regime)
check("crisis 判定为 bear", results["crisis"].regime == "bear", results["crisis"].regime)
check("bear 状态下概率标签被改写",
      "延续" in results["crisis"].probability_label, results["crisis"].probability_label)
check("bear 状态下 notes 有说明",
      any("已经处在熊市" in n for n in results["crisis"].notes))

print("\n=== 1b. 结构脆弱度 / 触发压力 双维度 ===")
for k, r in results.items():
    print(f"  [{k:7s}] 脆弱度 {r.fragility:5.1f}  触发压力 {r.trigger:5.1f}  → {r.configuration[:60]}")
check("三种市况的触发压力单调递增",
      results["calm"].trigger < results["stress"].trigger < results["crisis"].trigger)
check("脆弱度与触发压力都在 [0,100]",
      all(0 <= r.fragility <= 100 and 0 <= r.trigger <= 100 for r in results.values()))
check("crisis 判定为最危险配置", "最危险" in results["crisis"].configuration,
      results["crisis"].configuration[:40])
check("configuration 永不为空", all(r.configuration for r in results.values()))

print("\n=== 2. 概率映射的校准锚点 ===")
for s, expect in ((50, 0.08), (75, 0.25), (90, 0.45)):
    p = model.score_to_probability(s, CFG)
    check(f"score {s} → {expect*100:.0f}% (实际 {p*100:.1f}%)", abs(p - expect) < 0.015)
check("映射单调不减（含 floor/ceiling 截断）",
      all(model.score_to_probability(s, CFG) <= model.score_to_probability(s + 1, CFG) + 1e-12
          for s in range(0, 100)))
_unclamped = [s for s in range(0, 100)
              if CFG["probability_map"]["floor"] < model.score_to_probability(s, CFG)
              < CFG["probability_map"]["ceiling"]]
check("未截断区间内严格单调递增",
      all(model.score_to_probability(s, CFG) < model.score_to_probability(s + 1, CFG)
          for s in _unclamped[:-1]),
      f"未截断区间 score {_unclamped[0]}~{_unclamped[-1]}")

print("\n=== 3. 因子缺失时的权重重分配 ===")
d = scenario("stress")
full = model.assess(d, CFG, today=date(2026, 8, 10))
for k in ("cape", "hy_oas", "rsp", "brent"):
    d.pop(k, None)
partial = model.assess(d, CFG, today=date(2026, 8, 10))
check("缺 4 个因子仍能出结果", partial.score > 0)
check("缺失清单被记录", len(partial.missing) >= 3, str(partial.missing))
check("notes 里提示了权重重分配",
      any("重分配" in n for n in partial.notes), str(partial.notes))
print(f"  完整={full.score:.1f}  缺4项={partial.score:.1f}  缺失={partial.missing}")

# 数据大面积缺失时必须拒绝出结论，而不是拿季节性那 3 分权重硬凑一个概率
d2 = {k: Series(name=k, error="simulated outage") for k in ("spx", "vix", "hy_oas")}
try:
    bad = model.assess(d2, CFG, today=date(2026, 8, 10))
    check("数据大面积缺失时拒绝出结论", False, f"却给出了 score={bad.score:.1f}")
except RuntimeError as e:
    check("数据大面积缺失时拒绝出结论（抛 RuntimeError）", True)
    print(f"     └─ {e}")
except Exception as e:
    check("应抛 RuntimeError 而非其它异常", False, repr(e))

# 只缺少数几个因子时必须仍然能出结论
d3 = scenario("calm")
for k in ("cape", "brent"):
    d3.pop(k, None)
ok3 = model.assess(d3, CFG, today=date(2026, 8, 10))
check("只缺 2 个次要因子时仍正常出结论", ok3.score > 0 and len(ok3.missing) == 2)

print("\n=== 4. 告警规则 ===")
crisis = results["crisis"]
prev_low = {"probability": 0.08, "score": 45.0, "flat_values": {"death_cross": 0.0}}
fired = model.evaluate_alerts(crisis, scenario("crisis"), prev_low, CFG)
ids = [f["id"] for f in fired]
print(f"  crisis 触发：{ids}")
check("crisis 触发 prob_jump", "prob_jump" in ids)
check("crisis 触发 drawdown_10", "drawdown_10" in ids)
check("crisis 触发 vix_30", "vix_30" in ids)
check("crisis 触发 death_cross", "death_cross" in ids)

calm = results["calm"]
prev_calm = {"probability": calm.probability, "score": calm.score,
             "flat_values": {"death_cross": 0.0}}
fired_calm = model.evaluate_alerts(calm, scenario("calm"), prev_calm, CFG)
check("calm 不触发任何告警", len(fired_calm) == 0, str([f['id'] for f in fired_calm]))

# death_cross 去重：上次已经是死叉时不应再报
prev_dc = {"probability": 0.30, "score": 70.0, "flat_values": {"death_cross": 1.0}}
fired_dc = model.evaluate_alerts(crisis, scenario("crisis"), prev_dc, CFG)
check("死叉已存在时不重复告警", "death_cross" not in [f["id"] for f in fired_dc])

print("\n=== 5. Telegram 消息渲染 ===")
# 用 stress 场景自身触发的告警，保证消息里的告警与数字自洽
stress_fired = model.evaluate_alerts(results["stress"], scenario("stress"),
                                     {"probability": 0.12, "score": 55.0,
                                      "flat_values": {"death_cross": 0.0}}, CFG)
rec = report.build_record(results["stress"], stress_fired, "DeepSeek 测试文本")
prev_rec = {"probability": 0.12, "score": 55.0}
ds = "【本周变化】测试 <b>不该被解析的标签</b> & 特殊字符 < > &\n【驱动因素】略\n【结论】略"
cc = "Claude 段测试文本。" * 40
msg = notify.build_weekly_message(rec, prev_rec, ds, cc,
                                  "https://github.com/u/bear-watch/blob/main/REPORT.md")
check("消息非空", len(msg) > 200)
check("含 DS 段", "<b>DS：</b>" in msg)
check("含 CC 段", "<b>CC：</b>" in msg)
check("用户文本里的尖括号被转义", "&lt;b&gt;" in msg)
check("结构标签未被转义", msg.count("<b>") > 3)

long_text = "很长的一段中文测试。" * 900
parts = notify._split(long_text)
check("超长文本被正确分片", len(parts) > 1 and all(len(p) <= notify.TG_LIMIT for p in parts),
      f"分 {len(parts)} 片，最长 {max(len(p) for p in parts)}")
check("分片后内容无丢失",
      len("".join(parts).replace("\n", "")) >= len(long_text.replace("\n", "")) - len(parts))

alert_msg = notify.build_alert_message(rec, stress_fired[:3] or [{"id":"x","desc":"测试告警","actual":1.0}], "https://example.com/r")
check("告警消息渲染正常", "即时告警" in alert_msg and len(alert_msg) > 150)
print("\n----- 周报消息预览（前 1400 字符）-----")
print(msg[:1400])
print("----- 预览结束 -----")

print("\n=== 6. REPORT.md 渲染 ===")
md = report.render_markdown(results["stress"], stress_fired, ds, cc, CFG)
check("Markdown 非空且含关键小节",
      all(s in md for s in ("## 一、结论", "## 二、因子明细", "## 五、DS", "## 六、CC",
                            "给 Claude（CC）的指令")))
check("Markdown 表格行数合理", md.count("\n|") >= 10)
check("原始数值 JSON 块存在", "```json" in md)

print("\n=== 7. 权重与配置自洽 ===")
check("weights 之和 = 100", abs(sum(CFG["weights"].values()) - 100) < 1e-9,
      str(sum(CFG["weights"].values())))
check("每个权重都有对应因子实现",
      set(CFG["weights"]) == {f.key for f in results["calm"].factors},
      str(set(CFG["weights"]) ^ {f.key for f in results["calm"].factors}))
check("告警规则 id 唯一",
      len({r["id"] for r in CFG["alerts"]["rules"]}) == len(CFG["alerts"]["rules"]))

print("\n=== 8. 季节性因子的月份行为 ===")
seasonal = {}
for m in range(1, 13):
    f = model.f_seasonality(date(2027, m, 15), 3)   # 2027 非中期选举年
    seasonal[m] = f.score
print("  " + "  ".join(f"{m}月:{seasonal[m]:.0f}" for m in range(1, 13)))
check("6-8 月（未来3月含9/10月）风险分高于 10-12 月",
      min(seasonal[6], seasonal[7]) > max(seasonal[10], seasonal[11]),
      str(seasonal))
mid = model.f_seasonality(date(2026, 8, 15), 3).score
non = model.f_seasonality(date(2027, 8, 15), 3).score
check("中期选举年 8 月分数高于非中期年", mid > non, f"{mid} vs {non}")

print(f"\n{'='*54}")
print(f"  通过 {PASS} 项，失败 {FAIL} 项")
print(f"{'='*54}\n")
sys.exit(1 if FAIL else 0)
