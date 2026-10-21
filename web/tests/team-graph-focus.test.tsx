import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TaskGraph from '../src/TaskGraph'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent => ({ seq, type, payload, actor })
const message = (seq: number, sender: string, recipient: string, kind = 'message', extra = {}) => event(seq, 'team.message_received',
  { kind, recipient, text: `原文 ${seq}`, ...extra }, { id: sender, kind: sender === 'human' ? 'human' : 'agent', role: sender })
const team: TeamState = { enabled: true, objective: '当前目标', status: 'running', pending_count: 0, runs: [],
  assignments: [], checkpoint: '', revision: 13,
  reviews: [{ request_seq: 3, root_seq: 3, author: 'dev', reviewer: 'test', status: 'approved', revision: 1, artifacts: [] }] }
const events: TeamEvent[] = [message(1, 'human', 'manager'), message(2, 'manager', 'dev', 'assignment'),
  event(3, 'team.review_requested', { author: 'dev', reviewer: 'test', instruction: '旧版复核' }),
  event(4, 'team.review_recorded', { review_seq: 3, root_seq: 3, reviewer: 'test', verdict: 'approved', feedback: '旧版已批准' }),
  message(10, 'human', 'manager'), event(11, 'team.run_started', { run_id: 'current', message_seq: 10, role: 'manager' }),
  event(13, 'team.objective_updated', { goal: '当前目标', run_id: 'current' }),
  event(15, 'team.tool_started', { role: 'manager', run_id: 'current', message_seq: 10, name: 'delegate' }),
  message(16, 'manager', 'dev', 'assignment'),
  event(17, 'team.tool_finished', { role: 'manager', run_id: 'current', started_seq: 15, result: { status: 'ok', message_seq: 16 } }),
  message(18, 'dev', 'manager', 'report', { reply_to: 16 })]
const props = { team, events, actorText: (id: string) => id, open: true, onOpenChange: vi.fn() }
const details = () => screen.getByRole('complementary', { name: '协作记录详情' })
const viewport = () => document.querySelector<HTMLElement>('.react-flow__viewport')!
const node = (seq: number) => screen.getByRole('button', { name: new RegExp(`^[^#]+ #${seq}：`) })
const frame = () => act(async () => { await new Promise(resolve => requestAnimationFrame(resolve)) })

// Exercise real React Flow; substitute only the layout primitives missing from jsdom.
beforeEach(() => {
  Object.defineProperty(SVGElement.prototype, 'getBBox', { configurable: true, value: () => new DOMRect(0, 0, 60, 18) })
  const dimensions = (element: HTMLElement) => element.classList.contains('react-flow__handle') ? [6, 6]
    : element.classList.contains('react-flow__node') ? [248, 160] : [880, 560]
  for (const property of ['offsetWidth', 'clientWidth'] as const) {
    vi.spyOn(HTMLElement.prototype, property, 'get').mockImplementation(function () { return dimensions(this)[0] })
  }
  for (const property of ['offsetHeight', 'clientHeight'] as const) {
    vi.spyOn(HTMLElement.prototype, property, 'get').mockImplementation(function () { return dimensions(this)[1] })
  }
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    const [width, height] = dimensions(this)
    return new DOMRect(this.classList.contains('react-flow__handle-right') ? 245 : 0,
      this.classList.contains('react-flow__handle') ? 77 : 0, width, height)
  })
  vi.stubGlobal('DOMMatrixReadOnly', class {
    m22: number
    constructor(transform: string) { this.m22 = Number(/scale\(([^)]+)\)/.exec(transform)?.[1] ?? 1) }
  })
  vi.stubGlobal('ResizeObserver', class {
    targets = new Set<Element>()
    pending = false
    callback: ResizeObserverCallback
    constructor(callback: ResizeObserverCallback) { this.callback = callback }
    observe(target: Element) {
      this.targets.add(target)
      if (this.pending) return
      this.pending = true
      queueMicrotask(() => {
        this.pending = false
        if (this.targets.size) this.callback([...this.targets].map(element => ({ target: element,
          contentRect: element.getBoundingClientRect() } as ResizeObserverEntry)), this as unknown as ResizeObserver)
      })
    }
    unobserve(target: Element) { this.targets.delete(target) }
    disconnect() { this.targets.clear() }
  })
})
afterEach(() => { delete (SVGElement.prototype as SVGElement & { getBBox?: () => DOMRect }).getBBox })

