import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { conversationParticipants } from '../src/participants'

const goals: Record<string, string> = { a: '甲任务', b: '乙任务', c: '衍生任务' }
const basis = { seq: 1, anchor: 'captured-version-hash', source_seqs: [{ seq: 1, kind: 'goal' }], history_seqs: [7, 9] }
const question = '@米寇 为什么选这个方案？'
const answer = '依据当时的成果，可以先复查这项观察。'
const quote = '<script>原文不执行</script>\n  保留原文的空格和换行。'
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function summary(id: string) {
  return { id, goal: goals[id], status: 'done', status_label: '已完成', conversation_count: 1, latest_seq: 1, version: 1 }
}

function detail(id = 'b') {
  return { task: { id, goal: goals[id], acceptance: [], status: 'done', messages: [], assignments: [], deliverables: [] },
    events: [{ seq: 1, type: 'task.created', actor: { id: 'human', kind: 'human' }, payload: { goal: `${goals[id]}历史` } }],
    activity: { active: false, state: 'idle' }, review: {} }
}

function answered(id = 'b', status = 'answered', outdated = false, system = false) {
  const result = { question_seq: 2, status, answer, evidence: [{ source_seq: 1, quote }], limits: ['仅适用于提问时可引用的成果。'], reason: '请结合来源复查。', ...(system ? {} : { speaker_id: 'dev' }) }
  return { ...detail(id),
    events: [...detail(id).events,
      { seq: 2, type: 'question.asked', actor: { id: 'human', kind: 'human' }, payload: { question, role_id: 'dev', request_id: 'request-a', basis } },
      { seq: 3, type: 'question.started', actor: { id: 'explainer', kind: 'system' }, payload: { question_seq: 2, text: '不能生成开始气泡' } },
      { seq: 4, type: 'question.finished', actor: { id: 'explainer', kind: 'system' }, payload: { ...result, reasoning: '不能显示的隐藏推理' } },
    ], questions: [{ seq: 2, question, role_id: 'dev', basis, outdated, ...result }] }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
let handler: (url: string, init: RequestInit) => Response | Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.useFakeTimers()
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    const override = handler(url, init)
    if (override) return override
    if (url === '/api/task-summaries') return json({ tasks: [summary('a'), summary('b')] })
    if (url === '/api/organization') return json([{ id: 'dev', name: '米寇', role: 'dev' }])
    if (url === '/api/organization/profile') return json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [{ id: 'dev', name: '米寇', role: 'dev' }] })
    if (/^\/api\/tasks\/[abc]$/.test(url)) return json(detail(url.split('/')[3]))
    if (/^\/api\/tasks\/[ab]\/questions$/.test(url)) return new Response(JSON.stringify(answered(url.split('/')[3])), { status: 202, headers: { 'Content-Type': 'application/json' } })
    if (url === '/api/tasks/b/derive') return json(detail('c'))
    if (url === '/api/tasks/b/command') return json(detail())
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const mount = async () => { await act(async () => { render(<App />) }) }
const normalInput = () => screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
const questionInput = () => screen.getByRole('textbox', { name: '向当前对话提问' }) as HTMLTextAreaElement
const derivedInput = () => screen.getByRole('textbox', { name: '衍生任务目标' }) as HTMLTextAreaElement
const write = (input: HTMLElement, value: string) => fireEvent.change(input, { target: { value } })
const toggle = () => fireEvent.click(screen.getByRole('button', { name: '提问', exact: true }))
const beginDerivation = () => fireEvent.click(screen.getByRole('button', { name: '基于此对话开新任务' }))
const select = async (id: string) => { await act(async () => fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: new RegExp(`^${goals[id]}`) }))) }
const send = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送提问' }))) }
const posts = () => fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')
const bodies = () => posts().map(([, init]) => JSON.parse(init.body as string))

