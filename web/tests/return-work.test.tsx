import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import { conversationActivity, teamToolReceipt } from '../src/conversationActivityState'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { teamMessageEchoes } from '../src/team'
import type { TeamEvent, TeamState } from '../src/team'
import { projectTeamGraph } from '../src/teamGraph'

const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent => ({ seq, type, payload, actor })
const agent = (id: string) => ({ id, kind: 'agent', role: id })
const text = '入口已修复，测试通过，交回核对。'
const team: TeamState = { enabled: true, objective: '修复入口', status: 'running', pending_count: 0,
  revision: 1, runs: [], assignments: [], checkpoint: '' }
const names = { manager: '店长', dev: '开发', test: '测试' }
const fixture = () => {
  const source = event(1, 'team.message_received', { kind: 'assignment', recipient: 'dev', text: '修复入口', revision: 1 }, agent('manager'))
  const trigger = event(2, 'team.message_received', { kind: 'report', recipient: 'dev', text: '测试通过', revision: 1 }, agent('test'))
  const run = event(3, 'team.run_started', { role: 'dev', run_id: 'return-run', message_seq: 2, revision: 1 })
  const said = event(4, 'msg.said', { phase: 'team', run_id: 'return-run', text, audience: 'team', recipient: 'manager', reply_to: 1 }, agent('dev'))
  const start = event(5, 'team.tool_started', { name: 'return_work', role: 'dev', run_id: 'return-run', call_id: 'return-call',
    message_seq: 2, revision: 1, arguments: { text } })
  const report = event(6, 'team.message_received', { kind: 'report', visible: true, text, recipient: 'manager', reply_to: 1, revision: 1 }, agent('dev'))
  const finish = event(7, 'team.tool_finished', { name: 'return_work', role: 'dev', run_id: 'return-run', started_seq: 5,
    result: { status: 'ok', message_seq: 6 } })
  const end = event(9, 'team.run_finished', { role: 'dev', run_id: 'return-run', message_seq: 2, status: 'completed', revision: 1 })
  return { source, trigger, run, said, start, report, finish, end, events: [source, trigger, run, said, start, report, finish, end] }
}
type Fixture = ReturnType<typeof fixture>
const relations = (events: TeamEvent[]) => projectTeamGraph(team, events).edges.map(edge => [edge.source, edge.target, edge.label])

test.each([4, 8])('return_work 仅有 text 参数时合并同轮全文回声，保留宿主报告及两个实际来源：发言 #%s', seq => {
  const { events, said, start } = fixture()
  said.seq = seq
  const before = structuredClone(events)
  expect(start.payload.arguments).toEqual({ text })
  expect(teamMessageEchoes(events)).toEqual(new Set([seq]))
  expect(teamMessageEchoes([...events].reverse())).toEqual(new Set([seq]))
  expect(relations(events)).toEqual([['event-1', 'event-6', '回报'], ['event-2', 'event-6', '协作消息']])
  const graph = projectTeamGraph(team, events)
  expect(graph.nodes.find(node => node.seq === 1)?.status).toBe('已收到回报')
  expect(graph.nodes.find(node => node.seq === 6)).toMatchObject({ kind: 'report', sender: 'dev', recipient: 'manager', text, status: '回报已投递' })
  expect(graph.nodes.some(node => /交付|验收通过/.test(node.status))).toBe(false)
  expect(projectTeamGraph(team, [...events].reverse())).toEqual(graph)
  expect(events).toEqual(before)
})

test('原委托也是当前输入时不重复图边，重复回执也不重复消息或关系', () => {
  const { events, start, run, finish } = fixture()
  start.payload.message_seq = run.payload.message_seq = 1
  events.push({ ...finish, seq: 8 })
  expect(teamMessageEchoes(events)).toEqual(new Set([4]))
  expect(relations(events)).toEqual([['event-1', 'event-6', '回报']])
  expect(projectTeamGraph(team, events).nodes.filter(node => node.seq === 6)).toHaveLength(1)
})

