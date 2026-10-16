# 可选 MCP stdio 工具

Cater 可通过宿主预先配置的 MCP 服务访问外部工具。目前只支持本机 stdio、文本和结构化结果；不支持 HTTP/SSE、OAuth、采样、roots 或 tasks。Python 核心保持零第三方运行依赖；使用官方 TypeScript SDK 的 Node 桥是可选组件，未配置 MCP 时不需要 Node。

## 配置与启动

由宿主管理员准备可信服务、安装其依赖，并设置独立的 MCP JSON 配置。配置、桥接程序及服务程序须放在角色不可写的位置，例如授权工作目录之外，并通过实际系统权限保护；若已授权的命令或外部工具仍能修改它们，本接线不提供额外权限隔离。[示例配置](../examples/mcp.example.json) 中的绝对路径和工具名均需按本机实际安装替换；记录服务需自行提供，示例不会安装或创建它。

可选 Node 桥位于 [`tools/mcp-bridge`](../tools/mcp-bridge/README.md)。在该目录按其说明安装锁定依赖，再将 `bridge_command` 设置为本机 Node 与 `bridge.mjs` 的绝对路径。Cater 不运行 `npx` 自动安装服务，也不会因服务声明能力而自动扩权。

全局参数 `--mcp` 放在子命令之前：

```powershell
python -m cater --mcp C:/Cater/config/mcp.json web
```

也可与 `--roles`、`--drivers`、`--workspaces`、`--skills` 并用。角色和工作区标识须与对应配置一致；MCP 配置不提供模型驱动，也不会单独赋予本地演示驱动工具调用能力。不传 `--mcp` 时，原有用法不变，不启动 MCP 子进程或发现请求。

配置是严格 JSON，最大 1 MiB，拒绝重复键。顶层包含必需的 `bridge_command` 命令数组和 `servers` 列表；每个服务配置如下：

| 字段 | 含义 |
| --- | --- |
| `id`、`title`、`description` | 服务标识、显示名和用途；描述只用于按需选择。 |
| `command` | 服务可执行文件及参数数组；首项为绝对路径，禁用 shell。 |
| `cwd` | 已存在的绝对工作目录。 |
| `roles` | 必需的非空角色标识列表。 |
| `tools` | 必需的非空工具名映射，每项显式标为 `read` 或 `write`。 |
| `workspaces` | 可选的工作区可见范围；设置后须匹配当前绑定工作区。 |
| `env` | 可选映射：子进程环境变量名 → 宿主环境变量名。 |
| `timeout` | 操作超时，单位为秒。 |

最多配置 16 个服务，每个服务最多授权 64 个工具。服务工具的 `annotations` 不作为授权依据，读写分类以宿主 `tools` 配置为准。

`env` 只存变量名，不存密钥。例如示例将宿主 `CATER_MCP_SERVER_TOKEN` 的值传给子进程 `SERVER_TOKEN`。宿主应在启动前通过自己的环境或密钥管理设施提供值；缺失映射值时拒绝使用相应服务。MCP 子进程只获得最小运行环境（系统默认 PATH、临时目录、UTF-8 设置及 Windows 系统目录）和显式映射，不默认继承模型 API 密钥、用户 HOME 或用户 PATH。Windows 变量名按大写规范化，拒绝配置内的大小写重复项。配置路径、命令、工作目录、环境映射及其值不进入模型目录。

## 按需目录与调用

创建 Runtime、启动 Web 和读取服务索引都不会启动 MCP 进程。角色首先看到按角色和工作区筛选的纯元数据索引，默认每页最多 5 项，包括服务标识、标题、用途、工具数量和配置状态；索引状态不表示服务已经通过连通性验证。

具备可见服务时，执行角色可按需使用：

- `list_mcp_tools(server_id?, offset?, limit?)`：不填服务 ID 时只分页列出宿主配置的服务元数据；填入后才连接指定服务，分页读取已授权工具定义。
- `call_mcp_tool(server_id, tool_name, schema_sha256, arguments)`：使用目录返回的 schema 摘要与参数调用工具。

