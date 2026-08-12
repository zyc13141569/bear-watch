#!/usr/bin/env python3
"""把最近一次结果写进 GitHub Actions 的运行摘要页。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(ROOT, "data", sys.argv[1] if len(sys.argv) > 1 else "latest.json")
out = os.environ.get("GITHUB_STEP_SUMMARY")

if not os.path.exists(path):
    line = "### Bear Watch\n\n⚠️ 没有找到结果文件，本次可能采集失败。\n"
else:
    d = json.load(open(path, encoding="utf-8"))
    alerts = [a["id"] for a in d.get("alerts", [])] or ["无"]
    miss = d.get("missing") or ["无"]
    top = sorted([f for f in d["factors"] if f["score"] is not None],
                 key=lambda x: -x["score"])[:5]
    rows = "\n".join(f"| {f['label']} | {f['weight']:.0f} | {f['score']:.0f} | {f['detail']} |"
                     for f in top)
    line = (
        f"### 🐻 Bear Watch — {d['as_of']}\n\n"
        f"| 指标 | 值 |\n|---|---|\n"
        f"| 综合风险分 | **{d['score']:.1f} / 100** |\n"
        f"| 未来 3 个月熊市概率 | **{d['probability']*100:.1f}%** |\n"
        f"| 触发告警 | {', '.join(alerts)} |\n"
        f"| 缺失因子 | {', '.join(miss)} |\n\n"
        f"**风险最高的因子**\n\n"
        f"| 因子 | 权重 | 分数 | 说明 |\n|---|---:|---:|---|\n{rows}\n"
    )

print(line)
if out:
    with open(out, "a", encoding="utf-8") as f:
        f.write(line)