const invalidPairs: [string, (input: Fixture) => void][] = [
  ['missing start', f => f.events.splice(f.events.indexOf(f.start), 1)],
  ['missing finish', f => f.events.splice(f.events.indexOf(f.finish), 1)],
  ['wrong start type', f => { f.start.type = 'workspace.tool_started' }],
  ['wrong finish type', f => { f.finish.type = 'workspace.tool_finished' }],
  ['wrong start tool', f => { f.start.payload.name = 'workspace_read' }],
  ['legacy start tool', f => { f.start.payload.name = 'send_message' }],
  ['wrong finish tool', f => { f.finish.payload.name = 'send_message' }],
  ['missing finish name', f => { delete f.finish.payload.name }],
  ['conflicting start alias', f => { f.start.payload.tool = 'workspace_read' }],
  ['conflicting finish alias', f => { f.finish.payload.tool = 'workspace_read' }],
  ['non-host start kind', f => { f.start.actor = { ...host, kind: 'agent' } }],
  ['non-host start id', f => { f.start.actor = { ...host, id: 'other' } }],
  ['non-host start role', f => { f.start.actor = { ...host, role: 'other' } }],
  ['non-host finish kind', f => { f.finish.actor = { ...host, kind: 'agent' } }],
  ['non-host finish id', f => { f.finish.actor = { ...host, id: 'other' } }],
  ['non-host finish role', f => { f.finish.actor = { ...host, role: 'other' } }],
  ['missing start run', f => { delete f.start.payload.run_id }],
  ['empty run', f => { f.start.payload.run_id = f.finish.payload.run_id = ' ' }],
  ['missing finish run', f => { delete f.finish.payload.run_id }],
  ['other run', f => { f.finish.payload.run_id = 'other-run' }],
  ['wrong start role', f => { f.start.payload.role = 'test' }],
  ['wrong finish role', f => { f.finish.payload.role = 'test' }],
  ['missing start role', f => { delete f.start.payload.role }],
  ['missing finish role', f => { delete f.finish.payload.role }],
  ['wrong call', f => { f.finish.payload.call_id = 'other-call' }],
  ['wrong trigger', f => { f.finish.payload.message_seq = 1 }],
  ['wrong start reference', f => { f.finish.payload.started_seq = 3 }],
  ['string start reference', f => { f.finish.payload.started_seq = '5' }],
  ['finish before start', f => { f.start.seq = 8; f.finish.payload.started_seq = 8 }],
]

test.each(invalidPairs)('不可信 return_work 配对不能消掉发言或增加图边：%s', (_, change) => {
  const input = fixture()
  change(input)
  expect(teamMessageEchoes(input.events)).toEqual(new Set())
  if (input.events.includes(input.start)) expect(teamToolReceipt(input.start, input.events)).toBeUndefined()
  // The persisted report remains evidence of its own reply, independently of the bad receipt.
  expect(relations(input.events)).toEqual([['event-1', 'event-6', '回报']])
})

const invalidReports: [string, (input: Fixture) => void][] = [
  ['failed result with message seq', f => { f.finish.payload.result = { status: 'error', message_seq: 6, code: 'assignment_already_returned' } }],
  ['failed receipt status', f => { f.finish.payload.status = 'error' }],
  ['incomplete result', f => { f.finish.payload.result = { status: 'ok', message_seq: 6, complete: false } }],
  ['missing result', f => { delete f.finish.payload.result }],
  ['missing report', f => { f.finish.payload.result = { status: 'ok', message_seq: 99 } }],
  ['string report seq', f => { f.finish.payload.result = { status: 'ok', message_seq: '6' } }],
  ['wrong report type', f => { f.report.type = 'msg.said' }],
  ['wrong report actor', f => { f.report.actor = agent('test') }],
  ['human report', f => { f.report.actor = { ...agent('dev'), kind: 'human' } }],
  ['system report', f => { f.report.actor = host }],
  ['wrong message kind', f => { f.report.payload.kind = 'message' }],
  ['hidden report', f => { f.report.payload.visible = false }],
  ['missing visibility', f => { delete f.report.payload.visible }],
  ['string visibility', f => { f.report.payload.visible = 'true' }],
  ['missing recipient', f => { delete f.report.payload.recipient }],
  ['blank recipient', f => { f.report.payload.recipient = ' ' }],
  ['missing reply', f => { delete f.report.payload.reply_to }],
  ['string reply', f => { f.report.payload.reply_to = '1' }],
  ['invalid reply', f => { f.report.payload.reply_to = 0 }],
  ['future reply', f => { f.report.payload.reply_to = 8 }],
  ['wrong source type', f => { f.source.type = 'msg.said' }],
  ['wrong source kind', f => { f.source.payload.kind = 'notification' }],
  ['wrong source sender', f => { f.source.actor = agent('test') }],
  ['wrong source recipient', f => { f.source.payload.recipient = 'test' }],
  ['report before start', f => { f.start.seq = 7; f.finish.seq = 8; f.finish.payload.started_seq = 7 }],
  ['report after finish', f => { f.report.seq = 8; f.finish.payload.result = { status: 'ok', message_seq: 8 } }],
  ['non-integer report seq', f => { f.report.seq = 5.5; f.finish.payload.result = { status: 'ok', message_seq: 5.5 } }],
]

