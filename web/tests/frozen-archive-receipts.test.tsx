import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fireEvent, render } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails, EventDetailsList } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { NativeFrozenArchiveReceipt } from '../src/NativeFrozenArchiveReceipt'
import { conversationActivity, frozenArchiveReadEvent, nativeFrozenArchiveReceiptStatus } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

type Data = Record<string, unknown>
const obj = (value: unknown) => value as Data
const host = { id: 'team', kind: 'system', role: 'team' }
const event = (seq: number, type: string, payload: Data, actor = host): TeamEvent => ({ seq, type, payload, actor })
// Exact hashes, anchors and member data from the native 9 -> derive -> 23 fixture.
const zipHash = '3c9d5ecd61e8c21ae6b2704c0307d89aa9a3893978ea03c33dec8ca4993aa94d'
const eventHash = '1647e5ae7e27a8424fdec1a8eaf9d1afd6dc1f6280d5149ca4130797fd6aeaa7'
const snapshot = { captured_at: '2026-10-17T06:09:22.677468+00:00', source_hash: '30459b7c55f2593efbbb2439ec2142017ebcf1d48f5e4ed131fc0fa9cd6e8e5d',
  source_seq: 15, source_task_id: 'task_34c73de0a4' }
const source = { content_sha256: zipHash, event_hash: eventHash, filename: 'project.zip', format: 'archive', seq: 9,
  task_id: 'task_34c73de0a4', verification: 'unverified' }
const files = [
  { bytes: 256, executable: false, path: 'assets/图像.bin', sha256: '40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880' },
  { bytes: 18, executable: false, path: 'docs/说明.md', sha256: '1c1eb4e9d9e40e20d252cfc4694d34644f3c03e5da9090687e492d7b46bcaf16' },
  { bytes: 0, executable: false, path: 'empty.txt', sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' },
]
const envelope = (archive_read: Data, offset = 0): Data => ({ archive_read, complete: true, historical_only: true, offset,
  section: 'artifact', snapshot: { ...snapshot }, source: { ...source }, status: 'ok' })
const manifest = () => envelope({ archive: { files, total_bytes: 274, version: 1 }, content_bytes: 604,
  content_sha256: zipHash, filename: 'project.zip', format: 'archive', seq: 9, status: 'ok' })
const member = (index = 1, offset = 0, content = '甲😀', total = 6): Data => envelope({ archive_sha256: zipHash, artifact_seq: 9,
  bytes: files[index].bytes, content, content_kind: 'utf8', executable: false, path: files[index].path,
  read_range: { content_complete: offset === 0 && Array.from(content).length === total, has_more: offset + Array.from(content).length < total,
    next_offset: offset + Array.from(content).length < total ? offset + Array.from(content).length : null,
    offset, returned_chars: Array.from(content).length, total_chars: total, unit: 'unicode_codepoints' },
  sha256: files[index].sha256, status: 'ok' }, offset)
const binary = () => envelope({ archive_sha256: zipHash, artifact_seq: 9, ...files[0], content: null, content_kind: 'binary', status: 'ok' })
const start = (args: Data = {}) => event(33, 'team.tool_started', { name: 'read_background', role: 'dev', run_id: 'run', message_seq: 1,
  call_id: 'frozen-read', arguments: { section: 'artifact', artifact_seq: 9, ...args } })
const finish = (value: unknown, started: TeamEvent): TeamEvent => event(34, 'team.tool_finished', {
  ...started.payload, started_seq: started.seq, status: 'ok', result: value })
const team: TeamState = { enabled: true, objective: '读取冻结工程', revision: 0, status: 'running', pending_count: 0, runs: [], assignments: [], checkpoint: '' }
const activityLine = (started: TeamEvent, receipt: TeamEvent) => conversationActivity({ team, names: {}, leaderId: 'manager', sending: false,
  executionState: 'paused', events: [event(1, 'team.message_received', { kind: 'message', recipient: 'dev', text: '读取' }, { id: 'human', kind: 'human', role: 'human' }),
    event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }), started, receipt] }).lines.find(line => line.role === 'dev')!
const surfaces = (started: TeamEvent, receipt: TeamEvent) => [render(<EventDetails event={receipt} />),
  render(<TeamToolProgress events={[started, receipt]} names={{}} full />)]
const cases: [Data, Data, string][] = [
  [manifest(), {}, '工程文件清单已读取'],
  [member(), { path: files[1].path, limit: 2 }, '工程文件本页已读取'],
  [member(1, 6, ''), { path: files[1].path, offset: 6, limit: 2 }, '工程文件本页已读取'],
  [binary(), { path: files[0].path }, '工程二进制字节已核对'],
  [member(2, 0, '', 0), { path: files[2].path }, '工程文件正文已读取'],
]

