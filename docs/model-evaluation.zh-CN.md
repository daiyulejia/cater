# 真实模型接入与测试

## Anthropic Messages 协议

Python 核心仍只使用标准库。配置示例见 [anthropic_driver_config.json](../examples/anthropic_driver_config.json)：把 `endpoint` 替换为服务商的完整 Messages URL，`model` 替换为账号可用的模型名，`api_key_env` 只填写环境变量名。

该适配器发送非流式 `POST` 请求，使用顶层 `system`、`messages`、必需的 `max_tokens`、`x-api-key` 和 `anthropic-version`。具体字段见 [Anthropic 官方协议](https://platform.claude.com/docs/en/api/messages/create)。兼容服务的域名和模型由服务商提供；适配器不会自动拼接路径或跨地址重定向。

将密钥放入运行 Cater 的进程环境。Windows 用户也可通过“编辑账户的环境变量”保存，再重新打开终端；不要把值粘贴进聊天、角色配置或 Git 文件。以名称 `CATER_ANTHROPIC_API_KEY` 为例，当前 PowerShell 进程可从用户变量加载：

```powershell
$env:CATER_ANTHROPIC_API_KEY = [Environment]::GetEnvironmentVariable('CATER_ANTHROPIC_API_KEY', 'User')
```

三层角色共享一个通道时，可配合现有角色配置：

```powershell
python -m cater --roles examples/single_api_roles.json --drivers examples/anthropic_driver_config.json web --port 8766
```

角色身份、职责和任务上下文保持各自独立；共享通道同时共享模型、账号额度与调用限制。实际参与成员按任务安排，不强制每次全员调用。

返回的正文只取 `text` 内容块，不保存隐藏推理块。输出被截断、请求工具或缺少有效正文时保留已报告用量，并阻止将不完整文本作为成果。`max_tokens` 对兼容模型可能同时限制推理和最终输出；过小会导致响应不完整，应结合停止原因和真实用量调整。

用量包含服务商报告的输入、输出与缓存字段。缺失或无效的用量标为未知，不估算为真实消耗或货币价格。配置预算按已报告用量检查，不能保证最后一个请求不会超过预算。

若服务商和模型支持，可在配置中显式设置 `"thinking": "disabled"`。默认省略该字段，沿用服务端设置；不要假定兼容服务都支持同样的思考控制。应比较真实任务的质量、时延与用量，再选择是否关闭。

## 群聊理解评估

先测试店长如何理解群消息，再决定如何采用任务变更。以下命令仅向模型发送仓库内的十个合成样例，默认运行前三个，覆盖讨论、新工作和已有要求修改：

```powershell
python scripts/evaluate_room_dialogue.py --config examples/anthropic_driver_config.json --driver shared_api --limit 3
```

也可指定单个场景：

```powershell
python scripts/evaluate_room_dialogue.py --config examples/anthropic_driver_config.json --driver shared_api --case long_message_tiny_change --limit 1
```

该路径不读取个人项目内容，不创建任务、不改要求、不调度执行，不自动重试请求。提供方异常会停止后续调用。通道必须具有正数 `token_budget`；本地演示驱动不能作为真实模型评估通道。CLI 调用不复用先前进程历史。

报告默认写到忽略提交的 `.cater/evaluations/`；可通过 `--output` 指定该目录下尚不存在的 JSON 文件或仓库外路径。请求限制、额度不足或用量未知导致未运行的样例会明确标记，不算成功。报告分别记录：

- 请求是否正常完成、是否出现截断。
- 输出是否符合约定，并引用本条消息和候选任务。
- 建议动作与任务归属是否符合场景预期。
- 每次耗时、已报告用量及停止原因。

当前报告只自动比较建议动作和目标任务。它不证明聊天自然、分工优秀、工程成果正确或长任务可靠；自然表达与交付质量仍须结合实际成果检查。评估脚本本身不采用建议。网页在配置店长模型后，已接入任务内消息的语义分流；这与尚未接回网页的长期群聊多任务接口不同。
