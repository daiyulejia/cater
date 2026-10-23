import { fireEvent, render } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { EventDetails, EventDetailsList } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { NativeReceiptRead } from '../src/NativeReceiptRead'
import { conversationActivity, receiptReadArguments } from '../src/conversationActivityState'
import { receiptReadProvenance } from '../src/receiptReadStatus'
import type { TeamEvent, TeamState } from '../src/team'

type Data = Record<string, unknown>
const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Data, actor = host): TeamEvent => ({ seq, type, payload, actor })
const readerRun = '0fb35cb9-7ff0-4fb2-8374-000000000100'
const originalRun = '0fb35cb9-7ff0-4fb2-8374-000000000020'
const start = (args: Data = {}) => event(100, 'team.tool_started', { name: 'read_receipts', role: 'dev', run_id: readerRun,
  message_seq: 1, call_id: 'read-call', arguments: args })
// Native finishes omit arguments, message_seq and call_id; recover inputs from the separate start.
const finish = (result: unknown = page()): TeamEvent => event(101, 'team.tool_finished', {
  name: 'read_receipts', role: 'dev', run_id: readerRun, started_seq: 100, status: 'ok', result })
const marker = (): Data => ({ kind: 'persisted_events', reexecutes_operations: false, reader_run_id: readerRun,
  reader_message_seq: 1, reader_started_seq: 100 })
const row = (seq = 20, extra: Data = {}): Data => ({ seq, started_seq: seq - 1, message_seq: 1,
  name: 'workspace_run', role: 'dev', run_id: originalRun, result: { status: 'ok', content: 'PRIVATE_BODY', complete: true }, ...extra })
const page = (extra: Data = {}): Data => ({ status: 'ok', receipts: [row()], has_more: false,
  next_before_seq: null, record_read: marker(), ...extra })
const observation = (extra: Data = {}): Data => ({ seq: 20, started_seq: 19, message_seq: 1, role: 'dev', run_id: originalRun,
  path: 'docs/readme.txt', sha256: 'a'.repeat(64), bytes: 100, offset: 4, returned_bytes: 5, truncated: true,
  content_complete: false, read_tool: 'read_receipts', read_args: { receipt_seq: 20 }, ...extra })
const files = (extra: Data = {}): Data => ({ status: 'ok', record_read: marker(), observations: [observation()],
  scope_available: true, query_state: 'matched_records', workspace_binding: { binding_seq: 3, workspace_id: 'work', fingerprint: 'binding-fingerprint' },
  has_more: false, next_before_seq: null, ...extra })
const team: TeamState = { enabled: true, objective: 'Read saved receipts', revision: 0, status: 'running', pending_count: 0,
  runs: [], assignments: [], checkpoint: '' }
const activity = (started: TeamEvent, receipt: TeamEvent, context: TeamEvent[] = [started, receipt]) => conversationActivity({ team,
  names: { dev: '开发' }, leaderId: 'manager', sending: false, executionState: 'paused', events: [
    event(1, 'team.message_received', { recipient: 'dev', text: 'Read', kind: 'message' }, { id: 'human', kind: 'human', role: 'human' }),
    event(2, 'team.run_started', { role: 'dev', run_id: readerRun, message_seq: 1, revision: 0 }), ...context,
  ] }).lines.find(line => line.role === 'dev')!
const surfaces = (started: TeamEvent, receipt: TeamEvent, context = [started, receipt]) => [
  render(<EventDetails event={receipt} contextEvents={context} />),
  render(<TeamToolProgress events={context} names={{ dev: '开发' }} full />),
]
const parsed = (value: unknown, args: Data = {}) => receiptReadProvenance(receiptReadArguments(finish(value), [start(args)]))
const assertQuiet = (container: HTMLElement) => {
  expect(container.querySelector('a, button, img, script, iframe, pre')).toBeNull()
  expect(container.textContent).not.toMatch(/PRIVATE_BODY|原始事件 JSON|reader_run_id|binding-fingerprint/)
  expect(container.textContent).not.toContain(readerRun)
  expect(container.textContent).not.toContain(originalRun)
  expect([...container.querySelectorAll('details')].every(detail => !detail.open)).toBe(true)
}

