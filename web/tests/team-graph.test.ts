import { expect, test } from 'vitest'
import { projectTeamGraph } from '../src/teamGraph'
import type { TeamEvent, TeamState } from '../src/team'

const state: TeamState = { enabled: true, objective: '入口', status: 'running', pending_count: 0, runs: [], assignments: [], checkpoint: '', revision: 1 }
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = { id: 'team', kind: 'system', role: 'team' }): TeamEvent => ({ seq, type, payload, actor })
const message = (seq: number, sender: string, recipient: string, kind = 'assignment', extra = {}) => event(seq, 'team.message_received',
  { recipient, kind, text: `原文 ${seq}`, revision: 1, ...extra }, { id: sender, kind: sender === 'human' ? 'human' : 'agent', role: sender })
const request = (seq: number, extra = {}) => event(seq, 'team.review_requested', { author: 'dev', reviewer: 'test', instruction: '验证实际文件',
  artifacts: [{ seq: seq - 1, filename: '入口.html', content_sha256: 'a'.repeat(64) }], revision: 1, ...extra })
const decision = (seq: number, reviewSeq: number, root: number, verdict = 'approved') => event(seq, 'team.review_recorded',
  { review_seq: reviewSeq, root_seq: root, reviewer: 'test', verdict, feedback: `验证反馈 ${seq}` })
const relations = (events: TeamEvent[], team = state) => projectTeamGraph(team, events).edges.map(edge => [edge.source, edge.target, edge.label])

test('委托、子委托、逐级回报仅使用真实消息与准确参与者，不推导严格依赖', () => {
  const events = [message(2, 'manager', 'dev'), message(3, 'dev', 'test', 'assignment', { parent_assignment_seq: 2 }),
    message(4, 'test', 'dev', 'report', { reply_to: 3 }), message(5, 'dev', 'manager', 'report', { reply_to: 2 })]
  const before = structuredClone(events)
  const graph = projectTeamGraph(state, events)
  expect(graph.nodes.map(node => node.seq)).toEqual([2, 3, 4, 5])
  expect(relations(events)).toEqual([['event-2', 'event-3', '子委托'], ['event-3', 'event-4', '回报'], ['event-2', 'event-5', '回报']])
  expect(graph.nodes.find(node => node.seq === 2)?.status).toBe('已收到回报')
  expect(graph.nodes.some(node => node.status.includes('通过'))).toBe(false)
  expect(projectTeamGraph(state, [...events].reverse())).toEqual(graph)
  expect(events).toEqual(before)
})

test('知会和进展关联来源但不关闭委托，普通协作消息也可承接子委托与回报', () => {
  const events = [message(2, 'manager', 'dev', 'message'), message(3, 'dev', 'test', 'assignment', { parent_assignment_seq: 2 }),
    message(4, 'dev', 'manager', 'progress', { reply_to: 2 }), message(5, 'dev', 'manager', 'notification', { parent_assignment_seq: 2 })]
  expect(relations(events).map(edge => edge[2])).toEqual(['子委托', '进展', '进展知会'])
  expect(projectTeamGraph(state, events).nodes[0].status).toBe('消息等待处理')
})

test.each(['missing', 'wrong-parent', 'wrong-reporter', 'wrong-recipient', 'future', 'self'])('不为缺失或不匹配的引用伪造关系：%s', (name) => {
  const child = message(3, 'dev', 'test', 'assignment', { parent_assignment_seq: name === 'missing' ? 999 : name === 'future' ? 7 : name === 'self' ? 3 : 2 })
  const report = message(4, name === 'wrong-reporter' ? 'other' : 'test', name === 'wrong-recipient' ? 'manager' : 'dev', 'report', { reply_to: 3 })
  const events = [message(2, 'manager', name === 'wrong-parent' ? 'other' : 'dev'), child, report, message(7, 'manager', 'dev')]
  const graph = projectTeamGraph(state, events)
  expect(graph.nodes).toHaveLength(4)
  expect(graph.nodes.some(node => node.seq === 999)).toBe(false)
  if (['wrong-reporter', 'wrong-recipient'].includes(name)) expect(graph.edges.some(edge => edge.label === '回报')).toBe(false)
  else expect(graph.edges.some(edge => edge.label === '子委托')).toBe(false)
})

const delegation = () => [message(2, 'human', 'manager', 'message'),
  event(3, 'team.tool_started', { role: 'manager', name: 'delegate', message_seq: 2, run_id: 'run' }),
  message(4, 'manager', 'dev'),
  event(5, 'team.tool_finished', { role: 'manager', name: 'delegate', started_seq: 3, run_id: 'run', result: { status: 'ok', message_seq: 4 } })]

