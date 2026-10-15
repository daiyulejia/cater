import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { SUBMIT_TIMEOUT_MS } from '../src/request'
import { CREATION_RECOVERY_KEY, CREATION_RECOVERY_TTL } from '../src/creationRecovery'

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
  window.sessionStorage.clear()
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

const remount = async () => { cleanup(); await act(async () => { render(<App />) }) }
const restore = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '恢复待确认任务' }))) }

test.each(['fetch', 'body', '500'])('刷新恢复 %s 未确认创建，保留原参数和空白，不自动重发，成功后清除', async (stage) => {
  await mount(); await project('p1'); write('  刷新前的草稿  ')
  const pending = deferred<Response>()
  const response = json(detail('c'))
  vi.spyOn(response, 'json').mockReturnValue(new Promise(() => {}))
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST'
    ? stage === 'fetch' ? pending.promise : stage === 'body' ? response : new Response('回包失败', { status: 500 }) : undefined
  await send()
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toContain(body(0).request_id)
  await remount()
  expect(screen.getByRole('button', { name: '恢复待确认任务' })).toBeTruthy()
  await tick(SUBMIT_TIMEOUT_MS)
  expect(posts()).toHaveLength(1)
  await restore()
  expect(input().value).toBe('  刷新前的草稿  ')
  expect(posts()).toHaveLength(1)
  await select()
  expect(screen.queryByRole('textbox', { name: '发起任务对话' })).toBeNull()
  expect(screen.queryByText(/原草稿已恢复/)).toBeNull()
  begin()
  expect(input().value).toBe('  刷新前的草稿  ')
  handler = () => undefined
  await send()
  expect(posts()[1][1].body).toBe(posts()[0][1].body)
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  await act(async () => pending.resolve(json(detail('c'))))
  await remount()
  expect(screen.queryByRole('button', { name: '恢复待确认任务' })).toBeNull()
  begin(); expect(input().value).toBe('')
})

test.each(['text', 'project'])('恢复后修改 %s 再改回也不复用旧 ID，旧记录立即失效', async (field) => {
  await mount(); write('旧目标')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? new Response('失败', { status: 500 }) : undefined
  await send(); await remount(); await restore()
  if (field === 'text') { write('新目标'); write('旧目标') }
  else { await project('p1'); await project('') }
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  await send()
  expect(body(1).request_id).not.toBe(body(0).request_id)
})

test.each(['broken', 'expired', 'account', 'version'])('刷新遇到 %s 记录安全降级，不恢复或自动重发', async (failure) => {
  await mount(); write('旧账户草稿')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? new Response('失败', { status: 500 }) : undefined
  await send()
  const record = JSON.parse(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)!)
  if (failure === 'expired') record.attempt.createdAt -= CREATION_RECOVERY_TTL
  if (failure === 'version') record.version = 99
  if (failure === 'account') window.sessionStorage.setItem('cater.access_token', 'different-account')
  window.sessionStorage.setItem(CREATION_RECOVERY_KEY, failure === 'broken' ? '{' : JSON.stringify(record))
  await remount(); begin()
  expect(screen.queryByRole('button', { name: '恢复待确认任务' })).toBeNull()
  expect(input().value).toBe('')
  expect(screen.getByText(/待确认创建记录已失效/)).toBeTruthy()
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  expect(posts()).toHaveLength(1)
})

test.each(['getter', 'quota'])('sessionStorage %s 不可用时页面可用，内存重试仍使用原 ID', async (failure) => {
  if (failure === 'getter') vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new Error('disabled') })
  else vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  await mount(); write('仍然可发送')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? new Response('失败', { status: 500 }) : undefined
  await send(); await send()
  expect(posts()).toHaveLength(2)
  expect(body(1)).toEqual(body(0))
  expect(screen.getByText(/无法保存待确认创建记录/)).toBeTruthy()
})

test('账户在恢复入口出现后变化，不能恢复旧草稿或用旧 ID 发送', async () => {
  await mount(); write('私有草稿')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? new Response('失败', { status: 500 }) : undefined
  await send(); await remount()
  window.sessionStorage.setItem('cater.access_token', 'new-account')
  await restore(); begin()
  expect(input().value).toBe('')
  write('新草稿'); await send()
  expect(posts()).toHaveLength(1)
  expect(screen.getByText(/账户已变化/)).toBeTruthy()
})

test('放弃恢复后清除记录；刷新或按发送均不会静默重试旧任务', async () => {
  await mount(); write('放弃的草稿')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? new Response('失败', { status: 500 }) : undefined
  await send(); await remount(); begin()
  expect(screen.getByRole('button', { name: '发送新任务' }).getAttribute('aria-busy')).toBe('false')
  write('另一项任务'); await send()
  expect(posts()).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: '放弃恢复' }))
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  await send()
  expect(body(1).request_id).not.toBe(body(0).request_id)
})

test('响应结构不完整不能当作确认成功，刷新仍可恢复', async () => {
  await mount(); write('不完整的回包')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? json({ task: { id: 'c' } }) : undefined
  await send()
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).not.toBeNull()
  await remount(); await restore()
  expect(input().value).toBe('不完整的回包')
})

test('切换任务后迟到成功只清理对应新建记录，保留当前任务草稿和导航', async () => {
  await mount(); write('正在创建')
  const pending = deferred<Response>()
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? pending.promise : undefined
  await send(); await select()
  const direction = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(direction, { target: { value: 'a 的独立草稿' } })
  await act(async () => pending.resolve(json(detail('c'))))
  expect(screen.getByRole('heading', { name: 'a任务', level: 1 })).toBeTruthy()
  expect(direction.value).toBe('a 的独立草稿')
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  begin(); expect(input().value).toBe('')
})

test('未卸载页面的记录过期后先提示核对，不直接用新 ID 重发', async () => {
  await mount(); write('长时间待确认')
  handler = (url, init) => url === '/api/tasks' && init.method === 'POST' ? new Response('失败', { status: 500 }) : undefined
  await send()
  vi.setSystemTime(Date.now() + CREATION_RECOVERY_TTL)
  await send()
  expect(posts()).toHaveLength(1)
  expect(screen.getByText(/待确认创建记录已过期/)).toBeTruthy()
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
})