test.each(cases)('真实合同两表面及activity显示冻结旧版本: %j', (value, args, status) => {
  const started = start(args), receipt = finish(value, started)
  expect(nativeFrozenArchiveReceiptStatus(receipt)).toBe(status)
  const line = activityLine(started, receipt)
  expect(line.warning).toBe(false)
  expect(line.moving).toBe(false)
  expect(line.text).toContain('冻结历史 · 来源群 task_34c73de0a4 · 原成果 #9 · 冻结截止 #15')
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toContain(status)
    expect(view.container.textContent).toContain('冻结历史参考 · 来源群 task_34c73de0a4 · 原成果 #9 · project.zip')
    expect(view.container.textContent).toContain('冻结截止 #15')
    expect(view.container.textContent).toContain('不继承原验收结论')
    expect(view.container.querySelector('a, button, img, script, iframe')).toBeNull()
    expect([...view.container.querySelectorAll('details')].every(detail => !detail.open)).toBe(true)
    if (status === '工程二进制字节已核对' || status === '工程文件清单已读取') expect(view.container.querySelector('pre')).toBeNull()
    if (value.offset === 6) {
      expect(view.container.textContent).toContain('已到末页，无下一页')
      expect(view.container.textContent).toContain('末页不代表已读取全文')
    }
  }
})

const fixtureUrl = resolve(process.cwd(), '../.cater/qa/frozen-archive-20261017/fixture.json')
test.skipIf(!existsSync(fixtureUrl))('本地原生fixture的五份read_results逐份与真实工具参数配对', () => {
  const fixture = JSON.parse(readFileSync(fixtureUrl, 'utf8')) as { read_results: Data[]; detail: { events: TeamEvent[] } }
  const starts = fixture.detail.events.filter(item => item.type === 'team.tool_started' && item.payload.name === 'read_background')
  expect(fixture.read_results).toHaveLength(5)
  fixture.read_results.forEach((value, index) => {
    const started = starts[index], raw = fixture.detail.events.find(item => item.payload.started_seq === started.seq)!
    const receipt = { ...raw, payload: { ...started.payload, ...raw.payload } }
    expect(value).toEqual(raw.payload.result)
    expect(nativeFrozenArchiveReceiptStatus(receipt)).toBe(cases[index][2])
    for (const view of surfaces(started, receipt)) expect(view.container.textContent).toContain(cases[index][2])
    const actual = render(<EventDetails event={raw} contextEvents={[started, raw]} />)
    expect(actual.container.textContent).toContain(cases[index][2])
  })
})

test('同组审计使用发起参数，不反推请求或改写原事件', () => {
  const started = start({ path: files[1].path, limit: 2 }), receipt = finish(member(), started)
  delete receipt.payload.arguments
  delete receipt.payload.call_id
  delete receipt.payload.message_seq
  const saved = JSON.stringify([started, receipt])
  const lone = render(<EventDetails event={receipt} />)
  expect(lone.container.textContent).toContain('待核对')
  expect(lone.container.querySelector('pre')).toBeNull()
  lone.unmount()
  const view = render(<EventDetailsList events={[started, receipt]} />)
  const detail = view.container.querySelector('[aria-label="事件 #34 详情"]')!
  expect(detail.textContent).toContain('工程文件本页已读取')
  expect(detail.querySelector('pre')?.textContent).toBe('甲😀')
  expect(JSON.stringify([started, receipt])).toBe(saved)
})

test('显式冲突调用ID不与发起事件拼接，即使started_seq相同', () => {
  const started = start({ path: files[1].path, limit: 2 }), receipt = finish(member(), started)
  delete receipt.payload.arguments
  receipt.payload.call_id = 'conflicting-call'
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(progress.container.textContent).not.toContain('工程文件本页已读取')
  expect(progress.container.textContent).not.toContain('甲😀')
  const audit = render(<EventDetails event={receipt} contextEvents={[started, receipt]} />)
  expect(audit.container.textContent).toContain('待核对')
  expect(audit.container.querySelector('pre')).toBeNull()
  expect(activityLine(started, receipt).text).not.toContain('工程文件本页已读取')
})

test.each(['run_id', 'call_id', 'role', 'message_seq', 'source', 'type', 'name', 'duplicate', 'future', 'explicit-args'])('同组审计不拼接不匹配的%s', mode => {
  const started = start({ path: files[1].path, limit: 2 }), receipt = finish(member(), started)
  delete receipt.payload.arguments
  if (['run_id', 'call_id', 'role'].includes(mode)) started.payload[mode] = 'other'
  if (mode === 'message_seq') started.payload.message_seq = 2
  if (mode === 'source') started.actor = { ...host, kind: 'agent' }
  if (mode === 'type') started.type = 'team.message_received'
  if (mode === 'name') started.payload.name = 'read_artifact'
  if (mode === 'future') { started.seq = 35; receipt.payload.started_seq = 35 }
  if (mode === 'explicit-args') receipt.payload.arguments = null
  const events = mode === 'duplicate' ? [started, { ...started }] : [started]
  const view = render(<EventDetails event={receipt} contextEvents={events} />)
  expect(view.container.textContent).toContain('待核对')
  expect(view.container.querySelector('pre')).toBeNull()
  expect(view.container.textContent).not.toContain('甲😀')
})

