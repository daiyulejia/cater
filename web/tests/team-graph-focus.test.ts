// @vitest-environment node
import { expect, test } from 'vitest'
import { focusTeamGraph, projectTeamGraph } from '../src/teamGraph'
import type { TeamEvent, TeamState } from '../src/team'

const team: TeamState = { enabled: true, objective: '当前目标', status: 'running', pending_count: 0,
  runs: [], assignments: [], checkpoint: '', revision: 99 }
const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent => ({ seq, type, payload, actor })
const message = (seq: number, sender: string, recipient: string, kind = 'message', extra = {}) => event(seq, 'team.message_received',
  { kind, recipient, text: `原文 ${seq}`, ...extra }, { id: sender, kind: sender === 'human' ? 'human' : 'agent', role: sender })
const objective = () => [message(8, 'human', 'manager'),
  event(9, 'team.run_started', { run_id: 'current-run', message_seq: 8, role: 'manager', revision: 7 }),
  event(10, 'team.objective_updated', { goal: '当前目标', run_id: 'current-run' })]
const focus = (events: TeamEvent[], state = team) => focusTeamGraph(projectTeamGraph(state, events), events)
const seqs = (graph: ReturnType<typeof focus>) => graph.nodes.map(node => node.seq)

test('cutoff 以目标事件为准，保留 run 验证的顾客输入；旧顾客输入不依赖 historical 盲过滤', () => {
  const events = [message(1, 'human', 'manager'), message(7, 'human', 'manager'), ...objective(),
    message(11, 'human', 'test'), message(12, 'manager', 'dev', 'assignment')]
  const before = structuredClone(events)
  const all = projectTeamGraph(team, events)
  const current = focusTeamGraph(all, events)
  expect(current).toMatchObject({ objectiveSeq: 10, triggerSeq: 8 })
  expect(seqs(current)).toEqual([8, 11, 12])
  expect(all.nodes.filter(node => node.historical).map(node => node.seq)).toEqual([1, 7])
  expect(all.nodes.find(node => node.seq === 8)?.historical).toBe(false)
  expect(all.nodes.map(node => node.seq)).toEqual([1, 7, 8, 11, 12])
  expect(focus([...events].reverse())).toEqual(current)
  expect(events).toEqual(before)
})

test.each(['missing-run', 'wrong-run', 'untrusted-run', 'wrong-role', 'missing-role', 'non-human', 'future-source',
  'string-source', 'future-run', 'finished-run', 'ambiguous-run', 'non-message'])('来源不匹配 %s 时不猜最近顾客输入或补造边', invalid => {
  const events = [message(1, 'human', 'manager'), ...objective(), message(12, 'manager', 'dev', 'assignment')]
  const source = events[1], start = events[2]
  if (invalid === 'missing-run') events.splice(2, 1)
  if (invalid === 'wrong-run') start.payload.run_id = 'other'
  if (invalid === 'untrusted-run') start.actor = { ...host, kind: 'agent' }
  if (invalid === 'wrong-role') start.payload.role = 'other'
  if (invalid === 'missing-role') delete start.payload.role
  if (invalid === 'non-human') source.actor = { id: 'dev', kind: 'agent', role: 'dev' }
  if (invalid === 'future-source') source.seq = 11
  if (invalid === 'string-source') start.payload.message_seq = '8'
  if (invalid === 'future-run') start.seq = 11
  if (invalid === 'finished-run') {
    events[3].seq = 11
    events.push(event(10, 'team.run_finished', { run_id: 'current-run' }))
  }
  if (invalid === 'ambiguous-run') events.push(event(7, 'team.run_started', { ...start.payload, message_seq: 1 }))
  if (invalid === 'non-message') source.payload.kind = 'notification'
  const current = focus(events)
  expect(current.objectiveSeq).toBe(invalid === 'finished-run' ? 11 : 10)
  expect(current.triggerSeq).toBeUndefined()
  expect(current.nodes.some(node => node.seq < 10)).toBe(false)
  expect(current.edges).toEqual([])
})

test('不是最近输入而是 objective.run_id 对应的输入；完成 revision 和工具 revision 不移动 cutoff', () => {
  const events = objective()
  events[2].seq = 11
  events.push(message(10, 'human', 'manager'), event(12, 'team.objective_completed', { run_id: 'current-run' }),
    message(13, 'manager', 'dev', 'assignment'))
  expect(focus(events, { ...team, revision: 12, status: 'done' })).toMatchObject({ objectiveSeq: 11, triggerSeq: 8 })
  expect(seqs(focus(events))).toEqual([8, 13])
})

