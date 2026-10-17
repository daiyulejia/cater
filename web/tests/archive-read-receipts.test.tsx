import { createHash } from 'node:crypto'
import { fireEvent, render, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import { NativeArtifactReceipt } from '../src/NativeArtifactReceipt'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity, nativeArtifactReceiptStatus } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

type Data = Record<string, unknown>
const host = { id: 'team', kind: 'system', role: 'team' }
const digest = (text: string) => createHash('sha256').update(text).digest('hex')
const size = (text: string) => new TextEncoder().encode(text).length
const archiveSha = digest('saved ZIP fixture')
const text = '甲😀乙🚀丙'
const path = 'src/main.ts'
const uncertain = '成果读取回执待核对'
const statuses = ['工程文件清单已读取', '工程文件正文已读取', '工程文件本页已读取', '工程二进制字节已核对']
const event = (seq: number, type: string, payload: Data, actor = host): TeamEvent => ({ seq, type, payload, actor })
const start = (args: Data = {}) => event(10, 'team.tool_started', { name: 'read_artifact', role: 'dev',
  run_id: 'archive-run', message_seq: 1, call_id: 'archive-read', arguments: { artifact_seq: 4, ...args } })
const finish = (result: unknown, started = start(), actor = host) => event(11, 'team.tool_finished', {
  ...started.payload, started_seq: started.seq, status: 'ok', result,
}, actor)
const manifest = (extra: Data = {}): Data => ({ status: 'ok', seq: 4, filename: '工程.zip', title: '工程.zip',
  format: 'archive', content_sha256: archiveSha, content_bytes: 512, source: 'team', phase: 'team',
  archive: { version: 1, total_bytes: size(text) + 2, files: [
    { path: 'assets/data.bin', bytes: 2, sha256: digest('\0x'), executable: false },
    { path, bytes: size(text), sha256: digest(text), executable: true },
  ] }, ...extra })
// Match read_member's native keys: no legacy seq/filename/content_bytes aliases.
const member = (full = text, offset = 0, length = 8000, extra: Data = {}): Data => {
  const chars = Array.from(full), content = chars.slice(offset, offset + length).join('')
  const end = offset + Array.from(content).length
  return { status: 'ok', artifact_seq: 4, archive_sha256: archiveSha, path, bytes: size(full),
    sha256: digest(full), executable: true, content_kind: 'utf8', content,
    read_range: { unit: 'unicode_codepoints', offset, returned_chars: end - offset, total_chars: chars.length,
      has_more: end < chars.length, next_offset: end < chars.length ? end : null,
      content_complete: offset === 0 && end === chars.length }, ...extra }
}
const binary = (extra: Data = {}): Data => ({ status: 'ok', artifact_seq: 4, archive_sha256: archiveSha,
  path: 'assets/data.bin', bytes: 2, sha256: digest('\0x'), executable: false, content_kind: 'binary', content: null,
  note: '仅核对保存的二进制字节与摘要，未解释或执行文件内容。', ...extra })
const team: TeamState = { enabled: true, objective: '读取工程', status: 'running', revision: 0,
  runs: [], assignments: [], pending_count: 0, checkpoint: '' }
const activityLine = (started: TeamEvent, receipt: TeamEvent) => conversationActivity({ team,
  events: [event(1, 'team.message_received', { text: '读取工程', recipient: 'dev', kind: 'message' },
    { id: 'human', kind: 'human', role: 'human' }),
  event(2, 'team.run_started', { role: 'dev', run_id: 'archive-run', message_seq: 1, revision: 0 }), started, receipt],
  names: { dev: '开发' }, leaderId: 'manager', sending: false, executionState: 'running',
}).lines.find(line => line.role === 'dev')!
const surfaces = (receipt: TeamEvent, started: TeamEvent) => [
  render(<EventDetails event={receipt} />),
  render(<TeamToolProgress events={[started, receipt]} names={{ dev: '开发' }} full />),
]
const checkStatus = (result: unknown, args: Data, status: string) => {
  const started = start(args), receipt = finish(result, started)
  expect(nativeArtifactReceiptStatus(receipt)).toBe(status)
  const line = activityLine(started, receipt)
  expect(line.text).toContain(`读取成果 · ${status}`)
  expect(line.warning).toBe(status === uncertain)
  return surfaces(receipt, started)
}
const reject = (result: unknown, args: Data) => {
  for (const view of checkStatus(result, args, uncertain)) {
    expect(view.container.querySelector('pre, ul, a, img, script')).toBeNull()
    expect(view.container.textContent).not.toMatch(/甲|😀|assets\/data.bin|src\/main.ts|工程包版本|alert\(1\)|PRIVATE_/)
    for (const status of statuses) expect(view.container.textContent).not.toContain(status)
    expect(view.container.textContent).not.toMatch(/工具已完成|原始事件 JSON/)
    view.unmount()
  }
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('Archive receipt tests must remain offline') }))
})

