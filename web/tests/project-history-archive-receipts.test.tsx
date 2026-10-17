import { render } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { NativeProjectHistoryReceipt } from '../src/NativeProjectHistoryReceipt'
import { conversationActivity, nativeProjectHistoryReceiptStatus, projectHistoryArchiveReadEvent, projectHistoryReadArguments } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

type Data = Record<string, unknown>
const obj = (value: unknown) => value as Data
const hash = 'a'.repeat(64), zipHash = 'b'.repeat(64), memberHash = 'c'.repeat(64)
const host = { id: 'team', kind: 'system', role: 'team' }
const source = { project_id: 'project-a', task_id: 'old-task', delivery_seq: 50, delivery_hash: hash,
  artifact_seq: 40, content_sha256: zipHash, filename: 'project.zip', author: 'dev', format: 'archive', event_hash: memberHash }
const file = { path: 'src/main.txt', bytes: 7, sha256: memberHash, executable: false }
const envelope = (archive_read: Data, offset = 0): Data => ({ status: 'ok', complete: true, historical_only: true,
  section: 'artifact', source: { ...source }, as_of_seq: 100, as_of_hash: hash, offset, archive_read })
const manifest = () => envelope({ status: 'ok', seq: 40, filename: 'project.zip', content_sha256: zipHash,
  content_bytes: 200, format: 'archive', archive: { version: 1, total_bytes: 7, files: [{ ...file }] } })
const member = (content = 'A😀BC', offset = 0, total = 4, bytes = 7): Data => envelope({ status: 'ok', artifact_seq: 40,
  archive_sha256: zipHash, ...file, bytes, content, content_kind: 'utf8',
  read_range: { unit: 'unicode_codepoints', offset, returned_chars: Array.from(content).length, total_chars: total,
    has_more: offset + Array.from(content).length < total, next_offset: offset + Array.from(content).length < total ? offset + Array.from(content).length : null,
    content_complete: offset === 0 && Array.from(content).length === total } }, offset)
const binary = () => envelope({ status: 'ok', artifact_seq: 40, archive_sha256: zipHash, ...file, content_kind: 'binary', content: null })
const start = (args: Data = {}): TeamEvent => ({ seq: 110, type: 'team.tool_started', actor: host,
  payload: { name: 'read_project_history', project_scope: 'project-a', role: 'dev', run_id: 'run', call_id: 'history', message_seq: 1,
    arguments: { section: 'artifact', delivery_seq: 50, artifact_seq: 40, as_of_seq: 100, ...args } } })
const finish = (value: Data, started = start()): TeamEvent => ({ seq: 111, type: 'team.tool_finished', actor: host,
  payload: { ...started.payload, started_seq: started.seq, status: 'ok', result: value } })
const team: TeamState = { enabled: true, objective: 'Read history', status: 'running', revision: 0,
  runs: [], pending_count: 0, assignments: [], checkpoint: '' }
const activityLine = (started: TeamEvent, receipt: TeamEvent) => conversationActivity({ team, names: {}, leaderId: 'manager',
  sending: false, executionState: 'paused', events: [
    { seq: 1, type: 'team.message_received', actor: { id: 'human', kind: 'human', role: 'human' }, payload: { recipient: 'dev' } },
    { seq: 2, type: 'team.run_started', actor: host, payload: { role: 'dev', run_id: 'run', message_seq: 1 } }, started, receipt,
  ] }).lines.find(line => line.role === 'dev')!

const cases: [Data, Data, string][] = [
  [manifest(), {}, '工程文件清单已读取'],
  [member(), { path: file.path }, '工程文件正文已读取'],
  [member('A😀', 0, 4), { path: file.path, length: 2 }, '工程文件本页已读取'],
  [member('BC', 2, 4), { path: file.path, offset: 2, length: 2 }, '工程文件本页已读取'],
  [member('', 4, 4), { path: file.path, offset: 4 }, '工程文件本页已读取'],
  [member('', 0, 0, 0), { path: file.path }, '工程文件正文已读取'],
  [binary(), { path: file.path }, '工程二进制字节已核对'],
]

