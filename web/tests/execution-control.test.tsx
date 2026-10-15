import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { SUBMIT_TIMEOUT_MS } from '../src/request'
import type { ControlAction, ExecutionControl } from '../src/executionControl'

const labels = { pause: '暂停执行', resume: '恢复执行', cancel: '取消执行' }
const states = { pause: 'paused', resume: 'running', cancel: 'cancelled' } as const
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const json = (value: unknown) => new Response(JSON.stringify(value))
const summary = (id: string, version = 1) => ({ id, goal: `${id}任务`, status: 'running', status_label: '正在协作', conversation_count: 1, latest_seq: version, version })
function detail(id: string, state: ExecutionControl['state'] = 'running', seq: number | null = null) {
  return { task: { id, goal: `${id}任务`, acceptance: [], status: 'running', messages: [], assignments: [], deliverables: [] },
    events: [{ seq: 1, type: 'task.created', actor: { id: 'human' }, payload: { goal: `${id}任务` } }],
    summary: summary(id, seq ?? 1), activity: { active: true, state: 'running' }, review: {},
    execution_control: { state, seq, reason: `${state} 的服务端原因` } }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
let data: Record<string, ReturnType<typeof detail>>
let handler: (url: string, init: RequestInit) => Response | Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>
beforeEach(() => {
  vi.useFakeTimers()
  data = { a: detail('a'), b: detail('b') }
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/task-summaries') return json({ tasks: [data.a, data.b].map((item) => ({ ...item.summary, execution_control: item.execution_control })) })
    if (url === '/api/organization') return json([])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] })
    if (/^\/api\/tasks\/[ab]$/.test(url) || url.endsWith('/questions') || url.endsWith('/command')) return json(data[url.split('/')[3]])
    if (url.endsWith('/control')) {
      const id = url.split('/')[3]
      const action: ControlAction = JSON.parse(init.body as string).action
      data[id] = detail(id, states[action], (data[id].execution_control.seq ?? 1) + 1)
      return json(data[id])
    }
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const mount = async () => { await act(async () => { render(<App />) }) }
const tick = async (ms = 4000) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
const menu = () => {
  if (!screen.queryByRole('group', { name: '任务操作' })) fireEvent.click(screen.getByRole('button', { name: '更多任务操作' }))
}
const control = async (action: ControlAction, retry = false) => {
  menu()
  await act(async () => fireEvent.click(screen.getByRole('button', { name: `${retry ? '重试' : ''}${labels[action]}`, exact: true })))
}
const select = async (id: string) => { await act(async () => fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: new RegExp(`^${id}任务`) }))) }
const posts = () => fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')
const body = (index: number) => JSON.parse(posts()[index][1].body as string)
const input = () => screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
const write = (value: string) => fireEvent.change(input(), { target: { value } })

test('更多操作使用图标；暂停等待服务端确认，恢复只调用 control 并保留草稿', async () => {
  await mount(); write('继续原来的执行')
  const pause = deferred<Response>()
  handler = (url) => url.endsWith('/control') ? pause.promise : undefined
  menu()
  expect(screen.getByRole('button', { name: '暂停执行' }).querySelector('svg')).toBeTruthy()
  expect(screen.getByRole('button', { name: '取消执行' }).title).toContain('不可恢复')
  await control('pause')
  expect(body(0)).toEqual({ action: 'pause', request_id: expect.stringMatching(uuid) })
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('请求待确认')
  expect(screen.queryByText('已暂停')).toBeNull()
  menu()
  expect((screen.getByRole('button', { name: '取消执行' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '暂停执行' }))
  expect(posts()).toHaveLength(1)
  data.b = detail('b', 'paused', 2)
  await act(async () => pause.resolve(json(data.b)))
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已暂停后续执行；在途调用仍可能返回')
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('paused 的服务端原因')
  expect(input().value).toBe('继续原来的执行')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /b任务.*已暂停/ })).toBeTruthy()
  handler = () => undefined
  await control('resume')
  expect(body(1)).toEqual({ action: 'resume', request_id: expect.stringMatching(uuid) })
  expect(posts().every(([url]) => url === '/api/tasks/b/control')).toBe(true)
  expect(screen.queryByRole('status', { name: '执行状态' })).toBeNull()
  expect(input().value).toBe('继续原来的执行')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  await control('pause')
  expect(body(2).request_id).not.toBe(body(0).request_id)
})

