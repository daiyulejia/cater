# Cater

**语言：** [English](README.en.md) | 中文

> 自治多 Agent 组织运行时。

Cater 把多个 AI agent 组织成一个可观察、可干预的自治组织。agent 以协作产出为主，互相交换成果；必要时才把真实分歧显性化、投票或决策；只有组织自身无法解决时，才极少数升级给人类。

## 为什么做

大多数 agent demo 只优化最终答案。Cater 的核心判断是：**过程本身也是产品**。

- 每个关键步骤都是只追加事件。
- 任务状态由事件流折叠出来。
- 默认是协作；battle 和决策阶梯只用于有意义的分歧。
- 人类命令和人工覆盖也是普通事件。
- CLI 暴露的能力会和未来 Web / 移动端共用同一套运行时。

## 定位

Cater 不试图替代 LangChain 或 LangGraph。

LangChain / LangGraph 可以作为某个 driver 或复杂执行后端的内部实现。但 Cater 核心会保留自己的事件流、任务模型、决策阶梯和人类命令语义，确保协作过程始终可观察、可迁移、可审计。

## 快速开始

先跑一个可回放 demo：

```bash
cater demo
cater demo --direct
cater review <task_id> --plain
cater chain --json
cater web
```

`cater demo` 展示治理较重的路径。`cater demo --direct` 展示轻量直派路径：没有风险时，治理层保持空闲。demo 命令使用确定性的内置角色和 driver。

如果希望使用对话优先的本地工作台，请先按 [`web/`](web/README.md) 构建可选的
React 前端，再运行 `cater web --port 8766`。Python 运行时会从同一个本地地址托管
工作台与 API，同时保持零第三方运行依赖；前端依赖独立管理。

再探索组织入口：

```bash
cater drivers
cater show
cater inbox
cater policy
cater doctor
cater doctor --probe
cater doctor --json
cater dispatch <task_id>
cater run <task_id>
cater export <task_id>
cater events <task_id>
cater verify <task_id> -- python -m unittest discover -s tests
cater review --next
cater decide <task_id> --resolution done --reason "Ship the smallest loop first"
```

默认数据库写入 `./.cater/cater.db`。

如果是在源码目录直接运行，等价写法是 `python -m cater.cli ...`。

## 本地验证

```bash
python scripts/check.py
```

如果 Windows 上的 `python` 指向 Microsoft Store 占位符，可以使用：

```powershell
.\scripts\check.ps1
```

## 命令

```bash
cater new "Fix the CLI typo" --assign-to dev --run
cater run <task_id>
cater battle <task_id>
cater dispatch <task_id>
cater assign <task_id> dev --objective "Implement the smallest useful path"
cater show [task_id]
cater events [task_id]
cater export [task_id] --format json
cater export [task_id] --format jsonl
cater review [task_id] --record
cater review [task_id] --plain
cater review [task_id] --next
cater verify <task_id> -- python -m unittest discover -s tests
cater verify <task_id> --for-event <诊断或交接事件序号> -- python -m unittest discover -s tests
cater inbox
cater profiles
cater policy
cater path <task_id>
cater path <task_id> --json
cater doctor
cater doctor --probe
cater doctor --probe --json
cater decide <task_id> --resolution done --reason "Accepted"
cater chain
cater chain --json
cater drivers
cater --drivers drivers.json probe-driver product
cater --driver-attempts 3 --circuit-breaker-threshold 4 run <task_id>
cater web --host 127.0.0.1 --port 8765
cater --audit-key "dev-secret" demo
cater --audit-key "dev-secret" chain
cater --audit-key "dev-secret" chain --json
```

`run` 是常规协作循环：记录 agent 的产出，并且只有在出现 challenge、方案分裂、低置信度或其他风险信号时才启动治理层。`battle` 是强制治理轮次，用于你明确希望内阁审查的时候。

`dispatch` 会记录金字塔式任务分发路径：顶层定义 mission，内阁把 mission 拆成执行 lane，worker agent 在下一轮协作前拿到明确分工。它是可选能力；小任务可以用 `assign` 或 `new --assign-to dev --run` 直接派给执行层。

也支持只配置店长和执行猫咪。在源码目录运行 `python -m cater --roles examples/compact_roles.json web`，可使用月亮、罐头、米寇、三角的精简编队。没有外部 driver 配置时仍为本地演示。`organization.cabinet_members: []` 明确关闭固定中间层，并要求移除内阁投票策略；店长负责派发，风险或强制复核按本地规则记录补证/返工，不生成空投票。这不代表独立成果验收或智能店长仲裁已实现。详见 [编队配置](docs/roster-policy.md)。

