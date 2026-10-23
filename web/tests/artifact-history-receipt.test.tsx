import { fireEvent, render, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { artifactHistoryReceipt } from '../src/artifactHistoryReceipt'
import { NativeArtifactReceipt } from '../src/NativeArtifactReceipt'
import { EventDetails } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity, nativeArtifactReceiptStatus } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const success = '成果发布历史已读取，未读正文'
const sha = 'a'.repeat(64)
const row = (seq: number, extra = {}) => ({ seq, filename: 'result.txt', actor: 'retired-publisher',
  source: 'team', format: 'text', content_bytes: 13, content_sha256: sha, ...extra })
// Matches cater.team's read_artifact(section="versions") metadata projection.
const result = (extra = {}): Record<string, unknown> => ({ status: 'ok', section: 'versions', artifact_seq: 5,
  filename: 'result.txt', scope: 'same_filename_in_this_chat', versions: [row(5), row(15)],
  versions_total: 2, has_more: false, next_seq: 15, ...extra })
const started = (args = {}): TeamEvent => ({ seq: 90, type: 'team.tool_started', actor: host,
  payload: { name: 'read_artifact', role: 'dev', run_id: 'history-run', message_seq: 1, call_id: 'history',
    arguments: { artifact_seq: 5, section: 'versions', ...args } } })
const finished = (value: unknown = result(), args = {}, payload = {}): TeamEvent => ({ seq: 91,
  type: 'team.tool_finished', actor: host, payload: { ...started(args).payload, started_seq: 90,
    status: 'ok', result: value, ...payload } })
// Expanded display fixtures have already associated start arguments. Durable
// finish records omit them; split-event coverage below exercises that boundary.
const surfaces = (event: TeamEvent) => [render(<NativeArtifactReceipt event={event} />),
  render(<EventDetails event={event} />), render(<TeamToolProgress events={[event]} names={{ dev: '开发' }} />)]
const rejected = (event: TeamEvent) => {
  expect(artifactHistoryReceipt(event)).toBeUndefined()
  expect(nativeArtifactReceiptStatus(event)).not.toBe(success)
  for (const view of surfaces(event)) {
    expect(within(view.container).queryByLabelText('成果发布历史回执')).toBeNull()
    expect(view.container.textContent).not.toMatch(/成果正文已读取|成果本页已读取|工程文件清单已读取|正文不可读|PRIVATE_|\[object Object\]/)
    expect(view.container.querySelector('img, script, a, iframe')).toBeNull()
    if (['team.tool_started', 'team.tool_finished'].includes(event.type)
      && (event.payload.name ?? event.payload.tool) === 'read_artifact') expect(view.container.querySelector('pre')).toBeNull()
    view.unmount()
  }
}

test('expanded metadata renders quietly in both existing entry points, with a collapsed sequence list', () => {
  const event = finished()
  expect(nativeArtifactReceiptStatus(event)).toBe(success)
  expect(artifactHistoryReceipt(event)?.versions).toEqual([{ seq: 5 }, { seq: 15 }])
  for (const view of surfaces(event)) {
    const receipt = within(view.container).getByLabelText('成果发布历史回执')
    expect(receipt.textContent).toContain('成果发布历史 · result.txt')
    expect(receipt.textContent).toContain('本页 2 条 / 共 2 条 · 所选成果 #5')
    expect(receipt.textContent).toContain('仅按本群确切文件名')
    expect(receipt.textContent).toContain('同名不代表同一目标或返修关系')
    expect(receipt.textContent).toContain('未读取正文或当前磁盘文件')
    expect(receipt.textContent).toContain('不代表复核通过、验证完成或任务交付')
    expect(receipt.textContent).not.toMatch(/retired-publisher|SHA-256|全文|工程包|成果正文|原始事件 JSON/)
    const list = within(receipt).getByLabelText('本页发布序号')
    expect(within(list).getAllByRole('listitem').map(item => item.textContent)).toEqual(['发布版本 #5', '发布版本 #15'])
    const disclosure = list.closest('details')!
    expect(disclosure.open).toBe(false)
    fireEvent.click(within(disclosure).getByText('发布序号 · 本页 2 条'))
    expect(disclosure.open).toBe(true)
    expect(view.container.querySelector('pre, button, a')).toBeNull()
    view.unmount()
  }
})

test('durable start and finish records recover matching arguments without modifying either event', () => {
  const start = started()
  const end = finished()
  delete end.payload.arguments
  const before = JSON.stringify([start, end])
  const view = render(<EventDetails event={end} contextEvents={[start, end]} />)
  expect(within(view.container).getByLabelText('成果发布历史回执').textContent).toContain('本页 2 条 / 共 2 条')
  expect(view.container.textContent).toContain(success)
  expect(JSON.stringify([start, end])).toBe(before)
})

