import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'

type Activity = NonNullable<TaskDetail['activity']>
const taskId = '重启同步任务'
const goal = '核对重启后的团队进展'
const draft = '保留这条未发送的补充要求'
const oldInstance = 'z-旧服务实例'
const newInstance = 'a-新服务实例'
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function activity(state: Activity['state'], revision: number, instance_id: string | undefined = newInstance): Activity {
  return { state, revision, instance_id, active: state === 'running' || state === 'queued' }
}

function summary(current: Activity, version = 10, completed = false, paused = false, targetRevision = 10) {
  return {
    id: taskId, goal, status: 'running', status_label: '夹具状态', conversation_count: 1,
    version, latest_seq: version, evaluated_at: current.instance_id === oldInstance ? 900000 : 1,
    activity: current,
    ...(paused ? { execution_control: { state: 'paused' as const, seq: 50, reason: '用户要求暂停' } } : {}),
    team: { enabled: true, status: 'running', revision: targetRevision, pending_count: 0,
      runs: completed ? [{ message_seq: 8, role: 'dev', status: 'completed', revision: targetRevision }] : [] },
  }
}

function detail(row: ReturnType<typeof summary>, embeddedSummary = true): TaskDetail {
  return {
    task: { id: taskId, goal, status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [],
      team: { ...row.team, objective: goal, assignments: [], checkpoint: '' } },
    events: [{ seq: row.version, type: 'team.objective_updated', category: 'team',
      actor: { id: 'team', kind: 'system', role: 'team' }, payload: { goal } }],
    activity: row.activity, review: {}, ...(embeddedSummary ? { summary: row } : {}),
    ...(row.execution_control ? { execution_control: row.execution_control } : {}),
  }
}

