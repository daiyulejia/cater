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
const spannedSnapshot = (text: string, start = 0): PublicProgressSnapshot => {
  const data = snapshot()
  Object.assign(data.runs[0], { text, truncated: start > 0,
    text_span: { version: 1, start, end: start + Array.from(text).length } })
  return data
}
const savedProgress = (seq: number, text: string, start = 0, extra: Record<string, unknown> = {}): TeamEvent =>
  event(seq, 'msg.said', { phase: 'team', response_kind: 'progress', audience: 'team',
    run_id: identity.run_id, reply_to: identity.message_seq, revision: 0, text,
    public_text_span: { version: 1, external_started_seq: 3, start, end: start + Array.from(text).length }, ...extra },
  { id: identity.role, kind: 'agent', role: 'software_engineer' })
const reportFor = (data: PublicProgressSnapshot, saved: TeamEvent[]) =>
  publicReports(data, now, activity, [...events, ...saved], 0, now).get(identity.run_id)!

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

test('工具边界已持久化的进展仅从展示扣除，正文和移动摘要只剩新增尾段', () => {
  vi.useFakeTimers(); vi.setSystemTime(now)
  const saved = '我先核对输入。', tail = '现在保存文件。'
  const data = spannedSnapshot(saved + tail)
  const records = [...events, savedProgress(4, saved),
    event(5, 'team.tool_started', { ...identity, name: 'workspace_write' }),
    event(6, 'team.tool_finished', { ...identity, started_seq: 5, status: 'completed' })]
  const before = JSON.stringify({ data, records })
  const report = reportFor(data, records.slice(events.length))
  expect(report).toMatchObject({ text: saved + tail, displaySegments: [tail], age_ms: 500, expiresAt: now + 59_500 })
  render(<ConversationActivity {...props({ publicProgress: data, events: records })} />)
  const region = screen.getByRole('region', { name: '当前工作进展' })
  expect(region.querySelector('pre')?.textContent).toBe(tail)
  expect(screen.getByLabelText('当前进展摘要').textContent).toBe(`月亮 · ${tail}`)
  expect(region.textContent).not.toContain(saved)
  expect(JSON.stringify({ data, records })).toBe(before)
})

test('全覆盖仅隐藏重复正文，仍按原始观察年龄保持活动，重渲染不续期', () => {
  vi.useFakeTimers(); vi.setSystemTime(now)
  const text = '原文全部已有团队记录。', data = spannedSnapshot(text)
  data.runs[0].age_ms = 59_000
  const initial = props({ publicProgress: data })
  const mounted = render(<ConversationActivity {...initial} />)
  expect(screen.getByText('工作说明')).toBeTruthy()
  act(() => vi.advanceTimersByTime(900))
  const archived = { ...initial, events: [...events, savedProgress(4, text)] }
  mounted.rerender(<ConversationActivity {...archived} />)
  expect(reportFor(data, [savedProgress(4, text)])).toMatchObject({ text, displaySegments: [], expiresAt: now + 1000 })
  const view = conversationActivity(archived, now + 900)
  expect(view.lines[0]).toMatchObject({ publicText: '', publicActive: true, moving: true })
  expect(view.nextCheckAt).toBe(now + 1000)
  expect(screen.getByLabelText('有近期活动反馈')).toBeTruthy()
  expect(screen.queryByLabelText('有近期执行反馈')).toBeNull()
  expect(screen.queryByText('工作说明')).toBeNull()
  expect(screen.getByRole('region', { name: '当前工作进展' }).querySelector('pre')).toBeNull()
  expect(screen.getByText('公开进展已保存至团队记录')).toBeTruthy()
  expect(screen.getByLabelText('当前进展摘要').textContent).not.toContain(text)
  expect(screen.getByRole('button', { name: '查看过程' })).toBeTruthy()
  act(() => vi.advanceTimersByTime(99))
  mounted.rerender(<ConversationActivity {...archived} />)
  expect(screen.getByLabelText('有近期活动反馈')).toBeTruthy()
  act(() => vi.advanceTimersByTime(2))
  expect(screen.queryByLabelText('有近期活动反馈')).toBeNull()
  expect(screen.queryByText('公开进展已保存至团队记录')).toBeNull()
  expect(screen.getByRole('region', { name: '当前工作进展' }).textContent).toContain('暂无新反馈')
})

test('跨调用、run、角色、reply_to、revision 或来源不符的持久 progress 不能证明覆盖', () => {
  const text = '相同正文也不能跨身份扣除', data = spannedSnapshot(text), saved = savedProgress(4, text)
  const mismatches = [
    { public_text_span: { ...saved.payload.public_text_span as object, external_started_seq: 30 } },
    { run_id: 'other' }, { speaker: 'test' }, { reply_to: 9 }, { revision: 1 }, { revision: undefined },
    { audience: 'customer' }, { audience: undefined }, { response_kind: 'reply' }, { response_kind: undefined }, { phase: 'other' },
  ]
  for (const change of mismatches) {
    expect(reportFor(data, [{ ...saved, payload: { ...saved.payload, ...change } }]).displaySegments).toEqual([text])
  }
  for (const change of [
    { actor: { id: 'other', kind: 'agent', role: 'software_engineer' } }, { actor: host },
    { actor: { id: 'dev', kind: 'system', role: 'software_engineer' } },
    { actor: { id: 'dev', kind: 'agent', role: '' } },
    { type: 'team.message_received' }, { seq: 2 }, { seq: NaN },
  ]) expect(reportFor(data, [{ ...saved, ...change }]).displaySegments).toEqual([text])
  // The attempt id must distinguish retries even when all other identifiers and text are unchanged.
  const retry = spannedSnapshot(text)
  retry.runs[0].external_started_seq = 7
  const retryEvents = [...events, saved, event(7, 'team.external_run_started', { ...identity, run_started_seq: 2 })]
  expect(publicReports(retry, now, activity, retryEvents, 0, now).get(identity.run_id)?.displaySegments).toEqual([text])
})