test.each(invalidReports)('错误或不完整 return_work 证据不吞掉真实发言：%s', (_, change) => {
  const input = fixture()
  change(input)
  expect(teamMessageEchoes(input.events)).toEqual(new Set())
  expect(relations(input.events).some(([source]) => source === 'event-2')).toBe(false)
})

test.each(['customer', 'other recipient', 'other run', 'other actor', 'spoofed speaker', 'different text'])('交回成功仍保留真实发言：%s', variation => {
  const { events, said } = fixture()
  if (variation === 'customer') { said.payload.audience = 'customer'; said.payload.recipient = 'human' }
  if (variation === 'other recipient') said.payload.recipient = 'test'
  if (variation === 'other run') said.payload.run_id = 'next-run'
  if (variation === 'other actor' || variation === 'spoofed speaker') said.actor = agent('test')
  if (variation === 'spoofed speaker') said.payload.speaker = 'dev'
  if (variation === 'different text') said.payload.text = `${text}还有补充。`
  expect(teamMessageEchoes(events)).toEqual(new Set())
})

test.each(['pending', 'ok', 'error'])('现有进展条与工具过程使用中文动作并保留 %s 状态', status => {
  const { source, trigger, run, start, report, finish, end } = fixture()
  const events = [source, trigger, run, start]
  if (status === 'ok') events.push(report, finish, end)
  if (status === 'error') {
    finish.payload.result = { status: 'error', code: 'no_current_assignment', message: '本轮没有明确的原委托，未交回任何工作。' }
    events.push(finish)
  }
  const view = conversationActivity({ team, events, names, leaderId: 'manager', sending: false, executionState: 'running' })
  const line = view.lines.find(entry => entry.role === 'dev')!
  expect(line.text).toContain('交回当前工作')
  expect(line.text).toContain(status === 'pending' ? '已发起，等待回执' : status === 'ok' ? '已完成' : '未完成')
  if (status === 'ok') {
    expect(line.text).toContain('本轮消息处理已结束')
    expect(line.moving).toBe(false)
  }
  render(<TeamToolProgress events={events} names={names} />)
  expect(screen.getByText('交回当前工作')).toBeTruthy()
  expect(screen.getByLabelText('团队工具过程').children).toHaveLength(1)
  expect(screen.getByLabelText('团队工具过程').textContent).toContain(status === 'pending' ? '已发起，等待回执' : status === 'ok' ? '工具已完成' : '工具未完成')
  expect(screen.getByLabelText('团队工具过程').textContent).not.toContain('return_work')
  if (status === 'ok') expect(screen.getByText('已投递消息 #6')).toBeTruthy()
  if (status === 'error') expect(screen.getByText('本轮没有明确的原委托，未交回任何工作。')).toBeTruthy()
})

test('旧 send_message 缺少完成回执 name/run 时仍按原规则去重和配对', () => {
  const { events, start, finish } = fixture()
  start.payload.name = 'send_message'
  delete finish.payload.name
  delete finish.payload.run_id
  expect(teamMessageEchoes(events)).toEqual(new Set([4]))
  expect(teamToolReceipt(start, events)).toBe(finish)
})