test.each(['paused', 'cancelled'] as const)('%s 始终允许普通聊天及快捷键，由后端语义处理继续；取消不会恢复', async (state) => {
  data.b = detail('b', state, 2)
  handler = (url, init) => {
    if (!url.endsWith('/command')) return
    if (state === 'paused' && JSON.parse(init.body as string).instruction === '继续') data.b = detail('b', 'running', 3)
    return json(data.b)
  }
  await mount()
  const notice = screen.getByRole('status', { name: '执行状态' })
  expect(notice.textContent).toContain('在途调用仍可能返回')
  write('进度怎么样？')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送消息' })))
  expect(body(0)).toEqual({ instruction: '进度怎么样？', continue: true, request_id: expect.stringMatching(uuid) })
  write('继续')
  await act(async () => fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true }))
  expect(body(1)).toEqual({ instruction: '继续', continue: true, request_id: expect.stringMatching(uuid) })
  expect(posts().every(([url]) => url === '/api/tasks/b/command')).toBe(true)
  expect(body(1).request_id).not.toBe(body(0).request_id)
  if (state === 'paused') expect(screen.queryByRole('status', { name: '执行状态' })).toBeNull()
  if (state === 'cancelled') {
    expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('不可恢复')
    menu()
    expect(screen.queryByRole('button', { name: '恢复执行' })).toBeNull()
    expect((screen.getByRole('button', { name: '取消执行' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: '暂停执行' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.keyDown(document, { key: 'Escape' })
  }
  write('未发送的聊天草稿')
  fireEvent.click(screen.getByRole('button', { name: '提问', exact: true }))
  const question = screen.getByRole('textbox', { name: '向当前对话提问' })
  fireEvent.change(question, { target: { value: '说明目前的结果' } })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送提问' })))
  expect(posts()).toHaveLength(3)
  expect(posts()[2][0]).toBe('/api/tasks/b/questions')
  expect(body(2)).toEqual({ question: '说明目前的结果', request_id: expect.stringMatching(uuid) })
  fireEvent.click(screen.getByRole('button', { name: '取消提问' }))
  expect(input().value).toBe('未发送的聊天草稿')
  fireEvent.click(screen.getByRole('button', { name: '基于此对话开新任务' }))
  expect(screen.getByRole('textbox', { name: '衍生任务目标' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  expect(screen.getByRole('textbox', { name: '发起任务对话' })).toBeTruthy()
})

test('控制请求仍在途时允许普通聊天，消息不隐式调用恢复或重复控制', async () => {
  await mount()
  const pending = deferred<Response>()
  handler = (url) => url.endsWith('/control') ? pending.promise : undefined
  await control('pause')
  write('现在做到哪里了？')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  await act(async () => fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true }))
  expect(posts().map(([url]) => url)).toEqual(['/api/tasks/b/control', '/api/tasks/b/command'])
  expect(body(1).instruction).toBe('现在做到哪里了？')
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('请求待确认')
  data.b = detail('b', 'paused', 2)
  await act(async () => pending.resolve(json(data.b)))
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已暂停')
})

test.each((['pause', 'resume', 'cancel'] as const).flatMap((action) => ['fetch', 'body'].map((stage) => ({ action, stage }))))('$action 的 $stage 超时不自动重发；轮询状态改变后仍可用原 ID 核对重试', async ({ action, stage }) => {
  data.b = detail('b', action === 'resume' ? 'paused' : 'running', 2)
  await mount()
  const pending = deferred<Response>()
  const oldBody = deferred<unknown>()
  const response = json({})
  vi.spyOn(response, 'json').mockReturnValue(oldBody.promise)
  handler = (url) => url.endsWith('/control') ? stage === 'fetch' ? pending.promise : response : undefined
  await control(action)
  data.b = detail('b', states[action], 3)
  await tick(SUBMIT_TIMEOUT_MS)
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  expect(posts()[0][1].signal.aborted).toBe(true)
  await tick(SUBMIT_TIMEOUT_MS)
  expect(posts()).toHaveLength(1)
  const retry = deferred<Response>()
  handler = (url) => url.endsWith('/control') ? retry.promise : undefined
  await control(action, true)
  expect(posts()[1][1].body).toBe(posts()[0][1].body)
  await act(async () => { pending.resolve(json(detail('b', 'running', 99))); oldBody.resolve(detail('b', 'running', 99)) })
  expect(posts()[1][1].signal.aborted).toBe(false)
  menu()
  expect((screen.getByRole('button', { name: '取消执行' }) as HTMLButtonElement).disabled).toBe(true)
  await act(async () => retry.resolve(json(data.b)))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByRole('button', { name: new RegExp(`^重试${labels[action]}$`) })).toBeNull()
  if (states[action] !== 'running') expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain(action === 'pause' ? '已暂停' : '已取消')
  else expect(screen.queryByRole('status', { name: '执行状态' })).toBeNull()
})

test('网络和409错误沿用原 ID，改变操作分配新 ID，错误不冒充状态成功', async () => {
  await mount()
  let attempts = 0
  handler = (url) => {
    if (!url.endsWith('/control')) return
    attempts += 1
    return attempts === 1 ? Promise.reject(new TypeError('Failed to fetch')) : new Response('控制请求冲突', { status: 409 })
  }
  await control('pause')
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  await control('pause', true)
  expect(screen.getByRole('alert').textContent).toBe('控制请求冲突')
  expect(screen.queryByRole('status', { name: '执行状态' })).toBeNull()
  expect(body(1)).toEqual(body(0))
  await control('cancel')
  expect(body(2).request_id).not.toBe(body(0).request_id)
  expect(body(2).action).toBe('cancel')
})

test('切任务后控制失败不串错误，返回原任务可重试；控制请求不会锁住另一任务', async () => {
  await mount(); write('b 草稿')
  const pending = deferred<Response>()
  handler = (url) => url === '/api/tasks/b/control' ? pending.promise : undefined
  await control('pause')
  await select('a'); write('a 草稿')
  await control('pause')
  expect(posts()[1][0]).toBe('/api/tasks/a/control')
  expect(body(1).request_id).not.toBe(body(0).request_id)
  await act(async () => pending.resolve(new Response('b 的控制失败', { status: 503 })))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(input().value).toBe('a 草稿')
  await select('b')
  expect(screen.getByRole('alert').textContent).toContain('b 的控制失败')
  expect(screen.getByRole('alert').textContent).toContain('提交结果未知')
  expect(input().value).toBe('b 草稿')
  handler = () => undefined
  await control('pause', true)
  expect(body(2)).toEqual(body(0))
})

test('旧恢复响应不得覆盖轮询得到的更新取消状态，普通聊天仍然可用', async () => {
  data.b = detail('b', 'paused', 2)
  await mount(); write('为什么取消了？')
  const resume = deferred<Response>()
  handler = (url) => url.endsWith('/control') ? resume.promise : undefined
  await control('resume')
  data.b = detail('b', 'cancelled', 4)
  await tick()
  const stale = detail('b', 'running', 3)
  stale.summary = summary('b', 8)
  await act(async () => resume.resolve(json(stale)))
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已取消后续执行')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  await act(async () => fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true }))
  expect(posts()).toHaveLength(2)
  expect(posts()[1][0]).toBe('/api/tasks/b/command')
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已取消后续执行')
  menu()
  expect(screen.queryByRole('button', { name: '恢复执行' })).toBeNull()
})

test('首次摘要显示未打开任务的暂停取消状态，详情到达前控制禁用且不追加详情请求', async () => {
  data.a = detail('a', 'paused', 3)
  data.b = detail('b', 'cancelled', 4)
  const pending = deferred<Response>()
  handler = (url) => url === '/api/tasks/b' ? pending.promise : undefined
  await mount()
  const nav = within(screen.getByRole('navigation', { name: '任务列表' }))
  expect(nav.getByRole('button', { name: /a任务.*已暂停/ })).toBeTruthy()
  expect(nav.getByRole('button', { name: /b任务.*已取消/ })).toBeTruthy()
  menu()
  expect((screen.getByRole('button', { name: '取消执行' }) as HTMLButtonElement).disabled).toBe(true)
  expect(fetchMock.mock.calls.filter(([url]) => /^\/api\/tasks\/[ab]$/.test(url)).map(([url]) => url)).toEqual(['/api/tasks/b'])
  await act(async () => pending.resolve(json(detail('b', 'running', 2))))
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已取消')
  expect(screen.queryByRole('button', { name: '恢复执行' })).toBeNull()
})

test('摘要按控制 seq 合并，旧摘要不回滚详情，新摘要可覆盖更低 seq 的详情', async () => {
  data.b = detail('b', 'paused', 5)
  await mount()
  let listed = detail('b', 'running', 3)
  handler = (url) => {
    if (url === '/api/task-summaries') return json({ tasks: [{ ...listed.summary, version: 100, execution_control: listed.execution_control }] })
    if (url === '/api/tasks/b') return json(detail('b', 'running', 4))
  }
  await tick()
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已暂停')
  listed = detail('b', 'cancelled', 6)
  await tick()
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已取消')
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /b任务.*已取消/ })).toBeTruthy()
})

