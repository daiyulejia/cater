import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { webcrypto } from 'node:crypto'
import { StrictMode } from 'react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { NAVIGATION_MEMORY_KEY, PAGE_RECOVERY_KEY, savePageRecovery } from '../src/pageRecovery'
import { accountBinding } from '../src/creationRecovery'

const leader = { id: 'store_manager', name: '店长' }
const team = { enabled: true, objective: '原工作目标', status: 'running', pending_count: 2, revision: 1, runs: [], assignments: [], checkpoint: '' }
const control = { state: 'paused', seq: 4, reason: '顾客暂时暂停自动工作' }
const summary = (id: string) => ({ id, goal: id === 'task_alpha' ? '甲项目' : '乙项目', status: 'running', status_label: '已暂停',
  conversation_count: 1, latest_seq: 4, version: 4, execution_control: control,
  team: { ...team, objective: id === 'task_alpha' ? '甲项目' : '乙项目' } })
const detail = (id: string) => ({ task: { ...summary(id), acceptance: [], messages: [], assignments: [], deliverables: [] },
  summary: summary(id), team: summary(id).team, execution_control: control, activity: { active: false, state: 'completed' }, review: {},
  events: [{ seq: 1, type: 'task.created', actor: { id: 'human', kind: 'human' }, payload: { goal: summary(id).goal } },
    { seq: 4, type: 'task.execution_controlled', actor: { id: 'human', kind: 'human' }, payload: { action: 'pause', state: 'paused' } }] })
const json = (value: unknown) => new Response(JSON.stringify(value))
let handler: (url: string) => Response | Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>
const remember = (selectedId: string | null, account = 'anonymous') => window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY,
  JSON.stringify({ version: 1, account, selectedId }))
const selected = () => screen.getByRole('navigation', { name: '任务列表' }).querySelector('[aria-current="page"]')
const posts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')

beforeEach(() => {
  window.sessionStorage.clear()
  vi.stubGlobal('crypto', webcrypto)
  handler = () => undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    if (init.method === 'POST') throw new Error('Restoring location must not submit anything')
    const overridden = handler(url)
    if (overridden) return overridden
    if (url === '/api/organization') return json([leader])
    if (url === '/api/organization/profile') return json({ leader, workers: [], cabinet: [] })
    if (url === '/api/task-summaries') return json({ tasks: [summary('task_alpha'), summary('task_beta')] })
    if (/^\/api\/tasks\/task_(alpha|beta)$/.test(url)) return json(detail(url.split('/')[3]))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

test('普通重载回同一群，暂停和待处理工作不因恢复位置启动，未保存正文', async () => {
  const first = render(<App />)
  await screen.findByRole('textbox', { name: '向团队发送消息' })
  await waitFor(() => expect(selected()?.textContent).toContain('乙项目'))
  fireEvent.click(within(screen.getByRole('navigation', { name: '任务列表' })).getByRole('button', { name: /^甲项目/ }))
  await waitFor(() => expect(selected()?.textContent).toContain('甲项目'))
  fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: 'PRIVATE_UNSENT_BODY' } })
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBe('task_alpha')
  expect(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)).not.toContain('PRIVATE_UNSENT_BODY')
  first.unmount()
  render(<App />)
  await waitFor(() => expect(selected()?.textContent).toContain('甲项目'))
  expect((await screen.findByRole('status', { name: '执行状态' })).textContent).toContain('已暂停')
  expect((screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement).value).toBe('')
  expect(posts()).toHaveLength(0)
})

test('明确新建页也会保留，不因刷新或列表返回而打开另一项任务', async () => {
  const first = render(<App />)
  await screen.findByRole('textbox', { name: '向团队发送消息' })
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBeNull()
  first.unmount()
  render(<App />)
  await screen.findByRole('button', { name: /^甲项目/ })
  expect(screen.getByRole('textbox', { name: '发起任务对话' })).toBeTruthy()
  expect(selected()).toBeNull()
  expect(posts()).toHaveLength(0)
})

test('缺失原群时不跳另一项目、不查询未知ID，用户仍可选择已有群', async () => {
  remember('task_missing')
  render(<App />)
  await screen.findByText(/上次的对话当前不可用/)
  expect(selected()).toBeNull()
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/tasks/task_missing')).toBe(false)
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBe('task_missing')
  fireEvent.click(screen.getByRole('button', { name: /^甲项目/ }))
  await waitFor(() => expect(selected()?.textContent).toContain('甲项目'))
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBe('task_alpha')
  expect(posts()).toHaveLength(0)
})

