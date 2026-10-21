import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ConversationActivity } from '../src/ConversationActivity'
import { conversationActivity } from '../src/conversationActivityState'
import { EventDetails } from '../src/EventDetails'
import { ModelUsagePanel } from '../src/ModelUsagePanel'
import { usageSummary } from '../src/modelUsage'
import { commonResponseDiagnostics, responseDiagnosticFields, responseDiagnostics } from '../src/responseDiagnostics'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const event = (payload: Record<string, unknown>, type = 'team.model_called', seq = 3, actor = host): TeamEvent =>
  ({ seq, type, actor, payload })
const contained = {
  role: 'dev', response_status: 'contained', response_reason: 'invalid_tool_calls',
  response_protocol: 'openai_compatible', response_client_tools_executed: false,
}
const issues = [
  ['calls_not_array', '工具请求列表不是数组'],
  ['invalid_call_shape', '工具请求结构无效'],
  ['unsupported_call_type', '工具请求类型不受支持'],
  ['invalid_function', '工具函数定义无效'],
  ['invalid_call_id', '工具请求标识无效'],
  ['duplicate_call_id', '工具请求标识重复'],
  ['invalid_tool_name', '工具名称无效'],
  ['unregistered_tool', '请求了本轮未提供的工具'],
  ['arguments_not_text', '工具参数未使用 JSON 文本'],
  ['invalid_arguments_json', '工具参数不是有效且无歧义的 JSON'],
  ['arguments_not_object', '工具参数不是对象'],
  ['invalid_arguments_value', '工具参数值无效'],
] as const
const pairs = [
  ['openai_compatible', 'invalid_tool_calls', '响应中的工具请求格式无效'],
  ['anthropic', 'invalid_content', '响应内容格式无效'],
] as const
const team: TeamState = { enabled: true, objective: '', status: 'running', pending_count: 0,
  revision: 0, runs: [], assignments: [], checkpoint: '' }
const human = event({ recipient: 'dev', kind: 'message', text: '请处理' }, 'team.message_received', 1,
  { id: 'human', kind: 'human', role: 'human' })
const start = event({ role: 'dev', message_seq: 1, revision: 0, run_id: 'run' }, 'team.run_started', 2)
const finish = (payload: Record<string, unknown>, actor = host) => event({ ...payload,
  role: 'dev', message_seq: 1, run_id: 'run', status: 'failed' }, 'team.run_finished', 3, actor)
const input = (ends: TeamEvent[]) => ({ team, events: [human, start, ...ends], names: { dev: '开发' },
  leaderId: 'manager', sending: false, executionState: 'running' })
const progress = (ends: TeamEvent[]) => {
  const view = conversationActivity(input(ends))
  return [view.message, ...view.lines.map(line => line.text)].join('\n')
}
const privateFields = { reason: 'PRIVATE_RAW_REASON invalid_arguments_json',
  arguments: 'PRIVATE_ARGUMENTS', content: 'PRIVATE_CONTENT', meta: { response_tool_issue: 'PRIVATE_META' } }

