# 🐻 bear-watch — 美股熊市概率监控

每周日一条 Telegram 消息，告诉你未来 3 个月美股进入熊市的概率、是什么在驱动它、
以及和上周相比什么变了。消息里有两段独立解读：**DS**（DeepSeek）和 **CC**（Claude）。

平时完全静音。只有触发高阈值规则时才会额外推送一条即时告警。

---

## 它是怎么工作的

```
周日 21:30 UTC ─┐
  GitHub Actions │ 抓数据 → 10 因子打分 → 算概率 → 调 DeepSeek
                 │ 写入 REPORT.md / data/history.jsonl / ds_brief.md → commit
                 └─────────────────────────────┐
                                               ▼
周日 23:00 UTC ─┐                    仓库里有了本周完整数据
  Cowork 定时任务 │ Claude 读 REPORT.md 的 raw 链接 → 独立判断
   （Claude）     │ 写 cc_brief.md → push 到仓库
                 └─────────────────────────────┐
                                               ▼
              cc_brief.md 被 push ──► GitHub Actions 组装 DS+CC ──► 📱 Telegram
```

如果周日深夜 Claude 那一步没跑成，`fallback-send.yml` 会在 03:30 UTC 把 DS 段先发出去，
不会整周漏报。`run_notify.py` 里有去重标记，正常路径已发过时兜底会自动跳过。

---

## 模型：为什么给你两个数字而不是一个

单一的"熊市概率"会骗人。市场创新高、VIX 15、信用利差极窄的时候，任何以趋势为主
的模型都会给出很低的读数 —— **它永远不会提前喊顶**。这不是缺陷，是这类模型的本质：
它测的是"点火"，不是"火药"。

所以报告里永远有两个数：

| 维度 | 由哪些因子构成 | 回答什么问题 | 择时价值 |
|---|---|---|---|
| **结构脆弱度** | 估值、收益率曲线、货币政策、市场宽度 | 一旦开跌，会不会跌成熊市、跌多深 | 几乎没有（可以在高位停留数年） |
| **触发压力** | 趋势、信用利差、波动率、就业、大宗、季节性 | 现在有没有真正的下跌动能 | 有 |

危险的从来不是"脆弱度高"，而是 **脆弱度高 + 触发压力开始上升**。
`fragile_and_firing` 这条告警规则监控的就是这个组合。

**实测（2026-08-10 的真实读数）：** 综合概率 5.3%，但结构脆弱度 66.9（偏高）、
触发压力 28.5（低）→ 判定为"火药很多，但还没有火星"。这个 5.3% 和一个人类分析师
可能给出的 10-15% 的差距，几乎全部来自模型看不见的东西（仓位极端、政策转向风险、
地缘尾部）。**这正是 DS 段和 CC 段存在的理由 —— 它们是模型盲区的补丁，不是装饰。**

### 10 个因子与权重

| 因子 | 权重 | 数据源 | 类别 |
|---|---:|---|---|
| 价格趋势（200日线/死叉/回撤） | 18 | Stooq ^spx | 触发 |
| 信用利差 HY OAS（水平+60日变化）| 16 | FRED BAMLH0A0HYM2 | 触发 |
| 估值 CAPE 历史分位 | 12 | multpl | 结构 |
| 就业（Sahm 规则 + 非农 3 月均）| 12 | FRED UNRATE / PAYEMS | 触发 |
| 收益率曲线 10y-3m（含倒挂滞后）| 10 | FRED T10Y3M | 结构 |
| 波动率 VIX（水平+20日变化）| 10 | Stooq ^vix | 触发 |
| 货币政策（实际利率+通胀缺口）| 8 | FRED DFF / CPILFESL | 结构 |
| 市场宽度 RSP/SPY 比价 | 6 | Stooq | 结构 |
| 能源冲击（布伦特 3 月变化）| 5 | Stooq | 触发 |
| 季节性 + 总统周期 | 3 | 内置 | 触发 |

**概率映射：** `logit(p) = 0.056 × (总分 − 50) − 2.442`
校准锚点：总分 50 → 8%（≈1950 年以来任意季度开启 -20% 熊市的无条件基准概率），
75 → 25%，90 → 45%。

权重和阈值全在 `config.yaml` 里，改完下次运行立即生效，不用动代码。

---

## 部署（约 10 分钟）

### 1. 建仓库

在 GitHub 建一个新仓库，名字随意（下面用 `bear-watch`），**建成 public**。

> 为什么必须 public：Claude 每周日要通过 `raw.githubusercontent.com` 读 `REPORT.md`。
> 私有仓库的 raw 链接需要带 token，Claude 那边没法安全地存 token。
> **仓库里不含任何密钥** —— 所有密钥都在 GitHub Secrets 里，不进代码库。
> 里面只有公开市场数据和分析文本，公开没有风险。

把本项目所有文件放进去，push 到 `main` 分支。

### 2. 建 Telegram 机器人

1. 在 Telegram 里找 **@BotFather** → 发 `/newbot` → 起个名字 → 拿到形如
   `8123456789:AAH...` 的 token
2. 给你的新机器人发一条任意消息（**必须先发，否则机器人无权给你发消息**）
3. 浏览器打开 `https://api.telegram.org/bot<你的TOKEN>/getUpdates`，
   在返回的 JSON 里找 `"chat":{"id":123456789` —— 这个数字就是你的 chat_id

