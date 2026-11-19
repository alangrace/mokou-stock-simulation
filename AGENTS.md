# AGENTS.md

本仓库是「SayuStock 模拟盘机制分析 + 独立复刻」的交付物。**任何 agent 在本目录工作前，先读本文件**；需要完整步骤时读 [docs/new-machine-runbook.md](docs/new-machine-runbook.md)。

## 这是什么

- `analysis/`：对 [SayuStock](https://github.com/KimigaiiWuyi/SayuStock)（GPL-3.0-or-later）模拟盘「自主交易」能力的机制拆解与复刻路线评估。只读参考。
- `sayu-paper-trader/`：可运行的独立复刻。纯 Python 3.12 标准库 + SQLite，A 股模拟盘，行情来自东方财富（腾讯日 K 兜底）。
- 上游源码**不随本仓库分发**。不要去找 `SayuStock/` 目录，也不要把它加进来。

## 在新机器上跑起来（最短路径）

```bash
# 0) 前置：Python >= 3.12（代码用了 dataclass(slots=True) 和 zoneinfo）
python3.12 --version

# 1) 进入实现目录（所有命令都在这里跑）
cd sayu-paper-trader

# 2) 建账 + 载入计划（新机器 data/paper.db 不存在，必须先 init）
python3.12 -m papertrader.cli init --account 默认模拟盘 --cash 1000000
python3.12 -m papertrader.cli plan-load --file plans/entry-20261118-defensive.json

# 3) 冒烟：能拉到行情才算环境通
python3.12 -m papertrader.cli preview --account 默认模拟盘
python3.12 -c "from papertrader.market import MarketClient; q=MarketClient().quote('600036'); print(q.price, q.change_pct)"

# 4) 跑一轮 + 刷新监控页
python3.12 -m papertrader.cli tick --account 默认模拟盘
python3.12 -m papertrader.cli dashboard
```

跑通后核对 `reports/dashboard.html` 与 `reports/live-status.md` 有内容。

## 硬性约束

- **保持零第三方依赖**。只允许标准库；需要数据就调用东财/腾讯公开接口。
- `data/`（SQLite 账本）、`logs/`、`__pycache__/` 一律不提交；`.gitignore` 已覆盖，不要用 `git add -f` 绕过。
- 不要提交任何密钥、cookie、券商凭据。本项目只做模拟盘，不接真实券商。
- 改交易逻辑（`ledger.py` / `rules.py` / `engine.py` / `planner.py` / `plan.py`）后必须跑：

  ```bash
  python3.12 -m unittest discover -s tests -v
  ```

- 不要自动改策略或替换入场计划。计划里的宏观档位、止损止盈是人的决策；agent 只执行与报告。

## 需要 agent 主动完成的事

1. **Python 环境**：新机器没有 3.12 时先装（macOS `brew install python@3.12`；Windows `winget install Python.Python.3.12`；Linux 用发行版包或 `uv python install 3.12`），并确认 `python3.12 -m papertrader.cli --help` 可用。
2. **重建定时任务**：定时任务不随仓库迁移。新机器上按 [runbook 第 6 节](docs/new-machine-runbook.md#6-定时任务) 用 cron / 计划任务 / DSH schedule 重建；DSH 的 rrule：

   ```
   DTSTART;TZID=Asia/Shanghai:20261120T090000
   RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9,10,11,13,14,15;BYMINUTE=0,30
   ```

   命令是 `python3.12 -m papertrader.cli tick --account 默认模拟盘`，工作目录 `sayu-paper-trader/`。
3. **每日定档**：仓库里的计划是 2026-11-18 的防御档样例。新机器换日运行前，先和用户确认当天的宏观档位与候选池，再 `plan-load` 新计划，不要套用旧计划直接成交。

## 常见故障（先看这里再看日志）

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: papertrader` | 没在 `sayu-paper-trader/` 里跑 | `cd sayu-paper-trader` 后重跑 |
| `RuntimeError: 账户不存在` | 新库没建账 | 先 `init`，再 `plan-load` |
| `remote end closed connection` / 决策全是 `no_data` | 东财限频或网络不通 | 等 1–2 分钟重试；检查是否能直连 `push2.eastmoney.com` |
| 通知不弹 | 非 macOS 会静默跳过 | 正常行为，不影响成交 |
| 节假日判断错误 | 日历是 2026 静态表 | 跨年要更新 `papertrader/calendar.py` |

## 什么时候先问人

- 要改策略阈值、仓位上限、止损止盈；
- 要接入新的行情源或 LLM planner；
- 要把账本从零重建或清空；
- 顺手发现上游 SayuStock 有更新（只报告，确认后再同步）。
