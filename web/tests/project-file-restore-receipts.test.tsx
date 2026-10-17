import { render } from '@testing-library/react'
import { expect, test } from 'vitest'
import { NativeProjectRestoreReceipt } from '../src/NativeProjectRestoreReceipt'
import { EventDetails } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity, nativeProjectRestoreReceiptStatus, projectHistoryReadArguments, projectRestoreReceiptEvent } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

type Data = Record<string, unknown>
const obj = (value: unknown) => value as Data
const hash = 'a'.repeat(64), zipHash = 'b'.repeat(64), fileHash = 'c'.repeat(64)
const name = 'workspace_restore_project_file'
const host = { id: 'team', kind: 'system', role: 'team' }
// Self-contained protocol examples: no private evidence or cross-test fixtures.
const start = (args: Data = {}): TeamEvent => ({ seq: 110, type: 'team.tool_started', actor: host,
  payload: { name, project_scope: 'project-a', role: 'dev', run_id: 'run', call_id: 'restore', message_seq: 1,
    arguments: { as_of_seq: 100, delivery_seq: 50, artifact_seq: 40, path: 'src/main.bin', ...args } } })
const result = (created = true): Data => ({ status: 'ok', complete: true, outcome: created ? 'written' : 'already_present',
  as_of_seq: 100, as_of_hash: hash,
  source: { project_id: 'project-a', task_id: 'old-task', delivery_seq: 50, delivery_hash: hash,
    artifact_seq: 40, content_sha256: zipHash, filename: 'project.zip', author: 'dev', format: 'archive', event_hash: fileHash },
  member: { path: 'src/main.bin', sha256: fileHash, bytes: 7, executable: true },
  restored_file: { path: 'src/main.bin', sha256: fileHash, bytes: 7, executable: false,
    executable_requested: true, created, already_present: !created } })
const finish = (value = result(), started = start()): TeamEvent => ({ seq: 111, type: 'team.tool_finished', actor: host,
  payload: { ...started.payload, started_seq: started.seq, result: value } })
const bare = (started = start(), value = result()): TeamEvent => ({ seq: 111, type: 'team.tool_finished', actor: host,
  payload: { started_seq: started.seq, role: 'dev', run_id: 'run', name, result: value } })
const activity = (started: TeamEvent, receipt: TeamEvent, extra: TeamEvent[] = []) => {
  const team: TeamState = { enabled: true, objective: '恢复文件', status: 'running', revision: 0,
    runs: [], pending_count: 0, assignments: [], checkpoint: '' }
  return conversationActivity({ team, names: {}, leaderId: 'manager', sending: false, executionState: 'paused', events: [
    { seq: 1, type: 'team.message_received', actor: { id: 'human', kind: 'human', role: 'human' }, payload: { recipient: 'dev' } },
    { seq: 2, type: 'team.run_started', actor: host, payload: { role: 'dev', run_id: 'run', message_seq: 1 } }, started, receipt, ...extra,
  ] }).lines.find(line => line.role === 'dev')!
}

test.each([true, false])('创建和已有相同回执、真实host finish补全及折叠来源：%s', created => {
  const started = start(), receipt = bare(started, result(created))
  const checked = projectHistoryReadArguments(receipt, [started])
  const status = created ? '历史文件已创建' : '已有相同文件，未重写'
  expect(nativeProjectRestoreReceiptStatus(receipt)).toBe('恢复回执待核对')
  expect(checked.payload.arguments).toEqual(started.payload.arguments)
  expect(checked.payload.project_scope).toBe('project-a')
  expect(nativeProjectRestoreReceiptStatus(checked)).toBe(status)
  expect(projectRestoreReceiptEvent(checked)).toBe(checked)
  expect(activity(started, receipt)).toMatchObject({ warning: false, text: expect.stringContaining(status) })
  const view = render(<NativeProjectRestoreReceipt event={checked} />)
  expect(view.container.querySelector('details')?.open).toBe(false)
  for (const text of [status, '源相对路径：src/main.bin', '目标相对路径：src/main.bin', '7 字节', fileHash,
    '来源有 · 请求设置 · 目标实际无', '未运行，未验收', 'old-task', '事件 #100', hash, zipHash]) {
    expect(view.container.textContent).toContain(text)
  }
  expect(view.container.querySelector('pre,button,a')).toBeNull()
})

test.each([0, 32 * 1024 * 1024])('接受空文件与32MiB边界及明确destination：%s', bytes => {
  const value = result(), started = start({ destination: 'restored/main.bin' })
  obj(value.member).bytes = bytes
  Object.assign(obj(value.restored_file), { bytes, path: 'restored/main.bin' })
  expect(nativeProjectRestoreReceiptStatus(finish(value, started))).toBe('历史文件已创建')
})

