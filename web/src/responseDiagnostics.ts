const reasons = {
  incomplete_stop: '响应未完整结束',
  invalid_content: '响应内容格式无效',
  inconsistent_stop: '结束状态与响应内容不一致',
  unsupported_content: '响应包含不支持的内容类型',
  invalid_tool_calls: '响应中的工具请求格式无效',
} as const
const protocols = { openai_compatible: 'OpenAI 兼容协议', anthropic: 'Anthropic 协议' } as const
const toolIssues = {
  calls_not_array: '工具请求列表不是数组',
  invalid_call_shape: '工具请求结构无效',
  unsupported_call_type: '工具请求类型不受支持',
  invalid_function: '工具函数定义无效',
  invalid_call_id: '工具请求标识无效',
  duplicate_call_id: '工具请求标识重复',
  invalid_tool_name: '工具名称无效',
  unregistered_tool: '请求了本轮未提供的工具',
  arguments_not_text: '工具参数未使用 JSON 文本',
  invalid_arguments_json: '工具参数不是有效且无歧义的 JSON',
  arguments_not_object: '工具参数不是对象',
  invalid_arguments_value: '工具参数值无效',
} as const
const openaiStops = {
  stop: '正常结束标记', tool_calls: '工具请求标记', length: '达到输出长度限制',
  content_filter: '内容过滤标记', function_call: '旧式函数请求标记',
} as const
const anthropicStops = {
  end_turn: '回合结束标记', stop_sequence: '停止序列标记', tool_use: '工具请求标记',
  max_tokens: '达到输出长度限制', pause_turn: '回合暂停标记', refusal: '拒绝响应标记',
  model_context_window_exceeded: '上下文窗口超限标记',
} as const
const stages = {
  setup: '准备处理', context_assembly: '组装上下文', model_request: '请求模型',
  response_handling: '处理模型响应', tool_dispatch: '分派工具', tool_execution: '执行工具',
  tool_result_recording: '记录工具结果',
} as const
const transportIssues = {
  dns_lookup_failed: ['域名解析失败', '核对服务地址、DNS 与代理设置；此记录不能区分配置错误或临时故障。'],
  tls_certificate_error: ['TLS 证书校验失败', '核对服务证书、系统时间及受信代理证书；不要关闭证书校验。'],
  tls_error: ['TLS 连接异常', '核对服务端 TLS 与代理配置；不要通过关闭安全校验绕过。'],
  connection_refused: ['连接被拒绝', '核对地址、端口及服务状态；记录不能确定拒绝来自服务端还是中间网络。'],
  connection_reset: ['连接被重置', '核对服务与代理的连接日志；无法仅凭此记录认定请求未被处理。'],
  broken_pipe: ['连接写入中断', '核对连接及代理日志，先检查此前回执，勿直接重复操作。'],
  remote_disconnected: ['远端断开连接', '核对服务和代理日志；连接断开不代表远端没有收到或处理请求。'],
  incomplete_response: ['HTTP 响应读取不完整', '核对服务与代理的响应日志；保留此前成果，不将截断响应当成完整结果。'],
  http_protocol_error: ['HTTP 传输协议异常', '核对服务地址、协议及代理配置；这不是模型正文格式或余额的判断。'],
} as const

export type ResponseDiagnostics = {
  responseStatus?: 'contained' | 'protocol_error'
  responseReason?: keyof typeof reasons
  responseProtocol?: keyof typeof protocols
  responseToolIssue?: keyof typeof toolIssues
  responseStop?: keyof typeof openaiStops | keyof typeof anthropicStops
  responseClientToolsExecuted?: false
  failureStage?: keyof typeof stages
  responseDiagnosticsConflict?: true
}

function known<T extends object>(value: unknown, allowed: T): keyof T | undefined {
  return typeof value === 'string' && Object.hasOwn(allowed, value) ? value as keyof T : undefined
}

function toolIssue(protocol: unknown, reason: unknown, value: unknown): keyof typeof toolIssues | undefined {
  return protocol === 'openai_compatible' && reason === 'invalid_tool_calls'
    || protocol === 'anthropic' && reason === 'invalid_content' ? known(value, toolIssues) : undefined
}

export function responseDiagnostics(payload: Record<string, unknown>, includeStage = false): ResponseDiagnostics {
  const get = (key: string) => Object.hasOwn(payload, key) ? payload[key] : undefined
  const result: ResponseDiagnostics = {}
  const status = get('response_status')
  if (status === 'contained' || status === 'protocol_error') result.responseStatus = status
  if (status === 'contained') {
    const reason = known(get('response_reason'), reasons)
    const protocol = known(get('response_protocol'), protocols)
    if (reason) result.responseReason = reason
    const issue = toolIssue(protocol, reason, get('response_tool_issue'))
    if (issue) result.responseToolIssue = issue
    if (protocol) {
      result.responseProtocol = protocol
      const stop = protocol === 'anthropic' ? known(get('response_stop'), anthropicStops) : known(get('response_stop'), openaiStops)
      if (stop) result.responseStop = stop
    }
    if (get('response_client_tools_executed') === false) result.responseClientToolsExecuted = false
  }
  const stage = includeStage ? known(get('failure_stage'), stages) : undefined
  if (stage) result.failureStage = stage
  return result
}