test.each(cases)('工程清单、成员、分页、空文件及二进制复用原生回执: %s', (value, args, status) => {
  const started = start(args), receipt = finish(value, started)
  expect(nativeProjectHistoryReceiptStatus(receipt)).toBe(status)
  expect(activityLine(started, receipt).text).toContain(status)
  expect(activityLine(started, receipt).warning).toBe(false)
  expect(projectHistoryArchiveReadEvent(receipt)?.payload.result).toBe(value.archive_read)
  for (const view of [render(<EventDetails event={receipt} />), render(<TeamToolProgress events={[started, receipt]} names={{}} full />)]) {
    const text = view.container.textContent ?? ''
    expect(text).toContain(status)
    expect(text).toContain('同项目历史工程包')
    expect(text).toContain('old-task')
    expect(text).toContain('目录版本：事件 #100')
    expect(text).not.toMatch(/原始事件 JSON|"archive_read"|工具已完成|历史正文共/)
    if (status === '工程文件清单已读取') {
      expect(text).toContain('未读取成员正文')
      expect(view.container.querySelector('pre')).toBeNull()
    } else if (status === '工程二进制字节已核对') {
      expect(text).toContain('不是文本正文读取')
      expect(view.container.querySelector('pre')).toBeNull()
    } else expect(text).toContain(status === '工程文件正文已读取' ? '本页覆盖全文' : '本页未覆盖全文')
  }
})

test('错误来源与失败包不显示任何工程内容', () => {
  const receipt = finish(manifest())
  const forged = { ...receipt, actor: { id: 'dev', kind: 'agent', role: 'dev' } }
  expect(nativeProjectHistoryReceiptStatus(forged)).toBe('历史回执来源待核对')
  const failed = finish({ status: 'error', complete: true, outcome: 'not_started', code: 'project_history_content_unavailable', message: '不可读取' })
  expect(nativeProjectHistoryReceiptStatus(failed)).toBe('未开始读取历史')
  for (const event of [forged, failed]) {
    expect(render(<NativeProjectHistoryReceipt event={event} />).container.textContent).toBe('')
    expect(render(<EventDetails event={event} />).container.textContent).not.toMatch(/同项目历史工程包|原始事件 JSON|工程文件清单已读取/)
  }
})

const invalid: [string, (event: TeamEvent) => void][] = [
  ['scope缺失', e => { delete e.payload.project_scope }],
  ['scope错配', e => { e.payload.project_scope = 'other' }],
  ['cutoff错配', e => { obj(e.payload.arguments).as_of_seq = 99 }],
  ['未来cutoff', e => { obj(e.payload.result).as_of_seq = 112; obj(e.payload.arguments).as_of_seq = 112 }],
  ['交付越界', e => { obj(e.payload.result).as_of_seq = 49; obj(e.payload.arguments).as_of_seq = 49 }],
  ['cutoff同事件哈希错配', e => { obj(e.payload.result).as_of_seq = 50; obj(e.payload.arguments).as_of_seq = 50; obj(e.payload.result).as_of_hash = zipHash }],
  ['交付错配', e => { obj(e.payload.arguments).delivery_seq = 51 }],
  ['成员版本错配', e => { obj(e.payload.arguments).artifact_seq = 41 }],
  ['来源缺任务', e => { delete obj(obj(e.payload.result).source).task_id }],
  ['来源缺发布人', e => { delete obj(obj(e.payload.result).source).author }],
  ['来源事件坏摘要', e => { obj(obj(e.payload.result).source).event_hash = 'invalid' }],
  ['ZIP摘要错配', e => { obj(obj(e.payload.result).source).content_sha256 = hash }],
  ['ZIP文件名错配', e => { obj(obj(e.payload.result).source).filename = 'other.zip' }],
  ['manifest+path', e => { obj(e.payload.arguments).path = file.path }],
  ['manifest显式offset0', e => { obj(e.payload.arguments).offset = 0 }],
  ['manifest显式length', e => { obj(e.payload.arguments).length = 1 }],
  ['未知参数', e => { obj(e.payload.arguments).task_id = 'old-task' }],
  ['limit不代替length', e => { obj(e.payload.arguments).limit = 2 }],
  ['混入顶层正文', e => { obj(e.payload.result).content = 'SECRET_RAW' }],
  ['未完成', e => { obj(e.payload.result).complete = false }],
  ['宿主失败', e => { e.payload.status = 'failed' }],
  ['坏清单', e => { obj(obj(obj(e.payload.result).archive_read).archive).total_bytes = 8 }],
]
test.each(invalid)('拒绝错误来源与范围：%s', (_label, mutate) => {
  const receipt = finish(manifest())
  mutate(receipt)
  expect(nativeProjectHistoryReceiptStatus(receipt)).toBe('历史回执待核对')
  expect(projectHistoryArchiveReadEvent(receipt)).toBeUndefined()
  const view = render(<EventDetails event={receipt} />)
  expect(view.container.textContent).toContain('历史回执待核对')
  expect(view.container.textContent).not.toMatch(/同项目历史工程包|原始事件 JSON|SECRET_RAW|工程文件清单已读取/)
})