test.each(['absent', 'duplicate', 'actor', 'role', 'run', 'call', 'name', 'late', 'message'])
  ('durable arguments are not guessed from an invalid start binding: %s', kind => {
    const start = started(), end = finished()
    delete end.payload.arguments
    if (kind === 'actor') start.actor = { ...host, kind: 'agent' }
    if (kind === 'role') start.payload.role = 'another'
    if (kind === 'run') start.payload.run_id = 'another'
    if (kind === 'call') start.payload.call_id = 'another'
    if (kind === 'name') start.payload.name = 'read_background'
    if (kind === 'late') start.seq = end.seq + 1
    if (kind === 'message') start.payload.message_seq = 0
    const events = kind === 'absent' ? [end] : kind === 'duplicate' ? [start, { ...start }, end] : [start, end]
    const view = render(<EventDetails event={end} contextEvents={events} />)
    expect(within(view.container).queryByLabelText('成果发布历史回执')).toBeNull()
    expect(view.container.textContent).not.toContain(success)
  })

test('archive and legacy metadata rows do not enter body, ZIP manifest, or member rendering', () => {
  for (const version of [row(5, { format: 'archive' }),
    { seq: 5, filename: 'result.txt', actor: 'historical-import', content_error: 'invalid_content' },
    { seq: 5, filename: 'result.txt' }]) {
    const event = finished(result({ versions: [version], versions_total: 1, next_seq: 5 }))
    expect(nativeArtifactReceiptStatus(event)).toBe(success)
    const view = render(<NativeArtifactReceipt event={event} />)
    expect(view.container.textContent).toContain('发布版本 #5')
    expect(view.container.querySelector('pre')).toBeNull()
    view.unmount()
  }
})

test('first, intermediate, final, and after-end pages preserve metadata counts and cursor meaning', () => {
  const cases = [
    { args: { limit: 1 }, extra: { versions: [row(5)], has_more: true, next_seq: 5 } },
    { args: { after_seq: 5, limit: 1 }, extra: { versions: [row(15)], versions_total: 3, has_more: true } },
    { args: { after_seq: 5, limit: 1 }, extra: { versions: [row(15)] } },
    { args: { after_seq: 15 }, extra: { versions: [], next_seq: 15 } },
    { args: { after_seq: 10000 }, extra: { versions: [], next_seq: 10000 } },
  ]
  for (const { args, extra } of cases) {
    const event = finished(result(extra), args)
    expect(nativeArtifactReceiptStatus(event)).toBe(success)
    const view = render(<NativeArtifactReceipt event={event} />)
    expect(view.container.textContent).toContain(`本页 ${extra.versions.length} 条 / 共 ${extra.versions_total ?? 2} 条`)
    expect(view.container.textContent).toContain(extra.has_more ? `还有后续发布记录 · 本页末条 #${extra.next_seq ?? 15}。` : '已到发布历史末页')
    expect(view.container.textContent).not.toContain('after_seq')
    expect(view.container.textContent).not.toMatch(/已读取全文|本页覆盖全文|成果正文已读取/)
    if (!extra.versions.length) expect(view.container.textContent).toContain('此游标之后没有发布记录')
    view.unmount()
  }
})

test('default limit 20 and explicit maximum 50 are enforced without truncating inconsistent rows', () => {
  for (const limit of [20, 50]) {
    const versions = Array.from({ length: limit }, (_, index) => row(index + 5))
    const value = result({ versions, versions_total: limit + 1, has_more: true, next_seq: limit + 4 })
    const args = limit === 20 ? {} : { limit }
    expect(nativeArtifactReceiptStatus(finished(value, args))).toBe(success)
    rejected(finished({ ...value, versions: [...versions, row(limit + 5)], next_seq: limit + 5 }, args))
  }
})

test.each(['status', 'section', 'artifact_seq', 'filename', 'scope', 'versions', 'versions_total', 'has_more', 'next_seq'])(
  'missing required result field %s never claims metadata or body success', key => {
    const value = result()
    delete value[key]
    rejected(finished(value))
  })