test.each([
  [{}, page(), '已存回执已读取'],
  [{ receipt_seq: 20 }, page(), '已存回执已读取'],
  [{ receipt_seq: 99 }, page({ receipts: [] }), '已存回执已读取'],
  [{ limit: 1 }, page({ has_more: true, next_before_seq: 20 }), '已存回执已读取'],
  [{ section: 'file_reads' }, files(), '已存文件读取观察已读取'],
  [{ section: 'file_reads', file_path: 'missing.txt' }, files({ observations: [], query_state: 'no_matching_records' }), '当前筛选无匹配文件读取记录'],
  [{ section: 'file_reads' }, files({ observations: [], scope_available: false, workspace_binding: null, query_state: 'scope_unavailable' }), '文件读取来源范围不可查询'],
] as [Data, Data, string][])('query inputs %j validate across all three surfaces', (args, value, status) => {
  const started = start(args), receipt = finish(value), saved = JSON.stringify([started, receipt])
  expect(receipt.payload).not.toHaveProperty('arguments')
  expect(parsed(value, args)).toMatchObject({ valid: true, status })
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toContain(status)
    expect(view.container.textContent).toContain('不重新执行原操作')
    assertQuiet(view.container)
  }
  const line = activity(started, receipt)
  expect(line.text).toContain(status)
  expect(line.warning).toBe(status === '文件读取来源范围不可查询')
  expect(line.moving).toBe(false)
  expect(line.text).not.toMatch(/PRIVATE_BODY|重新核验通过/)
  expect(JSON.stringify([started, receipt])).toBe(saved)
})

test('pending and completed sources have separate bounded pages, with no completion or replay claim', () => {
  const value = page({ pending_operations: [row(30, { started_seq: 30, outcome: 'unknown', run_ended: true, replay_authorized: false })],
    pending_has_more: true, pending_next_before_seq: 30 })
  const started = start({ include_pending: true, limit: 1 }), receipt = finish(value)
  expect(parsed(value, started.payload.arguments as Data)?.valid).toBe(true)
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toContain('回执 #20')
    expect(view.container.textContent).toContain('未结算操作 #30')
    expect(view.container.textContent).toContain('不代表操作已完成或允许重试')
    expect(view.container.textContent).toContain('还有后续记录')
    assertQuiet(view.container)
  }
  expect(activity(started, receipt).warning).toBe(false)
})

test('same-run and mixed legacy records retain only validated optional run relations', () => {
  const value = page({ receipts: [row(20, { run_id: readerRun }), row(30), { seq: 40 }, row(50, { run_id: { unsafe: 'object' } })] })
  expect(parsed(value)?.valid).toBe(true)
  for (const view of surfaces(start(), finish(value))) {
    expect(view.container.textContent).toContain('同轮原观察（非重新核验）')
    expect(view.container.textContent).toContain('其他轮次原观察')
    expect(view.container.textContent).toContain('回执 #40')
    expect(view.container.textContent).toContain('回执 #50')
    assertQuiet(view.container)
  }
})

test('scope unavailable and an empty available filter cannot imply no history ever', () => {
  const unavailable = files({ observations: [], query_state: 'scope_unavailable', scope_available: false, workspace_binding: null })
  const empty = files({ observations: [], query_state: 'no_matching_records' })
  for (const view of surfaces(start({ section: 'file_reads' }), finish(unavailable))) {
    expect(view.container.textContent).toContain('空列表不表示没有历史读取')
    expect(view.container.textContent).toContain('也不证明发生过解绑或权限变更')
  }
  for (const view of surfaces(start({ section: 'file_reads' }), finish(empty))) {
    expect(view.container.textContent).toContain('当前可查询范围及筛选条件下没有匹配记录')
    expect(view.container.textContent).toContain('不证明文件从未读过')
    expect(view.container.textContent).toContain('原观察可能为节选，不补齐全文')
  }
})

test('reader IDs may be absent together for direct backend calls, with no invented run label', () => {
  const value = page({ record_read: { kind: 'persisted_events', reexecutes_operations: false } })
  expect(parsed(value)).toMatchObject({ valid: true, sources: [{ seq: 20, kind: '回执' }] })
  expect(parsed(value)?.sources?.[0].relation).toBeUndefined()
  const lone = render(<EventDetails event={finish(value)} />)
  expect(lone.container.textContent).toContain('待核对')
  expect(lone.container.textContent).not.toContain('已存回执已读取')
})

const reject = (value: unknown, args: Data = {}) => {
  const started = start(args), receipt = finish(value)
  expect(parsed(value, args)).toMatchObject({ valid: false, status: '已存回执读取待核对' })
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toContain('已存回执读取待核对')
    expect(view.container.textContent).not.toContain('工具已完成')
    expect(view.container.querySelector('[aria-label="已读取的来源事件序号"]')).toBeNull()
    assertQuiet(view.container)
  }
  expect(activity(started, receipt).warning).toBe(true)
}

