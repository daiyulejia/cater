import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { ClarificationCard } from '../src/ClarificationCard'
import type { Clarification, ClarificationControls } from '../src/ClarificationCard'

const member = { id: 'dev', name: '米寇', role: 'dev' }
const leader = { id: 'store_manager', name: '店长', role: 'leader' }
const prompt = '这次优先面向谁？'
const options = ['第一次来的顾客', '<script>老顾客</script>']
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const row = (status: Clarification['status'] = 'pending'): Clarification => ({ seq: 2, role_id: 'dev', question: prompt, options, status })
function detail(id = 'b', version = 3, clarification: Clarification | null = row()) {
  return { task: { id, goal: `${id}任务`, acceptance: [], status: 'pending', assignments: [], messages: [], deliverables: [] },
    summary: { id, goal: `${id}任务`, status: 'pending', status_label: '待补充', conversation_count: 2, version, latest_seq: version },
    clarifications: clarification ? [clarification] : undefined,
    events: [
      { seq: 1, type: 'task.created', actor: { id: 'human', kind: 'human' }, payload: { goal: `${id}任务` } },
      { seq: 2, type: 'clarification.requested', actor: { id: 'dev', kind: 'agent' }, payload: { role_id: 'dev', question: prompt, options, scope: { direction_seq: 1 } } },
      { seq: 3, type: 'msg.said', actor: { id: 'dev', kind: 'agent' }, payload: { text: '提问计费回执', phase: 'clarification_receipt' } },
    ], activity: { active: false, state: 'idle' }, review: {} }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
let current: ReturnType<typeof detail>
let handler: (url: string, init: RequestInit) => Response | Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>
beforeEach(() => {
  current = detail()
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/task-summaries') return json({ tasks: [detail('a').summary, current.summary] })
    if (url === '/api/organization') return json([leader, member])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [member] })
    if (url === '/api/tasks/a') return json(detail('a'))
    if (url === '/api/tasks/b') return json(current)
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})
const mount = async () => {
  await act(async () => { render(<App />) })
  await act(async () => { await vi.dynamicImportSettled() })
  screen.getByRole('form', { name: `补充信息：${prompt}` })
}
const form = () => screen.getByRole('form', { name: `补充信息：${prompt}` })
const posts = () => fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')
const body = (index = 0) => JSON.parse(posts()[index][1].body as string)
const click = async (name: string) => { await act(async () => fireEvent.click(screen.getByRole('button', { name, exact: true }))) }
const selectTask = async (id: string) => {
  fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: new RegExp(`^${id}任务`) }))
  await screen.findByRole('heading', { name: `${id}任务`, level: 1 })
  await screen.findByText(prompt)
}
const refresh = async () => { await act(async () => fireEvent(document, new Event('visibilitychange'))) }

