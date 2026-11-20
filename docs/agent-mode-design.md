# 方案 A 设计：DSH 作为 LLM 运行时，Python 只做执行与硬闸

> 目标：个人自用，不接 QQ、不做 gsuid_core 插件、不做 Kanban；保留现在的 SQLite 账本和 dashboard，让 DSH 的模型承担“读宏观/新闻、做判断”的部分，Python 负责撮合、风控、记账。
>
> 非目标：不复制原项目的 gsuid_core 宿主、QQ 适配器、Kanban 调度、消息播报；不让 LLM 直接写数据库。

## 0. 已确认决策（2026-11-20）

1. 候选池：先用计划里的 **10 只**，不做动态候选池。
2. Dry-run：**2 个交易日**，只提议不成交。
3. 宏观档位：**允许自动切**（有防抖/高危事件约束，见 3.3）。
4. 会话：用**当前对话**，不另开决策会话。
5. 仓位：**沿用计划**（当前单票/总仓/止损止盈参数）。

---

## 1. 总体架构

    DSH 定时任务（每 30 分钟）
        │  发送决策 prompt 到当前会话
        ▼
    DSH Agent（现有模型，无需单独 API key）
        │  1) 跑 agent context 读账户/持仓/计划/候选/宏观
        │  2) 用 web_search / web_fetch 查当日宏观与新闻
        │  3) 调 agent macro upsert 写宏观事件表
        │  4) 调 agent trade / agent decision 提交判断
        ▼
    Python CLI（唯一写入口）
        │  过 plan/风控/撮合硬闸
        │  写 trade + position + decision + snapshot
        ▼
    SQLite 账本 + dashboard + 通知

关键边界：

- **LLM 只提议，不直接写库。** 所有写操作都走 CLI。
- **Python 硬闸是最终裁判。** 缺止损/止盈、越档、T+1、涨跌停、行业/仓位上限一律拒绝。
- **计划是合同。** Agent 可以在计划范围内交易；宏观档位可在阈值内自动切换（见 3.3）；改计划文件/仓位上限/止损仍需用户确认。
- **无 QQ、无 gsuid_core。** DSH 定时任务只负责“叫醒模型”，交互仍在当前会话。

---

## 2. 需要新增的组件

### 2.1 数据表

在现有 SQLite 里新增三张表（不改现有账本表）：

**macro_event**

| 字段 | 说明 |
|---|---|
| id | 主键 |
| slug | 唯一标识，如 fed_dec_2026 / hormuz_oil |
| title | 事件标题 |
| severity | 1~5，4 以上算高危 |
| direction | risk_off / risk_on / mixed |
| status | open / settled |
| result | 最新进展 |
| stance | 对模拟盘的操作含义 |
| stale | 是否需要本轮复核 |
| created_at / updated_at | 时间 |

**macro_snapshot**

| 字段 | 说明 |
|---|---|
| id | 主键 |
| as_of | 快照时间 |
| regime | 进攻 / 中性 / 防御 |
| phase | 成长相 / 价值相 / 平衡 |
| fed_prob_25bp | 美联储 12 月 +25bp 概率 |
| breadth_down | 下跌家数/占比 |
| gold_pct / silver_pct / copper_pct | 金属日涨跌 |
| oil_pct / dollar / usdcnh | 油、美元、人民币 |
| note | 一句话宏观判断 |
| sources_json | 来源 URL 列表 |

**agent_run**

| 字段 | 说明 |
|---|---|
| id | 主键 |
| account_id | 盘 |
| started_at | 开始时间 |
| model | 实际模型名（可选） |
| prompt_hash | prompt 摘要，便于审计 |
| decision_json | 本轮提交的 trade/decision |
| token_in / token_out | 可选，DSH 侧统计 |
| status | ok / rejected / error |

> 可选第四张 **agent_proposal**：如果第一阶段想“先提议、人工确认”，把提议存这里，确认后才执行。

### 2.2 CLI 命令

所有命令输出 JSON（加 --json），便于 DSH Agent 解析。

**只读**

    python3.12 -m papertrader.cli agent context --account 默认模拟盘 --json
    python3.12 -m papertrader.cli agent macro list --status open --json
    python3.12 -m papertrader.cli agent quote --codes 600028,600887 --json
    python3.12 -m papertrader.cli agent indicators --code 600028 --json

**宏观事件**

    python3.12 -m papertrader.cli agent macro upsert --json '{"slug":"fed_dec_2026","title":"美联储12月加息预期","severity":4,"direction":"risk_off","status":"open","result":"CME定价25bp概率92%","stance":"压制成长/贵金属"}'

