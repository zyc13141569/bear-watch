#!/usr/bin/env python3
"""
周日采集入口。

做四件事：
  1. 抓数据 → 打分 → 算概率
  2. 调 DeepSeek 生成 DS 段
  3. 写 data/history.jsonl、data/latest.json、ds_brief.md
  4. 渲染 REPORT.md（CC 段用仓库里现有的 cc_brief.md，可能是上周的）

不发 Telegram —— 发送由 run_notify.py 负责，等 Claude 写完 cc_brief.md 之后触发。
用 --send-now 可以跳过等待直接发（兜底工作流用）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import yaml

from bearwatch import llm, model, notify, report, sources

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("collect")

ROOT = os.path.dirname(os.path.abspath(__file__))


def read_text(path: str, default: str = "") -> str:
    p = os.path.join(ROOT, path)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def write_text(path: str, content: str) -> None:
    with open(os.path.join(ROOT, path), "w", encoding="utf-8") as f:
        f.write(content)


def cc_is_fresh(cfg: dict) -> bool:
    """cc_brief.md 里第一行如果有 <!-- ts: ISO8601 --> 就用它判断新鲜度。"""
    txt = read_text("cc_brief.md")
    if not txt.strip():
        return False
    import re
    m = re.search(r"<!--\s*ts:\s*([0-9TZ:+\-\.]+)\s*-->", txt)
    if not m:
        return False
    try:
        ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return False
    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    return age_h <= cfg["llm"].get("cc_brief_max_age_hours", 60)


def strip_meta(txt: str) -> str:
    import re
    return re.sub(r"<!--.*?-->", "", txt, flags=re.S).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--send-now", action="store_true",
                    help="采集完立即发 Telegram（不等 Claude 的 CC 段）")
    ap.add_argument("--no-llm", action="store_true", help="跳过 DeepSeek 调用（自测用）")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tw = sum(cfg["weights"].values())
    if abs(tw - 100) > 1e-6:
        log.error("config.yaml 里 weights 之和 = %s，必须等于 100", tw)
        return 2

    log.info("=== 抓取数据 ===")
    data = sources.fetch_all(cfg)

    log.info("=== 打分 ===")
    prev = report.load_latest()
    a = model.assess(data, cfg)
    alerts = model.evaluate_alerts(a, data, prev, cfg)
    log.info("综合分 %.1f  →  概率 %.1f%%", a.score, a.probability * 100)
    for f in a.factors:
        log.info("  %-22s w=%-3.0f score=%s", f.label, f.weight,
                 f"{f.score:.0f}" if f.available else "N/A")
    if alerts:
        log.warning("触发告警：%s", [x["id"] for x in alerts])

    rec_preview = report.build_record(a, alerts, "")

    ds = "_（已跳过）_"
    if not args.no_llm:
        log.info("=== 调用 DeepSeek ===")
        ds = llm.call_deepseek(llm.build_user_prompt(rec_preview, prev, alerts), cfg)
    write_text("ds_brief.md", ds)

    rec = report.persist(a, alerts, ds, kind="weekly")

    cc_raw = read_text("cc_brief.md")
    cc = strip_meta(cc_raw) if cc_is_fresh(cfg) else ""
    if cc_raw.strip() and not cc:
        log.info("cc_brief.md 存在但已过期，本次报告中标记为待更新")

    md = report.render_markdown(a, alerts, ds, cc, cfg)
    report.write_report(md)
    log.info("已写入 REPORT.md（%d 字符）", len(md))

    if args.send_now:
        repo_url = os.environ.get("REPORT_URL", "")
        msg = notify.build_weekly_message(rec, prev, ds, cc, repo_url)
        ok = notify.send(msg)
        log.info("Telegram 发送%s", "成功" if ok else "失败")
        write_text("data/.notified_for", rec["as_of"])
        return 0 if ok else 1

    log.info("采集完成。等待 Claude 写入 cc_brief.md 后由 notify 工作流发送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
