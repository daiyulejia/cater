import { act, fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ConversationActivity } from '../src/ConversationActivity'
import type { ConversationActivityProps } from '../src/ConversationActivity'
import type { TeamEvent, TeamState } from '../src/team'

const now = Date.parse('2026-10-16T08:00:00Z')
const team: TeamState = { enabled: true, objective: '', status: 'running', pending_count: 0,
  revision: 0, runs: [], assignments: [], checkpoint: '' }
const event = (seq: number, type: string, payload: Record<string, unknown>, human = false): TeamEvent & { ts: string } => ({
  seq, type, payload, ts: new Date(now).toISOString(),
  actor: human ? { id: 'human', kind: 'human', role: 'human' } : { id: 'team', kind: 'system', role: 'team' },
})
const events = [
  event(1, 'team.message_received', { recipient: 'dev', kind: 'message' }, true),
  event(2, 'team.run_started', { role: 'dev', message_seq: 1, revision: 0, run_id: 'dev-run' }),
  event(3, 'team.message_received', { recipient: 'test', kind: 'message' }, true),
  event(4, 'team.run_started', { role: 'test', message_seq: 3, revision: 0, run_id: 'test-run' }),
]
const props = (extra: Partial<ConversationActivityProps> = {}): ConversationActivityProps => ({
  team, events, names: { dev: '开发', test: '测试' }, leaderId: 'manager', sending: false,
  executionState: 'running', activity: { active: true, state: 'running' },
  portrait: (id) => <span>{id}</span>, onInspect: vi.fn(), onRefresh: vi.fn(), onPause: vi.fn(), ...extra,
})
const region = () => screen.getByRole('region', { name: '当前工作进展' })
const toggle = () => screen.getByRole('button', { name: /^(展开|收起)工作进展$/ })

test('折叠摘要始终跟随最新顾客消息，不用旧角色失败覆盖当前请求', () => {
  const failures = [...events,
    event(5, 'team.run_finished', { role: 'dev', message_seq: 1, run_id: 'dev-run', status: 'failed' }),
    event(6, 'team.run_finished', { role: 'test', message_seq: 3, run_id: 'test-run', status: 'failed' }),
  ]
  const input = props({ events: failures, activity: { state: 'completed', active: false } })
  const view = render(<ConversationActivity {...input} />)
  expect(screen.getByLabelText('当前进展摘要').textContent).toContain('测试本轮处理未完成')
  expect(screen.getByLabelText('当前进展摘要').textContent).not.toContain('开发')
  expect(screen.getByLabelText('2 项进展需核对')).toBeTruthy()
  view.rerender(<ConversationActivity {...input} sending />)
  expect(screen.getByLabelText('当前进展摘要').textContent).toContain('消息正在发送')
  expect(region().querySelectorAll('.activity-line')).toHaveLength(2)
})

test('展开与收起只更新展开标记和可访问状态，原有正文节点始终保留', () => {
  render(<ConversationActivity {...props({ syncError: 'offline' })} />)
  const root = region()
  const body = root.querySelector('.activity-body')!
  const evidence = [...body.querySelectorAll('p')]
  const originalBody = body.innerHTML
  expect(root.classList.contains('conversation-activity')).toBe(true)
  expect(root.dataset.expanded).toBe('false')
  expect(root.dataset.hasLines).toBe('true')
  expect(body.getAttribute('role')).toBe('status')
  expect(body.getAttribute('aria-live')).toBe('polite')
  expect(body.getAttribute('aria-atomic')).toBe('true')
  expect(root.querySelector('.activity-message')).not.toBeNull()
  expect([...root.querySelectorAll('.activity-line')].map(line => line.getAttribute('data-secondary'))).toEqual(['false', 'true'])
  expect(toggle().classList.contains('sm:hidden')).toBe(true)
  expect(toggle().querySelector('svg.lucide-chevron-down')).not.toBeNull()
  expect(toggle().getAttribute('aria-controls')).toBe(body.id)
  expect(body.id).not.toBe('')
  for (const expanded of [true, false]) {
    fireEvent.click(toggle())
    expect(root.dataset.expanded).toBe(String(expanded))
    expect(toggle().getAttribute('aria-expanded')).toBe(String(expanded))
    expect(toggle().getAttribute('aria-label')).toBe(expanded ? '收起工作进展' : '展开工作进展')
    expect(body.innerHTML).toBe(originalBody)
    expect([...body.querySelectorAll('p')]).toEqual(evidence)
    expect(evidence.every(node => body.contains(node))).toBe(true)
    expect(root.querySelector('[hidden], [aria-hidden="true"].activity-line, [style]')).toBeNull()
  }
})

