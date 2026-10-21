import { expect, test } from 'vitest'
import { resolveTeam, teamRuns, teamRunStatus, teamStatus } from '../src/team'
import type { TeamEvent, TeamState } from '../src/team'
import { projectTeamGraph } from '../src/teamGraph'

test('waiting 只说明本次处理结束，不推断接收者、交接、继续执行或交付', () => {
  expect(teamRunStatus('waiting')).toBe('本次处理结束，待后续信息')
  expect(teamRunStatus('waiting')).not.toMatch(/伙伴|顾客|交接|正在|完成|交付/)
  expect(teamRunStatus('completed')).toBe('消息处理完成')
  expect(teamRunStatus('failed')).toBe('消息处理失败')
  expect(teamRunStatus('interrupted')).toBe('消息处理中断')
})

test('同一消息多次运行取最近记录，各消息独立保留 waiting 与失败，整体 done 不改写运行事件', () => {
  const team: TeamState = { enabled: true, objective: '原目标', status: 'done', revision: 1, pending_count: 0,
    checkpoint: '', assignments: [], runs: [{ message_seq: 1, role: 'manager', status: 'failed', reason: '第一次失败', revision: 1 }] }
  const host = { id: 'team', kind: 'system', role: 'team' }
  const event = (seq: number, type: string, payload: Record<string, unknown>): TeamEvent => ({ seq, type, payload, actor: host })
  const events: TeamEvent[] = [
    { seq: 1, type: 'team.message_received', actor: { id: 'human', kind: 'human' }, payload: { kind: 'message', recipient: 'manager', text: '原输入' } },
    event(2, 'team.run_finished', { message_seq: 1, role: 'manager', status: 'failed', reason: '第一次失败', run_id: 'first' }),
    event(3, 'team.run_started', { message_seq: 1, role: 'manager', run_id: 'retry' }),
    event(4, 'team.run_finished', { message_seq: 1, role: 'manager', status: 'waiting', reason: '本轮静默等待', run_id: 'retry' }),
    { seq: 5, type: 'team.message_received', actor: { id: 'manager', kind: 'agent' }, payload: { kind: 'assignment', recipient: 'dev', text: '独立检查' } },
    event(6, 'team.run_finished', { message_seq: 5, role: 'dev', status: 'failed', reason: '独立检查失败' }),
    event(7, 'team.objective_completed', { run_id: 'retry' }),
  ]
  const before = structuredClone({ team, events })
  expect(teamRuns(team, events.slice(0, 3))[0].status).toBe('running')
  const resolved = resolveTeam(team, events)!
  expect(resolved.status).toBe('done')
  expect(resolved.runs).toEqual([
    { message_seq: 1, role: 'manager', status: 'waiting', reason: '本轮静默等待', revision: 1 },
    { message_seq: 5, role: 'dev', status: 'failed', reason: '独立检查失败', revision: undefined },
  ])
  const graph = projectTeamGraph(resolved, events)
  expect(graph.nodes.map(({ seq, status, processing, reason }) => ({ seq, status, processing, reason }))).toEqual([
    { seq: 1, status: '本次处理结束，待后续信息', processing: '本次处理结束，待后续信息', reason: '本轮静默等待' },
    { seq: 5, status: '消息处理失败', processing: '消息处理失败', reason: '独立检查失败' },
  ])
  expect(graph.edges).toEqual([])
  expect(resolved.runs.some(run => run.status === 'completed')).toBe(false)
  expect(teamStatus({ ...team, runs: [resolved.runs[0]] })).toBe('店长确认可交付')
  expect(teamStatus({ ...team, status: 'running', runs: [resolved.runs[0]] })).toBe('本轮结束，等待消息')
  expect({ team, events }).toEqual(before)
})
