# sayu-paper-trader · SayuStock 模拟盘功能的独立复刻

这是对 [SayuStock](https://github.com/KimigaiiWuyi/SayuStock) 模拟盘"自主交易"能力的**最小可运行复刻**：

- 纯 Python 3.12 标准库，不依赖 gsuid_core、不依赖 LLM；
- 用 SQLite 做单源账本（账户 / 持仓 / 流水 / 决策 / 快照 / 计划 / 播报）；
- 用东方财富公开行情做实时价 / 日 K / 1 分钟 K；
- 用规则化 planner 替代 LLM 决策层，保留同样的**计划字段 + 硬闸 + 原子成交**契约；
- 支持实时心跳（daemon）与历史时点回放（replay）。

> 仅用于模拟盘/工程研究，不构成投资建议，也不接真实券商。

---

## 1. 快速开始

> 需要 Python 3.12+（代码用了 `dataclass(slots=True)`）。本机 `python3` 是 3.9，请用 `python3.12`。

```bash
cd sayu-paper-trader

# 1) 建账（默认 100 万）
python3.12 -m papertrader.cli init --account 默认模拟盘 --cash 1000000

# 2) 载入入场计划
python3.12 -m papertrader.cli plan-load --file plans/entry-20261118-defensive.json

# 3) 预览（非交易时段只写"挂起"决策，不成交）
python3.12 -m papertrader.cli preview --account 默认模拟盘

# 4) 历史回放（用 1 分钟 K 线回到指定时点成交）
python3.12 -m papertrader.cli run --account 回放盘-20261118 \
  --replay --as-of "2026-11-18 10:30" --snapshot

# 5) 实时心跳：等到开盘，每 10 分钟一轮，13:15 退出
python3.12 -m papertrader.cli daemon --account 默认模拟盘 --interval 600 --until 13:15

# 6) 查看状态 / 生成 Markdown 报告 / 生成可视化页面
python3.12 -m papertrader.cli status --account 默认模拟盘 --md live-status.md
python3.12 -m papertrader.cli dashboard --account 默认模拟盘
open reports/dashboard.html
```

定时任务（DSH / cron / launchd 都可以调用）：

```bash
python3.12 -m papertrader.cli tick --account 默认模拟盘
```

`tick` 会自己判断：

- 交易时段：跑一轮决策（成交/写 hold），刷新 dashboard；
- 收盘后（post_close）：写当日净值快照，刷新 dashboard；
- 开盘前 / 午休 / 节假日：直接跳过，只刷新 dashboard。

首次使用回放盘需要先建账并载入计划：

```bash
python3.12 -m papertrader.cli init --account 回放盘-20261118 --cash 1000000
python3.12 -m papertrader.cli plan-load --file plans/entry-20261118-defensive.json \
  --account 回放盘-20261118
```

---

## 2. 命令

| 命令 | 作用 |
|---|---|
| `init` | 创建命名模拟盘（默认 100 万，multi_factor） |
| `plan-load` | 校验并入库存入入场计划 JSON |
| `screen` | 按计划阈值给候选池打分，不下单、不写决策 |
| `preview` | 跑一轮评估；非交易时段写"挂起"决策，不成交 |
| `run` | 跑一轮并成交；`--replay --as-of` 回放历史时点 |
| `daemon` | 等到开盘，按 interval 跑心跳，直到 until，写快照退出 |
| `tick` | 定时任务专用：交易时段跑一轮，收盘后写快照，并刷新 dashboard |
| `dashboard` | 生成自包含的 `reports/dashboard.html` 仪表盘 |
| `status` | 账户/持仓/流水/决策/播报；`--md` 输出 Markdown |
| `snapshot` | 写当日净值快照 |

---

## 3. 入场计划 JSON

最小结构见 [plans/entry-20261118-defensive.json](plans/entry-20261118-defensive.json)：

```json
{
  "plan_id": "entry-20261118-defensive",
  "name": "2026-11-18 防御档入场计划",
  "account": "默认模拟盘",
  "macro": {
    "regime": "防御",
    "note": "上证位于 MA20/MA60 下方，RSI6 超卖，按防御档控制仓位",
    "risk_off_severe": false
  },
  "risk": {
    "initial_cash": 1000000,
    "max_total_exposure_pct": 0.6,
    "max_single_pct": 0.15,
    "max_positions": 6,
    "min_cash_pct": 0.1,
    "max_daily_trades": 12
  },
  "entry": {
    "min_score": 0.2,
    "max_candidates_per_round": 3,
    "max_per_sector": 1,
    "require_above_ma20": true,
    "min_rsi6": 30,
    "max_rsi6": 75,
    "max_change_pct": 7,
    "stop_pct": -0.06,
    "take_pct": 0.12
  },
  "exit": { "score_exit": -0.45 },
  "universe": [
    {
      "code": "601166",
      "name": "兴业银行",
      "sector": "银行",
      "weight": 1.0,
      "defensive": true,
      "thesis": "低估值股份行，股息率提供安全垫；均线多头，作为防御档银行底仓"
    }
  ]
}
```

校验规则（[papertrader/plan.py](papertrader/plan.py)）：

- `macro.regime` 必须是 进攻/中性/防御；
- `macro.note` ≥8 字；
- `entry.stop_pct < 0`，`entry.take_pct > 0`；
- 每只候选必须有 code / name / thesis；
- buy 落库前还必须通过 `validate_buy`：数值止损 + 宏观档位 + 宏观依据；防御档只买 `defensive=true` 的票。

---

## 4. 一轮心跳做了什么

```text
读计划
  ↓
交易时段守卫（非交易时段只写挂起决策）
  ↓
持仓退出：计划止损 / 计划止盈 / 技术评分破坏 / T+1
  ↓
候选入场：计划阈值 + 站上 MA20 + RSI 区间 + 涨跌停 + 行业上限 + 单轮名额
  ↓
宏观档位仓位上限（进攻 80/15、中性 60/10、防御 30/5）
  ↓
仓位预算 → 100 股整手 → 费用
  ↓
原子成交：流水 + 现金 + 持仓（含 plan_stop_*）
  ↓
写决策（hold 也写）+ 成交播报
```

---

## 5. 与 SayuStock 的对应关系

| SayuStock | 本复刻 |
|---|---|
| SayuPaperAccount / Position / Trade / Decision / Snapshot | SQLite 同名表 |
| 决策代理 prompt | `planner.score_candidate / entry_blockers / exit_signal` |
| `gate_buy` | `plan.validate_buy` |
| `match_order` | `rules + ledger.record_fill` |
| 30 分钟 cron | `cli.daemon` |
| 成交冒泡 | `ledger.broadcast` + stdout |
| `papertrade_*` 工具 | CLI 子命令（也可被 LLM 调用） |

---

## 6. 回放模式

```bash
python3.12 -m papertrader.cli run --account 回放盘-20261118 \
  --replay --as-of "2026-11-19 10:30"
```

- 成交价：取该时点或之前最近一根 1 分钟 K 线的收盘价；
- 指标：日 K 只用到 `as-of` 前一日，避免未来函数；
- 交易日/时段：按 `as-of` 判断；
- 所有决策/流水标记 `data_source=replay`，播报带 `[回放]` 前缀。

> 回放只能回放**当天**：东财 1 分钟接口只返回当日分时，`--as-of` 写更早的交易日会全部 `no_data`。要回放历史交易日需先补历史分钟线数据源。

---

## 7. 文件

| 文件 | 作用 |
|---|---|
| `papertrader/market.py` | 东财行情客户端 + 腾讯快照/日 K 回退 + TTL 缓存 |
| `papertrader/calendar.py` | 交易日/交易时段 |
| `papertrader/rules.py` | 费用/整手/涨跌停/T+1 |
| `papertrader/indicators.py` | MA/MACD/RSI/ATR/量比/分位 |
| `papertrader/ledger.py` | SQLite 账本与原子成交 |
| `papertrader/plan.py` | 计划校验与硬闸 |
| `papertrader/planner.py` | 规则化决策 |
| `papertrader/engine.py` | 一轮心跳 |
| `papertrader/report.py` | 文本/Markdown 报告 |
| `papertrader/cli.py` | 命令行入口 |
| `papertrader/dashboard.py` | 自包含 HTML 监控页生成 |
| `reports/*.md` | 生成的状态报告 |
| `reports/dashboard.html` | 监控页（每 30 秒自动刷新） |
| `reports/dashboard.png` | 监控页预览截图 |
| `upstream_state.json` | 上游周检基线（上次看到的 commit） |
| `data/paper.db` | 账本（运行后生成） |

---

## 8. 已知局限

- 规则评分只看技术面，事件/财报/舆情维度弱；生产版建议接 LLM planner（保持同样契约）。
- 按最新价/1 分钟收盘价成交，不模拟排队、滑点、部分成交。
- SQLite 单写者，适合单机低频；多盘高并发建议 PostgreSQL。
- 交易日历是 2026 年静态表；跨年/临时休市需接指数自证或更新表。
- 目前没有自动对账（幽灵仓/缺仓），长期跑建议补每日对账任务。

---

## 9. 可视化监控

```bash
python3.12 -m papertrader.cli dashboard --account 默认模拟盘
open reports/dashboard.html
```

生成的 `reports/dashboard.html` 是**自包含页面**（无 CDN / 无后端），包含：

- 总资产、最新快照盈亏、现金、持仓市值、浮盈、已实现；
- 净值曲线（快照 ≥2 个后显示）；
- 持仓表（现价、市值、浮盈、计划止损/止盈、距止损）；
- 最近成交、最近决策（含 hold 与硬闸拦截原因）；
- 入场计划与完整候选池。

页面每 30 秒自动刷新；数据由 `tick` 每次运行重写。`reports/dashboard.png` 是 headless Chrome 的预览截图。

## 10. 成交通知

macOS 上，只要本轮有成交，交易引擎会自动弹一条 **Notification Center 通知**：

- 标题「模拟盘成交」，副标题「默认模拟盘 · N 笔」；
- 正文列出入/卖出代码、股数、成交价（有已实现盈亏也会带上）；
- 一轮多笔成交会合并成一条，避免连续弹窗；
- 带系统提示音；
- 默认开启；`SAYU_PAPER_NOTIFY=0` 可关闭；
- 只对实时成交触发，**回放模式不会弹**。

通知优先走 `terminal-notifier`（有独立 bundle id；`brew install terminal-notifier`），若系统拒绝其通知权限，则自动回退为 **Tkinter 右上角浮层**（10 秒自动消失，不阻塞交易）。也可以用 `SAYU_PAPER_NOTIFY_MODE=toast|dialog|notification|auto` 强制通道；非 macOS 或没有图形会话时静默跳过，不影响交易主流程。

> 本机实测：terminal-notifier 3.1.0 已安装，但 macOS 以 ad-hoc 签名拒绝了它的通知权限（`Notifications are not allowed for this application`），所以当前实际走 **右上角 toast** 通道。

## 11. 定时任务

DSH 定时任务每 30 分钟调用一次：

```bash
python3.12 -m papertrader.cli tick --account 默认模拟盘
```

`tick` 的行为：

- 交易时段：跑一轮决策，刷新 dashboard；
- 收盘后：写当日净值快照，刷新 dashboard；
- 开盘前 / 午休 / 节假日：跳过，只刷新 dashboard。

当前 DSH 任务：周一到周五 09:00–15:30 每 30 分钟（`BYHOUR=9,10,11,13,14,15` + `BYMINUTE=0,30`），首次 2026-11-19 09:30。09:00 与 15:30 的触发由 `tick` 自己判断为跳过/收盘快照。

> 注意：DSH 定时任务只在 Harness 运行时投递；关机、睡眠或 Harness 未打开时会跳过/合并到最近一次。

## 12. 上游周检

每周一 09:00（Asia/Shanghai）检查一次 SayuStock 的模拟盘相关更新：

- 关注路径：`SayuStock/stock_papertrade`、`stock_agent`、`stock_macro`、`utils/database/papertrade_models.py`；
- 基线记录在 [upstream_state.json](upstream_state.json)；
- 有相关新提交时，只输出 commit / 文件 / 变化 / 对本地复刻的影响摘要，**先等确认再改本地**；
- 本地复刻不是原仓库分支，不能直接 merge，按摘要逐项同步。

## 13. 安全

- 纯模拟盘，只写本地 SQLite，不接券商、不发真实委托。
- 行情以东方财富为主，失败时回退腾讯快照/日 K；两者都不可达时拒绝成交，而不是按旧价成交。
- 不要把本项目当作投资建议或实盘交易系统。