test('成功工具回执将真实首层委托关联到顾客消息；单独工具意图不产生节点', () => {
  expect(relations(delegation())).toEqual([['event-2', 'event-4', '委托']])
  expect(projectTeamGraph(state, [delegation()[1]]).nodes).toEqual([])
})

const directReview = () => [
  message(2, 'human', 'dev', 'message', { text: '@作者 请发布并请伙伴复核' }),
  event(3, 'team.run_started', { role: 'dev', message_seq: 2, run_id: 'author-run', revision: 1 }),
  event(4, 'team.tool_started', { role: 'dev', message_seq: 2, run_id: 'author-run', name: 'publish_artifact' }),
  event(5, 'deliverable.published', { filename: '入口.html', content: '<html></html>', content_sha256: 'a'.repeat(64), source: 'team', phase: 'team' },
    { id: 'dev', kind: 'agent', role: 'dev' }),
  event(6, 'team.tool_finished', { started_seq: 4, role: 'dev', run_id: 'author-run', name: 'publish_artifact', result: { status: 'ok', artifact_seq: 5, filename: '入口.html' } }),
  event(7, 'team.tool_started', { role: 'dev', message_seq: 2, run_id: 'author-run', name: 'request_review', arguments: { reviewer: 'test', artifact_seqs: [5], instruction: '验证实际文件' } }),
  request(8, { root_seq: null, run_id: 'author-run', requested_by: 'dev', artifacts: [{ seq: 5, filename: '入口.html', content_sha256: 'a'.repeat(64) }] }),
  message(9, 'dev', 'test', 'assignment', { review_seq: 8, parent_assignment_seq: null }),
  event(10, 'team.tool_finished', { started_seq: 7, role: 'dev', run_id: 'author-run', name: 'request_review', result: { status: 'ok', review_seq: 8, root_seq: 8, message_seq: 9, waiting_for_review: true } }),
  event(11, 'team.run_finished', { role: 'dev', message_seq: 2, run_id: 'author-run', status: 'waiting' }),
]

test('直接点名作者发布后成功发起复核，来源沿顾客消息到复核请求再到实际委托', () => {
  const events = directReview()
  expect(relations(events)).toEqual([
    ['event-8', 'event-9', '投递复核'], ['event-2', 'event-8', '请求复核'],
  ])
  expect(projectTeamGraph(state, events).nodes.map(node => node.seq)).toEqual([2, 8, 9])
  expect(projectTeamGraph(state, [...events].reverse())).toEqual(projectTeamGraph(state, events))
})

test('相同复核来源重复成功回执不增加关系，也不重复已有的投递复核边', () => {
  const events = directReview()
  const repeated = { ...events[8], seq: 12 }
  expect(relations([...events, repeated])).toEqual(relations(events))
  expect(relations([...events, repeated])).toHaveLength(2)
})

test('已有子委托边只去重同源关系，不吞掉成功回执证明的另一条来源', () => {
  const events = [message(2, 'manager', 'dev'), message(3, 'human', 'dev', 'message'),
    event(4, 'team.tool_started', { role: 'dev', name: 'delegate', message_seq: 2, run_id: 'run' }),
    message(5, 'dev', 'test', 'assignment', { parent_assignment_seq: 2 }),
    event(6, 'team.tool_finished', { role: 'dev', name: 'delegate', started_seq: 4, run_id: 'run', result: { status: 'ok', message_seq: 5 } })]
  expect(relations(events)).toEqual([['event-2', 'event-5', '子委托']])
  events[2].payload.message_seq = 3
  expect(relations(events)).toEqual([['event-2', 'event-5', '子委托'], ['event-3', 'event-5', '委托']])
})

test.each(['failed', 'wrong-role', 'wrong-run', 'wrong-source', 'untrusted'])('拒绝无证据或不匹配的委托工具回执：%s', name => {
  const events = delegation()
  if (name === 'failed') events[3].payload.result = { status: 'error', message_seq: 4 }
  if (name === 'wrong-role') events[3].payload.role = 'other'
  if (name === 'wrong-run') events[3].payload.run_id = 'other'
  if (name === 'wrong-source') events[0].payload.recipient = 'other'
  if (name === 'untrusted') events[3].actor.kind = 'agent'
  expect(relations(events)).toEqual([])
})

