# Cater Driver 示例

本目录展示如何接入外部工具，同时不让它们渗入 Cater 核心。

## 可选 MCP stdio 服务

[`mcp.example.json`](mcp.example.json) 是宿主配置模板，显式指定绝对命令路径、工作目录、角色、工具读写分类和环境变量映射。替换为本机已安装且可信的桥与服务，核对 `roles`、`workspaces` 和真实工具名后使用：

```powershell
python -m cater --mcp C:/Cater/config/mcp.json web
```

示例中的记录服务需自行提供；不传 `--mcp` 不启动 MCP 进程。Python 核心零第三方运行依赖，可选 Node 桥使用官方 SDK，不自动通过 `npx` 安装服务。JSON 只填写环境变量名，不填写密钥。

MCP 授权与目录模式独立，工作区只读不会限制外部 MCP 进程的写入能力，进程管理也不提供 OS 沙箱。目录和调用按需启动，失败不自动重放；正常 `isError` 与可能已有副作用的未知结果分别处理。配置字段、pool 生命周期和恢复说明见 [MCP 使用文档](../docs/mcp.zh-CN.md)。

## 精简编队

[`compact_roles.json`](compact_roles.json) 配置店长和月亮、罐头、米寇、三角，不设固定中间层：

```bash
python -m cater --roles examples/compact_roles.json web
```

在源码目录运行；端口占用时加 `--port` 选择其他端口。未指定 `--drivers` 时使用本地演示，不会调用付费模型。角色可以接到既有 API/CLI driver，但角色卡本身不会提供工具权限或独立验收。如何比较与配置不同编队，见 [编队说明](../docs/roster-policy.md)。

## 可选店长模型规划

在自己的角色配置中，保留 `organization` 的其他字段，只给 `leader` 增加 `driver`：

```json
{"id": "store_manager", "name": "店长", "role": "猫咖店长", "driver": "product"}
```

这是 `organization.leader` 的字段示例，不是完整配置。`product` 应指向你已配置且支持规划响应的真实通道，也可另设 `planner` 通道；不填 `driver` 时继续使用默认本地规则，不产生模型费用。不要将默认演示 driver 误当成智能规划器。