test.each(pairs.flatMap(([protocol, reason, main]) => issues.map(([issue, chinese]) =>
  ({ protocol, reason, main, issue, chinese }))))('安全细分 $protocol / $issue 原位显示中文且计量不变',
  ({ protocol, reason, main, issue, chinese }) => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const payload = { ...contained, ...privateFields, response_protocol: protocol, response_reason: reason,
      response_tool_issue: issue, token_usage: 'reported', tokens: 19 }
    const receipt = event(payload)
    const summary = usageSummary([receipt, structuredClone(receipt)])
    expect(summary.calls).toHaveLength(1)
    expect(summary.calls[0]).toMatchObject({ responseToolIssue: issue, responseReason: reason,
      source: 'reported', tokens: 19 })
    expect(summary.reportedTokens).toBe(19)
    expect(summary.calls[0].responseDiagnosticsConflict).toBeUndefined()
    const mounted = render(<ModelUsagePanel events={[receipt, structuredClone(receipt)]} names={{ dev: '开发' }} />)
    fireEvent.click(screen.getByText('成员用量与最近返回'))
    const recent = screen.getByRole('list', { name: '最近模型返回' })
    expect(within(recent).getAllByRole('listitem')).toHaveLength(1)
    expect(recent.textContent).toContain(`工具请求诊断：${chinese}`)
    expect(recent.textContent).toContain(main)
    expect(screen.getByLabelText('已报告总用量').textContent).toBe('19 token')
    expect(mounted.container.querySelectorAll('summary')).toHaveLength(1)
    expect(mounted.container.querySelector('button, a, input')).toBeNull()
    expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_/)
    for (const type of ['team.model_called', 'team.run_finished']) {
      mounted.rerender(<EventDetails event={event({ ...payload, status: 'failed' }, type)} />)
      expect(screen.getByText('工具请求诊断').nextElementSibling?.textContent).toBe(chinese)
      expect(mounted.container.querySelector('dl')?.textContent).toContain(main)
      expect(mounted.container.querySelectorAll('summary')).toHaveLength(1)
      expect(mounted.container.querySelector('button, a, input')).toBeNull()
      expect(screen.getByLabelText('安全事件 JSON').textContent).not.toMatch(/PRIVATE_/)
      expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_/)
    }
    const end = finish(payload)
    const view = conversationActivity(input([end]))
    expect(view.message).toContain(`本轮处理未完成：${chinese}`)
    expect(view.lines.find(line => line.role === 'dev')).toMatchObject({ moving: false, warning: true })
    expect(progress([end])).not.toMatch(/PRIVATE_|已停止计费|远端未执行|允许重试/)
    expect(fetch).not.toHaveBeenCalled()
  })

const malicious = [undefined, null, true, false, 0, NaN, [], ['invalid_call_id'], {},
  { response_tool_issue: 'invalid_call_id' }, '__proto__', 'constructor', 'toString', 'hasOwnProperty',
  'PRIVATE_UNKNOWN', '<img src=x onerror=PRIVATE_SCRIPT>', new String('invalid_call_id'),
  Symbol('PRIVATE_SYMBOL'), () => 'invalid_call_id',
  { toString: () => { throw new Error('PRIVATE_COERCION') } },
]
test.each(malicious.map((value, index) => ({ value, index })))('恶意或未知细分类型 $index 不反射也不强制转字符串', ({ value }) => {
  const payload = { ...contained, ...privateFields, response_tool_issue: value }
  expect(responseDiagnostics(payload).responseToolIssue).toBeUndefined()
  expect(responseDiagnosticFields(responseDiagnostics(payload)).some(([name]) => name === '工具请求诊断')).toBe(false)
  const mounted = render(<ModelUsagePanel events={[event(payload)]} names={{}} />)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|onerror|__proto__|constructor|toString|hasOwnProperty|工具请求诊断/)
  for (const type of ['team.model_called', 'team.run_finished']) {
    mounted.rerender(<EventDetails event={event(payload, type)} />)
    expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|onerror|__proto__|constructor|toString|hasOwnProperty|工具请求诊断/)
  }
  expect(progress([finish(payload)])).toBe(progress([finish(contained)]))
})

test.each([
  { response_status: undefined }, { response_status: 'protocol_error' }, { response_status: true },
  { response_reason: undefined }, { response_reason: 'invalid_content' }, { response_reason: 'incomplete_stop' },
  { response_reason: 'invalid_tool_calls invalid_arguments_json' },
  { response_protocol: undefined }, { response_protocol: 'PRIVATE_PROTOCOL' }, { response_protocol: '__proto__' },
  { response_protocol: 'anthropic' },
  { response_protocol: 'anthropic', response_reason: 'unsupported_content' },
  { response_protocol: 'anthropic', response_reason: 'invalid_tool_calls' },
])('细分必须是 contained 且协议与主原因配对：%j', mismatch => {
  const payload = { ...contained, ...privateFields, response_tool_issue: 'invalid_arguments_json', ...mismatch }
  expect(responseDiagnostics(payload).responseToolIssue).toBeUndefined()
  expect(responseDiagnosticFields(responseDiagnostics(payload)).some(([name]) => name === '工具请求诊断')).toBe(false)
  const mounted = render(<ModelUsagePanel events={[event(payload)]} names={{}} />)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断/)
  expect(progress([finish(payload)])).toBe(progress([finish(contained)]))
})

