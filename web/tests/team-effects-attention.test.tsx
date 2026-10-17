import { act, render, screen, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import type { ExecutionControl } from '../src/executionControl'
import { hasActiveTeamMessages, resolveTeamTask, teamStatus } from '../src/team'
import type { TeamActivity, TeamState } from '../src/team'

const goal = '核对恢复操作'
const attention = '有操作影响待核对'
const diagnostic = '操作状态待核对'
const delivered = '店长确认可交付'
const idle = { state: 'idle', active: false, revision: 1 }
const team = (count: unknown, status = 'done'): TeamState => ({
  enabled: true, objective: goal, status, revision: 10, pending_count: 0,
  effects_pending_count: count as number | null | undefined,
  runs: [], assignments: [], checkpoint: '',
})

test.each([null, -1, 0.5, Number.NaN, Number.POSITIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1, '2', false, {}, []])(
  'invalid or unavailable count %j cannot claim a normal state', count => {
    for (const activity of [idle, { state: 'queued', active: true }, { state: 'running', active: true }]) {
      expect(teamStatus(team(count), activity)).toBe(diagnostic)
    }
  },
)

test.each([undefined, 0, 1, 5, null, -1])('new summary replaces old positive count with %s', count => {
  const detail = { goal, status: 'done', team: team(2) }
  const next = team(count)
  if (count === undefined) delete next.effects_pending_count
  const summary = { goal, status: 'done', version: 20, team: next }
  for (const revision of [10, 11]) {
    const merged = resolveTeamTask(detail, { ...summary, team: { ...next, revision } }, 10)!
    expect(merged.team.effects_pending_count).toBe(count)
    expect(teamStatus(merged.team, idle)).toBe(count === null || count === -1 ? diagnostic : count ? attention : delivered)
  }
  expect(detail.team.effects_pending_count).toBe(2)
  expect(resolveTeamTask(detail, summary, 20)).toBe(detail)
  expect(resolveTeamTask(detail, summary, 30)).toBe(detail)
})

test('attention never adds live work or erases live work evidence', () => {
  for (const count of [undefined, 0, 2, null, -1]) {
    expect(hasActiveTeamMessages(team(count))).toBe(false)
    expect(hasActiveTeamMessages({ ...team(count), runs: [{ status: 'uncertain' }] })).toBe(false)
    expect(hasActiveTeamMessages({ ...team(count), runs: [{ status: 'running' }] })).toBe(true)
    expect(hasActiveTeamMessages({ ...team(count), pending_count: 1 })).toBe(true)
  }
})

function snapshot(count: unknown, version = 10, activity: TeamActivity = idle, state: ExecutionControl['state'] = 'running', status = 'done') {
  const value = team(count, status)
  const summary = { id: 'attention-task', goal, status, status_label: 'fixture', version, latest_seq: version,
    conversation_count: 1, team: value, activity, execution_control: { state, seq: 1, reason: '' } }
  const detail: TaskDetail = {
    task: { id: summary.id, goal, status, acceptance: [], messages: [], assignments: [], deliverables: [], team: value },
    events: [{ seq: version, type: status === 'done' ? 'team.objective_completed' : 'team.objective_updated', category: 'team',
      actor: { id: 'team', kind: 'system', role: 'team' }, payload: { goal } }],
    activity, execution_control: summary.execution_control, review: {},
  }
  return { summary, detail }
}

function mount(source: () => { summary: ReturnType<typeof snapshot>['summary']; detail?: TaskDetail }) {
  window.sessionStorage.clear()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  let detailReads = 0
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetchMock = vi.fn((url: string, _init: RequestInit = {}): Promise<Response> => {
    const current = source()
    if (url === '/api/task-summaries') return Promise.resolve(json({ tasks: [current.summary] }))
    if (url === '/api/tasks/attention-task') {
      detailReads += 1
      return current.detail ? Promise.resolve(json(current.detail)) : new Promise(() => {})
    }
    if (url === '/api/organization') return Promise.resolve(json([]))
    if (url === '/api/organization/profile') return Promise.resolve(json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] }))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const app = render(<App />)
  return { app, fetchMock, detailReads: () => detailReads }
}

async function expectEntries(label: string, panelLabel = label) {
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  await screen.findByLabelText('团队执行状态')
  await waitFor(() => {
    expect(screen.getByRole('button', { name: new RegExp(goal) }).textContent).toContain(label)
    expect(document.querySelector('.chat-heading .status-pill')?.textContent).toBe(label)
    expect(screen.getByLabelText('团队执行状态').textContent).toBe(panelLabel)
  })
}