test.each([
  { artifact_seq: '5' }, { artifact_seq: 0 }, { artifact_seq: true }, { artifact_seq: 15 },
  { filename: '' }, { filename: { private: 'PRIVATE_NAME' } }, { filename: 'x'.repeat(4097) },
  { scope: 'other_chat' }, { scope: {} }, { section: 'future' }, { status: 'future' },
  { versions: 'PRIVATE_JSON' }, { versions: {} }, { versions: [null] }, { versions: [[]] },
  { versions: [row(15), row(5)] }, { versions: [row(5), row(5)], next_seq: 5 },
  { versions: [row(5), row(91)], next_seq: 91 }, { versions: [row(15), row(20)], next_seq: 20 },
  { versions: [row(5), row(15, { filename: 'Result.txt' })] },
  { versions_total: 0 }, { versions_total: 1 }, { versions_total: 3 }, { versions_total: '2' },
  { versions_total: true }, { versions_total: 1.5 }, { versions_total: Number.MAX_SAFE_INTEGER },
  { next_seq: 5 }, { next_seq: '15' }, { next_seq: {} }, { has_more: 'false' }, { has_more: true },
  { complete: false }, { complete: {} }, { truncated: true }, { outcome: 'passed' },
])('invalid identity, ordering, pagination, or partial result is rejected: %j', extra => rejected(finished(result(extra))))

test.each([
  { artifact_seq: undefined }, { artifact_seq: 15 }, { section: undefined }, { section: 'content' },
  { after_seq: -1 }, { after_seq: '0' }, { after_seq: true }, { after_seq: null }, { after_seq: 5 },
  { limit: 0 }, { limit: 51 }, { limit: true }, { limit: '2' }, { limit: 1 }, { limit: undefined },
  { path: 'PRIVATE_MEMBER' }, { offset: 0 }, { length: 1 }, { path: undefined }, { summary: 'PRIVATE_SUMMARY' },
])('arguments must bind an exact versions request without body or member parameters: %j', args => rejected(finished(result(), args)))

test.each(['content', 'summary', 'local_path', 'archive', 'read_range', 'path', 'seq', 'approval', 'title'])(
  'mixed %s metadata at either result or row level never leaks or becomes body evidence', key => {
    for (const value of [result({ [key]: { private: 'PRIVATE_MIXED' } }),
      result({ versions: [row(5), row(15, { [key]: { private: 'PRIVATE_MIXED' } })] })]) rejected(finished(value))
  })

test('either history hint blocks a legacy full-body result from falling through, even with an invalid envelope', () => {
  const content = 'PRIVATE_BODY'
  const body = { status: 'ok', seq: 5, filename: 'result.txt', content, content_bytes: content.length, content_sha256: sha }
  for (const event of [
    finished(body),
    finished({ ...body, section: 'versions' }, { section: 'content' }),
    finished({ ...body, section: 'versions' }, {}, { arguments: undefined }),
    finished(Object.assign(Object.create({}), body, { section: 'versions' }), { section: 'content' }),
    finished({ ...body, versions: [row(5)] }, { section: 'content' }),
  ]) rejected(event)
})

test.each([
  { seq: '15' }, { seq: -1 }, { seq: 1.5 }, { filename: {} }, { actor: {} }, { source: [] },
  { format: {} }, { content_error: {} }, { content_bytes: -1 }, { content_bytes: '13' },
  { content_sha256: 'bad' }, { content_sha256: {} }, { actor: 'x'.repeat(257) },
])('malformed optional row metadata cannot reach React as an object: %j', extra => {
  rejected(finished(result({ versions: [row(5), row(15, extra)] })))
})

test('counter consistency accounts for prior records, missing anchor, full pages, and empty tails', () => {
  const cases = [
    finished(result({ versions: [], next_seq: 0 })),
    finished(result({ versions: [], has_more: true, next_seq: 15 }), { after_seq: 15, limit: 1 }),
    finished(result({ versions: [], next_seq: 14 }), { after_seq: 15 }),
    finished(result({ versions: [row(15)], versions_total: 1 }), { after_seq: 5 }),
    finished(result({ versions: [row(15)], versions_total: 2, has_more: true }), { after_seq: 5, limit: 1 }),
    finished(result({ versions: [row(15)], versions_total: 2 }), { after_seq: 1 }),
    finished(result({ versions: [], next_seq: 4 }), { after_seq: 4 }),
    finished(result({ versions: [row(15)], has_more: true }), { limit: 1 }),
  ]
  for (const event of cases) rejected(event)
  expect(nativeArtifactReceiptStatus(finished(result({ artifact_seq: 15, versions: [row(5)],
    has_more: true, next_seq: 5 }), { artifact_seq: 15, limit: 1 }))).toBe(success)
})

