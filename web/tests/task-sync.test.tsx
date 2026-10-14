import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
window.sessionStorage.setItem('cater.access_token', 'fixture-token')
const { default: App } = await import('../src/App')
window.sessionStorage.removeItem('cater.access_token')

function summary(id: string, version = 1, status = 'pending', count = 1) {
  return { id, goal: `${id}任务`, project_id: null, status,
    status_label: status === 'done' ? '已交付' : '待开始', conversation_count: count, latest_seq: version, version,
    activity: { active: false, state: 'idle', revision: 0 } }
}

function detail(id: string, version = 1) {
  return { task: { id, goal: `${id}任务`, acceptance: [], status: 'pending', messages: [], assignments: [], deliverables: [] },
    events: [{ seq: version, type: 'task.created', actor: { id: 'human' }, payload: { goal: `${id}聊天 #${version}` } }],
    summary: summary(id, version), activity: { active: false, state: 'idle' }, review: {} }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

const json = (body: unknown, etag?: string) => new Response(JSON.stringify(body), {
  headers: { 'Content-Type': 'application/json', ...(etag ? { ETag: etag } : {}) },
})
let rows = [summary('甲'), summary('乙')]
let handler: (url: string, init: RequestInit) => Response | Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  rows = [summary('甲'), summary('乙')]
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/task-summaries') return json({ tasks: rows }, '"initial"')
    if (url === '/api/organization') return json([])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] })
    if (url.startsWith('/api/tasks/')) return json(detail(url.split('/')[3]))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const mount = async () => { await act(async () => { render(<App />) }) }
const tick = async (ms = 4000) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const nav = () => within(screen.getByRole('navigation', { name: '任务列表' }))
const select = (id: string) => fireEvent.click(nav().getByRole('button', { name: new RegExp(`^${id}任务`) }))
const input = () => screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
const write = (value: string) => fireEvent.change(input(), { target: { value } })
const summaryCalls = () => fetchMock.mock.calls.filter(([url]) => url === '/api/task-summaries')

test('另一窗口新增、改标题和撤销完成更新列表，保留选择、草稿、筛选及聊天滚动', async () => {
  await mount()
  write('乙的未发送内容')
  const stream = screen.getByLabelText('聊天记录')
  Object.defineProperties(stream, { clientHeight: { value: 200 }, scrollHeight: { value: 1000 } })
  stream.scrollTop = 100; fireEvent.scroll(stream)
  rows = [summary('甲', 5, 'done', 7), summary('乙', 5), summary('丙', 5)]
  await tick()
  expect(nav().getByRole('button', { name: /甲任务.*7 条对话.*已交付/ })).toBeTruthy()
  expect(nav().getByRole('button', { name: /丙任务/ })).toBeTruthy()
  expect(nav().getByRole('button', { name: /乙任务/ }).getAttribute('aria-current')).toBe('page')
  expect(input().value).toBe('乙的未发送内容')
  expect(screen.getByText('乙聊天 #1')).toBeTruthy()
  expect(stream.scrollTop).toBe(100)
  fireEvent.click(within(screen.getByRole('group', { name: '任务状态筛选' })).getByRole('button', { name: '已完成' }))
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '甲' } })
  rows = [{ ...summary('甲', 6, 'running', 9), goal: '甲的新要求', status_label: '需要重新核对' }, summary('乙', 6), summary('丙', 6)]
  await tick()
  expect(nav().queryByRole('button', { name: /甲/ })).toBeNull()
  expect(input().value).toBe('乙的未发送内容')
  fireEvent.click(within(screen.getByRole('group', { name: '任务状态筛选' })).getByRole('button', { name: '全部' }))
  expect(nav().getByRole('button', { name: /甲的新要求.*9 条对话.*需要重新核对/ })).toBeTruthy()
  expect((screen.getByRole('searchbox') as HTMLInputElement).value).toBe('甲')
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/tasks')).toBe(false)
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/tasks/甲')).toBe(false)
})

test('304 不读取正文，转发 ETag，仍由选中详情独立读取聊天', async () => {
  await mount(); write('保留草稿')
  const unchanged = new Response(null, { status: 304, headers: { ETag: '"initial"' } })
  const parse = vi.spyOn(unchanged, 'json')
  handler = (url) => url === '/api/task-summaries' ? unchanged : undefined
  await tick()
  expect(new Headers(summaryCalls()[1][1].headers).get('If-None-Match')).toBe('"initial"')
  expect(parse).not.toHaveBeenCalled()
  expect(input().value).toBe('保留草稿')
  expect(fetchMock.mock.calls.filter(([url]) => url === '/api/tasks/乙')).toHaveLength(2)
})

