import { render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import { conversationActivity, teamToolReceipt } from '../src/conversationActivityState'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { isTeamContinueWorkReceipt, teamContinuationSource, teamContinueWorkMessage, teamMessageEchoes } from '../src/team'
import type { TeamEvent, TeamState } from '../src/team'
import { projectTeamGraph } from '../src/teamGraph'

const host = { id: 'team', kind: 'system', role: 'team' }
const agent = (id: string, role = id) => ({ id, kind: 'agent', role })
const event = (seq: number, type: string, payload: Record<string, unknown>, actor: TeamEvent['actor'] = host): TeamEvent & { ts: string } => ({ seq, type, payload, actor, ts: new Date().toISOString() })
const instruction = '保留原实现，补查真实回执后继续测试。'
const team: TeamState = { enabled: true, objective: '修复入口', status: 'running', pending_count: 0,
  revision: 1, runs: [], assignments: [], checkpoint: '' }
const names = { manager: '店长', dev: '开发', test: '测试' }
const fixture = () => {
  const source = event(1, 'team.message_received', { kind: 'assignment', recipient: 'test', text: '原测试委托', revision: 1, reply_to: null, parent_assignment_seq: null }, agent('dev', 'developer'))
  const input = event(2, 'team.message_received', { kind: 'assignment', recipient: 'dev', text: '当前修复委托', revision: 1, reply_to: null, parent_assignment_seq: null }, agent('manager', 'leader'))
  const run = event(3, 'team.run_started', { role: 'dev', run_id: 'continue-run', message_seq: 2, revision: 1 })
  const said = event(4, 'msg.said', { phase: 'team', run_id: 'continue-run', text: instruction, audience: 'team', recipient: 'test', reply_to: 2 }, agent('dev', 'developer'))
  const start = event(5, 'team.tool_started', { name: 'continue_work', role: 'dev', run_id: 'continue-run', call_id: 'continue-call',
    message_seq: 2, revision: 1, arguments: { assignment_seq: 1, instruction } })
  const continued = event(6, 'team.message_received', { kind: 'continuation', visible: true, text: instruction, recipient: 'test',
    continue_assignment_seq: 1, revision: 1, reply_to: null, parent_assignment_seq: null }, agent('dev', 'developer'))
  const finish = event(7, 'team.tool_finished', { name: 'continue_work', role: 'dev', run_id: 'continue-run', started_seq: 5,
    result: { status: 'ok', message_seq: 6 } })
  return { source, input, run, said, start, continued, finish, events: [source, input, run, said, start, continued, finish] }
}
type Fixture = ReturnType<typeof fixture>
type Variation = [string, (f: Fixture) => void]
const bySeq = (events: TeamEvent[]) => new Map(events.map(entry => [entry.seq, entry]))
const relations = (events: TeamEvent[]) => projectTeamGraph(team, events).edges.map(edge => [edge.source, edge.target, edge.label])
const currentEdges = (events: TeamEvent[]) => relations(events).filter(([source]) => source === 'event-2')

test.each(['assignment', 'message'])('continue_work 精确关联原%s、当前输入及可见正文，不关闭工作', kind => {
  const f = fixture()
  f.source.payload.kind = kind
  const before = structuredClone(f.events)
  expect(isTeamContinueWorkReceipt(f.start, f.finish)).toBe(true)
  expect(teamToolReceipt(f.start, f.events)).toBe(f.finish)
  expect(teamContinueWorkMessage(f.start, f.finish, bySeq(f.events))).toBe(f.continued)
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  expect(teamMessageEchoes([...f.events].reverse())).toEqual(new Set([4]))
  expect(relations(f.events)).toEqual([['event-1', 'event-6', '接续原委托'], ['event-2', 'event-6', '接续交接']])
  const graph = projectTeamGraph(team, f.events)
  expect(graph.nodes.find(node => node.seq === 6)).toMatchObject({ kind: 'continuation', sender: 'dev', recipient: 'test', text: instruction, status: '接续指令已投递' })
  expect(graph.nodes.filter(node => [1, 2].includes(node.seq)).every(node => node.status !== '已收到回报')).toBe(true)
  expect(graph.nodes.some(node => /交付|验收通过/.test(node.status))).toBe(false)
  expect(projectTeamGraph(team, [...f.events].reverse())).toEqual(graph)
  expect(f.events).toEqual(before)
})

test.each([4, 8])('同轮全文回声在工具前后均合并，消息 #%s 不重复', seq => {
  const f = fixture()
  f.said.seq = seq
  expect(teamMessageEchoes(f.events)).toEqual(new Set([seq]))
})

test('接续指令的长内部空白保持原文，不作为端点空白重新匹配', () => {
  const f = fixture()
  const body = `核对${' '.repeat(31000)}证据`
  f.start.payload.arguments = { assignment_seq: 1, instruction: `\u0085${body}\u001c` }
  f.continued.payload.text = f.said.payload.text = body
  expect(teamContinueWorkMessage(f.start, f.finish, bySeq(f.events))).toBe(f.continued)
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  expect(currentEdges(f.events)).toHaveLength(1)
})

test('重复回执及持久 parent 关联不重复节点或图边，可匹配一致的可选元数据', () => {
  const f = fixture()
  f.continued.payload.parent_assignment_seq = 2
  Object.assign(f.finish.payload, { call_id: 'continue-call', message_seq: 2, revision: 1, tool: 'continue_work', arguments: { instruction, assignment_seq: 1 } })
  f.start.payload.tool = 'continue_work'
  f.events.push({ ...f.finish, seq: 8 })
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  expect(relations(f.events)).toEqual([['event-1', 'event-6', '接续原委托'], ['event-2', 'event-6', '接续交接']])
  expect(projectTeamGraph(team, f.events).nodes.filter(node => node.seq === 6)).toHaveLength(1)
})

test.each([undefined, 'ok', 'completed', 'succeeded'])('成功回执的可选 status=%s 保留投递及去重', status => {
  const f = fixture()
  f.finish.payload.status = status
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  expect(currentEdges(f.events)).toHaveLength(1)
})

test('instruction 原参数保留，消息按 producer 去除端点空格，内部换行原样匹配', () => {
  const f = fixture()
  const body = `${instruction}\n核对第二项。`
  f.start.payload.arguments = { assignment_seq: 1, instruction: ` ${body} ` }
  f.continued.payload.text = f.said.payload.text = body
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  expect(projectTeamGraph(team, f.events).nodes.find(node => node.seq === 6)?.text).toBe(body)
})

const invalidPairs: Variation[] = [
  ['missing start', f => { f.events = f.events.filter(entry => entry !== f.start) }],
  ['missing finish', f => { f.events = f.events.filter(entry => entry !== f.finish) }],
  ['wrong start type', f => { f.start.type = 'workspace.tool_started' }],
  ['wrong finish type', f => { f.finish.type = 'workspace.tool_finished' }],
  ...(['start', 'finish'] as const).flatMap(which => [
    [`${which} fake host kind`, (f: Fixture) => { f[which].actor = { ...host, kind: 'agent' } }],
    [`${which} fake host id`, (f: Fixture) => { f[which].actor = { ...host, id: 'other' } }],
    [`${which} fake host role`, (f: Fixture) => { f[which].actor = { ...host, role: 'other' } }],
    [`${which} wrong name`, (f: Fixture) => { f[which].payload.name = 'send_message' }],
    [`${which} return_work name`, (f: Fixture) => { f[which].payload.name = 'return_work' }],
    [`${which} missing name`, (f: Fixture) => { delete f[which].payload.name }],
    [`${which} conflicting alias`, (f: Fixture) => { f[which].payload.tool = 'send_message' }],
    [`${which} missing run`, (f: Fixture) => { delete f[which].payload.run_id }],
    [`${which} wrong run`, (f: Fixture) => { f[which].payload.run_id = 'other-run' }],
    [`${which} missing role`, (f: Fixture) => { delete f[which].payload.role }],
    [`${which} wrong role`, (f: Fixture) => { f[which].payload.role = 'test' }],
  ] as Variation[]),
  ['blank runs', f => { f.start.payload.run_id = f.finish.payload.run_id = ' ' }],
  ['blank roles', f => { f.start.payload.role = f.finish.payload.role = ' ' }],
  ['missing call', f => { delete f.start.payload.call_id }],
  ['blank call', f => { f.start.payload.call_id = ' ' }],
  ['cross call', f => { f.finish.payload.call_id = 'other-call' }],
  ['cross input', f => { f.finish.payload.message_seq = 1 }],
  ['cross revision', f => { f.finish.payload.revision = 9 }],
  ['wrong started_seq', f => { f.finish.payload.started_seq = 3 }],
  ['string started_seq', f => { f.finish.payload.started_seq = '5' }],
  ['missing started_seq', f => { delete f.finish.payload.started_seq; f.finish.payload.call_id = 'continue-call' }],
  ['finish before start', f => { f.start.seq = 8; f.finish.payload.started_seq = 8 }],
  ['fractional start', f => { f.start.seq = 4.5; f.finish.payload.started_seq = 4.5 }],
  ['fractional finish', f => { f.finish.seq = 7.5 }],
  ['missing input seq', f => { delete f.start.payload.message_seq }],
  ['string input seq', f => { f.start.payload.message_seq = '2' }],
  ['future input seq', f => { f.start.payload.message_seq = 8 }],
  ['missing arguments', f => { delete f.start.payload.arguments }],
  ...([null, [], 'instruction', { assignment_seq: 1 }, { instruction }, { assignment_seq: 1, instruction, recipient: 'test' },
    { assignment_seq: 1, instruction, text: instruction }, { assignment_seq: 1, instruction: '' }, { assignment_seq: 1, instruction: ' ' },
    { assignment_seq: 1, instruction: 123 }] as unknown[]).map((args, index): Variation => [`bad args ${index}`, f => { f.start.payload.arguments = args }]),
  ...([0, -1, 1.5, '1', true, 5, 9, Number.MAX_SAFE_INTEGER + 1] as unknown[]).map((seq): Variation => [`bad assignment_seq ${seq}`, f => {
    f.start.payload.arguments = { assignment_seq: seq, instruction }
  }]),
  ...([null, [], {}, { assignment_seq: 2, instruction }, { assignment_seq: 1, instruction: '替换指令' },
    { assignment_seq: 1, instruction, recipient: 'test' }] as unknown[]).map((args, index): Variation => [`conflicting repeated args ${index}`, f => { f.finish.payload.arguments = args }]),
]

test.each(invalidPairs)('不可信 continue_work 配对不能吞正文或新增输入图边：%s', (_, change) => {
  const f = fixture()
  change(f)
  expect(teamMessageEchoes(f.events)).toEqual(new Set())
  expect(currentEdges(f.events)).toEqual([])
  if (f.events.includes(f.start)) expect(teamToolReceipt(f.start, f.events)).toBeUndefined()
  // A persisted continuation independently proves its original assignment relation.
  expect(relations(f.events)).toEqual([['event-1', 'event-6', '接续原委托']])
})

const invalidMessages: Variation[] = [
  ['error result', f => { f.finish.payload.result = { status: 'error', message_seq: 6 } }],
  ['failed receipt', f => { f.finish.payload.status = 'failed' }],
  ['incomplete receipt', f => { f.finish.payload.result = { status: 'ok', message_seq: 6, complete: false } }],
  ['not started outcome', f => { f.finish.payload.result = { status: 'ok', message_seq: 6, outcome: 'not_started' } }],
  ['missing result', f => { delete f.finish.payload.result }],
  ['array result', f => { f.finish.payload.result = [] }],
  ['missing message', f => { f.finish.payload.result = { status: 'ok', message_seq: 99 } }],
  ['string message seq', f => { f.finish.payload.result = { status: 'ok', message_seq: '6' } }],
  ['wrong message type', f => { f.continued.type = 'msg.said' }],
  ['wrong sender', f => { f.continued.actor = agent('test') }],
  ['wrong sender role', f => { f.continued.actor.role = 'other' }],
  ['human sender', f => { f.continued.actor.kind = 'human' }],
  ['system sender', f => { f.continued.actor = host }],
  ...['report', 'message', 'notification', 'assignment'].map((kind): Variation => [`wrong kind ${kind}`, f => { f.continued.payload.kind = kind }]),
  ...[false, undefined, 'true'].map((visible): Variation => [`bad visibility ${visible}`, f => { f.continued.payload.visible = visible }]),
  ['wrong recipient', f => { f.continued.payload.recipient = 'manager' }],
  ['blank recipient', f => { f.continued.payload.recipient = ' ' }],
  ['missing recipient', f => { delete f.continued.payload.recipient }],
  ['wrong original seq', f => { f.continued.payload.continue_assignment_seq = 2 }],
  ['string original seq', f => { f.continued.payload.continue_assignment_seq = '1' }],
  ['wrong actual text', f => { f.continued.payload.text = `${instruction}额外指令。` }],
  ['missing actual text', f => { delete f.continued.payload.text }],
  ['conflicting reply', f => { f.continued.payload.reply_to = 1 }],
  ['conflicting notify', f => { f.continued.payload.notify_only = true }],
  ['cross message revision', f => { f.continued.payload.revision = 8 }],
  ['cross source revision', f => { f.source.payload.revision = 8 }],
  ['missing original', f => { f.events = f.events.filter(entry => entry !== f.source) }],
  ['wrong original type', f => { f.source.type = 'msg.said' }],
  ['wrong original kind', f => { f.source.payload.kind = 'report' }],
  ['wrong original sender', f => { f.source.actor = agent('manager', 'leader') }],
  ['wrong original role', f => { f.source.actor.role = 'other' }],
  ['human original', f => { f.source.actor.kind = 'human' }],
  ['wrong original recipient', f => { f.source.payload.recipient = 'manager' }],
  ['missing current input', f => { f.events = f.events.filter(entry => entry !== f.input) }],
  ['wrong current input recipient', f => { f.input.payload.recipient = 'test' }],
  ['wrong current input type', f => { f.input.type = 'msg.said' }],
  ['forged current input', f => { f.input.actor = { ...host, kind: 'agent' }; f.input.payload.kind = 'recovery' }],
  ['run input mismatch', f => { f.run.payload.message_seq = 1 }],
  ['run role mismatch', f => { f.run.payload.role = 'test' }],
  ['run starts after tool', f => { f.run.seq = 8 }],
  ['run starts before input', f => { f.input.seq = 4; f.start.payload.message_seq = f.run.payload.message_seq = 4; f.said.seq = 8 }],
  ['duplicate host run start', f => { f.said.seq = 8; f.events.push({ ...f.run, seq: 4 }) }],
  ['run already ended', f => { f.said.seq = 8; f.events.push(event(4, 'team.run_finished', { role: 'dev', run_id: 'continue-run', message_seq: 2, status: 'completed' })) }],
  ['message before start', f => { f.start.seq = 6; f.continued.seq = 5; f.finish.payload.started_seq = 6; f.finish.payload.result = { status: 'ok', message_seq: 5 } }],
  ['message at finish', f => { f.continued.seq = 7; f.finish.seq = 8; f.finish.payload.result = { status: 'ok', message_seq: 7 }; f.start.seq = 7; f.finish.payload.started_seq = 7 }],
  ['message after finish', f => { f.continued.seq = 8; f.finish.payload.result = { status: 'ok', message_seq: 8 } }],
  ['fractional message seq', f => { f.continued.seq = 5.5; f.finish.payload.result = { status: 'ok', message_seq: 5.5 } }],
  ['wrong parameter target', f => {
    f.events.push(event(8, 'team.message_received', { ...f.source.payload }, f.source.actor))
    f.start.payload.arguments = { assignment_seq: 8, instruction }
  }],
  ['different valid original target', f => {
    f.events = f.events.filter(entry => entry !== f.run)
    f.events.push(event(3, 'team.message_received', { ...f.source.payload }, f.source.actor))
    f.start.payload.arguments = { assignment_seq: 3, instruction }
  }],
]

test.each(invalidMessages)('不一致的参数、投递或目标不吞正文、不补图边：%s', (_, change) => {
  const f = fixture()
  change(f)
  expect(teamContinueWorkMessage(f.events.includes(f.start) ? f.start : undefined, f.finish, bySeq(f.events))).toBeUndefined()
  expect(teamMessageEchoes(f.events)).toEqual(new Set())
  expect(currentEdges(f.events)).toEqual([])
})

const differentSpeech: Variation[] = [
  ['customer', f => { f.said.payload.audience = 'customer'; f.said.payload.recipient = 'human' }],
  ['other recipient', f => { f.said.payload.recipient = 'manager' }],
  ['other run', f => { f.said.payload.run_id = 'other-run' }],
  ['different body', f => { f.said.payload.text = `${instruction}另有说明。` }],
  ['other actor', f => { f.said.actor = agent('test') }],
  ['spoofed speaker', f => { f.said.actor = agent('test'); f.said.payload.speaker = 'dev' }],
  ['same id wrong actor role', f => { f.said.actor.role = 'other' }],
  ['human same id', f => { f.said.actor.kind = 'human' }],
  ['fake host', f => { f.said.actor = { ...host, role: 'other' }; f.said.payload.speaker = 'dev' }],
  ['speech before input', f => { f.said.seq = 1 }],
]

test.each(differentSpeech)('成功 continue_work 仍保留非全文同轮发言：%s', (_, change) => {
  const f = fixture()
  change(f)
  expect(teamMessageEchoes(f.events)).toEqual(new Set())
})

test('真实宿主代录同轮发言可合并', () => {
  const f = fixture()
  f.said.actor = host
  f.said.payload.speaker = 'dev'
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
})

test('当前输入也是接续时，明确面向顾客的同文发言仍保留', () => {
  const f = fixture()
  f.input.payload.kind = 'continuation'
  f.input.payload.visible = true
  f.input.actor = agent('test')
  f.said.payload.audience = 'customer'
  expect(teamMessageEchoes(f.events)).toEqual(new Set())
  expect(currentEdges(f.events)).toHaveLength(1)
})

test('run 记录缺页时可信回执仍可绑定，伪造同名 run 不提供证据', () => {
  const f = fixture()
  f.run.actor = agent('team')
  f.run.payload.role = 'test'
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  expect(currentEdges(f.events)).toHaveLength(1)
  expect(teamMessageEchoes(f.events.filter(entry => entry !== f.run))).toEqual(new Set([4]))
})

test('同 run_id 但在该轮结束之后的发言仍保留', () => {
  const f = fixture()
  f.events.push(event(8, 'team.run_finished', { role: 'dev', run_id: 'continue-run', message_seq: 2, status: 'completed' }))
  f.said.seq = 9
  expect(teamMessageEchoes(f.events)).toEqual(new Set())
  expect(currentEdges(f.events)).toHaveLength(1)
})

test.each(['hidden continuation', 'recovery', 'unknown message'])('图不补出未展示输入的悬空边：%s', kind => {
  const f = fixture()
  f.input.payload.kind = kind === 'recovery' ? 'recovery' : kind === 'hidden continuation' ? 'continuation' : 'unknown'
  f.input.payload.visible = false
  if (kind === 'recovery') f.input.actor = host
  delete f.said.payload.reply_to
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  const graph = projectTeamGraph(team, f.events)
  const nodes = new Set(graph.nodes.map(node => node.id))
  expect(graph.edges.every(edge => nodes.has(edge.source) && nodes.has(edge.target))).toBe(true)
  expect(currentEdges(f.events)).toEqual([])
})

test.each(['pending', 'ok', 'error'])('中文工具过程与活动状态呈现 %s，接续不终止发件人', status => {
  const f = fixture()
  const events = [f.source, f.input, f.run, f.start]
  if (status === 'ok') events.push(f.continued, f.finish)
  if (status === 'error') {
    f.finish.payload.result = { status: 'error', code: 'continuation_closed', message: '原委托已交回，未接续。' }
    events.push(f.finish)
  }
  const line = conversationActivity({ team, events, names, leaderId: 'manager', sending: false, executionState: 'running' }).lines.find(entry => entry.role === 'dev')!
  expect(line.text).toContain('接续原工作')
  expect(line.text).toContain(status === 'pending' ? '已发起，等待回执' : status === 'ok' ? '已完成' : '未完成')
  expect(line.text).not.toMatch(/交回当前工作|本轮消息处理已结束/)
  expect(line.moving).toBe(true)
  render(<TeamToolProgress events={events} names={names} />)
  const progress = screen.getByLabelText('团队工具过程')
  expect(progress.children).toHaveLength(1)
  expect(within(progress).getByText('接续原工作')).toBeTruthy()
  expect(progress.textContent).not.toContain('continue_work')
  expect(progress.textContent).toContain(status === 'pending' ? '已发起，等待回执' : status === 'ok' ? '工具已完成' : '工具未完成')
  if (status === 'ok') expect(within(progress).getByText('已投递消息 #6')).toBeTruthy()
  if (status === 'error') expect(within(progress).getByText('原委托已交回，未接续。')).toBeTruthy()
})

test('接续后发送者继续工具调用，原收件人用 return_work 回原委托', () => {
  const f = fixture()
  const next = event(8, 'team.tool_started', { name: 'workspace_read', role: 'dev', run_id: 'continue-run', message_seq: 2 })
  const receiverRun = event(9, 'team.run_started', { role: 'test', run_id: 'test-run', message_seq: 6, revision: 1 })
  const text = '回执核对通过，交回原委托。'
  const returnStart = event(10, 'team.tool_started', { name: 'return_work', role: 'test', run_id: 'test-run', message_seq: 6, arguments: { text } })
  const report = event(11, 'team.message_received', { kind: 'report', recipient: 'dev', reply_to: 1, visible: true, text }, agent('test'))
  const returnFinish = event(12, 'team.tool_finished', { name: 'return_work', role: 'test', run_id: 'test-run', started_seq: 10, result: { status: 'ok', message_seq: 11 } })
  const events = [...f.events, next, receiverRun, returnStart, report, returnFinish]
  const graph = projectTeamGraph(team, events)
  expect(graph.nodes.find(node => node.seq === 1)?.status).toBe('已收到回报')
  expect(graph.nodes.find(node => node.seq === 2)?.status).not.toBe('已收到回报')
  expect(relations(events)).toContainEqual(['event-1', 'event-11', '回报'])
  expect(relations(events)).toContainEqual(['event-6', 'event-11', '协作消息'])
  const line = conversationActivity({ team, events, names, leaderId: 'manager', sending: false, executionState: 'running' }).lines.find(entry => entry.role === 'dev')!
  expect(line.text).toContain('读取文件')
  expect(line.moving).toBe(true)
})

const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
async function mount(events: TeamEvent[]) {
  window.sessionStorage.clear()
  const leader = { id: 'manager', name: '店长', role: 'leader' }
  const workers = [{ id: 'dev', name: '开发', role: 'developer' }, { id: 'test', name: '测试', role: 'tester' }]
  const detail: TaskDetail = { task: { id: 'continue-task', goal: team.objective, status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [], team },
    events: events.map(entry => ({ ...entry, category: 'collaboration',
      actor: { ...entry.actor, kind: entry.actor.kind ?? 'agent', role: entry.actor.role ?? entry.actor.id } })),
    activity: { active: true, state: 'running' }, review: {} }
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'continue-task', goal: team.objective, status: 'running', status_label: '进行中', version: 7, latest_seq: 7, conversation_count: 3, team }] })
    if (url === '/api/tasks/continue-task') return json(detail)
    if (url === '/api/organization') return json([leader, ...workers])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers })
    throw new Error(`Unexpected request ${url}`)
  }))
  render(<App />)
  await screen.findByRole('region', { name: '团队进度' }, { timeout: 3000 })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  return within(screen.getByLabelText('聊天记录'))
}

