# Cater

**语言：** [English](README.en.md) | 中文

> 像一家猫咖一样协作的自治多 Agent 组织运行时。

Cater 把多个 AI agent 组织成一个可观察、可干预的自治组织：顾客只说目标，店长负责理解和路由，内阁只在需要治理时出现，执行猫咪负责协作交付。agent 以协作产出为主，互相交换成果；必要时才把真实分歧显性化、投票或决策；只有组织自身无法解决时，才极少数升级给人类。

## 为什么做

大多数 agent demo 只优化最终答案。Cater 的核心判断是：**过程本身也是产品**。

- 每个关键步骤都是只追加事件。
- 任务状态由事件流折叠出来。
- 任务启动时会记录组织配置指纹，便于区分角色卡或治理策略变更前后的历史回放，而不把完整本地配置写入事件链。
- 默认是协作；battle 和决策阶梯只用于有意义的分歧。
- 人类命令和人工覆盖也是普通事件。
- CLI 暴露的能力会和未来 Web / 移动端共用同一套运行时。

## 定位

Cater 不试图替代 LangChain 或 LangGraph。

LangChain / LangGraph 可以作为某个 driver 或复杂执行后端的内部实现。但 Cater 核心会保留自己的事件流、任务模型、决策阶梯和人类命令语义，确保协作过程始终可观察、可迁移、可审计。

## 快速开始

从源码目录直接跑一个可回放 demo（Python 3.11+）：

```bash
python -m cater demo
python -m cater demo --direct
python -m cater review <task_id> --plain
python -m cater chain --json
python -m cater web
```

想把 `cater` 安装成命令后再使用简写，可在仓库根目录执行 `python -m pip install -e .`。这不会给核心运行时增加第三方依赖。

`cater demo` 展示治理较重的路径。`cater demo --direct` 展示轻量直派路径：没有风险时，治理层保持空闲。demo 命令使用确定性的内置角色和 driver。

Windows 上 CLI 会以 UTF-8 输出中文；若使用旧式终端仍显示异常，请先执行 `chcp 65001`，或改用 Windows Terminal / PowerShell 7。

发布包会直接携带对话优先的本地工作台；运行 `cater web --port 8766` 即可打开它。
从源码修改前端时，再按 [`web/`](web/README.md) 构建 React 工作台。Python 运行时会从
同一个本地地址托管工作台与 API，同时保持零第三方运行依赖；前端依赖独立管理。

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

Web 控制面默认只允许绑定 `127.0.0.1`、`::1` 或 `localhost`，避免把本地任务、对话和 driver 调用意外暴露到局域网。远程访问必须同时显式加 `--allow-remote` 并提供访问令牌；没有令牌会拒绝启动。令牌优先从 `CATER_WEB_ACCESS_TOKEN` 或 `--access-token-file` 读取，避免把它写进 shell 历史。Cater 自己不提供 TLS，远程部署仍应位于 HTTPS 反向代理、VPN 或等效受控网络边界之后：

```powershell
$env:CATER_WEB_ACCESS_TOKEN = "请替换为足够长的随机值"
cater web --host 0.0.0.0 --allow-remote
```

远程打开工作台时，首次使用 `https://你的地址/#access_token=你的令牌`；前端会立即将片段保存到当前浏览器会话并从地址栏移除。令牌只随同源 API 请求通过 `X-Cater-Access-Token` 发送，不会出现在服务器请求 URL、事件链或审计导出中。不要把含令牌的链接发送给他人。

`run` 是常规协作循环：记录 agent 的产出，并且只有在出现 challenge、方案分裂、低置信度或其他风险信号时才启动治理层。`battle` 是强制治理轮次，用于你明确希望内阁审查的时候。

`dispatch` 会记录金字塔式任务分发路径：店长定义 mission，内阁把 mission 拆成执行 lane，执行猫咪在下一轮协作前拿到明确分工。它是可选能力；小任务可以用 `assign` 或 `new --assign-to dev --run` 直接派给执行层。默认四只执行猫为月亮（需求与体验）、罐头（交互与视觉）、米寇（工程实现）和三角（质量与验证）；内阁成员为白露（需求）、阿程（工程）和知夏（质量），它们与执行席位在配置上相互独立。

CLI 中只执行 `new` 时，任务会保持待安排，方便脚本随后显式调用 `dispatch` 或 `assign`；一旦使用 `new --run`、`new --battle` 或 `new --assign-to`，店长也会先记录任务理解与路线选择，再进入执行层。这与对话工作台保持一致。