test('迟到初始列表不能离开用户已输入的新对话', async () => {
  const initial = deferred<Response>()
  handler = (url) => url === '/api/task-summaries' ? initial.promise : undefined
  await mount()
  const composer = screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement
  fireEvent.change(composer, { target: { value: '尚未发送的新任务' } })
  await act(async () => initial.resolve(json({ tasks: rows }, '"first"')))
  expect(composer.value).toBe('尚未发送的新任务')
  expect(screen.queryByRole('textbox', { name: '向团队发送消息' })).toBeNull()
  expect(nav().getAllByRole('button')).toHaveLength(2)
})

test('新对话已有草稿时远端新任务只进入列表；切任务仍恢复各自草稿', async () => {
  await mount(); write('乙草稿')
  await act(async () => select('甲')); write('甲草稿')
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  fireEvent.change(screen.getByRole('textbox', { name: '发起任务对话' }), { target: { value: '新草稿' } })
  rows = [...rows, summary('丙', 7)]
  await tick()
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('新草稿')
  await act(async () => select('乙')); expect(input().value).toBe('乙草稿')
  await act(async () => select('甲')); expect(input().value).toBe('甲草稿')
})

test('旧摘要不能覆盖新发送详情的标题、状态与计数', async () => {
  await mount()
  const old = deferred<Response>()
  handler = (url) => {
    if (url === '/api/task-summaries') return old.promise
    if (url.endsWith('/command')) {
      const next = detail('乙', 9)
      return json({ ...next, task: { ...next.task, goal: '乙最新要求' },
        summary: { ...summary('乙', 9, 'running', 4), goal: '乙最新要求', status_label: '需要重新核对' } })
    }
  }
  await tick(); write('补充要求')
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送消息' })))
  await act(async () => old.resolve(json({ tasks: [summary('甲', 2), summary('乙', 2), summary('丙', 2)] }, '"old"')))
  expect(nav().getByRole('button', { name: /乙最新要求.*4 条对话.*需要重新核对/ })).toBeTruthy()
  expect(nav().getByRole('button', { name: /丙任务/ })).toBeTruthy()
  expect(screen.getByText('乙聊天 #9')).toBeTruthy()
})

test('相同事件版本的旧详情不覆盖较晚发送后的活动状态', async () => {
  await mount()
  const old = deferred<Response>()
  handler = (url) => {
    if (url === '/api/tasks/乙') return old.promise
    if (url.endsWith('/command')) {
      const next = detail('乙')
      return json({ ...next, activity: { active: true, state: 'running' },
        summary: { ...next.summary, status_label: '正在协作', activity: { active: true, state: 'running', revision: 2 } } })
    }
  }
  await tick(); write('继续')
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送消息' })))
  await act(async () => old.resolve(json(detail('乙'))))
  expect(nav().getByRole('button', { name: /乙任务.*正在协作/ })).toBeTruthy()
  expect(screen.getAllByText('正在协作').length).toBeGreaterThan(0)
})

test('租约到期摘要不被同事件版本但较早计算的详情覆盖', async () => {
  await mount()
  const old = deferred<Response>()
  handler = (url) => {
    if (url === '/api/task-summaries') return json({ tasks: [summary('甲'), {
      ...summary('乙'), evaluated_at: 111, status_label: '结果待核实',
    }] }, '"expired"')
    if (url === '/api/tasks/乙') return old.promise
  }
  await tick()
  const previous = detail('乙')
  await act(async () => old.resolve(json({ ...previous, summary: { ...previous.summary, evaluated_at: 105 } })))
  expect(nav().getByRole('button', { name: /乙任务.*结果待核实/ })).toBeTruthy()
})

test('发送较早发起但返回更高活动版本时，仍接受较新的服务端状态', async () => {
  await mount()
  const command = deferred<Response>()
  handler = (url) => url.endsWith('/command') ? command.promise : undefined
  write('继续')
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await tick()
  const next = detail('乙')
  await act(async () => command.resolve(json({ ...next, activity: { active: true, state: 'running' },
    summary: { ...next.summary, status_label: '正在协作', activity: { active: true, state: 'running', revision: 2 } } })))
  expect(nav().getByRole('button', { name: /乙任务.*正在协作/ })).toBeTruthy()
  expect(screen.getAllByText('正在协作').length).toBeGreaterThan(0)
})

test('初始摘要到达前新建结果已选中，旧列表不能覆盖本窗口新任务', async () => {
  const initial = deferred<Response>()
  handler = (url, init) => {
    if (url === '/api/task-summaries') return initial.promise
    if (url === '/api/tasks' && init.method === 'POST') return json(detail('丙', 9))
  }
  await mount()
  fireEvent.change(screen.getByRole('textbox', { name: '发起任务对话' }), { target: { value: '新任务要求' } })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送新任务' })))
  await act(async () => initial.resolve(json({ tasks: rows }, '"old-initial"')))
  expect(nav().getByRole('button', { name: /丙任务/ }).getAttribute('aria-current')).toBe('page')
  expect(screen.getByText('丙聊天 #9')).toBeTruthy()
})