test.each([true, false])('详情摘要控制状态可以独立生效，顶层存在=%s 时也按 seq 选择新状态', async (withTopLevel) => {
  const next = detail('b', 'running', 2)
  handler = (url) => url === '/api/tasks/b' ? json({ ...next,
    execution_control: withTopLevel ? next.execution_control : undefined,
    summary: { ...next.summary, execution_control: { state: 'paused', seq: 5, reason: '最新摘要状态' } },
  }) : undefined
  await mount()
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('最新摘要状态')
  menu()
  expect(screen.getByRole('button', { name: '恢复执行' })).toBeTruthy()
})

test('取消请求返回后，仍在途的普通发送不会恢复执行状态或覆盖新草稿', async () => {
  await mount(); write('最初方向')
  const command = deferred<Response>()
  handler = (url) => url.endsWith('/command') ? command.promise : undefined
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  write('刚补充的草稿')
  await control('cancel')
  await act(async () => command.resolve(json(detail('b'))))
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain('已取消后续执行')
  expect(input().value).toBe('刚补充的草稿')
  expect(posts()).toHaveLength(2)
})

test('暂停和取消不允许通过澄清回答触发执行，并保留所选答案', async () => {
  const clarification = { seq: 2, role_id: 'dev', question: '选择范围', options: ['本次任务'], status: 'pending' }
  const withQuestion = () => ({ ...data.b, clarifications: [clarification], events: [...data.b.events,
    { seq: 2, type: 'clarification.requested', actor: { id: 'dev' }, payload: clarification }] })
  handler = (url) => url === '/api/tasks/b' ? json(withQuestion()) : undefined
  await mount()
  await act(async () => { await vi.dynamicImportSettled() })
  fireEvent.click(screen.getByRole('radio', { name: '本次任务' }))
  for (const state of ['paused', 'cancelled'] as const) {
    data.b = detail('b', state, state === 'paused' ? 3 : 4)
    await tick()
    const form = screen.getByRole('form', { name: '补充信息：选择范围' })
    expect(form.querySelector('fieldset')?.disabled).toBe(true)
    fireEvent.submit(form)
    expect(posts()).toHaveLength(0)
  }
  expect((screen.getByRole('radio', { name: '本次任务' }) as HTMLInputElement).checked).toBe(true)
})

test('更多操作可以 Escape 或点外部关闭，不会产生控制请求', async () => {
  await mount(); menu()
  screen.getByRole('button', { name: '暂停执行' }).focus()
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('group', { name: '任务操作' })).toBeNull()
  expect(document.activeElement).toBe(screen.getByRole('button', { name: '更多任务操作' }))
  menu(); fireEvent.pointerDown(input())
  expect(screen.queryByRole('group', { name: '任务操作' })).toBeNull()
  expect(posts()).toHaveLength(0)
})
