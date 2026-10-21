import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ReactFlowProvider, useReactFlow } from '@xyflow/react'
import type { ReactFlowInstance } from '@xyflow/react'
import { readFileSync } from 'node:fs'
import { useEffect } from 'react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { TeamGraphBody } from '../src/TeamGraphBody'
import { teamGraphMotion } from '../src/graphMotion'
import { projectTeamGraph } from '../src/teamGraph'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Record<string, unknown>): TeamEvent => ({ seq, type, payload, actor: host })
const message = (seq: number, sender: string, recipient: string, extra = {}): TeamEvent => ({ seq,
  type: 'team.message_received', actor: { id: sender, kind: 'agent', role: sender },
  payload: { kind: 'assignment', recipient, revision: 1, text: `记录 ${seq}`, ...extra } })
const team: TeamState = { enabled: true, objective: '协作', status: 'running', pending_count: 0, revision: 1,
  checkpoint: '', assignments: [], runs: [{ message_seq: 4, role: 'dev', status: 'running', revision: 1 }] }
const events = [message(2, 'manager', 'test'), message(4, 'test', 'dev', { parent_assignment_seq: 2 }),
  event(5, 'team.run_started', { message_seq: 4, role: 'dev', run_id: 'work', revision: 1 })]
const motion = (state = team, input = events, allowed = true) => teamGraphMotion(state, input, projectTeamGraph(state, input), allowed)

test('只向当前正在处理的接收节点和已验证的入边赋予动画，不改原始数据', () => {
  const before = structuredClone({ team, events })
  expect([...motion().nodes]).toEqual([['event-4', 'running']])
  expect([...motion().edges]).toEqual(['2:4:子委托'])
  expect(motion({ ...team, runs: [] }).nodes.get('event-4')).toBe('running')
  expect({ team, events }).toEqual(before)
})

test.each(['pending', 'queued', 'waiting', 'paused', 'failed', 'blocked', 'interrupted', 'uncertain', 'cancelled', 'done', 'completed', 'finished', 'succeeded', 'unknown'])(
  '%s 快照不会被旧的开始事件复活动画', status => {
    const state = { ...team, runs: [{ ...team.runs[0], status }] }
    expect(motion(state).nodes.size).toBe(0)
    expect(motion(state).edges.size).toBe(0)
  },
)

test.each([undefined, false])('motionAllowed=%s 时旧 running 快照与事件均不能启动动画', allowed => {
  const result = teamGraphMotion(team, events, projectTeamGraph(team, events), allowed)
  expect(result.nodes.size).toBe(0)
  expect(result.edges.size).toBe(0)
})

test.each([
  { executionState: 'paused', active: true },
  { executionState: 'cancelled', active: true },
  { executionState: 'running', active: false },
  { executionState: 'running', active: undefined },
])('控制状态 $executionState / active=$active 时不声称仍在执行', ({ executionState, active }) => {
  const allowed = executionState === 'running' && active === true
  expect(motion(team, events, allowed).nodes.size).toBe(0)
  expect(motion(team, events, allowed).edges.size).toBe(0)
})

test('整体终态默认静止，明确允许的后续运行仍须有节点运行证据', () => {
  const input = [...events, event(6, 'team.objective_completed', {}),
    { ...message(7, 'human', 'dev', { kind: 'message', revision: 6 }), actor: { id: 'human', kind: 'human' } },
    event(8, 'team.run_started', { message_seq: 7, role: 'dev', revision: 6, run_id: 'follow-up' })]
  const done = { ...team, status: 'done', revision: 6,
    runs: [...team.runs, { message_seq: 7, role: 'dev', revision: 6, status: 'running' }] }
  expect(teamGraphMotion(done, input, projectTeamGraph(done, input)).nodes.size).toBe(0)
  expect([...motion(done, input, true).nodes]).toEqual([['event-7', 'running']])
  expect(motion({ ...done, runs: [] }, input.slice(0, -1), true).nodes.size).toBe(0)
})