export const containedNote = '已保留先前成果，本次响应未执行宿主工具；格式纠正情况以关联记录为准。先前操作及本次响应的远端或驱动内部执行、计费仍需核对回执与服务商账单。'
export const stageNote = '阶段仅表示错误浮现的位置，不代表根因或允许重试。'

export function requestFailureFields(payload: Record<string, unknown>): [string, string][] {
  const get = (key: string) => Object.hasOwn(payload, key) ? payload[key] : undefined
  const kinds = {
    http_error: '模型服务返回 HTTP 错误', network_error: '模型连接或传输失败', timeout: '模型请求或响应超时',
    invalid_request: '请求参数或接入配置未通过检查', missing_credentials: '本机未读到该通道的凭据',
  }
  const kind = known(get('failure_kind'), kinds)
  const code = get('status_code')
  const fields: [string, string][] = []
  const externalCodes = {
    deadline_exceeded: '本地等待时限已到，不代表远端已停止',
    execution_budget: '本轮已达到配置的总执行预算，未据此认定接入故障',
    idle_timeout: '等待客户端输出或工具结果已超时，不代表远端已停止',
    process_exited: '客户端在返回结果前退出',
    request_rejected: '客户端或服务端拒绝了请求，具体原因尚未确认',
    protocol_error: '客户端通信格式不符合约定',
    transport_unavailable: '客户端连接不可用',
    external_error: '外部调用未完成，原因待核查',
  }
  const external = known(get('external_failure_code'), externalCodes)
  if (external) fields.push(['外部客户端诊断', externalCodes[external]])
  if (external === 'request_rejected') {
    const rpcCode = get('external_rpc_code')
    const methods = {
      initialize: '初始化连接（initialize）',
      'session/new': '创建会话（session/new）',
      'session/prompt': '提交会话请求（session/prompt）',
    }
    const method = known(get('external_rpc_method'), methods)
    const validCode = typeof rpcCode === 'number' && Number.isInteger(rpcCode) && rpcCode >= -2147483648 && rpcCode <= 2147483647
    if (validCode) fields.push(['ACP RPC 错误码', String(rpcCode)])
    if (method) fields.push(['ACP RPC 方法', methods[method]])
    if (validCode || method) fields.push(['ACP RPC 诊断边界', 'RPC 码不代表 HTTP 状态码或 CLI 结果码，也不代表余额、额度或允许重试；未知数字不据此推断根因。'])
  }
  if (kind) fields.push(['请求诊断', kinds[kind]])
  if (typeof code === 'number' && Number.isInteger(code) && code >= 100 && code <= 599) {
    fields.push(['HTTP 状态', code === 429 ? 'HTTP 429（服务商限流）' : `HTTP ${code}`])
  }
  const phase = known(get('transport_phase'), { request: true, response_read: true })
  if (kind && phase) fields.push(['传输浮现阶段', phase === 'response_read' ? '接收响应时' : '请求过程中'])
  const issue = kind === 'network_error' ? known(get('transport_issue'), transportIssues) : undefined
  if (issue) {
    const [observation, direction] = transportIssues[issue]
    fields.push(['已记录连接类别', observation], ['核对方向', direction],
      ['诊断边界', '类别来自底层异常，不是完整根因，也不证明远端未执行或未计费。'])
  }
  return fields
}

export function responseDiagnosticFields(diagnostic: ResponseDiagnostics): [string, string][] {
  const fields: [string, string][] = []
  if (diagnostic.responseStatus === 'contained') {
    fields.push(['响应状态', '响应未通过完整性检查'], ['响应原因', diagnostic.responseReason ? reasons[diagnostic.responseReason] : '具体原因未记录'])
    if (diagnostic.responseProtocol) fields.push(['响应协议', protocols[diagnostic.responseProtocol]])
    const issue = toolIssue(diagnostic.responseProtocol, diagnostic.responseReason, diagnostic.responseToolIssue)
    if (issue) fields.push(['工具请求诊断', toolIssues[issue]])
    if (diagnostic.responseStop && diagnostic.responseProtocol) {
      const stops = diagnostic.responseProtocol === 'anthropic' ? anthropicStops : openaiStops
      if (Object.hasOwn(stops, diagnostic.responseStop)) fields.push(['响应结束状态', (stops as Record<string, string>)[diagnostic.responseStop]])
    }
    if (diagnostic.responseClientToolsExecuted === false) fields.push(['本次响应工具', '这次响应的宿主工具请求未执行'])
  }
  if (diagnostic.failureStage) fields.push(['错误浮现阶段', stages[diagnostic.failureStage]])
  if (diagnostic.responseDiagnosticsConflict) fields.push(['响应诊断核对', '同一事件的响应诊断不一致，差异字段未采用'])
  return fields
}

export function commonResponseDiagnostics(a: ResponseDiagnostics, b: ResponseDiagnostics): ResponseDiagnostics {
  const result: ResponseDiagnostics = {}
  const keys = ['responseStatus', 'responseReason', 'responseProtocol', 'responseStop', 'responseToolIssue', 'responseClientToolsExecuted'] as const
  for (const key of keys) {
    if (a[key] !== undefined && a[key] === b[key]) Object.assign(result, { [key]: a[key] })
  }
  if (a.responseDiagnosticsConflict || b.responseDiagnosticsConflict
    || keys.some(key => a[key] !== b[key])) {
    result.responseDiagnosticsConflict = true
  }
  return result
}