test.each([null, [], 'metadata', true, 1, {}, { kind: 'persisted_events', reexecutes_operations: true },
  { ...marker(), reader_run_id: originalRun }, { ...marker(), reader_message_seq: 2 }, { ...marker(), reader_started_seq: 99 },
  { kind: 'persisted_events', reexecutes_operations: false, reader_run_id: readerRun },
])('metadata present but invalid is uncertain: %j', value => reject(page({ record_read: value })))

test.each([
  { receipts: {} }, { receipts: [null] }, { receipts: ['row'] }, { receipts: [{ seq: '20' }] },
  { receipts: [row(), row()] }, { receipts: [row(101)] }, { receipts: [row(30), row(20)] },
  { receipts: Array.from({ length: 51 }, (_, index) => ({ seq: index + 1 })) },
  { has_more: 'false' }, { has_more: true }, { next_before_seq: 20 }, { complete: false }, { status: 'partial' },
  { pending_operations: [] }, { record_read: { ...marker(), reader_run_id: {} } },
])('typed page and cursor contradictions are uncertain: %j', extra => reject(page(extra)))

test.each([
  { limit: 0 }, { limit: 51 }, { limit: '1' }, { limit: null }, { section: null }, { section: 'bad' },
  { include_pending: 'false' }, { role: {} }, { message_seq: true }, { file_path: 'docs/readme.txt' },
  { receipt_seq: 21 }, { receipt_seq: 20, include_pending: true }, { before_seq: 20 }, { run_id: readerRun },
])('actual invalid or mismatched arguments are uncertain: %j', args => reject(page(), args))

test.each([
  { observations: {} }, { observations: [null] }, { scope_available: false }, { workspace_binding: null },
  { workspace_binding: {} }, { query_state: 'unknown' }, { query_state: 'no_matching_records' },
  { query_state: 'scope_unavailable' }, { observations: [], query_state: 'matched_records' },
  { observations: [], query_state: 'no_matching_records', has_more: true, next_before_seq: 20 },
  { observations: [observation({ content_complete: true })] }, { observations: [observation({ started_seq: 3 })] },
  { observations: [observation({ bytes: '100' })] }, { observations: [observation({ read_args: { receipt_seq: 21 } })] },
  { observations: [observation({ sha256: '<script>bad</script>' })] },
])('file state and observation contradictions are uncertain: %j', extra => reject(files(extra), { section: 'file_reads' }))

test.each([
  { observations: [], query_state: 'scope_unavailable', scope_available: true, workspace_binding: null },
  { observations: [], query_state: 'scope_unavailable', scope_available: false },
  { query_state: 'scope_unavailable', scope_available: false, workspace_binding: null },
  { observations: [], query_state: 'scope_unavailable', scope_available: false, workspace_binding: null, has_more: true },
])('unavailable scope requires false/null/empty/no-more: %j', extra => reject(files(extra), { section: 'file_reads' }))

test('include_pending requires typed arrays and both independent cursor fields', () => {
  reject(page(), { include_pending: true })
  reject(page({ pending_operations: {}, pending_has_more: false, pending_next_before_seq: null }), { include_pending: true })
  reject(page({ pending_operations: [], pending_has_more: true, pending_next_before_seq: 20 }), { include_pending: true })
})

test.each(['run_id', 'role', 'call_id', 'message_seq', 'started_seq', 'name', 'tool', 'actor', 'duplicate', 'future', 'explicit-args', 'args-array', 'reader-message'])('strict paired start rejects %s', mode => {
  const started = start(), receipt = finish()
  if (['run_id', 'role', 'name', 'tool'].includes(mode)) started.payload[mode] = 'mismatch'
  if (mode === 'call_id') receipt.payload.call_id = 'mismatch'
  if (mode === 'message_seq') receipt.payload.message_seq = 2
  if (mode === 'started_seq') receipt.payload.started_seq = 99
  if (mode === 'actor') started.actor = { ...host, kind: 'agent' }
  if (mode === 'future') started.seq = 102
  if (mode === 'explicit-args') receipt.payload.arguments = { limit: 1 }
  if (mode === 'args-array') started.payload.arguments = []
  if (mode === 'reader-message') started.payload.message_seq = 2
  const context = mode === 'duplicate' ? [started, { ...started }, receipt] : [started, receipt]
  for (const view of surfaces(started, receipt, context)) {
    expect(view.container.textContent).not.toContain('已存回执已读取')
    expect(view.container.textContent).toMatch(/待核对|等待回执/)
    expect(view.container.querySelector('pre')).toBeNull()
  }
  expect(activity(started, receipt, context).text).not.toContain('已存回执已读取')
})

