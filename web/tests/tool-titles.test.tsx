import { cleanup, render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { toolTitle, conversationActivity } from '../src/conversationActivityState'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { EventDetails } from '../src/EventDetails'
import type { TeamEvent, TeamState } from '../src/team'

const actor = { id: 'team', kind: 'system', role: 'team' }
const now = Date.parse('2026-10-24T01:00:00Z')
const event = (seq: number, type: string, payload: Record<string, unknown>): TeamEvent =>
  ({ seq, type, actor, payload, ts: new Date(now).toISOString() })
const started = (name: unknown) => event(3, 'team.tool_started', {
  name, run_id: 'run', message_seq: 1, role: 'manager', arguments: {},
})

test.each([
  ['read_handoff_sources', '读取交付依据'], ['read_receipts', '核对执行回执'],
  ['request_review', '请求复核'], ['read_checkpoint', '读取工作检查点'],
  ['list_mcp_tools', '查看外部工具目录'], ['call_mcp_tool', '调用外部工具'],
])('进度与事件详情共用中文工具名：%s', (name, title) => {
  expect(toolTitle(name)).toBe(title)
  const events = [started(name)]
  const progress = render(<TeamToolProgress events={events} names={{ manager: '店长' }} />)
  const text = screen.getByLabelText('团队工具过程').textContent
  expect(text).toContain(title)
  expect(text).toContain('等待回执')
  expect(text).not.toContain(name)
  expect(text).not.toMatch(/已完成|已交付|检查通过/)
  progress.unmount()
  const detail = render(<EventDetails event={events[0]} />)
  const ordinary = detail.container.cloneNode(true) as HTMLElement
  ordinary.querySelectorAll('details').forEach(item => item.remove())
  expect(ordinary.textContent).toContain(title)
  expect(ordinary.textContent).not.toContain(name)
  expect(ordinary.textContent).not.toMatch(/已完成|已交付|检查通过/)
})

test.each([undefined, null, '', '   ', 12, {}])('没有有效名称时不猜测工具身份：%j', name => {
  expect(toolTitle(name)).toBe('工具调用')
})

test.each(['new_custom_tool', 'constructor', '__proto__'])('详情保留未知工具标识：%s', name => {
  expect(toolTitle(name)).toBe(name)
  const view = render(<TeamToolProgress events={[started(name)]} names={{}} />)
  expect(view.container.textContent).toContain(name)
  expect(view.container.textContent).not.toMatch(/已完成|已交付|检查通过/)
})

test('主状态使用新工具中文名，但仍不展开未知工具标识或猜成功', () => {
  const team: TeamState = { enabled: true, objective: '本次任务', status: 'running',
    revision: 0, pending_count: 0, runs: [], assignments: [] }
  const events = [event(1, 'team.message_received', { recipient: 'manager', kind: 'message', text: '请继续' }),
    event(2, 'team.run_started', { role: 'manager', message_seq: 1, revision: 0, run_id: 'run' })]
  const project = (name: string) => conversationActivity({ team, events: [...events, started(name)],
    leaderId: 'manager', sending: false, executionState: 'running', names: { manager: '店长' } }, now)
  expect(project('read_handoff_sources').lines.map(line => line.text).join('\n')).toContain('读取交付依据')
  expect(project('new_custom_tool').lines.map(line => line.text).join('\n')).not.toContain('new_custom_tool')
  cleanup()
})
