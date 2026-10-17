import { act, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import { resolveTeamTask } from '../src/team'
import type { TeamRun } from '../src/team'

const failedRun: TeamRun = { message_seq: 11, role: 'dev', status: 'failed', revision: 10 }
const completedRun: TeamRun = { message_seq: 14, role: 'dev', status: 'completed', revision: 10 }
type SyncCase = {
  name: string; activity: TaskDetail['activity']; pending: number; label: string; newObjective: boolean
  oldRuns?: TeamRun[]; runs?: TeamRun[]; oldPending?: number; oldActivity?: TaskDetail['activity']
}

test.each<SyncCase>([
  { name: 'queued activity', activity: { state: 'queued', active: true, revision: 2 }, pending: 1, label: '消息处理已排队', newObjective: true },
  { name: 'missing activity', activity: undefined, pending: 0, label: '任务待推进', newObjective: true },
  { name: 'same objective, newer queued activity', activity: { state: 'queued', active: true, revision: 2 }, pending: 1, label: '消息处理已排队', newObjective: false },
  { name: 'same objective, newer completed activity and run while detail hangs',
    activity: { state: 'completed', active: false, revision: 4 }, pending: 0,
    label: '消息处理完成，任务待交付', newObjective: false, oldRuns: [failedRun], runs: [completedRun], oldPending: 1 },
  { name: 'same objective, newer summary version with unchanged activity while detail hangs',
    activity: { state: 'completed', active: false, revision: 4 }, oldActivity: { state: 'completed', active: false, revision: 4 },
    pending: 0, label: '消息处理完成，任务待交付', newObjective: false, oldRuns: [failedRun], runs: [completedRun], oldPending: 1 },
  { name: 'same objective, compact completed summary omits old running run while detail hangs',
    activity: { state: 'completed', active: false, revision: 4 }, oldActivity: { state: 'running', active: true, revision: 2 },
    pending: 0, label: '消息处理完成，任务待交付', newObjective: false,
    oldRuns: [{ ...failedRun, status: 'running' }], runs: [completedRun] },
])('$name keeps all status entries consistent', async ({ activity, pending, label, newObjective, oldRuns = [], runs = [], oldPending = 0, oldActivity }) => {
  window.sessionStorage.clear()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  const old: TaskDetail = {
    task: { id: 'race-task', goal: 'Old objective', status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [],
      team: { enabled: true, objective: 'Old objective', status: 'running', revision: 10, checkpoint: '', pending_count: oldPending, runs: oldRuns, assignments: [] } },
    events: [{ seq: 10, type: 'team.objective_updated', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' }, payload: { goal: 'Old objective' } },
      ...oldRuns.flatMap(run => [
        { seq: 12, type: 'team.run_started', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' },
          payload: { message_seq: run.message_seq, role: run.role, revision: run.revision, run_id: 'old-run' } },
        ...(run.status === 'running' ? [] : [
          { seq: 13, type: 'team.run_finished', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' },
            payload: { ...run, run_id: 'old-run' } },
        ]),
      ])],
    activity: oldActivity ?? { active: false, state: 'failed', revision: 1 }, review: {},
  }
  let stage = 0
  let pendingDetails = 0
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetchMock = vi.fn((url: string, _init: RequestInit = {}): Promise<Response> => {
    if (url === '/api/task-summaries') return Promise.resolve(json({ tasks: [{ id: 'race-task', goal: stage && newObjective ? 'New objective' : 'Old objective',
      status: 'running', status_label: 'fixture', version: stage ? 20 : 10, latest_seq: stage ? 20 : 10, conversation_count: 1,
      ...(!stage ? { activity: old.activity } : activity ? { activity } : {}),
      team: { enabled: true, status: 'running', revision: stage && newObjective ? 20 : 10, pending_count: stage ? pending : oldPending, runs: stage ? runs : oldRuns } }] }))
    if (url === '/api/tasks/race-task') {
      if (!stage) return Promise.resolve(json(old))
      pendingDetails += 1
      return new Promise(() => {})
    }
    if (url === '/api/organization') return Promise.resolve(json([]))
    if (url === '/api/organization/profile') return Promise.resolve(json({ leader: { id: 'store_manager', name: 'Leader', role: 'leader' }, cabinet: [], workers: [] }))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const app = render(<App />)
  expect((await screen.findByLabelText('团队执行状态')).textContent).toBe(
    old.activity?.state === 'running' ? '正在处理消息'
      : old.activity?.state === 'failed' ? '执行失败，需跟进' : '处理失败，仍有待办',
  )
  const baselineProgress = (await screen.findByLabelText('当前工作进展')).textContent
  if (old.activity?.state === 'running') {
    expect(baselineProgress).toContain('dev · 暂无新反馈，执行状态待确认')
    expect(baselineProgress).not.toContain('后台协作异常')
  } else if (oldRuns.length) {
    expect(baselineProgress).toContain('dev · 本轮处理未完成，需核对过程')
    expect(baselineProgress).toContain('后台有 1 条消息等待处理')
    expect(baselineProgress).not.toContain('后台协作异常')
  } else {
    expect(baselineProgress).toContain('后台协作异常')
  }
  stage = 1
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  const goal = newObjective ? 'New objective' : 'Old objective'
  await waitFor(() => {
    expect(pendingDetails).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: new RegExp(goal) }).textContent).toContain(label)
    expect(document.querySelector('.chat-heading h1')?.textContent).toBe(goal)
    expect(document.querySelector('.chat-heading .status-pill')?.textContent).toBe(label)
    expect(screen.getByLabelText('团队执行状态').textContent).toBe(label)
    expect(screen.getByLabelText('当前工作进展').textContent).not.toContain('后台协作异常')
  })
  expect(document.querySelector('.chat-heading .status-pill.is-running')).toBeNull()
  expect(screen.queryByLabelText('有近期执行反馈')).toBeNull()
  expect(screen.getByRole('button', { name: new RegExp(goal) }).textContent).not.toContain('失败')
  if (runs.length) {
    const panel = screen.getByLabelText('团队进度')
    expect(panel.textContent).not.toContain('待处理消息：')
    expect(panel.textContent).toContain('消息处理 · 1 项')
    expect(panel.textContent).not.toContain('消息 #11')
    expect(panel.textContent).toContain('消息处理完成 · 消息 #14')
    expect(panel.textContent).toContain('详细记录正在同步。')
  }
  expect(fetchMock.mock.calls.some(([, init]) => init.method === 'POST')).toBe(false)
  app.unmount()
})

test.each([false, true])('older summary cannot overwrite newer detail (embedded summary: %s)', async embeddedSummary => {
  window.sessionStorage.clear()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  const activity: TaskDetail['activity'] = { state: 'completed', active: false, revision: 4 }
  const summary = {
    id: 'race-task', goal: 'Current objective', status: 'running', status_label: 'fixture',
    version: 30, latest_seq: 30, conversation_count: 1, activity,
    team: { enabled: true, status: 'running', revision: 10, pending_count: 0, runs: [failedRun, completedRun] },
  }
  const fresh: TaskDetail = {
    task: { id: summary.id, goal: summary.goal, status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [],
      team: { ...summary.team, objective: summary.goal, checkpoint: '', assignments: [] } },
    events: [
      { seq: 10, type: 'team.objective_updated', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' }, payload: { goal: summary.goal } },
      { seq: 30, type: 'team.run_finished', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' }, payload: completedRun },
    ],
    activity, review: {}, ...(embeddedSummary ? { summary } : {}),
  }
  const stale = { ...summary, version: 20, latest_seq: 20,
    activity: { state: 'failed', active: false, revision: 1 },
    team: { ...summary.team, pending_count: 2, runs: [failedRun] } }
  let summaryReads = 0
  let detailReads = 0
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetchMock = vi.fn((url: string, _init: RequestInit = {}): Promise<Response> => {
    if (url === '/api/task-summaries') {
      summaryReads += 1
      return Promise.resolve(json({ tasks: [stale] }))
    }
    if (url === '/api/tasks/race-task') {
      detailReads += 1
      return detailReads === 1 ? Promise.resolve(json(fresh)) : new Promise(() => {})
    }
    if (url === '/api/organization') return Promise.resolve(json([]))
    if (url === '/api/organization/profile') return Promise.resolve(json({ leader: { id: 'store_manager', name: 'Leader', role: 'leader' }, cabinet: [], workers: [] }))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const app = render(<App />)
  const label = '消息处理完成，任务待交付'
  await waitFor(() => expect(screen.getByLabelText('团队执行状态').textContent).toBe(label))
  const readsBeforeRefresh = summaryReads
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  await waitFor(() => {
    expect(summaryReads).toBeGreaterThan(readsBeforeRefresh)
    expect(detailReads).toBeGreaterThan(1)
    expect(screen.getByRole('button', { name: /Current objective/ }).textContent).toContain(label)
    expect(document.querySelector('.chat-heading .status-pill')?.textContent).toBe(label)
    expect(screen.getByLabelText('团队执行状态').textContent).toBe(label)
    expect(screen.getByLabelText('团队进度').textContent).not.toContain('待处理消息：')
    expect(screen.getByLabelText('当前工作进展').textContent).not.toContain('后台协作异常')
  })
  expect(fetchMock.mock.calls.some(([, init]) => init.method === 'POST')).toBe(false)
  app.unmount()
})

test('newer compact summary supplies displayed runs without mutating detail or events', () => {
  const running = { ...completedRun, status: 'running', reason: 'old progress' }
  const sibling = { ...running, role: 'test' }
  const task = { goal: 'Current objective', status: 'running', events: [
    { seq: 12, type: 'team.run_started', category: 'team', actor: { id: 'team', kind: 'system', role: 'team' },
      payload: { message_seq: sibling.message_seq, role: sibling.role, revision: 10, run_id: 'old-running' } },
  ], team: {
    enabled: true, objective: 'Current objective', status: 'running', revision: 10, pending_count: 1,
    runs: [failedRun, running, sibling], assignments: [], checkpoint: 'old checkpoint',
    reviews: [{ root_seq: 11, request_seq: 11, author: 'dev', reviewer: 'test', artifacts: [], status: 'approved' as const, revision: 10 }],
  } }
  const summary = { goal: task.goal, status: 'running', version: 20,
    team: { enabled: true, status: 'running', revision: 10, pending_count: 0, runs: [completedRun] } }
  const original = structuredClone(task)
  const merged = resolveTeamTask(task, summary, 13)!
  expect(merged.team?.runs).toEqual([completedRun])
  expect(merged.team?.pending_count).toBe(0)
  expect(merged.team?.reviews).toBe(task.team.reviews)
  expect(merged.team?.checkpoint).toBe('详细记录正在同步。')
  expect(merged.events).toBe(task.events)
  expect(task.team.runs).toEqual([failedRun, running, sibling])
  expect(task.team.pending_count).toBe(1)
  expect(resolveTeamTask(task, summary, 20)).toBe(task)
  expect(resolveTeamTask(task, summary, 30)).toBe(task)
  const nextObjective = resolveTeamTask(task, { ...summary, goal: 'New objective',
    team: { ...summary.team, revision: 40, runs: [] } }, 13)!
  expect(nextObjective.goal).toBe('New objective')
  expect(nextObjective.team?.runs).toEqual([])
  expect(nextObjective.team?.reviews).toEqual([])
  expect(nextObjective.team?.checkpoint).toBe('新目标已收到，详细记录正在同步。')
  expect(nextObjective.events).toBe(task.events)
  expect(task.team.reviews).toHaveLength(1)
  expect(task).toEqual(original)
})