test('untrusted host and conflicting native tool aliases do not expose source records', () => {
  for (const extra of [{ tool: 'workspace_run' }, {}]) {
    const receipt = finish()
    Object.assign(receipt.payload, extra)
    if (!extra.tool) receipt.actor = { ...host, kind: 'agent' }
    const view = render(<EventDetails event={receipt} contextEvents={[start()]} />)
    expect(view.container.textContent).toContain('待核对')
    assertQuiet(view.container)
  }
})

test('hostile stored bodies and object metadata never become markup or fetch requests', () => {
  const fetch = vi.fn(), xhr = vi.spyOn(XMLHttpRequest.prototype, 'open')
  vi.stubGlobal('fetch', fetch)
  const hostile = '<img src=x onerror=alert(1)><script>alert(1)</script>https://example.invalid'
  const value = page({ receipts: [row(20, { result: { content: hostile }, observations: [{ text: hostile }], name: hostile })], summary: hostile, note: hostile })
  for (const view of surfaces(start(), finish(value))) {
    expect(view.container.textContent).not.toContain(hostile)
    assertQuiet(view.container)
    for (const summary of view.container.querySelectorAll('summary')) fireEvent.click(summary)
  }
  const invalid = render(<EventDetails event={finish(page({ record_read: { unsafe: hostile } }))} contextEvents={[start()]} />)
  expect(invalid.container.textContent).not.toContain(hostile)
  expect(fetch).not.toHaveBeenCalled()
  expect(xhr).not.toHaveBeenCalled()
})

test('legacy responses retain their old generic fallback and do not acquire provenance claims', () => {
  const value = page()
  delete value.record_read
  const receipt = finish(value), started = start()
  expect(parsed(value)).toBeUndefined()
  const native = render(<NativeReceiptRead event={receipt} />)
  expect(native.container.textContent).toBe('')
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toContain('工具已完成')
    expect(view.container.textContent).not.toContain('已存回执读取待核对')
    expect(view.container.textContent).not.toContain('来源事件序号')
  }
  expect(activity(started, receipt).warning).toBe(false)
})

test('EventDetailsList recovers only the exact start, without mutating the input fixture', () => {
  const events = [start({ receipt_seq: 20 }), finish()]
  const saved = JSON.stringify(events)
  const view = render(<EventDetailsList events={events} />)
  expect(view.container.querySelector('[aria-label="事件 #101 详情"]')?.textContent).toContain('已存回执已读取')
  expect(JSON.stringify(events)).toBe(saved)
})

test.each(['page', 'exact', 'pending', 'file_reads'])('concurrent other-role records before query finish remain readable: %s', mode => {
  const concurrent = row(105, { started_seq: 103, message_seq: 102, role: 'test' })
  const args: Data = mode === 'exact' ? { receipt_seq: 105 } : mode === 'pending' ? { include_pending: true }
    : mode === 'file_reads' ? { section: 'file_reads' } : {}
  const value = mode === 'file_reads' ? files({ observations: [observation({ seq: 105, started_seq: 103, message_seq: 102,
    role: 'test', read_args: { receipt_seq: 105 } })] }) : mode === 'pending' ? page({ pending_operations: [
    { ...concurrent, seq: 103, started_seq: 103, outcome: 'unsettled', run_ended: false, replay_authorized: false },
  ], pending_has_more: false, pending_next_before_seq: null }) : page({ receipts: [concurrent] })
  const started = start(args), receipt = { ...finish(value), seq: 110 }
  const read = receiptReadProvenance(receiptReadArguments(receipt, [started]))
  expect(read?.valid).toBe(true)
  expect(read?.sources?.some(source => source.seq > started.seq && source.seq < receipt.seq)).toBe(true)
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toContain(read!.status)
    expect(view.container.textContent).toContain(mode === 'pending' ? '未结算操作 #103' : mode === 'file_reads' ? '文件读取 #105' : '回执 #105')
    assertQuiet(view.container)
  }
  expect(activity(started, receipt)).toMatchObject({ warning: false, moving: false })
  expect(activity(started, receipt).text).toContain(read!.status)
})

test.each(['receipts', 'pending_operations', 'observations'])('records at or after query finish are rejected: %s', key => {
  for (const seq of [101, 102]) {
    const value = key === 'observations' ? files({ observations: [observation({ seq, read_args: { receipt_seq: seq } })] })
      : key === 'pending_operations' ? page({ pending_operations: [row(seq, { started_seq: seq })],
        pending_has_more: false, pending_next_before_seq: null }) : page({ receipts: [row(seq)] })
    reject(value, key === 'observations' ? { section: 'file_reads' } : key === 'pending_operations' ? { include_pending: true } : {})
  }
})

