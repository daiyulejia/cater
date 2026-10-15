import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { SUBMIT_TIMEOUT_MS } from '../src/request'

const json = (value: unknown) => new Response(JSON.stringify(value))
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const summary = (id: string) => ({ id, goal: `${id}任务`, status: 'pending', status_label: '待开始', conversation_count: 1, latest_seq: 1, version: 1 })
function detail(id: string) {
  return { task: { id, goal: `${id}任务`, acceptance: [], status: 'pending', messages: [], assignments: [], deliverables: [] },
    events: [{ seq: 1, type: 'task.created', actor: { id: 'human' }, payload: { goal: `${id}任务` } }],
    summary: summary(id), activity: { active: false, state: 'idle' }, review: {} }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
let handler: (url: string, init: RequestInit) => Response | Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>
beforeEach(() => {
  vi.useFakeTimers()
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/task-summaries') return json({ tasks: [summary('a'), summary('b')] })
    if (url === '/api/organization') return json([])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] })
    if (url === '/api/projects') return json([{ id: 'p1', name: '项目一' }, { id: 'p2', name: '项目二' }])
    if (url.startsWith('/api/rules')) return json({ rules: [] })
    if (url === '/api/tasks' && init.method === 'POST') return json(detail('c'))
    if (/^\/api\/tasks\/[abc]$/.test(url)) return json(detail(url.split('/')[3]))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const tick = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const begin = () => fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
const input = () => screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement
const write = (text: string) => fireEvent.change(input(), { target: { value: text } })
const send = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送新任务' }))) }
const posts = () => fetchMock.mock.calls.filter(([url, init]) => url === '/api/tasks' && init.method === 'POST')
const body = (index: number) => JSON.parse(posts()[index][1].body as string)
const mount = async () => { await act(async () => { render(<App />) }); begin() }
const select = async () => { await act(async () => fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /^a任务/ }))) }
const project = async (id: string) => {
  fireEvent.click(screen.getByTitle('项目与店规'))
  await tick(0)
  await act(async () => fireEvent.change(screen.getByLabelText('所属项目'), { target: { value: id } }))
  fireEvent.click(screen.getByRole('button', { name: '关闭店规' }))
}

test.each(['fetch', 'body'])('新建任务 %s 超时释放发送锁，手动重试保留完整 payload，迟到响应不抢导航', async (stage) => {
  await mount(); await project('p1'); write('  新任务目标  ')
  const pending = deferred<Response>()
  const pendingBody = deferred<unknown>()
  const response = json(detail('c'))
  vi.spyOn(response, 'json').mockReturnValue(pendingBody.promise)
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? (stage === 'fetch' ? pending.promise : response) : undefined
  await send()
  fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true })
  expect(posts()).toHaveLength(1)
  expect(body(0)).toEqual({ goal: '新任务目标', project_id: 'p1', request_id: expect.stringMatching(uuid), run: true, background: true })
  await tick(SUBMIT_TIMEOUT_MS)
  expect(posts()[0][1].signal.aborted).toBe(true)
  expect(screen.getByRole('alert').textContent).toContain('请求超时，提交结果未知')
  expect((screen.getByRole('button', { name: '发送新任务' }) as HTMLButtonElement).disabled).toBe(false)
  expect(input().value).toBe('  新任务目标  ')
  await tick(SUBMIT_TIMEOUT_MS)
  expect(posts()).toHaveLength(1)
  await select()
  expect(screen.queryByText(/提交结果未知/)).toBeNull()
  begin()
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  const retry = deferred<Response>()
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? retry.promise : undefined
  await send()
  expect(posts()[1][1].body).toBe(posts()[0][1].body)
  await act(async () => { pending.resolve(json(detail('c'))); pendingBody.resolve(detail('c')) })
  expect(input().value).toBe('  新任务目标  ')
  expect((screen.getByRole('button', { name: '发送新任务' }) as HTMLButtonElement).disabled).toBe(true)
  expect(posts()[1][1].signal.aborted).toBe(false)
  await act(async () => retry.resolve(json(detail('c'))))
  expect(screen.getByRole('heading', { name: 'c任务', level: 1 })).toBeTruthy()
  begin(); expect(input().value).toBe('')
})

test('断网、HTTP错误和409均保留新建 ID；未选择项目时省略 project_id', async () => {
  await mount(); write('同一任务')
  let attempts = 0
  handler = (url, init) => {
    if (url !== '/api/tasks' || init.method !== 'POST') return
    attempts += 1
    if (attempts === 1) return Promise.reject(new TypeError('Failed to fetch'))
    return new Response(attempts === 2 ? '暂时不可用' : '同一请求不能更换创建参数', { status: attempts === 2 ? 503 : 409 })
  }
  await send()
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  await send()
  expect(screen.getByRole('alert').textContent).toContain('暂时不可用')
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  await send(); expect(screen.getByRole('alert').textContent).toBe('同一请求不能更换创建参数')
  await tick(SUBMIT_TIMEOUT_MS)
  expect(posts()).toHaveLength(3)
  expect(body(0)).toEqual({ goal: '同一任务', request_id: expect.stringMatching(uuid), run: true, background: true })
  expect(posts().every((post) => post[1].body === posts()[0][1].body)).toBe(true)
  expect(input().value).toBe('同一任务')
})

test('服务端已新建但返回500时保留原请求，手动重试取得同一任务且不自动重发', async () => {
  await mount(); write('可能已经创建的任务')
  const created = new Map<string, ReturnType<typeof detail>>()
  handler = (url, init) => {
    if (url !== '/api/tasks' || init.method !== 'POST') return
    const id: string = JSON.parse(init.body as string).request_id
    if (created.has(id)) return json(created.get(id))
    created.set(id, detail('c'))
    return new Response('创建后回包失败', { status: 500 })
  }
  await send()
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  expect(input().value).toBe('可能已经创建的任务')
  await tick(SUBMIT_TIMEOUT_MS)
  expect(posts()).toHaveLength(1)
  await send()
  expect(posts()[1][1].body).toBe(posts()[0][1].body)
  expect(created.size).toBe(1)
  expect(screen.getByRole('heading', { name: 'c任务', level: 1 })).toBeTruthy()
})

test('编辑目标或项目分配新 ID，同内容成功后再次新建也使用新 ID，兼容 HTTP 环境', async () => {
  vi.stubGlobal('crypto', { getRandomValues: crypto.getRandomValues.bind(crypto) })
  await mount(); write('相同目标')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? new Response('暂时不可用', { status: 503 }) : undefined
  await send()
  write('编辑过的目标'); write('相同目标')
  await send()
  expect(body(1).request_id).not.toBe(body(0).request_id)
  await project('p1'); await send()
  await project('p2'); await send()
  expect(body(2).project_id).toBe('p1')
  expect(body(3).project_id).toBe('p2')
  expect(new Set(posts().map((_, index) => body(index).request_id)).size).toBe(4)
  expect(posts().every((_, index) => uuid.test(body(index).request_id))).toBe(true)
  handler = () => undefined
  await send()
  expect(body(4)).toEqual(body(3))
  begin(); write('相同目标'); await send()
  expect(body(5).request_id).not.toBe(body(4).request_id)
})

test('发送时编辑后恢复原文，迟到成功不清除新草稿，也不切走当前页面', async () => {
  await mount(); write('原始目标')
  const pending = deferred<Response>()
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? pending.promise : undefined
  await send()
  write('修改目标'); write('原始目标')
  await act(async () => pending.resolve(json(detail('c'))))
  expect(input().value).toBe('原始目标')
  await send()
  expect(body(1).request_id).not.toBe(body(0).request_id)
})