type EntryCase = { name: string; count: unknown; label: string; activity?: TeamActivity;
  state?: ExecutionControl['state']; status?: string; panelLabel?: string }
test.each<EntryCase>([
  { name: 'queued', count: 2, activity: { state: 'queued', active: true }, label: '消息已排队，有操作待核对', status: 'running' },
  { name: 'running', count: 2, activity: { state: 'running', active: true }, label: '正在处理，另有操作待核对', status: 'running' },
  { name: 'idle', count: 2, label: attention, status: 'running' },
  { name: 'failed', count: 2, activity: { state: 'failed', active: false }, label: attention },
  { name: 'done then unknown', count: 2, label: attention },
  { name: 'done with queued follow-up', count: 2, activity: { state: 'queued', active: true }, label: '消息已排队，有操作待核对' },
  { name: 'done with running follow-up', count: 2, activity: { state: 'running', active: true }, label: '正在处理，另有操作待核对' },
  { name: 'paused', count: 2, state: 'paused', label: '已暂停', panelLabel: '已暂停后续执行' },
  { name: 'cancelled', count: 2, state: 'cancelled', label: '已取消', panelLabel: '已取消后续执行' },
  { name: 'zero', count: 0, label: delivered },
  { name: 'legacy missing', count: undefined, label: delivered },
  { name: 'projection failure', count: null, label: diagnostic },
  { name: 'negative', count: -1, label: diagnostic },
  { name: 'string', count: '2', label: diagnostic },
  { name: 'fraction', count: 0.5, label: diagnostic },
  { name: 'paused projection failure', count: null, state: 'paused', label: '已暂停', panelLabel: '已暂停后续执行' },
  { name: 'cancelled projection failure', count: null, state: 'cancelled', label: '已取消', panelLabel: '已取消后续执行' },
])('$name is consistent in App list, heading and progress', async ({ count, label, activity, state, status, panelLabel }) => {
  const data = snapshot(count, 10, activity, state, status)
  const { app, fetchMock } = mount(() => data)
  await expectEntries(label, panelLabel)
  const panel = screen.getByLabelText('团队进度')
  if (count === 2) expect(panel.textContent).toContain('待核对操作：2 项。操作影响尚未确认，请先核对实际结果，勿直接重试。')
  else if (count === undefined || count === 0) expect(panel.textContent).not.toContain('勿直接重试')
  else {
    expect(panel.textContent).toContain('操作状态待核对。操作影响尚未确认，请先核对实际结果，勿直接重试。')
    expect(panel.textContent).not.toContain('待核对操作：')
  }
  if (!activity?.active) expect(document.querySelector('.chat-heading .status-pill.is-running')).toBeNull()
  expect(fetchMock.mock.calls.every(([, init]) => !init.method || init.method === 'GET')).toBe(true)
  app.unmount()
})

test('new summaries reveal unknown after delivery, propagate failure, reconcile to zero and clear omitted counts while detail waits', async () => {
  let data: { summary: ReturnType<typeof snapshot>['summary']; detail?: TaskDetail } = snapshot(0)
  const { app, fetchMock, detailReads } = mount(() => data)
  await expectEntries(delivered)
  let version = 10
  for (const [count, expected] of [[2, attention], [null, diagnostic], [0, delivered], [3, attention],
    [undefined, delivered], [-1, diagnostic], [4, attention], [0, delivered]] as const) {
    data = { summary: { ...snapshot(count, ++version).summary, latest_seq: 10 } }
    await act(async () => document.dispatchEvent(new Event('visibilitychange')))
    await expectEntries(expected)
    await waitFor(() => expect(detailReads()).toBeGreaterThan(1))
    const panel = screen.getByLabelText('团队进度').textContent
    if (typeof count === 'number' && count > 0) expect(panel).toContain(`待核对操作：${count} 项`)
    else expect(panel).not.toContain('待核对操作：')
    if (count === 0 || count === undefined) expect(panel).not.toContain('勿直接重试')
  }
  expect(fetchMock.mock.calls.every(([, init]) => !init.method || init.method === 'GET')).toBe(true)
  app.unmount()
})

test.each([20, 30])('summary version %s cannot clear newer detail attention at version 30', async version => {
  const fresh = snapshot(2, 30)
  const stale = snapshot(0, version, { ...idle, revision: 0 })
  let initial = true
  const { app } = mount(() => ({ summary: stale.summary, detail: initial ? fresh.detail : undefined }))
  await expectEntries(attention)
  initial = false
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  await expectEntries(attention)
  expect(screen.getByLabelText('团队进度').textContent).toContain('待核对操作：2 项')
  app.unmount()
})
