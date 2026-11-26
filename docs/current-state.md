# 当前状态（compact 后恢复用）

> 截至 2026-11-20 14:37（Asia/Shanghai）。
> 用途：DSH compact/上下文压缩后，先读这一页恢复关键状态；详细设计见 agent-mode-design.md，项目约定见 ../AGENTS.md。

## 1. 项目与地址

- 仓库：https://github.com/alangrace/mokou-stock-simulation
- GitHub Pages：https://alangrace.github.io/mokou-stock-simulation/
- 页面入口：docs/index.html（由 scripts/publish_dashboard.sh 每日收盘生成并推送）
- 本地实现：sayu-paper-trader/（纯 Python 3.12 + SQLite，无第三方依赖）
- 当前仓库是 public，dashboard 含模拟盘现金/持仓/成交/决策，注意隐私。

## 2. 模拟盘账户

账户：默认模拟盘（id=1，multi_factor，balanced）

| 项目 | 数值 |
|---|---:|
| 现金 | 801,940.50 |
| 持仓市值 | 199,385.00 |
| 总资产 | 1,001,325.50 |
| 浮盈 | +1,325.50 |
| 已实现 | 0.00 |
| 持仓数 | 4 |

持仓：

| 代码 | 名称 | 股数 | 成本 | 现价 | 浮盈 | 计划止损 | 计划止盈 |
|---|---|---:|---:|---:|---:|---:|---:|
| 600028 | 中国石化 | 8,500 | 5.881 | 5.940 | +497.50 | 5.527 | 6.586 |
| 600887 | 伊利股份 | 1,500 | 32.338 | 32.910 | +857.88 | 30.39 | 36.21 |
| 601166 | 兴业银行 | 2,500 | 19.995 | 20.030 | +87.51 | 18.791 | 22.389 |
| 601668 | 中国建筑 | 10,500 | 4.721 | 4.710 | -117.39 | 4.437 | 5.286 |

最近成交 4 笔，全是买入；无卖出。T+1 已过，持仓可正常按计划卖出。

## 3. 计划与仓位上限

计划文件：sayu-paper-trader/plans/entry-20261118-defensive.json

- plan_id：entry-20261118-defensive
- 宏观档位：防御（静态，P2 未自动切换）
- 候选池：20 只（2026-11-26 从 10 扩到 20，新增 601318 中国平安、601899 紫金矿业、600309 万华化学 三只防御档可买）
- max_positions：10（2026-11-26 从 8 上调）
- 计划 risk：总仓 80%、单票 10%
- 实际生效（防御档取 min）：总仓 30%、单票 5%、单日最多 12 笔、同行业最多 1 只、最低现金 10%
- 进攻档（若切换）：计划总仓 80% 与宏观 80% 取 min → 80%；单票 10%（宏观上限 15%）→ 满仓 8 只即 80%，max_positions=10 只是留 2 只余量，不额外放宽总仓
- 中性档：总仓 60%、单票 10% → 6 只满单票
- 自动切档：设计上已允许，但 P2 未生效；P3 才让引擎读当日 macro_snapshot
- 2026-11-20 P0：buy 已强制止盈（plan_take_pct/plan_take_price）+ 百分比归一化（-6→-0.06，12→0.12）；agent dry-run 同步走该硬闸
- 2026-11-20 P1a：position 已加 plan_updated_at/plan_rev_count；agent plan-update --dry-run 支持 backfill/trail_stop/macro_tighten/take_adjust；只审计、不写计划

## 4. 调度（DSH）

| 任务 | 下次/时间 | ID |
|---|---|---|
| 运行模拟盘决策心跳（规则 tick） | 工作日 09:00–14:30 每 30 分钟 | schedule-697a25b4-5725-454a-ace4-891d5c0734a6 |
| 收盘前扫描模拟盘（规则 tick） | 工作日 14:45 | schedule-b07de742-4169-425f-be27-c15ee7db59aa |
| 写模拟盘收盘快照（tick + 发布） | 工作日 15:05 | schedule-c9926178-0664-48e2-a330-d97c332612f8 |
| Agent dry-run 决策 | 2026-11-20 13:00 至 2026-11-23 15:00，工作日 09:00–14:30 每 30 分钟 | schedule-1dfe4947-ab14-49c5-87fb-377138d8161c |
| Agent dry-run 收盘前扫描 | 同一时段 14:45 | schedule-203b40f1-5461-429d-ae74-8d8e146b916e |
| **Agent 主动决策（LLM 决策层）** | 工作日 10:15 / 11:15 / 13:15 | schedule-3ada88b4-7ad2-4e88-a7f6-f61bae5b7273 |
| 检查 SayuStock 模拟盘策略更新 | 每周一 09:00，首次 2026-11-23 | schedule-f3464ae8-63cd-4bf6-87c2-e972d2039b69 |
| P3 切换复盘提醒 | 2026-11-23 15:30 一次性 | schedule-b83eff03-249f-488a-a4f6-5205426bcf2a |

注意：DSH 任务只在 Harness 运行时投递；关机/睡眠会跳过或合并。

## 5. P2 dry-run 状态