`path` 会汇总单个任务经过的分发、协作、治理和决策步骤。

`--driver-attempts` 和 `--circuit-breaker-threshold` 可以调节 driver 重试与熔断策略，不需要改 driver 代码。

`doctor --json` 会输出同一套机器可读健康检查，适合 CI、定时自审和接入新 driver 前的探针。

`verify` 仅在你显式调用时，在本机运行 `--` 后的命令，记录退出码、耗时、输出摘要与当时的 Git 状态。默认不把命令输出写入事件链；`--record-output` 才会保存最多 8 KiB 的输出摘录。命令参数本身会被记录，所以不要把令牌、密码或其他敏感值写进命令行。它不会自动执行 Agent 建议的命令，也不会因为一条命令通过就宣称诊断或整项任务已经证实。可用 `--cwd` 指定工作目录、`--timeout` 限制秒数。

检查结束或超时时会清理管理范围内的派生进程，命令标准输入关闭。适合测试和构建，不适合启动常驻服务。详细行为见 [本机检查说明](docs/local-verification.zh-CN.md)。GitHub 自动检查覆盖 Windows、Ubuntu 和 Python 3.11/3.13。

什么时候用直接派发、内阁分发或治理路径，见 [docs/organization-flow.md](docs/organization-flow.md)。

## 本地 Web API

启动 `cater web` 后，本地工具可以通过这些入口读取机器友好的状态：

```text
POST /api/tasks
POST /api/tasks/<task_id>/run
POST /api/tasks/<task_id>/battle
POST /api/tasks/<task_id>/dispatch
POST /api/tasks/<task_id>/assign
POST /api/tasks/<task_id>/command
POST /api/tasks/<task_id>/decide
GET /api/tasks
GET /api/tasks/<task_id>
GET /api/chain
```

`POST /api/tasks` 接收 JSON：必填 `goal`，可选 `acceptance`，以及可选布尔值 `dispatch`、`run` 或 `battle`。
任务动作接口会返回和 `GET /api/tasks/<task_id>` 一致的回放 payload。
零依赖客户端示例见 [examples/api_client.py](examples/api_client.py)。

## 当前范围

伙伴可以发布带正文的 Markdown、纯文本或代码成果；在聊天或成果板打开正文后，可以复制、下载并回到原对话。内置演示只提供明确标识的样例，正文合同和边界见 [可读成果说明](docs/artifacts.zh-CN.md)。

当前已经包含：

- SQLite 只追加事件流 + 哈希链校验
- 任务创建、协作轮次、风险触发治理、必要时 battle、内阁投票、最高者兜底、极少数人类命令/覆盖
- 可选的金字塔式任务分发，包含 mission 定义、内阁拆解、直接派给执行层和可回放 plan
- 执行层 agent 可通过目标角色和事件序号进行轻量定向回应
- 确定性 scripted driver，便于演示和测试
- JSON 配置角色与 driver
- 长驻 NDJSON CLI driver，支持存活探针与崩溃自愈
- 可配置内阁投票 tally，包含通过阈值、quorum、否决角色、推荐项与开放疑虑
- 本地 Web 工作台，支持任务创建、启动协作轮次、可视化决策阶梯、极少数人类输入、回放、内阁 tally 和命令覆盖
- 从事件流投影出的 agent profile、任务内协作记忆，以及同一轮内的产出共享
- driver 失败 containment，包含重试、失败事件和进程内 circuit breaker
- 在事件哈希链之上的可选 HMAC 审计签名
- 从事件流纯函数折叠任务快照
- 面向人的 CLI 过程视图
- 运行时零第三方依赖

## Driver 配置

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

CLI 子进程使用 NDJSON 协议。Cater 会发送：

```json
{"id":"...","type":"ping"}
{"id":"...","type":"complete","role":"product","prompt":"...","context":{"task_id":"...","goal":"..."}}
```

Driver 返回：

```json
{"id":"...","ok":true,"pong":true}
{"id":"...","ok":true,"text":"{\"stance\":\"assert\",\"option_id\":\"A\",\"claim\":\"...\",\"evidence\":[],\"text\":\"...\"}","tokens":123,"meta":{}}
```

这个边界刻意很小。LangChain、LangGraph、Codex CLI、Claude Code、Gemini CLI 或自定义 API wrapper 都可以藏在这个进程后面，而不污染 Cater 的核心运行时。