test.each([4, 8])('真实聊天保留可见接续正文且全文只出现一次：回声 #%s', async seq => {
  const f = fixture()
  f.said.seq = seq
  const chat = await mount(f.events)
  expect(chat.getAllByText(instruction)).toHaveLength(1)
  expect(chat.getByText(instruction).closest('article')?.className).toBe('collaboration-notice')
  expect(chat.getByText('@开发 → @测试 · 接续工作交接')).toBeTruthy()
  const board = within(screen.getByRole('complementary', { name: '成果与过程' }))
  expect(within(board.getByLabelText('团队工具过程')).getByText('接续原工作')).toBeTruthy()
})

const visibleFailures: Variation[] = [
  invalidPairs.find(([name]) => name === 'start fake host kind')!,
  invalidPairs.find(([name]) => name === 'finish fake host role')!,
  invalidPairs.find(([name]) => name === 'finish wrong run')!,
  invalidPairs.find(([name]) => name === 'finish wrong role')!,
  invalidPairs.find(([name]) => name === 'cross call')!,
  invalidPairs.find(([name]) => name === 'string started_seq')!,
  invalidPairs.find(([name]) => name === 'start wrong name')!,
  invalidPairs.find(([name]) => name === 'conflicting repeated args 4')!,
  invalidMessages.find(([name]) => name === 'error result')!,
  invalidMessages.find(([name]) => name === 'wrong recipient')!,
  invalidMessages.find(([name]) => name === 'different valid original target')!,
  invalidMessages.find(([name]) => name === 'wrong actual text')!,
  differentSpeech.find(([name]) => name === 'other run')!,
  differentSpeech.find(([name]) => name === 'spoofed speaker')!,
  differentSpeech.find(([name]) => name === 'same id wrong actor role')!,
]