test('新事件和计时器更新不重置用户展开选择，也不触发请求或控制回调', () => {
  vi.useFakeTimers().setSystemTime(now)
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const input = props()
  const mounted = render(<ConversationActivity {...input} />)
  const bodyId = toggle().getAttribute('aria-controls')
  const timers = vi.getTimerCount()
  expect(timers).toBe(1)
  fireEvent.click(toggle())
  expect(vi.getTimerCount()).toBe(timers)
  const nextEvents = [...events, event(5, 'team.tool_started', {
    role: 'test', run_id: 'test-run', message_seq: 3, name: 'workspace_read', call_id: 'read',
  })]
  mounted.rerender(<ConversationActivity {...input} events={nextEvents} />)
  expect(region().dataset.expanded).toBe('true')
  expect(region().textContent).toContain('读取文件')
  expect(toggle().getAttribute('aria-controls')).toBe(bodyId)
  act(() => vi.advanceTimersByTime(60_001))
  expect(region().dataset.expanded).toBe('true')
  expect(screen.queryByLabelText('有近期执行反馈')).toBeNull()
  fireEvent.click(toggle())
  mounted.rerender(<ConversationActivity {...input} events={nextEvents} />)
  expect(region().dataset.expanded).toBe('false')
  expect(fetch).not.toHaveBeenCalled()
  expect(input.onInspect).not.toHaveBeenCalled()
  expect(input.onRefresh).not.toHaveBeenCalled()
  expect(input.onPause).not.toHaveBeenCalled()
})

test.each([{ syncError: 'offline' }, { controlBusy: true }, { executionState: 'unknown' }])('提示不参与折叠：%j', extra => {
  render(<ConversationActivity {...props(extra)} />)
  const notice = region().querySelector('.activity-notice')!
  expect(notice).not.toBeNull()
  const text = notice.textContent
  for (let i = 0; i < 2; i++) {
    expect(notice.hasAttribute('hidden')).toBe(false)
    expect(notice.hasAttribute('data-secondary')).toBe(false)
    expect(notice.classList.contains('activity-message')).toBe(false)
    expect(notice.classList.contains('activity-line')).toBe(false)
    fireEvent.click(toggle())
    expect(region().querySelector('.activity-notice')).toBe(notice)
    expect(notice.textContent).toBe(text)
  }
})

test('等待顾客不显示折叠按钮或正文，查看与刷新仍可用', () => {
  const input = props({ activity: { active: false, state: 'completed' }, events: [
    ...events.slice(0, 2), event(3, 'team.questions_requested', { role_id: 'dev', message_seq: 1 }),
    event(4, 'team.run_finished', { role: 'dev', message_seq: 1, run_id: 'dev-run', status: 'waiting' }),
  ] })
  render(<ConversationActivity {...input} />)
  expect(screen.getByRole('status').textContent).toBe('待你补充')
  expect(screen.queryByRole('button', { name: /工作进展$/ })).toBeNull()
  expect(region().querySelector('.activity-body')).toBeNull()
  expect(screen.queryByRole('button', { name: '暂停后续执行' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看过程' }))
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  expect(input.onInspect).toHaveBeenCalledTimes(1)
  expect(input.onRefresh).toHaveBeenCalledTimes(1)
  expect(input.onPause).not.toHaveBeenCalled()
})

test.each(['paused', 'cancelled'])('%s 保持原返回条件与控制按钮规则', executionState => {
  const input = props({ executionState, activity: { active: false, state: 'completed' } })
  const mounted = render(<ConversationActivity {...input} />)
  expect(screen.queryByRole('region')).toBeNull()
  mounted.rerender(<ConversationActivity {...input} activity={{ active: true, state: 'running' }} />)
  expect(region()).not.toBeNull()
  expect(screen.queryByRole('button', { name: '暂停后续执行' })).toBeNull()
  expect(region().querySelector('.activity-notice')).toBeNull()
  fireEvent.click(toggle())
  expect(input.onPause).not.toHaveBeenCalled()
  mounted.rerender(<ConversationActivity {...input} syncError="offline" />)
  expect(region().querySelector('.activity-notice')?.textContent).toContain('同步失败')
  mounted.rerender(<ConversationActivity {...input} sending />)
  expect(region().querySelector('.activity-message')?.textContent).toContain('消息正在发送')
})

test('展开不改变暂停的 active、running、busy 条件', () => {
  const input = props()
  const mounted = render(<ConversationActivity {...input} />)
  fireEvent.click(toggle())
  fireEvent.click(screen.getByRole('button', { name: '暂停后续执行' }))
  expect(input.onPause).toHaveBeenCalledTimes(1)
  for (const extra of [{ controlBusy: true }, { activity: { active: false, state: 'running' } }]) {
    mounted.rerender(<ConversationActivity {...input} {...extra} />)
    const pause = screen.getByRole('button', { name: '暂停后续执行' }) as HTMLButtonElement
    expect(pause.disabled).toBe(true)
    fireEvent.click(pause)
    fireEvent.click(toggle())
    expect(input.onPause).toHaveBeenCalledTimes(1)
  }
})

test('无进展行时保留消息，多个实例的正文 id 独立', () => {
  const input = props({ sending: true, team: { ...team, status: 'done' }, events: [], activity: undefined })
  render(<><ConversationActivity {...input} /><ConversationActivity {...input} /></>)
  const roots = screen.getAllByRole('region', { name: '当前工作进展' })
  const ids = roots.map(root => {
    expect(root.dataset.hasLines).toBe('false')
    expect(root.querySelector('.activity-message')?.textContent).toContain('消息正在发送')
    const body = root.querySelector('.activity-body')!
    expect(root.querySelector('[aria-controls]')?.getAttribute('aria-controls')).toBe(body.id)
    return body.id
  })
  expect(new Set(ids).size).toBe(2)
})
