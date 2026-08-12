"""DeepSeek 调用层。

只负责"把数字讲成人话"，不负责编造数字。系统提示里明确禁止 LLM 自己给出
一个与规则模型不同的概率 —— 它可以质疑规则模型，但必须说明理由，而不是
偷偷换一个数。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional

import requests

log = logging.getLogger("bearwatch.llm")

SYSTEM_PROMPT = """你是一名给专业投资者写晨报的宏观策略师。语气精准、直接、不打太极，
可以给出负面结论。禁止套话（"需要密切关注"、"投资有风险"这类一律不写）。

硬性规则：
1. 规则模型算出的熊市概率是给定的事实，你不能替换它。如果你认为它偏高或偏低，
   要明确说"我认为规则模型在 X 因子上高估/低估了，理由是……"，并给出你自己的
   调整幅度（例如"我会下调 5 个百分点"）。
2. 只使用提供给你的数字。禁止编造任何未在输入中出现的数据、日期、公司名或引用。
   如果某个因子数据缺失，就说它缺失。
3. 不要复述表格。只写：本周与上周相比什么变了、哪个因子在驱动分数、以及一个
   具体的可执行结论。
4. 用中文。总长度控制在 400-550 字之间，分 3 段：
   【本周变化】【驱动因素】【结论与操作含义】
"""


def build_user_prompt(rec: dict, prev: Optional[dict], alerts: List[dict]) -> str:
    parts = []
    parts.append(f"数据截止日期：{rec['as_of']}")
    parts.append(f"规则模型综合风险分：{rec['score']:.1f}/100")
    parts.append(f"规则模型给出的「未来3个月进入熊市(-20%)」概率：{rec['probability']*100:.1f}%")
    if prev:
        d = (rec["probability"] - prev.get("probability", rec["probability"])) * 100
        parts.append(f"上次概率：{prev.get('probability',0)*100:.1f}%（本次变化 {d:+.1f} 个百分点）")
        parts.append(f"上次总分：{prev.get('score',0):.1f}")
    parts.append("")
    parts.append("各因子（权重 / 风险分 0-100 / 说明）：")
    prev_scores = {f["key"]: f.get("score") for f in (prev or {}).get("factors", [])}
    for f in rec["factors"]:
        if f["score"] is None:
            parts.append(f"- {f['label']}（权重{f['weight']:.0f}）：数据缺失 —— {f['detail']}")
            continue
        ps = prev_scores.get(f["key"])
        chg = f"（上次 {ps:.0f}，变化 {f['score']-ps:+.0f}）" if isinstance(ps, (int, float)) else ""
        parts.append(f"- {f['label']}（权重{f['weight']:.0f}）：{f['score']:.0f} 分{chg} —— {f['detail']}")
    if alerts:
        parts.append("")
        parts.append("本次触发的告警：")
        for a in alerts:
            parts.append(f"- {a['desc']}（实际值 {a.get('actual')}）")
    if rec.get("notes"):
        parts.append("")
        parts.append("数据质量提示：" + "；".join(rec["notes"]))
    parts.append("")
    parts.append("请按系统提示的格式输出。")
    return "\n".join(parts)


def call_deepseek(prompt: str, cfg: dict, api_key: Optional[str] = None) -> str:
    key = (api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
    if not key:
        return "_（未配置 DEEPSEEK_API_KEY，本次跳过 DeepSeek 解读）_"
    c = cfg["llm"]["deepseek"]
    url = c["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": c["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": c.get("temperature", 0.3),
        "max_tokens": c.get("max_tokens", 1100),
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last = None
    for attempt in range(1, 4):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=c.get("timeout", 180))
            if r.status_code == 200:
                data = r.json()
                txt = data["choices"][0]["message"]["content"].strip()
                if txt:
                    return txt
                last = "empty content"
            else:
                last = f"HTTP {r.status_code}: {r.text[:300]}"
        except Exception as exc:
            last = repr(exc)
        log.warning("deepseek attempt %d failed: %s", attempt, last)
        time.sleep(4 * attempt)
    return f"_（DeepSeek 调用失败：{last}）_"