test('跨目标只保留直接真实引用，不扩张旧来源的父链、兄弟回报或历史复核结论', () => {
  const oldRequest = event(6, 'team.review_requested', { author: 'dev', reviewer: 'test', instruction: '旧版复核' })
  const events = [message(1, 'human', 'manager'), message(2, 'manager', 'dev', 'assignment'),
    message(3, 'dev', 'test', 'assignment', { parent_assignment_seq: 2 }),
    message(4, 'test', 'dev', 'report', { reply_to: 3 }), oldRequest,
    event(7, 'team.review_recorded', { review_seq: 6, root_seq: 6, reviewer: 'test', verdict: 'approved' }),
    ...objective(), message(12, 'dev', 'test', 'continuation', { continue_assignment_seq: 3, visible: true }),
    event(13, 'team.review_requested', { author: 'dev', reviewer: 'test', root_seq: 6, instruction: '检查新版' })]
  const current = focus(events)
  expect(seqs(current)).toEqual([3, 6, 8, 12, 13])
  expect(current.edges.map(edge => [edge.source, edge.target, edge.label])).toEqual([
    ['event-3', 'event-12', '接续原委托'], ['event-6', 'event-13', '再次复核'],
  ])
  expect(current.nodes.filter(node => node.historical).map(node => node.seq)).toEqual([3, 6])
  expect(current.edges.every(edge => current.nodes.some(node => node.id === edge.source)
    && current.nodes.some(node => node.id === edge.target))).toBe(true)
  expect(projectTeamGraph(team, events).nodes.map(node => node.seq)).toEqual([1, 2, 3, 4, 6, 7, 8, 12, 13])
})

test('无效旧引用和仅出现在正文的序号不会引入历史', () => {
  const events = [message(1, 'manager', 'other', 'assignment'), ...objective(),
    message(12, 'dev', 'test', 'assignment', { parent_assignment_seq: 1, text: '参考旧成果 #1 / #475' }),
    message(13, 'dev', 'test', 'continuation', { continue_assignment_seq: 1, visible: true })]
  expect(seqs(focus(events))).toEqual([8, 12, 13])
  expect(focus(events).edges).toEqual([])
})

test('旧批准快照及 cutoff 后补到的旧请求结论仍是历史复核', () => {
  const events = [event(2, 'team.review_requested', { author: 'dev', reviewer: 'test', instruction: '旧版' }),
    event(3, 'team.review_recorded', { review_seq: 2, root_seq: 2, reviewer: 'test', verdict: 'approved' }), ...objective(),
    event(12, 'team.review_recorded', { review_seq: 2, root_seq: 2, reviewer: 'test', verdict: 'approved', feedback: '补到的旧版批准' })]
  const state: TeamState = { ...team, reviews: [{ request_seq: 2, root_seq: 2, author: 'dev', reviewer: 'test',
    status: 'approved', revision: 1, artifacts: [] }] }
  const all = projectTeamGraph(state, events)
  expect(all.nodes.filter(node => ['review', 'decision'].includes(node.kind))
    .every(node => node.historical && node.status === '历史复核')).toBe(true)
  expect(seqs(focusTeamGraph(all, events))).toEqual([2, 8, 12])
})

test('没有可信目标时全部展示，空目标范围不退回全历史，事件为空不造记录', () => {
  const old = [message(1, 'human', 'manager'), message(2, 'manager', 'dev')]
  const fake = event(10, 'team.objective_updated', { goal: '伪造' }, { ...host, kind: 'agent' })
  expect(seqs(focus([...old, fake]))).toEqual([1, 2])
  expect(focus(old).objectiveSeq).toBeUndefined()
  expect(focus([...old, event(10, 'team.objective_updated', { goal: '新方向' })]).nodes).toEqual([])
  expect(focus([])).toMatchObject({ nodes: [], edges: [], objectiveSeq: undefined, triggerSeq: undefined })
})

test.each([0, 6, 75, 160])('当前记录增长到 %s 条也不按固定数量截断，旧输入总可在完整投影查阅', count => {
  const old = Array.from({ length: 20 }, (_, index) => message(index + 1, 'human', 'manager'))
  const events = [...old, event(30, 'team.objective_updated', { goal: '新目标' }),
    ...Array.from({ length: count }, (_, index) => message(index + 31, 'manager', 'dev'))]
  expect(focus(events).nodes).toHaveLength(count)
  expect(projectTeamGraph(team, events).nodes).toHaveLength(old.length + count)
})
