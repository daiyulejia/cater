import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TaskGraph from '../src/TaskGraph'
import type { TeamEvent, TeamState, TeamWithdrawal } from '../src/team'

const waiting = '本次处理结束，待后续信息'
const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Record<string, unknown>): TeamEvent => ({ seq, type, payload, actor: host })
const message = (seq: number, sender: string, recipient: string, kind = 'message', extra = {}): TeamEvent => ({
  seq, type: 'team.message_received', actor: { id: sender, kind: sender === 'human' ? 'human' : 'agent', role: sender },
  payload: { kind, recipient, text: `原文 ${seq}`, ...extra },
})
const withdrawal: TeamWithdrawal = { message_seq: 12, source_hash: 'a'.repeat(64), requester: 'manager', sender: 'manager',
  recipient: 'dev', leader_id: 'manager', revision: 3, reason: '改用另一方案', run_id: 'withdrawn', withdrawal_seq: 15 }
const team: TeamState = { enabled: true, objective: '核对交付', status: 'done', pending_count: 0, revision: 3,
  checkpoint: '', assignments: [], runs: [
    { message_seq: 4, role: 'dev', status: 'failed', reason: '历史检查失败', revision: 3 },
    { message_seq: 12, role: 'dev', status: 'failed', reason: '撤回前的真实失败', revision: 3 },
    { message_seq: 1, role: 'manager', status: 'waiting', reason: '首轮处理已结束', revision: 3 },
    { message_seq: 8, role: 'manager', status: 'waiting', reason: '后续信息尚未到来', revision: 3 },
  ], withdrawals: [withdrawal],
  reviews: [{ request_seq: 6, root_seq: 6, author: 'dev', reviewer: 'test', status: 'approved', revision: 3, artifacts: [] }] }
const events: TeamEvent[] = [message(1, 'human', 'manager'),
  event(2, 'team.run_started', { message_seq: 1, role: 'manager', run_id: 'first', revision: 3 }),
  event(3, 'team.objective_updated', { goal: team.objective, run_id: 'first' }),
  message(4, 'manager', 'dev', 'assignment'),
  event(5, 'team.run_finished', { message_seq: 4, role: 'dev', status: 'failed', reason: '历史检查失败' }),
  event(6, 'team.review_requested', { author: 'dev', reviewer: 'test', instruction: '核对这个版本' }),
  event(7, 'team.review_recorded', { review_seq: 6, root_seq: 6, reviewer: 'test', verdict: 'approved', feedback: '该版本证据充分' }),
  message(8, 'human', 'manager'),
  event(9, 'team.run_started', { message_seq: 8, role: 'manager', run_id: 'follow-up' }),
  event(10, 'team.run_finished', { message_seq: 8, role: 'manager', status: 'waiting', reason: '后续信息尚未到来' }),
  event(11, 'team.run_finished', { message_seq: 1, role: 'manager', status: 'waiting', reason: '首轮处理已结束' }),
  { ...message(12, 'manager', 'dev', 'assignment', { revision: 3 }), hash: withdrawal.source_hash },
  event(14, 'team.run_finished', { message_seq: 12, role: 'dev', status: 'failed', reason: '撤回前的真实失败' }),
  event(15, 'team.message_withdrawn', { ...withdrawal }),
  event(18, 'team.objective_completed', { run_id: 'follow-up' }),
]
const props = { team, events, actorText: (id: string) => id, open: true, onOpenChange: vi.fn() }
const details = () => screen.getByRole('complementary', { name: '协作记录详情' })
const node = (seq: number) => screen.getByRole('button', { name: new RegExp(`^[^#]+ #${seq}：`) })
const viewport = () => document.querySelector<HTMLElement>('.react-flow__viewport')!
const summary = () => within(document.querySelector<HTMLElement>('.task-graph-summary')!)
const processing = () => within(within(details()).getByRole('heading', { name: '最近一次处理记录', level: 4 }).closest('section')!)
const frame = () => act(async () => { await new Promise(resolve => requestAnimationFrame(resolve)) })

// Exercise real React Flow with the same jsdom layout primitives as the focus tests.
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