test.each(visibleFailures)('真实聊天不因伪造或交叉证据隐藏发言：%s', async (name, change) => {
  const f = fixture()
  change(f)
  const chat = await mount(f.events)
  expect(chat.getAllByText(instruction).some(node => node.closest('article')?.className !== 'collaboration-notice')).toBe(true)
  expect(currentEdges(f.events)).toEqual(differentSpeech.some(([speech]) => speech === name) ? [['event-2', 'event-6', '接续交接']] : [])
})

test.each([false, undefined, 'true'])('隐藏或未确认可见的接续不能替代真实发言：visible=%s', async visible => {
  const f = fixture()
  f.continued.payload.visible = visible
  const chat = await mount(f.events)
  expect(chat.getAllByText(instruction)).toHaveLength(1)
  expect(chat.getByText(instruction).closest('article')?.className).not.toBe('collaboration-notice')
  expect(projectTeamGraph(team, f.events).nodes.some(node => node.seq === 6)).toBe(false)
})

test('旧 send_message 无回执 name/run 仍兼容去重；原委托来源校验独立于新工具', () => {
  const f = fixture()
  f.start.payload.name = 'send_message'
  f.start.payload.arguments = { recipient: 'test', text: instruction, continue_assignment_seq: 1 }
  delete f.finish.payload.name
  delete f.finish.payload.run_id
  expect(teamMessageEchoes(f.events)).toEqual(new Set([4]))
  expect(teamToolReceipt(f.start, f.events)).toBe(f.finish)
  expect(teamContinuationSource(f.continued, bySeq(f.events))).toBe(f.source)
})

