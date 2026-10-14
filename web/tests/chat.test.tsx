import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'

function detail(id: string, seq = 2) {
  const goal = `${id}任务`
  return {
    task: { id, goal, acceptance: [], status: 'pending', messages: [], assignments: [], deliverables: [] },
    events: [
      { seq: 1, type: 'task.created', category: 'task', actor: { id: 'human' }, payload: { goal } },
      { seq, type: 'msg.said', category: 'collaboration', actor: { id: 'dev' }, payload: { text: `${id}任务专属消息 #${seq}` } },
    ],
    activity: { active: false, state: 'idle' }, review: {},
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
let handler: (url: string, init: RequestInit) => Promise<Response> | Response | undefined
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/tasks') return Promise.resolve(json([detail('甲').task, detail('乙').task]))
    if (url === '/api/organization') return Promise.resolve(json([]))
    if (url === '/api/organization/profile') return Promise.resolve(json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [], mode: 'local_demo' }))
    if (url.startsWith('/api/tasks/')) return Promise.resolve(json(detail(url.split('/')[3])))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const select = (id: string) => fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${id}任务`) }))
const input = () => screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
const write = (text: string) => fireEvent.change(input(), { target: { value: text } })
const ready = (id: string) => screen.findByText(`${id}任务专属消息 #2`)

test('聊天内点名保留草稿并聚焦，既不发送也不污染其他任务', async () => {
  render(<App />); await ready('乙')
  write('补充一下')
  fireEvent.click(screen.getByRole('button', { name: '点名 店长' }))
  expect(input().value).toBe('补充一下 @店长 ')
  expect(document.activeElement).toBe(input())
  expect(fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(0)
  select('甲'); await ready('甲')
  expect(input().value).toBe('')
  select('乙'); await ready('乙')
  expect(input().value).toBe('补充一下 @店长 ')
  expect(screen.getByRole('button', { name: '全部' }).getAttribute('aria-pressed')).toBe('true')
})

test('任务详情未到达时不展示旧消息，也不能向新任务发送', async () => {
  render(<App />)
  await ready('乙')
  const pending = deferred<Response>()
  handler = (url) => url === '/api/tasks/甲' ? pending.promise : undefined
  select('甲')
  expect(screen.getByRole('heading', { name: '甲任务', exact: true })).toBeTruthy()
  expect(screen.queryByText('乙任务专属消息 #2')).toBeNull()
  expect(screen.getByText('正在接上这段对话')).toBeTruthy()
  write('这是甲的补充')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(true)
  await act(async () => pending.resolve(json(detail('甲'))))
  expect(input().value).toBe('这是甲的补充')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
})

test('每个任务和新任务都保留自己的内存草稿', async () => {
  render(<App />)
  await ready('乙')
  write('乙的草稿')
  select('甲'); await ready('甲'); write('甲的草稿')
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  const newInput = screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement
  fireEvent.change(newInput, { target: { value: '新的一件事' } })
  select('乙'); await ready('乙'); expect(input().value).toBe('乙的草稿')
  select('甲'); await ready('甲'); expect(input().value).toBe('甲的草稿')
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('新的一件事')
})

test('慢发送响应不覆盖切换后的任务或清空另一份草稿', async () => {
  render(<App />); await ready('乙')
  const pending = deferred<Response>()
  handler = (url, init) => url === '/api/tasks/乙/command' && init.method === 'POST' ? pending.promise : undefined
  write('乙的补充'); fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  select('甲'); await ready('甲'); write('甲的草稿')
  await act(async () => pending.resolve(json(detail('乙', 9))))
  expect(screen.getByRole('heading', { name: '甲任务', exact: true })).toBeTruthy()
  expect(screen.queryByText('乙任务专属消息 #9')).toBeNull()
  expect(input().value).toBe('甲的草稿')
})

test('发送期间的新输入保留，连续快捷键只发送一次', async () => {
  render(<App />); await ready('乙')
  const pending = deferred<Response>()
  handler = (url, init) => url === '/api/tasks/乙/command' && init.method === 'POST' ? pending.promise : undefined
  write('最初方向')
  fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true })
  fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true })
  write('发送后又想到的新方向')
  await act(async () => pending.resolve(json(detail('乙', 9))))
  expect(input().value).toBe('发送后又想到的新方向')
  expect(fetchMock.mock.calls.filter(([url, init]) => url === '/api/tasks/乙/command' && init.method === 'POST')).toHaveLength(1)
})

