import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { PAGE_RECOVERY_KEY, savePageRecovery } from '../src/pageRecovery'
import type { PageDraftSnapshot } from '../src/pageRecovery'
import { pageNavigation } from '../src/pageReload'

const flags = vi.hoisted(() => ({ broken: true, markdownBroken: false }))
vi.mock('../src/ConversationActivity', () => ({ ConversationActivity: () => { if (flags.broken) throw new Error('lazy-feature-error'); return <p>当前进展已恢复</p> } }))
vi.mock('../src/ChatMarkdown', () => ({ ChatMarkdown: ({ text }: { text: string }) => { if (flags.markdownBroken) throw new Error('lazy-markdown-error'); return <p>{text}</p> } }))
const json = (value: unknown) => new Response(JSON.stringify(value))
let native = true
let allowFailedPost = false
let incompletePost: { value: unknown } | null = null
const team = { enabled: true, objective: '当前任务', status: 'running', pending_count: 0, revision: 0, runs: [], assignments: [], checkpoint: '' }
const taskTeam = (id: string) => ({ ...team, objective: `${id}任务` })
const summary = (id: string) => ({ id, goal: `${id}任务`, status: 'running', status_label: '进行中', conversation_count: 1, latest_seq: 4, version: 4, ...(native ? { team: taskTeam(id) } : {}) })
const detail = (id: string) => ({
  task: { id, goal: `${id}任务`, acceptance: [], status: 'running', messages: [], assignments: [], deliverables: [], ...(native ? { team: taskTeam(id) } : {}) },
  ...(native ? { team: taskTeam(id) } : {}), summary: summary(id), activity: { active: false, state: 'idle' }, review: {},
  events: [{ seq: 1, type: 'task.created', actor: { kind: 'human', id: 'human', role: 'human' }, payload: { goal: `${id}任务` } },
    ...(native ? [{ seq: 2, type: 'team.started', actor: { kind: 'system', id: 'team', role: 'team' }, payload: { goal: '当前任务' } }] : []),
    { seq: 4, type: 'msg.said', actor: { id: 'store_manager', kind: 'agent', role: 'manager' }, payload: { text: '已保存的店长答复', claim: '已保存的店长答复', phase: native ? 'team' : 'execution', audience: 'customer', recipient: 'human', response_kind: 'reply' } }],
})
const saved = (extra: Partial<PageDraftSnapshot> = {}): PageDraftSnapshot => ({ drafts: { task_alpha: '普通草稿', $new: '新建草稿' }, selectedId: 'task_alpha', questionMode: false, derivation: null, project: null, ...extra })
let request: ReturnType<typeof vi.fn>
beforeEach(() => {
  window.sessionStorage.clear()
  flags.broken = true
  flags.markdownBroken = false
  native = true
  allowFailedPost = false
  incompletePost = null
  vi.spyOn(console, 'error').mockImplementation(() => {})
  request = vi.fn((url: string, init: RequestInit = {}) => {
    if (init.method && init.method !== 'GET') {
      if (incompletePost) return Promise.resolve(json(incompletePost.value))
      if (allowFailedPost) return Promise.reject(new TypeError('Simulated lost response'))
      throw new Error('No automatic mutation expected')
    }
    if (url === '/api/task-summaries') return Promise.resolve(json({ tasks: [summary('task_alpha'), summary('task_beta')] }))
    if (url === '/api/organization') return Promise.resolve(json([]))
    if (url === '/api/organization/profile') return Promise.resolve(json({ leader: { id: 'store_manager', name: '店长' }, cabinet: [], workers: [] }))
    if (url === '/api/workspaces') return Promise.resolve(json([
      { id: '__managed__', title: '新建独立工作目录', root: 'F:/群工作目录', mode: 'execute', commands: ['test'], managed: true },
      { id: 'docs', title: '资料', root: 'F:/资料', mode: 'read_only', commands: [] },
    ]))
    if (url === '/api/projects') return Promise.resolve(json([]))
    if (url.startsWith('/api/rules')) return Promise.resolve(json({ rules: [] }))
    if (/^\/api\/tasks\/task_(alpha|beta)$/.test(url)) return Promise.resolve(json(detail(url.split('/')[3])))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', request)
})
const posts = () => request.mock.calls.filter(([, init]) => init.method && init.method !== 'GET')
const input = () => screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement

test.each([undefined, null, '__managed__', 'docs'])('新群草稿目录 %s 经实际保留刷新流程恢复，managed 默认不覆盖用户选择', async workspaceId => {
  const reload = vi.spyOn(pageNavigation, 'reload').mockImplementation(() => {})
  await act(async () => { render(<App />) })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '新建任务' })))
  if (workspaceId !== undefined) {
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '工作目录', exact: true })))
    if (workspaceId !== null) fireEvent.change(screen.getByRole('combobox', { name: '可选目录' }), { target: { value: workspaceId } })
    fireEvent.click(screen.getByRole('button', { name: workspaceId === null ? '不使用目录' : '用于新对话', exact: true }))
  }
  fireEvent.change(screen.getByRole('textbox', { name: '发起任务对话' }), { target: { value: '保留的新群草稿' } })
  await act(async () => fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /^task_alpha任务/ })))
  await screen.findByText('这部分暂时打不开')
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '保留草稿并刷新' })))
  expect(reload).toHaveBeenCalledTimes(1)
  const snapshot = JSON.parse(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)!).snapshot
  expect(snapshot.workspaceId).toBe(workspaceId)
  if (workspaceId === undefined) expect(snapshot).not.toHaveProperty('workspaceId')
  cleanup()
  await act(async () => { render(<StrictMode><App /></StrictMode>) })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '新建任务' })))
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('保留的新群草稿')
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '工作目录', exact: true })))
  expect((screen.getByRole('combobox', { name: '可选目录' }) as HTMLSelectElement).value).toBe(workspaceId === undefined ? '__managed__' : workspaceId ?? '')
  fireEvent.click(screen.getByRole('button', { name: '关闭工作目录' }))
  expect(posts()).toHaveLength(0)
  allowFailedPost = true
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送新任务' })))
  const body = JSON.parse(posts()[0][1].body as string)
  expect(body.workspace_id).toBe(workspaceId)
  if (workspaceId === undefined) expect(body).not.toHaveProperty('workspace_id')
})