test.each(['response_tool_issue', 'response_status', 'response_reason', 'response_protocol'])('继承的 %s 不参与细分诊断', key => {
  const own: Record<string, unknown> = { ...contained, ...privateFields, response_tool_issue: 'invalid_call_id' }
  const inherited = own[key]
  delete own[key]
  const payload = Object.assign(Object.create({ [key]: inherited }), own)
  expect(responseDiagnostics(payload).responseToolIssue).toBeUndefined()
  const mounted = render(<EventDetails event={event(payload)} />)
  expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断/)
  mounted.rerender(<ModelUsagePanel events={[event(payload)]} names={{}} />)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断/)
  const end = finish({})
  end.payload = Object.assign(Object.create({ [key]: inherited }), { ...end.payload, ...own })
  expect(progress([end])).not.toContain('工具请求标识无效')
})

test.each(['duplicate_call_id', undefined, 'PRIVATE_UNKNOWN'])('相同事件细分冲突 %s 走既有冲突路径且重放不恢复', other => {
  const first = event({ ...contained, response_tool_issue: 'invalid_call_id', tokens: 10, token_usage: 'reported' })
  const second = event({ ...first.payload, response_tool_issue: other })
  const summary = usageSummary([first, second, first, second])
  expect(summary).toEqual(usageSummary([second, first, second, first]))
  expect(summary.calls).toHaveLength(1)
  expect(summary.calls[0]).toMatchObject({ responseDiagnosticsConflict: true, source: 'reported', tokens: 10 })
  expect(summary.calls[0].responseToolIssue).toBeUndefined()
  expect(summary.reportedTokens).toBe(10)
  const mounted = render(<ModelUsagePanel events={[first, second, first]} names={{}} />)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  expect(screen.getByText(/同一事件的响应诊断不一致/)).toBeTruthy()
  expect(mounted.container.textContent).not.toMatch(/工具请求诊断|工具请求标识无效|工具请求标识重复|PRIVATE_/)
  const ends = [finish(first.payload), finish(second.payload)]
  expect(progress([...ends, ...ends])).toBe(progress([...ends].reverse()))
  expect(progress(ends)).toContain('同一事件的响应诊断不一致，差异字段未采用')
  expect(progress(ends)).not.toMatch(/工具请求标识无效|工具请求标识重复|PRIVATE_/)
})

test('协议或主原因冲突后即便细分相同也不得显示，冲突合并不选择某一协议', () => {
  const a = responseDiagnostics({ ...contained, response_tool_issue: 'invalid_call_id' })
  const b = responseDiagnostics({ ...contained, response_protocol: 'anthropic', response_reason: 'invalid_content',
    response_tool_issue: 'invalid_call_id' })
  const common = commonResponseDiagnostics(a, b)
  expect(common.responseDiagnosticsConflict).toBe(true)
  expect(common).toEqual(commonResponseDiagnostics(b, a))
  expect(responseDiagnosticFields(common).some(([name]) => name === '工具请求诊断')).toBe(false)
})

test.each(['reported', 'estimated', 'unavailable', undefined])('安全细分不改变单次记录计量来源 %s', token_usage => {
  const original = event({ ...contained, token_usage, tokens: 19 })
  const detailed = event({ ...original.payload, response_tool_issue: 'invalid_call_id' })
  const before = usageSummary([original])
  const after = usageSummary([detailed, structuredClone(detailed)])
  const { responseToolIssue, ...call } = after.calls[0]
  expect(responseToolIssue).toBe('invalid_call_id')
  expect({ ...after, calls: [call] }).toEqual(before)
})