test('中文图标入口在同一聊天区切换，普通、提问、衍生及新任务草稿独立', async () => {
  await mount()
  write(normalInput(), '乙的普通草稿')
  const button = screen.getByRole('button', { name: '提问', exact: true })
  expect(button.getAttribute('title')).toBe('向当前对话提问')
  expect(button.getAttribute('aria-pressed')).toBe('false')
  expect(button.querySelector('svg')).toBeTruthy()
  toggle()
  expect(button.getAttribute('aria-pressed')).toBe('true')
  expect(questionInput().value).toBe('')
  expect(document.activeElement).toBe(questionInput())
  expect(screen.getAllByRole('textbox')).toHaveLength(1)
  expect((screen.getByRole('button', { name: '发送提问' }) as HTMLButtonElement).disabled).toBe(true)
  write(questionInput(), '乙的提问草稿')
  fireEvent.click(screen.getByRole('button', { name: '取消提问' }))
  expect(normalInput().value).toBe('乙的普通草稿')
  expect(document.activeElement).toBe(normalInput())
  toggle(); expect(questionInput().value).toBe('乙的提问草稿')
  beginDerivation()
  expect(button.getAttribute('aria-pressed')).toBe('false')
  expect(derivedInput().value).toBe('')
  write(derivedInput(), '乙的衍生草稿')
  toggle(); expect(questionInput().value).toBe('乙的提问草稿')
  beginDerivation(); expect(derivedInput().value).toBe('乙的衍生草稿')
  fireEvent.click(screen.getByRole('button', { name: '取消新任务草稿' }))
  expect(normalInput().value).toBe('乙的普通草稿')
  await select('a'); toggle(); write(questionInput(), '甲的提问草稿')
  await select('b')
  expect(normalInput().value).toBe('乙的普通草稿')
  toggle(); expect(questionInput().value).toBe('乙的提问草稿')
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  write(screen.getByRole('textbox', { name: '发起任务对话' }), '独立新任务草稿')
  await select('a'); toggle(); expect(questionInput().value).toBe('甲的提问草稿')
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('独立新任务草稿')
  expect(posts()).toHaveLength(0)
})

test('完成任务在原群 POST questions，保留点名，不调用 command 或创建新任务', async () => {
  await mount()
  write(normalInput(), '继续协作的草稿')
  toggle(); write(questionInput(), `  ${question}  `); await send()
  expect(posts()).toHaveLength(1)
  expect(posts()[0][0]).toBe('/api/tasks/b/questions')
  expect(bodies()[0]).toEqual({ question, request_id: expect.stringMatching(uuid) })
  expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
  expect(questionInput().value).toBe('')
  expect(screen.getByText(answer)).toBeTruthy()
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /乙任务.*3 条对话.*已完成/ })).toBeTruthy()
  toggle(); expect(normalInput().value).toBe('继续协作的草稿')
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送消息' })))
  expect(posts()[1][0]).toBe('/api/tasks/b/command')
  expect(bodies()[1]).toEqual({ instruction: '继续协作的草稿', continue: true, request_id: expect.stringMatching(uuid) })
})