test('默认当前目标，完整旧输入可切回；范围、图、列表共用投影及详情，旧批准不能作为当前完成', async () => {
  render(<TaskGraph {...props} />)
  expect(screen.getByRole('dialog', { name: '任务脉络' })).toBeTruthy()
  expect(screen.getByRole('button', { name: '当前目标' }).getAttribute('aria-pressed')).toBe('true')
  expect(screen.getByText('当前目标 · 3 / 7 条协作记录 · 只读')).toBeTruthy()
  expect(screen.getByText('目标更新 #13 · 顾客输入 #10')).toBeTruthy()
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(2))
  expect(screen.queryByRole('button', { name: /^消息 #1：/ })).toBeNull()
  expect(within(details()).getByRole('heading', { name: '消息 #10' })).toBeTruthy()
  const focusedViewport = viewport()
  fireEvent.click(screen.getByRole('button', { name: '全部记录' }))
  await waitFor(() => expect(document.querySelectorAll('.react-flow__node')).toHaveLength(7))
  expect(viewport()).not.toBe(focusedViewport)
  fireEvent.click(node(1))
  expect(within(details()).getByText('原文 1')).toBeTruthy()
  expect(within(details()).getByText('旧目标记录，不作为当前目标完成依据')).toBeTruthy()
  fireEvent.click(node(4))
  expect(within(details()).getByText('历史复核')).toBeTruthy()
  expect(within(details()).getByText('旧版已批准')).toBeTruthy()
  expect(within(details()).queryByText('该版本复核通过')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '协作列表' }))
  expect(within(screen.getByRole('list', { name: '当前团队协作列表' })).getAllByRole('listitem')).toHaveLength(7)
  fireEvent.click(screen.getByRole('button', { name: '当前目标' }))
  expect(within(screen.getByRole('list', { name: '当前团队协作列表' })).getAllByRole('listitem')).toHaveLength(3)
  expect(within(details()).getByRole('heading', { name: '消息 #10' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '协作图' }))
  expect(node(10).getAttribute('aria-pressed')).toBe('true')
  await frame()
})

test('状态轮询保留选择、焦点和缩放，图列表切换不重置视口；同目标新增记录不重挂图', async () => {
  const view = render(<TaskGraph {...props} />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(2))
  fireEvent.click(node(16))
  const heading = within(details()).getByRole('heading', { name: '委托 #16' })
  await waitFor(() => expect(document.activeElement).toBe(heading))
  fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
  await frame()
  heading.focus()
  const previousViewport = viewport(), transform = viewport().style.transform
  view.rerender(<TaskGraph {...props} team={{ ...team, runs: [{ message_seq: 16, role: 'dev', status: 'failed', reason: '真实失败' }] }}
    events={[...events, event(19, 'team.run_finished', { message_seq: 16, role: 'dev', status: 'failed', reason: '真实失败' })]} />)
  expect(node(16).getAttribute('aria-pressed')).toBe('true')
  expect(within(details()).getByText('真实失败')).toBeTruthy()
  expect(document.activeElement).toBe(heading)
  expect(viewport()).toBe(previousViewport)
  expect(viewport().style.transform).toBe(transform)
  fireEvent.click(screen.getByRole('button', { name: '协作列表' }))
  const list = screen.getByRole('list', { name: '当前团队协作列表' })
  expect(within(list).getByRole('button', { name: /委托 #16/ }).getAttribute('aria-pressed')).toBe('true')
  fireEvent.click(screen.getByRole('button', { name: '协作图' }))
  expect(viewport()).toBe(previousViewport)
  expect(viewport().style.transform).toBe(transform)
  view.rerender(<TaskGraph {...props} events={[...events, message(20, 'human', 'test')]} />)
  expect(node(20)).toBeTruthy()
  expect(node(16).getAttribute('aria-pressed')).toBe('true')
  expect(viewport()).toBe(previousViewport)
  expect(viewport().style.transform).toBe(transform)
})

test('目标更新重新适配并选择新输入；所选节点消失后详情和关联不会残留或复活旧选择', async () => {
  const view = render(<TaskGraph {...props} />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(2))
  fireEvent.click(node(18))
  const previousViewport = viewport()
  const next = [...events, message(20, 'human', 'test'),
    event(21, 'team.run_started', { run_id: 'next', role: 'test', message_seq: 20 }),
    event(22, 'team.objective_updated', { run_id: 'next', goal: '下一目标' }), message(23, 'test', 'dev', 'assignment')]
  view.rerender(<TaskGraph {...props} team={{ ...team, objective: '下一目标', revision: 22 }} events={next} />)
  expect(screen.getByText('目标更新 #22 · 顾客输入 #20')).toBeTruthy()
  expect(viewport()).not.toBe(previousViewport)
  expect(within(details()).getByRole('heading', { name: '消息 #20' })).toBeTruthy()
  fireEvent.click(node(23))
  expect(within(details()).getByRole('heading', { name: '委托 #23' })).toBeTruthy()
  view.rerender(<TaskGraph {...props} events={next.filter(entry => entry.seq !== 23)} />)
  expect(within(details()).getByRole('heading', { name: '消息 #20' })).toBeTruthy()
  expect(within(details()).queryByText('原文 23')).toBeNull()
  view.rerender(<TaskGraph {...props} events={next} />)
  expect(node(20).getAttribute('aria-pressed')).toBe('true')
  expect(node(23).getAttribute('aria-pressed')).toBe('false')
  await frame()
})

test('直接引用的历史上下文早于当前输入时，默认详情仍选择已验证的当前顾客输入', async () => {
  render(<TaskGraph {...props} events={[...events,
    message(19, 'manager', 'dev', 'continuation', { continue_assignment_seq: 2, visible: true })]} />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(3))
  expect(node(2)).toBeTruthy()
  expect(within(details()).getByRole('heading', { name: '消息 #10' })).toBeTruthy()
  fireEvent.click(node(2))
  expect(within(details()).getByText('旧目标记录，不作为当前目标完成依据')).toBeTruthy()
  expect(within(details()).getByRole('button', { name: '接续原委托 · 接续交接 #19' })).toBeTruthy()
  await frame()
})

test('无目标显示完整记录，无法匹配来源时不拿最近旧输入冒充，空范围能切回历史，空事件不造节点', async () => {
  const old = events.slice(0, 2)
  const view = render(<TaskGraph {...props} events={old} />)
  expect(screen.getByText('尚无目标更新记录，显示全部记录。')).toBeTruthy()
  expect(node(1)).toBeTruthy()
  view.rerender(<TaskGraph {...props} events={[...old, event(30, 'team.objective_updated', { goal: '待处理' })]} />)
  expect(screen.getByText('目标更新 #30 · 触发顾客输入的关联待核对')).toBeTruthy()
  expect(screen.getByText('当前目标尚无协作记录，可切换全部记录查看历史。')).toBeTruthy()
  expect(screen.queryByRole('complementary', { name: '协作记录详情' })).toBeNull()
  expect(document.querySelectorAll('.react-flow__node')).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: '全部记录' }))
  expect(node(1)).toBeTruthy()
  expect(within(details()).getByText('原文 1')).toBeTruthy()
  view.rerender(<TaskGraph {...props} events={[]} />)
  expect(screen.getByText('尚无协作消息或复核记录。')).toBeTruthy()
  expect(document.querySelectorAll('.react-flow__node')).toHaveLength(0)
})

