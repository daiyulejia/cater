import { render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'
import { requestFailureFields } from '../src/responseDiagnostics'
import { teamRunStatus } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const event = (payload: Record<string, unknown>, actor = host): DetailEvent => ({
  seq: 9, type: 'team.run_finished', actor, payload: { status: 'failed', role: 'dev', ...payload },
})
const rejected = { external_failure_code: 'request_rejected', reason: 'PRIVATE_REMOTE_REASON',
  message: 'PRIVATE_REMOTE_MESSAGE', data: { token: 'PRIVATE_CREDENTIAL' }, prompt: 'PRIVATE_PROMPT' }

test.each([
  ['initialize', '初始化连接（initialize）'],
  ['session/new', '创建会话（session/new）'],
  ['session/prompt', '提交会话请求（session/prompt）'],
])('展开既有诊断安全显示请求阶段 %s，不显示远端正文或私密元数据', (method, chinese) => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const receipt = event({ ...rejected, external_rpc_code: -32603, external_rpc_method: method })
  const before = structuredClone(receipt)
  const view = render(<EventDetails event={receipt} />)
  expect(screen.getByText('ACP RPC 错误码').nextElementSibling?.textContent).toBe('-32603')
  expect(screen.getByText('ACP RPC 方法').nextElementSibling?.textContent).toBe(chinese)
  expect(view.container.textContent).toContain('RPC 码不代表 HTTP 状态码或 CLI 结果码')
  expect(view.container.textContent).toContain('远端是否已处理或计费需核对')
  expect(view.container.textContent).not.toMatch(/PRIVATE_|余额不足|额度耗尽|已停止计费/)
  const safe = JSON.parse(screen.getByLabelText('安全事件 JSON').textContent!)
  expect(Object.keys(safe.payload)).toEqual(['requestFailure'])
  expect(JSON.stringify(safe)).not.toContain('PRIVATE_')
  expect(view.container.querySelector('button, a, input, img, script')).toBeNull()
  expect(view.container.querySelectorAll('summary')).toHaveLength(1)
  expect(fetch).not.toHaveBeenCalled()
  expect(receipt).toEqual(before)
})

test.each([-2147483648, -32602, 0, 110, 429, 2147483647])('RPC 数字 %s 不被当成 HTTP 或 CLI 结果码，也不映射余额/重试', code => {
  const fields = requestFailureFields({ external_failure_code: 'request_rejected', external_rpc_code: code })
  expect(fields).toContainEqual(['ACP RPC 错误码', String(code)])
  expect(fields.some(([label]) => label === 'HTTP 状态')).toBe(false)
  expect(fields.flat().join(' ')).not.toMatch(/每日|余额不足|额度耗尽|服务商限流/)
})

test.each([undefined, null, true, false, '-32603', 1.5, NaN, Infinity, -Infinity,
  2147483648, -2147483649, [], {}, '<script>PRIVATE_CODE</script>'])('拒绝诊断忽略无效或私密码 %j', code => {
  const view = render(<EventDetails event={event({ ...rejected, external_rpc_code: code })} />)
  expect(screen.queryByText('ACP RPC 错误码')).toBeNull()
  expect(screen.queryByText('ACP RPC 诊断边界')).toBeNull()
  expect(view.container.textContent).not.toMatch(/PRIVATE_|NaN|Infinity|script/)
})

test.each(['constructor', '__proto__', 'toString', 'PRIVATE_METHOD', true, {}, [],
  '<img src=x onerror=PRIVATE_METHOD>'])('方法名只接受主机枚举，未知值 %j 不反射', method => {
  const view = render(<EventDetails event={event({ ...rejected, external_rpc_method: method })} />)
  expect(screen.queryByText('ACP RPC 方法')).toBeNull()
  expect(screen.queryByText('ACP RPC 诊断边界')).toBeNull()
  expect(view.container.textContent).not.toMatch(/PRIVATE_|constructor|__proto__|toString|onerror/)
})

test.each(['deadline_exceeded', 'external_error', 'PRIVATE_CODE', undefined])('非拒绝类别 %j 不借用 RPC 字段背书', code => {
  render(<EventDetails event={event({ ...rejected, external_failure_code: code,
    external_rpc_code: -32603, external_rpc_method: 'session/prompt' })} />)
  expect(screen.queryByText('ACP RPC 错误码')).toBeNull()
  expect(screen.queryByText('ACP RPC 方法')).toBeNull()
})

test.each([
  { id: 'team', role: 'team', kind: 'agent' },
  { id: 'other', role: 'team', kind: 'system' },
  { id: 'team', role: 'other', kind: 'system' },
])('非宿主来源不将 RPC 码呈现为可信诊断：%j', actor => {
  const view = render(<EventDetails event={event({ ...rejected,
    external_rpc_code: -32603, external_rpc_method: 'session/prompt' }, actor)} />)
  expect(screen.queryByText('ACP RPC 错误码')).toBeNull()
  expect(screen.queryByText('ACP RPC 方法')).toBeNull()
  expect(view.container.textContent).not.toMatch(/PRIVATE_|-32603|session\/prompt/)
})

test('继承字段不采纳，旧记录不补诊断，waiting 状态复用当前共享短语', () => {
  const inherited = Object.create({ ...rejected, external_rpc_code: -32603, external_rpc_method: 'session/prompt' })
  expect(requestFailureFields(inherited)).toEqual([])
  const view = render(<EventDetails event={event(inherited)} />)
  expect(view.container.textContent).not.toMatch(/PRIVATE_|-32603|session\/prompt/)
  view.rerender(<EventDetails event={event({ status: 'waiting', reason: '本轮静默等待新信息' })} />)
  expect(screen.getByText(teamRunStatus('waiting'))).toBeTruthy()
  expect(screen.queryByText('等待伙伴回复')).toBeNull()
  expect(screen.queryByText('ACP RPC 错误码')).toBeNull()
  expect(screen.getByText('本轮静默等待新信息')).toBeTruthy()
})
