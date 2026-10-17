import { render, screen, within, fireEvent } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'
import { ModelUsagePanel } from '../src/ModelUsagePanel'
import { usageSummary } from '../src/modelUsage'
import { responseDiagnostics, responseDiagnosticFields } from '../src/responseDiagnostics'

const event = (payload: Record<string, unknown>, type = 'team.model_called', seq = 1): DetailEvent => ({
  seq, type, actor: { id: 'team', kind: 'system', role: 'team' }, payload: { role: 'dev', ...payload },
})
const contained = {
  response_status: 'contained', response_reason: 'incomplete_stop', response_protocol: 'openai_compatible',
  response_stop: 'length', response_client_tools_executed: false,
}
const reasonCases = [
  ['incomplete_stop', '响应未完整结束'], ['invalid_content', '响应内容格式无效'],
  ['inconsistent_stop', '结束状态与响应内容不一致'], ['unsupported_content', '响应包含不支持的内容类型'],
  ['invalid_tool_calls', '响应中的工具请求格式无效'],
]

test.each(reasonCases)('两处既有展示用中文解释 %s，计量独立保留', (reason, chinese) => {
  const receipt = event({ ...contained, response_reason: reason, tokens: 8700, token_usage: 'reported',
    usage: { prompt_tokens: 8185, completion_tokens: 515 }, failure_stage: 'response_handling' })
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const view = render(<ModelUsagePanel events={[receipt, structuredClone(receipt)]} names={{ dev: '开发成员' }} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('8,700 token')
  expect(screen.getByText('成员用量与最近返回').closest('details')!.open).toBe(false)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  const recent = screen.getByRole('list', { name: '最近模型返回' })
  expect(within(recent).getAllByRole('listitem')).toHaveLength(1)
  expect(recent.textContent).toContain(chinese)
  expect(recent.textContent).toContain('这次响应的宿主工具请求未执行')
  expect(recent.textContent).toContain('先前操作及本次响应的远端或驱动内部执行、计费仍需核对')
  expect(view.container.querySelector('button, a, input')).toBeNull()
  expect(view.container.querySelectorAll('summary')).toHaveLength(1)
  view.rerender(<EventDetails event={{ ...receipt, type: 'team.run_finished', payload: { ...receipt.payload, status: 'failed' } }} />)
  const fields = view.container.querySelector('dl')!
  expect(fields.textContent).toContain(chinese)
  expect(fields.textContent).toContain('错误浮现阶段处理模型响应')
  expect(view.container.textContent).toContain('不代表根因或允许重试')
  expect(fields.textContent).not.toMatch(/token|已报告用量|余额/)
  expect(view.container.querySelector('button, a, input')).toBeNull()
  expect(view.container.querySelectorAll('summary')).toHaveLength(1)
  view.rerender(<EventDetails event={receipt} />)
  expect(screen.getByText('用量状态').nextElementSibling?.textContent).toBe('用量已报告')
  expect(screen.getByText('已报告用量').nextElementSibling?.textContent).toBe('8700 token')
  expect(screen.queryByText('错误浮现阶段')).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

test.each([
  ['openai_compatible', ['stop', 'tool_calls', 'length', 'content_filter', 'function_call']],
  ['anthropic', ['end_turn', 'stop_sequence', 'tool_use', 'max_tokens', 'pause_turn', 'refusal', 'model_context_window_exceeded']],
] as const)('协议 %s 的停止状态严格按既有枚举及协议匹配', (protocol, stops) => {
  for (const stop of stops) {
    const diagnostic = responseDiagnostics({ ...contained, response_protocol: protocol, response_stop: stop })
    expect(diagnostic.responseStop).toBe(stop)
    const fields = responseDiagnosticFields(diagnostic)
    expect(fields.find(([name]) => name === '响应结束状态')?.[1]).toMatch(/[\u4e00-\u9fff]/)
  }
  const other = protocol === 'anthropic' ? 'length' : 'refusal'
  expect(responseDiagnostics({ ...contained, response_protocol: protocol, response_stop: other }).responseStop).toBeUndefined()
})

test.each(['setup', 'context_assembly', 'model_request', 'response_handling', 'tool_dispatch', 'tool_execution', 'tool_result_recording'])('仅有阶段 %s 的异常不推断响应原因或工具执行', failure_stage => {
  const view = render(<EventDetails event={event({ failure_stage, status: 'failed', reason: 'PRIVATE_EXCEPTION' }, 'team.run_finished')} />)
  expect(screen.getByText('错误浮现阶段')).toBeTruthy()
  expect(view.container.textContent).toContain('不代表根因或允许重试')
  expect(view.container.textContent).not.toMatch(/PRIVATE_EXCEPTION|响应未通过完整性检查|宿主工具请求未执行|未自动续试|余额/)
})

test.each(['__proto__', 'constructor', 'toString', '<img src=x onerror=PRIVATE_META>', true, [], {}])('未知恶意元信息不从摘要或开发者详情泄露：%j', malicious => {
  const payload = { ...contained, response_reason: malicious, response_protocol: malicious, response_stop: malicious,
    failure_stage: malicious, response_client_tools_executed: malicious,
    reason: 'PRIVATE_EXCEPTION', meta: { text: 'PRIVATE_TEXT', provider_content: 'PRIVATE_PROVIDER', reasoning: 'PRIVATE_REASONING', signature: 'PRIVATE_SIGNATURE', arguments: 'PRIVATE_ARGS' } }
  const call = usageSummary([event(payload)]).calls[0]
  expect(call).toMatchObject({ responseStatus: 'contained', tokens: null })
  expect(call.responseReason).toBeUndefined()
  expect(call.responseProtocol).toBeUndefined()
  expect(call.responseStop).toBeUndefined()
  const view = render(<ModelUsagePanel events={[event(payload)]} names={{}} />)
  expect(view.container.textContent).not.toMatch(/PRIVATE_|onerror|constructor|__proto__|toString|0 token|成功/)
  for (const type of ['team.run_finished', 'team.model_called']) {
    view.rerender(<EventDetails event={event({ ...payload, status: 'failed' }, type)} />)
    expect(view.container.textContent).not.toMatch(/PRIVATE_|onerror|constructor|__proto__|toString|0 token|成功/)
    expect(view.container.querySelector('img, script, button, a')).toBeNull()
    expect(screen.getByText('具体原因未记录')).toBeTruthy()
    expect(screen.queryByText('这次响应的宿主工具请求未执行')).toBeNull()
  }
})

test.each(['reported', 'estimated', 'unavailable', undefined])('响应诊断不改变计量来源 %s 或凭空补零', token_usage => {
  const receipt = event({ ...contained, token_usage, tokens: 19 })
  const summary = usageSummary([receipt, { ...receipt, type: 'team.run_finished', seq: 2 }])
  expect(summary.calls).toHaveLength(1)
  expect(summary.calls[0].source).toBe(token_usage ?? 'unspecified')
  expect(summary.reportedTokens).toBe(token_usage === 'reported' ? 19 : null)
  expect(summary.estimatedTokens).toBe(token_usage === 'estimated' ? 19 : null)
  const missing = usageSummary([event({ ...contained, token_usage })])
  expect(missing.calls[0].tokens).toBeNull()
  expect(missing.reportedTokens).toBeNull()
  expect(missing.estimatedTokens).toBeNull()
})

test.each(['response_reason', 'response_protocol', 'response_stop', 'response_client_tools_executed', 'response_status'])('相同计数不同 %s 不被重复事件合并吞掉，重放不恢复诊断或计数', key => {
  const first = event({ ...contained, token_usage: 'reported', tokens: 10 })
  const values: Record<string, unknown> = { response_reason: 'invalid_content', response_protocol: 'anthropic',
    response_stop: 'content_filter', response_client_tools_executed: true, response_status: 'protocol_error' }
  const second = event({ ...first.payload, [key]: values[key] })
  const summary = usageSummary([first, second, first, second])
  expect(summary).toEqual(usageSummary([second, first, second, first]))
  expect(summary.calls).toHaveLength(1)
  expect(summary.calls[0]).toMatchObject({ responseDiagnosticsConflict: true, source: 'unavailable', tokens: null })
  expect(summary.reportedTokens).toBeNull()
  render(<ModelUsagePanel events={[first, second]} names={{}} />)
  expect(screen.getByText(/同一事件的响应诊断不一致/)).toBeTruthy()
})

test('旧3310无新增字段不补诊断，继承的字段与伪造宿主不背书', () => {
  const old = event({ status: 'failed', reason: '原有安全失败说明' }, 'team.run_finished', 3310)
  const view = render(<EventDetails event={old} />)
  expect(screen.getByText('原有安全失败说明')).toBeTruthy()
  expect(view.container.textContent).not.toMatch(/响应未通过完整性检查|错误浮现阶段|宿主工具请求未执行/)
  expect(responseDiagnostics(Object.create(contained), true)).toEqual({})
  for (const type of ['team.run_finished', 'team.model_called']) {
    view.rerender(<EventDetails event={{ ...event(contained, type), actor: { id: 'team', kind: 'agent', role: 'team' } }} />)
    expect(view.container.textContent).not.toMatch(/响应未通过完整性检查|宿主工具请求未执行/)
  }
})

test.each([
  [{ failure_kind: 'http_error', status_code: 429 }, 'HTTP 429（服务商限流）'],
  [{ failure_kind: 'http_error', status_code: 503 }, 'HTTP 503'],
  [{ failure_kind: 'network_error', transport_phase: 'response_read' }, '模型连接或传输失败'],
  [{ failure_kind: 'timeout', transport_phase: 'request' }, '模型请求或响应超时'],
  [{ failure_kind: 'invalid_request' }, '请求参数或接入配置未通过检查'],
  [{ failure_kind: 'missing_credentials' }, '本机未读到该通道的凭据'],
])('安全HTTP及传输诊断随新增阶段保留，不显示异常原文：%j', (diagnostic, chinese) => {
  const view = render(<EventDetails event={event({ ...diagnostic, failure_stage: 'model_request', status: 'failed',
    reason: 'PRIVATE_EXCEPTION', error_type: 'PRIVATE_CLASS', transport: 'PRIVATE_TRANSPORT' }, 'team.run_finished')} />)
  expect(view.container.querySelector('dl')!.textContent).toContain(chinese)
  expect(view.container.textContent).toContain('远端是否已处理或计费需核对')
  expect(view.container.textContent).not.toMatch(/PRIVATE_|宿主工具请求未执行|响应未通过完整性检查/)
  expect(screen.getByLabelText('安全事件 JSON').textContent).toContain(chinese)
})

test('未知协议、停止值、阶段及传输字段不反射，contained布尔字段严格且不猜成功', () => {
  const payload = { response_status: 'PRIVATE_STATUS', response_reason: 'PRIVATE_REASON', response_stop: 'PRIVATE_STOP',
    response_protocol: 'PRIVATE_PROTOCOL', failure_stage: 'PRIVATE_STAGE', failure_kind: 'PRIVATE_KIND',
    status_code: 'PRIVATE_CODE', transport_phase: 'PRIVATE_PHASE', reason: 'PRIVATE_EXCEPTION', response_client_tools_executed: 'false' }
  const view = render(<EventDetails event={event(payload, 'team.run_finished')} />)
  expect(view.container.textContent).not.toMatch(/PRIVATE_|成功|完成|响应未通过完整性检查|错误浮现阶段|宿主工具请求未执行/)
  expect(responseDiagnostics({ ...contained, response_client_tools_executed: 'false' }).responseClientToolsExecuted).toBeUndefined()
  expect(responseDiagnostics({ ...contained, response_status: true })).toEqual({})
  expect(responseDiagnostics({ ...contained, response_protocol: 'PRIVATE_PROTOCOL' }).responseStop).toBeUndefined()
})