test('真实成员提问显示 @你，单选确认使用专用 API，成功显示后端答案并保留普通草稿', async () => {
  await mount()
  await waitFor(() => expect(screen.getByText(prompt).closest('article')!.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('米寇'))
  const article = screen.getByText(prompt).closest('article')!
  expect(article.querySelector('[data-actor="dev"].is-agent')).toBeTruthy()
  expect(within(article).getByText('@你')).toBeTruthy()
  expect(screen.queryByText('提问计费回执')).toBeNull()
  expect(screen.getByLabelText('聊天记录').querySelectorAll('article')).toHaveLength(2)
  expect((screen.getByRole('button', { name: '确认补充' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: '普通草稿' } })
  fireEvent.click(screen.getByRole('radio', { name: options[1] }))
  expect(posts()).toHaveLength(0)
  expect(document.querySelector('script')).toBeNull()
  const request = deferred<Response>()
  handler = (url) => url.endsWith('/clarifications') ? request.promise : undefined
  fireEvent.submit(form()); fireEvent.submit(form())
  expect(posts()).toHaveLength(1)
  expect(posts()[0][0]).toBe('/api/tasks/b/clarifications')
  expect(body()).toEqual({ question_seq: 2, kind: 'choice', option_index: 1, request_id: expect.any(String) })
  expect(new Headers(posts()[0][1].headers).get('Content-Type')).toBe('application/json')
  expect((screen.getByRole('radio', { name: options[1] }) as HTMLInputElement).disabled || screen.getByRole('radio', { name: options[1] }).closest('fieldset')!.disabled).toBe(true)
  expect(form().getAttribute('aria-busy')).toBe('true')
  current = detail('b', 5, { ...row('answered'), response: { kind: 'choice', option_index: 1, answer: options[1] } })
  current.events.push({ seq: 5, type: 'human.commanded', actor: { id: 'human', kind: 'human' }, payload: { instruction: '@米寇 关于这个问题：老顾客', clarification: { question_seq: 2, kind: 'choice' } } } as typeof current.events[number])
  await act(async () => request.resolve(json(current)))
  expect(screen.getByText('已答')).toBeTruthy()
  expect(screen.getByText(options[1])).toBeTruthy()
  expect(screen.getByText('@米寇 关于这个问题：老顾客').closest('.is-human')).toBeTruthy()
  expect((screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement).value).toBe('普通草稿')
  expect(posts()).toHaveLength(1)
})

test('HTTP、网络和409错误保留单选、自填和 request_id，切任务后仍可重试；编辑重新分配 ID', async () => {
  await mount()
  fireEvent.click(screen.getByRole('radio', { name: options[0] }))
  await click('自己填写')
  fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value: '  优先已有会员  ' } })
  let attempts = 0
  handler = (url) => {
    if (!url.endsWith('/clarifications')) return
    attempts += 1
    if (attempts === 2) return Promise.reject(new Error('网络中断'))
    return new Response(attempts === 1 ? '暂时无法保存' : '问题状态需要刷新', { status: attempts === 1 ? 503 : 409 })
  }
  await click('确认补充')
  expect(screen.getByRole('alert').textContent).toBe('暂时无法保存')
  expect(body()).toEqual({ question_seq: 2, kind: 'custom', answer: '优先已有会员', request_id: expect.any(String) })
  await refresh(); await selectTask('a')
  expect((screen.getByRole('button', { name: '选择选项' })).getAttribute('aria-pressed')).toBe('true')
  expect((screen.getByRole('radio', { name: options[0] }) as HTMLInputElement).checked).toBe(false)
  await selectTask('b')
  expect((screen.getByRole('textbox', { name: '你的补充' }) as HTMLTextAreaElement).value).toBe('  优先已有会员  ')
  await click('确认补充')
  expect(screen.getByRole('alert').textContent).toBe('网络中断')
  await click('确认补充')
  expect(screen.getByRole('alert').textContent).toBe('问题状态需要刷新')
  expect(body(1)).toEqual(body()); expect(body(2)).toEqual(body())
  fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value: '修改过' } })
  fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value: '  优先已有会员  ' } })
  await click('确认补充'); expect(body(3).request_id).not.toBe(body().request_id)
  await click('选择选项')
  expect((screen.getByRole('radio', { name: options[0] }) as HTMLInputElement).checked).toBe(true)
  await click('确认补充')
  expect(body(4).kind).toBe('choice'); expect(body(4).option_index).toBe(0)
  expect(body(4)).not.toHaveProperty('answer')
})

test('跳过不附带选择或自填，失败重试使用相同 ID，后端 answered skip 显示已跳过', async () => {
  await mount()
  fireEvent.click(screen.getByRole('radio', { name: options[0] }))
  await click('自己填写')
  fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value: '还没确认' } })
  handler = (url) => url.endsWith('/clarifications') ? new Response('稍后重试', { status: 503 }) : undefined
  await click('跳过')
  expect(body()).toEqual({ question_seq: 2, kind: 'skip', request_id: expect.any(String) })
  current = detail('b', 4, { ...row('answered'), response: { kind: 'skip', answer: '未确认任何选项' } })
  handler = (url) => url.endsWith('/clarifications') ? json(current) : undefined
  await click('跳过')
  expect(body(1)).toEqual(body())
  expect(screen.getByText('已跳过')).toBeTruthy()
  expect(screen.queryByRole('form')).toBeNull()
})

test('切任务后的迟到失败不串错误，返回原任务仍保留选择、pending 防重复及重试 ID', async () => {
  await mount()
  const request = deferred<Response>()
  handler = (url) => url === '/api/tasks/b/clarifications' ? request.promise : undefined
  fireEvent.click(screen.getByRole('radio', { name: options[0] })); fireEvent.submit(form())
  await selectTask('a')
  await selectTask('b')
  expect(form().getAttribute('aria-busy')).toBe('true')
  fireEvent.submit(form()); expect(posts()).toHaveLength(1)
  await selectTask('a')
  await act(async () => request.resolve(new Response('乙的失败', { status: 503 })))
  expect(screen.queryByRole('alert')).toBeNull()
  expect((screen.getByRole('radio', { name: options[0] }) as HTMLInputElement).checked).toBe(false)
  await selectTask('b')
  expect(screen.getByRole('alert').textContent).toBe('乙的失败')
  expect((screen.getByRole('radio', { name: options[0] }) as HTMLInputElement).checked).toBe(true)
  handler = (url) => url.endsWith('/clarifications') ? new Response('再试', { status: 503 }) : undefined
  await click('确认补充'); expect(body(1)).toEqual(body())
})

