import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import type { TeamState } from '../src/team'

type Entry = TaskDetail['events'][number]
const event = (seq: number, type: string, payload: Record<string, unknown> = {}, id = 'team', kind = 'system'): Entry => ({
  seq, ts: new Date().toISOString(), type, payload, category: 'lifecycle', actor: { id, role: id, kind },
})
const leader = { id: 'store_manager', name: '店长', role: 'leader' }
const worker = { id: 'dev', name: '米寇', role: 'dev' }
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const progress = () => screen.getByRole('region', { name: '当前工作进展' })

function payload(done: boolean): TaskDetail {
  const team: TeamState = { enabled: true, objective: '网页工作', status: done ? 'done' : 'running',
    revision: done ? 3 : 1, pending_count: 0, runs: [], assignments: [], checkpoint: '' }
  return { task: { id: 'progress-task', goal: '网页工作', status: team.status, team, acceptance: [], messages: [], assignments: [], deliverables: [] },
    team, events: [event(1, 'task.created', { goal: '网页工作' }, 'human', 'human'), event(2, 'team.started'),
      ...(done ? [event(3, 'team.objective_completed', { summary: '旧版本已交付' })] : [])],
    activity: { active: false, state: 'completed' }, review: {} }
}

async function mount(initial: TaskDetail) {
  window.sessionStorage.clear()
  let current = initial, offline = false
  let resolvePost: ((response: Response) => void) | undefined
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    if (url === '/api/organization') return json([leader, worker])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [worker] })
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'progress-task', goal: '网页工作', status: current.task.status,
      status_label: '网页工作', version: current.events.at(-1)?.seq ?? 1, latest_seq: current.events.at(-1)?.seq ?? 1, conversation_count: 1, team: current.team }] })
    if (url === '/api/tasks/progress-task/command' && init.method === 'POST') return new Promise<Response>((resolve) => { resolvePost = resolve })
    if (url === '/api/tasks/progress-task/control' && init.method === 'POST') {
      current = { ...current, execution_control: { state: 'paused', seq: 100, reason: '顾客暂停' } }
      return json(current)
    }
    if (url === '/api/tasks/progress-task') {
      if (offline) return Promise.reject(new Error('offline'))
      return json(current)
    }
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  render(<App />)
  await screen.findByRole('textbox', { name: '向团队发送消息' })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  return {
    fetch,
    async accept(next: TaskDetail) {
      current = next
      await act(async () => { resolvePost?.(json(current)) })
    },
    async update(next: TaskDetail) {
      current = next
      await act(async () => { fireEvent(document, new Event('visibilitychange')) })
    },
    async disconnect() {
      offline = true
      await act(async () => { fireEvent(document, new Event('visibilitychange')) })
    },
  }
}

test('发送、接收、处理与回答在同一进展区更新，保留后续草稿，不把旧交付当作新答复', async () => {
  const initial = payload(true)
  const app = await mount(initial)
  await screen.findByText('旧版本已交付')
  const input = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '这个网站怎么打开？' } })
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  const region = await screen.findByRole('region', { name: '当前工作进展' })
  expect(screen.getByLabelText('聊天记录').contains(region)).toBe(false)
  expect(region.textContent).toContain('正在发送')
  fireEvent.change(input, { target: { value: '下一条尚未发送的补充' } })
  const source = event(4, 'team.message_received', { text: '这个网站怎么打开？', recipient: 'store_manager', kind: 'message' }, 'human', 'human')
  const received = { ...initial, team: { ...initial.team!, pending_count: 1 }, events: [...initial.events, source], activity: { active: true, state: 'running' as const } }
  await app.accept(received)
  await waitFor(() => expect(progress().textContent).not.toContain('正在发送'))
  expect(progress()).toBe(region)
  expect(input.value).toBe('下一条尚未发送的补充')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  expect(within(screen.getByLabelText('聊天记录')).getAllByText('这个网站怎么打开？')).toHaveLength(1)
  const start = event(5, 'team.run_started', { message_seq: 4, role: 'store_manager', run_id: 'followup', revision: 3 })
  const running = { ...received, team: { ...received.team, pending_count: 0 }, events: [...received.events, start] }
  await app.update(running)
  await waitFor(() => expect(progress().textContent).toContain('店长正在处理'))
  expect(progress()).toBe(region)
  const answer = event(6, 'msg.said', { text: '双击 index.html 就可以打开。', phase: 'team', audience: 'customer', recipient: 'human', reply_to: 4, run_id: 'followup' }, 'store_manager', 'agent')
  const end = event(7, 'team.run_finished', { message_seq: 4, role: 'store_manager', run_id: 'followup', status: 'completed' })
  await app.update({ ...running, events: [...running.events, answer, end], activity: { active: false, state: 'completed' } })
  await screen.findByText('双击 index.html 就可以打开。')
  await waitFor(() => expect(screen.queryByRole('region', { name: '当前工作进展' })).toBeNull())
  expect(app.fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
})

test('工作期间可继续聊天，查看与刷新不提交，暂停使用既有控制接口，断连撤销忙碌反馈', async () => {
  const initial = payload(false)
  initial.events.push(event(3, 'team.message_received', { text: '检查网页', recipient: 'dev', kind: 'assignment', revision: 1 }, 'store_manager', 'agent'),
    event(4, 'team.run_started', { message_seq: 3, role: 'dev', run_id: 'worker', revision: 1 }))
  initial.activity = { active: true, state: 'running' }
  const app = await mount(initial)
  await screen.findByRole('region', { name: '当前工作进展' })
  const input = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '补充：按钮用中文' } })
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(within(progress()).getByRole('button', { name: '查看过程' }))
  await screen.findByRole('dialog', { name: '成果与过程' })
  fireEvent.click(screen.getByRole('button', { name: '关闭成果面板' }))
  const reads = app.fetch.mock.calls.filter(([url]) => url === '/api/tasks/progress-task').length
  fireEvent.click(within(progress()).getByRole('button', { name: '刷新状态' }))
  await waitFor(() => expect(app.fetch.mock.calls.filter(([url]) => url === '/api/tasks/progress-task').length).toBeGreaterThan(reads))
  expect(app.fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0)
  fireEvent.click(within(progress()).getByRole('button', { name: '暂停后续执行' }))
  await waitFor(() => expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已暂停后续执行'))
  expect(progress().textContent).not.toContain('在途调用仍可能返回')
  expect(screen.getAllByText(/已暂停后续执行；在途调用仍可能返回/)).toHaveLength(1)
  expect(input.value).toBe('补充：按钮用中文')
  const posts = app.fetch.mock.calls.filter(([, init]) => init?.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(posts[0][0]).toBe('/api/tasks/progress-task/control')
  expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({ action: 'pause' })
  await app.update({ ...initial, execution_control: { state: 'paused', seq: 100, reason: '顾客暂停' },
    activity: { active: false, state: 'completed' } })
  await waitFor(() => expect(screen.queryByRole('region', { name: '当前工作进展' })).toBeNull())
  await app.disconnect()
  await waitFor(() => expect(progress().textContent).toMatch(/待确认|无法|断开/))
  expect(within(progress()).queryByLabelText('有近期执行反馈')).toBeNull()
  expect(input.value).toBe('补充：按钮用中文')
})
