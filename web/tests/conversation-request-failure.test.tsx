import { render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ConversationActivity } from '../src/ConversationActivity'
import type { ConversationActivityProps } from '../src/ConversationActivity'
import { conversationActivity } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

const now = Date.parse('2026-10-16T08:00:00Z')
const host = { id: 'team', kind: 'system', role: 'team' }
const team: TeamState = { enabled: true, objective: '目标正文不重复', status: 'running', pending_count: 0,
  revision: 0, runs: [], assignments: [], checkpoint: '交付正文不重复' }
const event = (seq: number, type: string, payload: Record<string, unknown> = {}, actor = host): TeamEvent & { ts: string } =>
  ({ seq, type, payload, actor, ts: new Date(now).toISOString() })
const human = (seq = 1, recipient = 'dev') => event(seq, 'team.message_received',
  { recipient, kind: 'message', text: '@开发 请回答' }, { id: 'human', kind: 'human', role: 'human' })
const start = (seq = 2, message = 1, role = 'dev', run = 'run', revision = 0) =>
  event(seq, 'team.run_started', { role, message_seq: message, revision, run_id: run })
const finish = (seq = 3, diagnostic: Record<string, unknown> = {}, status = 'failed', message = 1, role = 'dev', run = 'run') =>
  event(seq, 'team.run_finished', { role, message_seq: message, run_id: run, status, ...diagnostic })
const props = (events: TeamEvent[], extra: Partial<ConversationActivityProps> = {}): ConversationActivityProps => ({
  team, events, names: { dev: '开发', test: '测试', manager: '店长' }, leaderId: 'manager', sending: false,
  executionState: 'running', portrait: id => <span data-testid={`portrait-${id}`}>{id}</span>,
  onInspect: vi.fn(), onRefresh: vi.fn(), onPause: vi.fn(), ...extra,
})
const viewFor = (events: TeamEvent[], extra: Partial<ConversationActivityProps> = {}) => conversationActivity(props(events, extra), now)
const progressText = (view: ReturnType<typeof conversationActivity>) =>
  [view.message, ...view.lines.map(line => line.text), view.notice].filter(Boolean).join('\n')
const rejectedText = '客户端或服务端拒绝了请求，具体原因尚未确认'
const privateFields = { reason: 'PRIVATE_RAW_REASON', rawreason: 'PRIVATE_RAWREASON', privatebody: 'PRIVATE_BODY',
  message: 'PRIVATE_REMOTE_MESSAGE', data: { token: 'PRIVATE_CREDENTIAL' }, prompt: 'PRIVATE_PROMPT' }
const rejected = { ...privateFields, external_failure_code: 'request_rejected' }
const unsupportedClaims = /HTTP\s*\d|余额|额度|限流|允许重试|可以重试|已停止计费|远端未执行|进程已停止|进程已退出|已确认停止|清理完成/

test.each(['failed', 'interrupted'])('真实宿主 %s 回执在消息和角色行保留通用文本并直接显示安全拒绝类别', status => {
  const events = [human(), start(),
    event(3, 'team.external_run_started', { role: 'dev', run_id: 'run', message_seq: 1, run_started_seq: 2 }),
    event(4, 'team.external_run_finished', { role: 'dev', run_id: 'run', message_seq: 1, started_seq: 3,
      phase: 'prompting', prompt_attempted: true, result: 'failed', outcome: 'unknown', cleanup_complete: status === 'failed' }),
    finish(5, { ...rejected, error_type: 'AcpRequestRejected', failure_stage: 'prompting',
      external_observations: { failure_ms: 30, tool_calls: 1 } }, status)]
  const before = structuredClone(events)
  const view = viewFor(events)
  expect(view.visible).toBe(true)
  expect(view.message).toContain('最新消息答复待确认 · 开发本轮处理未完成')
  expect(view.message).toContain(rejectedText)
  expect(view.messageRole).toBe('dev')
  const line = view.lines.find(line => line.role === 'dev')
  expect(line?.text).toContain('开发 · 本轮处理未完成，需核对过程')
  expect(line?.text).toContain(rejectedText)
  expect(line?.warning).toBe(true)
  expect(line?.moving).toBe(false)
  expect(progressText(view)).not.toMatch(unsupportedClaims)
  expect(progressText(view)).not.toMatch(/PRIVATE_|AcpRequestRejected|prompting|tool_calls|目标正文|交付正文/)
  expect(events).toEqual(before)
})

