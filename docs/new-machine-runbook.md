# 新机器运行指南（sayu-paper-trader）

> 目标：在一台**全新的电脑**上，从 `git clone` 到跑通一轮模拟盘心跳，全程不需要第三方向导。
> 本文所有命令都在 `sayu-paper-trader/` 目录下执行（`python3.12 -m papertrader.cli ...`）。
> 本文档在 2026-11-19 用「干净 clone + 现场行情」实测通过（init / plan-load / preview / tick / status / dashboard）；回放受「仅当日分钟线」与东财限频限制，见第 7 节。

## 目录

1. [适用场景与结论](#1-适用场景与结论)
2. [前置条件](#2-前置条件)
3. [拉取代码](#3-拉取代码)
4. [5 分钟冒烟验证](#4-5-分钟冒烟验证)
5. [建账与入场计划](#5-建账与入场计划)
6. [定时任务](#6-定时任务)
7. [日常命令速查](#7-日常命令速查)
8. [迁移旧账本（可选）](#8-迁移旧账本可选)
9. [环境变量](#9-环境变量)
10. [成交通知](#10-成交通知)
11. [故障排查](#11-故障排查)
12. [已知局限](#12-已知局限)
13. [验收清单](#13-验收清单)

---

## 1. 适用场景与结论

可以做到「拉下来就能跑」，但有几项前提必须有人在新机器上确认：

| 前提 | 说明 |
|---|---|
| Python ≥ 3.12 | 代码用了 `dataclass(slots=True)` 和 `zoneinfo`，3.11 及以下直接起不来 |
| 能访问东方财富 | 行情、K 线、回放都依赖 `push2.eastmoney.com` / `push2his.eastmoney.com`；海外或强制代理环境可能失败 |
| 重新建账 | 账本 `data/paper.db` 不入库，新机器是空库，必须先 `init` |
| 重建定时任务 | 定时任务属于旧机器，不随仓库迁移（见第 6 节） |
| 回放仅当天 | 1 分钟线接口只给当日分时，`--as-of` 写历史日期会全是 `no_data`（见第 7 节） |

不需要：第三方依赖、数据库服务、API key、券商账号。

## 2. 前置条件

| 项目 | 要求 |
|---|---|
| 系统 | macOS / Linux / Windows 均可（成交通知仅 macOS） |
| Python | ≥ 3.12；`python3.12 --version` 能输出版本号 |
| 网络 | 能直连东财；国内家庭/办公网络通常没问题，公司代理或海外 VPS 需自行验证 |
| 磁盘 | < 50 MB（代码约 1 MB，账本与日志增长很慢） |

安装 Python 3.12（按需选一条）：

```bash
# macOS
brew install python@3.12

# Linux（Debian/Ubuntu）
sudo apt install python3.12

# 任意平台，用 uv 最省事
uv python install 3.12
```

Windows 用 `winget install Python.Python.3.12`，之后命令里的 `python3.12` 换成 `py -3.12`。

> 验证：`python3.12 -c "import dataclasses; print('ok')"` 不报错即可。

## 3. 拉取代码

```bash
git clone https://github.com/alangrace/mokou-stock-simulation.git
cd mokou-stock-simulation/sayu-paper-trader
```

仓库是公开的，HTTPS 匿名可克隆。如果所在网络连不上 GitHub，先配置代理再克隆（例如 `git config --global http.proxy http://127.0.0.1:7890`），克隆完成后可以取消。

新机器上不需要、也不应该出现 `SayuStock/` 目录：上游源码不随本仓库分发。

## 4. 5 分钟冒烟验证

按顺序执行，每一步都应有明确输出。

```bash
# ① 版本
python3.12 --version                 # 期望 >= 3.12

# ② CLI 可用
python3.12 -m papertrader.cli --help # 期望列出 init/plan-load/preview/run/daemon/tick/status/snapshot/dashboard/screen

# ③ 行情可达（最关键一步）
python3.12 -c "from papertrader.market import MarketClient; q=MarketClient().quote('600036'); print('现价', q.price, '涨跌', q.change_pct)"

# ④ 建账（新机器 data/paper.db 不存在，必须先建）
python3.12 -m papertrader.cli init --account 默认模拟盘 --cash 1000000

# ⑤ 载入示例计划
python3.12 -m papertrader.cli plan-load --file plans/entry-20261118-defensive.json

# ⑥ 跑一次预览（只评估不成交）
python3.12 -m papertrader.cli preview --account 默认模拟盘

# ⑦ 跑一轮并生成监控页
python3.12 -m papertrader.cli tick --account 默认模拟盘
python3.12 -m papertrader.cli dashboard
```

③ 打印不出价格、或 ⑥ 里 10 条决策全是 `行情/指标不可达，跳过`，说明网络到东财不通，先解决网络再看后面。

⑦ 成功后应有 `reports/dashboard.html`，浏览器打开即可看到账户、持仓、决策与候选池。

> **限频提醒**：东财对短时间内的连续请求会直接断连（`RemoteDisconnected`），抓得太密会短暂封 IP。代码在连续失败 2 次后会熔断 120 秒并让日 K 回退腾讯，但**分钟线没有回退**，所以回放（第 7 节）在限频时会输出 `no_data`。停几分钟再试，不要连着重跑。

## 5. 建账与入场计划

一个账户 = 一条 SQLite 账户记录；计划是**人的决策输入**，不是代码常量。

```bash
# 新建一个盘（名字随便起，例如按日期）
python3.12 -m papertrader.cli init --account 默认模拟盘 --cash 1000000

# 载入计划（--account 不写时用计划里自带的 account 字段）
python3.12 -m papertrader.cli plan-load --file plans/entry-20261118-defensive.json --account 默认模拟盘
```

计划 JSON 的最小结构与校验规则见 [`../sayu-paper-trader/README.md`](../sayu-paper-trader/README.md#3-入场计划-json)。仓库里的 `plans/entry-20261118-defensive.json` 是 2026-11-18 的防御档样例，**只适合当模板**：

- 换日运行前，先确认当天的宏观档位（进攻/中性/防御）与候选池，写新计划再 `plan-load`；
- 防御档只允许买 `defensive=true` 的票，仓位上限 30%/5%；进攻档 80%/15%；中性档 60%/10%；
- 计划不合格会在加载时被拒（`regime` 非法、`note` 少于 8 字、`stop_pct ≥ 0`、候选缺 `thesis` 等）。

## 6. 定时任务

`tick` 是幂等的定时入口：交易时段跑一轮，收盘后写净值快照，其余时段只刷新 dashboard。**不要用 `daemon` 配 cron**，二选一。

### 6.1 DSH（DeepSeek Harness）

用 DSH 的 schedule 建三条工作日任务：

```text
# 心跳：09:00–14:30 每 30 分钟（09:00 会被 tick 判为开盘前跳过）
DTSTART;TZID=Asia/Shanghai:20261120T090000
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9,10,11,13,14;BYMINUTE=0,30

# 14:45 收盘前最后扫描（仍在交易时段）
DTSTART;TZID=Asia/Shanghai:20261120T144500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=14;BYMINUTE=45

# 15:05 收盘快照（tick 判定 post_close，只写快照）
DTSTART;TZID=Asia/Shanghai:20261120T150500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=15;BYMINUTE=5
```

任务提示词直接写：

```text
在 sayu-paper-trader 目录执行：python3.12 -m papertrader.cli tick --account 默认模拟盘
如有成交，报告代码、股数、成交价、费用和当前总资产；没有成交就一句话带过。
```

注意：DSH 的定时任务只在 Harness 运行时投递；关机、睡眠或 Harness 未打开时会跳过或合并到最近一次。

### 6.2 cron（macOS / Linux）

```cron
# 工作日 09:00–11:30、13:00–14:30 每 30 分钟
0,30 9-11,13-14 * * 1-5 cd /绝对路径/mokou-stock-simulation/sayu-paper-trader && /usr/bin/env python3.12 -m papertrader.cli tick --account 默认模拟盘 >> logs/cron.log 2>&1
# 14:45 收盘前最后扫描
45 14 * * 1-5 cd /绝对路径/mokou-stock-simulation/sayu-paper-trader && /usr/bin/env python3.12 -m papertrader.cli tick --account 默认模拟盘 >> logs/cron.log 2>&1
# 15:05 收盘快照
5 15 * * 1-5 cd /绝对路径/mokou-stock-simulation/sayu-paper-trader && /usr/bin/env python3.12 -m papertrader.cli tick --account 默认模拟盘 >> logs/cron.log 2>&1
```

用绝对路径，cron 的 `PATH` 很短，`python3.12` 最好写成 `/usr/local/bin/python3.12` 或 `$(which python3.12)` 的真实路径。

### 6.3 Windows 任务计划程序

「创建任务」→ 三个触发器，均为每周一至周五：09:00 起**重复任务间隔 30 分钟、持续时间 5 小时 30 分钟**（到 14:30）；14:45；15:05。操作：启动程序

```
程序：py
参数：-3.12 -m papertrader.cli tick --account 默认模拟盘
起始于：C:\绝对路径\mokou-stock-simulation\sayu-paper-trader
```

### 6.4 launchd（macOS，替代 cron）

不想用 cron 的话，把下面存成 `~/Library/LaunchAgents/com.sayu.papertrader.tick.plist` 再 `launchctl load`。`StartCalendarInterval` 每 30 分钟一次不方便表达，推荐仍用 cron；要用 launchd 就写成 09:00 起每 1800 秒的 `StartInterval`，并额外加 14:45、15:05 两个 `StartCalendarInterval`，脚本里自己判断交易时段。

## 7. 日常命令速查

```bash
# 看账户/持仓/流水/决策，并写 Markdown 报告
python3.12 -m papertrader.cli status --account 默认模拟盘 --md live-status.md

# 生成自包含监控页（每 30 秒自动刷新）
python3.12 -m papertrader.cli dashboard --account 默认模拟盘
open reports/dashboard.html          # Linux: xdg-open，Windows: start

# 手动跑一轮并成交
python3.12 -m papertrader.cli run --account 默认模拟盘

# 历史时点回放（需要先 init 该账户并 plan-load；只能回放当天，见下方说明）
python3.12 -m papertrader.cli init --account 回放盘-20261118 --cash 1000000
python3.12 -m papertrader.cli plan-load --file plans/entry-20261118-defensive.json --account 回放盘-20261118
python3.12 -m papertrader.cli run --account 回放盘-20261118 --replay --as-of "2026-11-19 10:30"

# 盘中守候：等到开盘，每 10 分钟一轮，13:15 退出并写快照
python3.12 -m papertrader.cli daemon --account 默认模拟盘 --interval 600 --until 13:15

# 只给候选池打分，不下单
python3.12 -m papertrader.cli screen --file plans/entry-20261118-defensive.json
```

回放说明：成交价取 `as-of` 时点或之前最近一根 1 分钟 K 线收盘价；日 K 只用到 `as-of` 前一日（避免未来函数）；决策与流水标记 `data_source=replay`，播报带 `[回放]` 前缀。

> **回放只能回放当天。** 东财的 1 分钟接口只返回**当日**分时：实测请求 500 根，拿到的仍是当天 09:30 起到当前时刻的数据。因此 `--as-of` 写昨天，候选全部会是 `no_data`（「行情/指标不可达，跳过」）。仓库 README 里 `2026-11-18 10:30` 的例子当时能跑通，是因为执行那天就是 11-18。要回放更早的交易日，需要先补一个能提供历史分钟线的数据源。

## 8. 迁移旧账本（可选）

想把旧机器的模拟盘历史搬过来，只需要一个文件：

```bash
# 旧机器
scp sayu-paper-trader/data/paper.db 新机器:/绝对路径/mokou-stock-simulation/sayu-paper-trader/data/paper.db

# 新机器验证
python3.12 -m papertrader.cli status --account 默认模拟盘
```

SQLite 文件跨系统可直接用，但要等旧机器上的 tick 跑完再拷，避免拷到半个事务。搬过来后**不要**再 `init` 同名账户（会冲突），直接 `status` 确认即可。`logs/`、`reports/` 是生成物，不搬也不影响账本。

## 9. 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `SAYU_PAPER_DB` | 账本路径 | `sayu-paper-trader/data/paper.db` |
| `SAYU_PAPER_NOTIFY` | 设为 `0` 关闭成交通知 | 开启（仅 macOS 生效） |
| `SAYU_PAPER_NOTIFY_MODE` | `auto`/`toast`/`dialog`/`notification` | `auto` |

## 10. 成交通知

只有 macOS 会弹通知，其他系统静默跳过（不影响交易）。macOS 上优先 `terminal-notifier`（`brew install terminal-notifier`），被系统拒绝权限时自动回退 Tkinter 右上角浮层。回放模式不弹。

## 11. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: papertrader` | 不在 `sayu-paper-trader/` 目录 | `cd sayu-paper-trader` 后重跑 |
| `RuntimeError: 账户不存在（先执行 init）` | 新库空，或账户名写错 | `init` 后再跑；注意 `--account` 与计划里的 account 一致 |
| `TypeError: 'slots' is an invalid keyword` 之类 | Python < 3.12 | 装 3.12，用 `python3.12` 而不是 `python3` |
| `remote end closed connection` / 决策全 `no_data` | 东财限频（高频抓取后会短暂封 IP，实测可持续数分钟到更久）或网络不通 | 停几分钟再试；验证 `curl -s -o /dev/null -w '%{http_code}' https://push2.eastmoney.com/`；正常 30 分钟一轮的 tick 不会触发 |
| 回放没有成交、全是 `no_data` | `--as-of` 不是当天（1 分钟线只保留当日），或当天被限频 | 把 `--as-of` 改成当天盘中时点；避开高频重试 |
| dashboard 打不开 / 数据旧 | 文件是生成物 | 重跑 `dashboard`；`tick` 每轮都会重写 |
| 通知不弹 | 非 macOS，或系统拒绝通知权限 | 正常；macOS 可查「系统设置 → 通知」 |
| 节假日仍在跑 | 日历是 2026 静态表 | 更新 `papertrader/calendar.py` 的 `HOLIDAYS_2026` 或接指数自证 |
| `Permission denied` 写日志 | 目录权限 | `chmod u+w logs reports data` |

## 12. 已知局限

- 规则评分只看技术面，事件/财报/舆情维度弱；
- 回放只能回放当天（东财 1 分钟线只覆盖当日分时），不能回放更早的交易日；
- 按最新价或 1 分钟收盘价成交，不模拟排队、滑点、部分成交；
- SQLite 单写者，适合单机低频；
- 交易日历是 2026 年静态表，跨年或临时休市需更新；
- 没有自动对账（幽灵仓/缺仓），长期跑建议补每日对账；
- 上游 SayuStock 是 GPL-3.0-or-later，本仓库只做独立复刻，不分发其源码。

## 13. 验收清单

新机器交付前逐条打勾：

- [ ] `python3.12 --version` ≥ 3.12
- [ ] `python3.12 -m papertrader.cli --help` 列出全部子命令
- [ ] `MarketClient().quote('600036')` 能打印现价与涨跌幅
- [ ] `init` 输出 `✅ 账户已就绪`
- [ ] `plan-load` 输出 `✅ 入场计划已加载`
- [ ] `preview` 输出 10 条决策（不是全 `no_data`）
- [ ] `tick` 跑完并生成 `reports/dashboard.html`
- [ ] `status --md live-status.md` 生成了 Markdown
- [ ] 定时任务已建好，且用 `tick` 手动验证过一次
- [ ] 当天计划已由人确认档位与候选池，不是直接套用 2026-11-18 的样例