test.each(['./a.txt', '.\\a.txt', 'dir/./a.txt', 'dir//a.txt', 'dir\\a.txt', 'a.txt/'])('literal backend file filter can validly match nothing: %s', file_path => {
  const args = { section: 'file_reads', file_path }, value = files({ observations: [], query_state: 'no_matching_records' })
  expect(parsed(value, args)).toMatchObject({ valid: true, status: '当前筛选无匹配文件读取记录' })
  for (const view of surfaces(start(args), finish(value))) {
    expect(view.container.textContent).toContain('当前筛选无匹配文件读取记录')
    expect(view.container.textContent).toContain('不证明文件从未读过')
    assertQuiet(view.container)
  }
  expect(activity(start(args), finish(value)).warning).toBe(false)
})

test('literal filters are not normalized to manufacture matches, and row paths remain canonical', () => {
  reject(files(), { section: 'file_reads', file_path: './docs/readme.txt' })
  reject(files({ observations: [observation({ path: './docs/readme.txt' })] }), { section: 'file_reads' })
  for (const file_path of [null, {}, '', 'a'.repeat(2049)]) {
    reject(files({ observations: [], query_state: 'no_matching_records' }), { section: 'file_reads', file_path })
  }
})

const fileErrors = [
  ['file_read_scope_changed', '文件读取来源范围已变化，需重新查询', '目录绑定已变化，请重新查询来源。'],
  ['file_read_metadata_unavailable', '文件读取元数据暂不可用', '文件读取元数据暂不可用，请稍后重新查询来源；未回退加载整段历史正文。'],
]
const fileError = (code: unknown): Data => ({ status: 'error', code, record_read: marker(),
  message: 'PRIVATE_BODY<img src=x onerror=alert(1)>', content: 'PRIVATE_BODY', note: 'PRIVATE_BODY' })

test.each(fileErrors)('trusted file query error %s shows only a fixed recovery note and stays failed', (code, status, errorNote) => {
  const fetch = vi.fn(), xhr = vi.spyOn(XMLHttpRequest.prototype, 'open')
  vi.stubGlobal('fetch', fetch)
  const started = start({ section: 'file_reads' }), receipt = finish(fileError(code))
  delete receipt.payload.status
  expect(receipt.payload).not.toHaveProperty('arguments')
  const saved = JSON.stringify([started, receipt])
  expect(receiptReadProvenance(receiptReadArguments(receipt, [started]))).toMatchObject({ valid: false, status, errorNote })
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toContain(status)
    expect(view.container.textContent).toContain(errorNote)
    expect(view.container.textContent).not.toMatch(/工具已完成|已存回执已读取|本次查询已到末页|当前筛选无匹配/)
    expect(view.container.querySelector('[aria-label="已读取的来源事件序号"]')).toBeNull()
    assertQuiet(view.container)
  }
  expect(activity(started, receipt)).toMatchObject({ warning: true, moving: false })
  expect(activity(started, receipt).text).toContain(status)
  expect(JSON.stringify([started, receipt])).toBe(saved)
  expect(fetch).not.toHaveBeenCalled()
  expect(xhr).not.toHaveBeenCalled()
})

test.each(['unknown', {}, null, ...fileErrors.map(([code]) => code)])('error recovery remains gated by code, native pairing and metadata: %j', code => {
  const known = typeof code === 'string' && fileErrors.some(([trusted]) => trusted === code)
  for (const mode of known ? ['host', 'pair', 'metadata', 'section', 'contradiction', 'payload-status'] : ['code']) {
    const started = start({ section: 'file_reads' }), value = fileError(code), receipt = finish(value)
    delete receipt.payload.status
    if (mode === 'host') receipt.actor = { ...host, kind: 'agent' }
    if (mode === 'pair') receipt.payload.call_id = 'other-call'
    if (mode === 'metadata') value.record_read = { ...marker(), reader_run_id: originalRun }
    if (mode === 'section') started.payload.arguments = {}
    if (mode === 'contradiction') value.observations = []
    if (mode === 'payload-status') receipt.payload.status = 'ok'
    for (const view of surfaces(started, receipt)) {
      expect(view.container.textContent).not.toContain('文件读取来源范围已变化，需重新查询')
      expect(view.container.textContent).not.toContain('文件读取元数据暂不可用')
      expect(view.container.textContent).toMatch(/待核对|等待回执/)
      assertQuiet(view.container)
    }
    expect(activity(started, receipt).text).not.toMatch(/文件读取来源范围已变化|文件读取元数据暂不可用/)
  }
})