test('成员path、offset、length及内部范围必须一一匹配', () => {
  for (const args of [{ path: 'other.txt', length: 2 }, { path: file.path, offset: 1, length: 2 },
    { path: file.path, length: 3 }, { path: '../main.txt', length: 2 }, { path: file.path, length: 0 }]) {
    expect(nativeProjectHistoryReceiptStatus(finish(member('A😀', 0, 4), start(args)))).toBe('历史回执待核对')
  }
  for (const change of [{ content_complete: true }, { returned_chars: 3 }, { next_offset: 3 }, { total_chars: 1 }]) {
    const value = member('A😀', 0, 4)
    Object.assign(obj(obj(value.archive_read).read_range), change)
    expect(nativeProjectHistoryReceiptStatus(finish(value, start({ path: file.path, length: 2 })))).toBe('历史回执待核对')
  }
  const value = binary()
  obj(value.archive_read).content = 'SECRET_RAW'
  expect(nativeProjectHistoryReceiptStatus(finish(value, start({ path: file.path })))).toBe('历史回执待核对')
})

test('文本与其他section不接受path或工程包伪装', () => {
  const value = { status: 'ok', complete: true, historical_only: true, section: 'artifact', as_of_seq: 100, as_of_hash: hash,
    source: { ...source, format: undefined }, offset: 0, content: 'old text', total_chars: 8, has_more: false, next_offset: null }
  expect(nativeProjectHistoryReceiptStatus(finish(value))).toBe('历史文件已读取')
  expect(nativeProjectHistoryReceiptStatus(finish(value, start({ path: file.path })))).toBe('历史回执待核对')
  const archive = manifest()
  archive.section = 'delivery'
  expect(nativeProjectHistoryReceiptStatus(finish(archive, start({ section: 'delivery' })))).toBe('历史回执待核对')
})