test.each(['src/COM0.txt', 'src/LPT0.txt'])('宿主允许的设备近似名正常读取：%s', accepted => {
  const value = manifest({ archive: { version: 1, total_bytes: size(text), files: [
    { path: accepted, bytes: size(text), sha256: digest(text), executable: false },
  ] } })
  for (const view of checkStatus(value, {}, statuses[0])) {
    expect(view.container.textContent).toContain(accepted)
    view.unmount()
  }
  for (const view of checkStatus(member(text, 0, 8000, { path: accepted }), { path: accepted }, statuses[1])) {
    expect(view.container.querySelector('pre')?.textContent).toBe(text)
    view.unmount()
  }
  for (const view of checkStatus(binary({ path: accepted }), { path: accepted }, statuses[3])) {
    expect(view.container.querySelector('pre')).toBeNull()
    view.unmount()
  }
})

test.each(['src/COM1.txt', 'src/LPT1.txt'])('真实保留设备名仍拒绝：%s', rejected => {
  reject(member(text, 0, 8000, { path: rejected }), { path: rejected })
  reject(binary({ path: rejected }), { path: rejected })
  reject(manifest({ archive: { version: 1, total_bytes: 0, files: [
    { path: rejected, bytes: 0, sha256: digest(''), executable: false },
  ] } }), {})
})

test('真实清单回执：三个表面共用状态，版本、清单和ZIP指纹默认折叠', () => {
  const result = manifest()
  expect(result).not.toHaveProperty('content')
  for (const view of checkStatus(result, {}, statuses[0])) {
    expect(view.container.textContent).toContain('工程包版本 #4')
    expect(view.container.textContent).toContain('读取不代表复核通过、验证完成或任务交付')
    expect(view.container.querySelector('pre, a, button')).toBeNull()
    const list = within(view.container).getByLabelText('已读取的工程文件清单')
    expect(list.textContent).toContain(path)
    expect(list.textContent).toContain('assets/data.bin')
    expect(list.textContent).toContain('17 字节')
    expect(list.textContent).toContain('可执行文件')
    expect(list.closest('details')?.open).toBe(false)
    for (const detail of view.container.querySelectorAll('details')) expect(detail.open).toBe(false)
    fireEvent.click(list.closest('details')!.querySelector('summary')!)
    expect(list.closest('details')?.open).toBe(true)
    const fingerprint = within(view.container).getByText('工程包指纹与大小').closest('details')!
    fireEvent.click(fingerprint.querySelector('summary')!)
    expect(fingerprint.textContent).toContain(archiveSha)
    expect(fingerprint.textContent).toContain('512 字节')
    expect(view.container.textContent).toMatch(/未读取成员正文/)
    expect(view.container.textContent).toMatch(/未.*核对 ZIP 字节/)
  }
  expect(fetch).not.toHaveBeenCalled()
})

