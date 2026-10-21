import { render } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { EventDetails, EventDetailsList } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'
import { requestFailureFields } from '../src/responseDiagnostics'

const cases = [
  ['dns_lookup_failed', '域名解析失败', 'DNS 与代理设置'],
  ['tls_certificate_error', 'TLS 证书校验失败', '不要关闭证书校验'],
  ['tls_error', 'TLS 连接异常', '不要通过关闭安全校验绕过'],
  ['connection_refused', '连接被拒绝', '拒绝来自服务端还是中间网络'],
  ['connection_reset', '连接被重置', '无法仅凭此记录认定请求未被处理'],
  ['broken_pipe', '连接写入中断', '勿直接重复操作'],
  ['remote_disconnected', '远端断开连接', '不代表远端没有收到或处理请求'],
  ['incomplete_response', 'HTTP 响应读取不完整', '不将截断响应当成完整结果'],
  ['http_protocol_error', 'HTTP 传输协议异常', '不是模型正文格式或余额的判断'],
] as const

const event = (issue: unknown, kind = 'network_error'): DetailEvent => ({
  seq: 20, type: 'team.run_finished', actor: { id: 'team', kind: 'system', role: 'team' },
  payload: { status: 'failed', role: 'dev', run_id: 'fixture', message_seq: 2,
    failure_stage: 'model_request', failure_kind: kind, transport_phase: 'request', transport_issue: issue,
    error_type: 'DriverUnavailable', reason: 'PRIVATE_EXCEPTION PRIVATE_URL PRIVATE_KEY',
    provider_body: 'PRIVATE_BODY' },
})

test.each([
  ['deadline_exceeded', '不代表远端已停止'],
  ['execution_budget', '本轮已达到配置的总执行预算，未据此认定接入故障'],
  ['idle_timeout', '等待新的有效工具结果已超时，不代表远端已停止'],
  ['process_exited', '客户端在返回结果前退出'],
  ['request_rejected', '具体原因尚未确认'],
  ['protocol_error', '客户端通信格式不符合约定'],
  ['transport_unavailable', '客户端连接不可用'],
  ['external_error', '原因待核查'],
])('external diagnosis %s is visible without exposing exception text', (code, label) => {
  const source = event(undefined)
  source.payload = { status: 'failed', failure_stage: 'failed', external_failure_code: code,
    reason: 'PRIVATE_EXCEPTION', error_type: 'PRIVATE_CLASS' }
  const view = render(<EventDetails event={source} />)
  expect(view.container.textContent).toContain(label)
  expect(view.container.textContent).toContain('诊断不代表允许重试')
  expect(view.container.textContent).not.toMatch(/PRIVATE_/)
})

test('external codes must be known own fields and come from a host event', () => {
  for (const value of ['PRIVATE_UNKNOWN', '__proto__', 'constructor', {}, null]) {
    expect(requestFailureFields({ external_failure_code: value })).toEqual([])
  }
  expect(requestFailureFields(Object.create({ external_failure_code: 'deadline_exceeded' }))).toEqual([])
  const source = event(undefined)
  source.payload.external_failure_code = 'deadline_exceeded'
  source.actor = { id: 'dev', kind: 'agent', role: 'dev' }
  const view = render(<EventDetails event={source} />)
  expect(view.container.textContent).not.toContain('外部客户端诊断')
})

test('legacy external errors do not acquire an inferred timeout category', () => {
  const source = event(undefined)
  source.payload = { status: 'failed', failure_stage: 'failed', error_type: 'AcpUnavailable',
    reason: 'PRIVATE_EXCEPTION' }
  const view = render(<EventDetails event={source} />)
  expect(view.container.textContent).not.toMatch(/PRIVATE_|超时|时限|拒绝|外部客户端诊断/)
  expect(view.container.textContent).toContain('消息处理失败')
})

test.each(cases)('%s only shows registered Chinese observations and directions', (issue, label, direction) => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const view = render(<EventDetails event={event(issue)} />)
  expect(view.container.querySelector('dl')?.textContent).toContain(label)
  expect(view.container.textContent).toContain(direction)
  expect(view.container.textContent).toContain('不是完整根因')
  expect(view.container.textContent).toContain('不证明远端未执行或未计费')
  expect(view.container.textContent).toContain('诊断不代表允许重试')
  expect(view.container.textContent).not.toMatch(/PRIVATE_|network_error|transport_issue|DriverUnavailable/)
  expect(view.container.querySelector('button, a, input')).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

test.each([undefined, null, 'PRIVATE_UNKNOWN', '__proto__', 'constructor', false, 1, {}, []])('old or unknown detail %j never guesses a connection category', issue => {
  const fields = requestFailureFields(event(issue).payload)
  expect(fields).toEqual([['请求诊断', '模型连接或传输失败'], ['传输浮现阶段', '请求过程中']])
})

test.each(['timeout', 'http_error', 'missing_credentials', 'invalid_request', 'PRIVATE_UNKNOWN'])('%s cannot adopt a network-only issue', kind => {
  const fields = requestFailureFields(event('tls_certificate_error', kind).payload)
  expect(fields.some(([name]) => name === '已记录连接类别' || name === '核对方向')).toBe(false)
})

test('inherited issue is not a recorded field', () => {
  const payload = Object.assign(Object.create({ transport_issue: 'tls_certificate_error' }), {
    failure_kind: 'network_error', transport_phase: 'request',
  })
  expect(requestFailureFields(payload)).toHaveLength(2)
})

test('non-host actor cannot appear as a trusted connection diagnosis in the event list', () => {
  const receipt = event('tls_certificate_error')
  const view = render(<EventDetailsList events={[{ ...receipt, actor: { id: 'dev', kind: 'agent', role: 'dev' } }]} />)
  expect(view.container.textContent).not.toContain('已记录连接类别')
  expect(view.container.textContent).not.toContain('不要关闭证书校验')
  expect(view.container.textContent).not.toMatch(/PRIVATE_|tls_certificate_error/)
})