function reject(value: Data, args: Data = { path: files[1].path, limit: 2 }, payload: Data = {}) {
  const started = start(args), receipt = finish(value, started)
  Object.assign(receipt.payload, payload)
  expect(frozenArchiveReadEvent(receipt)).toBeUndefined()
  expect(activityLine(started, receipt).warning).toBe(true)
  for (const view of surfaces(started, receipt)) {
    expect(view.container.textContent).toMatch(/待核对|未完成/)
    expect(view.container.textContent).not.toMatch(/工程文件.*已读取|工程二进制字节已核对|甲😀|PRIVATE_|原始事件 JSON/)
    expect(view.container.querySelector('pre, a, script, iframe')).toBeNull()
  }
}

test.each([
  { complete: false }, { complete: undefined }, { historical_only: false }, { historical_only: undefined }, { section: 'source' },
  { status: 'partial' }, { status: 'unknown' }, { status: 'error' }, { truncated: true }, { outcome: 'partial' },
  { offset: 1 }, { offset: '0' }, { offset: undefined }, { snapshot: null }, { source: null }, { archive_read: null },
])('外层合同不完整拒绝 %j', extra => reject({ ...member(), ...extra }))

test.each([
  { task_id: '' }, { seq: 23 }, { seq: '9' }, { format: 'text' }, { filename: '../project.zip' },
  { content_sha256: 'b'.repeat(64) }, { content_sha256: 'bad' }, { event_hash: undefined }, { event_hash: 'bad' }, { verification: 'approved' }, { verification: ['unverified'] },
])('来源版本与嵌套不一致拒绝 %j', extra => reject({ ...member(), source: { ...source, ...extra } }))

test.each([
  { source_seq: 8 }, { source_seq: '15' }, { source_seq: undefined }, { source_hash: undefined }, { source_hash: 'bad' },
  { source_task_id: '' }, { captured_at: undefined }, { captured_at: 'yesterday' }, { captured_at: '2026-10-17' },
])('冻结锚点缺失或早于原发布拒绝 %j', extra => reject({ ...member(), snapshot: { ...snapshot, ...extra } }))

test.each([
  { path: '../secret' }, { path: files[0].path }, { path: undefined }, { artifact_seq: 23 }, { artifact_seq: '9' },
  { offset: -1 }, { offset: true }, { offset: 1 }, { limit: 1 }, { limit: 0 }, { limit: 16001 }, { limit: null },
  { length: 2 }, { task_id: 'other' }, { source_id: 'other' }, { section: 'source' },
])('请求参数不匹配拒绝 %j', extra => reject(member(), { path: files[1].path, limit: 2, ...extra }))

test.each([
  { artifact_seq: 23 }, { archive_sha256: 'b'.repeat(64) }, { sha256: 'bad' }, { status: 'partial' }, { complete: false },
  { content_kind: 'unknown' }, { path: '../secret' }, { bytes: 1 }, { content: null }, { read_range: undefined },
  { read_range: { ...obj(obj(member().archive_read).read_range), returned_chars: 3 } },
  { read_range: { ...obj(obj(member().archive_read).read_range), content_complete: true } },
])('共享成员校验拒绝错误分页及嵌套 %j', extra => reject({ ...member(), archive_read: { ...obj(member().archive_read), ...extra } }))

test.each(['partial', 'unknown', 'error', 'timeout'])('外层状态不能被ok覆盖 %s', status => reject(member(), { path: files[1].path, limit: 2 }, { status }))

test('清单禁止分页、坏清单与混合回执不能冒充成功', () => {
  for (const args of [{ offset: 0 }, { limit: 8000 }]) reject(manifest(), args)
  for (const extra of [{ seq: 23 }, { filename: 'new.zip' }, { content_sha256: 'b'.repeat(64) }, { archive: { version: 1, files: [], total_bytes: 0 } }, { content: 'PRIVATE_BODY' }]) {
    reject({ ...manifest(), archive_read: { ...obj(manifest().archive_read), ...extra } }, {})
  }
  reject({ ...binary(), archive_read: { ...obj(binary().archive_read), content: 'PRIVATE_BODY' } }, { path: files[0].path })
})