test.each(['paused', 'cancelled'] as const)('进展模块失败时%s说明与聊天仍可用，不自动提交控制', async state => {
  const original = request.getMockImplementation()!
  request.mockImplementation((url: string, init: RequestInit = {}) => /^\/api\/tasks\/task_(alpha|beta)$/.test(url)
    ? Promise.resolve(json({ ...detail(url.split('/')[3]), execution_control: { state, seq: 5, reason: '本次控制的原始说明' } }))
    : original(url, init))
  render(<App />)
  await screen.findByText('这部分暂时打不开')
  expect(screen.getByRole('status', { name: '执行状态' }).textContent).toContain(state === 'paused' ? '已暂停后续执行' : '已取消后续执行')
  fireEvent.change(input(), { target: { value: '模块失效也保留的草稿' } })
  const reason = screen.getByText('本次控制的原始说明').closest('details')!
  expect(reason.open).toBe(false)
  fireEvent.click(within(reason).getByText(state === 'paused' ? '暂停说明' : '取消说明'))
  expect(reason.open).toBe(true)
  expect(input().value).toBe('模块失效也保留的草稿')
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  expect(posts()).toHaveLength(0)
})

test.each(['bad_questions', 'wrong_summary', 'wrong_task'] as const)('轮询收到%s不污染已显示聊天和未发送草稿', async response => {
  await act(async () => { render(<App />) })
  await screen.findByText('已保存的店长答复')
  fireEvent.change(input(), { target: { value: '轮询失败也不能清除' } })
  const original = request.getMockImplementation()!
  request.mockImplementation((url: string, init: RequestInit = {}) => url === '/api/tasks/task_alpha'
    ? Promise.resolve(json(response === 'bad_questions' ? { ...detail('task_alpha'), questions: {} }
      : response === 'wrong_summary' ? { ...detail('task_alpha'), summary: { ...summary('task_beta'), version: 99 } }
        : detail('task_beta')))
    : original(url, init))
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  expect(input().value).toBe('轮询失败也不能清除')
  expect(screen.getByText('已保存的店长答复')).toBeTruthy()
  expect(within(screen.getByRole('navigation', { name: '任务列表' })).getAllByRole('button')).toHaveLength(2)
  expect(posts()).toHaveLength(0)
})
test('局部出错聊天与输入仍存在，显式刷新保存草稿，再挂载自动恢复但不自动发送', async () => {
  const reload = vi.spyOn(pageNavigation, 'reload').mockImplementation(() => {})
  await act(async () => { render(<App />) })
  await screen.findByText('这部分暂时打不开')
  expect(screen.getByText('已保存的店长答复')).toBeTruthy()
  fireEvent.change(input(), { target: { value: '保留这个尚未发送的补充' } })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '保留草稿并刷新' })))
  expect(reload).toHaveBeenCalledTimes(1)
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).not.toBeNull()
  expect(posts()).toHaveLength(0)
  cleanup()
  flags.broken = false
  await act(async () => { render(<StrictMode><App /></StrictMode>) })
  await waitFor(() => expect(input().value).toBe('保留这个尚未发送的补充'))
  expect(screen.getByText('当前进展已恢复')).toBeTruthy()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
  expect(posts()).toHaveLength(0)
})