Skills 和 MCP 也应该放在这条边界后面：用 driver 进程包住它们，让 adapter 调用工具，再返回 Cater 的小 JSON 结构。`examples/ndjson_tool_driver.py` 提供了一个零依赖 adapter 骨架。如果 Windows 上的 `python` 指向 Microsoft Store 占位符，请把 `examples/tool_driver_config.json` 里的命令换成真实 Python 解释器完整路径。

## 内阁策略

角色可以和投票策略一起配置：

```json
{
  "cabinet": {
    "approval_threshold": 2,
    "quorum": 3,
    "veto_roles": ["test"]
  },
  "governance": {
    "trigger_on_challenge": true,
    "trigger_on_option_split": true,
    "low_confidence_threshold": 0.5
  },
  "roles": [
    {"id": "product", "name": "Product", "role": "product", "driver": "product"},
    {"id": "dev", "name": "Developer", "role": "developer", "driver": "dev"},
    {"id": "test", "name": "Tester", "role": "tester", "driver": "test"}
  ]
}
```

这让组织模型本身可见。项目可以从简单多数投票开始，再随着可靠性要求增加 quorum、否决角色或最高者兜底。

治理触发条件决定常规协作什么时候需要内阁介入。团队可以严格到方案分裂就审查，也可以允许 agent 继续交换产出，直到出现 challenge 或低置信度信号。

## 审计

事件流始终有哈希链。如果提供 audit key，新事件还会带上 HMAC 签名：

```bash
cater --audit-key "dev-secret" demo
cater --audit-key "dev-secret" chain
```

也可以使用 `--audit-key-file` 或 `CATER_AUDIT_KEY` 环境变量。

可以使用 `cater export` 生成机器可读的回放包，或用 `cater export --format jsonl` 导出原始事件流。

## 自省复盘

```bash
cater review --plain
cater review <task_id> --record
```

`review` 会从产品、设计、测试、UI、逻辑、可靠性和采用门槛等角度审视当前工作。`--plain` 用于快速阅读。不带 `--record` 时它只是纯投影；带 `--record` 时会追加 `review.generated` 事件，让批判和修正建议本身也可回放。

架构级自审见 [docs/architecture-review.md](docs/architecture-review.md)。它专门检查 Cater 自己的分发、裁判、治理机制是否值得付出 token、延迟和复杂度成本。

## 路线图

下一轮迭代：

1. 产品入口：让 `cater demo`、`cater new`、`cater dispatch`、`cater run`、`cater command`、`cater drivers` 形成连贯的新手首次体验。
2. 分发引擎：让内阁拆解更丰富，同时保持 plan 来自事件、可解释、可检查。
3. 协作模式：在 CLI/Web 区分常规产出交流和显性分歧轮次。
4. Web 回放：加入实时刷新和过滤器。
5. 记忆治理：近期保持上下文可解释、有边界、来自事件投影；更深的学习系统留作未来研究层。
6. 可靠性：在 CLI/Web 暴露恢复控制，并让失败策略更容易检查。
7. 审计：加入 key rotation 元数据、未签名历史策略和可导出的审计报告。
8. 分发：补充主流第三方 CLI wrapper 示例，同时保持核心零依赖。

## 参与贡献

本地验证命令、driver 边界约定和提交说明规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 文档

组织流程和架构自审说明见 [docs/README.md](docs/README.md)。

## 迭代视角

Cater 应该通过一个个可用小闭环生长。每次迭代都要从多个角度复盘：

- 产品：下一步操作是否显而易见？
- 设计：协作过程是否能一眼看懂？
- 测试：失败是否可回放、可复现？
- UI：人类命令入口是否清晰、克制、可行动？
- 逻辑：事件、投影、执行层交流和 driver 边界是否仍然简单？
- 采用：新贡献者是否不依赖额外服务就能跑起来？

## 设计原则

1. 过程优先：协作过程必须能被看见。
2. 事件优先：状态只是只追加历史的一个视图。
3. Driver 可替换：模型、CLI、API 不能焊死在骨架里。
4. 协作优先：分歧是常态，但只在能改善任务时显性化，而不是默认表演争论。
5. 分发要成比例：简单任务可以直接派给执行层，复杂任务再走内阁拆解。
6. 监督由事件触发：风险信号、低置信度、失败或 review 发现问题时，上层再重新介入。
7. 人类发命令和覆盖：人类可以随时重定向组织，但不是默认决策引擎。
