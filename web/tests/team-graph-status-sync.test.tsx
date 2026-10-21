import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'

// Keep the real lazy graph and React Flow; substitute only jsdom's missing layout.
beforeEach(() => {
  Object.defineProperty(SVGElement.prototype, 'getBBox', { configurable: true, value: () => new DOMRect(0, 0, 60, 18) })
  const dimensions = (element: HTMLElement) => element.classList.contains('react-flow__handle') ? [6, 6]
    : element.classList.contains('react-flow__node') ? [248, 160] : [880, 560]
  for (const property of ['offsetWidth', 'clientWidth'] as const) {
    vi.spyOn(HTMLElement.prototype, property, 'get').mockImplementation(function () { return dimensions(this)[0] })
  }
  for (const property of ['offsetHeight', 'clientHeight'] as const) {
    vi.spyOn(HTMLElement.prototype, property, 'get').mockImplementation(function () { return dimensions(this)[1] })
  }
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    const [width, height] = dimensions(this)
    return new DOMRect(this.classList.contains('react-flow__handle-right') ? 245 : 0,
      this.classList.contains('react-flow__handle') ? 77 : 0, width, height)
  })
  vi.stubGlobal('DOMMatrixReadOnly', class {
    m22: number
    constructor(transform: string) { this.m22 = Number(/scale\(([^)]+)\)/.exec(transform)?.[1] ?? 1) }
  })
  vi.stubGlobal('ResizeObserver', class {
    targets = new Set<Element>()
    pending = false
    callback: ResizeObserverCallback
    constructor(callback: ResizeObserverCallback) { this.callback = callback }
    observe(target: Element) {
      this.targets.add(target)
      if (this.pending) return
      this.pending = true
      queueMicrotask(() => {
        this.pending = false
        if (this.targets.size) this.callback([...this.targets].map(element => ({ target: element,
          contentRect: element.getBoundingClientRect() } as ResizeObserverEntry)), this as unknown as ResizeObserver)
      })
    }
    unobserve(target: Element) { this.targets.delete(target) }
    disconnect() { this.targets.clear() }
  })
})
afterEach(() => { delete (SVGElement.prototype as SVGElement & { getBBox?: () => DOMRect }).getBBox })