用原来的 `--roles ... --drivers ... web` 启动，在同一聊天中发布任务、补充要求即可。店长返回工作项、依赖、验收标准和需求覆盖关系，系统检查后才下发。更换驱动不自动扩大工具或文件权限；运行边界与恢复限制见 [工作项与阶段验收](../docs/work-items.zh-CN.md#店长规划通道)。

## Web API 客户端

先启动 Cater：

```bash
cater web
```

再在另一个终端运行：

```bash
python examples/api_client.py
```

示例会通过 `POST /api/tasks` 创建任务，通过 `POST /api/tasks/<task_id>/run` 启动一轮协作，并校验 `/api/chain`。

访问受令牌保护的远程工作台时，示例会自动从 `CATER_WEB_ACCESS_TOKEN` 读取令牌并发送
`X-Cater-Access-Token`；令牌不需要也不应出现在命令行参数中。需要使用不同环境变量名时，可传入
`--access-token-env YOUR_TOKEN_ENV`。

## NDJSON Driver 协议

CLI driver 每行读取一个 JSON 请求，并且每行写入一个 JSON 响应。探针请求与响应如下：

```json
{"id":"req-1","type":"ping"}
{"id":"req-1","ok":true,"pong":true}
```

完成任务的请求：

```json
{"id":"req-2","type":"complete","role":"developer","prompt":"...","context":{"task_id":"...","goal":"..."}}
```

完成任务的响应必须包含字符串 `text`、整数 `tokens` 和可选 `meta`：

```json
{"id":"req-2","ok":true,"text":"{\"stance\":\"assert\",\"claim\":\"...\",\"evidence\":[],\"text\":\"...\"}","tokens":123,"meta":{}}
```

嵌套在 `text` 中的 Agent 消息可选包含 `target` 与 `reply_to`，用于真实的继续、修正或交接：

```json
{"target":"dev","reply_to":12}
```

`target` 必须是已配置角色，`reply_to` 必须是该 Agent 在提示词里可见的事件序号；无效元数据会被丢弃。接入外部 driver 前先运行探针、核验真实 I/O 形状、明确超时和失败语义，并避免把密钥写入事件流。

### 信任边界

`kind: "cli"` 的 `command` 会作为当前用户启动的本地进程执行；它不是模型配置字段，也不是
沙箱。只应加载自己维护或已审阅来源的 roles / drivers JSON，不能把他人提供的 driver 配置直接
用于本机。MCP、skills、LangChain 或其他工具框架若由 adapter 调用，也继承该 adapter 的文件、
网络与账号权限；应为不同信任等级使用独立账号、工作目录和最小权限。

Cater 限制单条 CLI 请求为 4 MiB UTF-8 字节、响应为 4 Mi 字符，并在超时、崩溃或协议错误后重启长驻进程，但这些是
运行时生命周期护栏，不会把不可信工具变成安全工具。不要让 driver 将密钥、完整 stderr、私人文件或
不必要的原始工具输出写入可回放消息。

CLI 启动先建立 Windows Job / POSIX 进程组，再允许启动目标驱动。每次尝试的超时覆盖启动、写请求和读响应；
超时与关闭先结束管理范围内的进程，再关闭管道，避免不读请求或不响应的驱动卡住清理。
清理有有限的收尾时间，失败会报告错误，不宣称所有进程均已消失。POSIX 上主动创建新会话的程序可能脱离进程组。
这些措施没有限制文件或网络权限，也不提供 CPU/内存配额；权限沙箱仍需实际执行后端。
运行时与内置 CLI driver 不会自动重放失败的完成请求；存活探针仍可重建进程。
第三方 adapter 内部的重试不在该保证之内，有外部副作用时仍需自行提供幂等或关闭内部重试。

详见 [接入复盘](../docs/integration-review.md)。

## Codex CLI 适配器

`codex_cli_driver.py` 是可以直接使用的真实模型适配器：它保持 Cater 的长驻
NDJSON 进程，同时为每次 completion 启动一个隔离的 `codex exec`。这符合 Codex
CLI 当前的单次 `exec` 契约，也不会把该契约耦合进 Cater 核心。

先确认本机 Codex CLI 已登录，再以源码目录为工作目录运行：

```powershell
$env:CATER_CODEX_WORKDIR = (Get-Location).Path
cater --drivers examples/codex_cli_driver_config.json doctor --probe
cater --drivers examples/codex_cli_driver_config.json new "为猫咖做一页预约说明" --run
```

若 `python` 是 Microsoft Store 占位符，请把配置文件中每个 `command` 的第一个
元素换成真实 Python 解释器的绝对路径。常用环境变量：

```text
CATER_CODEX_COMMAND=codex                 # 可换成 Codex 可执行文件完整路径
CATER_CODEX_MODEL=<可用模型名>             # 可选，不填则沿用 Codex 默认配置
CATER_CODEX_TIMEOUT=180                   # 单次模型调用的秒数
CATER_CODEX_WORKDIR=<现有目录>             # 可选的 Codex 工作目录
```

适配器固定传入 `--sandbox read-only`、`--ephemeral` 和 JSON Schema；不会启用
跳过审批或跳过沙箱的危险参数。它只将简短的失败原因写回 Cater，避免把 CLI stderr、
令牌或其他潜在敏感内容写进可回放事件链。每次模型调用的实际 tokens 当前由 Codex
CLI 不提供给此适配器，因此事件中会记录为 `0`，不伪造成本数据。

## OpenAI 兼容 API 适配器

Anthropic Messages 协议使用独立的 [`anthropic_driver_config.json`](anthropic_driver_config.json)，见 [协议配置与真实测试](../docs/model-evaluation.zh-CN.md)。不要把两种协议的地址和请求格式混用。

[`openai_compatible_driver_config.json`](openai_compatible_driver_config.json) 是按专业
通道配置的模板：产品、设计、工程、质量可以使用不同模型或不同账号。它直接使用
Python 标准库发出 Chat Completions 请求，不需要在 Cater 核心安装 SDK。先把每个
`endpoint` 和 `model` 的占位值替换为 provider 的真实值，再把密钥放到当前终端环境：

```powershell
$env:CATER_PRODUCT_API_KEY = "..."
$env:CATER_DESIGN_API_KEY = "..."
$env:CATER_DEV_API_KEY = "..."
$env:CATER_TEST_API_KEY = "..."
cater --drivers examples/openai_compatible_driver_config.json doctor
cater --drivers examples/openai_compatible_driver_config.json web
```

不把密钥写进 JSON、角色卡或任务消息。该 adapter 只把 provider 已返回的
`usage.total_tokens` 作为真实用量；缺失时会明确标记为未知。它不做不可靠的
“存活探针”，首次实际任务才是端点、模型名和账户权限的完整验证。

可在 `openai_compatible` 通道中选配 `"max_tokens": 4096`，或在供应商要求时使用
`"max_completion_tokens": 4096`，两者只能设置一个，值必须为正整数。
不配置时沿用供应商默认行为；Cater 不猜测模型支持的字段或上限，也不在失败后自动换字段或提高额度。
该值限制单次请求的生成量，不限制输入、整个任务、重试总量或实际费用。
部分模型会把推理 token 计入输出额度，具体含义以供应商文档为准。
响应明确标记 `finish_reason: "length"` 时，不能当作完整文本或可执行的工具调用；
请检查中断原因后再决定是否调整配置。此配置不会修改已有通道的累计 `token_budget`。

如果想让内阁共用一个账号、执行层共用另一个账号，可成对使用
[`layered_api_driver_config.json`](layered_api_driver_config.json) 与
[`layered_api_roles.json`](layered_api_roles.json)：

```powershell
$env:CATER_COUNCIL_API_KEY = "..."
$env:CATER_WORKSHOP_API_KEY = "..."
cater --roles examples/layered_api_roles.json --drivers examples/layered_api_driver_config.json web
```

同一个 driver 被多个角色引用时，会共享该通道的账户、模型、速率限制和 token 预算；
是否拆分，取决于权限隔离、成本归属和模型专长，而不是角色数量。

### 店长与全员共用一个 API 通道

成对使用 [`single_api_roles.json`](single_api_roles.json) 和
[`single_api_driver_config.json`](single_api_driver_config.json)：店长、三位顾问和四位执行成员
均显式绑定 `shared_api`，只使用 `CATER_SHARED_API_KEY` 一个密钥环境变量。
店长的绑定位于 `organization.leader.driver`；省略它会走本地规则，不会自动沿用成员的 API。
这些是独立模板，不改变仓库默认配置；运行时仍会合并保留默认驱动，但这八个角色都使用 `shared_api`。

在项目根目录先执行离线检查（`python` 不可用时换成实际解释器路径）：

```powershell
python -B -m unittest discover -s tests -p test_single_api_setup.py -v
python -B -m cater --db :memory: --roles examples/single_api_roles.json --drivers examples/single_api_driver_config.json doctor
```

回归测试隔离环境变量、阻断网络、使用内存数据库，不读取真实密钥。`doctor` 只检查配置与事件链，
不验证连通性或供应商余额。缺失或空密钥时全员为 `on_leave / missing_key`，仅设置密钥仍为 `unverified`。

准备实际接入时，将 `endpoint` 换成供应商公开的完整 Chat Completions HTTPS URL（通常以
`/v1/chat/completions` 结尾，不能只填 base URL），将 `model` 换成模型 ID；模板的 `.invalid`
地址不可用于真实请求。驱动使用 Bearer 认证，读取 `choices[0].message.content`，模型需按提示返回
Cater 所需的 JSON。在本机安全设置 `CATER_SHARED_API_KEY`，再启动：

```powershell
python -B -m cater --roles examples/single_api_roles.json --drivers examples/single_api_driver_config.json web --port 8766
```

密钥不要写入 JSON、仓库或聊天。实际运行任务会调用模型并可能收费，第一轮接入验收见
[模型试跑说明](../docs/model-trial.zh-CN.md)。本模板未设置 `token_budget`；如自行添加，
它只按当前事件库中该通道的已报告 token 用量限制后续调用，店长与全员共用，
**不是供应商余额，也不是严格成本上限**，具体限制见下一节。

### Token 统计与预算含义

CLI 明确返回非负整数 `tokens` 时，默认视为适配器报告；可通过 `meta.token_usage: "estimated"` 标为估算。
缺失计数或来源未知时标为 `unavailable`，不当作免费调用。布尔值、负数、浮点数和字符串不算合法计数。
自定义 Python driver 应明确返回 `meta.token_usage: "reported"` 或 `"estimated"`；只返回 `tokens` 不证明来源。

`token_budget` 按驱动通道在当前事件库中的已报告用量，决定是否允许后续调用。
它不是供应商费用硬上限：当前调用可能越过余额，未知/估算用量不纳入已报告账本，失败调用可能仍收费，
CLI 内部重试也可能重复产生费用。需要硬费用限制时还必须使用供应商配额或实际执行后端的调用额度。
复盘会对缺失、估算和失败尝试的未知成本发出提醒；没有足够证据时不声称完整账单已知。
