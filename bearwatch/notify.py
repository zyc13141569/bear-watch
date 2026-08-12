"""Telegram 推送层。

Telegram 单条消息上限 4096 字符，这里统一走 HTML parse_mode（比 Markdown
容错高得多 —— Markdown 里一个未闭合的 * 就会让整条消息 400）。
"""

from __future__ import annotations

import html
import logging
import os
import time
from typing import List, Optional

import requests

log = logging.getLogger("bearwatch.notify")

TG_LIMIT = 4000          # 留 96 字符余量


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _split(text: str, limit: int = TG_LIMIT) -> List[str]:
    """按段落切分，尽量不切断句子。"""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > limit:
            if cur:
                chunks.append(cur.rstrip())
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            cur = para + "\n"
        else:
            cur += para + "\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


def send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None,
         disable_preview: bool = True) -> bool:
    token = (token or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = (chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not chat_id:
        log.error("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID，无法发送")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok_all = True
    parts = _split(text)
    for i, chunk in enumerate(parts):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        sent = False
        for attempt in range(1, 4):
            try:
                r = requests.post(url, json=payload, timeout=45)
                if r.status_code == 200 and r.json().get("ok"):
                    sent = True
                    break
                # HTML 解析失败时降级为纯文本再试一次
                if r.status_code == 400 and "parse" in r.text.lower():
                    payload.pop("parse_mode", None)
                    log.warning("HTML 解析失败，降级为纯文本重试")
                    continue
                log.warning("telegram %d/%d HTTP %s: %s", i + 1, len(parts),
                            r.status_code, r.text[:300])
            except Exception as exc:
                log.warning("telegram send error: %r", exc)
            time.sleep(3 * attempt)
        ok_all = ok_all and sent
        time.sleep(0.4)
    return ok_all


def build_weekly_message(rec: dict, prev: Optional[dict], ds: str, cc: str,
                         repo_url: str = "") -> str:
    p = rec["probability"] * 100
    score = rec["score"]
    dtxt = ""
    if prev:
        d = p - prev.get("probability", rec["probability"]) * 100
        arrow = "▲" if d > 0.05 else ("▼" if d < -0.05 else "＝")
        dtxt = f"  {arrow} {d:+.1f}pp"

    if p < 10:
        emoji, word = "🟢", "低"
    elif p < 20:
        emoji, word = "🟡", "偏低"
    elif p < 32:
        emoji, word = "🟠", "中等偏高"
    elif p < 45:
        emoji, word = "🔴", "高"
    else:
        emoji, word = "🚨", "很高"

    regime = rec.get("regime", "normal")
    regime_txt = {
        "normal": "", "pullback": "🟨 当前处于小幅回调（回撤 5-10%）",
        "correction": "🟧 当前处于技术性调整（回撤 10-20%）",
        "bear": "🐻 <b>S&amp;P 500 已确认进入熊市（回撤 ≥20%）</b>",
    }.get(regime, "")
    label = "熊市延续 / 继续下探的风险" if regime == "bear" else "未来 3 个月熊市概率"
    if regime == "bear":
        emoji = "🐻"

    L: List[str] = []
    L.append(f"<b>🐻 Bear Watch 周报 · {esc(rec['as_of'])}</b>")
    L.append("")
    if regime_txt:
        L.append(regime_txt)
    L.append(f"{emoji} <b>{label}：{p:.1f}%</b>{esc(dtxt)}")
    L.append(f"风险等级：<b>{word}</b>　综合分：{score:.1f}/100　（基准 8%）")
    L.append("")

    fr, tg = rec.get("fragility"), rec.get("trigger")
    if fr is not None and tg is not None:
        from .model import level_word
        L.append(f"🧱 结构脆弱度 <b>{fr:.0f}</b>（{level_word(fr)}）— 跌下去会有多糟")
        L.append(f"🔥 触发压力　 <b>{tg:.0f}</b>（{level_word(tg)}）— 现在有没有下跌动能")
        L.append(f"<i>{esc(rec.get('configuration',''))}</i>")
        L.append("")

    alerts = rec.get("alerts") or []
    if alerts:
        L.append("🚨 <b>触发告警</b>")
        for a in alerts:
            L.append(f"• {esc(a['desc'])}")
        L.append("")

    top = sorted([f for f in rec["factors"] if f["score"] is not None],
                 key=lambda x: -x["score"])[:4]
    low = sorted([f for f in rec["factors"] if f["score"] is not None],
                 key=lambda x: x["score"])[:2]
    L.append("<b>风险最高的因子</b>")
    for f in top:
        L.append(f"• {esc(f['label'])} <b>{f['score']:.0f}</b> — {esc(f['detail'])}")
    L.append("")
    # 只有分数真的低才叫"安全"；否则如实说这只是相对最低的
    head = "目前仍然安全的" if low and low[0]["score"] < 40 else "相对最不紧张的（注意：并不等于安全）"
    L.append(f"<b>{head}</b>")
    for f in low:
        L.append(f"• {esc(f['label'])} <b>{f['score']:.0f}</b> — {esc(f['detail'])}")
    L.append("")

    miss = [f["label"] for f in rec["factors"] if f["score"] is None]
    if miss:
        L.append(f"<i>⚠️ 数据缺失因子：{esc('、'.join(miss))}（权重已重分配）</i>")
        L.append("")

    L.append("━━━━━━━━━━━━━━")
    L.append("<b>DS：</b>")
    L.append(esc(ds.strip()) if ds.strip() else "<i>（本次未生成）</i>")
    L.append("")
    L.append("━━━━━━━━━━━━━━")
    L.append("<b>CC：</b>")
    L.append(esc(cc.strip()) if cc.strip() else "<i>（Claude 本次未写入 cc_brief.md）</i>")

    if repo_url:
        L.append("")
        L.append(f'📄 <a href="{esc(repo_url)}">完整报告 REPORT.md</a>')
    return "\n".join(L)


def build_alert_message(rec: dict, alerts: List[dict], repo_url: str = "") -> str:
    p = rec["probability"] * 100
    L: List[str] = []
    L.append("<b>🚨 Bear Watch 即时告警</b>")
    L.append(f"<i>{esc(rec['as_of'])}</i>")
    L.append("")
    for a in alerts:
        act = a.get("actual")
        act_s = f"（当前 {act:.2f}）" if isinstance(act, (int, float)) else ""
        L.append(f"⚠️ <b>{esc(a['desc'])}</b>{esc(act_s)}")
    L.append("")
    L.append(f"当前熊市概率：<b>{p:.1f}%</b>　综合分 {rec['score']:.1f}/100")
    L.append("")
    drivers = sorted([f for f in rec["factors"] if f["score"] is not None],
                     key=lambda x: -x["score"])[:3]
    L.append("<b>驱动因子</b>")
    for f in drivers:
        L.append(f"• {esc(f['label'])} <b>{f['score']:.0f}</b> — {esc(f['detail'])}")
    if repo_url:
        L.append("")
        L.append(f'📄 <a href="{esc(repo_url)}">完整报告</a>')
    L.append("")
    L.append("<i>这是阈值告警，不是周报。周报仍按周日正常发送。</i>")
    return "\n".join(L)