test('新任务的慢响应尊重用户后续导航，并保留后续新任务草稿', async () => {
  render(<App />); await ready('乙')
  const pending = deferred<Response>()
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? pending.promise : undefined
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  const newInput = screen.getByRole('textbox', { name: '发起任务对话' })
  fireEvent.change(newInput, { target: { value: '原始新任务' } })
  fireEvent.click(screen.getByRole('button', { name: '发送新任务' }))
  fireEvent.change(newInput, { target: { value: '接下来想做的另一件事' } })
  select('甲'); await ready('甲')
  await act(async () => pending.resolve(json(detail('丙'))))
  expect(screen.getByRole('heading', { name: '甲任务', exact: true })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('接下来想做的另一件事')
})

test('旧的详情请求不能覆盖较新的发送结果', async () => {
  render(<App />); await ready('乙')
  const old = deferred<Response>()
  let reads = 0
  handler = (url, init) => {
    if (url === '/api/tasks/乙/command' && init.method === 'POST') return Promise.resolve(json(detail('乙', 9)))
    if (url === '/api/tasks/乙' && ++reads === 1) return old.promise
  }
  fireEvent.click(screen.getByRole('button', { name: '新建任务' })); select('乙')
  write('新方向'); fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await screen.findByText('乙任务专属消息 #9')
  await act(async () => old.resolve(json(detail('乙', 2))))
  expect(screen.queryByText('乙任务专属消息 #2')).toBeNull()
  expect(screen.getByText('乙任务专属消息 #9')).toBeTruthy()
})

test('空闲对话继续刷新，慢请求不会叠加', async () => {
  vi.useFakeTimers()
  const pending = deferred<Response>()
  let reads = 0
  handler = (url) => url === '/api/tasks/乙' ? (++reads === 1 ? pending.promise : Promise.resolve(json(detail('乙', 7)))) : undefined
  await act(async () => { render(<App />) })
  await act(async () => { await vi.advanceTimersByTimeAsync(16000) })
  expect(reads).toBe(1)
  await act(async () => pending.resolve(json(detail('乙'))))
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  expect(reads).toBe(2)
  expect(screen.getByText('乙任务专属消息 #7')).toBeTruthy()
})

test('阅读历史时保留位置，点击新消息入口后恢复底部跟随', async () => {
  vi.useFakeTimers()
  await act(async () => { render(<App />) })
  const stream = screen.getByLabelText('聊天记录')
  Object.defineProperties(stream, { clientHeight: { value: 200 }, scrollHeight: { value: 1000 } })
  stream.scrollTop = 100
  fireEvent.scroll(stream)
  handler = (url) => url === '/api/tasks/乙' ? Promise.resolve(json(detail('乙', 7))) : undefined
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  expect(stream.scrollTop).toBe(100)
  fireEvent.click(screen.getByRole('button', { name: '查看新消息 · 1' }))
  expect(stream.scrollTop).toBe(1000)
  expect(screen.queryByRole('button', { name: /查看新消息/ })).toBeNull()
})

test('发送错误留在对应任务，切换后的对话不显示另一项任务的错误', async () => {
  render(<App />); await ready('乙')
  const pending = deferred<Response>()
  handler = (url, init) => url === '/api/tasks/乙/command' && init.method === 'POST' ? pending.promise : undefined
  write('乙的补充'); fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  select('甲'); await ready('甲')
  await act(async () => pending.resolve(new Response('乙任务发送失败', { status: 500 })))
  expect(screen.queryByText('乙任务发送失败')).toBeNull()
  select('乙'); await ready('乙')
  expect(screen.getByText('乙任务发送失败')).toBeTruthy()
  expect(input().value).toBe('乙的补充')
})

test('后台页面暂停刷新，重新显示立即读取', async () => {
  vi.useFakeTimers()
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  await act(async () => { render(<App />) })
  const reads = () => fetchMock.mock.calls.filter(([url]) => url === '/api/tasks/乙').length
  expect(reads()).toBe(1)
  visibility.mockReturnValue('hidden')
  fireEvent(document, new Event('visibilitychange'))
  await act(async () => { await vi.advanceTimersByTimeAsync(16000) })
  expect(reads()).toBe(1)
  visibility.mockReturnValue('visible')
  await act(async () => { fireEvent(document, new Event('visibilitychange')) })
  expect(reads()).toBe(2)
})
