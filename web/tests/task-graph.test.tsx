import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TaskGraph from '../src/TaskGraph'
import { layoutTaskGraph } from '../src/taskGraphLayout'
import type { WorkState } from '../src/WorkItems'

const actorText = (id: string) => ({ dev: '米寇', test: '三角' }[id] ?? id)
const item = (id: string, depends_on: string[] = [], status = 'planned'): WorkState['items'][number] => ({
  id, depends_on, status, owner: 'dev', reviewer: 'test', objective: `完成 ${id}`, reason: '', criteria: [], checks: [], submission: null,
})
const work = (): WorkState => ({ plan_seq: 4, stage_accepted: false, scope_current: true, items: [item('入口', [], 'submitted'), item('资料', ['入口'], 'blocked')] })

// Real React Flow, with only browser layout primitives substituted for jsdom.
beforeEach(() => {
  const dimensions = (element: HTMLElement) => element.classList.contains('react-flow__handle') ? [6, 6]
    : element.classList.contains('react-flow__node') ? [248, 160] : [880, 560]
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function () { return dimensions(this)[0] })
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function () { return dimensions(this)[1] })
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function () { return dimensions(this)[0] })
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(function () { return dimensions(this)[1] })
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    const [width, height] = dimensions(this)
    const x = this.classList.contains('react-flow__handle-right') ? 245 : 0
    const y = this.classList.contains('react-flow__handle') ? 77 : 0
    return new DOMRect(x, y, width, height)
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
        if (this.targets.size) this.callback([...this.targets].map(element => ({ target: element, contentRect: element.getBoundingClientRect() } as ResizeObserverEntry)), this as unknown as ResizeObserver)
      })
    }
    unobserve(target: Element) { this.targets.delete(target) }
    disconnect() { this.targets.clear() }
  })
})

function Controlled({ value }: { value: WorkState }) {
  const [open, setOpen] = useState(false)
  return <><button onClick={() => setOpen(true)}>任务脉络</button><TaskGraph work={value} actorText={actorText} open={open} onOpenChange={setOpen} /></>
}
async function openGraph(value = work()) {
  const view = render(<Controlled value={value} />)
  const trigger = screen.getByRole('button', { name: '任务脉络' })
  trigger.focus()
  fireEvent.click(trigger)
  await screen.findByRole('dialog', { name: '任务脉络' })
  await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(layoutTaskGraph(value.items).edges.length))
  return view
}
const details = () => screen.getByRole('complementary', { name: '工作项详情' })
const nodeButton = (ordinal: number) => screen.getByRole('button', { name: new RegExp(`^工作 ${ordinal}：`) })
const viewport = () => document.querySelector<HTMLElement>('.react-flow__viewport')!

describe('current work topology', () => {
  it('lays out branch, merge and independent work deterministically without extra nodes or edges', () => {
    const items = [item('a'), item('b', ['a']), item('c', ['a']), item('d', ['b', 'c']), item('alone')]
    const graph = layoutTaskGraph(items)
    expect(graph).toEqual(layoutTaskGraph([...items].reverse()))
    expect(graph.nodes.map(node => node.id)).toEqual(['a', 'alone', 'b', 'c', 'd'])
    expect(graph.edges.map(edge => [edge.source, edge.target])).toEqual([['a', 'b'], ['a', 'c'], ['b', 'd'], ['c', 'd']])
    graph.edges.forEach(edge => expect(graph.nodes.find(node => node.id === edge.source)!.position.x)
      .toBeLessThan(graph.nodes.find(node => node.id === edge.target)!.position.x))
    graph.nodes.forEach((node, index) => graph.nodes.slice(index + 1).forEach(other => {
      expect(Math.abs(node.position.x - other.position.x) >= node.width || Math.abs(node.position.y - other.position.y) >= node.height).toBe(true)
    }))
  })

  it('deduplicates declared dependencies, keeps only current references and does not mutate inputs', () => {
    const items = [item('a'), item('b', ['a', 'missing', 'a'])]
    const before = structuredClone(items)
    expect(layoutTaskGraph(items).edges).toEqual([{ id: '["a","b"]', source: 'a', target: 'b' }])
    expect(items).toEqual(before)
    expect(layoutTaskGraph([])).toEqual({ nodes: [], edges: [] })
  })

  it('keeps valid positions and exact relationships even for a cyclic or self-dependent plan', () => {
    const graph = layoutTaskGraph([item('a', ['b']), item('b', ['a']), item('self', ['self'])])
    expect(graph.edges.map(edge => [edge.source, edge.target])).toEqual([['b', 'a'], ['a', 'b'], ['self', 'self']])
    graph.nodes.forEach(node => { expect(Number.isFinite(node.position.x)).toBe(true); expect(Number.isFinite(node.position.y)).toBe(true) })
  })

  it('is unchanged by live status, reason, scope, criteria or evidence', () => {
    const value = work()
    const before = layoutTaskGraph(value.items)
    value.scope_current = false
    value.items[0].status = 'rework'
    value.items[0].objective = '新目标'
    value.items[0].reason = '用例未通过'
    value.items[0].submission = { seq: 20, artifact_seqs: [19] }
    expect(layoutTaskGraph(value.items)).toEqual(before)
  })
})

