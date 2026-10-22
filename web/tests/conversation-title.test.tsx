import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { TaskDetail } from '../src/App'
import { conversationTitle } from '../src/conversationTitle'
import { TaskList } from '../src/TaskList'
import { detail, event, leader, members } from './native-handoff.fixtures'

const longGoal = '修复工程里的total_cents：输入是以分为单位的整数列表，负数表示退款，也要计入合计；空列表返回0，不修改输入。先运行已有测试保留失败证据，再修复，保留原四个测试方法原文，可以追加，更新中文README。请伙伴独立复核后交付完整工程ZIP。告诉我最短使用/测试方法、旧实现为何错、验证到了什么以及没有验证什么。详细说明里附实际运行记录，失败与成功分别按真实回执说明；不要让我照着猜目录或把Cater工具字段当终端参数。'
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const makeTask = (id: string, goal: string, revision = 1): TaskDetail => {
  const value = detail([event(revision, 'team.objective_updated', { goal })], id)
  value.task.goal = goal
  value.task.team = { ...value.task.team!, objective: goal, revision }
  return value
}
const summary = ({ task, events }: TaskDetail) => ({ id: task.id, goal: task.goal, status: task.status,
  status_label: '已完成', conversation_count: events.length, latest_seq: task.team!.revision,
  version: task.team!.revision!, team: task.team })
const fullGoal = () => screen.getByRole('region', { name: '当前完整目标' }).querySelector('p')!.textContent

beforeEach(() => window.sessionStorage.clear())

test('短目标原样显示，长真实目标仅作诚实开头节选而非语义摘要', () => {
  expect(conversationTitle('搭建猫咖网页')).toEqual({ label: '搭建猫咖网页', excerpt: false })
  expect(conversationTitle('核对入口')).toEqual({ label: '核对入口', excerpt: false })
  expect(conversationTitle(longGoal)).toEqual({ label: '修复工程里的total_cents…', excerpt: true })
  expect(conversationTitle('请' + longGoal).label).toBe('请修复工程里的total_cents…')
  expect(conversationTitle('')).toEqual({ label: '', excerpt: false })
})

test('只规范展示空白，首行以外内容明确省略，短单行标点不被无谓切开', () => {
  expect(conversationTitle(' \r\n  修复  接口\r\n  保留兼容\n')).toEqual({ label: '修复 接口…', excerpt: true })
  expect(conversationTitle('修复接口：保留兼容')).toEqual({ label: '修复接口：保留兼容', excerpt: false })
  expect(conversationTitle(' \r\n 核对入口 \n ')).toEqual({ label: '核对入口', excerpt: false })
})

test.each(['C:\\project\\version.2\\very-long-file-name.txt', 'https://example.test/a/very-long-path?key=value'])('不按 ASCII 标点猜任务含义：%s', goal => {
  const parts = Array.from(new Intl.Segmenter('zh', { granularity: 'grapheme' }).segment(goal), part => part.segment)
  expect(conversationTitle(goal)).toEqual({ label: parts.slice(0, 28).join('') + '…', excerpt: true })
})

test.each(['👩🏽‍💻', 'e\u0301', '🇨🇳'])('28 字素预算不切坏组合字符：%s', grapheme => {
  expect(conversationTitle(grapheme.repeat(29))).toEqual({ label: grapheme.repeat(28) + '…', excerpt: true })
  expect(conversationTitle(grapheme.repeat(28))).toEqual({ label: grapheme.repeat(28), excerpt: false })
})

test('没有 Segmenter 时保守保留全部原文，不拿码点或 UTF-16 模拟字素', () => {
  vi.stubGlobal('Intl', { Segmenter: undefined })
  const goal = '👩🏽‍💻e\u0301'.repeat(40) + '\r\n全文保留'
  expect(conversationTitle(goal)).toEqual({ label: goal, excerpt: false })
})

test('侧栏保留全文尾部/id搜索、同前缀身份、草稿与状态筛选，没有第二全文入口', () => {
  const onChoose = vi.fn()
  const tasks = [{ id: 'first-id', goal: longGoal, status: 'done' },
    { id: 'second-id', goal: longGoal + '附加要求', status: 'running' }]
  render(<TaskList tasks={tasks} selectedId="first-id" drafts={{ 'second-id': '保留草稿' }} collapsed={false}
    searchOpen onChoose={onChoose} statusLabel={task => task.status === 'done' ? '已完成' : '进行中'} />)
  const nav = within(screen.getByRole('navigation', { name: '任务列表' }))
  expect(nav.getAllByRole('button')).toHaveLength(2)
  const search = screen.getByRole('searchbox')
  fireEvent.change(search, { target: { value: '附加要求' } })
  const second = nav.getByRole('button')
  expect(within(second).getByRole('img', { name: '有未发送内容' })).toBeTruthy()
  fireEvent.click(second)
  expect(onChoose).toHaveBeenCalledWith('second-id')
  fireEvent.change(search, { target: { value: 'FIRST-ID' } })
  expect(nav.getByRole('button').getAttribute('aria-current')).toBe('page')
  fireEvent.change(search, { target: { value: '不要让我照着猜目录' } })
  expect(nav.getAllByRole('button')).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: '已完成', exact: true }))
  expect(nav.getAllByRole('button')).toHaveLength(1)
  expect(screen.queryByRole('button', { name: '查看完整目标' })).toBeNull()
})

