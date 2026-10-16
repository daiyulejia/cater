# 可选 MCP stdio 桥接器

中文 | [English](README.en.md)

独立 Node 适配器，使用官方 `@modelcontextprotocol/sdk@1.32.1`。Python 无新增运行依赖。首次收到实际目录或调用请求才启动下游；主进程 stdin/stdout 仅使用内部 NDJSON，下游初始化、JSON-RPC、请求 ID、取消和关闭由 SDK 负责。

## 安装与测试

需要 Node >=18.14.1。SDK 声明 >=18，固定依赖树中的 `@hono/node-server@1.19.15` 声明 >=18.14.1；它满足 SDK 的依赖范围并避开旧版静态服务通告。此桥接器不加载 HTTP server 路径。本次实际测试使用 Windows / Node 24.21.0，没有执行其他 Node 版本。

```powershell
Set-Location tools/mcp-bridge
npm ci --ignore-scripts --registry=https://registry.npmjs.org
node --test test/bridge.test.mjs
```

固定 package-lock 由 `npm install --ignore-scripts` 生成，resolved URL 统一为官方 registry。直接依赖包括 SDK、Ajv 8.20.0、ajv-formats 3.0.1、Zod 4.6.7，全部固定版本。没有安装或运行第三方 MCP 服务，没有调用模型、账号或公网 MCP。

## 宿主边界

- 配置、桥接器和服务程序须由宿主保护，放在角色不可写位置并落实系统文件权限。工具接口没有改配置操作，不等于能阻止另一个已授权命令修改这些文件；本适配器不弥补这种权限暴露。
- `server.command[0]` 和 `cwd` 必须为绝对路径。程序必须是现存普通文件，cwd 必须是目录。Windows 额外要求 `.exe` 和 `MZ` 文件头，防止 cross-spawn 退回 cmd/script 执行；脚本通过绝对 Node/Python 可执行程序加脚本参数启动。
- 配置在首次有效请求绑定，后续 command/cwd/env/allowed_tools 变化均拒绝。允许改变 timeout 和目录页码；allowlist 顺序不影响身份。断开后同一个 wrapper 永不重连。
- 桥接启动后清除自身父环境，并屏蔽 SDK 的默认继承变量；下游只有 IPC `server.env` 显式提供的变量。Windows 变量名归一为大写，大小写重复键拒绝。不会修改 Python 宿主环境。
- 本机 Windows 的 Node fixture 必须在 IPC 中获得 `SYSTEMROOT`，否则 Node 在 SDK 初始化前退出。Cater 的 Python 宿主会提供最小系统环境，再覆盖显式配置的映射；直接使用本桥接器的其他宿主需自行提供运行环境。桥接本身不继承额外环境或扩大权限。
- stderr 在启动前接好排空并丢弃；错误只返回固定安全码，不转发异常、命令或环境。SDK 初始化 instructions、工具 annotations、原始 `_meta` 不进入目录或宿主状态。
- capabilities 为空，不提供 sampling/roots/elicitation；required-task 工具在调用前拒绝。不会读取资源链接、下载多媒体或执行服务端 instructions。
- 仅为协议适配，不是 OS 沙箱。SDK close 关闭直接子进程并按 SDK 策略升级终止；宿主 ProcessScope 负责强杀、Windows 不可捕获终止和进程树兜底。

## 内部协议

每个请求必须单行 UTF-8 JSON，以换行结束；串行处理，不自动重试：

```json
{"type":"complete","id":"opaque","action":"list","server":{"command":["ABS_EXE","ABS_SCRIPT"],"cwd":"ABS_DIR","env":{}},"allowed_tools":["write_note","read_note"],"timeout_ms":30000,"offset":0,"limit":10}
```

call 另需 `tool_name`、小写 64 位 `schema_sha256`、对象 `arguments`、非空 `invocation_id`。通过 `_meta:{"cater/invocation_id":"..."}` 传给服务用于日志核对。响应原样绑定内部 request id；SDK 独立绑定下游 RPC id，不用服务端数据覆盖宿主 id/status。

正常内部输出总是 `{id,ok:true,result:{...}}`；无效 IPC 使用 `{id,ok:false,error:"固定安全码"}`，无法取得有效 id 时为 null。

目录 result：`status:"ok",operation:"list",tools,total,offset,next_offset,has_more,complete:true`。工具项仅 `name,title,description,input_schema,schema_sha256`；缺少的 title/description 为空字符串。effect 由 Python 宿主补入，绝不采纳服务提示。`next_offset` 始终为整数，末页等于 total；超出 total 的 offset 收敛到 total。请求 limit 为 1..64，实际每页最多 20 项，并按宿主 result 字节预算缩小页面。目录错误返回固定 code、message、`status:"error",operation:"list",complete:true`。

schema hash 固定为：

```text
SHA256(UTF8(canonicalJSON({name:tool.name,inputSchema:tool.inputSchema})))
```

