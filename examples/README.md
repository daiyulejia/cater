# Cater Driver 示例

本目录展示如何接入外部工具，同时不让它们渗入 Cater 核心。

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