test('迟到成功不抢任务或清新草稿，返回原任务观察成功；不同问题 seq 草稿独立', async () => {
  await mount()
  const request = deferred<Response>()
  handler = (url) => url.endsWith('/clarifications') ? request.promise : undefined
  fireEvent.click(screen.getByRole('radio', { name: options[1] })); fireEvent.submit(form())
  await selectTask('a')
  await click('自己填写')
  fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value: '甲的答案' } })
  current = detail('b', 5, { ...row('answered'), response: { kind: 'choice', answer: options[1] } })
  await act(async () => request.resolve(json(current)))
  expect(screen.getByRole('heading', { name: 'a任务', level: 1 })).toBeTruthy()
  expect((screen.getByRole('textbox', { name: '你的补充' }) as HTMLTextAreaElement).value).toBe('甲的答案')
  await selectTask('b')
  expect(screen.getByText('已答')).toBeTruthy()
  const next = { ...row(), seq: 8, question: '接下来选什么？' }
  current = detail('b', 9, next)
  current.events = [{ seq: 8, type: 'clarification.requested', actor: { id: 'dev', kind: 'agent' }, payload: { question: next.question } } as typeof current.events[number]]
  await refresh()
  await screen.findByRole('form', { name: `补充信息：${next.question}` })
  expect((screen.getByRole('radio', { name: options[1] }) as HTMLInputElement).checked).toBe(false)
})

test('旧详情和较早提交结果不能覆盖更新的 stale 状态，失效后无提交入口', async () => {
  await mount()
  const request = deferred<Response>(), oldPoll = deferred<Response>()
  handler = (url) => url === '/api/tasks/b' ? oldPoll.promise : url.endsWith('/clarifications') ? request.promise : undefined
  await refresh()
  fireEvent.click(screen.getByRole('radio', { name: options[0] })); fireEvent.submit(form())
  current = detail('b', 9, row('stale'))
  await act(async () => request.resolve(json(current)))
  expect(screen.getByText('此问题已失效')).toBeTruthy()
  await act(async () => oldPoll.resolve(json(detail())))
  expect(screen.getByText('此问题已失效')).toBeTruthy()
  expect(screen.queryByRole('radio')).toBeNull()
  expect(posts()).toHaveLength(1)
})

test('较早提交响应落后于新轮询时，保留已更新的状态', async () => {
  await mount()
  const request = deferred<Response>()
  handler = (url) => url.endsWith('/clarifications') ? request.promise : undefined
  fireEvent.click(screen.getByRole('radio', { name: options[0] })); fireEvent.submit(form())
  current = detail('b', 12, row('stale'))
  await refresh()
  expect(screen.getByText('此问题已失效')).toBeTruthy()
  await act(async () => request.resolve(json(detail('b', 5, { ...row('answered'), response: { kind: 'choice', answer: options[0] } }))))
  expect(screen.getByText('此问题已失效')).toBeTruthy()
  expect(screen.queryByText('已答')).toBeNull()
})

test('旧记录无 clarifications 投影时只读，真实店长问题不冒充执行成员', async () => {
  current = detail('b', 3, null)
  current.events[1].actor = { id: 'store_manager', kind: 'system' }
  render(<App />)
  await screen.findByText('此条历史问题暂无可提交状态')
  await waitFor(() => expect(screen.getByText(prompt).closest('article')!.querySelector('[role="img"]')?.getAttribute('aria-label')).toBe('店长'))
  expect(screen.getByText(prompt).closest('.is-agent')?.getAttribute('data-actor')).toBe('store_manager')
  expect(screen.getByText('@你')).toBeTruthy()
  expect(screen.queryByRole('radio')).toBeNull()
  expect(posts()).toHaveLength(0)
})

test('无选项默认自填，空白不能提交；多题及任意答案文本均为普通文本', async () => {
  const controls: ClarificationControls = { store: new Map(), createRequestId: () => 'request-1', submit: vi.fn().mockResolvedValue(undefined) }
  const question = { ...row(), options: [], question: '<img src=x onerror=alert(1)>' }
  const view = render(<ClarificationCard taskId="a" question={question} controls={controls} />)
  expect(screen.queryByRole('radio')).toBeNull()
  fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value: '   ' } })
  fireEvent.submit(screen.getByRole('form')); expect(controls.submit).not.toHaveBeenCalled()
  fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value: '<script>alert(1)</script>' } })
  await click('确认补充')
  expect(controls.submit).toHaveBeenCalledWith('a', { question_seq: 2, kind: 'custom', answer: '<script>alert(1)</script>', request_id: 'request-1' })
  view.rerender(<ClarificationCard taskId="a" question={{ ...question, status: 'answered', response: { kind: 'custom', answer: '<img src=x onerror=alert(1)>' } }} controls={controls} />)
  expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeTruthy()
  expect(document.querySelector('img, script')).toBeNull()
})