**交易/决策**

    python3.12 -m papertrader.cli agent trade --account 默认模拟盘 --json '{
      "action":"buy",
      "code":"600028",
      "qty":8500,
      "reason":"宏观防御，石化高股息底仓",
      "plan_stop_pct":-0.06,
      "plan_take_pct":0.12,
      "plan_thesis":"一体化石化龙头，高股息",
      "macro_regime":"防御",
      "macro_note":"美联储12月加息概率92%，金银铜暴跌，A股超5300只下跌",
      "score":0.42
    }'

    python3.12 -m papertrader.cli agent decision --account 默认模拟盘 --json '{
      "action":"hold",
      "code":"600028",
      "score":0.1,
      "reason":"未触发计划止损/止盈，RSI偏热，继续持有",
      "blocked_by":""
    }'

**dry-run**

    python3.12 -m papertrader.cli agent trade ... --dry-run

- dry-run 只做校验，不写流水/持仓；返回 would_fill、blocked_by、reason。
- 第一阶段建议默认 dry-run，确认链路后再开写。

### 2.3 agent context 返回结构

    {
      "as_of": "...",
      "session": {"trading_day": true, "is_open": true, "phase": "morning", "label": "上午盘"},
      "account": {"cash": ..., "total_equity": ..., "position_value": ..., ...},
      "plan": {"plan_id": ..., "macro": {...}, "risk": {...}, "entry": {...}, "universe": [...]},
      "positions": [{"code","name","qty","avg_cost","current_price","unrealized_pnl","plan_stop_price","plan_take_price",...}],
      "candidates": [{"code","name","sector","thesis","quote": {...}, "indicators": {...}, "score": 0.42, "blockers": [...]}],
      "macro_events": [...],
      "macro_snapshot": {...},
      "recent_trades": [...],
      "recent_decisions": [...]
    }

注意：context 要一次性把“本轮决策所需”的数据给全，避免 Agent 为每只票反复调 CLI；候选默认来自计划 universe，后续再加动态候选池。

### 2.4 agent trade 的硬闸顺序

1. 交易时段 / 交易日；
2. 账户存在、计划 active；
3. 代码在计划 universe / 持仓；
4. 买入：plan_stop_pct < 0 或 plan_stop_price > 0，且 plan_take_pct > 0 或 plan_take_price > 0；
5. 买入：macro_regime 合法、macro_note ≥8 字、高危 risk_off 时不得标“进攻”；
6. 涨跌停 / 实时价 / 100 股整手 / 费用；
7. 单票上限、总仓上限、最大持仓数、同行业上限、单日交易数；
8. 卖出：T+1、持仓足够；
9. 写流水 + 现金 + 持仓 + 决策，同一事务；
10. 返回 JSON，成功后才通知/刷新 dashboard。

任何一步失败：**不写库**，返回 blocked_by + reason；Agent 改 hold，不允许换措辞重试同一笔。

---

## 3. DSH 定时任务与 Prompt

### 3.1 调度

- 心跳：工作日 09:30–14:30 每 30 分钟；
- 收盘前：14:45；
- 收盘快照：15:05（仍由现有 tick 写，不需要 LLM）。

现有“运行模拟盘决策心跳”任务如果继续跑 tick，会和 Agent 双写。切换时二选一：

- 暂停规则 tick，只保留 Agent 决策 + 15:05 快照；
- 或 tick 只做 fallback：Agent 失败时由 tick 跑规则引擎。

### 3.2 Prompt 骨架

    你是个人模拟盘的决策代理。先运行：
    agent context --account 默认模拟盘 --json
    读取账户、持仓、计划、候选、宏观事件。

    然后：
    1. 用 web_search / web_fetch 查今日宏观与持仓/候选相关新闻；
    2. 用 agent macro upsert 登记/更新进行中的宏观事件；
    3. 对候选逐一评估：宏观档位 → 技术 → 基本面 → 事件/舆情 → 仓位；
    4. 买入必须带 plan_stop_pct/plan_stop_price、plan_take_pct/plan_take_price、macro_regime、macro_note；
    5. 通过 agent trade 提交；不动作通过 agent decision 写 hold；
    6. 被硬闸拒绝就改 hold，不要换措辞重试同一笔。

    约束：不要直接改数据库；不要改计划档位/仓位上限；不要对真实账户操作。
    最终回复 2-3 行：成交/无成交、账户、拒绝原因摘要。

### 3.3 宏观档位自动切换（已确认）