test.each(['waiting', 'failed', 'done', undefined])('可信结束回执 %s 立即关闭动画，即使快照仍运行', status => {
  const input = [...events, event(6, 'team.run_finished', { message_seq: 4, role: 'dev', run_id: 'work', status })]
  expect(motion(team, input).nodes.size).toBe(0)
})

test('完成事件或同运行结束、提问回执先于快照到达时，也不会冒充执行', () => {
  expect(motion(team, [...events, event(6, 'team.objective_completed', {})]).nodes.size).toBe(0)
  expect(motion(team, [...events, event(6, 'team.run_finished', { run_id: 'work' })]).nodes.size).toBe(0)
  expect(motion(team, [...events, event(6, 'team.questions_requested', {
    run_id: 'work', role_id: 'dev', message_seq: 4,
  })]).nodes.size).toBe(0)
})

test('忽略伪造开始和错误接收者；过期 revision、歧义快照和来源不会触发动画', () => {
  const spoofed = { ...events[2], actor: { id: 'dev', kind: 'agent', role: 'team' } }
  expect(motion({ ...team, runs: [] }, [...events.slice(0, 2), spoofed]).nodes.size).toBe(0)
  expect(motion({ ...team, runs: [{ ...team.runs[0], role: 'test' }] }, events.slice(0, 2)).nodes.size).toBe(0)
  expect(motion({ ...team, runs: [{ ...team.runs[0], revision: 0 }] }).nodes.size).toBe(0)
  expect(motion({ ...team, runs: [...team.runs, ...team.runs] }).nodes.size).toBe(0)
  expect(motion(team, [events[0], message(4, 'test', 'dev', { revision: 0 }), events[2]]).nodes.size).toBe(0)
  const stale = { ...team, revision: 10, runs: [] }
  expect(motion(stale, [message(4, 'test', 'dev', { revision: undefined }),
    event(5, 'team.run_started', { message_seq: 4, role: 'dev', run_id: 'old' })]).nodes.size).toBe(0)
})

test('同一运行的 wait_for_reply 表示等待，其他运行的等待不影响当前节点', () => {
  const tool = event(6, 'team.tool_started', { message_seq: 4, role: 'dev', run_id: 'work', name: 'wait_for_reply' })
  expect(motion(team, [...events, tool]).nodes.size).toBe(0)
  expect(motion(team, [...events, { ...tool, payload: { ...tool.payload, run_id: 'other' } }]).nodes.size).toBe(1)
})

test('历史记录保持静止且不产生历史流动，当前目标的触发输入可以保留实际执行', () => {
  const input: TeamEvent[] = [
    { ...message(2, 'human', 'manager', { kind: 'message', revision: undefined }), actor: { id: 'human', kind: 'human' } },
    event(3, 'team.run_started', { message_seq: 2, role: 'manager', run_id: 'adopt', revision: 1 }),
    event(6, 'team.objective_updated', { run_id: 'adopt', goal: '当前目标' }),
    message(7, 'manager', 'dev', { revision: 6 }),
  ]
  const state = { ...team, revision: 6, runs: [
    { message_seq: 2, role: 'manager', revision: 1, status: 'running' },
    { message_seq: 7, role: 'dev', revision: 6, status: 'started' },
  ] }
  expect([...motion(state, input).nodes.keys()]).toEqual(['event-2', 'event-7'])
  expect(motion({ ...team, revision: 6 }, [...events, input[2]]).nodes.size).toBe(0)
})

test('撤回节点及其出边保持静止，已独立运行的子工作保留执行状态', () => {
  const graph = projectTeamGraph(team, events)
  graph.nodes[0].withdrawal = { message_seq: 2, source_hash: 'a'.repeat(64), requester: 'manager', sender: 'manager',
    recipient: 'test', leader_id: 'manager', revision: 1, reason: '更换方案', run_id: 'withdraw', withdrawal_seq: 6 }
  const result = teamGraphMotion(team, events, graph, true)
  expect(result.nodes.get('event-4')).toBe('running')
  expect(result.edges.size).toBe(0)
})