- 周期：2026-11-20 13:00 至 2026-11-23 15:00，2 个交易日。
- 只写 agent_run，不写 trade/position/cash/decision；不成交。
- 规则 tick 仍在跑，账户会变；dry-run 提议按当时账户校验。
- 13:00：600519 100 股被 total_exposure 拒（34.1%>30%）。
- 13:30：600036 被 sector_cap 拒；600519 被 total_exposure 拒（34.3%）。
- 14:00/14:30：002415 在修复前曾 would-fill；14:37 补上防御名单硬闸后，002415 已被 macro_defensive_only 拒绝。
- 重要修复（2026-11-20 14:37）：proposal.py 之前漏了“防御档只买 defensive=true”的硬闸；已补上。修复前的 002415 ok=true 记录是旧逻辑，不能当作有效提议。
- 规则引擎的 14:30 决策已正确把 002415/600519 标为 macro_defensive_only。
- **P2 已于 2026-11-23 15:00 结束**，完整复盘见 docs/p2-review.md：
  - 129 条 agent_run：hold 100、blocked 20、ok 4；
  - 拒绝原因：macro_defensive_only 9、sector_cap 6、total_exposure 2、single_exposure 1、not_enough_profit 1；
  - 关键发现：dry-run 没执行 planner.entry_blockers 与 min_score，RSI6/MA20 超限也能过；
  - 结论：切 P3 前必须先做「入场拦截对齐」。

## 6. GitHub 发布

- 每日 15:05：tick 写收盘快照 → scripts/publish_dashboard.sh → 生成 dashboard → 复制 docs/index.html → commit → push。
- 已测试：脚本可 commit/push；最新 commit 以 git log 为准，页面数据时点以 docs/index.html 为准。
- GitHub Pages 已启用，地址见上。
- 仓库 public；dashboard 含账户/持仓/成交/决策，公开可见。

## 7. 上游周检

- 基线：sayu-paper-trader/upstream_state.json
- 上次记录：64db0c1（2026-11-17T20:14:12Z）
- 下次检查：2026-11-23 09:00
- 有更新只报告，确认后才同步；不自动改代码。

## 8. compact 后恢复步骤

1. 读本文件、../AGENTS.md、agent-mode-design.md。
2. 看账户：cd sayu-paper-trader 后跑 python3.12 -m papertrader.cli status --account 默认模拟盘 --md live-status.md
3. 看 dry-run 记录：python3.12 -m papertrader.cli agent runs --limit 20
4. 看调度：调用 schedule_list，确认上表任务仍在。
5. 看发布：git log -3；打开 https://alangrace.github.io/mokou-stock-simulation/ 确认页面。
6. 若用户在 15:05 后问“今天推送了吗”，先看 git log 是否有 dashboard: YYYY-MM-DD 提交。

## 9. 当前待办/边界

- P2 已完成并复盘（docs/p2-review.md）。
- 2026-11-23 15:40 已完成「入场拦截对齐」：proposal.validate 现在调用 planner.entry_blockers，并把 min_score 从软警告改为硬拦截；新增 RSI 超限、评分不足两个测试。
- 2026-11-23 15:40 已确认 P3 参数：单日 Agent 成交 3 笔、与规则 tick 共用 max_daily_trades（12）、不开影子账户。
- 2026-11-23 15:40 已统一「带名称」：agent decision 会自动回填股票名称，engine 指标错误也带名称。
- P1b 已完成（2026-11-23 16:58）：agent plan-update 支持 --execute，但需 SAYU_AGENT_TRADING=1；写入只改计划字段并追加 hold 审计。
- P3 已完成（2026-11-24 09:10）：agent trade --execute 可写库，需 SAYU_AGENT_TRADING=1；单日 Agent 3 笔、与 tick 共用 max_daily_trades、单日 Agent 金额 ≤10% 权益、回撤 2% 熔断；trade 表新增 source 字段并在 dashboard 显示来源。
- 2026-11-26 15:00 打开 Agent 总开关并落地自动移动止损：
  - 新增 `papertrader/switches.py`：环境变量 `SAYU_AGENT_TRADING` 优先，其次读 `data/agent_trading.flag`。**必须落盘**，因为 DSH 定时任务每次心跳都是全新 shell，不继承终端 export。
  - 新增 `agent switch --on/--off`（写/删 flag）与 `agent trail [--execute]`（单票浮盈 ≥+8% 时把止损上移到现价 -3%，单次幅度 ≥2%，仍受 20h 冷却、不得高于现价、不得宽于模式硬止损约束）。
  - `tick` 在开关打开时会自动跑一轮 trail；**只改 position 的计划字段，不产生买卖流水、不动现金**。
  - 开关已置为开启（flag 存在）。当前 7 只持仓浮盈均 <8%，尚无止损被上移。
- 2026-11-26 15:20 接入 LLM 决策层（方案 A 落地）：
  - 新增 schedule「Agent 主动决策」：工作日 10:15 / 11:15 / 13:15 各一轮，跑 `agent context --compact` → 判断 → `agent trade --execute` 或 `agent decision`（**无成交也必须留痕**）。
  - 与规则 tick 错开 15 分钟，避免抢同一批报价；Agent 只做「主动机会」，规则 tick 继续全量巡检。
  - `agent context --compact` 把 89KB 裁到 60KB（候选只留关键指标、历史只留最近 12 条决策/8 条成交），降 token 成本。
  - dashboard 新增「Agent 判断记录」面板：时间 / 状态 / 标的（带名称）/ 结论 / 判断依据，含 `blocked_by` 拦截码。
- 下一步：观察 1~2 周 P3 成交质量；确认是否调整 Agent 配额或止盈止损策略。
- 自动切宏观档位未生效；P3 实现时按设计加防抖/高危约束。
- 15:05 发布依赖 Harness 运行；要每天必推需加 launchd/cron。
- 公开仓库隐私风险未处理；如不能接受，改私有/脱敏。