test.each([
  ['完整', text, 0, 8000, statuses[1]], ['首个码点页', text, 0, 2, statuses[2]],
  ['尾页', text, 3, 2, statuses[2]], ['EOF空页', text, 5, 2, statuses[2]],
  ['空文件', '', 0, 8000, statuses[1]], ['非BMP全文', '😀🚀', 0, 8000, statuses[1]],
] as const)('%s：原始成员path、码点分页、折叠正文和完整成员指纹', (_name, full, offset, length, status) => {
  const result = member(full, offset, length)
  expect(result).not.toHaveProperty('seq')
  expect(result).not.toHaveProperty('content_bytes')
  for (const view of checkStatus(result, { path, offset, length }, status)) {
    expect(view.container.textContent).toContain(`工程包版本 #4 · ${path}`)
    expect(view.container.textContent).toContain(`本页起点：${offset} · 本页 ${Array.from(result.content as string).length} 字符 · 全文共 ${Array.from(full).length} 字符`)
    expect(view.container.textContent).toContain('读取不代表复核通过、验证完成或任务交付')
    expect(view.container.textContent).toContain(status === statuses[1] ? '本页覆盖全文' : '本页未覆盖全文')
    const pre = within(view.container).getByLabelText('成果读取正文')
    expect(pre.textContent).toBe(result.content || '（空正文）')
    expect(pre.tabIndex).toBe(0)
    expect(pre.className).toContain('max-h-64')
    expect(pre.className).toContain('overflow-auto')
    expect(pre.className).toContain('[overflow-wrap:anywhere]')
    expect(pre.closest('details')?.open).toBe(false)
    fireEvent.click(pre.closest('details')!.querySelector('summary')!)
    expect(pre.closest('details')?.open).toBe(true)
    const fingerprint = within(view.container).getByText('成员文件指纹与大小').closest('details')!
    expect(fingerprint.open).toBe(false)
    expect(fingerprint.textContent).toContain(digest(full))
    expect(fingerprint.textContent).toContain(archiveSha)
    expect(fingerprint.textContent).toContain(`${size(full)} 字节`)
    if (offset > 0) {
      expect(view.container.textContent).toContain('末页不代表已读取全文')
      expect(view.container.textContent).not.toContain('本页覆盖全文')
    }
    if ((result.read_range as Data).has_more) expect(view.container.textContent).toContain(`下一页起点 ${offset + length}`)
    else {
      expect(view.container.textContent).toContain('已到末页，无下一页')
      expect(view.container.textContent).not.toContain('下一页起点')
    }
  }
})

test.each([{}, { offset: 1 }, { length: 16000 }])('成员默认分页及最大页遵循宿主：%j', args => {
  const full = '😀'.repeat(16001), offset = 'offset' in args ? args.offset! : 0
  const length = 'length' in args ? args.length! : 8000
  for (const view of checkStatus(member(full, offset, length), { path, ...args }, statuses[2])) {
    expect(Array.from(view.container.querySelector('pre')!.textContent!).length).toBe(length)
  }
})

test.each([{}, { offset: 9000, length: 1 }])('binary content=null且无范围，只核对字节，不冒充正文：%j', args => {
  for (const view of checkStatus(binary(), { path: 'assets/data.bin', ...args }, statuses[3])) {
    expect(view.container.textContent).toContain('工程包版本 #4 · assets/data.bin')
    expect(view.container.textContent).toContain('未解释或执行文件内容')
    expect(view.container.textContent).toContain('读取不代表复核通过、验证完成或任务交付')
    expect(view.container.querySelector('pre, ul')).toBeNull()
    expect(view.container.textContent).not.toMatch(/本页起点|本页覆盖全文|正文已读取|本页已读取/)
    expect(view.container.textContent).toContain('2 字节')
  }
})

test.each([
  { seq: 5 }, { seq: '4' }, { format: 'zip' }, { filename: '../工程.zip' },
  { content_sha256: 'bad' }, { content_sha256: undefined }, { content_bytes: 0 },
  { content_bytes: '512' }, { content_bytes: true }, { content_bytes: 65 * 1024 * 1024 + 1 },
  { content: '' }, { read_range: null }, { archive: null },
  { archive: { version: 1, total_bytes: 0, files: [] } },
])('清单结构、版本和ZIP元信息不可信时不显示清单：%j', extra => reject(manifest(extra), {}))

test.each([
  { version: 2 }, { version: true }, { total_bytes: 1 }, { total_bytes: '19' }, { extra: true },
  { files: Array(257).fill({ path, bytes: 0, sha256: digest(''), executable: false }) },
])('清单顶层伪字段或大小拒绝：%j', extra => reject(manifest({ archive: { ...(manifest().archive as Data), ...extra } }), {}))