canonicalJSON 对每层对象的键用 JS `sort()` 字典序排序，递归处理；数组顺序不变，标量用 JSON.stringify，紧凑无空格。直接构造 JSON 字符串，正确保留 `__proto__` 和整数样式键的排序。title/description/outputSchema/annotations 不参与 hash；Python 只传回 hash。

调用前重新读取全部有界目录，检查 allowlist、工具存在、hash、required tasks 和真实 JSON Schema validator。使用实际安装 SDK 导出的 `AjvJsonSchemaValidator`，配置严格 Ajv 2020-12（也接受显式 draft-07）；未知 keyword/format、无法编译的 schema、外部未解析 `$ref` 均安全拒绝，不联网取 schema。每个 schema 使用新的 Ajv，避免 `$id` 碰撞和无界缓存。不应用默认值、不改写类型、不删除属性。

SDK `listTools()` 每页会覆盖元数据缓存，且缓存函数在声明中为 private，因此目录用 SDK 公开 `request` 配合 `ListToolsResultSchema` 分页，避免对未授权定义预编译。只给选中工具编译输入/输出 validator。`callTool()` 校验 terminal 协议形状，再按 SDK 同样规则校验 structuredContent；不调用 SDK 私有方法。

## 结果与未知

- preflight 拒绝：`status:error,complete:true,outcome:not_started,code,message`，仅发生在调用 SDK `callTool()` 之前。
- 有效 terminal：`status:ok|error,complete:true,outcome:returned|tool_error,mcp_result`，严格按原 `isError` 决定。isError 允许已经发生写入，不表示无副作用，不触发自动重试。
- SDK callTool 后协议/输出校验失败、断开、超时或无法确认结果：`status:error,complete:false,outcome:effects_unknown,code:MCP_EFFECTS_UNKNOWN,message`。立即废弃连接，之后同一 wrapper 拒绝请求，不重发原调用。Python 应回收失效 wrapper，让后续操作者明确发起的读取使用新桥接；未知写保护仍由宿主持久回执决定。
- terminal 的正文不能完整表示时，仍保持 `complete:true` 和原 `returned|tool_error`，另设 `representation_complete:false,code:MCP_REPRESENTATION_INCOMPLETE`。多媒体/资源块列入 `unsupported_content_types`，不假装文本全文。`mcp_result` 保留有界文本/结构化对象及原 isError；扩展元数据不作为宿主控制字段。
- 每个有效 terminal 都保留 `result_bytes/result_sha256`，针对完整收到的 result JSON 值按 JSON.stringify 语义序列化后的 UTF-8（不含 RPC envelope/换行，不是原始 wire 空白/转义字节）。摘要计算迭代完成，深层结构化内容可省略展示并保留正确 hash。表示不完整时另给 `result_excerpt`，文本摘要最多 1024 字节；未支持内容可通过类型列表识别。

result 根字段严格限于：`status,complete,outcome,operation,tools,total,offset,next_offset,has_more,code,message,note,mcp_result,representation_complete,unsupported_content_types,result_bytes,result_sha256,result_excerpt,schema_sha256`。没有新增 outcome，也不返回 server instructions、command/env 或 host effect。

## 大小与生命周期

| 项目 | 上限 |
| --- | --- |
| 内部请求/输出整行，含换行 | 1 MiB |
| 宿主 result | 256 KiB |
| mcp_result JSON 正文 | 64 KiB |
| 单工具原始定义 | 64 KiB |
| 一轮目录 | 128 页、4096 项、累计 8 MiB |
| allowlist | 64 项 |
| 单页目录输出 | 20 项并受 result 字节上限约束 |
| 输入/schema/展示深度 | 64 |
| SDK 下游读缓冲 | 4 MiB |
| 单请求 timeout_ms | 1..600000，默认 30000 |

超出 SDK transport 缓冲的响应无法建立可信 terminal，因此调用后为未知；已通过 SDK 验证的大响应则有界表示并保留 terminal。单请求初始化、分页和调用共用单调时钟 deadline，不因进度通知延长。stdin EOF、可捕获 SIGINT/SIGTERM、stdout 管道关闭都会收尾。Windows 强制终止不能执行 JS signal handler，应依靠宿主进程域。

## 本地 Fixture

`test/fixture-server.mjs normal LOG_ABS STATE_ABS` 使用同版本官方 SDK Server/StdioServerTransport。目录两页：第一页 write_note 和隐藏工具，第二页 read_note。`write_note` 要求字符串 text，可选 behavior；先追加 `text + "\n"` 再返回或触发测试故障，便于证明 isError/崩溃/超时并非无副作用。read_note 接收 `{}`，读取实际文件。

fixture 只用于本地测试，会把环境和初始化/调用元信息写入指定私有日志以校验隔离；只使用测试值，切勿传入真实凭据。测试目录均在本工具目录内创建并收尾。套件覆盖初始化、分页、hash、严格输入拒绝零执行、schema 变化、输出校验、isError、写后崩溃/超时/超限、关闭、隐私、大文本、转义膨胀、深层/大结构化数据、多媒体、不读取链接和无 instructions 注入。当前 Windows 上可捕获 POSIX 信号用例会显式跳过，不据此宣称已验证跨平台强杀清理。
