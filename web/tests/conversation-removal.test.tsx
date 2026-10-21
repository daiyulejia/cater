import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'

const targetId = 'remove-task'
const otherId = 'keep-task'
const targetTitle = '待删除对话'
const otherTitle = '保留的对话'
const endpoint = `/api/tasks/${targetId}/remove`
const leader = { id: 'store_manager', name: '店长', role: 'leader' }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const title = (id: string) => id === targetId ? targetTitle : id === otherId ? otherTitle : `${id}对话`
const summary = (id: string, version = 2) => ({
  id, goal: title(id), status: 'pending', status_label: '待开始', latest_seq: version, version, conversation_count: 1,
})
const rows = () => [summary(otherId), summary(targetId)]
const ack = () => ({ id: targetId, removed: true, removed_seq: 10 })

function detail(id: string, version = 2): TaskDetail {
  return {
    task: { id, goal: title(id), status: 'pending', acceptance: [], messages: [], assignments: [], deliverables: [] },
    summary: summary(id, version), activity: { active: false, state: 'idle' }, review: {},
    events: [
      { seq: 1, type: 'task.created', category: 'task', actor: { id: 'human', role: 'human', kind: 'human' }, payload: { goal: title(id) } },
      { seq: version, type: 'msg.said', category: 'collaboration', actor: { id: leader.id, role: 'leader', kind: 'agent' }, payload: { text: `${id}消息 #${version}` } },
    ],
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

async function mount() {
  window.sessionStorage.clear()
  let list: unknown = { tasks: rows() }
  let handler: (url: string, init: RequestInit) => Response | Promise<Response> | undefined = () => undefined
  const unexpected: string[] = []
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/organization') return json([leader])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [] })
    if (url === '/api/task-summaries') return json(list)
    if (url === endpoint && init.method === 'POST') return json(ack())
    if (init.method === undefined || init.method === 'GET') {
      if (url === `/api/tasks/${targetId}`) return json(detail(targetId))
      if (url === `/api/tasks/${otherId}`) return json(detail(otherId))
    }
    unexpected.push(`${init.method ?? 'GET'} ${url}`)
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  render(<App />)
  const input = await screen.findByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  await screen.findByText(`${targetId}消息 #2`)
  const navigation = screen.getByRole('navigation', { name: '任务列表' })
  const row = within(navigation).getByRole('button', { name: new RegExp(`^${targetTitle}`) })
  expect(row.getAttribute('aria-current')).toBe('page')
  return {
    fetch, input, navigation, row, unexpected,
    posts: () => fetch.mock.calls.filter(([, init]) => init?.method === 'POST'),
    removals: () => fetch.mock.calls.filter(([url, init]) => url === endpoint && init?.method === 'POST'),
    override(next: typeof handler) { handler = next },
    setList(next: unknown) { list = next },
    async refresh() { await act(async () => { fireEvent(document, new Event('visibilitychange')) }) },
  }
}

function openDeletion() {
  fireEvent.click(screen.getByRole('button', { name: '更多任务操作' }))
  fireEvent.click(within(screen.getByRole('group', { name: '任务操作' })).getByRole('button', { name: '删除对话' }))
  return screen.getByRole('dialog', { name: '删除这段对话？' })
}

async function confirm(dialog: HTMLElement) {
  await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: '删除对话' })) })
}

function expectRemoved() {
  const navigation = within(screen.getByRole('navigation', { name: '任务列表' }))
  expect(navigation.queryByRole('button', { name: new RegExp(`^${targetTitle}`) })).toBeNull()
  expect(navigation.getByRole('button', { name: new RegExp(`^${otherTitle}`) })).toBeTruthy()
  expect(screen.queryByRole('textbox', { name: '向团队发送消息' })).toBeNull()
  expect(screen.getByRole('textbox', { name: '发起任务对话' })).toBeTruthy()
  expect(screen.queryByRole('heading', { name: targetTitle, level: 1 })).toBeNull()
}

test('更多任务操作打开 Radix 确认；保留对话不提交且保留输入，并说明文件与记录保留', async () => {
  const app = await mount()
  fireEvent.change(app.input, { target: { value: '尚未发送的输入' } })
  const dialog = openDeletion()
  expect(within(dialog).getByText(targetTitle)).toBeTruthy()
  const description = document.getElementById(dialog.getAttribute('aria-describedby')!)
  expect(description?.textContent).toContain('磁盘工程文件不会被删除')
  expect(description?.textContent).toContain('聊天、成果与审计记录仍保留在本机数据库中')
  expect(app.posts()).toHaveLength(0)
  fireEvent.click(within(dialog).getByRole('button', { name: '保留对话' }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '删除这段对话？' })).toBeNull())
  expect(app.row.isConnected).toBe(true)
  expect(app.input.value).toBe('尚未发送的输入')
  expect(app.posts()).toHaveLength(0)
  expect(app.unexpected).toEqual([])
})