test('慢摘要不叠加请求；切换后的迟到详情不能进入另一聊天', async () => {
  await mount()
  const oldSummary = deferred<Response>()
  const oldDetail = deferred<Response>()
  handler = (url) => url === '/api/task-summaries' ? oldSummary.promise : url === '/api/tasks/乙' ? oldDetail.promise : undefined
  await tick()
  await act(async () => select('甲')); write('甲草稿')
  await tick(16000)
  expect(summaryCalls()).toHaveLength(2)
  await act(async () => {
    oldSummary.resolve(json({ tasks: rows }, '"slow"'))
    oldDetail.resolve(json(detail('乙', 10)))
  })
  expect(input().value).toBe('甲草稿')
  expect(screen.queryByText('乙聊天 #10')).toBeNull()
  expect(screen.getByText('甲聊天 #1')).toBeTruthy()
})

test('初始隐藏页不读取摘要或详情，恢复可见立即读；再次隐藏暂停', async () => {
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  await mount(); await tick(20000)
  expect(summaryCalls()).toHaveLength(0)
  expect(fetchMock.mock.calls.some(([url]) => url.startsWith('/api/tasks/'))).toBe(false)
  visibility.mockReturnValue('visible')
  await act(async () => fireEvent(document, new Event('visibilitychange')))
  expect(summaryCalls()).toHaveLength(1)
  write('保留隐藏页草稿')
  visibility.mockReturnValue('hidden'); fireEvent(document, new Event('visibilitychange'))
  await tick(20000); expect(summaryCalls()).toHaveLength(1)
  visibility.mockReturnValue('visible')
  await act(async () => fireEvent(document, new Event('visibilitychange')))
  expect(summaryCalls()).toHaveLength(2)
  expect(input().value).toBe('保留隐藏页草稿')
})

test('隐藏取消摘要请求，忽略已经取消的迟到响应', async () => {
  await mount()
  const old = deferred<Response>()
  handler = (url) => url === '/api/task-summaries' ? old.promise : undefined
  await tick()
  const signal = summaryCalls()[1][1].signal!
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
  fireEvent(document, new Event('visibilitychange'))
  expect(signal.aborted).toBe(true)
  await act(async () => old.resolve(json({ tasks: [...rows, summary('丙', 10)] }, '"aborted"')))
  expect(nav().queryByRole('button', { name: /丙/ })).toBeNull()
  visibility.mockReturnValue('visible')
  await act(async () => fireEvent(document, new Event('visibilitychange')))
  expect(summaryCalls()).toHaveLength(3)
})

test('摘要错误后可恢复，使用相同访问令牌且不读取旧列表接口', async () => {
  handler = (url) => url === '/api/task-summaries' ? new Response('temporary error', { status: 503 }) : undefined
  await mount()
  expect(screen.getByRole('alert').textContent).toBe('temporary error')
  handler = () => undefined
  await tick()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('乙聊天 #1')).toBeTruthy()
  const headers = new Headers(summaryCalls()[1][1].headers)
  expect(headers.get('Content-Type')).toBe('application/json')
  expect(headers.get('X-Cater-Access-Token')).toBe('fixture-token')
  expect(new Headers(fetchMock.mock.calls.find(([url]) => url === '/api/tasks/乙')![1].headers)
    .get('X-Cater-Access-Token')).toBe('fixture-token')
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/tasks')).toBe(false)
})

test('无效摘要响应不能导致已打开的聊天崩溃或清除草稿', async () => {
  await mount(); write('我的草稿')
  handler = (url) => url === '/api/task-summaries' ? json({ task: detail('甲').task }) : undefined
  await tick()
  expect(input().value).toBe('我的草稿')
  expect(screen.getByText('乙聊天 #1')).toBeTruthy()
  expect(nav().getByRole('button', { name: /乙任务/ }).getAttribute('aria-current')).toBe('page')
})

test('卸载中止读取并清理刷新计时器', async () => {
  const pending = deferred<Response>()
  handler = (url) => url === '/api/task-summaries' ? pending.promise : undefined
  const view = render(<App />)
  const signal = summaryCalls()[0][1].signal!
  view.unmount()
  expect(signal.aborted).toBe(true)
  await act(async () => pending.resolve(json({ tasks: rows })))
  await tick(20000)
  expect(summaryCalls()).toHaveLength(1)
})