test.each(['reported', 'estimated', 'unavailable', undefined])('仅新增细分冲突或旧记录缺字段时保留一致的全部计量：%s', token_usage => {
  const original = event({ ...contained, driver: 'fixture', token_usage, tokens: 19,
    usage: { total_tokens: 19, prompt_tokens: 12, completion_tokens: 7,
      prompt_tokens_details: { cached_tokens: 4 }, completion_tokens_details: { reasoning_tokens: 2 } },
    request_sizes: { prompt_utf8_bytes: 100, history_json_bytes: 200, tools_json_bytes: 300, history_omitted_groups: 1 } })
  const first = event({ ...original.payload, response_tool_issue: 'invalid_call_id' })
  const other = event({ ...original.payload, response_tool_issue: 'duplicate_call_id' })
  const expected = usageSummary([original])
  for (const second of [original, other]) {
    for (const rows of [[first, second, first, second], [second, first, second, first]]) {
      const actual = usageSummary(rows)
      const { responseToolIssue, responseDiagnosticsConflict, ...call } = actual.calls[0]
      expect(responseToolIssue).toBeUndefined()
      expect(responseDiagnosticsConflict).toBe(true)
      expect({ ...actual, calls: [call] }).toEqual(expected)
    }
  }
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('假宿主 %j 不能给详情、模型返回或进展背书', actor => {
  const payload = { ...contained, ...privateFields, response_tool_issue: 'invalid_call_id' }
  const mounted = render(<ModelUsagePanel events={[event(payload, 'team.model_called', 3, actor)]} names={{}} />)
  expect(usageSummary([event(payload, 'team.model_called', 3, actor)]).calls).toEqual([])
  expect(mounted.container.textContent).not.toContain('工具请求诊断')
  for (const type of ['team.model_called', 'team.run_finished']) {
    mounted.rerender(<EventDetails event={event(payload, type, 3, actor)} />)
    expect(mounted.container.innerHTML).not.toMatch(/工具请求诊断|工具请求标识无效|PRIVATE_/)
  }
  expect(conversationActivity(input([finish(payload, actor)]))).toEqual(conversationActivity(input([])))
  const unmatched = { ...finish(payload), payload: { ...finish(payload).payload, run_id: 'other' } }
  expect(conversationActivity(input([unmatched]))).toEqual(conversationActivity(input([])))
})

test('旧事件缺少细分字段不补造，也不从原 reason、嵌套字段或模型返回猜进展', () => {
  const payload = { ...contained, ...privateFields, meta: { response_tool_issue: 'invalid_arguments_json' } }
  expect(responseDiagnostics(payload).responseToolIssue).toBeUndefined()
  const mounted = render(<EventDetails event={event(payload, 'team.run_finished', 3310)} />)
  expect(mounted.container.innerHTML).not.toMatch(/工具请求诊断|PRIVATE_|invalid_arguments_json/)
  mounted.rerender(<ModelUsagePanel events={[event(payload)]} names={{}} />)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  expect(mounted.container.innerHTML).not.toMatch(/工具请求诊断|PRIVATE_|invalid_arguments_json/)
  expect(progress([finish(payload)])).toBe(progress([finish({})]))
  const model = event({ ...contained, response_tool_issue: 'invalid_call_id' }, 'team.model_called', 4)
  expect(conversationActivity(input([finish({}), model]))).toEqual(conversationActivity(input([finish({})])))
})

test('进展细分保留既有请求诊断、控件和区域，不触发 fetch 或额外操作', () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const callbacks = { onInspect: vi.fn(), onRefresh: vi.fn(), onPause: vi.fn(), portrait: () => <span /> }
  const mounted = render(<ConversationActivity {...input([finish({})])} {...callbacks} />)
  const region = screen.getByRole('region', { name: '当前工作进展' })
  const buttons = within(region).getAllByRole('button').map(button => button.getAttribute('aria-label'))
  const payload = { ...contained, ...privateFields, response_tool_issue: 'invalid_arguments_json',
    failure_kind: 'network_error', transport_issue: 'dns_lookup_failed' }
  mounted.rerender(<ConversationActivity {...input([finish(payload)])} {...callbacks} />)
  expect(screen.getAllByRole('region', { name: '当前工作进展' })).toEqual([region])
  expect(within(region).getAllByRole('button').map(button => button.getAttribute('aria-label'))).toEqual(buttons)
  for (const element of [region.querySelector('.activity-message'), region.querySelector('.activity-line')]) {
    expect(element?.textContent).toContain('模型连接或传输失败 · 工具参数不是有效且无歧义的 JSON · 域名解析失败')
  }
  expect(region.innerHTML).not.toMatch(/PRIVATE_|invalid_arguments_json/)
  expect(fetch).not.toHaveBeenCalled()
  expect(callbacks.onInspect).not.toHaveBeenCalled()
  expect(callbacks.onRefresh).not.toHaveBeenCalled()
  expect(callbacks.onPause).not.toHaveBeenCalled()
})