type ProducerCase = {
  name: string; raw_instruction: string; python_stripped: string; parent: boolean; chain_verified: boolean
  source_seq: number; input_seq: number; said_seq: number; start_seq: number; message_seq: number | null; finish_seq: number
  events: TeamEvent[]
}
// Captured Runtime snapshots are embedded so this test is independent of local QA files.
const producerSnapshots: Record<string, TeamEvent[]> = {
  "parent": [
    {
      "seq": 8,
      "type": "team.message_received",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "kind": "assignment",
        "parent_assignment_seq": null,
        "recipient": "design",
        "reply_to": null,
        "revision": 0,
        "text": "Inspect and return this exact work."
      }
    },
    {
      "seq": 21,
      "type": "team.message_received",
      "actor": {
        "id": "design",
        "role": "\u4ea4\u4e92\u4e0e\u89c6\u89c9\u732b",
        "kind": "agent"
      },
      "payload": {
        "kind": "assignment",
        "parent_assignment_seq": null,
        "recipient": "product",
        "reply_to": null,
        "revision": 0,
        "text": "Inspect and return this exact work."
      }
    },
    {
      "seq": 27,
      "type": "team.run_started",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "run_id": "return-work-21-3",
        "role": "product",
        "message_seq": 21,
        "revision": 0,
        "control_seq": null
      }
    },
    {
      "seq": 29,
      "type": "msg.said",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "audience": "team",
        "claim": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
        "phase": "team",
        "recipient": "design",
        "reply_to": 21,
        "response_kind": "progress",
        "run_id": "return-work-21-3",
        "text": "Inspect the existing evidence.\nPreserve  internal\tspacing."
      }
    },
    {
      "seq": 30,
      "type": "team.tool_started",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "arguments": {
          "assignment_seq": 8,
          "instruction": "Inspect the existing evidence.\nPreserve  internal\tspacing."
        },
        "call_id": "supervision-6-0",
        "execution_contract": {
          "kind": "atomic_local_events",
          "receipt": "same_transaction",
          "version": 1
        },
        "message_seq": 21,
        "name": "continue_work",
        "revision": 0,
        "role": "product",
        "root": null,
        "run_id": "return-work-21-3"
      }
    },
    {
      "seq": 31,
      "type": "team.message_received",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "continue_assignment_seq": 8,
        "kind": "continuation",
        "parent_assignment_seq": 21,
        "recipient": "design",
        "reply_to": null,
        "revision": 0,
        "text": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
        "visible": true
      }
    },
    {
      "seq": 32,
      "type": "team.tool_finished",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "name": "continue_work",
        "result": {
          "message_seq": 31,
          "status": "ok"
        },
        "role": "product",
        "run_id": "return-work-21-3",
        "started_seq": 30
      }
    }
  ],
  "noParent": [
    {
      "seq": 8,
      "type": "team.message_received",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "kind": "assignment",
        "parent_assignment_seq": null,
        "recipient": "design",
        "reply_to": null,
        "revision": 0,
        "text": "Inspect and return this exact work."
      }
    },
    {
      "seq": 17,
      "type": "team.message_received",
      "actor": {
        "id": "human",
        "role": "human",
        "kind": "human"
      },
      "payload": {
        "text": "@\u6708\u4eae Inspect the current fixture input.",
        "recipient": "product",
        "kind": "message",
        "request_id": null
      }
    },
    {
      "seq": 18,
      "type": "team.run_started",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "run_id": "return-work-17-2",
        "role": "product",
        "message_seq": 17,
        "revision": 0,
        "control_seq": null
      }
    },
    {
      "seq": 20,
      "type": "msg.said",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "audience": "customer",
        "claim": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
        "phase": "team",
        "recipient": "human",
        "reply_to": 17,
        "response_kind": "progress",
        "run_id": "return-work-17-2",
        "text": "Inspect the existing evidence.\nPreserve  internal\tspacing."
      }
    },
    {
      "seq": 21,
      "type": "team.tool_started",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "arguments": {
          "assignment_seq": 8,
          "instruction": "Inspect the existing evidence.\nPreserve  internal\tspacing."
        },
        "call_id": "supervision-4-0",
        "execution_contract": {
          "kind": "atomic_local_events",
          "receipt": "same_transaction",
          "version": 1
        },
        "message_seq": 17,
        "name": "continue_work",
        "revision": 0,
        "role": "product",
        "root": null,
        "run_id": "return-work-17-2"
      }
    },
    {
      "seq": 22,
      "type": "team.message_received",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "continue_assignment_seq": 8,
        "kind": "continuation",
        "parent_assignment_seq": null,
        "recipient": "design",
        "reply_to": null,
        "revision": 0,
        "text": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
        "visible": true
      }
    },
    {
      "seq": 23,
      "type": "team.tool_finished",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "name": "continue_work",
        "result": {
          "message_seq": 22,
          "status": "ok"
        },
        "role": "product",
        "run_id": "return-work-17-2",
        "started_seq": 21
      }
    }
  ],
  "rejected": [
    {
      "seq": 8,
      "type": "team.message_received",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "kind": "assignment",
        "parent_assignment_seq": null,
        "recipient": "design",
        "reply_to": null,
        "revision": 0,
        "text": "Inspect and return this exact work."
      }
    },
    {
      "seq": 21,
      "type": "team.message_received",
      "actor": {
        "id": "design",
        "role": "\u4ea4\u4e92\u4e0e\u89c6\u89c9\u732b",
        "kind": "agent"
      },
      "payload": {
        "kind": "assignment",
        "parent_assignment_seq": null,
        "recipient": "product",
        "reply_to": null,
        "revision": 0,
        "text": "Inspect and return this exact work."
      }
    },
    {
      "seq": 27,
      "type": "team.run_started",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "run_id": "return-work-21-3",
        "role": "product",
        "message_seq": 21,
        "revision": 0,
        "control_seq": null
      }
    },
    {
      "seq": 29,
      "type": "msg.said",
      "actor": {
        "id": "product",
        "role": "\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b",
        "kind": "agent"
      },
      "payload": {
        "audience": "team",
        "claim": "Producer attempt.",
        "phase": "team",
        "recipient": "design",
        "reply_to": 21,
        "response_kind": "progress",
        "run_id": "return-work-21-3",
        "text": "Producer attempt."
      }
    },
    {
      "seq": 30,
      "type": "team.tool_started",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "arguments": {
          "assignment_seq": 8,
          "instruction": "\u0085\u001c\u001d\u001e\u001f "
        },
        "call_id": "supervision-6-0",
        "execution_contract": {
          "kind": "atomic_local_events",
          "receipt": "same_transaction",
          "version": 1
        },
        "message_seq": 21,
        "name": "continue_work",
        "revision": 0,
        "role": "product",
        "root": null,
        "run_id": "return-work-21-3"
      }
    },
    {
      "seq": 31,
      "type": "team.tool_finished",
      "actor": {
        "id": "team",
        "role": "team",
        "kind": "system"
      },
      "payload": {
        "name": "continue_work",
        "result": {
          "message": "\u64cd\u4f5c\u672a\u5b8c\u6210\uff0c\u8bf7\u6838\u5bf9\u5de5\u5177\u53c2\u6570\u3001\u6743\u9650\u4e0e\u5df2\u6709\u8bb0\u5f55\u540e\u4fee\u6b63\u3002",
          "status": "error"
        },
        "role": "product",
        "run_id": "return-work-21-3",
        "started_seq": 30
      }
    }
  ]
}
const producerCases: ProducerCase[] = [
  {
    "name": "no-parent",
    "raw_instruction": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": false,
    "source_seq": 8,
    "input_seq": 17,
    "said_seq": 20,
    "start_seq": 21,
    "message_seq": 22,
    "finish_seq": 23,
    "chain_verified": true
  },
  {
    "name": "plain",
    "raw_instruction": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "ascii-edges",
    "raw_instruction": " \t\r\nInspect the existing evidence.\nPreserve  internal\tspacing.\r\n\t ",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "next-line-0085",
    "raw_instruction": "\u0085Inspect the existing evidence.\nPreserve  internal\tspacing.\u0085",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "control-001c",
    "raw_instruction": "\u001cInspect the existing evidence.\nPreserve  internal\tspacing.\u001c",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "control-001d",
    "raw_instruction": "\u001dInspect the existing evidence.\nPreserve  internal\tspacing.\u001d",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "control-001e",
    "raw_instruction": "\u001eInspect the existing evidence.\nPreserve  internal\tspacing.\u001e",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "control-001f",
    "raw_instruction": "\u001fInspect the existing evidence.\nPreserve  internal\tspacing.\u001f",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "unicode-white-space",
    "raw_instruction": "\t\n\u000b\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000Inspect the existing evidence.\nPreserve  internal\tspacing.\t\n\u000b\f\r \u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "bom-edges",
    "raw_instruction": "\ufeffInspect the existing evidence.\nPreserve  internal\tspacing.\ufeff",
    "python_stripped": "\ufeffInspect the existing evidence.\nPreserve  internal\tspacing.\ufeff",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "bom-only",
    "raw_instruction": "\ufeff",
    "python_stripped": "\ufeff",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "bom-stops-strip",
    "raw_instruction": " \u0085\ufeff\u001c Inspect the existing evidence.\nPreserve  internal\tspacing. \u001f\ufeff\u0085 ",
    "python_stripped": "\ufeff\u001c Inspect the existing evidence.\nPreserve  internal\tspacing. \u001f\ufeff",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "internal-unicode",
    "raw_instruction": " \u0085Inspect the existing evidence.\nPreserve  internal\tspacing.\u0085\u001c\ufeff\nlast line\u001f ",
    "python_stripped": "Inspect the existing evidence.\nPreserve  internal\tspacing.\u0085\u001c\ufeff\nlast line",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": 31,
    "finish_seq": 32,
    "chain_verified": true
  },
  {
    "name": "empty-python-whitespace",
    "raw_instruction": "\u0085\u001c\u001d\u001e\u001f ",
    "python_stripped": "",
    "parent": true,
    "source_seq": 8,
    "input_seq": 21,
    "said_seq": 29,
    "start_seq": 30,
    "message_seq": null,
    "finish_seq": 31,
    "chain_verified": true
  }
].map(row => {
  const events = structuredClone(producerSnapshots[row.message_seq === null ? 'rejected' : row.parent ? 'parent' : 'noParent'])
  const start = events.find(entry => entry.seq === row.start_seq)!
  start.payload.arguments = { assignment_seq: row.source_seq, instruction: row.raw_instruction }
  const said = events.find(entry => entry.seq === row.said_seq)!
  said.payload.text = row.message_seq === null ? 'Producer attempt.' : row.python_stripped
  if (row.message_seq !== null) events.find(entry => entry.seq === row.message_seq)!.payload.text = row.python_stripped
  return { ...row, events }
})
const producer = (name: string) => structuredClone(producerCases.find(row => row.name === name)!)
const producerParts = (row: ProducerCase) => {
  const map = bySeq(row.events)
  return { map, start: map.get(row.start_seq)!, finish: map.get(row.finish_seq)!,
    message: row.message_seq === null ? undefined : map.get(row.message_seq), said: map.get(row.said_seq)! }
}