默认编队为何保持 3 位铲屎官与 4 只猫咪、又该如何扩容，见 [docs/roster-policy.md](docs/roster-policy.md)。

`path` 会汇总单个任务经过的分发、协作、治理和决策步骤。

`--driver-attempts` 和 `--circuit-breaker-threshold` 可以调节 driver 重试与熔断策略，不需要改 driver 代码。通过 `cater web` 运行时，熔断计数会随服务存活而保留，不会因为一次浏览器请求结束而重置；重启服务后再重新探测通道。

`doctor --json` 会输出同一套机器可读健康检查，适合 CI、定时自审和接入新 driver 前的探针。

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
在 `run` 或 `battle` 同时传入 `background: true` 时，接口会以 `202 Accepted` 立即返回；响应中的
`activity` 描述排队或执行状态，事件链会记录排队、完成或失败。对话工作台默认使用这个模式，只有任务
活跃时才轮询回放。

`POST /api/tasks/<task_id>/command` 传入 `{ "instruction": "...", "continue": true }` 时，
店长会先记录新方向；若当前轮仍在执行，则把一轮新的协作排在它之后。这样不会粗暴中断本地 CLI，
也不会让刚补充的意图被正在处理的旧上下文吞掉。

对话中的方向更新有明确而克制的调度语义：每条消息都会进入后续上下文；`@罐头` 这类点名会将对应猫咪加入下一轮，已经在编队中的角色只会收到新的重点而不会重复派活；`只让 @罐头` 或 `移除 @月亮` 才会改变编队；明确说“请内阁审查”会进入治理轮。`目标改为：…` 会立即更新任务目标，但只有明确提出“重新规划”或“重新分配”等大调整时，店长才会重拆分工。当前轮期间连续发出方向时，Cater 只保留一轮待执行的后续协作，并以最新明确策略覆盖旧策略；所有更新均会作为事件回放。

工作台把发起任务也做成一条对话：直接说明想做什么即可；写 `@月亮`、`@罐头`、`@米寇` 或 `@三角` 可以表达优先交给哪位猫咪。店长始终会先理解需求并记录路线；只有任务足够轻量时才尊重点名直派，复杂任务仍会升级为协作。验收标准不要求在发起时填写，店长会在对话中提示你补充；未补充时，先按可验证的最小结果推进。

后台协作不会在 Web 服务重启后自动重放旧模型调用：Cater 会把未终结活动写为“因重启中断”，
并保留原任务、命令和过程证据；随后从对话框补充或确认方向即可用新上下文重新启动，避免重复执行。
零依赖客户端示例见 [examples/api_client.py](examples/api_client.py)。

## 当前范围

当前已经包含：

- SQLite 只追加事件流 + 哈希链校验
- 任务创建、协作轮次、风险触发治理、必要时 battle、内阁投票、最高者兜底、极少数人类命令/覆盖
- 可选的金字塔式任务分发，包含 mission 定义、内阁拆解、直接派给执行层和可回放 plan
- 执行层 agent 可通过目标角色和事件序号进行轻量定向回应
- 可审计的 agent 工作状态事件，只展示当前阶段，不暴露隐藏推理
- 确定性本地演示 driver，便于验证协作、回放与测试
- JSON 配置角色与 driver
- 长驻 NDJSON CLI driver，支持存活探针与崩溃自愈
- 可配置内阁投票 tally，包含通过阈值、quorum、否决角色、推荐项与开放疑虑
- 本地 Web 工作台，支持任务创建、启动协作轮次、在同一对话中筛选协作 / 决策 / 成果、可视化决策阶梯、极少数人类输入、回放、内阁 tally 和命令覆盖
- 从事件流投影出的 agent profile、任务内协作记忆，以及同一轮内的产出共享
- driver 失败 containment，包含重试、失败事件和进程内 circuit breaker
- 在事件哈希链之上的可选 HMAC 审计签名
- 从事件流纯函数折叠任务快照
- 面向人的 CLI 过程视图
- 运行时零第三方依赖

## Driver 配置