test('普通消息发送失败后重试复用标识，成功后的下一条消息使用新标识', async () => {
  await mount()
  let calls = 0
  handler = (url) => {
    if (!url.endsWith('/command')) return
    calls += 1
    return calls === 1 ? new Response('连接中断', { status: 503 }) : json(detail())
  }
  const sendMessage = async () => { await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送消息' }))) }
  write(normalInput(), '继需了吗')
  await sendMessage()
  expect(normalInput().value).toBe('继需了吗')
  await sendMessage()
  expect(bodies()[0].request_id).toMatch(uuid)
  expect(bodies()[1]).toEqual(bodies()[0])
  expect(normalInput().value).toBe('')
  write(normalInput(), '继需了吗')
  await sendMessage()
  expect(bodies()[2].request_id).not.toBe(bodies()[0].request_id)
})

test('HTTP、网络及409失败保留草稿与 UUID，取消、切换和轮询后仍沿用', async () => {
  await mount()
  let attempts = 0
  handler = (url) => {
    if (!url.endsWith('/questions')) return
    attempts += 1
    if (attempts === 1) return new Response('暂时不能提问', { status: 503 })
    if (attempts === 2) return Promise.reject(new Error('网络中断'))
    return new Response('同一请求不能更换问题', { status: 409 })
  }
  toggle(); write(questionInput(), question); await send()
  expect(screen.getByRole('alert').textContent).toBe('暂时不能提问')
  toggle(); toggle(); await send()
  expect(screen.getByRole('alert').textContent).toBe('网络中断')
  await select('a'); toggle(); write(questionInput(), '另一项问题')
  await select('b'); toggle()
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  await send(); await send()
  expect(screen.getByRole('alert').textContent).toBe('同一请求不能更换问题')
  expect(questionInput().value).toBe(question)
  expect(bodies()).toHaveLength(4)
  expect(bodies()[0].request_id).toMatch(uuid)
  expect(bodies().every((body) => JSON.stringify(body) === JSON.stringify(bodies()[0]))).toBe(true)
  write(questionInput(), '改过的问题'); write(questionInput(), question)
  expect(screen.queryByRole('alert')).toBeNull()
  await send()
  expect(bodies()[4].request_id).not.toBe(bodies()[0].request_id)
  await select('a'); toggle(); await send()
  expect(posts()[5][0]).toBe('/api/tasks/a/questions')
  expect(bodies()[5].request_id).not.toBe(bodies()[4].request_id)
})

test('点击和快捷键防重复，发送中编辑再恢复原文仍保留新稿并换 UUID', async () => {
  await mount()
  const pending = deferred<Response>()
  handler = (url) => url.endsWith('/questions') ? pending.promise : undefined
  toggle(); write(questionInput(), question)
  fireEvent.keyDown(questionInput(), { key: 'Enter', ctrlKey: true })
  fireEvent.keyDown(questionInput(), { key: 'Enter', ctrlKey: true })
  fireEvent.click(screen.getByRole('button', { name: '发送提问' }))
  expect(posts()).toHaveLength(1)
  write(questionInput(), '后来又修改过'); write(questionInput(), question)
  await act(async () => pending.resolve(json(answered())))
  expect(questionInput().value).toBe(question)
  handler = () => undefined
  await send()
  expect(bodies()[1].request_id).not.toBe(bodies()[0].request_id)
  expect(questionInput().value).toBe('')
})

test('迟到成功不抢导航、不清另一任务或普通及衍生草稿', async () => {
  await mount()
  write(normalInput(), '乙的普通草稿')
  const pending = deferred<Response>()
  handler = (url) => url === '/api/tasks/b/questions' ? pending.promise : undefined
  toggle(); write(questionInput(), question)
  fireEvent.click(screen.getByRole('button', { name: '发送提问' }))
  beginDerivation(); write(derivedInput(), '乙的衍生草稿')
  await select('a'); toggle(); write(questionInput(), '甲的新问题')
  await act(async () => pending.resolve(json(answered())))
  expect(screen.getByRole('heading', { name: '甲任务', level: 1 })).toBeTruthy()
  expect(questionInput().value).toBe('甲的新问题')
  expect(screen.queryByText(answer)).toBeNull()
  handler = (url) => url === '/api/tasks/b' ? json(answered()) : undefined
  await select('b')
  expect(normalInput().value).toBe('乙的普通草稿')
  beginDerivation(); expect(derivedInput().value).toBe('乙的衍生草稿')
})

test('迟到失败不覆盖新问题的错误状态，其他模式不显示提问错误', async () => {
  await mount()
  const pending = deferred<Response>()
  handler = (url) => url.endsWith('/questions') ? pending.promise : undefined
  toggle(); write(questionInput(), question)
  fireEvent.click(screen.getByRole('button', { name: '发送提问' }))
  write(questionInput(), '全新的提问草稿')
  await act(async () => pending.resolve(new Response('旧问题失败', { status: 500 })))
  expect(screen.queryByRole('alert')).toBeNull()
  expect(questionInput().value).toBe('全新的提问草稿')
  handler = (url) => url.endsWith('/questions') ? new Response('旧问题失败', { status: 500 }) : undefined
  await send()
  expect(screen.getByRole('alert').textContent).toBe('旧问题失败')
  toggle(); expect(screen.queryByRole('alert')).toBeNull()
  beginDerivation(); expect(screen.queryByRole('alert')).toBeNull()
  await select('a'); toggle(); expect(screen.queryByRole('alert')).toBeNull()
})

test('衍生请求发出后转入提问，衍生迟到成功不会导航或清提问草稿', async () => {
  await mount()
  const pending = deferred<Response>()
  handler = (url) => url.endsWith('/derive') ? pending.promise : undefined
  beginDerivation(); write(derivedInput(), '另一个目标')
  fireEvent.click(screen.getByRole('button', { name: '创建衍生任务' }))
  toggle(); write(questionInput(), '仍在原群的问题')
  await act(async () => pending.resolve(json(detail('c'))))
  expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
  expect(questionInput().value).toBe('仍在原群的问题')
  await send()
  expect(posts()[1][0]).toBe('/api/tasks/b/questions')
})

test('问题与完成各一条气泡，原文和版本可展开，隐藏推理不进入审计展示', async () => {
  handler = (url) => url === '/api/tasks/b' ? json(answered('b', 'answered', true)) : undefined
  await mount()
  const stream = screen.getByLabelText('聊天记录')
  expect(stream.querySelectorAll('article')).toHaveLength(3)
  expect(screen.getByText(question).closest('.message-bubble')?.classList.contains('is-human')).toBe(true)
  const bubble = screen.getByText(answer).closest('.message-bubble')!
  expect(bubble.getAttribute('data-actor')).toBe('dev')
  expect(bubble.classList.contains('is-agent')).toBe(true)
  expect(within(bubble as HTMLElement).getByText('历史版本 · 回答基于提问时的任务版本')).toBeTruthy()
  const details = Array.from(bubble.querySelectorAll('details')).find((item) => item.querySelector('summary')?.textContent === '查看引用、版本与限制')!
  expect(details.open).toBe(false)
  fireEvent.click(details.querySelector('summary')!)
  expect(details.open).toBe(true)
  expect(details.querySelector('blockquote')?.textContent).toBe(quote)
  expect(details.querySelector('script')).toBeNull()
  expect(details.textContent).toContain('提问时版本 · 截至事件 #1')
  expect(details.textContent).not.toContain('captured-version-hash')
  expect(details.textContent).toContain('历史问答事件：#7、#9')
  expect(details.textContent).toContain('原文引用 · 来源事件 #1')
  expect(details.textContent).toContain('来源事件：#1（目标）')
  expect(details.textContent).toContain('提问对象：@米寇')
  expect(details.textContent).toContain('仅适用于提问时可引用的成果。')
  expect(stream.textContent).not.toContain('不能生成开始气泡')
  expect(stream.textContent).not.toContain('不能显示的隐藏推理')
  expect(stream.textContent).not.toContain('reasoning')
  fireEvent.click(within(screen.getByRole('group', { name: '对话筛选' })).getByRole('button', { name: '协作', exact: true }))
  expect(screen.getByText(question)).toBeTruthy()
  expect(screen.getByText(answer)).toBeTruthy()
})

test.each([
  ['answered', '讲解回答'], ['insufficient', '依据不足'], ['blocked', '提问受阻'], ['invalid', '问题未能处理'], ['uncertain', '回答待确认'],
])('%s 完成显示系统气泡，缺少 answer 时显示 reason 且不改变完成状态', async (status, label) => {
  const data = answered('b', status, false, true)
  delete (data.events[3].payload as { answer?: string }).answer
  handler = (url) => url === '/api/tasks/b' ? json(data) : undefined
  await mount()
  const bubble = screen.getByText(`· ${label}`).closest('.message-bubble')!
  expect(bubble.classList.contains('is-system')).toBe(true)
  expect(within(bubble as HTMLElement).getByText('系统', { exact: true })).toBeTruthy()
  expect(bubble.getAttribute('data-actor')).toBe('system')
  expect(within(bubble as HTMLElement).getByText('提问对象：@米寇')).toBeTruthy()
  expect(within(bubble as HTMLElement).getByText('请结合来源复查。', { exact: true })).toBeTruthy()
  expect(screen.queryByText('历史版本 · 回答基于提问时的任务版本')).toBeNull()
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /乙任务.*3 条对话.*已完成/ })).toBeTruthy()
})