test.each(producerCases.map(row => [row.name, row] as const))('真实 Runtime 持久 JSON 契约：%s', (_, row) => {
  const { map, start, finish, message } = producerParts(row)
  expect(row.chain_verified).toBe(true)
  expect(start.payload.arguments).toEqual({ assignment_seq: row.source_seq, instruction: row.raw_instruction })
  if (!message) {
    expect(row.python_stripped).toBe('')
    expect((finish.payload.result as Record<string, unknown>).status).toBe('error')
    expect(isTeamContinueWorkReceipt(start, finish)).toBe(false)
    expect(teamContinueWorkMessage(start, finish, map)).toBeUndefined()
    expect(teamMessageEchoes(row.events)).toEqual(new Set())
    return
  }
  expect(message.payload.reply_to).toBeNull()
  expect(message.payload.parent_assignment_seq).toBe(row.parent ? row.input_seq : null)
  expect(message.payload.text).toBe(row.python_stripped)
  expect(teamContinueWorkMessage(start, finish, map)).toBe(message)
  expect(teamToolReceipt(start, row.events)).toBe(finish)
  expect(teamMessageEchoes(row.events)).toEqual(new Set(row.parent ? [row.said_seq] : []))
  const graph = projectTeamGraph(team, row.events)
  expect(graph.nodes.find(node => node.seq === row.message_seq)?.text).toBe(row.python_stripped)
  expect(graph.edges).toContainEqual({ id: `${row.source_seq}:${row.message_seq}:接续原委托`, source: `event-${row.source_seq}`, target: `event-${row.message_seq}`, label: '接续原委托' })
  expect(graph.edges).toContainEqual({ id: `${row.input_seq}:${row.message_seq}:接续交接`, source: `event-${row.input_seq}`, target: `event-${row.message_seq}`, label: '接续交接' })
})