目录和调用按需建立连接，进程在 pool 中复用。调用前再次核对角色、工作区、当前配置和 schema；工具不存在、未授权、参数不合法或 schema 变化时，在实际调用前拒绝。配置错误不会保留旧 MCP 权限，也不会停止普通工具；修正配置后，后续 MCP 操作重新核对。暂停时不提供上述 MCP 工具；已提交给桥接器的在途请求不会被撤销，仍可能完成初始化、目录核对和调用，返回后保留回执。

只支持文本和结构化结果；多媒体、资源链接或超大结果可能带有表示不完整标记。Cater 不自动下载或打开资源链接，也不会把截断的内容称为完整交付。工具输出作为外部来源信息处理，不构成应用验证或验收批准。

## 授权与运行边界

MCP 授权与工程目录的 `read_only`、`edit`、`execute` 模式独立。`workspaces` 仅限制服务对哪个工作区可见，不限制外部进程能够访问的文件或网络。目录设为只读，不会撤销 MCP 服务的写权限；宿主仍需显式审阅 `roles` 和每个 `tools` 项。

stdio 服务以当前宿主用户权限执行。进程组、超时和关闭管理不提供 OS 沙箱、网络隔离或账号权限隔离。服务必须来自宿主信任的来源，需要额外隔离时由实际执行环境提供。MCP 工具接口不提供新增宿主命令、重写配置或扩大授权的操作；这不能代替对宿主配置和程序的文件权限保护。

## 失败与结果未知

调用前的明确拒绝可记录为 `outcome=not_started`。服务正常返回 `isError: true` 时，是已经返回的工具错误，记录为 `outcome=tool_error`；它不保证没有副作用，也不触发自动重放。

实际调用可能已发送后发生超时、断连或进程崩溃，结果为 `outcome=effects_unknown`、`complete=false`。未知写调用会阻止该服务继续写入及任务完成；应结合回执中的 `invocation_id`、服务日志与真实资源核对状态。失效进程被回收，显式授权的读取工具可用新连接核对，原写入不会重发；读失败不能证明写入未发生。可信操作者需通过 `runtime.team.recovery.reconcile_mcp_call(task_id, started_seq, reason=..., invocation_stopped=True)` 提交实际核对结果并确认原调用已停止；不能通过重试、修改配置、切换目标或同库新群隐藏未知写入。不同数据库、不同配置入口或服务别名不提供全局事务或去重保障。正常 `isError` 回执与未知结果分别处理。

回执使用现有工具开始、结束事件，记录服务、工具、schema 摘要、参数摘要与调用标识，不把通用参数正文复制进持久调用记录。服务自身的返回文本仍可能包含敏感信息，宿主需控制其输出。更多运行恢复边界见 [暂停与故障恢复](recovery.zh-CN.md)。

## Python 嵌入与生命周期

```python
from cater.runtime import Runtime

with Runtime("tasks.db", mcp_path="C:/Cater/config/mcp.json") as runtime:
    index = runtime.mcp.index(role_id="dev", workspace_id="project")
```

普通 Runtime 拥有自己的 MCP pool，`Runtime.close()` 委托 `runtime.mcp.close()` 收尾。Web `serve(..., mcp_path=...)` 创建一个 owner Runtime，把其 pool 传给请求和后台线程各自创建的 Runtime；SQLite 连接保持各线程独立，服务退出时由 owner 统一关闭 pool。

直接使用 `make_handler(..., mcp_path=..., shared_mcp_pool=...)` 时，共享 pool 的调用方负责最终关闭；未传共享 pool 时，各 Runtime 独立拥有自己的 pool。传入 `runtime_factory` 后，工厂无参调用并完整决定 Runtime 配置，`mcp_path` 和共享 pool 不覆盖工厂返回对象。工厂须在使用线程内创建 Runtime，并安排自有共享资源的关闭。