test('旧详情没有 questions 字段仍显示两类消息及事件中的版本，后续刷新可标历史版', async () => {
  let outdated = false
  handler = (url) => {
    if (url !== '/api/tasks/b') return
    const data = answered('b', 'answered', outdated)
    return json(outdated ? { ...data, events: [...data.events, { seq: 5, type: 'task.routed', actor: { id: 'system', kind: 'system' }, payload: {} }] } : { ...data, questions: undefined })
  }
  await mount()
  expect(screen.getByText(answer)).toBeTruthy()
  expect(screen.queryByText('历史版本 · 回答基于提问时的任务版本')).toBeNull()
  const bubble = screen.getByText(answer).closest('.message-bubble')!
  expect(bubble.textContent).toContain('提问时版本 · 截至事件 #1')
  toggle(); write(questionInput(), '刷新前尚未发送的问题')
  outdated = true
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  expect(screen.getByText('历史版本 · 回答基于提问时的任务版本')).toBeTruthy()
  expect(questionInput().value).toBe('刷新前尚未发送的问题')
  expect(posts()).toHaveLength(0)
})

test.each([['pending', '等待回答'], ['answering', '正在回答'], ['uncertain', '回答结果待确认']])('问题 %s 在原气泡显示状态，started 不增加计数或产生气泡', async (status, label) => {
  const data = answered()
  handler = (url) => url === '/api/tasks/b' ? json({ ...data, events: data.events.slice(0, 3), questions: [{ ...data.questions[0], status }] }) : undefined
  await mount()
  expect(screen.getByLabelText('聊天记录').querySelectorAll('article')).toHaveLength(2)
  expect(screen.getByText(label).getAttribute('role')).toBe('status')
  expect(screen.queryByText(answer)).toBeNull()
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /乙任务.*2 条对话/ })).toBeTruthy()
})

