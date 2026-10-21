import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ReactFlowProvider, useReactFlow } from '@xyflow/react'
import type { ReactFlowInstance } from '@xyflow/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { TeamGraphBody } from '../src/TeamGraphBody'
import type { TeamEvent, TeamState } from '../src/team'

const team: TeamState = { enabled: true, objective: 'Viewport', status: 'running', pending_count: 0,
  runs: [], assignments: [], checkpoint: '', revision: 1 }
const message = (seq: number, replyTo?: number): TeamEvent => ({ seq, type: 'team.message_received',
  actor: { id: replyTo ? 'test' : 'dev', kind: 'agent' }, payload: { kind: replyTo ? 'report' : 'assignment',
    recipient: replyTo ? 'dev' : 'test', text: `Message ${seq}`, reply_to: replyTo } })
const events = [message(2), message(3, 2)]
let flow: ReactFlowInstance
let size: [number, number]
const observers = new Set<LayoutObserver>()

function Probe() {
  const instance = useReactFlow()
  useEffect(() => { flow = instance }, [instance])
  return null
}
function Graph({ input = events, state = team }: { input?: TeamEvent[]; state?: TeamState }) {
  return <ReactFlowProvider><Probe /><TeamGraphBody team={state} events={input} actorText={id => id} /></ReactFlowProvider>
}
function dimensions(element: HTMLElement): [number, number] {
  if (element.closest('[hidden]')) return [0, 0]
  if (element.classList.contains('react-flow__node')) return [248, 160]
  if (element.classList.contains('react-flow__handle')) return [6, 6]
  return size
}
class LayoutObserver {
  targets = new Set<Element>()
  constructor(private callback: ResizeObserverCallback) { observers.add(this) }
  observe(target: Element) { this.targets.add(target); queueMicrotask(() => this.deliver()) }
  unobserve(target: Element) { this.targets.delete(target) }
  disconnect() { this.targets.clear(); observers.delete(this) }
  deliver() {
    if (this.targets.size) this.callback([...this.targets].map(target => ({ target,
      contentRect: target.getBoundingClientRect() } as ResizeObserverEntry)), this as unknown as ResizeObserver)
  }
}
const resize = () => act(async () => { observers.forEach(observer => observer.deliver()) })
const frame = () => act(async () => { await new Promise(resolve => requestAnimationFrame(resolve)) })
const viewport = () => document.querySelector<HTMLElement>('.react-flow__viewport')!
function expectVisible(all = false) {
  const { x, y, zoom } = flow.getViewport()
  expect([x, y, zoom].every(Number.isFinite)).toBe(true)
  const visible = flow.getNodes().map(node => {
    const left = x + node.position.x * zoom, top = y + node.position.y * zoom
    const right = left + node.width! * zoom, bottom = top + node.height! * zoom
    return all ? left >= 0 && top >= 0 && right <= size[0] && bottom <= size[1]
      : right > 0 && bottom > 0 && left < size[0] && top < size[1]
  })
  expect(visible.length).toBeGreaterThan(0)
  expect(all ? visible.every(Boolean) : visible.some(Boolean), JSON.stringify({ x, y, zoom, size,
    nodes: flow.getNodes().map(({ position, width, height }) => ({ position, width, height })) })).toBe(true)
}

let bbox: PropertyDescriptor | undefined
let visibility: PropertyDescriptor | undefined
beforeEach(() => {
  size = [390, 383]
  observers.clear()
  bbox = Object.getOwnPropertyDescriptor(SVGElement.prototype, 'getBBox')
  visibility = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'checkVisibility')
  Object.defineProperty(SVGElement.prototype, 'getBBox', { configurable: true, value: () => new DOMRect(0, 0, 60, 18) })
  Object.defineProperty(HTMLElement.prototype, 'checkVisibility', { configurable: true,
    value: function (this: HTMLElement) { return !this.closest('[hidden]') } })
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
  vi.stubGlobal('ResizeObserver', LayoutObserver)
  vi.stubGlobal('DOMMatrixReadOnly', class {
    m22: number
    constructor(transform: string) { this.m22 = Number(/scale\(([^)]+)\)/.exec(transform)?.[1] ?? 1) }
  })
})
afterEach(() => {
  if (bbox) Object.defineProperty(SVGElement.prototype, 'getBBox', bbox)
  else delete (SVGElement.prototype as SVGElement & { getBBox?: () => DOMRect }).getBBox
  if (visibility) Object.defineProperty(HTMLElement.prototype, 'checkVisibility', visibility)
  else delete (HTMLElement.prototype as Partial<HTMLElement>).checkVisibility
})