test('缺失回执参数仅从started_seq指向的宿主事件配对，范围从不由result猜测', () => {
  const started = start({ path: file.path, offset: 2, length: 2 }), receipt = finish(member('BC', 2, 4), started)
  delete receipt.payload.arguments
  delete receipt.payload.project_scope
  expect(nativeProjectHistoryReceiptStatus(receipt)).toBe('历史回执待核对')
  const paired = projectHistoryReadArguments(receipt, [start(), started])
  // Duplicate sequence is ambiguous even when another field looks suitable.
  expect(paired.payload.arguments).toBeNull()
  const recovered = projectHistoryReadArguments(receipt, [started])
  expect(nativeProjectHistoryReceiptStatus(recovered)).toBe('工程文件本页已读取')
  expect(activityLine(started, receipt).text).toContain('工程文件本页已读取')
  expect(render(<EventDetails event={recovered} />).container.textContent).toContain('工程文件本页已读取')
  expect(render(<TeamToolProgress events={[started, receipt]} names={{}} />).container.textContent).toContain('工程文件本页已读取')
  for (const change of [{ started_seq: 109 }, { call_id: 'wrong' }, { run_id: 'wrong' }, { role: 'other' }, { message_seq: 2 }]) {
    const wrong = { ...receipt, payload: { ...receipt.payload, ...change } }
    const checked = projectHistoryReadArguments(wrong, [started])
    if (change.started_seq === 109) expect(checked).toBe(wrong)
    else expect(checked.payload.arguments).toBeNull()
  }
  expect(projectHistoryReadArguments(receipt, [{ ...started, actor: { id: 'dev', kind: 'agent', role: 'dev' } }]).payload.arguments).toBeNull()
  const conflicting = finish(member('BC', 2, 4), started)
  conflicting.payload.arguments = { ...obj(started.payload.arguments), length: 3 }
  expect(nativeProjectHistoryReceiptStatus(projectHistoryReadArguments(conflicting, [started]))).toBe('历史回执待核对')
  expect(activityLine(started, conflicting).warning).toBe(true)
})

test.each(cases)('真实settle仅有started_seq/run_id/role/name/result仍能恢复读取参数: %s', (value, args, status) => {
  const started = start(args)
  const receipt: TeamEvent = { seq: 111, type: 'team.tool_finished', actor: host,
    payload: { started_seq: started.seq, run_id: 'run', role: 'dev', name: 'read_project_history', result: value } }
  const recovered = projectHistoryReadArguments(receipt, [started])
  expect(recovered.payload.arguments).toEqual(started.payload.arguments)
  expect(recovered.payload.project_scope).toBe('project-a')
  expect(nativeProjectHistoryReceiptStatus(recovered)).toBe(status)
  expect(activityLine(started, receipt).text).toContain(status)
  expect(activityLine(started, receipt).warning).toBe(false)
  expect(render(<EventDetails event={recovered} />).container.textContent).toContain(status)
  for (const change of [{ call_id: 'wrong' }, { message_seq: 2 }]) {
    expect(projectHistoryReadArguments({ ...receipt, payload: { ...receipt.payload, ...change } }, [started]).payload.arguments).toBeNull()
  }
  const explicit = { ...receipt, payload: { ...receipt.payload, call_id: 'history', message_seq: 1 } }
  expect(nativeProjectHistoryReceiptStatus(projectHistoryReadArguments(explicit, [started]))).toBe(status)
})

test('显式上下文冲突使自含成功回执失效，缺少上下文保留旧回执', () => {
  const receipt = finish(manifest()), started = start()
  expect(projectHistoryReadArguments(receipt, [])).toBe(receipt)
  expect(nativeProjectHistoryReceiptStatus(projectHistoryReadArguments(receipt, []))).toBe('工程文件清单已读取')
  for (const change of [{ role: 'other' }, { run_id: 'other' }, { name: 'read_artifact' }, { role: undefined }, { run_id: undefined }]) {
    const conflicting = { ...started, payload: { ...started.payload, ...change } }
    const checked = projectHistoryReadArguments(receipt, [conflicting])
    expect(checked.payload.arguments).toBeNull()
    expect(nativeProjectHistoryReceiptStatus(checked)).toBe('历史回执待核对')
    const view = render(<EventDetails event={checked} />)
    expect(view.container.textContent).not.toMatch(/工程文件清单已读取|同项目历史工程包|原始事件 JSON/)
  }
  const wrongName = { ...receipt, payload: { ...receipt.payload, name: 'read_artifact' } }
  expect(projectHistoryReadArguments(wrongName, [started]).payload.arguments).toBeNull()
  expect(projectHistoryReadArguments(receipt, [started, started]).payload.arguments).toBeNull()
})