test('提问沿用点名候选和键盘选择，点名操作不触发提交', async () => {
  await mount(); toggle()
  const input = questionInput()
  fireEvent.focus(input)
  write(input, '@米')
  input.setSelectionRange(2, 2)
  fireEvent.select(input)
  expect(screen.getByRole('option', { name: '米寇' })).toBeTruthy()
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(questionInput().value).toBe('@米寇 ')
  expect(posts()).toHaveLength(0)
  write(questionInput(), question); await send()
  expect(bodies()[0].question).toBe(question)
})

test('调用失败仅有系统 reason 时，即使附有 speaker_id 也不冒充角色回答', async () => {
  const data = answered('b', 'invalid')
  delete (data.events[3].payload as { answer?: string }).answer
  handler = (url) => url === '/api/tasks/b' ? json(data) : undefined
  await mount()
  const bubble = screen.getByText('· 问题未能处理').closest('.message-bubble')!
  expect(bubble.getAttribute('data-actor')).toBe('system')
  expect(bubble.classList.contains('is-system')).toBe(true)
})

test('问答负责角色加入成员，旧 participants 调用和未知角色保持兼容', () => {
  const task = { assignments: [], messages: [] }
  expect(conversationParticipants(task, 'store_manager', ['store_manager', 'dev'], ['dev', 'dev', 'unknown'])).toEqual(['store_manager', 'dev'])
  expect(conversationParticipants(task, 'store_manager', ['store_manager', 'dev'])).toEqual(['store_manager'])
})

test('202 返回排队中的标准详情后留在原群，保留版本引用且不把内部锚点倒给用户', async () => {
  await mount()
  const data = answered()
  const captured = { ...basis, anchor: { scope: 'scope-at-question', plan_seq: 12, submissions: {}, review_seq: null } }
  const pendingDetail = { ...data, events: [data.events[0], { ...data.events[1], payload: { ...data.events[1].payload, basis: captured } }],
    questions: [{ ...data.questions[0], basis: captured, status: 'pending' }] }
  handler = (url) => url.endsWith('/questions')
    ? new Response(JSON.stringify(pendingDetail), { status: 202, headers: { 'Content-Type': 'application/json' } }) : undefined
  toggle(); write(questionInput(), question); await send()
  expect(questionInput().value).toBe('')
  expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
  expect(screen.getByText('等待回答')).toBeTruthy()
  const bubble = screen.getByText(question).closest('.message-bubble')!
  expect(bubble.querySelector('details')?.textContent).toContain('提问时版本 · 截至事件 #1')
  expect(bubble.querySelector('details')?.textContent).not.toContain(JSON.stringify(captured.anchor))
  expect(screen.queryByText(answer)).toBeNull()
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /乙任务.*2 条对话.*已完成/ })).toBeTruthy()
  write(questionInput(), question); await send()
  expect(bodies()[1].request_id).not.toBe(bodies()[0].request_id)
  expect(posts().map(([url]) => url)).toEqual(['/api/tasks/b/questions', '/api/tasks/b/questions'])
})

test('旧轮询详情迟到不能覆盖较新的提问结果或清空后续问题草稿', async () => {
  await mount()
  const oldRead = deferred<Response>()
  handler = (url) => url === '/api/tasks/b' ? oldRead.promise : undefined
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  toggle(); write(questionInput(), question); await send()
  expect(screen.getByText(answer)).toBeTruthy()
  write(questionInput(), '后续问题草稿')
  await act(async () => oldRead.resolve(json(detail())))
  expect(screen.getByText(answer)).toBeTruthy()
  expect(questionInput().value).toBe('后续问题草稿')
  expect(screen.getByRole('heading', { name: '乙任务', level: 1 })).toBeTruthy()
})