test.each([
  { bytes: -1 }, { bytes: 1.5 }, { bytes: true }, { bytes: '17' }, { bytes: 32 * 1024 * 1024 + 1 },
  { sha256: 'g'.repeat(64) }, { sha256: undefined }, { executable: 'true' }, { executable: undefined },
  { extra: 'PRIVATE_FIELD' }, { path: '../secret' }, { path: 'a//secret' }, { path: 'CON.txt' },
  { path: 'javascript:alert(1)' }, { path: '<script>alert(1)</script>' },
])('清单成员假字段/hash/大小/路径拒绝：%j', extra => {
  const entry = { path, bytes: size(text), sha256: digest(text), executable: false, ...extra }
  reject(manifest({ archive: { version: 1, total_bytes: entry.bytes, files: [entry] } }), {})
})

test.each([
  { artifact_seq: 5 }, { artifact_seq: '4' }, { path: 'src/other.ts' }, { path: '../secret' },
  { archive_sha256: 'bad' }, { archive_sha256: undefined }, { sha256: 'bad' }, { sha256: undefined },
  { bytes: undefined }, { bytes: '17' }, { bytes: true }, { bytes: 1 }, { bytes: -1 }, { bytes: 1.5 },
  { bytes: Number.MAX_SAFE_INTEGER + 1 }, { executable: 'true' }, { executable: undefined },
  { content_kind: 'text' }, { content_kind: undefined }, { content: null }, { read_range: undefined },
  ...['seq', 'filename', 'content_bytes', 'content_sha256', 'format', 'archive'].map(key => ({ [key]: manifest()[key] })),
])('成员错seq/path与伪旧字段不允许投影正文：%j', extra => reject(member(text, 0, 2, extra), { path, length: 2 }))

test.each([
  { artifact_seq: 5 }, { artifact_seq: '4' }, { path: 'SRC/main.ts' }, { path: undefined }, { path: null },
  { offset: 1 }, { offset: -1 }, { offset: false }, { offset: '0' }, { offset: null },
  { length: 1 }, { length: 0 }, { length: '2' }, { length: 16001 }, { length: null },
])('请求版本、原始path或分页不匹配：%j', args => reject(member(text, 0, 2), { path, length: 2, ...args }))

test.each([
  ...['unit', 'offset', 'returned_chars', 'total_chars', 'has_more', 'next_offset', 'content_complete'].map(key => ({ [key]: undefined })),
  { unit: 'utf16' }, { returned_chars: 3 }, { returned_chars: '2' }, { total_chars: Infinity },
  { offset: 0.5 }, { offset: NaN }, { total_chars: 1 }, { has_more: 'true' },
  { has_more: false, next_offset: null }, { next_offset: 3 }, { content_complete: true },
])('成员分页篡改拒绝，emoji按码点计数：%j', extra => {
  const result = member(text, 0, 2)
  reject({ ...result, read_range: { ...(result.read_range as Data), ...extra } }, { path, length: 2 })
})

test('空文本大小、EOF范围及零进度伪页不能声称成功', () => {
  reject(member('', 0, 8000, { bytes: 1 }), { path })
  reject(member(text, 6, 2), { path, offset: 6, length: 2 })
  reject(member(text, 0, 2, { content: '', read_range: { unit: 'unicode_codepoints', offset: 0,
    returned_chars: 0, total_chars: 5, has_more: true, next_offset: 0, content_complete: false } }), { path, length: 2 })
})

test.each([{ content: '' }, { content: 'PRIVATE_BINARY' }, { content: undefined }, { read_range: null },
  { read_range: member().read_range }, { content_kind: 'unknown' }])('二进制假正文/假范围不降级成功：%j', extra => {
  reject(binary(extra), { path: 'assets/data.bin' })
})

test.each([{ status: 'partial' }, { status: 'unknown' }, { complete: false }, { truncated: true },
  { outcome: 'partial' }, { outcome: 'unknown' }])('三类回执partial/unknown均保持待核对：%j', extra => {
  reject(manifest(extra), {})
  reject(member(text, 0, 2, extra), { path, length: 2 })
  reject(binary(extra), { path: 'assets/data.bin' })
})

