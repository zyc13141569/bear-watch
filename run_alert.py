#!/usr/bin/env python3
"""
每日静默检查入口。

平时什么都不发 —— 只抓数据、算分、写 data/daily.jsonl。
只有当配置里的高阈值规则被触发、且不在冷却期内时，才推一条即时告警。

冷却机制：每条规则单独记录上次触发日期，cooldown_days 内不重复推送。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

import yaml

from bearwatch import model, notify, report, sources

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("alert")
ROOT = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发送")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    data = sources.fetch_all(cfg)
    baseline = report.load_latest() or report.last_daily()
    a = model.assess(data, cfg)
    fired = model.evaluate_alerts(a, data, baseline, cfg)
    log.info("综合分 %.1f → 概率 %.1f%%；触发 %d 条",
             a.score, a.probability * 100, len(fired))

    rec = report.persist(a, fired, "", kind="daily")

    if not fired:
        log.info("无告警，保持静默。")
        return 0

    state = report.load_alert_state()
    today = date.today()
    cooldown = int(cfg["alerts"].get("cooldown_days", 5))
    to_send = []
    for al in fired:
        last = state.get(al["id"])
        if last:
            try:
                if (today - date.fromisoformat(last)).days < cooldown:
                    log.info("规则 %s 在冷却期内（上次 %s），跳过", al["id"], last)
                    continue
            except ValueError:
                pass
        to_send.append(al)

    if not to_send:
        log.info("全部命中的规则都在冷却期内，保持静默。")
        return 0

    repo_url = os.environ.get("REPORT_URL", "")
    msg = notify.build_alert_message(rec, to_send, repo_url)
    if args.dry_run:
        print(msg)
        return 0

    ok = notify.send(msg)
    log.info("告警发送%s：%s", "成功" if ok else "失败", [x["id"] for x in to_send])
    if ok:
        for al in to_send:
            state[al["id"]] = today.isoformat()
        report.save_alert_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