test('宿主三项身份、独立缺参数与发起阶段保持待核对', () => {
  for (const actor of [{ ...host, id: 'other' }, { ...host, kind: 'agent' }, { ...host, role: 'other' }]) {
    const started = start({ path: files[1].path, limit: 2 }), receipt = { ...finish(member(), started), actor }
    expect(render(<EventDetails event={receipt} />).container.textContent).toContain('冻结材料来源待核对')
    expect(render(<NativeFrozenArchiveReceipt event={receipt} />).container.textContent).toBe('')
    expect(render(<TeamToolProgress events={[started, receipt]} names={{}} />).container.textContent).toContain('等待回执')
    expect(activityLine(started, receipt).text).toContain('等待回执')
  }
  reject(member(), { path: files[1].path, limit: 2 }, { arguments: undefined })
  const started = start({ path: files[1].path })
  expect(render(<EventDetails event={started} />).container.textContent).toContain('等待回执')
})

test('后代快照允许祖先来源，两个hash不要求相等；相同截点必须绑定原事件', () => {
  const started = start(), value = { ...manifest(), snapshot: { ...snapshot, source_task_id: 'intermediate-parent' } }
  expect(nativeFrozenArchiveReceiptStatus(finish(value, started))).toBe('工程文件清单已读取')
  const same = { ...snapshot, source_seq: 9, source_hash: eventHash }
  expect(frozenArchiveReadEvent(finish({ ...manifest(), snapshot: same }, started))).toBeDefined()
  reject({ ...manifest(), snapshot: { ...same, source_hash: snapshot.source_hash } }, {})
})

test('正文纯文本安全、未知内部字段不泄露，旧来源文本仍走原路径', () => {
  const hostile = '<script>alert(1)</script>\n[link](javascript:alert(1))', value = member(1, 0, hostile, Array.from(hostile).length)
  obj(value.archive_read).bytes = new TextEncoder().encode(hostile).length
  value.summary = 'PRIVATE_SUMMARY'
  value.content = 'PRIVATE_OUTER'
  obj(value.archive_read).note = 'PRIVATE_NOTE'
  const started = start({ path: files[1].path })
  for (const view of surfaces(started, finish(value, started))) {
    expect(view.container.querySelector('pre')?.textContent).toBe(hostile)
    expect(view.container.querySelector('a, img, script, iframe')).toBeNull()
    expect(view.container.textContent).not.toContain('PRIVATE_')
  }
  const oldStart = start(), legacy = { status: 'ok', historical_only: true, section: 'artifact', content: 'OLD_TEXT_BODY', offset: 0, total_chars: 13, source: { seq: 9, filename: 'old.txt' } }
  const view = render(<TeamToolProgress events={[oldStart, finish(legacy, oldStart)]} names={{}} />)
  expect(view.container.textContent).toContain('OLD_TEXT_BODY')
  expect(view.container.textContent).not.toContain('待核对')
})

test('事件、来源、截点、范围或版本重绑定后清除所有展开状态', () => {
  const started = start({ path: files[1].path, limit: 2 }), first = finish(member(), started)
  const nextStart = start({ path: files[1].path, offset: 6, limit: 2 })
  const variants = [finish(member(1, 6, ''), nextStart), { ...first, seq: 50 },
    finish({ ...member(), snapshot: { ...snapshot, source_task_id: 'intermediate' } }, started),
    finish({ ...member(), source: { ...source, event_hash: 'c'.repeat(64) } }, started),
    finish({ ...member(), source: { ...source, seq: 10 }, archive_read: { ...obj(member().archive_read), artifact_seq: 10 } },
      start({ path: files[1].path, limit: 2, artifact_seq: 10 }))]
  for (const mode of ['direct', 'audit', 'progress']) for (const next of variants) {
    const component = (receipt: TeamEvent) => mode === 'direct' ? <NativeFrozenArchiveReceipt event={receipt} />
      : mode === 'audit' ? <EventDetails event={receipt} /> : <TeamToolProgress events={[started, receipt]} names={{}} />
    const view = render(component(first))
    const inner = [...view.container.querySelectorAll('details')].filter(detail => mode !== 'progress' || detail !== view.container.firstElementChild)
    for (const detail of inner) fireEvent.click(detail.querySelector('summary')!)
    expect(inner.some(detail => detail.open)).toBe(true)
    view.rerender(component(next))
    const details = [...view.container.querySelectorAll('details')].filter(detail => mode !== 'progress' || detail !== view.container.firstElementChild)
    expect(details.every(detail => !detail.open)).toBe(true)
  }
})

test('缺名结束事件沿用发起参数与tool别名，activity不误报', () => {
  const started = start({ path: files[1].path, limit: 2 }), receipt = finish(member(), started)
  delete receipt.payload.arguments
  delete receipt.payload.name
  const view = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(view.container.textContent).toContain('工程文件本页已读取')
  expect(activityLine(started, receipt).warning).toBe(false)
  started.payload.tool = started.payload.name
  delete started.payload.name
  expect(activityLine(started, receipt).text).toContain('读取来源材料 · 工程文件本页已读取')
})