test.each(['failed', 'interrupted', 'uncertain', 'waiting', 'completed'])('实际消息处理状态 %s 不冒充成果验收或回报', status => {
  const graph = projectTeamGraph(state, [message(2, 'manager', 'dev'), event(3, 'team.run_finished', { message_seq: 2, role: 'dev', status, reason: '真实原因' })])
  expect(graph.nodes[0].status).not.toMatch(/通过|交付|回报/)
  expect(graph.nodes[0].reason).toBe('真实原因')
  expect(graph.nodes[0].processing).toBe(graph.nodes[0].status)
})

test('失败运行即使发出自动回报仍显示失败，不显示已完成', () => {
  const graph = projectTeamGraph(state, [message(2, 'manager', 'dev'), event(3, 'team.run_finished', { message_seq: 2, role: 'dev', status: 'failed' }),
    message(4, 'dev', 'manager', 'report', { reply_to: 2 })])
  expect(graph.nodes[0].status).toBe('消息处理失败')
  expect(graph.edges[0].label).toBe('回报')
})

test('两轮真实复核保留各自版本及反馈，并标明被替代记录', () => {
  const events = [request(10), message(11, 'dev', 'test', 'assignment', { review_seq: 10 }), decision(12, 10, 10, 'changes_requested'),
    message(13, 'test', 'dev', 'report', { reply_to: 11, review_seq: 10 }), request(20, { root_seq: 10 }), decision(21, 20, 10)]
  const graph = projectTeamGraph(state, events)
  expect(graph.nodes.map(node => node.seq)).toEqual([10, 11, 12, 13, 20, 21])
  expect(graph.nodes.find(node => node.seq === 12)?.text).toBe('验证反馈 12')
  expect(graph.nodes.find(node => node.seq === 10)?.status).toBe('已被再次复核替代')
  expect(graph.nodes.find(node => node.seq === 20)).toMatchObject({ status: '该版本复核通过', artifacts: [{ seq: 19, filename: '入口.html' }] })
  expect(relations(events)).toContainEqual(['event-10', 'event-20', '再次复核'])
  expect(relations(events)).toContainEqual(['event-20', 'event-21', '复核结论'])
})

test.each(['outdated', 'stale', 'pending', 'changes_requested'] as const)('服务端复核状态 %s 撤回当前通过，不靠 team.revision 猜过期', status => {
  const events = [request(10), decision(12, 10, 10)]
  const team: TeamState = { ...state, revision: 100, status: 'done', reviews: [{ root_seq: 10, request_seq: 10, author: 'dev', reviewer: 'test', artifacts: [], status, revision: 1 }] }
  expect(projectTeamGraph(team, events).nodes.every(node => !node.status.includes('通过'))).toBe(true)
  expect(projectTeamGraph({ ...team, reviews: [] }, events).nodes[0].status).toBe('该版本复核通过')
})

test('新目标把旧委托和旧复核标为历史，完成事件的 revision 不使复核误过期', () => {
  const events = [message(2, 'manager', 'dev'), request(10), decision(12, 10, 10), event(14, 'team.objective_completed', {})]
  expect(projectTeamGraph({ ...state, revision: 14 }, events).nodes.find(node => node.seq === 10)?.status).toBe('该版本复核通过')
  const graph = projectTeamGraph(state, [...events, event(15, 'team.objective_updated', { goal: '新方向' }), message(16, 'manager', 'dev')])
  expect(graph.nodes.filter(node => node.seq < 15).every(node => node.historical)).toBe(true)
  expect(graph.nodes.find(node => node.seq === 10)?.status).toBe('历史复核')
  expect(graph.nodes.at(-1)?.historical).toBe(false)
})

test('复核结论必须属于实际请求、指定复核者和同一轮根记录', () => {
  const fake = decision(12, 10, 999)
  const wrongReviewer = decision(13, 10, 10); wrongReviewer.payload.reviewer = 'other'
  const untrusted = decision(14, 10, 10); untrusted.actor.kind = 'agent'
  expect(projectTeamGraph(state, [request(10), fake, wrongReviewer, untrusted, decision(15, 404, 404)]).nodes).toHaveLength(1)
})

test('没有真实事件时不拿旧 assignments 或 runs 伪造节点', () => {
  expect(projectTeamGraph({ ...state, assignments: [{ seq: 2, sender: 'manager', recipient: 'dev' }], runs: [{ message_seq: 2, status: 'running' }] }, []))
    .toEqual({ nodes: [], edges: [] })
})