type SyncCase = { name: string; activity: TaskDetail['activity']; control?: TaskDetail['execution_control']; label: string }
test.each<SyncCase>([
  { name: '较新后续执行 activity', activity: { state: 'running', active: true, revision: 2 }, label: '正在处理后续消息' },
  { name: '较新失败 activity', activity: { state: 'failed', active: false, revision: 2 }, label: '执行失败，需跟进' },
  { name: '暂停优先于运行 activity', activity: { state: 'running', active: true, revision: 2 },
    control: { state: 'paused', seq: 20, reason: '顾客暂停后续执行' }, label: '已暂停' },
  { name: '取消优先于运行 activity', activity: { state: 'running', active: true, revision: 2 },
    control: { state: 'cancelled', seq: 20, reason: '顾客取消后续执行' }, label: '已取消' },
])('$name 同步到实际懒加载图谱，与聊天标题一致且不沿用旧交付', async ({ activity, control, label }) => {
  window.sessionStorage.clear()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  const host = { id: 'team', kind: 'system', role: 'team' }
  const old: TaskDetail = {
    task: { id: 'graph-sync', goal: '已交付目标的后续消息', status: 'running', acceptance: [], messages: [], assignments: [], deliverables: [],
      team: { enabled: true, objective: '已交付目标的后续消息', status: 'done', revision: 3, checkpoint: '', pending_count: 0,
        runs: [{ message_seq: 1, role: 'store_manager', status: 'waiting', revision: 3 }], assignments: [] } },
    events: [
      { seq: 1, type: 'team.message_received', category: 'team', actor: { id: 'human', kind: 'human', role: 'human' },
        payload: { kind: 'message', recipient: 'store_manager', text: '请核对交付' } },
      { seq: 2, type: 'team.run_started', category: 'team', actor: host, payload: { message_seq: 1, role: 'store_manager', run_id: 'original' } },
      { seq: 3, type: 'team.objective_updated', category: 'team', actor: host, payload: { goal: '已交付目标的后续消息', run_id: 'original' } },
      { seq: 4, type: 'team.message_received', category: 'team', actor: { id: 'store_manager', kind: 'agent', role: 'store_manager' },
        payload: { kind: 'assignment', recipient: 'dev', text: '检查结果' } },
      { seq: 5, type: 'team.message_received', category: 'team', actor: { id: 'dev', kind: 'agent', role: 'dev' },
        payload: { kind: 'report', recipient: 'store_manager', text: '结果已核对', reply_to: 4 } },
      { seq: 6, type: 'team.objective_completed', category: 'team', actor: host, payload: { run_id: 'original' } },
      { seq: 7, type: 'msg.said', category: 'team', actor: { id: 'store_manager', kind: 'agent', role: 'store_manager' },
        payload: { phase: 'team', text: '已核对交付结果', run_id: 'original', delivery_seq: 6 } },
      { seq: 8, type: 'team.run_finished', category: 'team', actor: host,
        payload: { message_seq: 1, role: 'store_manager', status: 'waiting', run_id: 'original' } },
    ],
    activity: { state: 'completed', active: false, revision: 1 },
    execution_control: { state: 'running', seq: 0, reason: '' }, review: {},
  }
  const before = structuredClone(old)
  let refreshed = false
  let pendingDetails = 0
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetchMock = vi.fn((url: string, _init: RequestInit = {}): Promise<Response> => {
    if (url === '/api/task-summaries') return Promise.resolve(json({ tasks: [{ id: old.task.id, goal: old.task.goal,
      status: old.task.status, status_label: 'fixture', version: refreshed ? 20 : 8, latest_seq: refreshed ? 20 : 8, conversation_count: 1,
      activity: refreshed ? activity : old.activity, execution_control: refreshed ? control ?? old.execution_control : old.execution_control,
      team: old.task.team }] }))
    if (url === '/api/tasks/graph-sync') {
      if (!refreshed) return Promise.resolve(json(old))
      pendingDetails += 1
      return new Promise(() => {})
    }
    if (url === '/api/organization') return Promise.resolve(json([]))
    if (url === '/api/organization/profile') return Promise.resolve(json({ leader: { id: 'store_manager', name: 'Leader', role: 'leader' }, cabinet: [], workers: [] }))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const app = render(<App />)
  await waitFor(() => expect(document.querySelector('.chat-heading .status-pill')?.textContent).toBe('店长确认可交付'))
  expect(document.querySelector('.react-flow')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '任务脉络' }))
  const dialog = await screen.findByRole('dialog', { name: '任务脉络' })
  await waitFor(() => expect(dialog.querySelectorAll('.react-flow__edge')).toHaveLength(1))
  expect(dialog.querySelectorAll('.react-flow__node')).toHaveLength(3)
  expect(within(dialog).getByText('当前任务 · 店长确认可交付')).toBeTruthy()
  const previousViewport = dialog.querySelector('.react-flow__viewport')
  refreshed = true
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  await waitFor(() => {
    expect(pendingDetails).toBeGreaterThan(0)
    const headingStatus = document.querySelector('.chat-heading .status-pill')?.textContent
    expect(headingStatus).toBe(label)
    expect(within(dialog).getByText(`当前任务 · ${headingStatus}`)).toBeTruthy()
    expect(within(dialog).queryByText('当前任务 · 店长确认可交付')).toBeNull()
  })
  expect(dialog.querySelector('.react-flow__viewport')).toBe(previousViewport)
  const panel = within(dialog).getByRole('complementary', { name: '协作记录详情' })
  expect(within(panel).getByRole('heading', { name: '消息 #1' })).toBeTruthy()
  const processing = within(panel).getByRole('heading', { name: '最近一次处理记录', level: 4 }).closest('section')!
  expect(within(processing).getByText('本次处理结束，待后续信息')).toBeTruthy()
  expect(fetchMock.mock.calls.some(([, init]) => init.method && init.method !== 'GET')).toBe(false)
  expect(old).toEqual(before)
  app.unmount()
})