const invalid: [string, (event: TeamEvent) => void][] = [
  ['非宿主', e => { e.actor = { id: 'dev', kind: 'agent', role: 'dev' } }],
  ['非finish', e => { e.type = 'msg.said' }],
  ['工具别名冲突', e => { e.payload.tool = 'workspace_write' }],
  ['缺scope', e => { delete e.payload.project_scope }],
  ['scope错配', e => { e.payload.project_scope = 'other' }],
  ['缺参数', e => { delete e.payload.arguments }],
  ['未知参数', e => { obj(e.payload.arguments).task_id = 'old-task' }],
  ['缺cutoff参数', e => { delete obj(e.payload.arguments).as_of_seq }],
  ['cutoff错配', e => { obj(e.payload.arguments).as_of_seq = 99 }],
  ['未来cutoff', e => { obj(e.payload.result).as_of_seq = 111; obj(e.payload.arguments).as_of_seq = 111 }],
  ['交付越界', e => { obj(e.payload.result).as_of_seq = 49; obj(e.payload.arguments).as_of_seq = 49 }],
  ['cutoff同事件hash冲突', e => { Object.assign(obj(e.payload.result), { as_of_seq: 50, as_of_hash: zipHash }); obj(e.payload.arguments).as_of_seq = 50 }],
  ['交付错配', e => { obj(e.payload.arguments).delivery_seq = 51 }],
  ['成员版本错配', e => { obj(e.payload.arguments).artifact_seq = 41 }],
  ['成员越界', e => { obj(obj(e.payload.result).source).artifact_seq = 50; obj(e.payload.arguments).artifact_seq = 50 }],
  ...['project_id', 'task_id', 'author', 'delivery_hash', 'content_sha256', 'event_hash'].map(key =>
    [`缺来源${key}`, (e: TeamEvent) => { delete obj(obj(e.payload.result).source)[key] }] as [string, (e: TeamEvent) => void]),
  ['非工程包', e => { obj(obj(e.payload.result).source).format = 'text' }],
  ['坏ZIP名', e => { obj(obj(e.payload.result).source).filename = '../project.zip' }],
  ['坏cutoff摘要', e => { obj(e.payload.result).as_of_hash = 'bad' }],
  ['源路径错配', e => { obj(obj(e.payload.result).member).path = 'other.bin' }],
  ['目标路径错配', e => { obj(obj(e.payload.result).restored_file).path = 'other.bin' }],
  ['destination错配', e => { obj(e.payload.arguments).destination = 'other.bin' }],
  ['空destination不可默认', e => { obj(e.payload.arguments).destination = '' }],
  ['null destination不可默认', e => { obj(e.payload.arguments).destination = null }],
  ['源目标sha不同', e => { obj(obj(e.payload.result).restored_file).sha256 = hash }],
  ['源目标bytes不同', e => { obj(obj(e.payload.result).restored_file).bytes = 8 }],
  ['互斥标记双true', e => { obj(obj(e.payload.result).restored_file).already_present = true }],
  ['互斥标记双false', e => { obj(obj(e.payload.result).restored_file).created = false }],
  ['outcome冲突', e => { obj(e.payload.result).outcome = 'already_present' }],
  ['执行请求错配', e => { obj(obj(e.payload.result).restored_file).executable_requested = false }],
  ['执行状态非boolean', e => { obj(obj(e.payload.result).restored_file).executable = 0 }],
  ['缺源执行状态', e => { delete obj(obj(e.payload.result).member).executable }],
  ['超32MiB', e => { obj(obj(e.payload.result).member).bytes = 32 * 1024 * 1024 + 1; obj(obj(e.payload.result).restored_file).bytes = 32 * 1024 * 1024 + 1 }],
  ['宿主失败', e => { e.payload.status = 'failed' }],
  ['宿主unknown', e => { e.payload.status = 'unknown' }],
  ['结果失败', e => { obj(e.payload.result).status = 'error' }],
  ['未完成', e => { obj(e.payload.result).complete = false }],
  ['未知影响', e => { obj(e.payload.result).outcome = 'effects_unknown' }],
  ['未知outcome', e => { obj(e.payload.result).outcome = 'unknown' }],
  ['正文混入', e => { obj(e.payload.result).content = 'SECRET_RAW' }],
  ['原始字节混入', e => { obj(e.payload.result).bytes = [0, 255] }],
  ['base64混入', e => { obj(e.payload.result).base64 = 'SECRET_RAW' }],
  ['成员混入body', e => { obj(obj(e.payload.result).member).body = 'SECRET_RAW' }],
]

test.each(invalid)('失败关闭、不显示成功详情：%s', (_label, mutate) => {
  const receipt = finish()
  mutate(receipt)
  expect(projectRestoreReceiptEvent(receipt)).toBeUndefined()
  expect(nativeProjectRestoreReceiptStatus(receipt)).not.toMatch(/已创建|已有相同文件/)
  const shown = render(<NativeProjectRestoreReceipt event={receipt} />).container.textContent
  if (_label === '未知影响') expect(shown).toContain('文件可能已经写入')
  else expect(shown).toBe('')
  expect(shown).not.toMatch(/源与目标一致|源与目标 SHA-256/)
})