test('确认只 POST request_id，合法回执后移除当前列表项并显示新任务入口', async () => {
  const app = await mount()
  const dialog = openDeletion()
  await confirm(dialog)
  await waitFor(() => expect(screen.queryByRole('dialog', { name: '删除这段对话？' })).toBeNull())
  expect(app.removals()).toHaveLength(1)
  expect(app.posts()).toHaveLength(1)
  expect(JSON.parse(String(app.removals()[0][1]?.body))).toEqual({ request_id: expect.stringMatching(uuid) })
  expectRemoved()
  expect(app.unexpected).toEqual([])
})

test.each(['http', 'network'])('删除 %s 失败不隐藏对话，关闭重开后重试沿用 request_id', async (failure) => {
  const app = await mount()
  let attempts = 0
  app.override((url, init) => {
    if (url !== endpoint || init.method !== 'POST') return
    if (++attempts > 1) return json(ack())
    return failure === 'http' ? new Response('删除暂时不可用', { status: 503 }) : Promise.reject(new TypeError('Failed to fetch'))
  })
  fireEvent.change(app.input, { target: { value: '失败时保留草稿' } })
  let dialog = openDeletion()
  await confirm(dialog)
  await waitFor(() => expect(within(dialog).getByRole('alert').textContent).toContain('提交结果未知'))
  expect(app.row.isConnected).toBe(true)
  expect(app.navigation.textContent).toContain(targetTitle)
  expect(app.input.value).toBe('失败时保留草稿')
  expect(app.removals()).toHaveLength(1)
  const original = app.removals()[0][1]?.body
  fireEvent.click(within(dialog).getByRole('button', { name: '保留对话' }))
  dialog = openDeletion()
  await confirm(dialog)
  expect(app.removals()).toHaveLength(2)
  expect(app.removals()[1][1]?.body).toBe(original)
  expectRemoved()
  expect(app.unexpected).toEqual([])
})

const invalidAcks = [
  null,
  {},
  { removed: true, removed_seq: 10 },
  { id: otherId, removed: true, removed_seq: 10 },
  { id: targetId, removed: false, removed_seq: 10 },
  { id: targetId, removed: 'true', removed_seq: 10 },
  { id: targetId, removed: true },
  { id: targetId, removed: true, removed_seq: 0 },
  { id: targetId, removed: true, removed_seq: -1 },
  { id: targetId, removed: true, removed_seq: 1.5 },
  { id: targetId, removed: true, removed_seq: '10' },
  { id: targetId, removed: true, removed_seq: Number.MAX_SAFE_INTEGER + 1 },
]

test.each(invalidAcks)('未知删除 ack 不隐藏或清除输入，重试保留原请求：%j', async (response) => {
  const app = await mount()
  let attempts = 0
  app.override((url, init) => url === endpoint && init.method === 'POST' ? json(++attempts === 1 ? response : ack()) : undefined)
  fireEvent.change(app.input, { target: { value: '不能丢失的草稿' } })
  const dialog = openDeletion()
  await confirm(dialog)
  await waitFor(() => expect(within(dialog).getByRole('alert').textContent).toBeTruthy())
  expect(app.row.isConnected).toBe(true)
  expect(app.navigation.textContent).toContain(targetTitle)
  expect(app.input.value).toBe('不能丢失的草稿')
  expect(app.removals()).toHaveLength(1)
  await confirm(dialog)
  expect(app.removals()).toHaveLength(2)
  expect(app.removals()[1][1]?.body).toBe(app.removals()[0][1]?.body)
  expectRemoved()
  expect(app.unexpected).toEqual([])
})

test('删除发送中确认和取消均禁用，连续确认不产生双发，回执前对话仍在', async () => {
  const app = await mount()
  const pending = deferred<Response>()
  app.override((url, init) => url === endpoint && init.method === 'POST' ? pending.promise : undefined)
  const dialog = openDeletion()
  const button = within(dialog).getByRole('button', { name: '删除对话' })
  await act(async () => { fireEvent.click(button); fireEvent.click(button) })
  expect(app.removals()).toHaveLength(1)
  const busy = within(dialog).getByRole('button', { name: '正在删除' }) as HTMLButtonElement
  const keep = within(dialog).getByRole('button', { name: '保留对话' }) as HTMLButtonElement
  expect(busy.disabled).toBe(true)
  expect(keep.disabled).toBe(true)
  fireEvent.click(busy)
  fireEvent.click(keep)
  fireEvent.keyDown(dialog, { key: 'Escape', code: 'Escape' })
  expect(dialog.isConnected).toBe(true)
  expect(app.row.isConnected).toBe(true)
  expect(app.removals()).toHaveLength(1)
  await act(async () => { pending.resolve(json(ack())) })
  expectRemoved()
  expect(app.unexpected).toEqual([])
})

