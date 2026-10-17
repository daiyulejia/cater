import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { ModelUsagePanel } from '../src/ModelUsagePanel'
import { TeamProgressPanel } from '../src/TeamProgressPanel'
import { EventDetails } from '../src/EventDetails'
import type { TeamEvent } from '../src/team'
import type { DetailEvent } from '../src/EventDetails'

const event = (seq: number, payload: Record<string, unknown>): TeamEvent => ({
  seq, type: 'team.model_called', actor: { id: 'team', kind: 'system', role: 'team' }, payload,
})
const names = { dev: '米寇', test: '三角', store_manager: '店长' }
const report = (seq: number, role = 'dev', tokens = 100) => event(seq, { role, tokens, token_usage: 'reported' })

test('主展示直接看到本群已报告合计，不先展开几百条记录，明细默认折叠', () => {
  const events = [report(1, 'dev', 4000000), report(2, 'test', 2500210), report(3, 'store_manager', 3493965)]
  const saved = JSON.stringify(events)
  render(<ModelUsagePanel events={events} names={names} />)
  const panel = screen.getByRole('region', { name: '本群模型用量' })
  expect(within(panel).getByLabelText('已报告总用量').textContent).toBe('9,994,175 token')
  expect(within(panel).getByText('本群累计 · 含历史目标')).toBeTruthy()
  expect(within(panel).getByText(/已记录 3 次返回.*3 次已报告.*截至事件 #3/)).toBeTruthy()
  const fold = within(panel).getByText('成员用量与最近返回').closest('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(within(panel).getByText('成员用量与最近返回'))
  expect(fold.open).toBe(true)
  const rows = within(within(panel).getByRole('list', { name: '按成员汇总的模型用量' })).getAllByRole('listitem')
  expect(rows.map((row) => row.querySelector('strong')?.textContent)).toEqual(['@米寇', '@店长', '@三角'])
  expect(JSON.stringify(events)).toBe(saved)
})

test('旧总量可计入，估算与不可用不当真实零，来源未注明不混入已报告合计', () => {
  render(<ModelUsagePanel events={[
    report(1, 'dev', 150), event(2, { role: 'test', tokens: 90, token_usage: 'estimated' }),
    event(3, { role: 'test', tokens: 0, token_usage: 'unavailable' }),
    event(4, { role: 'store_manager', usage: { total_tokens: 300 } }),
  ]} names={names} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('150 token')
  expect(screen.getByText(/另有估算：90 token.*1 次/)).toBeTruthy()
  expect(screen.getByText(/1 次用量未报告；1 次来源未注明/)).toBeTruthy()
  expect(screen.getByText(/0 \/ 1 次有输入或输出明细/)).toBeTruthy()
  expect(screen.getByText('@三角 · 用量未报告')).toBeTruthy()
  expect(screen.queryByText('@三角 · 0 token')).toBeNull()
  expect(screen.getByText('@店长 · 300 token')).toBeTruthy()
})

test('显式报告零显示零，完全缺计量不显示零，也不添加空用量入口', () => {
  const view = render(<ModelUsagePanel events={[]} names={names} />)
  expect(view.container.childElementCount).toBe(0)
  view.rerender(<ModelUsagePanel events={[event(1, { role: 'dev', tokens: 0, token_usage: 'unavailable' })]} names={names} />)
  expect(screen.getByLabelText('已报告总用量').textContent).not.toContain('0')
  view.rerender(<ModelUsagePanel events={[report(2, 'dev', 0)]} names={names} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('0 token')
})

test('260条旧返回只展开最近8条，汇总仍含所有返回，不重复成聊天或创建网络请求', () => {
  render(<ModelUsagePanel events={Array.from({ length: 260 }, (_, i) => report(i + 1))} names={names} />)
  const list = screen.getByRole('list', { name: '最近模型返回', hidden: true })
  expect(within(list).getAllByRole('listitem', { hidden: true })).toHaveLength(8)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('26,000 token')
  expect(list.textContent).toContain('事件 #260')
  expect(list.textContent).toContain('事件 #253')
  expect(list.textContent).not.toContain('事件 #252')
  expect(screen.queryByRole('button')).toBeNull()
  expect(screen.queryByRole('link')).toBeNull()
})

test('两种输入口径与缓存分别展示，不双加总，字节不当token，私有元数据不展示', () => {
  render(<ModelUsagePanel events={[
    event(1, { role: 'dev', tokens: 130, token_usage: 'reported', usage: {
      prompt_tokens: 100, completion_tokens: 30, total_tokens: 130,
      prompt_tokens_details: { cached_tokens: 80 }, completion_tokens_details: { reasoning_tokens: 20 },
      secret: 'DO_NOT_SHOW',
    }, request_sizes: { prompt_utf8_bytes: 43000, history_json_bytes: 65000, tools_json_bytes: 4000, history_omitted_groups: 5 } }),
    event(2, { role: 'test', tokens: 35, token_usage: 'reported', usage: {
      input_tokens: 20, output_tokens: 5, cache_creation_input_tokens: 3, cache_read_input_tokens: 7,
    } }),
  ]} names={names} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('165 token')
  expect(screen.getByText('输入（含缓存）')).toBeTruthy()
  expect(screen.getByText('输入（不含缓存）')).toBeTruthy()
  expect(screen.getByText('43,000 字节')).toBeTruthy()
  expect(screen.getByText('65,000 字节')).toBeTruthy()
  expect(screen.getByText(/5 组较早的完整交互未重复发送/)).toBeTruthy()
  expect(screen.queryByText('DO_NOT_SHOW')).toBeNull()
})

test('换群重算而不累加旧视图，长名称与HTML为纯文本', () => {
  const view = render(<ModelUsagePanel events={[report(1, 'dev', 12)]} names={names} />)
  const title = '<img src=x onerror=alert(1)>' + '很长的成员名字'.repeat(8)
  view.rerender(<ModelUsagePanel events={[report(2, 'test', 24)]} names={{ test: title }} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('24 token')
  expect(screen.queryByText('@米寇')).toBeNull()
  expect(view.container.querySelector('img, script')).toBeNull()
  expect(view.container.textContent).toContain(title)
})

test('单条审计中的未报告0不再当作真实用量，伪造来源不显示计数', () => {
  const view = render(<EventDetails event={event(1, { role: 'dev', tokens: 0, token_usage: 'unavailable' }) as DetailEvent} />)
  expect(view.container.textContent).toContain('用量未报告')
  expect(view.container.textContent).not.toContain('0 token')
  view.rerender(<EventDetails event={{ ...report(2), actor: { id: 'team', kind: 'agent', role: 'team' } } as DetailEvent} />)
  expect(view.container.textContent).toContain('用量来源待核对')
  expect(view.container.textContent).not.toContain('100 token')
})

test.each(['paused', 'cancelled'] as const)('已有2条待处理时，工作板仍明确展示%s而不是正在执行', (executionState) => {
  render(<TeamProgressPanel team={{ enabled: true, objective: '检查现有成果', status: 'running',
    pending_count: 2, runs: [], assignments: [], checkpoint: '已收到2条待处理消息', revision: 1 }}
    events={[report(1)]} names={names} executionState={executionState} />)
  expect(screen.getByLabelText('团队执行状态').textContent).toBe(executionState === 'paused' ? '已暂停后续执行' : '已取消后续执行')
  expect(screen.getByText('待处理消息：2')).toBeTruthy()
  expect(screen.queryByText('正在处理消息')).toBeNull()
})

test.each([
  { tokens: 130, usage: { total_tokens: 130, prompt_tokens: 100, completion_tokens: 30,
    prompt_tokens_details: { cached_tokens: 80 }, completion_tokens_details: { reasoning_tokens: 20 } } },
  { tokens: 175, usage: { total_tokens: 175, input_tokens: 20, output_tokens: 5,
    cache_read_input_tokens: 100, cache_creation_input_tokens: 50 } },
])('格式错误在原折叠及事件详情标明停止，仍保留计量：$tokens', ({ tokens, usage }) => {
  const receipt = event(2, { role: 'dev', response_status: 'protocol_error', token_usage: 'reported', tokens, usage,
    request_sizes: { prompt_utf8_bytes: 40 }, message: '<img src=x onerror=alert(1)>', body: 'PRIVATE_BODY' })
  const view = render(<ModelUsagePanel events={[report(1, 'dev', 5), receipt, structuredClone(receipt)]} names={names} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe(`${tokens + 5} token`)
  expect(screen.getByText(/已记录 2 次返回.*2 次已报告/)).toBeTruthy()
  const fold = screen.getByText('成员用量与最近返回').closest('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  const recent = screen.getByRole('list', { name: '最近模型返回' })
  expect(within(recent).getAllByRole('listitem')).toHaveLength(2)
  expect(within(recent).getByText(/响应格式错误，工作未继续/)).toBeTruthy()
  expect(within(recent).getByText(`@米寇 · ${tokens} token`)).toBeTruthy()
  expect(view.container.textContent).not.toMatch(/PRIVATE_BODY|onerror|免费|成功/)
  expect(view.container.querySelector('img, script')).toBeNull()
  expect(screen.queryByRole('button')).toBeNull()
  expect(screen.queryByRole('link')).toBeNull()

  view.rerender(<EventDetails event={receipt as DetailEvent} />)
  expect(screen.getByText('响应格式错误')).toBeTruthy()
  expect(screen.getByText(/响应格式无效，工作未继续/)).toBeTruthy()
  const fields = view.container.querySelector('dl')!
  expect(within(fields).getByText('用量已报告')).toBeTruthy()
  expect(within(fields).getByText('已报告用量').nextElementSibling?.textContent).toBe(`${tokens} token`)
  expect(fields.textContent).not.toMatch(/PRIVATE_BODY|onerror|免费|成功/)
  expect(view.container.querySelector('img, script')).toBeNull()
})

test.each([
  {}, { token_usage: 'unavailable', tokens: 0 }, { token_usage: 'reported', tokens: '0' },
])('格式错误缺少可信计量时仍不冒充零：%j', payload => {
  const receipt = event(1, { role: 'dev', response_status: 'protocol_error', ...payload })
  const view = render(<ModelUsagePanel events={[receipt]} names={names} />)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('暂无已报告用量')
  expect(screen.getByText('@米寇 · 用量未报告')).toBeTruthy()
  expect(screen.getByText(/响应格式错误，工作未继续/)).toBeTruthy()
  expect(view.container.textContent).not.toMatch(/0 token|免费|成功/)
  view.rerender(<EventDetails event={receipt as DetailEvent} />)
  expect(screen.getByText('响应格式错误')).toBeTruthy()
  expect(screen.queryByText('已报告用量')).toBeNull()
  expect(view.container.textContent).not.toMatch(/0 token|免费|成功/)
})

test('格式错误状态冲突和已有计量冲突重放后均不恢复可用计数', () => {
  const failed = event(1, { role: 'dev', response_status: 'protocol_error', tokens: 100, token_usage: 'reported' })
  const view = render(<ModelUsagePanel events={[failed, report(1), failed]} names={names} />)
  fireEvent.click(screen.getByText('成员用量与最近返回'))
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('暂无已报告用量')
  expect(screen.getByText(/已记录 1 次返回.*0 次已报告/)).toBeTruthy()
  expect(screen.getByText('@米寇 · 用量未报告')).toBeTruthy()
  const conflicting = { ...failed, payload: { ...failed.payload, tokens: 101 } }
  view.rerender(<ModelUsagePanel events={[failed, conflicting, failed]} names={names} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('暂无已报告用量')
  expect(screen.getByText('@米寇 · 用量未报告')).toBeTruthy()
  expect(screen.getByText(/响应格式错误，工作未继续/)).toBeTruthy()
})

test.each(['<img src=x onerror=alert(1)>', 'success', '__proto__', { status: 'protocol_error' }])('未知状态不反射为用户摘要或改变历史展示：%j', response_status => {
  const original = report(1)
  const receipt = { ...original, payload: { ...original.payload, response_status } }
  const view = render(<ModelUsagePanel events={[original]} names={names} />)
  const historical = view.container.innerHTML
  view.rerender(<ModelUsagePanel events={[receipt]} names={names} />)
  expect(view.container.innerHTML).toBe(historical)
  view.rerender(<EventDetails event={original as DetailEvent} />)
  const header = view.container.querySelector('header')!.innerHTML
  const fields = view.container.querySelector('dl')!.innerHTML
  const note = view.container.querySelector('section > p')!.textContent
  view.rerender(<EventDetails event={receipt as DetailEvent} />)
  expect(view.container.querySelector('header')!.innerHTML).toBe(header)
  expect(view.container.querySelector('dl')!.innerHTML).toBe(fields)
  expect(view.container.querySelector('section > p')!.textContent).toBe(note)
  expect(view.container.querySelector('img, script')).toBeNull()
})

test('伪造团队来源的格式错误不能充当失败或计量依据', () => {
  const receipt = { ...event(1, { response_status: 'protocol_error', tokens: 100, token_usage: 'reported' }),
    actor: { id: 'team', kind: 'agent', role: 'team' } }
  const view = render(<ModelUsagePanel events={[receipt]} names={names} />)
  expect(view.container.childElementCount).toBe(0)
  view.rerender(<EventDetails event={receipt as DetailEvent} />)
  expect(screen.getByText('用量来源待核对')).toBeTruthy()
  expect(screen.queryByText('响应格式错误')).toBeNull()
  expect(screen.queryByText('已报告用量')).toBeNull()
})

test('已不在成员目录的特殊角色ID按文本显示，不读取对象原型作为姓名', () => {
  render(<ModelUsagePanel events={[report(1, '__proto__'), report(2, 'constructor'), report(3, 'toString')]} names={{}} />)
  expect(screen.getByLabelText('已报告总用量').textContent).toBe('300 token')
  for (const id of ['__proto__', 'constructor', 'toString']) expect(screen.getByText(`@${id} · 100 token`)).toBeTruthy()
})

test('只有历史窗口组数时仍展示记录，不凭空补齐缺失字节', () => {
  render(<ModelUsagePanel events={[event(1, { role: 'dev', tokens: 100, token_usage: 'reported',
    request_sizes: { history_omitted_groups: 3 } })]} names={names} />)
  expect(screen.getByText('最近有记录的请求信息 · 事件 #1')).toBeTruthy()
  expect(screen.getByText(/3 组较早的完整交互未重复发送/)).toBeTruthy()
  expect(screen.getAllByText('未记录')).toHaveLength(3)
  expect(screen.queryByText('0 字节')).toBeNull()
})
