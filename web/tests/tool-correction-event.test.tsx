import { fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'
import { usageSummary } from '../src/modelUsage'

const host = { id: 'team', role: 'team', kind: 'system' }
const requestType = 'team.response_correction_requested'
const metadata = { role: 'dev', run_id: 'PRIVATE_RUN', message_seq: 10, revision: 0,
  model_call_seq: 12, issue: 'invalid_call_id', attempt: 1 }
const privateFields = { reason: 'PRIVATE_REASON invalid_arguments_json', raw_reason: 'PRIVATE_RAW_REASON',
  work_item_id: 'PRIVATE_WORK', arguments: 'PRIVATE_ARGUMENTS', content: 'PRIVATE_CONTENT',
  response_protocol: 'PRIVATE_PROTOCOL', meta: { issue: 'PRIVATE_META' },
  toJSON: () => ({ leaked: 'PRIVATE_TO_JSON' }) }
const event = (payload: Record<string, unknown> = metadata, type = requestType, actor = host): DetailEvent =>
  ({ seq: 15, type, actor, payload })
const names = (id: string) => id === 'dev' ? '开发伙伴' : id
const field = (name: string) => screen.getByText(name).nextElementSibling?.textContent
const ordinary = (container: HTMLElement) => {
  const copy = container.cloneNode(true) as HTMLElement
  copy.querySelectorAll('details').forEach(element => element.remove())
  return copy.textContent
}
const issues = [
  ['calls_not_array', '工具请求列表不是数组'], ['invalid_call_shape', '工具请求结构无效'],
  ['unsupported_call_type', '工具请求类型不受支持'], ['invalid_function', '工具函数定义无效'],
  ['invalid_call_id', '工具请求标识无效'], ['duplicate_call_id', '工具请求标识重复'],
  ['invalid_tool_name', '工具名称无效'], ['unregistered_tool', '请求了本轮未提供的工具'],
  ['arguments_not_text', '工具参数未使用 JSON 文本'], ['invalid_arguments_json', '工具参数不是有效且无歧义的 JSON'],
  ['arguments_not_object', '工具参数不是对象'], ['invalid_arguments_value', '工具参数值无效'],
]

test.each(issues)('可信纠正安排 %s 原位显示中文，不伪造协议或宣称成功', (issue, chinese) => {
  const { container } = render(<EventDetails event={event({ ...metadata, ...privateFields, issue })} actorName={names} />)
  expect(screen.getByRole('heading', { name: '工具响应纠正安排' })).toBeTruthy()
  expect(screen.getByText('已安排一次纠正')).toBeTruthy()
  expect(field('处理成员')).toBe('@开发伙伴')
  expect(field('关联消息')).toBe('10')
  expect(field('目标版本')).toBe('0')
  expect(field('被拒绝的模型调用事件')).toBe('12')
  expect(field('纠正次数')).toBe('1')
  expect(field('工具请求诊断')).toBe(chinese)
  expect(container.textContent).toContain('不代表模型请求已发送或已成功返回')
  expect(container.textContent).toContain('不代表工具执行或任务完成')
  expect(container.textContent).toContain('计费仍需核对回执与服务商账单')
  expect(container.innerHTML).not.toMatch(/PRIVATE_|响应协议|兼容协议|Anthropic|纠正成功|工具已完成|任务已完成|免计费/)
  expect(container.querySelector('button, a, input')).toBeNull()
  expect(container.querySelectorAll('section')).toHaveLength(1)
  expect(container.querySelectorAll('details')).toHaveLength(1)
  fireEvent.click(screen.getByText('开发者详情'))
  expect(screen.getByLabelText('安全事件 JSON').textContent).not.toMatch(/PRIVATE_|response_protocol/)
})

const unknownIssues: unknown[] = [undefined, null, true, 12, [], ['invalid_call_id'], {},
  '__proto__', 'constructor', 'toString', 'hasOwnProperty', 'PRIVATE_UNKNOWN',
  '<img src=x onerror=PRIVATE_SCRIPT>', new String('invalid_call_id'), Symbol('PRIVATE_SYMBOL'),
  { toString: () => { throw new Error('PRIVATE_COERCION') } }]

test.each(unknownIssues.map((issue, index) => ({ issue, index })))('未知或恶意 issue $index 不反射、不从 reason 推断', ({ issue }) => {
  const { container } = render(<EventDetails event={event({ ...metadata, ...privateFields, issue })} />)
  expect(screen.getByText('已安排一次纠正')).toBeTruthy()
  expect(screen.queryByText('工具请求诊断')).toBeNull()
  expect(container.innerHTML).not.toMatch(/PRIVATE_|工具参数不是|__proto__|constructor|toString|hasOwnProperty|onerror/)
  expect(container.querySelector('img, script, iframe, a')).toBeNull()
})

test.each(Object.keys(metadata))('继承的 %s 不作为纠正元信息', key => {
  const own: Record<string, unknown> = { ...metadata, ...privateFields }
  const value = own[key]
  delete own[key]
  const payload = Object.assign(Object.create({ [key]: value }), own)
  const { container } = render(<EventDetails event={event(payload)} />)
  if (key === 'issue') expect(screen.getByText('已安排一次纠正')).toBeTruthy()
  else expect(screen.getByText('纠正安排待核对')).toBeTruthy()
  expect(container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断|工具请求标识无效/)
})

const invalidSequences: unknown[] = [undefined, null, 0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1,
  'PRIVATE_SEQ', true, { private: 'PRIVATE_SEQ' }]

test.each(['model_call_seq', 'message_seq'])('%s 必须是合法且在前的安全整数', key => {
  const mounted = render(<EventDetails event={event()} />)
  for (const value of [...invalidSequences, 15, 16]) {
    mounted.rerender(<EventDetails event={event({ ...metadata, ...privateFields, [key]: value })} />)
    expect(screen.getByText('纠正安排待核对')).toBeTruthy()
    expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断|被拒绝的模型调用事件|已安排一次纠正/)
  }
})

