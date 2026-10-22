import { fireEvent, render, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'
import { NativeArtifactVersionsReceipt } from '../src/NativeArtifactVersionsReceipt'
import { TeamToolProgress } from '../src/TeamProgressPanel'

const host = { id: 'team', kind: 'system', role: 'team' }
const sha = 'a'.repeat(64)
const result = (extra: Record<string, unknown> = {}) => ({
  status: 'ok', complete: true, outcome: 'passed', exit_code: 0,
  artifact_selection: 'version_directories', environment: 'temporary_artifact_copy_not_sandbox',
  scope_current: true, artifacts_unchanged: true, artifact_check_valid: true, selected_versions_current: false,
  checked_artifacts: [
    { seq: 5, filename: '工程.zip', content_sha256: sha, directory: '旧版本', latest_seq_at_check: 15, is_latest_at_check: false },
    { seq: 15, filename: '工程.zip', content_sha256: 'b'.repeat(64), directory: '新版本', latest_seq_at_check: 15, is_latest_at_check: true },
  ],
  checked_files: [
    { artifact_seq: 5, source_path: 'src/入口.py', path: '旧版本/src/入口.py', bytes: 32, sha256: 'c'.repeat(64), executable: false },
    { artifact_seq: 15, source_path: 'src/入口.py', path: '新版本/src/入口.py', bytes: 48, sha256: 'd'.repeat(64), executable: true },
  ],
  stdout: { excerpt: '唯一标准输出', complete: true, truncated: false },
  stderr: { excerpt: '唯一标准错误', complete: true, truncated: false },
  ...extra,
})
const finished = (value: unknown = result(), payload: Record<string, unknown> = {}): DetailEvent => ({
  seq: 40, type: 'team.tool_finished', actor: host, payload: { name: 'workspace_run', status: 'ok', role: 'test',
    arguments: { command: 'test', artifact_versions: [{ artifact_seq: 5, directory: '旧版本' }, { artifact_seq: 15, directory: '新版本' }] },
    started_seq: 30, result: value, ...payload },
})
const mount = (value: unknown = result()) => render(<NativeArtifactVersionsReceipt event={finished(value)} />)
const field = (container: HTMLElement, name: string) => within(container).getByText(name, { selector: 'dt' }).nextElementSibling?.textContent
const changedArtifact = (extra: Record<string, unknown>) => result({ checked_artifacts: [
  { ...result().checked_artifacts[0], ...extra }, result().checked_artifacts[1],
] })
const changedFile = (extra: Record<string, unknown>) => result({ checked_files: [
  { ...result().checked_files[0], ...extra }, result().checked_files[1],
] })

test('同名历史与新版本分别显示挂载目录，权限和确切字节有效不表示最新版', () => {
  const view = mount()
  const versions = within(view.container).getByLabelText('版本与挂载目录')
  expect(versions.textContent).toContain('成果版本 #5 · 工程.zip')
  expect(versions.textContent).toContain('成果版本 #15 · 工程.zip')
  expect(versions.textContent).toContain('挂载目录：旧版本/')
  expect(versions.textContent).toContain('挂载目录：新版本/')
  expect(versions.textContent).toContain('检查时为历史版本 · 当时最新版 #15')
  expect(versions.textContent).toContain('检查时为最新版 #15')
  expect(field(view.container, '任务权限与确切字节')).toBe('回执记录时有效')
  expect(field(view.container, '执行副本')).toBe('未改变')
  expect(field(view.container, '确切版本检查')).toBe('记录有效')
  expect(field(view.container, '所选版本')).toBe('包含检查时的历史版本')
  expect(field(view.container, '命令执行')).toBe('已结束 · 退出码 0')
  expect(view.container.textContent).toContain('非操作系统沙箱')
  expect(view.container.textContent).toContain('不等于业务通过、覆盖完整或独立批准')
  expect(view.container.textContent).not.toMatch(/检查通过|测试通过|业务已通过|独立批准已完成|唯一标准/)
})

test('映射和指纹默认折叠，原路径与实际挂载路径明确区分', () => {
  const view = mount()
  const mapping = within(view.container).getByText('原路径 → 执行路径 · 2 项').closest('details')!
  const hashes = within(view.container).getByText('版本与副本指纹').closest('details')!
  expect(mapping.open).toBe(false)
  expect(hashes.open).toBe(false)
  fireEvent.click(within(mapping).getByText('原路径 → 执行路径 · 2 项'))
  expect(mapping.open).toBe(true)
  const rows = within(mapping).getAllByRole('listitem')
  expect(rows[0].textContent).toContain('来源成果版本 #5原路径：src/入口.py执行路径：旧版本/src/入口.py')
  expect(rows[1].textContent).toContain('来源成果版本 #15原路径：src/入口.py执行路径：新版本/src/入口.py')
  expect(rows[0].textContent).toContain('32 字节 · 无可执行标记')
  expect(rows[1].textContent).toContain('48 字节 · 有可执行标记')
  expect(mapping.textContent).toContain('不代表本平台已核对权限位或已执行每个文件')
  fireEvent.click(within(hashes).getByText('版本与副本指纹'))
  expect(hashes.open).toBe(true)
  expect(hashes.textContent).toContain(sha)
  expect(hashes.textContent).toContain('c'.repeat(64))
  expect(view.container.querySelector('a, button, script, img, iframe')).toBeNull()
})

test.each(['ok', 'error'])('执行失败仍可保留确切版本有效记录，status=%s', status => {
  const view = mount(result({ status, outcome: 'failed', exit_code: 2 }))
  expect(field(view.container, '命令执行')).toBe('执行失败 · 退出码 2')
  expect(field(view.container, '确切版本检查')).toBe('记录有效')
  expect(field(view.container, '所选版本')).toBe('包含检查时的历史版本')
  expect(view.container.textContent).not.toMatch(/检查通过|工具通过|测试通过/)
})

test.each([
  ['timeout', null, '执行超时'], ['launch_error', null, '未能启动'], ['not_started', null, '未开始执行'],
  ['failed', 0, '执行失败 · 退出码 0'], ['passed', 1, '执行结果待核对'], ['future', 0, '执行结果待核对'],
  ['passed', '0', '执行结果待核对'], ['failed', null, '执行结果待核对'], ['passed', 0.5, '执行结果待核对'],
])('命令结果按明确执行状态显示：%s/%s', (outcome, exit_code, expected) => {
  const view = mount(result({ outcome, exit_code }))
  expect(field(view.container, '命令执行')).toBe(expected)
  expect(field(view.container, '命令执行')).not.toContain('通过')
})

test.each([{}, { complete: false }, { complete: 'true' }, { status: 'error' }])('status=ok 本身不产生成功结论 %j', extra => {
  const value: Record<string, unknown> = result(extra)
  if (!Object.keys(extra).length) delete value.outcome
  const view = mount(value)
  expect(field(view.container, '命令执行')).toBe('执行结果待核对')
})

test.each([
  [false, true, false, '已失效', '未改变', '记录无效'],
  [true, false, false, '回执记录时有效', '已改变', '记录无效'],
  [false, false, false, '已失效', '已改变', '记录无效'],
  [true, true, false, '回执记录时有效', '未改变', '有效性待核对'],
  [false, true, true, '已失效', '未改变', '有效性待核对'],
])('权限、字节和副本状态分层，不接受矛盾有效性：%s/%s/%s', (scope_current, artifacts_unchanged, artifact_check_valid, scope, copy, valid) => {
  const view = mount(result({ scope_current, artifacts_unchanged, artifact_check_valid }))
  expect(field(view.container, '任务权限与确切字节')).toBe(scope)
  expect(field(view.container, '执行副本')).toBe(copy)
  expect(field(view.container, '确切版本检查')).toBe(valid)
  expect(field(view.container, '所选版本')).toBe('包含检查时的历史版本')
})

test.each(['scope_current', 'artifacts_unchanged', 'artifact_check_valid'])('缺失或非法 %s 不确认确切版本有效', name => {
  for (const bad of [undefined, null, 'true', 1, {}, 'unknown']) {
    const view = mount(result({ [name]: bad }))
    expect(field(view.container, '确切版本检查')).toBe('有效性待核对')
    view.unmount()
  }
})

test.each([undefined, null, 'unknown', 'true', 1, true])('未知或矛盾的最新汇总不把历史版本标为最新版：%j', selected_versions_current => {
  const view = mount(result({ selected_versions_current }))
  expect(field(view.container, '所选版本')).toBe('是否最新待核对')
  expect(field(view.container, '确切版本检查')).toBe('记录有效')
})

test('所有所选版本均为检查时最新才肯定最新汇总', () => {
  const value = result()
  const view = mount(result({ checked_artifacts: [value.checked_artifacts[1]], checked_files: [value.checked_files[1]], selected_versions_current: true }))
  expect(field(view.container, '所选版本')).toBe('均为检查时最新版')
})

test('不同大小写文件名独立挂载时按确切文件名观察最新版本，不合并版本历史', () => {
  const value = result()
  const view = mount(result({ selected_versions_current: true, checked_artifacts: value.checked_artifacts.map((artifact, index) => ({
    ...artifact, filename: index ? 'Project.zip' : 'project.zip', latest_seq_at_check: artifact.seq, is_latest_at_check: true,
  })) }))
  expect(field(view.container, '所选版本')).toBe('均为检查时最新版')
  expect(field(view.container, '确切版本检查')).toBe('记录有效')
})

test.each([false, true])('最新观察故障以null呈现，保留命令结果与确切字节有效性；含已知历史=%s', historical => {
  const value = result()
  const unknown = { latest_seq_at_check: null, is_latest_at_check: null }
  const view = mount(result({ outcome: 'failed', exit_code: 2, selected_versions_current: historical ? false : null,
    checked_artifacts: value.checked_artifacts.map((artifact, index) => historical && index === 0 ? artifact : { ...artifact, ...unknown }),
  }))
  expect(field(view.container, '命令执行')).toBe('执行失败 · 退出码 2')
  expect(field(view.container, '任务权限与确切字节')).toBe('回执记录时有效')
  expect(field(view.container, '确切版本检查')).toBe('记录有效')
  expect(field(view.container, '执行副本')).toBe('未改变')
  expect(field(view.container, '所选版本')).toBe(historical ? '包含检查时的历史版本' : '是否最新待核对')
  const rows = within(view.container).getAllByRole('listitem').slice(0, 2)
  expect(rows[0].textContent).toContain(historical ? '检查时为历史版本 · 当时最新版 #15' : '该版本是否最新待核对')
  expect(rows[1].textContent).toContain('该版本是否最新待核对')
  expect(view.container.textContent).not.toContain('检查时为最新版')
  expect(within(view.container).getByLabelText('原路径与实际执行路径')).toBeTruthy()
})

test('最新汇总为null时仍独立呈现已知历史false，不从未知汇总推导全部最新', () => {
  const value = result()
  const view = mount(result({ selected_versions_current: null, checked_artifacts: [value.checked_artifacts[0],
    { ...value.checked_artifacts[1], latest_seq_at_check: null, is_latest_at_check: null }],
  }))
  expect(field(view.container, '所选版本')).toBe('是否最新待核对')
  expect(within(view.container).getByLabelText('版本与挂载目录').textContent).toContain('检查时为历史版本 · 当时最新版 #15')
})

test.each([
  { latest_seq_at_check: undefined }, { latest_seq_at_check: 4 }, { latest_seq_at_check: 40 },
  { latest_seq_at_check: '15' }, { latest_seq_at_check: 16 }, { is_latest_at_check: true }, { is_latest_at_check: 'false' },
])('逐项最新字段缺失、非法或同名版本互相矛盾时降级新旧状态：%j', extra => {
  const view = mount(changedArtifact(extra))
  expect(field(view.container, '所选版本')).toBe('是否最新待核对')
  const rows = within(within(view.container).getByLabelText('版本与挂载目录')).getAllByRole('listitem')
  expect(rows[0].textContent).toContain('该版本是否最新待核对')
  expect(field(view.container, '确切版本检查')).toBe('记录有效')
})

test.each([
  ['seq', 0], ['seq', 15], ['seq', 40], ['seq', 1.5], ['seq', '5'],
  ['filename', ''], ['filename', '../工程.zip'], ['filename', `${'长'.repeat(60)}.zip`], ['content_sha256', 'x'.repeat(64)],
  ['directory', '../旧版本'], ['directory', 'old/nested'], ['directory', 'C:'], ['directory', 'NUL'],
  ['directory', '新版本'], ['directory', 'x'.repeat(65)], ['directory', '.git'], ['unexpected', true],
])('非法版本字段 %s=%j 不确认范围或副本有效', (name, bad) => {
  const view = mount(changedArtifact({ [name]: bad }))
  expect(field(view.container, '任务权限与确切字节')).toBe('待核对')
  expect(field(view.container, '执行副本')).toBe('是否改变待核对')
  expect(field(view.container, '确切版本检查')).toBe('有效性待核对')
  expect(within(view.container).queryByLabelText('原路径与实际执行路径')).toBeNull()
})

test.each([
  { artifact_seq: 999 }, { artifact_seq: '5' }, { source_path: undefined }, { source_path: '../入口.py', path: '旧版本/../入口.py' },
  { source_path: '/入口.py', path: '旧版本//入口.py' }, { path: 'src/入口.py' }, { path: '新版本/src/入口.py' },
  { bytes: -1 }, { bytes: 1.5 }, { bytes: 16 * 1024 * 1024 + 1 }, { sha256: undefined }, { sha256: 'bad' },
  { executable: 'false' }, { executable: undefined }, { unexpected: true },
  { source_path: '.env', path: '旧版本/.env' }, { source_path: 'secrets.json', path: '旧版本/secrets.json' },
  { source_path: `${'长'.repeat(81)}.py`, path: `旧版本/${'长'.repeat(81)}.py` },
])('非法、缺失或跨版本路径的成员字段不确认完整范围：%j', extra => {
  const view = mount(changedFile(extra))
  expect(field(view.container, '确切版本检查')).toBe('有效性待核对')
  expect(view.container.textContent).toContain('版本或副本清单不完整或不一致')
  expect(within(view.container).queryByLabelText('原路径与实际执行路径')).toBeNull()
})

test.each(['checked_artifacts', 'checked_files'])('空、非数组或条目缺失的 %s 不变成有效范围', name => {
  for (const bad of [undefined, null, [], [null], {}, 'unknown']) {
    const view = mount(result({ [name]: bad }))
    expect(field(view.container, '确切版本检查')).toBe('有效性待核对')
    expect(field(view.container, '所选版本')).toBe('是否最新待核对')
    view.unmount()
  }
})

test('重复、父子路径、NFKC目录别名和超出数量或字节预算均不能确认范围', () => {
  const value = result(), file = value.checked_files[0]
  const invalid: unknown[] = [
    result({ checked_files: [file, file, value.checked_files[1]] }),
    result({ checked_files: [file, { ...file, source_path: 'src/入口.py/child', path: '旧版本/src/入口.py/child' }, value.checked_files[1]] }),
    result({ checked_files: [file, { ...file, source_path: 'straße/a', path: '旧版本/straße/a' },
      { ...file, source_path: 'STRASSE/b', path: '旧版本/STRASSE/b' }, value.checked_files[1]] }),
    result({ checked_files: Array.from({ length: 129 }, () => file) }),
    result({ checked_artifacts: Array.from({ length: 129 }, () => value.checked_artifacts[0]) }),
    result({ checked_artifacts: value.checked_artifacts.map((artifact, index) => ({ ...artifact, directory: index ? 'ＯＬＤ' : 'old' })) }),
    result({ checked_artifacts: value.checked_artifacts.map((artifact, index) => ({ ...artifact, directory: index ? 'STRASSE' : 'straße' })) }),
    result({ checked_files: [
      { ...file, bytes: 16 * 1024 * 1024 }, { ...value.checked_files[1], bytes: 16 * 1024 * 1024 },
      { ...file, source_path: 'extra', path: '旧版本/extra', bytes: 1 },
    ] }),
  ]
  for (const item of invalid) {
    const view = mount(item)
    expect(field(view.container, '确切版本检查')).toBe('有效性待核对')
    expect(within(view.container).queryByLabelText('原路径与实际执行路径')).toBeNull()
    view.unmount()
  }
})

test('原始文本的成员路径与哈希必须匹配版本，不能借用ZIP解释', () => {
  const artifact = { seq: 5, filename: '入口.py', directory: 'selected', content_sha256: sha, latest_seq_at_check: 5, is_latest_at_check: true }
  const file = { artifact_seq: 5, source_path: '入口.py', path: 'selected/入口.py', bytes: 0, sha256: sha, executable: false }
  for (const bad of [false, true]) {
    const view = mount(result({ checked_artifacts: [artifact], checked_files: [{ ...file, sha256: bad ? 'b'.repeat(64) : sha }], selected_versions_current: true }))
    expect(field(view.container, '确切版本检查')).toBe(bad ? '有效性待核对' : '记录有效')
    view.unmount()
  }
})

test.each([
  { artifact_selection: 'future' }, { artifact_selection: undefined }, { environment: 'sandbox' },
  { environment: undefined }, { status: 'future' },
])('新模式判别或环境未知时不确认有效、未改变或最新：%j', extra => {
  const view = mount(result(extra))
  expect(field(view.container, '确切版本检查')).toBe('有效性待核对')
  expect(field(view.container, '执行副本')).toBe('是否改变待核对')
  expect(field(view.container, '所选版本')).toBe('是否最新待核对')
  expect(view.container.textContent).toContain('版本选择或执行环境记录待核对')
})

test('预执行拒绝的新选择保留待核对说明，不捏造副本或范围', () => {
  const view = mount({ status: 'error', complete: true, outcome: 'not_started' })
  expect(field(view.container, '命令执行')).toBe('执行结果待核对')
  expect(field(view.container, '确切版本检查')).toBe('有效性待核对')
  expect(within(view.container).queryByLabelText('版本与挂载目录')).toBeNull()
})

test('只有宿主完成的 workspace_run 显示，兼容 tool 名称字段', () => {
  const original = finished()
  const excluded = [
    { ...original, type: 'team.tool_started' }, { ...original, type: 'workspace.tool_finished' },
    finished(result(), { name: 'browser_check' }),
    ...['id', 'kind', 'role'].map(key => ({ ...original, actor: { ...host, [key]: 'other' } })),
    { ...original, actor: Object.create(host) },
  ]
  for (const event of excluded) {
    const view = render(<NativeArtifactVersionsReceipt event={event} />)
    expect(view.container.innerHTML).toBe('')
    view.unmount()
  }
  const view = render(<NativeArtifactVersionsReceipt event={finished(result(), { name: undefined, tool: 'workspace_run' })} />)
  expect(within(view.container).getByLabelText('版本副本检查回执')).toBeTruthy()
})

test('旧 artifact_seqs 保留原输出和开发者详情，不增加版本副本内容', () => {
  const value: Record<string, unknown> = result()
  delete value.artifact_selection
  value.checked_artifacts = [{ seq: 5, filename: '工程.zip', content_sha256: sha }]
  value.checked_files = [{ artifact_seq: 5, path: 'src/入口.py', bytes: 32, sha256: sha, executable: false }]
  for (const selection of [undefined, 'explicit', 'current_objective']) {
    const event = finished({ ...value, artifact_selection: selection }, { arguments: { artifact_seqs: [5] } })
    const direct = render(<NativeArtifactVersionsReceipt event={event} />)
    expect(direct.container.innerHTML).toBe('')
    direct.unmount()
    const view = render(<EventDetails event={event} />)
    expect(within(view.container).queryByLabelText('版本副本检查回执')).toBeNull()
    expect(within(view.container).getAllByRole('group', { name: '标准输出' })).toHaveLength(1)
    expect(within(view.container).getByText('开发者详情').closest('details')?.open).toBe(false)
    expect(JSON.parse(within(view.container).getByLabelText('原始事件 JSON').textContent!)).toEqual(event)
    view.unmount()
  }
})

test('既有事件与工具过程都能查看版本详情，默认折叠且不重复输出', () => {
  const event = finished()
  const view = render(<EventDetails event={event} />)
  const native = within(view.container).getByLabelText('版本副本检查回执')
  expect(within(view.container).getAllByRole('group', { name: '标准输出' })).toHaveLength(1)
  expect(within(view.container).getAllByRole('group', { name: '错误输出' })).toHaveLength(1)
  expect(native.textContent).not.toMatch(/唯一标准输出|唯一标准错误/)
  expect(within(view.container).getByText('开发者详情').closest('details')?.open).toBe(false)
  expect(JSON.parse(within(view.container).getByLabelText('原始事件 JSON').textContent!)).toEqual(event)
  const progress = render(<TeamToolProgress events={[event]} names={{ test: '验证成员' }} />)
  const tools = within(progress.container).getByText('工具过程 · 1 项').closest('details')!
  const disclosure = within(progress.container).getByText('版本副本检查详情').closest('details')!
  expect(tools.open).toBe(false)
  expect(disclosure.open).toBe(false)
  fireEvent.click(within(tools).getByText('工具过程 · 1 项'))
  fireEvent.click(within(disclosure).getByText('版本副本检查详情'))
  expect(disclosure.open).toBe(true)
  expect(within(progress.container).getAllByLabelText('版本副本检查回执')).toHaveLength(1)
  expect(within(progress.container).getAllByRole('group', { name: '标准输出' })).toHaveLength(1)
  expect(within(progress.container).getAllByRole('group', { name: '错误输出' })).toHaveLength(1)
  expect(within(disclosure).getByLabelText('版本与挂载目录').textContent).toContain('旧版本/')
})

test('工具开始与真实完成配对后只出现一份版本详情，旧命令不出现空入口', () => {
  const binding = { role: 'test', run_id: 'version-run', message_seq: 22, call_id: 'version-check' }
  const receipt = finished(result(), binding)
  const start: DetailEvent = { seq: 30, type: 'team.tool_started', actor: host,
    payload: { ...binding, name: 'workspace_run', arguments: receipt.payload.arguments } }
  const view = render(<TeamToolProgress events={[start, receipt]} names={{ test: '验证成员' }} />)
  expect(within(view.container).getAllByText('版本副本检查详情')).toHaveLength(1)
  expect(within(view.container).getAllByLabelText('版本副本检查回执')).toHaveLength(1)
  expect(within(view.container).getByText('工具过程 · 1 项')).toBeDefined()
  view.unmount()
  const legacy = finished({ status: 'ok', outcome: 'passed', complete: true, exit_code: 0,
    stdout: { excerpt: '旧命令输出', complete: true, truncated: false } },
    { arguments: { command: 'python', artifact_seqs: [5] } })
  const old = render(<TeamToolProgress events={[legacy]} names={{ test: '验证成员' }} />)
  expect(within(old.container).queryByText('版本副本检查详情')).toBeNull()
  expect(within(old.container).queryByLabelText('版本副本检查回执')).toBeNull()
  expect(within(old.container).getAllByText('旧命令输出')).toHaveLength(1)
})

test('只展示已知字段的纯文本，不渲染路径HTML、链接或额外内部字段', () => {
  const value = result({ note: 'PRIVATE_NOTE', stdout: 'PRIVATE_STDOUT', private: '<script>PRIVATE_SCRIPT</script>' })
  const view = mount(value)
  expect(view.container.textContent).not.toMatch(/PRIVATE_/)
  expect(view.container.querySelector('a, script, img, iframe, video, audio')).toBeNull()
  const invalid = mount(changedFile({ source_path: '<img src=x onerror=alert(1)>', path: '旧版本/<img src=x onerror=alert(1)>' }))
  expect(field(invalid.container, '确切版本检查')).toBe('有效性待核对')
  expect(invalid.container.querySelector('img')).toBeNull()
})

test.each([320, 390, 1440])('长中文和连续路径在宽度 %i 保留原文、换行约束和内部限高；实际像素由主线程浏览器验收', width => {
  vi.stubGlobal('innerWidth', width)
  const filename = `${'版本对照成果'.repeat(8)}.zip`
  const sourcePath = `src/${Array.from({ length: 12 }, (_, index) => `第${index + 1}段${'长中文成员路径'.repeat(5)}`).join('/')}/${'W'.repeat(200)}.py`
  const value = result()
  const view = mount(result({
    checked_artifacts: value.checked_artifacts.map(artifact => ({ ...artifact, filename })),
    checked_files: value.checked_files.map((file, index) => ({ ...file, source_path: sourcePath, path: `${value.checked_artifacts[index].directory}/${sourcePath}` })),
  }))
  const root = within(view.container).getByLabelText('版本副本检查回执')
  expect(field(view.container, '确切版本检查')).toBe('记录有效')
  expect(root.className).toContain('[overflow-wrap:anywhere]')
  expect(root.className).toContain('min-w-0')
  expect(root.className).toContain('max-w-full')
  expect(root.textContent).toContain(filename)
  const mapping = within(root).getByLabelText('原路径与实际执行路径')
  expect(mapping.textContent).toContain(sourcePath)
  expect(mapping.textContent).toContain(`旧版本/${sourcePath}`)
  for (const list of [within(root).getByLabelText('版本与挂载目录'), mapping, within(root).getByLabelText('版本与副本 SHA-256')]) {
    expect(list.className).toMatch(/max-h-(48|64)/)
    expect(list.className).toContain('overflow-y-auto')
    expect(list.className).toContain('overflow-x-hidden')
    expect(list.tabIndex).toBe(0)
  }
  expect(root.querySelector('[class*="rounded"], table')).toBeNull()
  expect(root.querySelectorAll('details')).toHaveLength(2)
  expect(root.querySelector('details details')).toBeNull()
})
