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