test('事件自身 seq 非法时不显示诊断，也不反射非法值', () => {
  const mounted = render(<EventDetails event={event()} />)
  for (const seq of invalidSequences) {
    mounted.rerender(<EventDetails event={{ ...event({ ...metadata, ...privateFields }), seq: seq as number }} />)
    expect(screen.getByText('纠正安排待核对')).toBeTruthy()
    expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断|已安排一次纠正/)
  }
})

test.each([{ attempt: 2 }, { attempt: '1' }, { revision: -1 }, { revision: 0.5 },
  { revision: Number.MAX_SAFE_INTEGER + 1 }, { role: {} }, { role: '' }, { run_id: {} }, { run_id: '' },
  { message_seq: 12 }, { message_seq: 13 }])('非法关联元信息不展示诊断：%j', invalid => {
  const { container } = render(<EventDetails event={event({ ...metadata, ...privateFields, ...invalid })} />)
  expect(screen.getByText('纠正安排待核对')).toBeTruthy()
  expect(container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断|已安排一次纠正/)
})

const fakeHosts = [
  { ...host, kind: 'agent' }, { ...host, kind: 'human' }, { ...host, id: 'PRIVATE_ACTOR' },
  { ...host, role: 'PRIVATE_ROLE' }, Object.create(host) as typeof host,
]

test.each(fakeHosts)('假宿主不能背书纠正安排或关联：%j', actor => {
  const mounted = render(<EventDetails event={event({ ...metadata, ...privateFields }, requestType, actor)} />)
  expect(screen.getByText('纠正安排待核对')).toBeTruthy()
  expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|工具请求诊断|已安排一次纠正/)
  for (const type of ['team.model_called', 'team.run_finished']) {
    mounted.rerender(<EventDetails event={event({ role: 'dev', correction_of: 12, correction_attempt: 1,
      reason: 'PRIVATE_REASON', response_status: 'contained' }, type, actor)} />)
    expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|纠正原模型调用事件|纠正次数/)
  }
})

