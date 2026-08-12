#!/usr/bin/env python3
"""
发送入口。读取 data/latest.json + ds_brief.md + cc_brief.md，组装 DS/CC
双段消息发到 Telegram，并把 CC 段补回 REPORT.md。

被两个工作流调用：
  - notify.yml：cc_brief.md 被 push 之后立即触发（正常路径）
  - fallback.yml：周一凌晨兜底，如果 CC 一直没来就只发 DS 段

用 data/.notified_for 记录"这一期已经发过了"，防止重复推送。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

import yaml

from bearwatch import notify, report

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("notify")
ROOT = os.path.dirname(os.path.abspath(__file__))


def read_text(path: str, default: str = "") -> str:
    p = os.path.join(ROOT, path)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return f.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--require-cc", action="store_true",
                    help="cc_brief.md 为空或过期时直接退出，不发送")
    ap.add_argument("--force", action="store_true", help="忽略去重标记强制发送")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    rec = report.load_latest()
    if not rec:
        # 初次推送仓库时 cc_brief.md 会触发本工作流，但那时还没有任何采集数据。
        # 这是正常情况，不是错误 —— 干净退出，别在 Actions 里留红叉。
        log.info("还没有 data/latest.json（仓库刚初始化？）—— 无事可做，正常退出。"
                 "等周日的采集工作流跑完之后再发送。")
        return 0

    marker = read_text("data/.notified_for").strip()
    if marker == rec["as_of"] and not args.force:
        log.info("本期（%s）已发送过，跳过。", rec["as_of"])
        return 0

    ds = read_text("ds_brief.md") or rec.get("ds_brief", "")
    cc_raw = read_text("cc_brief.md")
    cc = re.sub(r"<!--.*?-->", "", cc_raw, flags=re.S).strip()

    if args.require_cc and not cc:
        log.info("cc_brief.md 为空，--require-cc 模式下不发送。")
        return 0
    if not cc:
        cc = "（本期 Claude 未写入解读，仅 DeepSeek 段。）"

    hist = report.load_history()
    prev = hist[-2] if len(hist) >= 2 else None
    repo_url = os.environ.get("REPORT_URL", "")

    msg = notify.build_weekly_message(rec, prev, ds, cc, repo_url)
    ok = notify.send(msg)
    log.info("Telegram 发送%s（消息 %d 字符）", "成功" if ok else "失败", len(msg))

    if ok:
        # 把 CC 段补进 REPORT.md，让文档和推送内容一致
        md = read_text("REPORT.md")
        if md:
            new_md = re.sub(
                r"(## 六、CC：Claude 解读\n\n)(.*?)(\n\n---)",
                lambda m: m.group(1) + cc + m.group(3),
                md, flags=re.S,
            )
            with open(os.path.join(ROOT, "REPORT.md"), "w", encoding="utf-8") as f:
                f.write(new_md)
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        with open(os.path.join(ROOT, "data/.notified_for"), "w", encoding="utf-8") as f:
            f.write(rec["as_of"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