let current: ReturnType<typeof summary>
let readSummary: () => Response | Promise<Response>
let readDetail: () => Response | Promise<Response>
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  window.sessionStorage.clear()
  vi.useFakeTimers()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  current = summary(activity('failed', 900, oldInstance))
  readSummary = () => json({ tasks: [current] })
  readDetail = () => json(detail(current))
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    if ((init.method ?? 'GET').toUpperCase() !== 'GET') throw new Error(`夹具禁止写请求：${url}`)
    if (url === '/api/task-summaries') return Promise.resolve(readSummary())
    if (url === `/api/tasks/${taskId}`) return Promise.resolve(readDetail())
    if (url === '/api/organization') return Promise.resolve(json([]))
    if (url === '/api/organization/profile') return Promise.resolve(json({
      leader: { id: 'store_manager', name: '店长', role: 'leader' }, cabinet: [], workers: [],
    }))
    throw new Error(`未声明的夹具请求：${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const input = () => screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
const refresh = async () => { await act(async () => { document.dispatchEvent(new Event('visibilitychange')) }) }
const tick = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const detailCalls = () => fetchMock.mock.calls.filter(([url]) => url === `/api/tasks/${taskId}`)

async function mount() {
  await act(async () => { render(<App />) })
  // Lazy panels must resolve before advancing the polling clock.
  await act(async () => { await vi.dynamicImportSettled() })
  expect(screen.getByLabelText('团队执行状态')).toBeTruthy()
  fireEvent.change(input(), { target: { value: draft } })
}

function expectEntries(label: string, paused = false) {
  const selected = within(screen.getByRole('navigation', { name: '任务列表' }))
    .getByRole('button', { name: new RegExp(goal) })
  expect(selected.textContent).toContain(label)
  expect(selected.getAttribute('aria-current')).toBe('page')
  expect(document.querySelector('.chat-heading .status-pill')?.textContent).toBe(label)
  expect(screen.getByLabelText('团队执行状态').textContent).toBe(paused ? '已暂停后续执行' : label)
  expect(input().value).toBe(draft)
  expect(within(selected).getByRole('img', { name: '有未发送内容' })).toBeTruthy()
  expect(fetchMock.mock.calls.every(([, init]) => (init.method ?? 'GET').toUpperCase() === 'GET')).toBe(true)
}

const states = [
  { state: 'idle', revision: 0, label: '任务待推进' },
  { state: 'queued', revision: 1, label: '消息处理已排队' },
  { state: 'running', revision: 2, label: '正在处理消息' },
  { state: 'completed', revision: 3, label: '消息处理完成，任务待交付' },
] as const

test.each((['failed', 'running'] as const).flatMap(oldState => [10, 20].flatMap(version =>
  states.map(next => ({ oldState, version, ...next })),
)))('旧实例 $oldState revision900 → 新实例 $state revision$revision，事件版本 $version，详情挂起', async ({ oldState, version, state, revision, label }) => {
  current = summary(activity(oldState, 900, oldInstance), 10, state === 'completed')
  await mount()
  expectEntries(oldState === 'failed' ? '执行失败，需跟进' : '正在处理消息')
  const pending = deferred<Response>()
  readDetail = () => pending.promise
  const reads = detailCalls().length
  current = summary(activity(state, revision), version, state === 'completed')
  await refresh()
  expect(detailCalls().length).toBeGreaterThan(reads)
  expectEntries(label)
  if (state !== 'running') expect(document.querySelector('.chat-heading .status-pill.is-running')).toBeNull()
})

test.each([false, true])('较早请求的旧实例高 revision 详情不能覆盖较晚新实例摘要（内嵌摘要：%s）', async embedded => {
  await mount()
  const late = deferred<Response>()
  const stale = detail(summary(activity('failed', 999, oldInstance)), embedded)
  readDetail = () => late.promise
  await tick(4000)
  expect(detailCalls()).toHaveLength(2)
  expect(detailCalls()[1][1].signal.aborted).toBe(false)
  current = summary(activity('queued', 1))
  await refresh()
  expectEntries('消息处理已排队')
  await act(async () => late.resolve(json(stale)))
  expect(detailCalls()[1][1].signal.aborted).toBe(false)
  expectEntries('消息处理已排队')
})

test.each([false, true])('摘要先返回后，较晚发起的新实例低 revision 详情可更新它（内嵌摘要：%s）', async embedded => {
  await mount()
  const fresh = detail(summary(activity('running', 2)), embedded)
  const pending = deferred<Response>()
  readDetail = () => pending.promise
  await refresh()
  expectEntries('执行失败，需跟进')
  await act(async () => pending.resolve(json(fresh)))
  expectEntries('正在处理消息')
})

test.each([
  { name: '实例 ID 字典序上升', previousId: newInstance, nextId: oldInstance },
  { name: '旧响应无实例 ID', previousId: undefined, nextId: newInstance },
  { name: '新响应无实例 ID', previousId: oldInstance, nextId: undefined },
])('$name 仍按请求开始顺序接受跨实例摘要', async ({ previousId, nextId }) => {
  current = { ...summary(activity('failed', 900)), evaluated_at: 900000,
    activity: { ...activity('failed', 900), instance_id: previousId } }
  await mount()
  expectEntries('执行失败，需跟进')
  readDetail = () => new Promise(() => {})
  current = { ...summary(activity('queued', 1)), evaluated_at: 1,
    activity: { ...activity('queued', 1), instance_id: nextId } }
  await refresh()
  expectEntries('消息处理已排队')
})

test('较早发起的旧实例摘要晚于新实例详情返回，不能用高 revision 或时间覆盖详情', async () => {
  await mount()
  const pending = deferred<Response>()
  const stale = summary(activity('failed', 999, oldInstance))
  readSummary = () => pending.promise
  readDetail = () => json(detail(summary(activity('running', 2))))
  await refresh()
  expectEntries('正在处理消息')
  await act(async () => pending.resolve(json({ tasks: [stale] })))
  expectEntries('正在处理消息')
})

test.each([newInstance, undefined])('同实例或均无实例 ID，较晚旧 revision 不覆盖较新 revision（%s）', async instanceId => {
  const make = (state: Activity['state'], revision: number) => ({ ...activity(state, revision), instance_id: instanceId })
  current = summary(make('running', 5))
  await mount()
  readDetail = () => json(detail(summary(make('failed', 4))))
  current = summary(make('queued', 3))
  await refresh()
  expectEntries('正在处理消息')
})

test.each(['事件版本', '目标版本'] as const)('%s 优先于实例请求顺序和 activity revision', async clock => {
  current = summary(activity('running', 900, oldInstance), 20)
  await mount()
  readDetail = () => new Promise(() => {})
  current = summary(activity('failed', 999, newInstance), clock === '事件版本' ? 19 : 20, false, false,
    clock === '目标版本' ? 9 : 10)
  await refresh()
  expectEntries('正在处理消息')
})

test('较早请求返回更高持久版本时接受新实例低 revision', async () => {
  await mount()
  const pending = deferred<Response>()
  readDetail = () => pending.promise
  await tick(4000)
  await refresh()
  expectEntries('执行失败，需跟进')
  await act(async () => pending.resolve(json(detail(summary(activity('running', 2), 20)))))
  expectEntries('正在处理消息')
})

test('暂停状态优先于重启后的 running 摘要，草稿和三个入口保持一致', async () => {
  current = summary(activity('failed', 900, oldInstance), 10, false, true)
  await mount()
  expectEntries('已暂停', true)
  readDetail = () => new Promise(() => {})
  current = summary(activity('running', 2), 20)
  await refresh()
  expectEntries('已暂停', true)
  expect(document.querySelector('.chat-heading .status-pill.is-running')).toBeNull()
})