async function mount(tasks: TaskDetail[], override?: (url: string) => Response | Promise<Response> | undefined) {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    const result = override?.(url)
    if (result) return result
    if (url === '/api/task-summaries') return json({ tasks: tasks.map(summary) })
    const task = tasks.find(task => url === `/api/tasks/${task.task.id}`)
    if (task) return json(task)
    if (url === '/api/organization') return json([leader, ...members])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  const app = render(<App />)
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  return { ...app, fetchMock }
}

test('实际 App 页头默认短节选，唯一全文展开保留当前原文、草稿、滚动且不请求 API', async () => {
  const original = makeTask('real-long', longGoal)
  const saved = JSON.stringify(original)
  const { fetchMock } = await mount([original])
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('修复工程里的total_cents…')
  expect(screen.getByText('目标节选', { exact: true })).toBeTruthy()
  expect(screen.queryByRole('region', { name: '当前完整目标' })).toBeNull()
  const toggle = screen.getByRole('button', { name: '查看完整目标' })
  expect(screen.getAllByRole('button', { name: '查看完整目标' })).toHaveLength(1)
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(toggle.getAttribute('type')).toBe('button')
  const input = screen.getByRole('textbox', { name: '向团队发送消息' }) as HTMLTextAreaElement
  fireEvent.change(input, { target: { value: '尚未发送的草稿' } })
  const stream = screen.getByLabelText('聊天记录')
  stream.scrollTop = 35
  fireEvent.scroll(stream)
  const count = fetchMock.mock.calls.length
  fireEvent.click(toggle)
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  expect(fullGoal()).toBe(longGoal)
  expect(screen.getByRole('region', { name: '当前完整目标' }).id).toBe(toggle.getAttribute('aria-controls'))
  expect(screen.getByText('任务编号：real-long')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '收起完整目标' }))
  expect(screen.queryByRole('region', { name: '当前完整目标' })).toBeNull()
  expect(input.value).toBe('尚未发送的草稿')
  expect(stream.scrollTop).toBe(35)
  expect(fetchMock.mock.calls).toHaveLength(count)
  expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true)
  expect(JSON.stringify(original)).toBe(saved)
})

test('切任务收起全文；短名字保留，全文以纯文本显示 HTML/Markdown 和原始换行', async () => {
  const dangerous = '  <img src=x onerror=alert(1)>\r\n[跳转](javascript:alert(1))\n' + '长行'.repeat(200)
  await mount([makeTask('plain', dangerous), makeTask('short', '核对入口')])
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('核对入口')
  expect(screen.queryByText('目标节选', { exact: true })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看完整目标' }))
  expect(fullGoal()).toBe('核对入口')
  const nav = within(screen.getByRole('navigation', { name: '任务列表' }))
  fireEvent.click(nav.getByRole('button', { name: /目标节选/ }))
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  expect(screen.queryByRole('region', { name: '当前完整目标' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看完整目标' }))
  expect(fullGoal()).toBe(dangerous)
  expect(screen.getByRole('region', { name: '当前完整目标' }).querySelector('a, img, script')).toBeNull()
  fireEvent.click(nav.getByRole('button', { name: /核对入口/ }))
  expect(screen.queryByRole('region', { name: '当前完整目标' })).toBeNull()
})

test('新摘要先到、旧详情迟到时，页头节选与全文同取当前目标，不重用初始目标', async () => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  const old = makeTask('race-goal', '旧目标', 1)
  const nextGoal = '新目标：' + '按当前修订完整核对'.repeat(10)
  const next = makeTask('race-goal', nextGoal, 20)
  let stage = 0
  const late: ((response: Response) => void)[] = []
  await mount([old], url => {
    if (!stage) return undefined
    if (url === '/api/task-summaries') return json({ tasks: [summary(next)] })
    if (url === '/api/tasks/race-goal') return new Promise<Response>(resolve => late.push(resolve))
  })
  fireEvent.click(screen.getByRole('button', { name: '查看完整目标' }))
  expect(fullGoal()).toBe('旧目标')
  stage = 1
  await act(async () => document.dispatchEvent(new Event('visibilitychange')))
  await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('新目标…'))
  expect(fullGoal()).toBe(nextGoal)
  await waitFor(() => expect(late.length).toBeGreaterThan(0))
  await act(async () => late[0](json(old)))
  expect(fullGoal()).toBe(nextGoal)
  expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('新目标…')
})