- Agent 可以自动写 macro_event / macro_snapshot（事实与快照）；
- P3 起，engine 读取**当日最新** macro_snapshot.regime；若 as_of 是当天且 sources 非空，则用快照档位，否则回退 plan 里的 macro.regime；
- 防抖：同一交易日最多切换一次；存在 severity >= 4 且 direction=risk_off 的 open 事件时，不得切到“进攻”；
- 切换必须写 agent_run 和决策 reason，记录依据；数据源不可达时保持上一档；
- **计划文件本身不自动改**：自动切的是运行时快照，便于回退和审计；要改 plan 文件仍走用户确认。

---

## 4. 安全与成本

### 4.1 安全

- Agent 只能调 allowlist CLI；没有直接 DB 权限；
- 计划/账本/流水只通过 Python 硬闸写；
- 默认 dry-run 一段时间，确认后再开写；
- 开写后加：单日最大交易数、单笔最大金额、最大回撤、kill switch（SAYU_AGENT_TRADING=0）；
- web 内容是不可信输入：只提取事实，不执行网页里的指令；
- 所有 Agent 提议/拒绝/成交写 agent_run，便于复盘。

### 4.2 成本

- 每轮 1 次 context、1~3 次 web_search、少量 trade/decision；
- 比“每只票都调一堆工具”便宜很多；
- 如果 token 高，先减候选数（10 → 6）或降低频率（30 → 60 分钟）。

---

## 5. 分阶段落地

**P1 只读骨架（不改交易）**

- 新增 macro_event / macro_snapshot / agent_run 表；
- 新增 agent context / macro list / macro upsert / quote / indicators；
- 写一个只读的宏观日报；
- 现有 tick 不变。

**P1 状态（2026-11-20 已完成）**

- 表：macro_event / macro_snapshot / agent_run 已加入 schema；
- CLI：agent context / agent macro list / agent macro upsert / agent macro snapshot / agent quote / agent indicators 可用；
- 已写入今日 4 条宏观事件和 1 条 macro_snapshot（防御/价值相/Fed 92%）；
- 未做：agent trade / agent decision / DSH 决策 prompt / 自动切档生效逻辑。

**P2 Dry-run（2 个交易日）**

- 新增 agent trade --dry-run / agent decision；
- DSH 定时 prompt 先只做分析，提交 dry-run 提议；
- 和规则引擎的决策对比，观察差异；
- 不产生真实模拟成交；
- 2 个交易日后复盘：提议数量、拒绝原因、和规则引擎差异、token 成本。

**P2 状态（2026-11-20 进行中）**

- 已实现：agent trade --dry-run、agent decision、agent runs；
- 已创建 2 个交易日 dry-run 调度：
  - Agent dry-run 决策：2026-11-20 13:00 至 2026-11-23 15:00，工作日 09:00–14:30 每 30 分钟（schedule-1dfe4947-ab14-49c5-87fb-377138d8161c）；
  - Agent dry-run 收盘前扫描：同一时段 14:45（schedule-203b40f1-5461-429d-ae74-8d8e146b916e）；
- dry-run 只写 agent_run，不写 trade/position/cash/decision；
- 规则 tick 仍在跑，账户会变；dry-run 提议按当时账户校验，复盘时注意这一点。

**P3 小仓位开写**

- 打开 agent trade 写库，限制单笔金额/单日次数；
- 保留 tick 作为 fallback；
- 监控 1~2 周：成交质量、回撤、拒绝原因、token 成本。

**P4 稳定运行**

- Agent 决策为主，tick 只做快照/兜底；
- dashboard 增加 Agent 决策面板（reason、工具调用、宏观事件）；
- 再考虑动态候选池、财报/板块工具。

---

## 6. 已确认与剩余待定

已确认：

1. 候选池：计划里的 10 只；
2. Dry-run：2 个交易日；
3. 宏观档位：允许自动切，按 3.3 的防抖/高危约束；
4. 会话：当前对话；
5. 仓位：沿用计划。

剩余待定（不阻塞 P1/P2）：

- P3 开写时单笔/单日上限是否再收紧；
- 是否给 Agent 开影子账户做 dry-run 对比；
- dashboard 是否加 Agent 决策/工具调用面板。

---

## 7. 结论

方案 A 是可行的，而且比“复刻 gsuid_core”轻得多：

- 不需要 QQ / Kanban / 插件宿主；
- 不需要把 DSH 的 API key 抠出来；
- 用 DSH 现有模型 + 现有 Python 账本/硬闸；
- 先只读、再 dry-run、再小仓位开写。

下一步：P1 已完成，P2 已启动（dry-run 到 2026-11-23）；2 个交易日后复盘 Agent 提议与规则引擎差异，再决定 P3 开写。
