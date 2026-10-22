import { act, fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ConversationActivity } from '../src/ConversationActivity'
import { conversationActivity } from '../src/conversationActivityState'
import { publicReports } from '../src/publicProgress'
import type { PublicProgressSnapshot } from '../src/publicProgress'
import type { ConversationActivityProps } from '../src/ConversationActivity'
import type { TeamEvent } from '../src/team'

const now = Date.parse('2026-10-23T08:00:00Z')
const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent => ({ seq, type, actor, payload })
const identity = { role: 'dev', run_id: 'run', message_seq: 1 }
const events = [event(1, 'team.message_received', { recipient: 'dev', kind: 'message' }, { id: 'human', kind: 'human', role: 'human' }),
  event(2, 'team.run_started', { ...identity, revision: 0 }),
  event(3, 'team.external_run_started', { ...identity, run_started_seq: 2 })]
const snapshot = (): PublicProgressSnapshot => ({ activity_id: 'activity', instance_id: 'instance', runs: [{ ...identity,
  run_started_seq: 2, external_started_seq: 3, revision: 0, text: '我先核对输入，再保存文件。', truncated: false, age_ms: 500 }] })
const activity = { id: 'activity', instance_id: 'instance', active: true, state: 'running' }
const props = (extra: Partial<ConversationActivityProps> = {}): ConversationActivityProps => ({
  team: { enabled: true, objective: '做一件普通工作', status: 'running', pending_count: 0, revision: 0, runs: [], assignments: [], checkpoint: '' },
  events, names: { dev: '月亮' }, leaderId: 'manager', sending: false, activity, executionState: 'running',
  publicProgress: snapshot(), progressReceivedAt: now, portrait: (id) => <span>{id}</span>,
  onInspect: vi.fn(), onRefresh: vi.fn(), onPause: vi.fn(), ...extra,
})

test('公开工作说明在现有角色进展行展开，不生成顾客答复或交付状态', () => {
  vi.useFakeTimers(); vi.setSystemTime(now)
  try {
    const view = conversationActivity(props(), now)
    expect(view.lines[0].publicText).toBe(snapshot().runs[0].text)
    expect(view.lines[0].moving).toBe(true)
    expect(view.message).not.toMatch(/已回应|已确认.*交付/)
    render(<ConversationActivity {...props()} />)
    expect(screen.getByText('工作说明')).toHaveProperty('tagName', 'SUMMARY')
    expect(screen.getByText('成员自述')).toBeTruthy()
    expect(screen.getByLabelText('当前进展摘要').textContent).toContain('月亮 · 我先核对输入')
    expect(screen.getByRole('region', { name: '当前工作进展' }).querySelectorAll('details')).toHaveLength(1)
  } finally { vi.useRealTimers() }
})

test('没有新轮询时按原观察年龄过期，重渲染不刷新进度时钟', () => {
  vi.useFakeTimers(); vi.setSystemTime(now)
  try {
    const mounted = render(<ConversationActivity {...props()} />)
    act(() => vi.advanceTimersByTime(59_400))
    mounted.rerender(<ConversationActivity {...props()} />)
    expect(screen.getByText('工作说明')).toBeTruthy()
    act(() => vi.advanceTimersByTime(101))
    expect(screen.queryByText('工作说明')).toBeNull()
    expect(screen.getByRole('region', { name: '当前工作进展' }).textContent).toContain('暂无新反馈')
  } finally { vi.useRealTimers() }
})

test.each([
  { activity: { ...activity, instance_id: 'new-instance' } },
  { activity: { ...activity, id: 'new-activity' } },
  { activity: { ...activity, active: false, state: 'completed' } },
  { activity: { ...activity, id: undefined } },
  { executionState: 'paused' }, { executionState: 'cancelled' },
  { syncError: 'offline' }, { controlBusy: true }, { progressReceivedAt: undefined },
])('实例、活动、控制或同步状态变化后不显示旧说明：%j', extra => {
  expect(conversationActivity(props(extra), now).lines.every(line => !line.publicText)).toBe(true)
})

test('新目标与完成记录使旧说明失效，伪造来源也不会让它重新出现', () => {
  for (const end of [event(4, 'team.run_finished', { ...identity, status: 'completed' }),
    event(4, 'team.external_run_finished', { started_seq: 3 })]) {
    expect(publicReports(snapshot(), now, activity, [...events, end], 0, now).size).toBe(0)
  }
  expect(publicReports(snapshot(), now, activity, events, 9, now).size).toBe(0)
  const fake = events.map(row => row.seq === 3 ? { ...row, actor: { ...host, kind: 'agent' } } : row)
  expect(publicReports(snapshot(), now, activity, fake, 0, now).size).toBe(0)
})

test('串角色、旧外部调用、超长片段、无效年龄与未接收快照均被忽略', () => {
  for (const changes of [{ role: 'test' }, { run_id: 'other' }, { message_seq: 9 }, { external_started_seq: 30 },
    { run_started_seq: 20 }, { text: '文'.repeat(4001) }, { text: '   ' }, { age_ms: -1 }, { age_ms: NaN }, { age_ms: 60_000 }]) {
    const data = snapshot(); Object.assign(data.runs[0], changes)
    expect(publicReports(data, now, activity, events, 0, now).size).toBe(0)
  }
  expect(publicReports(snapshot(), now + 1, activity, events, 0, now).size).toBe(0)
})

test('成员公开文本作为纯文本展示，窗口保留截断提示', () => {
  vi.useFakeTimers(); vi.setSystemTime(now)
  try {
    const data = snapshot()
    data.runs[0].text = '<img src=x onerror=alert(1)>\n继续核对。'
    data.runs[0].truncated = true
    render(<ConversationActivity {...props({ publicProgress: data })} />)
    fireEvent.click(screen.getByText('工作说明'))
    expect(screen.getByText('最近片段 · 成员自述')).toBeTruthy()
    const region = screen.getByRole('region', { name: '当前工作进展' })
    expect(region.querySelector('pre')?.textContent).toBe(data.runs[0].text)
    expect(region.querySelector('img')).toBeNull()
  } finally { vi.useRealTimers() }
})