test('位置不可用不会吞掉既有草稿恢复失败的提示', async () => {
  remember('task_missing')
  window.sessionStorage.setItem(PAGE_RECOVERY_KEY, '{}')
  render(<App />)
  await screen.findByText(/上次的对话当前不可用/)
  expect(screen.getByText(/页面草稿恢复记录已失效/)).toBeTruthy()
  expect(selected()).toBeNull()
  expect(posts()).toHaveLength(0)
})

test('显式完整恢复快照优先，未知提交不自动重发', async () => {
  remember('task_beta')
  expect(await savePageRecovery({ drafts: { task_alpha: '需要人工核对的原始输入' }, selectedId: 'task_alpha',
    questionMode: false, derivation: null, project: null, requests: { conversation: { task_alpha: {
      text: '需要人工核对的原始输入', requestId: '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca' } }, questions: {}, derivations: {} } }, '', () => true)).toBe('')
  render(<App />)
  await waitFor(() => expect(selected()?.textContent).toContain('甲项目'))
  expect((screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement).value).toBe('需要人工核对的原始输入')
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBe('task_alpha')
  expect(posts()).toHaveLength(0)
})

test('初始列表迟到时，用户新建意图和输入优先，不恢复旧群', async () => {
  remember('task_alpha')
  let resolve!: (value: Response) => void
  handler = url => url === '/api/task-summaries' ? new Promise<Response>(done => { resolve = done }) : undefined
  render(<App />)
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === '/api/task-summaries')).toBe(true))
  fireEvent.click(screen.getByRole('button', { name: '新建任务' }))
  const draft = screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement
  fireEvent.change(draft, { target: { value: '新的未发送想法' } })
  await act(async () => resolve(json({ tasks: [summary('task_alpha'), summary('task_beta')] })))
  expect(selected()).toBeNull()
  expect(draft.value).toBe('新的未发送想法')
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBeNull()
  expect(posts()).toHaveLength(0)
})

test('身份绑定尚在准备时的新输入优先，初始化结束后只记录新建页位置', async () => {
  window.sessionStorage.setItem('cater.access_token', 'test-account')
  const binding = await accountBinding('test-account')
  remember('task_alpha', binding)
  const digest = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode('test-account'))
  let resolve!: (value: ArrayBuffer) => void
  vi.stubGlobal('crypto', { subtle: { digest: () => new Promise<ArrayBuffer>(done => { resolve = done }) } })
  render(<App />)
  const draft = screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement
  fireEvent.change(draft, { target: { value: '不应被旧记录覆盖' } })
  await act(async () => resolve(digest))
  await screen.findByRole('button', { name: /^甲项目/ })
  expect(selected()).toBeNull()
  expect(draft.value).toBe('不应被旧记录覆盖')
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBeNull()
  expect(posts()).toHaveLength(0)
})

test('StrictMode重放初始化仍恢复一次正确位置，不提交工作', async () => {
  remember('task_alpha')
  render(<StrictMode><App /></StrictMode>)
  await waitFor(() => expect(selected()?.textContent).toContain('甲项目'))
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBe('task_alpha')
  expect(posts()).toHaveLength(0)
})

test('相同标题的群仍按ID恢复，不以标题猜测上下文', async () => {
  remember('task_alpha')
  handler = url => {
    if (url === '/api/task-summaries') return json({ tasks: ['task_alpha', 'task_beta'].map(id => ({ ...summary(id),
      goal: '相同标题', team: { ...team, objective: '相同标题' } })) })
    if (/^\/api\/tasks\/task_(alpha|beta)$/.test(url)) {
      const original = detail(url.split('/')[3])
      return json({ ...original, task: { ...original.task, goal: '相同标题' }, team: { ...team, objective: '相同标题' } })
    }
  }
  render(<App />)
  await screen.findByRole('heading', { name: '相同标题', level: 1 })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/tasks/task_alpha')).toBe(true)
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/tasks/task_beta')).toBe(false)
  expect(JSON.parse(window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)!).selectedId).toBe('task_alpha')
  expect(posts()).toHaveLength(0)
})