test.each(['../x', '/absolute', 'C:/x', 'a\\b', 'a//b', 'a/CON', 'a/..', 'a/ＮＵＬ', 'a/trailing.', 'a/\u0000', 'x'.repeat(2049)])('源和目标均拒绝非便携路径：%s', path => {
  for (const destination of [false, true]) {
    const value = result(), started = start(destination ? { destination: path } : { path })
    if (destination) obj(value.restored_file).path = path
    else { obj(value.member).path = path; obj(value.restored_file).path = path }
    expect(projectRestoreReceiptEvent(finish(value, started))).toBeUndefined()
  }
})

test('唯一start补全，所有显式冲突失败关闭；完整自含回执无上下文可显示', () => {
  const started = start(), receipt = bare(started)
  expect(projectHistoryReadArguments(finish(), [])).toEqual(finish())
  expect(projectHistoryReadArguments(receipt, []).payload.arguments).toBeUndefined()
  for (const change of [{ role: 'other' }, { run_id: 'other' }, { name: 'workspace_write' },
    { tool: 'workspace_write' }, { call_id: 'other' }, { message_seq: 2 },
    { arguments: { ...obj(started.payload.arguments), destination: 'other.bin' } }, { project_scope: 'other' }]) {
    const checked = projectHistoryReadArguments({ ...receipt, payload: { ...receipt.payload, ...change } }, [started])
    expect(checked.payload.arguments).toBeNull()
    expect(projectRestoreReceiptEvent(checked)).toBeUndefined()
  }
  for (const starts of [[started, started], [{ ...started, type: 'msg.said' }],
    [{ ...started, actor: { id: 'dev', kind: 'agent', role: 'dev' } }],
    [{ ...started, payload: { ...started.payload, role: undefined } }],
    [{ ...started, payload: { ...started.payload, name: 'workspace_write' } }]]) {
    const checked = projectHistoryReadArguments(finish(), starts)
    expect(checked.payload.arguments).toBeNull()
    expect(projectRestoreReceiptEvent(checked)).toBeUndefined()
  }
  expect(activity(started, receipt, [started]).warning).toBe(true)
  const forgedDuplicate = { ...started, actor: { id: 'dev', kind: 'agent', role: 'dev' } }
  expect(activity(started, receipt, [forgedDuplicate]).warning).toBe(true)
})

test('中断无回执、失败和unknown不声称成功', () => {
  expect(nativeProjectRestoreReceiptStatus(start())).toBe('已发起，等待恢复回执')
  expect(render(<NativeProjectRestoreReceipt event={start()} />).container.textContent).toBe('')
  for (const [outcome, status] of [['not_started', '未开始恢复历史文件'], ['effects_unknown', '恢复影响待核对']]) {
    const receipt = bare(start(), { status: 'error', complete: outcome === 'not_started', outcome, message: 'SECRET_RAW' })
    expect(nativeProjectRestoreReceiptStatus(receipt)).toBe(status)
    expect(activity(start(), receipt).warning).toBe(true)
    const text = render(<NativeProjectRestoreReceipt event={receipt} />).container.textContent
    expect(text).toContain(outcome === 'not_started' ? '未写入这份历史文件' : '不要直接重试')
    expect(text).not.toContain('SECRET_RAW')
  }
  const inconsistent = bare(start(), { status: 'error', complete: false, outcome: 'not_started' })
  expect(nativeProjectRestoreReceiptStatus(inconsistent)).toBe('恢复回执待核对')
  expect(render(<NativeProjectRestoreReceipt event={inconsistent} />).container.textContent).toBe('')
})

test.each([true, false])('真实开始/结束形状通过过程与审计展示，不泄露内部工具名：%s', created => {
  const started = start(), receipt = bare(started, result(created))
  const status = created ? '历史文件已创建' : '已有相同文件，未重写'
  for (const view of [render(<EventDetails event={receipt} contextEvents={[started, receipt]} />),
    render(<TeamToolProgress events={[started, receipt]} names={{ dev: '米寇' }} full />)]) {
    expect(view.container.textContent).toContain(status)
    expect(view.container.textContent).toContain('恢复历史工程文件')
    expect(view.container.textContent).toContain('未运行，未验收')
    expect(view.container.textContent).not.toMatch(/workspace_restore_project_file|原始事件 JSON|工具已完成/)
  }
})

test.each(['not_started', 'effects_unknown'])('失败恢复的审计与过程不展示原始异常或JSON：%s', outcome => {
  const started = start(), receipt = bare(started, { status: 'error', complete: outcome === 'not_started',
    outcome, message: 'PRIVATE_EXCEPTION /host/private/path', code: 'fixture_error' })
  const status = outcome === 'not_started' ? '未开始恢复历史文件' : '恢复影响待核对'
  for (const view of [render(<EventDetails event={receipt} contextEvents={[started, receipt]} />),
    render(<TeamToolProgress events={[started, receipt]} names={{ dev: '米寇' }} full />)]) {
    expect(view.container.textContent).toContain(status)
    expect(view.container.textContent).not.toMatch(/PRIVATE_EXCEPTION|host\/private|原始事件 JSON|workspace_restore_project_file/)
    expect(view.container.querySelector('pre')).toBeNull()
  }
})