test('目标先到而消息随后同步时，空图适配首批记录并选择已验证输入', async () => {
  const view = render(<TaskGraph {...props} events={[...events.slice(0, 4), events[6]]} />)
  expect(screen.getByText('当前目标尚无协作记录，可切换全部记录查看历史。')).toBeTruthy()
  const emptyViewport = viewport()
  view.rerender(<TaskGraph {...props} />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(2))
  expect(viewport()).not.toBe(emptyViewport)
  expect(within(details()).getByRole('heading', { name: '消息 #10' })).toBeTruthy()
  expect(screen.getByText('目标更新 #13 · 顾客输入 #10')).toBeTruthy()
  expect(viewport().style.transform).not.toBe('translate(0px,0px) scale(1)')
})

test('全部记录中追加新目标但节点未变时，保留有效历史选择、焦点与缩放', async () => {
  const view = render(<TaskGraph {...props} />)
  fireEvent.click(screen.getByRole('button', { name: '全部记录' }))
  await waitFor(() => expect(document.querySelectorAll('.react-flow__node')).toHaveLength(7))
  fireEvent.click(node(16))
  const heading = within(details()).getByRole('heading', { name: '委托 #16' })
  await waitFor(() => expect(document.activeElement).toBe(heading))
  fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
  await frame()
  heading.focus()
  const previousViewport = viewport(), transform = viewport().style.transform
  view.rerender(<TaskGraph {...props} team={{ ...team, objective: '新的方向', revision: 30 }}
    events={[...events, event(30, 'team.objective_updated', { goal: '新的方向' })]} />)
  expect(node(16).getAttribute('aria-pressed')).toBe('true')
  expect(viewport()).toBe(previousViewport)
  expect(viewport().style.transform).toBe(transform)
  expect(document.activeElement).toBe(heading)
  expect(within(details()).getByText('旧目标记录，不作为当前目标完成依据')).toBeTruthy()
})