test('整体 done 与多轮消息 waiting 并存，图、列表和最近处理详情保持原语义且不改输入', async () => {
  const before = structuredClone({ team, events })
  render(<TaskGraph {...props} />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__node')).toHaveLength(6))
  expect(summary().getByText('当前任务 · 店长确认可交付')).toBeTruthy()
  expect(within(details()).queryByRole('heading', { name: '消息处理' })).toBeNull()
  for (const [seq, reason] of [[1, '首轮处理已结束'], [8, '后续信息尚未到来']] as const) {
    fireEvent.click(node(seq))
    expect(within(node(seq)).getByText(waiting)).toBeTruthy()
    expect(node(seq).getAttribute('aria-label')).not.toMatch(/已交接|等待伙伴|等待顾客|正在处理|消息处理完成/)
    expect(processing().getByText(waiting)).toBeTruthy()
    expect(processing().getByText(reason)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '协作列表' }))
    const row = within(screen.getByRole('list', { name: '当前团队协作列表' })).getByRole('button', { name: new RegExp(`消息 #${seq} `) })
    expect(within(row).getByText(waiting)).toBeTruthy()
    fireEvent.click(row)
    expect(within(details()).getByRole('heading', { name: `消息 #${seq}` })).toBeTruthy()
    expect(processing().getByText(waiting)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '协作图' }))
  }
  await frame()
  expect({ team, events }).toEqual(before)
})

test.each(['正在处理后续消息', '执行失败，需跟进', '已暂停', '已取消'])(
  '轮询传入 %s 时覆盖旧交付标签，保留原记录、选择、焦点和真实视口', async statusLabel => {
    const before = structuredClone({ team, events })
    const view = render(<TaskGraph {...props} />)
    await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
    fireEvent.click(node(8))
    const heading = within(details()).getByRole('heading', { name: '消息 #8' })
    await waitFor(() => expect(document.activeElement).toBe(heading))
    const initial = viewport().style.transform
    fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
    await waitFor(() => expect(viewport().style.transform).not.toBe(initial))
    await frame()
    heading.focus()
    const previousViewport = viewport(), transform = previousViewport.style.transform
    const poll = structuredClone({ team, events })
    view.rerender(<TaskGraph {...props} {...poll} statusLabel={statusLabel} />)
    await frame()
    expect(summary().getByText(`当前任务 · ${statusLabel}`)).toBeTruthy()
    expect(summary().queryByText('当前任务 · 店长确认可交付')).toBeNull()
    expect(node(8).getAttribute('aria-pressed')).toBe('true')
    expect(processing().getByText(waiting)).toBeTruthy()
    expect(within(details()).getByRole('heading', { name: '消息 #8' })).toBe(heading)
    expect(document.activeElement).toBe(heading)
    expect(viewport()).toBe(previousViewport)
    expect(viewport().style.transform).toBe(transform)
    fireEvent.click(screen.getByRole('button', { name: '协作列表' }))
    const row = within(screen.getByRole('list', { name: '当前团队协作列表' })).getByRole('button', { name: /消息 #8 / })
    expect(row.getAttribute('aria-pressed')).toBe('true')
    expect(within(row).getByText(waiting)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '协作图' }))
    expect(viewport()).toBe(previousViewport)
    expect(viewport().style.transform).toBe(transform)
    expect(poll).toEqual(before)
    expect({ team, events }).toEqual(before)
  },
)

test('当前暂停不覆盖历史失败、复核结论和撤回，图与列表详情仍显示原始处理原因', async () => {
  const before = structuredClone({ team, events })
  render(<TaskGraph {...props} statusLabel="已暂停" />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
  for (const [seq, label, reason] of [[4, '消息处理失败', '历史检查失败'], [6, '该版本复核通过', undefined],
    [7, '该版本复核通过', '该版本证据充分'], [12, '工作已撤回', '撤回前的真实失败']] as const) {
    fireEvent.click(node(seq))
    expect(within(node(seq)).getByText(label)).toBeTruthy()
    expect(within(details()).getAllByText(label).length).toBeGreaterThan(0)
    if (reason) expect(within(details()).getByText(reason)).toBeTruthy()
    if (seq === 12) {
      expect(processing().getByText('消息处理失败')).toBeTruthy()
      expect(within(details()).getByText('原因：改用另一方案')).toBeTruthy()
      expect(within(details()).getByText('撤回事件 #15')).toBeTruthy()
    }
    fireEvent.click(screen.getByRole('button', { name: '协作列表' }))
    const row = within(screen.getByRole('list', { name: '当前团队协作列表' })).getByRole('button', { name: new RegExp(` #${seq} `) })
    expect(within(row).getByText(label)).toBeTruthy()
    fireEvent.click(row)
    expect(within(details()).getAllByText(label).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: '协作图' }))
    expect(summary().getByText('当前任务 · 已暂停')).toBeTruthy()
  }
  await frame()
  expect({ team, events }).toEqual(before)
})