test.each(['team.model_called', 'team.run_finished'])('%s 只展示纠正引用，保留用量和结果边界', type => {
  const original = event({ role: 'dev', run_id: 'PRIVATE_RUN', tokens: 19, token_usage: 'reported', status: 'failed' }, type)
  const corrected = { ...original, payload: { ...original.payload, ...privateFields, correction_of: 12, correction_attempt: 1 } }
  const before = usageSummary([original])
  const { container } = render(<EventDetails event={corrected} actorName={names} />)
  expect(field('纠正原模型调用事件')).toBe('12')
  expect(field('纠正次数')).toBe('1')
  expect(container.textContent).toContain('纠正关联仅为原模型调用的引用')
  expect(container.textContent).toContain('不代表纠正成功、工具执行或任务完成，也不代表免除计费')
  expect(screen.getByLabelText('安全事件 JSON').textContent).toContain('"correction_of": 12')
  expect(container.innerHTML).not.toMatch(/PRIVATE_/)
  expect(usageSummary([corrected])).toEqual(before)
  if (type === 'team.model_called') {
    expect(field('已报告用量')).toBe('19 token')
    expect(screen.getByText('用量已报告')).toBeTruthy()
  } else expect(screen.getByText('消息处理失败')).toBeTruthy()
})

test.each(['team.model_called', 'team.run_finished'])('%s 非法或继承的纠正引用不展示、不回填', type => {
  const base = { role: 'dev', status: 'failed', tokens: 19, token_usage: 'reported' }
  const mounted = render(<EventDetails event={event(base, type)} contextEvents={[event()]} />)
  const oldContent = ordinary(mounted.container)
  const malformed = [
    ...invalidSequences.map(correction_of => ({ correction_of, correction_attempt: 1 })),
    { correction_of: 15, correction_attempt: 1 }, { correction_of: 16, correction_attempt: 1 },
    { correction_of: 12 }, { correction_attempt: 1 }, { correction_of: 12, correction_attempt: 2 },
    { correction_of: 12, correction_attempt: '1' },
  ]
  for (const correction of malformed) {
    mounted.rerender(<EventDetails event={event({ ...base, ...correction }, type)} contextEvents={[event()]} />)
    expect(ordinary(mounted.container)).toBe(oldContent)
    expect(mounted.container.innerHTML).not.toMatch(/PRIVATE_|correction_of|correction_attempt/)
  }
  for (const key of ['correction_of', 'correction_attempt']) {
    const correction: Record<string, unknown> = { correction_of: 12, correction_attempt: 1 }
    const inherited = correction[key]
    delete correction[key]
    mounted.rerender(<EventDetails event={event(Object.assign(Object.create({ [key]: inherited }), base, correction), type)} />)
    expect(ordinary(mounted.container)).toBe(oldContent)
    expect(mounted.container.innerHTML).not.toMatch(/correction_of|correction_attempt/)
  }
})

test('旧事件不从原始原因、嵌套字段或上下文推断纠正，展示与展开均无 fetch', () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const mounted = render(<EventDetails event={event({ ...metadata, ...privateFields })} />)
  fireEvent.click(screen.getByText('开发者详情'))
  for (const type of ['team.model_called', 'team.run_finished']) {
    mounted.rerender(<EventDetails event={event({ role: 'dev', reason: 'invalid_call_id', status: 'failed',
      meta: { correction_of: 12, correction_attempt: 1 } }, type)} contextEvents={[event()]} />)
    expect(mounted.container.textContent).not.toMatch(/纠正原模型调用事件|纠正次数|纠正关联/)
    fireEvent.click(screen.getByText('开发者详情'))
  }
  expect(fetch).not.toHaveBeenCalled()
})