test.each([
  [-32603, 'initialize'], [-32602, 'session/new'], [429, 'session/prompt'], [401, 'session/prompt'],
  [0, 'session/new'], [110, 'initialize'], [-2147483648, 'initialize'], [2147483647, 'session/prompt'],
])('安全 RPC %s / %s 在进展只显示拒绝类别，不推断 HTTP、额度或重试', (code, method) => {
  const view = viewFor([human(), start(), finish(3, { ...rejected, external_rpc_code: code, external_rpc_method: method })])
  expect(view.message).toContain(rejectedText)
  expect(view.lines.find(line => line.role === 'dev')?.text).toContain(rejectedText)
  expect(progressText(view)).not.toMatch(unsupportedClaims)
  expect(progressText(view)).not.toMatch(/PRIVATE_|ACP RPC|initialize|session\/|错误码/)
})

test.each([
  { diagnostic: { failure_kind: 'http_error', status_code: 401 }, category: '模型服务返回 HTTP 错误', detail: 'HTTP 401' },
  { diagnostic: { failure_kind: 'missing_credentials' }, category: '本机未读到该通道的凭据', detail: undefined },
  { diagnostic: { failure_kind: 'network_error', transport_phase: 'request', transport_issue: 'dns_lookup_failed' },
    category: '模型连接或传输失败', detail: '域名解析失败' },
])('宿主请求失败在消息和角色行显示可读类别 $category', ({ diagnostic, category, detail }) => {
  const view = viewFor([human(), start(), finish(3, { ...privateFields, ...diagnostic })])
  for (const text of [view.message, view.lines.find(line => line.role === 'dev')?.text]) {
    expect(text).toContain('本轮处理未完成')
    expect(text).toContain(category)
    if (detail) expect(text).toContain(detail)
  }
  expect(progressText(view)).not.toMatch(/PRIVATE_|余额|额度|限流|允许重试|已停止计费|远端未执行/)
})

test.each([
  {},
  { external_failure_code: 'PRIVATE_UNKNOWN_CODE', failure_kind: 'PRIVATE_UNKNOWN_KIND' },
  { external_failure_code: 'constructor', failure_kind: '__proto__' },
  { external_failure_code: 'toString', external_rpc_code: 429, external_rpc_method: 'session/prompt' },
  { external_failure_code: { message: 'PRIVATE_CODE' }, failure_kind: ['http_error'] },
])('旧记录或未知类别 %j 维持通用摘要，不从原始 reason 或 RPC 猜诊断', diagnostic => {
  const events = [human(), start(), finish(3, { ...privateFields, ...diagnostic })]
  const view = viewFor(events)
  expect(view.message).toBe('最新消息答复待确认 · 开发本轮处理未完成')
  expect(view.lines.find(line => line.role === 'dev')?.text).toBe('开发 · 本轮处理未完成，需核对过程')
  expect(progressText(view)).not.toMatch(/PRIVATE_|constructor|__proto__|toString|HTTP|拒绝了请求|ACP RPC/)
})

