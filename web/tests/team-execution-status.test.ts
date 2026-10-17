import { expect, test } from 'vitest'
import { resolveTeamActivity, resolveTeamTask, teamStatus } from '../src/team'
import type { TeamActivity, TeamState, TeamSummary } from '../src/team'

const team = (extra: Partial<TeamSummary> = {}): TeamSummary => ({
  enabled: true, status: 'running', pending_count: 0, revision: 10, runs: [], ...extra,
})
const activity = (state: string, active = false): TeamActivity => ({ state, active })

test.each([
  ['queued', true, '消息处理已排队'],
  ['running', true, '正在处理消息'],
  ['failed', false, '执行失败，需跟进'],
  ['completed', false, '任务待推进'],
  ['idle', false, '任务待推进'],
] as const)('消费者 %s/%s 只展示有证据的执行状态', (state, active, expected) => {
  expect(teamStatus(team(), activity(state, active))).toBe(expected)
})

test.each([
  ['failed', '消息处理失败'],
  ['interrupted', '本轮消息处理中断'],
  ['waiting', '本轮结束，等待消息'],
  ['cancelled', '本轮消息处理已取消'],
  ['uncertain', '处理结果待核对'],
  ['blocked', '处理结果待核对'],
  ['completed', '消息处理完成，任务待交付'],
] as const)('消费者结束不掩盖角色的 %s 结果，也不冒充交付', (status, expected) => {
  expect(teamStatus(team({ runs: [{ status, revision: 10 }] }), activity('completed'))).toBe(expected)
})

test('排队与待处理不等于执行，失败后仍有待办也不隐藏失败', () => {
  expect(teamStatus(team({ pending_count: 2 }), activity('idle'))).toBe('消息等待处理')
  expect(teamStatus(team({ pending_count: 2, runs: [{ status: 'failed' }] }), activity('completed'))).toBe('处理失败，仍有待办')
})

test('并行角色仍在执行时不因另一个角色已完成就显示整轮结束', () => {
  const value = team({ runs: [{ status: 'running' }, { status: 'completed' }] })
  expect(teamStatus(value, activity('running', true))).toBe('正在处理消息')
  expect(teamStatus(value, activity('completed'))).toBe('执行状态待核对')
  expect(teamStatus(value)).toBe('正在处理消息')
})

test('旧目标失败不污染当前目标，未知旧摘要不虚构正在执行', () => {
  expect(teamStatus(team({ runs: [{ status: 'failed', revision: 9 }] }), activity('completed'))).toBe('任务待推进')
  expect(teamStatus(team({ runs: undefined }))).toBe('任务待推进')
})

test('已交付后的排队、执行与失败属于后续消息，不用旧交付掩盖', () => {
  const value = team({ status: 'done', pending_count: 1 })
  expect(teamStatus(value, activity('queued', true))).toBe('后续消息已排队')
  expect(teamStatus(value, activity('running', true))).toBe('正在处理后续消息')
  expect(teamStatus(value, activity('completed'))).toBe('后续消息等待处理')
  expect(teamStatus(value, activity('failed'))).toBe('执行失败，需跟进')
  expect(teamStatus(team({ status: 'done' }), activity('completed'))).toBe('店长确认可交付')
})

test('同目标也采用较新活动，旧摘要不覆盖新详情，新目标不继承旧失败', () => {
  const failed = { state: 'failed', active: false, revision: 1 }
  const queued = { state: 'queued', active: true, revision: 2 }
  expect(resolveTeamActivity(failed, queued, false)).toBe(queued)
  expect(resolveTeamActivity(queued, failed, false)).toBe(queued)
  expect(resolveTeamActivity(failed, undefined, true)).toBeUndefined()
  expect(resolveTeamActivity(undefined, queued, false)).toBe(queued)
  expect(resolveTeamActivity(failed, undefined, false)).toBe(failed)
})

test('新摘要只展示确知的当前运行，旧摘要不能覆盖新详情或改写历史', () => {
  const detail = { goal: '当前目标', status: 'running', team: {
    ...team(), objective: '当前目标', checkpoint: '', assignments: [],
    runs: [{ message_seq: 1, role: 'dev', status: 'failed' }, { message_seq: 3, role: 'dev', status: 'running' }],
  } as TeamState }
  const summary = { goal: '当前目标', status: 'running', version: 20, team: team({
    runs: [{ message_seq: 3, role: 'dev', status: 'completed' }],
  }) }
  const result = resolveTeamTask(detail, summary, 10)!
  expect(result.team.runs).toEqual(summary.team.runs)
  expect(teamStatus(result.team, activity('completed'))).toBe('消息处理完成，任务待交付')
  expect(detail.team.runs[1].status).toBe('running')
  expect(resolveTeamTask(detail, summary, 21)).toBe(detail)
  const newGoal = resolveTeamTask(detail, { ...summary, goal: '新目标', team: team({ revision: 21 }) }, 20)!
  expect(newGoal.team.runs).toEqual([])
  expect(newGoal.team.reviews).toEqual([])
  expect(newGoal.goal).toBe('新目标')
})