test('接续动画要求精确原委托关联和接收者实际开始处理，投递本身不启动动画', () => {
  const input = [message(2, 'manager', 'dev'), message(4, 'manager', 'dev', {
    kind: 'continuation', visible: true, continue_assignment_seq: 2,
  })]
  expect(motion({ ...team, runs: [] }, input).nodes.size).toBe(0)
  expect(motion(team, input).nodes.get('event-4')).toBe('handoff')
  expect([...motion(team, input).edges]).toEqual(['2:4:接续原委托'])
  expect(motion(team, [input[0], { ...input[1], payload: { ...input[1].payload, continue_assignment_seq: 99 } }]).nodes.size).toBe(0)
})

let flow: ReactFlowInstance
function Probe() {
  const instance = useReactFlow()
  useEffect(() => { flow = instance }, [instance])
  return null
}
const portrait = vi.fn((id: string, _size: 'sm' | 'md') => <span data-portrait={id} />)
function Graph({ state = team, input = events, label, allowed }: { state?: TeamState; input?: TeamEvent[]; label?: string; allowed?: boolean }) {
  return <ReactFlowProvider><Probe /><TeamGraphBody team={state} events={input} actorText={id => id} portrait={portrait} statusLabel={label} motionAllowed={allowed} /></ReactFlowProvider>
}
const node = () => screen.getByRole('button', { name: /^委托 #4：/ })
const frame = () => act(async () => { await new Promise(resolve => requestAnimationFrame(resolve)) })

// Keep React Flow real; supply only the layout APIs jsdom lacks.
let bbox: PropertyDescriptor | undefined
beforeEach(() => {
  portrait.mockClear()
  bbox = Object.getOwnPropertyDescriptor(SVGElement.prototype, 'getBBox')
  Object.defineProperty(SVGElement.prototype, 'getBBox', { configurable: true, value: () => new DOMRect(0, 0, 60, 18) })
  const dimensions = (element: HTMLElement) => element.classList.contains('react-flow__handle') ? [6, 6]
    : element.classList.contains('react-flow__node') ? [248, 160] : [880, 560]
  for (const property of ['offsetWidth', 'clientWidth', 'offsetHeight', 'clientHeight'] as const) {
    vi.spyOn(HTMLElement.prototype, property, 'get').mockImplementation(function () {
      return dimensions(this)[property.endsWith('Width') ? 0 : 1]
    })
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
    constructor(private callback: ResizeObserverCallback) {}
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
afterEach(() => {
  if (bbox) Object.defineProperty(SVGElement.prototype, 'getBBox', bbox)
  else delete (SVGElement.prototype as SVGElement & { getBBox?: () => DOMRect }).getBBox
})

test('真实 ReactFlow 轮询只更新动画，保留节点坐标、用户 zoom/pan、选择和键盘焦点', async () => {
  const view = render(<Graph allowed />)
  await waitFor(() => expect(document.querySelectorAll('.team-graph-edge-live')).toHaveLength(1))
  expect(node().getAttribute('data-motion')).toBe('running')
  expect(node().querySelector('[data-portrait="dev"]')).toBeTruthy()
  expect(portrait).toHaveBeenCalledWith('dev', 'md')
  fireEvent.click(node())
  const heading = within(screen.getByRole('complementary', { name: '协作记录详情' })).getByRole('heading', { name: '委托 #4' })
  await waitFor(() => expect(document.activeElement).toBe(heading))
  await act(async () => { await flow.setViewport({ x: 143, y: -67, zoom: 0.72 }, { duration: 0 }) })
  const viewport = document.querySelector<HTMLElement>('.react-flow__viewport')!
  const transform = viewport.style.transform
  const positions = flow.getNodes().map(({ id, position }) => ({ id, position }))
  for (const status of ['waiting', 'failed', 'running']) {
    view.rerender(<Graph allowed state={{ ...team, runs: [{ ...team.runs[0], status }] }} input={structuredClone(events)} />)
    await frame()
    expect(node().getAttribute('data-motion')).toBe(status === 'running' ? 'running' : 'idle')
    expect(document.querySelectorAll('.team-graph-edge-live')).toHaveLength(status === 'running' ? 1 : 0)
    expect(flow.getNodes().map(({ id, position }) => ({ id, position }))).toEqual(positions)
    expect(document.querySelector('.react-flow__viewport')).toBe(viewport)
    expect(viewport.style.transform).toBe(transform)
    expect(node().getAttribute('aria-pressed')).toBe('true')
    expect(document.activeElement).toBe(heading)
  }
  for (const allowed of [false, undefined, true]) {
    view.rerender(<Graph allowed={allowed} label="文案不参与动画控制" />)
    await frame()
    expect(node().getAttribute('data-motion')).toBe(allowed === true ? 'running' : 'idle')
    expect(document.querySelectorAll('.team-graph-edge-live')).toHaveLength(allowed === true ? 1 : 0)
    expect(flow.getNodes().map(({ id, position }) => ({ id, position }))).toEqual(positions)
    expect(document.querySelector('.react-flow__viewport')).toBe(viewport)
    expect(viewport.style.transform).toBe(transform)
    expect(document.activeElement).toBe(heading)
  }
})

test.each([undefined, false])('无明确 active 许可（%s）时图初始静止，运行文案不能打开动画', async allowed => {
  render(<Graph allowed={allowed} label="正在处理消息" />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
  expect(node().getAttribute('data-motion')).toBe('idle')
  expect(document.querySelectorAll('.team-graph-edge-live, [data-motion="running"], [data-motion="handoff"]')).toHaveLength(0)
  expect(document.querySelector('.react-flow__background')).toBeTruthy()
})

test.each([undefined, false, true])('motionAllowed=%s 时暂停、取消等显示文案变化不改变布尔门控', async allowed => {
  const view = render(<Graph allowed={allowed} />)
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
  const viewport = document.querySelector<HTMLElement>('.react-flow__viewport')!
  const background = document.querySelector('.react-flow__background')
  for (const label of ['正在处理消息', '已暂停', '已取消', '任意显示文案']) {
    view.rerender(<Graph allowed={allowed} label={label} />)
    await frame()
    expect(screen.getByText(`当前任务 · ${label}`)).toBeTruthy()
    expect(node().getAttribute('data-motion')).toBe(allowed === true ? 'running' : 'idle')
    expect(document.querySelectorAll('.team-graph-edge-live')).toHaveLength(allowed === true ? 1 : 0)
    expect(document.querySelector('.react-flow__viewport')).toBe(viewport)
    expect(document.querySelector('.react-flow__background')).toBe(background)
  }
})

test('允许动画时全部记录中的历史节点与历史连线仍保持静止', async () => {
  const input = [...events, event(6, 'team.objective_updated', { goal: '新目标' })]
  render(<Graph allowed state={{ ...team, revision: 6 }} input={input} />)
  fireEvent.click(screen.getByRole('button', { name: '全部记录' }))
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
  expect(node().getAttribute('data-historical')).toBe('true')
  expect(node().getAttribute('data-motion')).toBe('idle')
  expect(document.querySelectorAll('.team-graph-edge-live, [data-motion="running"], [data-motion="handoff"]')).toHaveLength(0)
})

test('动画仅在可见图且允许 motion 时启用；reduced-motion 保留静态执行高亮', () => {
  const css = readFileSync('src/TaskGraph.css', 'utf8')
  expect(css).toContain('@media (prefers-reduced-motion: no-preference)')
  expect(css).toContain('.team-graph-canvas:not([hidden]) .team-graph-edge-live .react-flow__edge-path { animation:')
  expect(css).toContain('@media (prefers-reduced-motion: reduce)')
  expect(css).toContain('animation: none !important; transition: none !important;')
  expect(css).toContain('.team-graph-edge-live .react-flow__edge-path { stroke: #348775;')
})
