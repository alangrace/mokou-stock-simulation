# SayuStock 模拟盘「自主交易」机制拆解与复刻评估

> 分析对象：[KimigaiiWuyi/SayuStock](https://github.com/KimigaiiWuyi/SayuStock)  
> 本地分析版本：commit 64db0c1（2026-11-18），插件版本 0.9  
> 分析范围：stock_papertrade / stock_agent / 相关 SQLModel 与 Kanban 调度  
> 结论一句话：**它不是"让 LLM 随便下单"，而是"LLM 在计划与硬闸划定的笼子里做选择，账本与撮合在笼子外确定性执行"。**

---

## 0. 先给结论

SayuStock 的模拟盘自主交易，核心不是某一段"神级 prompt"，而是四层工程约束叠加：

1. **计划层**：每只票买入时必须写下入场价、止损、止盈、一句话逻辑、宏观档位；没有数值止损直接拒单。
2. **决策层**：LLM 决策代理按固定 Phase 走完"宏观定档 → 候选池轮换 → 持仓计划回看 → 技术/基本面/事件 → 五维评分 → 风控"，每轮 30 分钟。
3. **执行层**：撮合、费用、100 股整手、涨跌停、T+1、交易时段、实时价偏差、单日交易次数、仓位上限全部是**函数硬校验**，不通过就拒绝落库。
4. **账本层**：成交在同一个数据库事务里写流水 + 改现金 + 改持仓；决策（含 hold）全量落库；成交由系统确定性播报，决策推理永不主动进群。

所以"复刻"是可行的，而且不需要复刻它的 LLM 才能验证骨架：**把决策层换成规则引擎，只要保持同样的计划字段、硬闸和账本契约，就能跑出可审计的模拟盘。** 本仓库里的 [sayu-paper-trader](../sayu-paper-trader/) 就是按这个思路做的独立复刻，并已在 2026-11-18 盘中完成回放与实盘成交（13:00–13:15 三笔买入）。

---

## 1. 它到底"自主"在哪

用户能做的只有：建盘、改名、启停、换策略、订阅成交播报、只读查询。  
用户**不能**指挥买哪只、改单笔仓位、给盘加自选当交易指令。

自主性体现在：

- 每个交易日 09:30–11:30 / 13:00–15:00，每 30 分钟自动醒一次；
- 自己轮换候选池（蓝筹底仓 + 行业/概念/热股/涨跌榜/成交额/高 ROE/新闻多源）；
- 自己读宏观事件表、财报、技术指标、新闻；
- 自己决定 buy / sell / hold，自己写决策日志；
- 成交后系统自动往订阅群推一行成交冒泡，**决策推理不播报**。

这套设计的价值观很明确：**把"判断"交给模型，把"能不能成交、记什么账、推什么消息"交给确定性代码。**

---

## 2. 运行骨架：两棵 Kanban 树 + 四个周期任务

> Kanban 是 gsuid_core 的任务树 / 能力代理调度框架：每个节点可以绑定 agent profile、子任务、cron、owner 与 scope_key。模拟盘借它把"建账 / 决策 / 快照 / 复盘"挂成可恢复的周期任务。

建盘入口是 `模拟盘初始化` / `模拟盘创建`，最终落到 [commands.py](../SayuStock/SayuStock/stock_papertrade/commands.py) 的 `_setup_papertrade_kanban_trees`：

| 树 | 用途 | 关键配置 |
|---|---|---|
| init 树 | 建账后自检一次 | leaf-root，`root_agent_profile=papertrade_setup_agent` |
| period 树 | 周期托管容器 | ROOT **不设** recurring_trigger；4 个子任务各自挂 cron |

period 树的 4 个子任务（[commands.py L151-L190](../SayuStock/SayuStock/stock_papertrade/commands.py#L151-L190)）：

| 子任务 | cron | 作用 |
|---|---|---|
| decision | `0,30 9-11,13-15 * * 1-5` | 每 30 分钟决策心跳 |
| snapshot | `5 15 * * 1-5` | 收盘后写当日净值快照 |
| monthly_report | `0 9 1 * *` | 月初复盘 |
| pool_refresh | `15 10,14 * * 1-5` | 独立轮换候选池，防止决策轮"只嚼同一批" |

这里有一个很值得复刻的踩坑记录：**ROOT 不能设 recurring_trigger**。一旦 ROOT 在创建时就是 armed，框架的 `execute_ready_tasks` 会早退，4 个子任务永远不会被 arm 到 APScheduler，表现为"开盘后 AI 心跳从未触发"。正确做法是 ROOT 保持非周期，创建后 `kick_root` 一次，让 `_maybe_arm_recurring_subtasks` 把子任务独立挂到调度器。

调度到点后，`recurring._fire_subtask_template` 克隆一个执行实例，再 `kick_root(root_task_id)` 跑这一轮。

---

## 3. 一次决策心跳的完整链路

决策代理的完整 prompt 在 [stock_agent/__init__.py L392-L702](../SayuStock/SayuStock/stock_agent/__init__.py#L392-L702)。把它的 Phase 抽出来，就是一条非常清晰的流水线：

### Phase -1：交易时段守卫（最省 token 的早退）

每轮第一件事调 `stock_is_trading_day`。非交易日、开盘前、午休、收盘后，直接输出 `<<NO_BROADCAST>>` 退出，**不查行情、不轮换候选、不写库**。

对应的执行层还有第二道：撮合器 `PaperTradeExecutor.match` 在非交易时段直接拒单（[trade_executor.py L145-L153](../SayuStock/SayuStock/stock_papertrade/trade_executor.py#L145-L153)）。

### Phase 0：候选池轮换（防锚定）

每轮必调 `papertrade_candidate_refresh()`：

- 清过期；
- 淘汰最旧的几只 auto 候选（持仓、群友关注永不淘汰）；
- 补蓝筹底仓；
- 补动量标的（行业/概念/热股/涨跌榜/成交额/高 ROE/新闻，多源轮询）；
- 入池前过滤涨停/过热；
- 目标约 16 只。

这不是"池子少于 3 只才刷"，而是**每轮新陈代谢**。原项目在 2026-07-02 修过一次：旧逻辑"池 <3 才刷"导致池子填满 5 只后连续数日冻结，每 30 分钟嚼同一批，账户长期空仓。

### Phase 1：账户与持仓

- `papertrade_account_query`：现金 / 模式 / 真·总资产（现金 + 持仓市值）。
- `papertrade_position_list`：现价、市值、浮盈、`quote_source`，以及**持仓入场计划字段**：
  `plan_stop_price` / `plan_stop_pct` / `plan_take_price` / `plan_thesis`。

### Phase 1.6：持仓入场计划回看（卖出一致性）

这是"计划"真正生效的地方。对每只持仓，在评估卖/持之前先读计划：

- 有止损/止盈价 → **先按原计划核对现价**；
- 未触发原计划止损，却因为"本轮重新算了一个完全不同的止损位"而卖 → **禁止**；
- 要收紧/放宽原计划，reason 必须写清：原计划是什么 → 新证据是什么 → 为何覆盖；
- 模式默认止损（balanced -8% / aggressive -12% / conservative -5%）只在没有可解析计划时兜底，或作为"不得更松"的硬底线。

### Phase 2：候选池合入

持仓 + 群友关注 + agent_pool + 板块/热图/新闻 = 本轮候选全集。即使持仓只有 1 只，也必须把池子里的标的拉进来评估，不能只盯持仓。

### Phase 3：宏观定档（先于个股，硬门）

> 宏观事件表是一张全局表（不分盘），由独立的宏观心跳每天 08:40 / 12:40 / 20:40 联网复核；决策心跳只负责读表和按表定档。

1. 读 `macro_event_list(status="open")`：每件事有 title / severity / direction / result / stance / stale。
2. 有 `severity>=4` 且 `risk_off` 的进行中事件 → 本轮 buy 不得标"进攻"（系统硬闸会拒）。
3. 读快讯、大盘、行业热图、VIX，回答宏观三问：
   - 钱多还是钱少（油价、长端美债、美元、国内国债、PPI）；
   - 成长相还是价值相（利率、楼市）；
   - 有什么大事在进行（事件表 + 最新进展）。
4. 定档：进攻（总仓 ≤80%、单票 ≤15%）/ 中性（≤60%、≤10%）/ 防御（≤30%、≤5%，只买高股息、公用、必需消费、上游资源）。

档位是**仓位上限**，个股逻辑再好也不能越档。

### Phase 4：个股深度分析（技术 + 基本面 + 事件，缺一不可）

- 技术：日线定方向（120 日）+ 60 分钟/30 分钟定时机；MA/MACD/RSI/KDJ/CMF/BOLL/CCI/BBI；要求多周期共振。
- 基本面：财报按日复用，不每 30 分钟对全池重扫；只有新票、拟买、或报告期陈旧才新拉。
- 事件：对全部持仓 + 拟买卖标的至少 1 次 `web_search`；搜到关键 URL 再 `web_fetch`。
- 禁止：只用单一技术指标下单；零次 web_search 却写"事件驱动"；无触发条件对全池刷财报。

### Phase 5：五维评分与决策

在模型脑内自评（没有独立 score 工具）：

- 宏观/事件 ≈ 30%
- 技术 ≈ 35%
- 基本面 ≈ 25%
- 波动率调节 ≈ ±10%

得到 score ∈ [-1, 1]，再结合持仓/模式做 buy/sell/hold，并过风控：单票仓位、日交易次数、止损、回撤熔断、现金缓冲、最大持仓数。

### Phase 6：撮合与落库（只走流水）

1. `papertrade_match_order`：按**撮合此刻的实时行情价**成交；传入 price 只是参考价；自动拉昨收判断涨跌停；非交易时段拒单。
2. `papertrade_trade_insert`：同一事务写流水 + 改现金 + 改持仓；buy 必须带止损计划；T+1 拦截；实时价偏差 >3% 拒绝。
3. `papertrade_decision_insert`：写决策日志；buy 必须带宏观档位与依据。

**禁止** `trade_insert` 和 `position_upsert` 并行：流水被闸拒绝时 upsert 仍会建仓，造成"无流水幽灵仓、净值虚高、群里不播报"。

### Phase 7：hold 也写决策

hold 不是"什么都不做"：必须写一条 `papertrade_decision_insert`，reason 里写清为什么不动、相对入场计划距止损/止盈还有多少。

### Phase 8：更新 `last_decided_at`

### 最终输出：只输出 `<<NO_BROADCAST>>`

这是播报纪律的铁律：

- 成交播报由 `papertrade_trade_insert` 成功那一刻**系统确定性**推送，buy/sell 都推、永不遗漏、永不重复；
- 决策推理、候选池汇总、hold 理由**永不主动进群**，但全部落库，主人 @ 机器人时用 `papertrade_decision_list` / `papertrade_trade_list` 读库回答；
- 代理最终消息除了 `<<NO_BROADCAST>>` 不许有任何其它字符，多一个字就会被框架当成播报内容推给群。

早期版本让代理"最终消息二选一（成交行 / NO_BROADCAST）"，结果 LLM 时而漏发成交行、时而把推理原样播到群，**随机不可控**。现在改成"成交由系统冒泡 + 决策一律 NO_BROADCAST"，才稳定下来。

---

## 4. 决策代理的"工具契约"

工具分三层（[strategies/tools.py](../SayuStock/SayuStock/stock_papertrade/strategies/tools.py)）：

| 层 | 工具 | 谁能用 |
|---|---|---|
| 账本只读 | account_query / position_list / trade_list / decision_list / watchlist_list / agent_pool_list | 主人格 + 决策代理 |
| 账本写 | decision_insert / trade_insert / position_upsert / match_order / candidate_refresh | 仅 `papertrade_*_agent`（`visible_when`） |
| 研究 | stock_indicators / stock_financials / PB / 大盘 / 行业 / 榜单 / 新闻 / VIX / macro_event_* / web_search / web_fetch | 按策略挂载 |

两个关键设计：

0. **代理边界**：研究型代理（stock_agent / 研报 / 宏观）只做研究，**严禁下单**；只有 `papertrade_*` 决策/建账代理能拿到写工具。这是"研究可以放开、交易必须收口"的分层。
1. **写工具不看用户传的盘名**：一律按当前任务树的 `root_task_id` 反查账户。传错盘名只会被纠正/拒绝，写不到别的盘。这是防串账的硬设计。
2. **策略声明自己的研究工具**：多因子盘挂宏观/财报/榜单/新闻；量能盘只挂 `papertrade_volume_scan` + 日 K 指标，避免每轮白烧财报/榜单 token。

---

## 5. "计划"在代码里到底是什么

### 5.1 买入时的结构化入场计划

买入落库时，`indicators` / `snapshot` JSON 顶层必须包含（[ai_tools.py L947-L959](../SayuStock/SayuStock/stock_papertrade/ai_tools.py#L947-L959)）：

| 字段 | 含义 | 硬性 |
|---|---|---|
| `plan_entry` | 入场价 | 建议 |
| `plan_stop_pct` | 止损幅度（负小数，如 -0.08） | **二选一必填** |
| `plan_stop_price` | 止损价（>0） | **二选一必填** |
| `plan_take_pct` / `plan_take_price` | 止盈 | 可选 |
| `plan_thesis` | 一句话逻辑 | 建议 |
| `macro_regime` | 进攻/中性/防御 | **多因子盘必填** |
| `macro_note` | ≥8 字宏观依据 | **多因子盘必填** |

止损字段接受别名 `stop_pct` / `stop_price` / `stop_loss_pct` 等；嵌套在 `indicators` 里的字段会摊平；非法 JSON 降级为空 dict，尾部多余字段忽略。

### 5.2 计划如何进入持仓

`PaperTradeRepo.append_with_cash_update` 在成交的同一事务里，把 snapshot 里的 `plan_stop_price` / `plan_stop_pct` / `plan_take_price` / `plan_thesis` 写进 `SayuPaperPosition`（[db.py L695-L752](../SayuStock/SayuStock/stock_papertrade/db.py#L695-L752)）。

之后 `papertrade_position_list` 直接返回这些字段，决策代理卖出前不必再翻大段 snapshot。

### 5.3 计划一致性

这是"计划"从"一段文字"变成"可执行约束"的关键：

- 买入时写计划；
- 持有期间每轮回看计划；
- 卖出时必须对照原计划：触发原止损 / 触发原止盈 / 未触计划但因…覆盖 / 无历史计划改用模式默认止损；
- 不允许每轮重新发明一套与买入时无关的止损叙事。

---

## 6. 硬闸：模型不能越过的红线

### 6.1 策略硬闸 `gate_buy`

多因子盘（[strategies/multi_factor.py L87-L94](../SayuStock/SayuStock/stock_papertrade/strategies/multi_factor.py#L87-L94)）：

```python
def gate_buy(self, params, gate):
    ind = dict(gate.indicators)
    if not indicators_have_entry_stop(ind):
        return "⚠️ buy 须在 indicators/snapshot JSON 写入 plan_stop_pct(<0) 或 plan_stop_price(>0) ..."
    return macro_gate_reason(ind)
```

宏观硬闸（[strategy.py L132-L163](../SayuStock/SayuStock/stock_papertrade/strategy.py#L132-L163)）：

- `macro_regime` 必须是三档之一；
- `macro_note` ≥8 字；
- 表内有 severity≥4 的 risk_off 进行中事件时，`macro_regime=进攻` 直接拒。

量能盘则用**函数**判定顶底和放量，覆盖 LLM 传入的值：近 5 年月 K 分位粗筛 → 近 60 日 K 分位 + 放量确认；不满足就拒单。顶底不是模型看图说"这是底部"。

### 6.2 执行层硬闸

| 闸 | 规则 | 位置 |
|---|---|---|
| 交易时段 | 非交易日 / 非 09:30-11:30、13:00-15:00 拒单 | trade_executor.match |
| 实时价 | 拿不到实时价 → 拒绝，不允许按旧价成交 | trade_executor.match / record_trade |
| 价格偏差 | 传入 price 与实时价偏差 >3% → 拒绝 | trade_executor.record_trade |
| 涨跌停 | 主板 ±10%、ST ±5%、科创/创业 ±20%、北交所 ±30%；达到名义幅度 90% 即拦截；涨停不追、跌停不割 | matcher |
| 整手 | 100 股整手，向下取整 | matcher.round_lot |
| 费用 | 佣金 max(成交额×0.025%, 5) + 卖出印花税 0.05% | matcher.calc_fee |
| 现金/持仓 | 买入现金不足自动降档；卖出超过持仓截断/拒绝 | matcher.match_order |
| T+1 | 当日买入股数当日不可卖 | trade_executor.record_trade |
| 单日交易 | balanced 6 / aggressive 12 / conservative 3 | strategy.MODE_RULES |
| 仓位 | 单票上限 / 最大持仓数 / 最低现金 / 回撤熔断 | strategy |
| 行业 | 本轮按计划控制同行业只数（复刻版新增） | 复刻版 |

注意"涨跌停 90% 拦截"的细节：主板普通股涨幅到 9% 就按触及涨停处理，ST 4.5%，科创/创业 18%，北交所 27%。原因是低价股涨停价四舍五入后实际涨幅可能只有 +9.7%，且封板前后有跳动。

### 6.3 写入鉴权

`papertrade_trade_insert` / `decision_insert` 会检查 `deny_write_reason(root_task_id, account)`，只有当前任务树对应的盘能写。这是为了防止多盘并发时把 A 盘的成交写到 B 盘。

---

## 7. 账本：8 张表与原子成交

数据模型在 [utils/database/papertrade_models.py](../SayuStock/SayuStock/utils/database/papertrade_models.py)：

| 表 | 作用 | 关键字段 |
|---|---|---|
| SayuPaperAccount | 命名盘 | name / strategy_id / cash / initial_cash / principal / mode / enabled / kanban_root_id |
| SayuPaperPosition | 每盘每票一行 | qty / avg_cost / last_quote_price / **plan_stop_price / plan_stop_pct / plan_take_price / plan_thesis** |
| SayuPaperTrade | append-only 流水 | side / price / qty / amount / fee / realized_pnl / snapshot / decision_id |
| SayuPaperDecision | 决策日志 | action / score / reason / indicators / blocked_by / trade_id |
| SayuPaperSnapshot | 每日净值 | trade_date / cash / position_value / total_equity / day_pnl / total_pnl |
| SayuPaperWatchlist | 群友关注 | 跟盘走，不跟群走 |
| SayuPaperAgentPool | AI 候选池 | reason / priority / expires_at |
| SayuPaperBroadcastTarget | 播报目标 | 一个盘 → 任意多个群 |

### 7.1 原子成交

`append_with_cash_update` 在同一个 session 里：

1. 校验 side / 账户存在 / 卖出不超过持仓；
2. 写流水；
3. `cash += cash_delta_for_fill(side, amount, fee)`；
4. sell 时 `principal += realized_pnl`；
5. 按成交算新 qty / avg_cost；buy 时把 snapshot 里的计划字段写进持仓；
6. qty=0 删除持仓；
7. 一次 flush。

`realized_pnl` 只进 principal / 卖出播报，**不再加进现金**——旧实现把盈亏加进现金，等于记了两遍。

### 7.2 双向对账与自愈

原项目为此写了不少"自愈"逻辑（holiday_heal / admin 对账）：

- 持仓与流水**双向对账**：补缺仓 + 删幽灵仓；
- 遍历范围必须包含"只有持仓、零流水"的盘；
- 逻辑必须排在 `if not dirty` 早退之前；
- 非交易日成交要能按交易日历回滚/重建；
- 报价缓存 60s TTL，失败 5s TTL；`quote_source` 标 live/db/cost。

这些是"长期跑"才会暴露的问题，复刻时如果只写 happy path，跑几天账本就会歪。

---

## 8. 撮合与 A 股规则

核心常量（[matcher.py](../SayuStock/SayuStock/stock_papertrade/matcher.py)）：

```python
COMMISSION_RATE = 0.00025   # 万 2.5
COMMISSION_MIN = 5.0        # 最低 5 元
STAMP_TAX_RATE = 0.0005     # 卖出印花税万 5
LOT_SIZE = 100              # 100 股整手
LIMIT_BLOCK_RATIO = 0.9     # 名义涨跌停的 90% 即拦截
```

板子识别：

- 688/689、300/301 → ±20%
- 83/87/920 → ±30%
- 主板 ST/*ST → ±5%
- 其余主板 → ±10%

主板拿不到证券名称时**拒绝撮合**，因为无法判断 ST（±5% vs ±10% 差一倍）。这个细节很值得学：宁可不出价，也不能把 ST 的涨跌停悄悄放宽。

交易日历不是手写假期表，而是用上证指数 `1.000001` 的日 K / 分时自证：

- 周末 → 非交易日；
- 上证日 K 里"最后一根 bar 之前没有 bar 的工作日" → 休市日；
- 交易时段开始满 3 分钟后，分时最后一点仍停在更早的日子 → 非交易日；
- 内置 holidays 表只在首次启动/网络不可达时兜底。

---

## 9. 调度、播报与"不打扰"

### 9.1 成交播报

`papertrade_trade_insert` 成功 → `broadcast.broadcast_fill` → `emit_proactive_message`：

```text
🟢 买入 贵州茅台(600519) 100 股 @¥1450.72
原因：宏观:中性|...｜计划入场：...
```

卖出带已实现盈亏：

```text
🔴 卖出 ...（+¥1,234.56）
原因：触发计划止盈价 ...
```

### 9.2 决策推理不播报

决策代理最终只输出 `<<NO_BROADCAST>>`，框架 `_strip_no_broadcast` 跳过 relay/notify。想看推理，@ 机器人用 `papertrade_decision_list` 查库。

### 9.3 为什么这样设计

早期"让 LLM 自己决定要不要播报"不可控：漏发、重复发、把推理原样发群。改成"系统确定性播报成交 + 推理只落库"后，才有稳定的群体验。

---

## 10. 复刻可行性评估

### 10.1 哪些能复刻

| 模块 | 复刻难度 | 说明 |
|---|---|---|
| 账户/持仓/流水/决策/快照表 | 低 | SQLite/SQLModel 即可 |
| 原子成交（流水+现金+持仓） | 低 | 一个事务搞定 |
| A 股费用/整手/涨跌停/T+1 | 低 | 纯函数 |
| 交易时段/交易日历 | 中 | 可用静态表起步，生产版用指数自证 |
| 计划字段与 gate_buy | 低 | JSON 字段 + 校验 |
| 候选池轮换 | 中 | 需要多路行情源；可先做蓝筹+动量 |
| 定时心跳 | 中 | cron/APScheduler/守护进程均可 |
| 成交播报 | 低 | 控制台/Webhook/群机器人 |
| 自愈/对账 | 中高 | 长期跑才需要，但必须有 |
| LLM 决策代理 | 高 | 需要模型、工具注册、上下文预算、并发控制 |
| gsuid_core Kanban/多平台适配 | 很高 | 不建议为了复刻模拟盘而重写 |

### 10.2 三条复刻路线

**路线 A：原样部署插件**

- 安装 gsuid_core + SayuStock + 一个 LLM provider；
- 发 `模拟盘初始化`；
- 优点：功能完整、少写代码；
- 缺点：依赖重、需要 QQ/微信等适配器、LLM 成本、调试链路长。

**路线 B：独立最小复刻（本次采用）**

- 只保留"计划 → 行情/指标 → 硬闸 → 撮合 → 账本 → 播报/报表"；
- 决策层用规则引擎替代 LLM；
- 优点：可离线验证、可审计、零 LLM 成本、能回放；
- 缺点：选股能力弱于 LLM，需要自己维护策略。

**路线 C：LLM 增强的独立复刻**

- 沿用 B 的账本/撮合/硬闸；
- 把 planner 换成一个 LLM 工具调用循环（同样的 `match_order` / `trade_insert` / `decision_insert` 契约）；
- 优点：保留可审计骨架，同时获得 LLM 的选股/事件理解能力；
- 建议：先跑通 B，再只替换 planner，不要一开始就把 LLM 和账本耦合。

### 10.3 复刻时必须保留的 9 条设计

1. **计划先于交易**：买入必须写止损/止盈/逻辑；没有计划不允许开仓。
2. **硬闸 > 提示词**：止损、仓位、T+1、涨跌停、交易时段必须是函数，不能只写在 prompt 里。
3. **原子账本**：流水 + 现金 + 持仓同事务；禁止"先建仓后补流水"。
4. **决策全量留痕**：hold 也要写；blocked_by 记录为什么没成交。
5. **成交确定性播报**：成交由系统推，推理不主动推。
6. **交易时段守卫**：非交易时段不产生任何成交。
7. **防锚定**：每轮轮换候选池，不能选完一批就永远嚼同一批。
8. **计划一致性**：卖出必须回看买入时写的计划，不能每轮重编止损。
9. **报价新鲜度**：拿不到实时价就拒绝成交；缓存要标 live/db/cost。

---

## 11. 本次独立复刻的实现

代码在 [sayu-paper-trader](../sayu-paper-trader/)，纯标准库（Python 3.12），不依赖 gsuid_core、不依赖 LLM。

### 11.1 目录

```text
sayu-paper-trader/
├── papertrader/
│   ├── market.py       # 东财快照/日K/1分钟K，失败时腾讯快照/日K回退，TTL 缓存
│   ├── calendar.py     # A 股交易日/交易时段
│   ├── rules.py        # 费用/整手/涨跌停/T+1/仓位
│   ├── indicators.py   # MA/MACD/RSI/ATR/量比/20日分位
│   ├── ledger.py       # SQLite 账本（6 张表 + 计划 + 播报）
│   ├── plan.py         # 入场计划校验 + 宏观/止损硬闸
│   ├── planner.py      # 规则化"决策代理"：评分/入场/退出/预算
│   ├── engine.py       # 一轮心跳：先卖后买、落库、播报
│   ├── report.py       # 文本/Markdown 状态报告
│   └── cli.py          # init/plan-load/preview/run/daemon/status/screen
├── plans/entry-20261118-defensive.json
├── data/paper.db
└── reports/*.md
```

### 11.2 与原文的映射

| SayuStock | 复刻版 | 说明 |
|---|---|---|
| SayuPaperAccount | account | 命名盘、现金、本金、策略、模式 |
| SayuPaperPosition | position | 含 plan_stop_* / plan_take_* / plan_thesis |
| SayuPaperTrade | trade | append-only，成交时原子写 |
| SayuPaperDecision | decision | hold 也写，blocked_by 记录拦截 |
| SayuPaperSnapshot | snapshot | 按 trade_date 幂等 upsert |
| 决策代理 prompt | planner.score_candidate / entry_blockers / exit_signal | 规则化替身 |
| gate_buy | plan.validate_buy | 止损 + 宏观档位 + 防御名单 |
| match_order | rules + ledger.record_fill | 费用/整手/涨跌停/T+1 |
| cron 心跳 | cli.daemon | 等到开盘，按 interval 跑 |
| 成交冒泡 | ledger.broadcast + stdout | 控制台播报 |
| papertrade_* 工具 | CLI 子命令 | 可脚本化/可被 LLM 调用 |

### 11.3 复刻版的关键约束

- **宏观档位是仓位上限**：进攻 80%/15%，中性 60%/10%，防御 30%/5%；计划里的总仓/单票上限会与宏观档位取更小值。本计划当前设为总仓 80%、单票 10%，所以进攻档正好 8×10%=80%；中性会被压到 60%/10%，防御压到 30%/5%。
- **防御档只买防御名单**：银行/建筑/公用/石化/煤炭/食品/医药等；成长股在防御档直接 hold。
- **行业上限**：同一行业最多 1 只（可配）。
- **单轮入场名额**：默认 3 只，避免一轮打满。
- **计划阈值**：score ≥ 0.2、站上 MA20、RSI6 30~75、当日振幅 ≤7%。
- **计划止损/止盈**：默认 -6% / +12%，可逐票覆盖。
- **T+1**：当日买入当日不可卖；卖出前查当日买入流水。
- **回放模式**：`--replay --as-of "YYYY-MM-DD HH:MM"` 用 1 分钟 K 线取当时价格，日 K 只用截至前一日的数据，避免未来函数。

### 11.4 可视化与定时

复刻版额外做了两件"长期跑"必需的事：

- **可视化**：`dashboard` 生成自包含的 `reports/dashboard.html`，含总资产/现金/持仓/净值曲线/成交/决策/计划；浏览器每 30 秒自动刷新，无 CDN、无后端。
- **成交通知**：实时成交自动弹 macOS 提示（一轮多笔合并成一条）；优先 terminal-notifier，权限被拒时回退 Tkinter 右上角浮层（10 秒自动消失），回放不弹。
- **定时**：`tick` 是给 DSH/cron/launchd 调用的单轮入口——交易时段跑一轮，收盘后写净值快照，开盘前/午休/节假日直接跳过，并顺手刷新 dashboard。
- **当前 DSH 任务**：周一到周五 09:00–14:30 每 30 分钟触发一次（09:00 由 `tick` 判为跳过），14:45 收盘前扫描，15:05 写收盘快照。

---

## 12. 实跑结果（2026-11-18）

### 12.1 当时市场

- 时间：11:35，A 股午休（11:30–13:00）。
- 上证指数：3570.21（-0.40%）。
- 上证 MA20 = 3647.5，MA60 = 3798.7，RSI6 = 13.3，20 日 -3.85%。
- 结论：指数在 MA20/MA60 下方，技术面偏弱，计划定档 **防御**。

### 12.2 午休预览（不成交）

命令：

```bash
python3.12 -m papertrader.cli preview --account 默认模拟盘
```

结果（节选）：

| 代码 | 名称 | score | 决策 | 拦截 |
|---|---|---:|---|---|
| 601166 | 兴业银行 | +0.37 | buy（挂起） | market_closed，拟买 2500 股 @19.99 |
| 600036 | 招商银行 | +0.31 | hold | 银行行业已达上限 1 只 |
| 601668 | 中国建筑 | +0.28 | buy（挂起） | market_closed，拟买 10500 股 @4.72 |
| 600900 | 长江电力 | +0.27 | hold | RSI6=84 > 75 |
| 600028 | 中国石化 | +0.27 | hold | RSI6=80 > 75 |
| 600519 | 贵州茅台 | +0.28 | hold | 防御档不买非防御名单 |
| 002415 | 海康威视 | +0.36 | hold | 防御档不买非防御名单 |

这说明：**计划、行业上限、RSI 上限、宏观防御名单都在生效，不是"评分高就买"。**

### 12.3 10:30 历史回放（真成交）

为了验证完整链路，另开一个"回放盘"，用 1 分钟 K 线回到 10:30 成交：

```bash
python3.12 -m papertrader.cli run --account 回放盘-20261118 \
  --replay --as-of "2026-11-18 10:30" --snapshot
```

成交：

| 代码 | 名称 | 股数 | 成交价 | 金额 | 费用 |
|---|---|---:|---:|---:|---:|
| 600036 | 招商银行 | 1,100 | 43.310 | 47,641.00 | 11.91 |
| 601668 | 中国建筑 | 10,600 | 4.710 | 49,926.00 | 12.48 |
| 600028 | 中国石化 | 8,400 | 5.920 | 49,728.00 | 12.43 |

- 现金：852,668.18
- 持仓市值（11:41 现价）：147,452.00
- 总资产：1,000,120.18
- 浮盈：+120.18
- 已实现：0.00
- 手续费合计：36.82

每笔买入都写入了持仓计划，例如招商银行：

- 入场价 43.310
- 止损价 40.711（-6%）
- 止盈价 48.507（+12%）
- 逻辑：零售银行龙头，ROE 与资产质量领先；与兴业二选一，按评分择优

### 12.4 实盘执行结果（13:00–13:15）

默认盘在 11:44 启动守护进程，13:00 开盘后自动执行：

```bash
python3.12 -m papertrader.cli daemon --account 默认模拟盘 \
  --interval 600 --until 13:15
```

13:00 第一轮按实时价成交 2 笔：

| 代码 | 名称 | 股数 | 成交价 | 金额 | 费用 |
|---|---|---:|---:|---:|---:|
| 601166 | 兴业银行 | 2,500 | 19.990 | 49,975.00 | 12.49 |
| 601668 | 中国建筑 | 10,500 | 4.720 | 49,560.00 | 12.39 |

13:10 第二轮：无新成交。600887 伊利股份 RSI6=78 仍高于计划上限 75；601166 / 601668 被 A 股 T+1 锁定，当日不可卖。

13:14:59 第三轮：600887 的 RSI6 回落到 75 以下（score=0.24），触发第三笔计划买入：

| 代码 | 名称 | 股数 | 成交价 | 金额 | 费用 |
|---|---|---:|---:|---:|---:|
| 600887 | 伊利股份 | 1,500 | 32.330 | 48,495.00 | 12.12 |

13:15 daemon 退出前快照（非收盘价，盘中快照）：

| 现金 | 持仓市值 | 总资产 | 当日盈亏 | 已实现 | 持仓数 |
|---:|---:|---:|---:|---:|---:|
| 851,933.00 | 147,725.00 | 999,658.00 | -342.00 | 0.00 | 3 |

三笔成交合计手续费 37.00 元；13:15 盘中总资产较初始 100 万少 342 元，主要来自手续费与持仓浮亏。

17:33 用 `tick` 补写收盘快照：三只票尾盘回升，总资产 **1,000,003.00**，当日 **+3.00**。SQLite 的当日快照按 `trade_date` 幂等 upsert，所以最终保留的是收盘口径；13:15 的 -342 是 daemon 退出前的盘中快照，只用于观察日内变化。

T+1 生效后，三只票当日均不可卖，下一次可卖窗口是 2026-11-19 开盘后。

> 注意：实盘执行用**实时价**，不是回放价；13:00 的 601166/601668 与 13:14:59 的 600887 都经过了计划阈值、行业上限、RSI 区间和 T+1 的实时复核。13:10 没成交、13:15 前 600887 才成交，正好说明"计划约束 + 实时数据"在起作用。

### 12.5 如何查看

```bash
cd sayu-paper-trader
python3.12 -m papertrader.cli status --account 默认模拟盘 --md live-status.md
python3.12 -m papertrader.cli status --account 回放盘-20261118 --md replay-status.md
```

报告包含：账户总览、持仓（含计划止损/止盈）、入场计划表、最近成交、最近决策（含 hold/挂起）、成交播报记录。

---

## 13. 局限与后续

### 13.1 复刻版的局限

- **没有 LLM 选股**：规则评分只看技术面 + 少量计划过滤，事件/舆情/财报维度弱；这是为了先验证骨架。
- **没有真实盘口**：按最新价/1 分钟收盘价成交，不模拟排队、滑点、部分成交；涨停/跌停只做拦截，不模拟封单。行情以东财为主，失败时腾讯快照/日 K 回退；两者都不可达时拒绝成交。
- **没有多账户并发锁**：SQLite 单写者，适合单机低频；多盘高并发需要换 PostgreSQL/加锁。
- **没有自动对账**：目前只有 T+1/持仓校验；长期跑需要补"流水 vs 持仓"双向对账、非交易日脏数据修复。
- **交易日历是静态表**：2026 年可用；跨年/临时休市需要接指数自证或更新表。
- **没有群播报**：控制台/DB 播报；接 QQ/飞书/Telegram 需要适配器。

### 13.2 建议的下一步

1. **先让 B 跑一周**：每天 13:00 和 15:05 各跑一次，观察计划止损/止盈、T+1、行业上限是否按预期工作。
2. **加 LLM planner**：把 `planner.score_candidate` 换成"LLM 读指标/新闻 → 返回同样 JSON 契约"；硬闸和账本不动。
3. **加宏观事件表**：一张全局表，记录关税/地缘/央行/油价事件，buy 时强制写 `macro_regime` / `macro_note`。
4. **加对账任务**：每天收盘后比对 Σbuy − Σsell 与持仓 qty，发现幽灵仓/缺仓自动修复并告警。
5. **加 Web 仪表盘**：账户净值曲线、持仓、决策时间线、成交播报；数据直接读 SQLite。

---

## 14. 参考

- 仓库：[KimigaiiWuyi/SayuStock](https://github.com/KimigaiiWuyi/SayuStock)
- 决策代理 prompt：[stock_agent/__init__.py L392-L702](../SayuStock/SayuStock/stock_agent/__init__.py#L392-L702)
- 建盘与心跳树：[stock_papertrade/commands.py L101-L240](../SayuStock/SayuStock/stock_papertrade/commands.py#L101-L240)
- 策略契约与硬闸：[strategies/base.py](../SayuStock/SayuStock/stock_papertrade/strategies/base.py)、[strategy.py](../SayuStock/SayuStock/stock_papertrade/strategy.py)
- 撮合：[matcher.py](../SayuStock/SayuStock/stock_papertrade/matcher.py)
- 执行器：[trade_executor.py](../SayuStock/SayuStock/stock_papertrade/trade_executor.py)
- 账本：[db.py](../SayuStock/SayuStock/stock_papertrade/db.py)、[papertrade_models.py](../SayuStock/SayuStock/utils/database/papertrade_models.py)
- 人格操作指南：[PAPERTRADE_GUIDE.md](../SayuStock/SayuStock/stock_papertrade/PAPERTRADE_GUIDE.md)
- 用户文档：[docs/papertrade.md](../SayuStock/docs/papertrade.md)
- 复刻实现：[sayu-paper-trader](../sayu-paper-trader/)

> 本文与复刻程序仅用于模拟盘/工程研究，不构成任何投资建议。
