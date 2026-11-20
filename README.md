# SayuStock 模拟盘功能分析 · 交付物

对 [KimigaiiWuyi/SayuStock](https://github.com/KimigaiiWuyi/SayuStock)（GPL-3.0-or-later）模拟盘「自主交易」能力的研究与独立复刻。上游源码仅作本地对照，**不随本仓库分发**；本仓库只包含分析文档与独立实现的代码。

本目录包含两部分：

1. **机制分析**：[analysis/SayuStock-模拟盘机制分析与复刻.md](analysis/SayuStock-模拟盘机制分析与复刻.md)
   - 拆解 SayuStock 如何让机器人自主看盘、按计划买卖并记账；
   - 逐层分析决策代理 prompt、计划字段、策略硬闸、原子账本、撮合规则、调度与播报；
   - 给出可复刻性评估与三条复刻路线。

2. **独立复刻**：[sayu-paper-trader/](sayu-paper-trader/)
   - 纯 Python 3.12 标准库 + SQLite + 东方财富公开行情；
   - 计划驱动、宏观档位仓位上限、A 股费用/整手/涨跌停/T+1、原子成交、决策留痕、成交播报；
   - 支持实时心跳与历史时点回放。

## 在新电脑上运行

换一台机器只需要 Python ≥ 3.12 和能访问东方财富的网络，不需要第三方依赖、数据库服务或 API key：

```bash
git clone https://github.com/alangrace/mokou-stock-simulation.git
cd mokou-stock-simulation/sayu-paper-trader
python3.12 -m papertrader.cli init --account 默认模拟盘 --cash 1000000
python3.12 -m papertrader.cli plan-load --file plans/entry-20261118-defensive.json
python3.12 -m papertrader.cli tick --account 默认模拟盘
```

完整步骤（含定时任务、回放、迁移旧账本、故障排查、验收清单）见 **[docs/new-machine-runbook.md](docs/new-machine-runbook.md)**；给 agent 看的执行约定见 **[AGENTS.md](AGENTS.md)**。

注意：账本 `data/paper.db` 不入库，新机器是空库，必须先 `init`；定时任务不随仓库迁移，需按 runbook 第 6 节重建。

## 文档

- 机制分析：[analysis/SayuStock-模拟盘机制分析与复刻.md](analysis/SayuStock-模拟盘机制分析与复刻.md)
- 方案 A 设计（DSH 当 LLM 运行时）：[docs/agent-mode-design.md](docs/agent-mode-design.md)
- 复刻实现：[sayu-paper-trader/](sayu-paper-trader/)

## 发布监控页到 GitHub Pages

仓库：[alangrace/mokou-stock-simulation](https://github.com/alangrace/mokou-stock-simulation)

- docs/index.html：每日收盘快照
- GitHub Pages 入口: https://alangrace.github.io/mokou-stock-simulation
- scripts/publish_dashboard.sh：生成 dashboard → 复制到 docs/index.html → commit → push；
- 15:05 的收盘快照任务会在写快照后执行该脚本；

> 仅用于模拟盘/工程研究，不构成投资建议。