test('4000 codepoint 截断窗口按绝对 Unicode 坐标扣除交集，保留空白和组合字符', () => {
  vi.useFakeTimers(); vi.setSystemTime(now)
  const prefix = '窗外😀', saved = '🧑‍💻e\u0301'.repeat(799), tail = '\n 新尾😀'
  expect(Array.from(saved + tail)).toHaveLength(4000)
  const data = spannedSnapshot(saved + tail, Array.from(prefix).length)
  const record = savedProgress(4, prefix + saved)
  expect(reportFor(data, [record]).displaySegments).toEqual([tail])
  render(<ConversationActivity {...props({ publicProgress: data, events: [...events, record] })} />)
  expect(screen.getByText('最近片段 · 成员自述')).toBeTruthy()
  expect(screen.getByRole('region', { name: '当前工作进展' }).querySelector('pre')?.textContent).toBe(tail)
  const partialWindow = spannedSnapshot('😀e\u0301', 1)
  expect(reportFor(partialWindow, [savedProgress(4, '前😀e\u0301后')]).displaySegments).toEqual([])
})

test('缺少或错误 span 保守回退，不影响有效 raw report 的活动资格', () => {
  const text = '😀原文', data = spannedSnapshot(text), saved = savedProgress(4, text)
  const invalid = [undefined, null, [], {},
    { version: 2, start: 0, end: 3 }, { version: 1, start: -1, end: 2 },
    { version: 1, start: 0.5, end: 3.5 }, { version: 1, start: 0, end: 0 },
    { version: 1, start: 3, end: 0 }, { version: 1, start: 0, end: 4 },
    { version: 1, start: '0', end: 3 }, { version: 1, start: 0, end: Infinity },
    { version: 1, start: Number.MAX_SAFE_INTEGER, end: Number.MAX_SAFE_INTEGER + 3 },
  ]
  for (const span of invalid) {
    const live = spannedSnapshot(text)
    Object.assign(live.runs[0], { text_span: span })
    expect(reportFor(live, [saved])).toMatchObject({ displaySegments: [text], expiresAt: now + 59_500 })
    const persisted = span && !Array.isArray(span) ? { ...span, external_started_seq: 3 } : span
    expect(reportFor(data, [{ ...saved, payload: { ...saved.payload, public_text_span: persisted } }]).displaySegments).toEqual([text])
  }
  expect(reportFor(data, [savedProgress(4, text, 0, { public_text_span: { version: 1, start: 0, end: 3 } })]).displaySegments).toEqual([text])
  const legacy = snapshot()
  legacy.runs[0].text = text
  expect(reportFor(legacy, [saved]).displaySegments).toEqual([text])
  // Equal length is insufficient; compare the exact intersection without normalization.
  expect(reportFor(data, [savedProgress(4, '😀异文')]).displaySegments).toEqual([text])
  expect(reportFor(spannedSnapshot('e\u0301'), [savedProgress(4, 'é ')]).displaySegments).toEqual(['e\u0301'])
  for (const lone of ['\uD800', '\uDC00']) {
    const raw = `${lone}原文`
    expect(reportFor(spannedSnapshot(raw), [savedProgress(4, raw)]).displaySegments).toEqual([raw])
    // Even an otherwise matching intersection is uncertain if its source has a lone surrogate.
    expect(reportFor(spannedSnapshot('原文', 1), [savedProgress(4, raw)]).displaySegments).toEqual(['原文'])
  }
})

test('乱序、重叠的已存区间取并集，多个未覆盖缺口明确分隔且不串接', () => {
  vi.useFakeTimers(); vi.setSystemTime(now)
  const data = spannedSnapshot('甲乙丙丁戊己庚辛壬'), saved = [
    savedProgress(6, '己庚辛', 5), savedProgress(5, '丙丁', 2), savedProgress(4, '乙丙', 1),
    savedProgress(7, '壬', 20), savedProgress(8, '错误', 4),
  ]
  const expected = ['甲', '戊', '壬']
  expect(reportFor(data, saved).displaySegments).toEqual(expected)
  render(<ConversationActivity {...props({ publicProgress: data, events: [...events, ...saved] })} />)
  const region = screen.getByRole('region', { name: '当前工作进展' })
  expect(Array.from(region.querySelectorAll('pre'), pre => pre.textContent)).toEqual(expected)
  expect(screen.getAllByText('中间进展已记录在团队记录中')).toHaveLength(2)
  expect(screen.getByLabelText('当前进展摘要').textContent).toBe('月亮 · 壬')
})

test('全覆盖后的报告仍遵循暂停、同步、活动、revision 和结束检查', () => {
  const text = '全部归档', data = spannedSnapshot(text), savedEvents = [...events, savedProgress(4, text)]
  const input = props({ publicProgress: data, events: savedEvents })
  for (const extra of [
    { executionState: 'paused' }, { executionState: 'cancelled' }, { syncError: 'offline' }, { controlBusy: true },
    { activity: { ...activity, active: false } }, { activity: { ...activity, instance_id: 'other' } },
    { team: { ...input.team, revision: 9 } },
    { events: [...savedEvents, event(5, 'team.external_run_finished', { started_seq: 3 })] },
    { events: [...savedEvents, event(5, 'team.run_finished', { ...identity, status: 'completed' })] },
  ]) {
    const view = conversationActivity({ ...input, ...extra }, now)
    expect(view.lines.every(line => !line.publicActive && !line.moving && line.publicText === undefined)).toBe(true)
  }
})