Driver 是可替换边界。默认包使用确定性本地演示 driver，它会根据任务生成可读的演示协作记录，但不是模型推理；接入本地 CLI 进程后，真实模型会在同一角色契约和事件链下工作：

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
{"id":"...","ok":true,"text":"{\"stance\":\"assert\",\"option_id\":\"A\",\"claim\":\"...\",\"evidence\":[],\"text\":\"...\"}","tokens":123,"meta":{"token_usage":"reported"}}
```

通过 `cater web` 接入时，CLI driver 会在整个 Web 服务存活期间复用同一个长驻进程；每个 HTTP
请求仍使用独立的 SQLite 连接，服务退出时才统一关闭 driver。这样既保留请求间的工具会话，又不让
Web 线程共享数据库连接。

这个边界刻意很小。LangChain、LangGraph、Codex CLI、Claude Code、Gemini CLI 或自定义 API wrapper 都可以藏在这个进程后面，而不污染 Cater 的核心运行时。

CLI driver 的 `command` 是本机执行权限，不是安全沙箱：只加载自己维护或已审阅来源的 driver
配置。由 adapter 连接的 MCP、skills 或工具框架继承该进程的文件、网络和账号权限；按信任等级
拆分账号、工作目录与最小权限。Cater 会限制每条 CLI 请求与响应各为 4 MiB，并在超时、崩溃或协议
错误后重启长驻进程，但这只是在限制损害范围，不能把不可信工具变成可信工具。

### 角色、团队与账号如何对应

一个角色卡只引用一个 driver 名；因此同一 driver 可以被多个角色复用。默认配置中，白露和月亮共享 `product`，阿程和米寇共享 `dev`，知夏和三角共享 `test`。这让接入粒度可以按实际成本、权限和模型能力选择：

- **按专业角色**：使用 [`examples/openai_compatible_driver_config.json`](examples/openai_compatible_driver_config.json)，给产品、设计、工程和质量分别配置模型或账号。
- **按组织层级**：使用 [`examples/layered_api_driver_config.json`](examples/layered_api_driver_config.json) 与 [`examples/layered_api_roles.json`](examples/layered_api_roles.json)，内阁共用一个通道，执行猫共用另一个通道。
- **按本机登录账号**：继续使用 CLI driver；不同 driver 可以启动不同的本机 adapter 进程，各自继承对应工具的登录态。

`openai_compatible` 是零第三方依赖的 Chat Completions 适配器。配置文件只放 `endpoint`、`model` 和 `api_key_env`；真实密钥只从当前进程环境读取，绝不进入角色卡、事件链、审计导出或网页。它接受兼容 Chat Completions 请求/响应形状的服务。使用前把示例里的占位 endpoint 与模型替换为实际值，再设置相应环境变量，例如：

外部 endpoint 默认必须使用 HTTPS，防止 API key 与任务内容经明文网络发送；仅 `localhost`、`127.0.0.1` 和 `::1` 的本地开发模型服务允许 HTTP。

```powershell
$env:CATER_COUNCIL_API_KEY = "..."
$env:CATER_WORKSHOP_API_KEY = "..."
cater --roles examples/layered_api_roles.json --drivers examples/layered_api_driver_config.json web
```

网页会显示“本地演示”“部分成员已接入外部驱动”或“已接入外部驱动”，并按执行成员显示通道状态；它不会显示账号、端点或密钥。provider 未返回可验证的 `usage.total_tokens` 时，成本会记为未知而不是伪造为零。

需要控制共享账号的消耗时，可在任一 driver 配置中增加 `token_budget`。预算按 driver 名跨角色累计，所以给 `council` 设置预算就是给整层内阁设置一个共享上限。它只累计 `token_usage=reported` 的实际 provider 用量；达到上限后 Cater 会写入 `driver.budget_exhausted` 并停止该通道后续调用。由于一次调用完成后才知道总用量，它是防止继续扩张的护栏，不是对单次请求的硬性账单上限；provider 不报告 usage 时，Cater 会明确保留“未知”，而不会假装预算可精确执行。

`meta.token_usage` 可选值为 `reported`、`estimated`、`unavailable`。缺省值是 `reported`，供已经返回可信用量的第三方 driver 保持兼容；演示 driver 会标记为 `estimated`。若 provider 没有可验证的用量数据，必须标记为 `unavailable`，Cater 会在回放和 `review` 中把成本显示为未知，而不是把它误报成零。随附的 Codex CLI 适配器遵循这一规则。

## 角色命名

角色卡中的 `name` 是界面对话、成果和分工中显示的名字，`role` 定义职责，`charter` 定义该席位面对模型时的工作约束，`voice` 则是可选的表达方式提示。`voice` 只能影响说话风格，不能覆盖任务、职责或协作规则。默认内阁成员是白露、阿程和知夏；想换成自己的角色名或口吻，只需复制并修改 `cater/packs/default_roles.json`，再用 `--roles` 指向这份文件。运行时不会把角色名写死在代码里。

Skills 和 MCP 也应该放在这条边界后面：用 driver 进程包住它们，让 adapter 调用工具，再返回 Cater 的小 JSON 结构。`examples/ndjson_tool_driver.py` 提供了一个零依赖 adapter 骨架。如果 Windows 上的 `python` 指向 Microsoft Store 占位符，请把 `examples/tool_driver_config.json` 里的命令换成真实 Python 解释器完整路径。

## 身份、记忆与纪律

角色身份由角色卡持续定义：名字、职责、charter、表达方式和绑定的 driver 不会因为一次对话结束而丢失。运行时不会把整段历史或模型隐藏推理塞回下一次调用，而是使用三层有界上下文：

- **身份记忆**：从角色卡读取的长期职责与表达边界。
- **任务共享记忆**：当前任务最近的协作消息、分工、命令与同轮产出，带事件序号，供伙伴明确回复或接力。
- **组织共享记忆**：其他任务中显式发布的最近三项成果摘要；每项保留原始事件序号、发布者和任务 ID，可回放验证，并被提示为“参考而非当前事实”。

团队纪律同样不是人格设定：每条贡献需要可见结论；质疑必须给出证据；定向回复必须同时指向有效角色和可见事件。运行时会把违反这些格式约束的情况记录为 `agent.discipline_flagged`。其中无证据质疑会被保留为可见提醒，但不能触发否决或升级为治理投票依据。这样既不把模型偶发格式错误悄悄吞掉，也不会让它劫持自治决策。

仓库也提供可直接尝试的 [`examples/codex_cli_driver.py`](examples/codex_cli_driver.py)：它把本机已登录的 Codex CLI 接到同一 NDJSON 边界，默认只读、短生命周期，并用 JSON Schema 限定模型的可见协作消息。接入步骤和安全边界见 [`examples/README.md`](examples/README.md#codex-cli-适配器)。

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

也可以使用 `--audit-key-file` 或 `CATER_AUDIT_KEY` 环境变量。需要轮换密钥时，为新的写入密钥设置新的
`--audit-key-id`，再通过 `--audit-keyring-file` 提供一个仅保存在受保护位置的 JSON 映射，例如
`{"2026-q1":"旧密钥"}`；它让历史签名按原始 `key_id` 继续校验。密钥文件不得提交到仓库。

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

## 持续自驱迭代铁律

路线图不是停止理由，完成一个功能也不是停止理由。Cater 的维护者与自治组织都应遵守以下约束：

1. 每次成组改动后，主动从产品、用户、交互、逻辑、测试、可靠性、成本、采用门槛和发起人目标复盘，而不是只检查代码是否能运行。
2. 发现有证据支持的缺口、回归或可用性障碍时，优先在当前周期自行修正、补验证并记录结果；只有需要外部权限、真实业务取舍或连续无法排除的阻塞时才请求人类介入。
3. 协作过程可以有表现力，但必须服务于任务理解、证据和成果，不能用“多 agent 表演”替代实际产出。
4. 不把自驱误解为无目的改动：每项修改都必须能追溯到用户目标、可观察问题或验证证据；当本轮没有明确改进项时保持安静，而不是制造变更。
5. 人类始终可以重定向目标；组织应主动校验当前实现是否仍帮助发起人完成其最初动机，并允许这个动机随使用中的新证据演化。

完整的执行清单见 [docs/autonomy-covenant.md](docs/autonomy-covenant.md)。

## 设计原则

1. 过程优先：协作过程必须能被看见。
2. 事件优先：状态只是只追加历史的一个视图。
3. Driver 可替换：模型、CLI、API 不能焊死在骨架里。
4. 协作优先：分歧是常态，但只在能改善任务时显性化，而不是默认表演争论。
5. 分发要成比例：简单任务可以直接派给执行层，复杂任务再走内阁拆解。
6. 监督由事件触发：风险信号、低置信度、失败或 review 发现问题时，上层再重新介入。
7. 人类发命令和覆盖：人类可以随时重定向组织，但不是默认决策引擎。
8. 持续自驱迭代：发现明确缺口就自修正、验证并沉淀；不因单次交付完成而停止，也不为制造活动而制造复杂度。