### 3. 填 Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| 名称 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | 上一步的 token |
| `TELEGRAM_CHAT_ID` | ✅ | 上一步的 chat id |
| `DEEPSEEK_API_KEY` | ✅ | platform.deepseek.com 生成，`sk-` 开头 |
| `FRED_API_KEY` | ⬜ | 可选。不填走公开 CSV 端点，也能跑；填了更稳 |

### 4. 开权限

仓库 → Settings → Actions → General → Workflow permissions →
勾选 **Read and write permissions** → Save。
（工作流要把 `REPORT.md` 和 `data/` 提交回仓库）

### 5. 建一个 PAT 给 Claude 用

GitHub → Settings（个人）→ Developer settings → Personal access tokens →
**Fine-grained tokens** → Generate new token：

- Repository access：**Only select repositories** → 只选这一个 `bear-watch`
- Permissions → Repository permissions → **Contents: Read and write**
- 有效期建议 90 天或 1 年（到期要换）

生成后把 token 发给 Claude，Claude 会写进每周日的定时任务里，用来 push `cc_brief.md`。

> ⚠️ 这个 PAT 只能改这一个公开仓库的内容，权限范围已经压到最小。
> 即便如此，如果你不接受这一点，可以把 CC 段改成手动模式 ——
> 每周日 Claude 把解读发给你，你自己贴进 `cc_brief.md`。

### 6. 先手动跑一次验证

Actions → **周日采集 (collect + DeepSeek)** → Run workflow → 把 `send_now` 勾上 →
Run。约 1-2 分钟后应该收到第一条 Telegram 消息，仓库里也会出现 `REPORT.md`。

---

## 时间表（多伦多时间，夏令时）

| 什么 | 时间 | UTC cron |
|---|---|---|
| 采集 + DeepSeek | 周日 17:30 | `30 21 * * 0` |
| Claude 读文档写 CC | 周日 19:00 | Cowork 定时任务 |
| 发周报 | CC push 后立即 | `on: push` |
| 兜底发送 | 周日 23:30 | `30 3 * * 1` |
| 每日静默告警检查 | 周一至周五 18:15 | `15 22 * * 1-5` |

> 冬令时（11 月至次年 3 月）这些时间会自动变成早一小时，因为 cron 走 UTC。
> 介意的话把 `weekly-collect.yml` 的 cron 改成 `30 22 * * 0` 即可。

---

## 告警规则（默认高阈值，平时静音）

平时**不会**给你发任何东西。以下任一条满足才推送，且同一条规则 5 天内不重复：

- 熊市概率相对上周跳升 > 15 个百分点
- 熊市概率首次突破 40%
- S&P 500 距 52 周高点回撤 > 10%
- VIX 收盘 > 30
- 高收益债利差 60 个交易日走扩 > 100bp
- **结构脆弱度 ≥62 且触发压力 ≥55**（最重要的一条）
- 50 日均线下穿 200 日均线（死叉，仅首次）
- 已确认进入熊市（回撤 ≥20%）

嫌吵就把 `config.yaml` 里对应规则删掉；嫌迟钝就把 threshold 调小。

---

## 本地开发

```bash
pip install -r requirements.txt

python tests/test_offline.py        # 46 项离线自测，不需要网络和密钥

export DEEPSEEK_API_KEY=sk-...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python run_collect.py --no-llm      # 只抓数据打分，不调 LLM 不发消息
python run_collect.py --send-now    # 完整跑一遍并立即推送
python run_alert.py --dry-run       # 打印告警内容但不发送
python run_notify.py --force        # 用现有数据强制重发一次周报
```

---

## 数据产物

| 文件 | 内容 |
|---|---|
| `REPORT.md` | 本周完整报告。人读，Claude 也读这个 |
| `ds_brief.md` | DeepSeek 本周解读 |
| `cc_brief.md` | Claude 本周解读（由 Cowork 定时任务写入） |
| `data/latest.json` | 最近一次周报的结构化结果 |
| `data/history.jsonl` | 全部周报历史，一行一条 —— 一年后可以用它回溯检验模型准不准 |
| `data/daily.jsonl` | 每日静默检查记录 |
| `data/alert_state.json` | 各告警规则上次触发日期（冷却用） |

`history.jsonl` 是这个项目最有价值的产物。跑满一年后，你可以拿它做一件事：
把每次的概率读数和随后 3 个月的实际走势对齐，看模型到底准不准。
**一个不能被证伪的模型没有价值。**

---

## 已知局限（请认真读）

1. **模型看不见新闻。** 地缘政治、政策突变、单一公司事件（比如某云厂商砍 AI 资本
   开支指引）它一概不知道。这些由 DS 段和 CC 段补。
2. **模型在高位系统性低估风险。** 见上文"两个数字"一节。别只看那一个百分比。
3. **CAPE 来自网页解析。** multpl 改版会让这个因子失效 —— 失效时会自动标为缺失
   并把权重分给其他因子，报告里会写明。
4. **概率映射的校准是先验的，不是回归拟合的。** 锚点（50→8%）来自历史基准频率，
   但因子到分数的映射曲线是人工设定的。跑满一两年、积累足够 `history.jsonl`
   之后应该重新校准。
5. **这不是投资建议。** 它是一个把一堆分散的宏观指标压缩成一个可追踪数字的工具。
   拿它做仓位决策的后果由你自己承担。