test.each(['detail', 'command'])('删除后迟到的旧 %s 不会重新加入列表或抢回当前选择', async (kind) => {
  const app = await mount()
  const old = deferred<Response>()
  const path = `/api/tasks/${targetId}${kind === 'command' ? '/command' : ''}`
  app.override((url) => url === path ? old.promise : undefined)
  if (kind === 'command') {
    fireEvent.change(app.input, { target: { value: '删除前已经发送的消息' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送消息' })) })
  } else {
    await app.refresh()
  }
  const request = app.fetch.mock.calls.filter(([url]) => url === path).at(-1)
  expect(request).toBeTruthy()
  expect(request?.[1]?.signal?.aborted).toBe(false)
  await confirm(openDeletion())
  expectRemoved()
  fireEvent.change(screen.getByRole('textbox', { name: '发起任务对话' }), { target: { value: '新对话的输入' } })
  if (kind === 'command') expect(request?.[1]?.signal?.aborted).toBe(false)
  else expect(request?.[1]?.signal?.aborted).toBe(true)
  await act(async () => { old.resolve(json(detail(targetId, 100))) })
  expectRemoved()
  expect(screen.queryByText(`${targetId}消息 #100`)).toBeNull()
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('新对话的输入')
  expect(app.unexpected).toEqual([])
})

test('删除前发出的旧摘要仍可接纳其他新对话，但不能复活已删除 id', async () => {
  const app = await mount()
  const oldPoll = deferred<Response>()
  app.override((url) => url === '/api/task-summaries' ? oldPoll.promise : undefined)
  await app.refresh()
  const summaryRequest = app.fetch.mock.calls.filter(([url]) => url === '/api/task-summaries').at(-1)
  expect(summaryRequest?.[1]?.signal?.aborted).toBe(false)
  await confirm(openDeletion())
  expectRemoved()
  const stale = { tasks: [...rows(), summary('fresh-task', 100)] }
  app.setList(stale)
  app.override(() => undefined)
  await act(async () => { oldPoll.resolve(json(stale)) })
  expectRemoved()
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /^fresh-task对话/ })).toBeTruthy()
  await app.refresh()
  expectRemoved()
  expect(app.posts()).toHaveLength(1)
  expect(app.unexpected).toEqual([])
})

test('另一页面轮询 removed 移除当前选择，后续缺少墓碑的旧列表仍不能复活', async () => {
  const app = await mount()
  app.setList({ tasks: rows(), removed: [{ id: targetId, seq: 10 }] })
  await app.refresh()
  expectRemoved()
  expect(app.posts()).toHaveLength(0)
  app.setList({ tasks: rows() })
  await app.refresh()
  expectRemoved()
  fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: new RegExp(`^${otherTitle}`) }))
  await screen.findByText(`${otherId}消息 #2`)
  expect(screen.getByRole('heading', { name: otherTitle, level: 1 })).toBeTruthy()
  expect(app.posts()).toHaveLength(0)
  expect(app.unexpected).toEqual([])
})

const invalidRemoved = [
  null,
  { id: targetId, seq: 10 },
  'remove-task',
  [null],
  [{ seq: 10 }],
  [{ id: 123, seq: 10 }],
  [{ id: targetId }],
  [{ id: targetId, seq: 0 }],
  [{ id: targetId, seq: -1 }],
  [{ id: targetId, seq: 1.5 }],
  [{ id: targetId, seq: '10' }],
  [{ id: targetId, seq: Number.MAX_SAFE_INTEGER + 1 }],
  [{ id: targetId, seq: 10 }, { id: otherId, seq: 0 }],
]

test.each(invalidRemoved)('非法 removed 不误删、不清空选择或输入，也不部分应用墓碑：%j', async (removed) => {
  const app = await mount()
  fireEvent.change(app.input, { target: { value: '轮询失败也保留的输入' } })
  app.setList({ tasks: [], removed })
  await app.refresh()
  expect(app.row.isConnected).toBe(true)
  expect(app.row.getAttribute('aria-current')).toBe('page')
  expect(app.navigation.textContent).toContain(otherTitle)
  expect(app.input.value).toBe('轮询失败也保留的输入')
  expect(screen.getByRole('heading', { name: targetTitle, level: 1 })).toBeTruthy()
  app.setList({ tasks: rows() })
  await app.refresh()
  expect(app.navigation.textContent).toContain(targetTitle)
  expect(app.navigation.textContent).toContain(otherTitle)
  expect(app.input.value).toBe('轮询失败也保留的输入')
  expect(app.posts()).toHaveLength(0)
  expect(app.unexpected).toEqual([])
})