describe('task graph dialog', () => {
  it('is controlled and has no internal trigger or empty graph', () => {
    const onOpenChange = vi.fn()
    const view = render(<TaskGraph work={work()} actorText={actorText} open={false} onOpenChange={onOpenChange} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    view.rerender(<TaskGraph work={{ ...work(), items: [] }} actorText={actorText} open onOpenChange={onOpenChange} />)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it.each(Object.entries({ planned: '已安排', ready: '可以开工', running: '正在执行', submitted: '等待验收', accepted: '阶段通过', blocked: '等待前置',
    stale: '需要重新核对', uncertain: '结果待核实', rework: '需要返工', unknown: '状态待核对' }))('renders actual %s status in Chinese', async (status, label) => {
    await openGraph({ ...work(), items: [item('真实工作', [], status)] })
    expect(within(nodeButton(1)).getByText(label)).toBeTruthy()
    expect(within(details()).getByText(label)).toBeTruthy()
  })

  it('shows stale scope separately from accepted work and final delivery', async () => {
    const value: WorkState = { ...work(), scope_current: false, stage_accepted: true, items: [item('入口', [], 'accepted')] }
    value.items[0].criteria = [{ id: 'review', text: '核对入口', kind: 'review' }]
    value.items[0].checks = [{ id: 'review', status: 'passed', reason: '旧审查通过', quote: '已确认入口分支', artifact_seq: 9 }]
    const view = await openGraph(value)
    expect(screen.getByText('方向或店规已更新，旧结果暂不放行。')).toBeTruthy()
    expect(within(nodeButton(1)).getByText('旧阶段通过')).toBeTruthy()
    expect(within(nodeButton(1)).getByText('范围已更新')).toBeTruthy()
    expect(screen.getByText('尚未整体交付')).toBeTruthy()
    expect(within(details()).getByText('旧记录通过，待重新核对')).toBeTruthy()
    expect(within(details()).getByText('“已确认入口分支”')).toBeTruthy()
    view.rerender(<Controlled value={{ ...value, delivery: { status: 'accepted', reason: '整体通过' } }} />)
    expect(screen.getByText('旧整体验收通过，待重新核对')).toBeTruthy()
    expect(screen.queryByText('整体验收通过')).toBeNull()
    view.rerender(<Controlled value={{ ...value, scope_current: true, delivery: { status: 'accepted', reason: '整体通过' } }} />)
    expect(screen.getByText('整体验收通过')).toBeTruthy()
    expect(screen.queryByText('方向或店规已更新，旧结果暂不放行。')).toBeNull()
    expect(screen.queryByText('范围已更新')).toBeNull()
    expect(within(details()).getByText('通过')).toBeTruthy()
  })

  it('opens current details and shows criteria, escaped reasons, evidence and actual artifact paths', async () => {
    const value = work()
    Object.assign(value.items[1], {
      reason: '错误密码分支未通过', reviewer: null,
      criteria: [{ id: 'check', text: '检查错误密码', kind: 'local_check' }, { id: 'review', text: '独立审查资料编辑', kind: 'review' }],
      checks: [{ id: 'check', status: 'failed', reason: '<script>bad()</script>', quote: '<img onerror=bad()>', artifact_seq: 15, receipt_seq: 16 },
        { id: 'extra', status: 'passed', reason: '补充结果', artifact_seq: 17, receipt_seq: 18 }],
      submission: { seq: 20, artifact_seqs: [15, 17], reused_from: 12 },
    })
    value.delivery = { status: 'rework', reason: '', manifest: { sources: [], files: [{ path: 'docs/真实结果.md', artifact_seq: 15 }, { path: 'unrelated.md', artifact_seq: 99 }] } }
    await openGraph(value)
    expect(nodeButton(2).closest('.react-flow__node')?.getAttribute('style')).not.toContain('pointer-events: none')
    fireEvent.click(nodeButton(2))
    const panel = within(details())
    expect(panel.getByText('完成 资料')).toBeTruthy()
    expect(panel.getByText('错误密码分支未通过')).toBeTruthy()
    expect(panel.getByText('尚未安排独立复核人')).toBeTruthy()
    expect(panel.getByRole('button', { name: '工作 1 · 完成 入口' })).toBeTruthy()
    expect(panel.getByText('检查错误密码')).toBeTruthy()
    expect(panel.getByText((_, element) => element?.tagName === 'P' && element.textContent === '未通过 · <script>bad()</script>')).toBeTruthy()
    expect(panel.getByText('“<img onerror=bad()>”')).toBeTruthy()
    expect(panel.getByText('检查回执 #16')).toBeTruthy()
    expect(panel.getByText('检查回执 #18')).toBeTruthy()
    expect(panel.getByText('依据成果 #15')).toBeTruthy()
    expect(panel.getByText('提交记录 #20')).toBeTruthy()
    expect(panel.getByText('成果记录：#15、#17')).toBeTruthy()
    expect(panel.getByText('沿用候选成果 #12，本轮验收独立记录。')).toBeTruthy()
    expect(panel.getByText('docs/真实结果.md · 成果 #15')).toBeTruthy()
    expect(panel.queryByText(/unrelated/)).toBeNull()
    expect(details().querySelector('script, img')).toBeNull()
    await waitFor(() => expect(document.activeElement).toBe(panel.getByRole('heading', { name: '工作 2' })))
  })

  it('has Chinese zoom controls that change the real viewport and retains library attribution', async () => {
    await openGraph()
    const initial = viewport().style.transform
    fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
    await waitFor(() => expect(viewport().style.transform).not.toBe(initial))
    const enlarged = viewport().style.transform
    fireEvent.click(screen.getByRole('button', { name: '缩小图谱' }))
    await waitFor(() => expect(viewport().style.transform).not.toBe(enlarged))
    fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
    await waitFor(() => expect(viewport().style.transform).not.toBe(initial))
    fireEvent.click(screen.getByRole('button', { name: '适应全部任务' }))
    await waitFor(() => expect(viewport().style.transform).toBe(initial))
    expect(screen.getByRole('link', { name: 'React Flow 图谱组件署名' }).textContent).toBe('React Flow')
    expect(document.querySelector('[aria-roledescription="node"], [aria-roledescription="edge"]')).toBeNull()
    expect(screen.queryByText(/Press enter|arrow keys|delete key/)).toBeNull()
  })

  it('preserves selection and the user viewport on polls and retained IDs across plan revisions', async () => {
    const value = work()
    const view = await openGraph(value)
    fireEvent.click(nodeButton(2))
    await waitFor(() => expect(within(details()).getByRole('heading', { name: '工作 2' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
    await act(async () => { await new Promise(resolve => requestAnimationFrame(resolve)) })
    const currentViewport = viewport()
    const transform = currentViewport.style.transform
    const updated = structuredClone(value)
    updated.items[1].status = 'running'
    updated.items[1].reason = '前置已验收'
    view.rerender(<Controlled value={updated} />)
    expect(within(details()).getByText('前置已验收')).toBeTruthy()
    expect(within(details()).getByText('正在执行')).toBeTruthy()
    expect(viewport()).toBe(currentViewport)
    expect(viewport().style.transform).toBe(transform)
    updated.plan_seq = 30
    updated.items.push(item('新增', ['资料']))
    view.rerender(<Controlled value={structuredClone(updated)} />)
    await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(2))
    expect(within(details()).getByRole('heading', { name: '工作 2' })).toBeTruthy()
    expect(viewport().style.transform).toBe(transform)
  })

  it('clears removed selection and cannot resurrect it when the same ID later returns', async () => {
    const value = work()
    const view = await openGraph(value)
    fireEvent.click(nodeButton(2))
    view.rerender(<Controlled value={{ ...value, plan_seq: 25, items: [item('新工作')] }} />)
    expect(within(details()).getByText('完成 新工作')).toBeTruthy()
    view.rerender(<Controlled value={{ ...value, plan_seq: 26, items: [item('新工作'), value.items[1]] }} />)
    expect(within(details()).getByText('完成 新工作')).toBeTruthy()
    expect(within(details()).queryByText('完成 资料')).toBeNull()
  })

  it('exposes a semantic selectable list with truthful missing dependencies and no fabricated graph nodes', async () => {
    const value = work()
    value.items[1].depends_on = ['入口', '不存在的依赖']
    await openGraph(value)
    expect(screen.getByRole('list', { name: '当前任务工作列表' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '工作列表' }))
    const list = screen.getByRole('list', { name: '当前任务工作列表' })
    const button = within(list).getByRole('button', { name: /完成 资料/ })
    button.focus()
    expect(document.activeElement).toBe(button)
    fireEvent.click(button)
    expect(within(details()).getByText('不存在的依赖 · 当前计划未包含')).toBeTruthy()
    expect(within(list).getByText('前置：工作 1、不存在的依赖（当前计划未包含）')).toBeTruthy()
    expect(document.querySelectorAll('.react-flow__node')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: '依赖图' }))
    expect(document.querySelector('.task-graph-canvas')?.hasAttribute('hidden')).toBe(false)
    await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
    fireEvent.keyDown(nodeButton(2), { key: 'Delete' })
    fireEvent.keyDown(nodeButton(2), { key: 'Backspace' })
    expect(document.querySelectorAll('.react-flow__node')).toHaveLength(2)
    expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1)
    expect(document.querySelector('.react-flow__node.draggable, .react-flow__handle.connectable')).toBeNull()
  })

  it('scrolls explicit mobile selection to details and supplies existing portraits', async () => {
    const scrollIntoView = vi.fn()
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList)
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1 })
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
    const value = work()
    render(<TaskGraph work={value} open actorText={actorText} onOpenChange={vi.fn()} portrait={(id, size) => <span role="img" aria-label={`${actorText(id)} ${size}`} />} />)
    fireEvent.click(nodeButton(2))
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start', behavior: 'instant' })
    expect(within(details()).getByRole('img', { name: '米寇 sm' })).toBeTruthy()
    expect(within(details()).getByRole('img', { name: '三角 sm' })).toBeTruthy()
    delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
  })

  it('closes with Escape or the close button and restores the parent trigger focus', async () => {
    await openGraph()
    const trigger = screen.getByRole('button', { name: '任务脉络', hidden: true })
    const initial = viewport().style.transform
    fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
    await waitFor(() => expect(viewport().style.transform).not.toBe(initial))
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(trigger))
    fireEvent.click(trigger)
    await screen.findByRole('dialog')
    await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
    await waitFor(() => expect(viewport().style.transform).toBe(initial))
    fireEvent.click(screen.getByRole('button', { name: '关闭任务脉络' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })
})