test('StrictMode恢复保留原选中群和各群草稿，不被默认任务选择覆盖', async () => {
  await savePageRecovery(saved(), '', () => true)
  await act(async () => { render(<StrictMode><App /></StrictMode>) })
  await waitFor(() => expect(input().value).toBe('普通草稿'))
  const list = within(screen.getByRole('navigation', { name: '任务列表' }))
  await act(async () => fireEvent.click(list.getByRole('button', { name: /^task_beta任务/ })))
  expect(input().value).toBe('')
  await act(async () => fireEvent.click(list.getByRole('button', { name: /^task_alpha任务/ })))
  expect(input().value).toBe('普通草稿')
  fireEvent.click(screen.getByRole('button', { name: '新建任务', exact: true }))
  expect((screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement).value).toBe('新建草稿')
  expect(posts()).toHaveLength(0)
})

test('衍生草稿仍显示来源模式，不能悄悄变成原群普通消息', async () => {
  await savePageRecovery(saved({ drafts: { '$derive:task_alpha': '衍生新目标', task_alpha: '原群草稿' }, derivation: { id: 'task_alpha', goal: '创建时的来源标题' } }), '', () => true)
  await act(async () => { render(<StrictMode><App /></StrictMode>) })
  await waitFor(() => expect((screen.getByRole('textbox', { name: '衍生任务目标' }) as HTMLTextAreaElement).value).toBe('衍生新目标'))
  expect(screen.getByRole('region', { name: '新任务草稿来源' }).textContent).toContain('创建时的来源标题')
  fireEvent.click(screen.getByRole('button', { name: '取消新任务草稿' }))
  expect(input().value).toBe('原群草稿')
  expect(posts()).toHaveLength(0)
})

test('旧式追问恢复其原模式，不自动重新提交或丢掉普通草稿', async () => {
  native = false
  await savePageRecovery(saved({ drafts: { '$question:task_alpha': '这是原成果追问', task_alpha: '原群草稿' }, questionMode: true }), '', () => true)
  await act(async () => { render(<StrictMode><App /></StrictMode>) })
  await waitFor(() => expect((screen.getByRole('textbox', { name: '向当前对话提问' }) as HTMLTextAreaElement).value).toBe('这是原成果追问'))
  expect(posts()).toHaveLength(0)
})

test('存储不可用时按钮不刷新，现有输入仍可继续编辑', async () => {
  const reload = vi.spyOn(pageNavigation, 'reload').mockImplementation(() => {})
  await act(async () => { render(<App />) })
  await screen.findByText('这部分暂时打不开')
  fireEvent.change(input(), { target: { value: '不能丢的输入' } })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '保留草稿并刷新' })))
  expect(reload).not.toHaveBeenCalled()
  expect(input().value).toBe('不能丢的输入')
  expect(screen.getByText(/无法访问页面草稿存储/)).toBeTruthy()
  expect(posts()).toHaveLength(0)
})

test('提交结果未知后刷新保留原请求标识，手动重试不会变成另一条指令', async () => {
  allowFailedPost = true
  vi.spyOn(pageNavigation, 'reload').mockImplementation(() => {})
  await act(async () => { render(<App />) })
  await screen.findByText('这部分暂时打不开')
  fireEvent.change(input(), { target: { value: '结果未知但不能重新派活' } })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送消息', exact: true })))
  expect(posts()).toHaveLength(1)
  const first = JSON.parse(posts()[0][1].body as string)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '保留草稿并刷新' })))
  cleanup()
  await act(async () => { render(<StrictMode><App /></StrictMode>) })
  await waitFor(() => expect(input().value).toBe('结果未知但不能重新派活'))
  expect(posts()).toHaveLength(1)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '发送消息', exact: true })))
  expect(posts()).toHaveLength(2)
  expect(JSON.parse(posts()[1][1].body as string)).toEqual(first)
})