test.each([[390, 383], [320, 327]])('fits hidden mobile graph at %ipx after layout, then keeps nodes visible on zoom', async (width, height) => {
  size = [width, height]
  vi.mocked(window.matchMedia).mockImplementation(media => ({ media, matches: media === '(max-width: 760px)' } as MediaQueryList))
  const view = render(<Graph />)
  await frame()
  const originalViewport = viewport()
  expect(document.querySelector<HTMLElement>('.team-graph-canvas')!.hidden).toBe(true)
  expect(document.querySelectorAll('.react-flow__node')).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: '协作图', exact: true }))
  // Deliver node measurements before the pane observer, reproducing the fit/resize race.
  await act(async () => {
    observers.forEach(observer => {
      if ([...observer.targets].some(target => target.classList.contains('react-flow__node'))) observer.deliver()
    })
  })
  await frame()
  expect(flow.getViewport()).toEqual({ x: 0, y: 0, zoom: 1 })
  await resize()
  await waitFor(() => expectVisible(true))
  expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1)
  const initial = flow.getViewport()
  fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
  await waitFor(() => expect(flow.getViewport().zoom).toBeGreaterThan(initial.zoom))
  expectVisible()
  expect(flow.getViewport().x).toBeCloseTo(width / 2 + (initial.x - width / 2) * 1.2)
  expect(flow.getViewport().y).toBeCloseTo(height / 2 + (initial.y - height / 2) * 1.2)
  const transform = viewport().style.transform
  view.rerender(<Graph state={{ ...team, status: 'paused' }} />)
  await resize()
  await frame()
  expect(viewport()).toBe(originalViewport)
  expect(viewport().style.transform).toBe(transform)
})

test('waits for a positive visible pane size before fitting a desktop mount', async () => {
  size = [0, 0]
  render(<Graph />)
  await frame()
  expect(flow.getViewport()).toEqual({ x: 0, y: 0, zoom: 1 })
  size = [880, 560]
  await resize()
  await waitFor(() => expectVisible(true))
  expect(flow.getViewport().zoom).toBeGreaterThan(0.5)
})

test('keeps deliberate off-canvas pan and zoom through polling, topology, resize and list switches', async () => {
  const view = render(<Graph />)
  await waitFor(() => expectVisible(true))
  await act(async () => { await flow.setViewport({ x: 10000, y: -10000, zoom: 0.7 }) })
  const originalViewport = viewport(), transform = viewport().style.transform
  view.rerender(<Graph input={[...events, message(4, 3)]} state={{ ...team, status: 'paused' }} />)
  await resize()
  await frame()
  fireEvent.click(screen.getByRole('button', { name: '协作列表', exact: true }))
  await resize()
  size = [320, 327]
  fireEvent.click(screen.getByRole('button', { name: '协作图', exact: true }))
  await resize()
  await frame()
  expect(viewport()).toBe(originalViewport)
  expect(viewport().style.transform).toBe(transform)
  fireEvent.click(screen.getByRole('button', { name: '适应全部任务' }))
  await waitFor(() => expectVisible(true))
})

test('fits first records and a new scope when they arrive while the mobile graph is hidden', async () => {
  size = [320, 327]
  vi.mocked(window.matchMedia).mockImplementation(media => ({ media, matches: media === '(max-width: 760px)' } as MediaQueryList))
  const view = render(<Graph input={[]} />)
  await frame()
  const emptyViewport = viewport()
  view.rerender(<Graph />)
  fireEvent.click(screen.getByRole('button', { name: '全部记录', exact: true }))
  await frame()
  expect(viewport()).not.toBe(emptyViewport)
  fireEvent.click(screen.getByRole('button', { name: '协作图', exact: true }))
  await resize()
  await waitFor(() => expectVisible(true))
})