test.each(['partial', 'unknown'])('外层%s不被内层ok覆盖', status => {
  for (const [result, args] of [[manifest(), {}], [member(), { path }], [binary(), { path: 'assets/data.bin' }]] as const) {
    const started = start(args), receipt = finish(result, started)
    receipt.payload.status = status
    expect(nativeArtifactReceiptStatus(receipt)).toBe(uncertain)
    expect(activityLine(started, receipt).warning).toBe(true)
    for (const view of surfaces(receipt, started)) {
      expect(view.container.textContent).toContain(uncertain)
      expect(view.container.querySelector('pre, ul')).toBeNull()
    }
  }
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('三种回执均校验宿主三项身份，伪来源不泄露：%j', actor => {
  for (const [result, args] of [[manifest(), {}], [member(), { path }], [binary(), { path: 'assets/data.bin' }]] as const) {
    const started = start(args), receipt = finish(result, started, actor)
    expect(nativeArtifactReceiptStatus(receipt)).toBe('成果读取来源待核对')
    const detail = render(<EventDetails event={receipt} />)
    expect(detail.container.textContent).toContain('成果读取来源待核对')
    expect(detail.container.querySelector('pre, ul')).toBeNull()
    expect(detail.container.textContent).not.toMatch(/工程包版本|src\/main.ts|assets\/data.bin|SHA-256/)
    expect(render(<NativeArtifactReceipt event={receipt} />).container.textContent).toBe('')
    const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
    expect(progress.container.textContent).toContain('已发起，等待回执')
    expect(progress.container.querySelector('pre, ul')).toBeNull()
    expect(activityLine(started, receipt).text).toContain('已发起，等待回执')
    expect(render(<TeamToolProgress events={[{ ...started, actor }, receipt]} names={{}} />).container.textContent).toBe('')
  }
})

test('危险正文保持纯文本，原始path保留字形，不产生链接或内部字段', () => {
  const content = '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[link](javascript:alert(1))\nhttps://example.invalid'
  const originalPath = `src/Ａ-${'长'.repeat(200)}.txt`
  const result = member(content, 0, 8000, { path: originalPath, note: 'PRIVATE_NOTE', cwd: 'PRIVATE_CWD',
    summary: 'PRIVATE_SUMMARY', env: { key: 'PRIVATE_KEY' } })
  for (const view of checkStatus(result, { path: originalPath }, statuses[1])) {
    expect(view.container.textContent).toContain(originalPath)
    expect(view.container.querySelector('pre')?.textContent).toBe(content)
    expect(view.container.querySelector('a, img, script, iframe, object, video, audio')).toBeNull()
    expect(view.container.textContent).not.toMatch(/PRIVATE_|原始事件 JSON/)
  }
  expect(fetch).not.toHaveBeenCalled()
})

test('切换同名版本、成员、范围或清单时，三个组件入口清除展开状态', () => {
  const first = finish(member(), start({ path }))
  const nextManifest = manifest({ seq: 5 })
  const targets = [finish(member(text, 0, 8000, { artifact_seq: 5 }), start({ artifact_seq: 5, path })),
    finish(member(text, 0, 8000, { path: 'src/other.ts' }), start({ path: 'src/other.ts' })),
    finish(member(text, 3, 2), start({ path, offset: 3, length: 2 })),
    finish(nextManifest, start({ artifact_seq: 5 })), finish(binary(), start({ path: 'assets/data.bin' }))]
  for (const surface of ['native', 'details', 'progress']) {
    const node = (receipt: TeamEvent) => surface === 'native' ? <NativeArtifactReceipt event={receipt} />
      : surface === 'details' ? <EventDetails event={receipt} />
        : <TeamToolProgress events={[start(receipt.payload.arguments as Data), receipt]} names={{}} full />
    for (const initial of [first, finish(manifest())]) for (const target of targets) {
      // Keep the event seq fixed too: receipt content changes must reset details independently.
      const view = render(node(initial))
      const receiptDetails = () => [...view.container.querySelectorAll('details')].filter(detail =>
        !detail.querySelector(':scope > summary')?.textContent?.startsWith('工具过程'))
      for (const detail of receiptDetails()) fireEvent.click(detail.querySelector('summary')!)
      expect(receiptDetails().length).toBeGreaterThan(0)
      expect(receiptDetails().every(detail => detail.open)).toBe(true)
      view.rerender(node(target))
      expect(receiptDetails().every(detail => !detail.open), `${surface}: ${String(target.payload.result && (target.payload.result as Data).path)}`).toBe(true)
      expect(view.container.textContent).toContain('工程包版本')
      view.unmount()
    }
  }
})