test.each(['questions', 'derivations'] as const)('恢复%s的未知提交后仍使用原标识和原模式', async kind => {
  allowFailedPost = true
  native = kind === 'derivations'
  const text = '这是尚未确认结果的提交'
  const key = `${kind === 'questions' ? '$question' : '$derive'}:task_alpha`
  const requestId = '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca'
  await savePageRecovery(saved({ drafts: { [key]: text }, questionMode: kind === 'questions',
    derivation: kind === 'derivations' ? { id: 'task_alpha', goal: '原群背景' } : null,
    requests: { conversation: {}, questions: {}, derivations: {}, [kind]: { [key]: { text, requestId } } } }), '', () => true)
  await act(async () => { render(<StrictMode><App /></StrictMode>) })
  const label = kind === 'questions' ? '向当前对话提问' : '衍生任务目标'
  await waitFor(() => expect((screen.getByRole('textbox', { name: label }) as HTMLTextAreaElement).value).toBe(text))
  expect(posts()).toHaveLength(0)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: kind === 'questions' ? '发送提问' : '创建衍生任务', exact: true })))
  expect(posts()).toHaveLength(1)
  expect(JSON.parse(posts()[0][1].body as string).request_id).toBe(requestId)
})

test.each((['conversation', 'questions', 'derivations'] as const).flatMap(kind =>
  (['empty', 'bad_events', 'wrong_task', 'bad_questions', 'wrong_summary'] as const).map(response => ({ kind, response }))))(
  '$kind 收到 $response 的2xx回执后仍保留草稿，显式刷新后复用原提交编号', async ({ kind, response }) => {
    native = kind !== 'questions'
    flags.markdownBroken = kind === 'questions'
    const text = '不要把残缺回执当作已经接收'
    const key = kind === 'conversation' ? 'task_alpha' : `${kind === 'questions' ? '$question' : '$derive'}:task_alpha`
    await savePageRecovery(saved({ drafts: { [key]: text }, questionMode: kind === 'questions',
      derivation: kind === 'derivations' ? { id: 'task_alpha', goal: '原群背景' } : null }), '', () => true)
    const correct = detail(kind === 'derivations' ? 'task_child' : 'task_alpha')
    incompletePost = { value: response === 'empty' ? {} : response === 'bad_events'
      ? { ...correct, events: [null] } : response === 'bad_questions' ? { ...correct, questions: {} }
        : response === 'wrong_summary' ? { ...correct, summary: { ...summary('task_beta'), version: 99 } }
          : detail(kind === 'derivations' ? 'task_alpha' : 'task_beta') }
    vi.spyOn(pageNavigation, 'reload').mockImplementation(() => {})
    const label = kind === 'conversation' ? '向团队发送消息' : kind === 'questions' ? '向当前对话提问' : '衍生任务目标'
    const send = kind === 'conversation' ? '发送消息' : kind === 'questions' ? '发送提问' : '创建衍生任务'
    const box = () => screen.getByRole('textbox', { name: label }) as HTMLTextAreaElement
    await act(async () => { render(<App />) })
    await waitFor(() => expect(box().value).toBe(text))
    await act(async () => fireEvent.click(screen.getByRole('button', { name: send, exact: true })))
    expect(posts()).toHaveLength(1)
    expect(box().value).toBe(text)
    expect(within(screen.getByRole('navigation', { name: '任务列表' })).getAllByRole('button')).toHaveLength(2)
    const first = JSON.parse(posts()[0][1].body as string)
    await act(async () => fireEvent.click(screen.getByRole('button', { name: '保留草稿并刷新' })))
    cleanup()
    await act(async () => { render(<StrictMode><App /></StrictMode>) })
    await waitFor(() => expect(box().value).toBe(text))
    expect(posts()).toHaveLength(1)
    await act(async () => fireEvent.click(screen.getByRole('button', { name: send, exact: true })))
    expect(posts()).toHaveLength(2)
    expect(JSON.parse(posts()[1][1].body as string)).toEqual(first)
  })