test('native host, finished type, name alias, request, and success envelope are required', () => {
  const original = finished()
  for (const event of [
    { ...original, type: 'team.tool_started' }, { ...original, type: 'workspace.tool_finished' },
    { ...original, seq: 15 }, { ...original, actor: Object.create(host) },
    ...['id', 'kind', 'role'].map(key => ({ ...original, actor: { ...host, [key]: 'other' } })),
    ...[undefined, null, [], JSON.stringify(result()), Object.create(result())].map(value =>
      ({ ...original, payload: { ...original.payload, result: value } })),
    ...[undefined, null, [], 'PRIVATE_ARGS'].map(arguments_ => finished(result(), {}, { arguments: arguments_ })),
    ...['future', 'error', 'failed', 'blocked', 'cancelled'].map(status => finished(result(), {}, { status })),
    ...['future', 'error', 'failed'].map(status => finished(result({ status }))),
    finished(result(), {}, { name: 'workspace_read' }),
  ]) rejected(event)
  expect(nativeArtifactReceiptStatus(finished(result(), {}, { name: undefined, tool: 'read_artifact' }))).toBe(success)
  expect(nativeArtifactReceiptStatus(finished(result({ complete: true, truncated: false }), {}, { status: undefined }))).toBe(success)
})

test('literal HTML filename stays text; clicks do not fetch, read bodies, or request approval', () => {
  const fetch = vi.fn(() => { throw new Error('Unexpected request') })
  vi.stubGlobal('fetch', fetch)
  const filename = '<img src=x onerror=alert(1)>.txt'
  const event = finished(result({ filename, versions: [row(5, { filename }), row(15, { filename })] }))
  for (const view of surfaces(event)) {
    expect(view.container.textContent).toContain(filename)
    for (const summary of view.container.querySelectorAll('summary')) fireEvent.click(summary)
    expect(view.container.querySelector('img, script, a, iframe, pre, button, input')).toBeNull()
    expect(view.container.textContent).not.toMatch(/全文指纹|成果正文已读取|原始事件 JSON|开发者详情|content_sha256/)
    view.unmount()
  }
  expect(fetch).not.toHaveBeenCalled()
})

test('rebound history pages collapse the old sequence disclosure and preserve wrapping and bounds', () => {
  const filename = `${'长文件名'.repeat(100)}.txt`
  const event = finished(result({ filename, versions: [row(5, { filename }), row(15, { filename })] }))
  const view = render(<NativeArtifactReceipt event={event} />)
  const receipt = within(view.container).getByLabelText('成果发布历史回执')
  expect(receipt.className).toContain('min-w-0')
  expect(receipt.className).toContain('max-w-full')
  expect(receipt.className).toContain('[overflow-wrap:anywhere]')
  const list = within(receipt).getByLabelText('本页发布序号')
  expect(list.className).toContain('max-h-48')
  expect(list.className).toContain('overflow-y-auto')
  expect(list.className).toContain('overflow-x-hidden')
  expect(list.tabIndex).toBe(0)
  expect(receipt.querySelector('[class*="rounded"], table, details details')).toBeNull()
  fireEvent.click(view.container.querySelector('summary')!)
  expect(view.container.querySelector('details')?.open).toBe(true)
  view.rerender(<NativeArtifactReceipt event={finished(result({ versions: [row(15)] }), { after_seq: 5 })} />)
  expect(view.container.querySelector('details')?.open).toBe(false)
  expect(view.container.textContent).not.toContain('发布版本 #5')
  view.rerender(<NativeArtifactReceipt event={finished(result({ versions: [{ seq: { private: 'PRIVATE_ROW' } }] }))} />)
  expect(view.container.innerHTML).toBe('')
})

test('paired progress and conversation activity share the validated metadata status and warning boundary', () => {
  const team: TeamState = { enabled: true, objective: '当前目标', status: 'running', revision: 0,
    runs: [], assignments: [], pending_count: 0, checkpoint: '' }
  const event = finished()
  const view = render(<TeamToolProgress events={[started(), event]} names={{ dev: '开发' }} />)
  expect(view.container.querySelector('details')?.open).toBe(false)
  expect(within(view.container).getAllByLabelText('成果发布历史回执')).toHaveLength(1)
  expect(view.container.textContent).toContain(success)
  const activity = (receipt: TeamEvent) => conversationActivity({ team, events: [
    { seq: 1, type: 'team.message_received', actor: { id: 'human', kind: 'human', role: 'human' },
      payload: { text: '读取历史', recipient: 'dev', kind: 'message' } },
    { seq: 2, type: 'team.run_started', actor: host, payload: { role: 'dev', run_id: 'history-run', message_seq: 1, revision: 0 } },
    started(), receipt,
  ], names: { dev: '开发' }, leaderId: 'manager', sending: false, executionState: 'running' }).lines.find(line => line.role === 'dev')
  expect(activity(event)?.text).toContain(success)
  expect(activity(event)?.warning).toBe(false)
  expect(activity(finished(result({ has_more: true })))?.warning).toBe(true)
})