test.each([null, undefined])('真实 producer 的 reply_to=%s 与缺省均可兼容', reply => {
  const row = producer('plain')
  const { start, finish, message, map } = producerParts(row)
  if (reply === undefined) delete message!.payload.reply_to
  else message!.payload.reply_to = reply
  expect(teamContinueWorkMessage(start, finish, map)).toBe(message)
  expect(teamMessageEchoes(row.events)).toEqual(new Set([row.said_seq]))
})

test.each([0, false, '', true, 1, '1', {}, []])('真实 producer 消息被注入非 null reply_to=%j 时不能去重', reply => {
  const row = producer('plain')
  const { start, finish, message, map } = producerParts(row)
  message!.payload.reply_to = reply
  expect(teamContinueWorkMessage(start, finish, map)).toBeUndefined()
  expect(teamMessageEchoes(row.events)).toEqual(new Set())
  expect(projectTeamGraph(team, row.events).edges.some(edge => edge.id === `${row.input_seq}:${row.message_seq}:协作消息`)).toBe(false)
})

const wrongProducerText: [string, string, (row: ProducerCase) => string][] = [
  ['unstripped ascii edges', 'ascii-edges', row => row.raw_instruction],
  ['JS trim leaves U0085', 'next-line-0085', row => row.raw_instruction.trim()],
  ...['001c', '001d', '001e', '001f'].map(code => [`JS trim leaves U${code}`, `control-${code}`, (row: ProducerCase) => row.raw_instruction.trim()] as [string, string, (row: ProducerCase) => string]),
  ['JS trim drops BOM', 'bom-edges', row => row.raw_instruction.trim()],
  ['JS trim empties BOM-only', 'bom-only', row => row.raw_instruction.trim()],
  ['strip beyond BOM barrier', 'bom-stops-strip', row => row.python_stripped.replace(/\ufeff/g, '').trim()],
  ['collapse internal spaces', 'plain', row => row.python_stripped.replace('  ', ' ')],
  ['change internal newline bytes', 'plain', row => row.python_stripped.replace('\n', '\r\n')],
  ['drop internal U0085', 'internal-unicode', row => row.python_stripped.replace(/\u0085/g, '')],
  ['drop internal U001C', 'internal-unicode', row => row.python_stripped.replace(/\u001c/g, '')],
  ['drop internal BOM', 'internal-unicode', row => row.python_stripped.replace(/\ufeff/g, '')],
]

