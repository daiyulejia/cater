import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import TaskGraph from '../src/TaskGraph'
import { initialGraphView } from '../src/taskGraphConfig'
import type { WorkState } from '../src/WorkItems'
import type { TeamEvent, TeamState } from '../src/team'

const mobileQuery = '(max-width: 760px)'
const actorText = (id: string) => id
const onOpenChange = vi.fn()
const longText = `第一行原文\n${'保留全部工作目标与原文，不应在 JavaScript 中截断。'.repeat(40)}\n<script>literal text only</script>\n最后一行必须保留`
type Kind = 'team' | 'work'
const kinds: Kind[] = ['team', 'work']
const labels = {
  team: { graph: '协作图', list: '协作列表', listName: '当前团队协作列表', details: '协作记录详情', heading: '回报 #3', node: /^回报 #3：/ },
  work: { graph: '依赖图', list: '工作列表', listName: '当前任务工作列表', details: '工作项详情', heading: '工作 2', node: /^工作 2：/ },
}
const frame = () => act(async () => { await new Promise(resolve => requestAnimationFrame(resolve)) })
const viewport = () => document.querySelector<HTMLElement>('.react-flow__viewport')!
const canvas = () => document.querySelector<HTMLElement>('.task-graph-canvas')!
const details = (kind: Kind) => screen.getByRole('complementary', { name: labels[kind].details })
const list = (kind: Kind) => screen.getByRole('list', { name: labels[kind].listName })

function graph(kind: Kind, polled = false) {
  if (kind === 'team') {
    const team: TeamState = { enabled: true, objective: '移动图谱', status: 'running', pending_count: 0,
      runs: polled ? [{ message_seq: 3, role: 'dev', status: 'failed', reason: 'poll failure' }] : [],
      assignments: [], checkpoint: '', revision: 1 }
    const events: TeamEvent[] = [
      { seq: 2, type: 'team.message_received', actor: { id: 'dev', kind: 'agent' },
        payload: { kind: 'assignment', recipient: 'test', text: '检查移动图谱' } },
      { seq: 3, type: 'team.message_received', actor: { id: 'test', kind: 'agent' },
        payload: { kind: 'report', recipient: 'dev', text: longText, reply_to: 2 } },
    ]
    return <TaskGraph team={team} events={events} open actorText={actorText} onOpenChange={onOpenChange} />
  }
  const work: WorkState = { plan_seq: 4, stage_accepted: false, scope_current: true, items: [
    { id: 'entry', depends_on: [], status: 'submitted', owner: 'dev', reviewer: 'test', objective: '检查移动图谱',
      reason: '', criteria: [], checks: [], submission: null },
    { id: 'review', depends_on: ['entry'], status: polled ? 'rework' : 'blocked', owner: 'test', reviewer: 'dev',
      objective: longText, reason: polled ? 'poll failure' : '', criteria: [], checks: [], submission: null },
  ] }
  return <TaskGraph work={work} open actorText={actorText} onOpenChange={onOpenChange} />
}

function mediaAt(mobile: boolean) {
  const query = Object.assign(new EventTarget(), { media: mobileQuery, matches: mobile, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn() }) as MediaQueryList
  const addEventListener = vi.spyOn(query, 'addEventListener')
  const matchMedia = vi.spyOn(window, 'matchMedia').mockImplementation(media => media === mobileQuery ? query
    : { media, matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList)
  return {
    matchMedia, query, addEventListener,
    async change(matches: boolean) {
      await act(async () => {
        Object.assign(query, { matches })
        const event = Object.assign(new Event('change'), { matches, media: mobileQuery })
        query.dispatchEvent(event)
        query.onchange?.call(query, event as MediaQueryListEvent)
        fireEvent(window, new Event('resize'))
      })
      await frame()
    },
  }
}

// Keep React Flow real; replace only browser layout primitives missing in jsdom.
let bboxDescriptor: PropertyDescriptor | undefined
let scrollDescriptor: PropertyDescriptor | undefined
let scrollIntoView: ReturnType<typeof vi.fn>
beforeEach(() => {
  bboxDescriptor = Object.getOwnPropertyDescriptor(SVGElement.prototype, 'getBBox')
  scrollDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView')
  scrollIntoView = vi.fn()
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView })
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

afterEach(() => {
  if (bboxDescriptor) Object.defineProperty(SVGElement.prototype, 'getBBox', bboxDescriptor)
  else delete (SVGElement.prototype as SVGElement & { getBBox?: () => DOMRect }).getBBox
  if (scrollDescriptor) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', scrollDescriptor)
  else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
})

function expectMode(kind: Kind, mode: 'graph' | 'list') {
  expect(screen.getByRole('button', { name: labels[kind].graph }).getAttribute('aria-pressed')).toBe(String(mode === 'graph'))
  expect(screen.getByRole('button', { name: labels[kind].list }).getAttribute('aria-pressed')).toBe(String(mode === 'list'))
  expect(canvas().hidden).toBe(mode === 'list')
  expect(list(kind).parentElement!.classList.contains('task-graph-sr-only')).toBe(mode === 'graph')
}

describe.each(kinds)('%s mobile graph view', kind => {
  it.each([true, false])('mounts with mobile=%s in the expected mode and keeps real graph topology', async mobile => {
    const media = mediaAt(mobile)
    const view = render(graph(kind))
    expectMode(kind, mobile ? 'list' : 'graph')
    expect(media.matchMedia).toHaveBeenCalledWith(mobileQuery)
    expect(within(list(kind)).getAllByRole('listitem')).toHaveLength(2)
    await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
    expect(document.querySelectorAll('.react-flow__node')).toHaveLength(2)
    expect(document.querySelector('.react-flow__node.draggable, .react-flow__handle.connectable')).toBeNull()
    await media.change(!mobile)
    expectMode(kind, mobile ? 'list' : 'graph')
    view.rerender(graph(kind, true))
    await frame()
    expectMode(kind, mobile ? 'list' : 'graph')
    expect(media.addEventListener).not.toHaveBeenCalled()
    expect(media.query.addListener).not.toHaveBeenCalled()
  })

  it.each([true, false])('preserves manual modes, selection, focus and zoom across polling and media changes (mobile=%s)', async mobile => {
    const media = mediaAt(mobile)
    const view = render(graph(kind))
    expectMode(kind, mobile ? 'list' : 'graph')
    fireEvent.click(screen.getByRole('button', { name: labels[kind].graph }))
    await waitFor(() => expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: labels[kind].node }))
    const panel = details(kind)
    const heading = within(panel).getByRole('heading', { name: labels[kind].heading })
    await waitFor(() => expect(document.activeElement).toBe(heading))
    if (mobile) expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start', behavior: 'instant' })
    else expect(scrollIntoView).not.toHaveBeenCalled()
    const initialTransform = viewport().style.transform
    fireEvent.click(screen.getByRole('button', { name: '放大图谱' }))
    await waitFor(() => expect(viewport().style.transform).not.toBe(initialTransform))
    await frame()
    heading.focus()
    const previousViewport = viewport(), transform = viewport().style.transform
    const expectReadingState = (mode: 'graph' | 'list') => {
      expectMode(kind, mode)
      expect(details(kind)).toBe(panel)
      expect(within(panel).getByRole('heading', { name: labels[kind].heading })).toBe(heading)
      expect(document.activeElement).toBe(heading)
      expect(within(list(kind)).getAllByRole('button')[1].getAttribute('aria-pressed')).toBe('true')
      expect(viewport()).toBe(previousViewport)
      expect(viewport().style.transform).toBe(transform)
      expect(document.querySelectorAll('.react-flow__edge')).toHaveLength(1)
      expect(document.querySelectorAll('.react-flow__node')).toHaveLength(2)
    }
    view.rerender(graph(kind, true))
    await frame()
    expect(within(panel).getByText('poll failure')).toBeTruthy()
    expectReadingState('graph')
    await media.change(!mobile)
    expectReadingState('graph')
    await media.change(mobile)
    expectReadingState('graph')
    expect(screen.getByRole('button', { name: labels[kind].node }).getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: labels[kind].list }))
    fireEvent.click(within(list(kind)).getAllByRole('button')[0])
    await frame()
    expect(within(list(kind)).getAllByRole('button')[0].getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(within(list(kind)).getAllByRole('button')[1])
    await waitFor(() => expect(document.activeElement).toBe(heading))
    expectReadingState('list')
    await media.change(false)
    expectReadingState('list')
    view.rerender(graph(kind))
    await frame()
    expect(within(panel).queryByText('poll failure')).toBeNull()
    expectReadingState('list')
    await media.change(true)
    expectReadingState('list')
    fireEvent.click(screen.getByRole('button', { name: labels[kind].graph }))
    await frame()
    expectReadingState('graph')
    expect(screen.getByRole('button', { name: labels[kind].node }).getAttribute('aria-pressed')).toBe('true')
    expect(media.addEventListener).not.toHaveBeenCalled()
    expect(media.query.addListener).not.toHaveBeenCalled()
  })

  it('keeps the full original text in the list preview and opens complete details', async () => {
    mediaAt(true)
    render(graph(kind))
    expectMode(kind, 'list')
    const button = within(list(kind)).getAllByRole('button')[1]
    const preview = button.querySelector('.task-graph-list-preview')!
    // CSS line clamping and touch target sizes require a real browser, not jsdom layout.
    expect(preview).not.toBeNull()
    expect(preview.textContent).toBe(longText)
    expect(button.querySelector('script')).toBeNull()
    const panel = details(kind)
    const textSelector = kind === 'team' ? '.task-graph-record-text' : '.task-graph-detail-objective'
    expect(panel.querySelector(textSelector)!.textContent).not.toBe(longText)
    fireEvent.click(button)
    const heading = within(panel).getByRole('heading', { name: labels[kind].heading })
    await waitFor(() => expect(document.activeElement).toBe(heading))
    const fullText = panel.querySelector(textSelector)!
    expect(fullText.textContent).toBe(longText)
    expect(fullText.classList.contains('task-graph-list-preview')).toBe(false)
    expect(fullText.closest('.task-graph-list-preview')).toBeNull()
    expect(panel.querySelector('script')).toBeNull()
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expectMode(kind, 'list')
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start', behavior: 'instant' })
  })
})

describe('initialGraphView', () => {
  it.each([true, false])('reads the mobile query on each call (matches=%s)', mobile => {
    const media = mediaAt(mobile)
    expect(initialGraphView()).toBe(mobile ? 'list' : 'graph')
    Object.assign(media.query, { matches: !mobile })
    expect(initialGraphView()).toBe(mobile ? 'graph' : 'list')
    expect(media.matchMedia.mock.calls).toEqual([[mobileQuery], [mobileQuery]])
  })

  it('falls back to graph without window', () => {
    const browserWindow = window
    vi.stubGlobal('window', undefined)
    try { expect(initialGraphView()).toBe('graph') }
    finally { vi.stubGlobal('window', browserWindow) }
  })

  it('falls back to graph without matchMedia', () => {
    const matchMedia = window.matchMedia
    vi.stubGlobal('matchMedia', undefined)
    try { expect(initialGraphView()).toBe('graph') }
    finally { vi.stubGlobal('matchMedia', matchMedia) }
  })
})