const forgedActors = [
  { id: 'team', kind: 'agent', role: 'team' },
  { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' },
  { id: 'team', kind: 'system', role: 'other' },
]
test.each(forgedActors)('伪造结束事件来源 %j 不改变真实在途摘要', actor => {
  const events = [human(), start()]
  const fake = { ...finish(3, rejected), actor }
  expect(viewFor([...events, fake])).toEqual(viewFor(events))
  expect(progressText(viewFor([...events, fake]))).not.toMatch(/PRIVATE_|拒绝了请求/)
})

test.each(forgedActors)('伪造开始事件来源 %j 不能配合宿主结束事件背书诊断', actor => {
  const fake = { ...start(), actor }
  const view = viewFor([human(), fake, finish(3, rejected)])
  expect(view.message).toBe('最新消息已收到，等待开发答复')
  expect(view.lines.some(line => line.role === 'dev')).toBe(false)
  expect(progressText(view)).not.toMatch(/PRIVATE_|拒绝了请求/)
})

test.each([{ run_id: 'other-run' }, { role: 'test' }, { message_seq: 99 }])(
  '结束回执绑定不匹配 %j 不污染当前 run', mismatch => {
    const events = [human(), start()]
    expect(viewFor([...events, finish(3, { ...rejected, ...mismatch })])).toEqual(viewFor(events))
  },
)

test('只有失败回执没有宿主开始事件，不补造当前 run 或诊断', () => {
  expect(viewFor([human(), finish(3, rejected)])).toEqual(viewFor([human()]))
})

test('旧目标失败及迟到的旧回执不污染新目标的摘要和角色行', () => {
  const old = [human(), start(), finish(3, rejected), event(10, 'team.objective_updated')]
  const current = [human(11), start(12, 11, 'dev', 'current-run', 10)]
  const extra = { team: { ...team, revision: 10 } }
  const view = viewFor([...old, ...current, finish(13, rejected)], extra)
  expect(view).toEqual(viewFor([old[3], ...current], extra))
  expect(view.message).toContain('开发正在处理最新消息')
  expect(progressText(view)).not.toMatch(/PRIVATE_|拒绝了请求|本轮处理未完成/)
})

test('同角色后续成功 run 覆盖旧失败，迟到的旧 run 拒绝不能使成功摘要退回失败', () => {
  const events = [human(), start(), finish(3, rejected), start(4, 1, 'dev', 'retry-run'),
    finish(5, {}, 'completed', 1, 'dev', 'retry-run'),
    event(6, 'msg.said', { text: '答复正文不重复', phase: 'team', response_kind: 'reply', reply_to: 1,
      run_id: 'retry-run', audience: 'customer', recipient: 'human' }, { id: 'dev', kind: 'agent', role: 'dev' }),
    finish(7, rejected)]
  const view = viewFor(events)
  expect(view.message).toBe('开发已回应最新消息')
  expect(view.lines.find(line => line.role === 'dev')?.text).toBe('开发 · 本轮消息处理已结束')
  expect(progressText(view)).not.toMatch(/PRIVATE_|拒绝了请求|本轮处理未完成|答复正文/)
})

test('后续新顾客消息成功后，旧消息的同角色失败诊断不进入当前摘要', () => {
  const events = [human(), start(), finish(3, rejected), human(4), start(5, 4, 'dev', 'new-run'),
    finish(6, {}, 'completed', 4, 'dev', 'new-run')]
  const view = viewFor(events)
  expect(view.message).toBe('最新消息本轮处理已结束，尚未确认答复')
  expect(view.lines.find(line => line.role === 'dev')?.text).toBe('开发 · 本轮消息处理已结束')
  expect(progressText(view)).not.toMatch(/PRIVATE_|拒绝了请求|本轮处理未完成/)
})

test.each(['completed', 'waiting'])('非失败状态 %s 不因附带失败字段显示拒绝诊断', status => {
  const view = viewFor([human(), start(), finish(3, rejected, status)])
  expect(progressText(view)).not.toMatch(/PRIVATE_|拒绝了请求|本轮处理未完成/)
})

test('snapshot 和角色自然语言不能替代匹配的宿主失败回执提供诊断', () => {
  const events = [human(), start(), event(3, 'msg.said', { ...rejected, text: 'PRIVATE_BODY',
    phase: 'team', response_kind: 'progress', reply_to: 1, run_id: 'run' }, { id: 'dev', kind: 'agent', role: 'dev' })]
  const snapshot = { ...team, runs: [{ role: 'dev', message_seq: 1, revision: 0, status: 'failed', ...rejected }] }
  const view = viewFor(events, { team: snapshot })
  expect(view.message).toBe('最新消息答复待确认 · 开发本轮处理未完成')
  expect(view.lines.find(line => line.role === 'dev')?.text).toBe('开发 · 本轮处理未完成，需核对过程')
  expect(progressText(view)).not.toMatch(/PRIVATE_|拒绝了请求/)
})

test('拒绝诊断原位出现在既有进展摘要和角色行，保留既有按钮且不读取远端正文', () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const input = props([human(), start(), finish()])
  const mounted = render(<ConversationActivity {...input} />)
  const region = screen.getByRole('region', { name: '当前工作进展' })
  const buttons = within(region).getAllByRole('button').map(button => button.getAttribute('aria-label'))
  expect(within(region).getByLabelText('当前进展摘要').textContent).toContain('最新消息答复待确认 · 开发本轮处理未完成')
  mounted.rerender(<ConversationActivity {...input} events={[human(), start(), finish(3, {
    ...rejected, external_rpc_code: 429, external_rpc_method: 'session/prompt',
  })]} />)
  expect(screen.getAllByRole('region', { name: '当前工作进展' })).toEqual([region])
  expect(within(region).getByLabelText('当前进展摘要').textContent).toContain(rejectedText)
  expect(region.querySelector('.activity-message')?.textContent).toContain(rejectedText)
  expect(region.querySelector('.activity-line')?.textContent).toContain(`开发 · 本轮处理未完成，需核对过程：${rejectedText}`)
  expect(within(region).getAllByRole('button').map(button => button.getAttribute('aria-label'))).toEqual(buttons)
  expect(region.textContent).not.toMatch(unsupportedClaims)
  expect(region.innerHTML).not.toMatch(/PRIVATE_|session\/prompt|ACP RPC/)
  expect(fetch).not.toHaveBeenCalled()
  expect(input.onInspect).not.toHaveBeenCalled()
  expect(input.onRefresh).not.toHaveBeenCalled()
  expect(input.onPause).not.toHaveBeenCalled()
})
