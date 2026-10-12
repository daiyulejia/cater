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
python -m cater.cli drivers
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
python -m cater.cli drivers
python -m cater.cli --drivers drivers.json probe-driver product
```

## Current Scope / 当前范围

Implemented now:

当前已经包含：

- SQLite append-only event store with hash-chain verification
- task creation, battle, escalation, and human decision
- deterministic scripted drivers for repeatable demos and tests
- configurable roles and drivers from JSON
- long-lived NDJSON CLI drivers with probe and crash recovery
- cabinet vote tally with threshold, recommendation, and open concerns
- pure fold from events to task snapshots
- CLI commands for process inspection
- zero third-party runtime dependencies
- SQLite 只追加事件流 + 哈希链校验
- 任务创建、部门 battle、升级、人类拍板
- 确定性 scripted driver，便于演示和测试
- JSON 配置角色与 driver
- 长驻 NDJSON CLI driver，支持存活探针与崩溃自愈
- 内阁投票 tally，包含阈值、推荐项与开放疑虑
- 从事件流纯函数折叠任务快照
- 面向人的 CLI 过程视图
- 运行时零第三方依赖

## Driver Config / Driver 配置

Drivers are replaceable. The default package uses deterministic scripted drivers, but you can override any driver with a local CLI process:

Driver 是可替换边界。默认包使用确定性 scripted driver，但你可以用本地 CLI 进程覆盖任意 driver：

```json
{
  "drivers": [
    {
      "kind": "cli",
      "name": "product",
      "command": ["python", "my_driver.py"],
      "timeout": 120,
      "restart_limit": 1
    }
  ]
}
```

The CLI child process speaks newline-delimited JSON. Cater sends:

CLI 子进程使用 NDJSON 协议。Cater 会发送：

```json
{"id":"...","type":"ping"}
{"id":"...","type":"complete","role":"product","prompt":"...","context":{"task_id":"...","goal":"..."}}
```

The driver replies:

Driver 返回：

```json
{"id":"...","ok":true,"pong":true}
{"id":"...","ok":true,"text":"{\"stance\":\"assert\",\"option_id\":\"A\",\"claim\":\"...\",\"evidence\":[],\"text\":\"...\"}","tokens":123,"meta":{}}
```

This is intentionally small. LangChain, LangGraph, Codex CLI, Claude Code, Gemini CLI, or a custom API wrapper can live behind this process boundary without leaking into Cater's core runtime.

这个边界刻意很小。LangChain、LangGraph、Codex CLI、Claude Code、Gemini CLI 或自定义 API wrapper 都可以藏在这个进程后面，而不污染 Cater 的核心运行时。

## Roadmap / 路线图

Next iterations:

下一轮迭代：

1. Product entry: make `cater demo`, `cater new`, `cater inbox`, and `cater drivers` feel like a coherent first-run workflow.
2. Cabinet layer: add configurable voting policy, quorum, veto roles, and auto-proposed decisions.
3. Web replay: add a minimal local web view for task summary, event timeline, messages, cabinet tally, brief, and human decision.
4. Reliability: add retry containment, circuit breaker, and driver failure events.
5. Audit: add optional HMAC signatures on top of the hash chain.
6. Distribution: add examples for wrapping popular third-party CLIs while keeping the core dependency-free.
1. 产品入口：让 `cater demo`、`cater new`、`cater inbox`、`cater drivers` 形成连贯的新手首次体验。
2. 内阁层：加入可配置投票策略、quorum、否决角色与自动建议决策。
3. Web 回放：增加本地极简 Web 视图，展示任务摘要、事件时间线、消息、内阁 tally、简报和人类裁决。
4. 可靠性：加入 retry containment、circuit breaker 和 driver 失败事件。
5. 审计：在哈希链之上增加可选 HMAC 签名。
6. 分发：补充主流第三方 CLI wrapper 示例，同时保持核心零依赖。

## Iteration Lens / 迭代视角

Cater should grow through small usable loops. Each iteration should be reviewed from multiple angles:

Cater 应该通过一个个可用小闭环生长。每次迭代都要从多个角度复盘：

- product: does this make the next user action obvious?
- design: can the process be understood at a glance?
- testing: can failures be replayed and reproduced?
- UI: is the human decision point calm and actionable?
- logic: are events, projections, and driver boundaries still simple?
- adoption: can a new contributor run it without extra services?
- 产品：下一步操作是否显而易见？
- 设计：协作过程是否能一眼看懂？
- 测试：失败是否可回放、可复现？
- UI：人类拍板点是否清晰、克制、可行动？
- 逻辑：事件、投影、driver 边界是否仍然简单？
- 采用：新贡献者是否不依赖额外服务就能跑起来？

## Design Principles / 设计原则

English:

1. Process first. The collaboration process must be visible.
2. Events first. State is a view over append-only history.
3. Drivers are replaceable. Models, CLIs, and APIs are not hardwired.
4. Disagreement is normal. Escalation is a feature, not a failure.
5. Humans are actors. Human decisions live in the same event stream.

中文：

1. 过程优先：协作过程必须能被看见。
2. 事件优先：状态只是只追加历史的一个视图。
3. Driver 可替换：模型、CLI、API 不能焊死在骨架里。
4. 分歧是常态：升级不是失败，而是一种产品能力。
5. 人类是一等参与者：人类裁决和 agent 发言进入同一条事件流。