test.each(wrongProducerText)('真实 producer 正文必须严格匹配 Python strip，拒绝误归并：%s', (_, name, change) => {
  const row = producer(name)
  const { start, finish, message, map } = producerParts(row)
  const wrong = change(row)
  expect(wrong).not.toBe(row.python_stripped)
  message!.payload.text = wrong
  // Remove the independent persisted parent relation to isolate receipt-created edges.
  message!.payload.parent_assignment_seq = null
  expect(teamContinueWorkMessage(start, finish, map)).toBeUndefined()
  expect(teamMessageEchoes(row.events)).toEqual(new Set())
  expect(projectTeamGraph(team, row.events).edges.filter(edge => edge.source === `event-${row.input_seq}`)).toEqual([])
})

test.each(['ascii-edges', 'next-line-0085', 'bom-edges', 'internal-unicode'])('真实 producer 回声不做额外正文规范：%s', name => {
  const row = producer(name)
  const { said } = producerParts(row)
  said.payload.text = name === 'bom-edges' ? row.python_stripped.trim() : row.raw_instruction
  expect(said.payload.text).not.toBe(row.python_stripped)
  expect(teamMessageEchoes(row.events)).toEqual(new Set())
})

test.each(['ascii-edges', 'next-line-0085', 'control-001c', 'bom-edges', 'internal-unicode', 'no-parent'])('真实 producer JSON 的聊天正文与中文工具进度：%s', async name => {
  const row = producer(name)
  const chat = await mount(row.events)
  expect(chat.getAllByText(/Inspect the existing evidence\./)).toHaveLength(row.parent ? 1 : 2)
  const board = within(screen.getByRole('complementary', { name: '成果与过程' }))
  const progress = within(board.getByLabelText('团队工具过程'))
  expect(progress.getByText('接续原工作')).toBeTruthy()
  expect(progress.getByText(`已投递消息 #${row.message_seq}`)).toBeTruthy()
  expect(board.getByLabelText('团队工具过程').textContent).toContain('工具已完成')
})
