# AGENTS.md（子目录指引）

本目录是 `sayu-paper-trader` 实现本身。若你把**仓库根目录**作为工作区打开，根目录的 [`../AGENTS.md`](../AGENTS.md) 会自动生效，无需读本文件。

如果你只打开了这个子目录，请先读：

1. [`../AGENTS.md`](../AGENTS.md) —— 项目约定、硬性约束、最短运行路径；
2. [`../docs/new-machine-runbook.md`](../docs/new-machine-runbook.md) —— 新机器完整操作手册（前置条件、定时任务、回放、故障排查、验收清单）。

要点速记：

- 所有命令都在本目录执行：`python3.12 -m papertrader.cli ...`
- 新机器先 `init` 再 `plan-load`，账本在 `data/paper.db`（不入库）
- 零第三方依赖；改交易逻辑后必须 `python3.12 -m unittest discover -s tests -v`
- 不要提交账本、日志、密钥；不要自动改策略或替换入场计划
