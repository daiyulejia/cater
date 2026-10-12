# Cater

> Human-steerable multi-agent collaboration runtime.
>
> 面向人的多 Agent 协作运行时。

Cater turns multiple AI agents into a small, inspectable team. Agents can propose, disagree, escalate, and wait for a human decision instead of pretending they reached consensus.

Cater 把多个 AI agent 组织成一支可观察、可干预的小团队。agent 可以提出方案、互相反对、升级分歧，并等待人类拍板，而不是假装已经达成共识。

## Why / 为什么做

Most agent demos optimize for the final answer. Cater treats the process as the product.

大多数 agent demo 只优化最终答案。Cater 的核心判断是：**过程本身也是产品**。

- every important step is an append-only event
- task state is folded from the event log
- disagreements become structured escalation briefs
- human decisions are normal events
- the CLI exposes the same surface future Web and mobile clients will use

- 每个关键步骤都是只追加事件
- 任务状态由事件流折叠出来
- 分歧会生成结构化升级简报
- 人类裁决也是普通事件
- CLI 暴露的能力会和未来 Web / 移动端共用同一套运行时

## Positioning / 定位

Cater is not trying to replace LangChain or LangGraph.

Cater 不试图替代 LangChain 或 LangGraph。

LangChain and LangGraph can be useful inside driver implementations or advanced execution backends. Cater keeps its own event log, task model, escalation path, and human decision semantics so the collaboration process remains inspectable and portable.

LangChain / LangGraph 可以作为某个 driver 或复杂执行后端的内部实现。但 Cater 核心会保留自己的事件流、任务模型、升级路径和人类裁决语义，确保协作过程始终可观察、可迁移、可审计。

## Quick Start / 快速开始

```bash
python -m cater.cli demo
python -m cater.cli show
python -m cater.cli inbox
python -m cater.cli events <task_id>
python -m cater.cli decide <task_id> --resolution done --reason "Ship the smallest loop first"
```

By default Cater writes to `./.cater/cater.db`.

默认数据库写入 `./.cater/cater.db`。

## Commands / 命令

```bash
python -m cater.cli new "Build the first demo" --accept "three roles respond" --battle
python -m cater.cli battle <task_id>
python -m cater.cli show [task_id]
python -m cater.cli events [task_id]
python -m cater.cli inbox
python -m cater.cli decide <task_id> --resolution done --reason "Accepted"
python -m cater.cli chain
```

## Current Scope / 当前范围

Implemented in the first seed:

第一版已经包含：

- SQLite append-only event store with hash-chain verification
- task creation, battle, escalation, and human decision
- deterministic scripted drivers for repeatable demos and tests
- pure fold from events to task snapshots
- CLI commands for process inspection
- zero third-party runtime dependencies

- SQLite 只追加事件流 + 哈希链校验
- 任务创建、部门 battle、升级、人类拍板
- 确定性 scripted driver，便于演示和测试
- 从事件流纯函数折叠任务快照
- 面向人的 CLI 过程视图
- 运行时零第三方依赖

## Roadmap / 路线图

Next:

下一步：

- real CLI/API model drivers
- Web summary and replay view
- richer escalation brief schema
- HMAC audit signatures
- retry containment and circuit breaker
- mobile decision inbox

- 真实 CLI/API 模型 driver
- Web 摘要与过程回放界面
- 更完整的升级简报 schema
- HMAC 审计签名
- retry containment 与 circuit breaker
- 移动端待拍板收件箱

## Design Principles / 设计原则

1. Process first. The collaboration process must be visible.
2. Events first. State is a view over append-only history.
3. Drivers are replaceable. Models, CLIs, and APIs are not hardwired.
4. Disagreement is normal. Escalation is a feature, not a failure.
5. Humans are actors. Human decisions live in the same event stream.

1. 过程优先：协作过程必须能被看见。
2. 事件优先：状态只是只追加历史的一个视图。
3. Driver 可替换：模型、CLI、API 不能焊死在骨架里。
4. 分歧是常态：升级不是失败，而是一种产品能力。
5. 人类是一等参与者：人类裁决和 agent 发言进入同一条事件流。
