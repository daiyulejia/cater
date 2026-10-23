import { applyPatch, reversePatch } from 'diff'
import { expect, test } from 'vitest'
import { comparisonDefaults, comparisonGroups, hasComparisonCandidates } from '../src/artifactComparisonData'
import { textComparison } from '../src/ArtifactComparison'
import type { Artifact } from '../src/Artifact'
import type { TeamEvent } from '../src/team'

const file = (seq: number, extra: Partial<Artifact> = {}): Artifact => ({ seq, filename: 'receipt.py', title: '正文', summary: '',
  format: 'code', content: `version ${seq}\n`, actor: 'dev', ...extra })
const boundary = (seq: number, extra: Partial<TeamEvent> = {}): TeamEvent => ({ seq, type: 'team.objective_updated',
  actor: { id: 'team', kind: 'system', role: 'team' }, payload: {}, ...extra })

test('同名按确切seq分组，不按摘要/actor/hash合并，并按真实事件排序', () => {
  const groups = comparisonGroups([file(5), file(2, { actor: 'manager', content_sha256: 'same' }), file(8, { filename: 'other.py' })], [boundary(1)])
  expect(groups).toHaveLength(1)
  expect(groups[0].versions.map(v => [v.item.seq, v.item.actor, v.objective])).toEqual([[2, 'manager', 1], [5, 'dev', 1]])
})

test('缺失/报错/归档/超限/字节数不符/重复编号正文都不当成完整版本', () => {
  const groups = comparisonGroups([file(1), file(2, { content: undefined }), file(3, { content_error: 'too_large' }),
    file(4, { format: 'archive' }), file(5, { content: 'x'.repeat(65537) }), file(6, { content_bytes: 1 }),
    file(7), file(7, { filename: 'another' }), file(-1), file(8, { content: '中', content_bytes: 3 })], [])
  expect(groups[0].versions.map(v => v.item.seq)).toEqual([1, 8])
  expect(groups[0].versions.every(v => v.objective === 0)).toBe(true)
})

test('允许真实空串与完整64KiB字符串，不将空白或空串当作正文缺失', () => {
  const groups = comparisonGroups([file(2, { content: '' }), file(3, { content: ' '.repeat(65536), content_bytes: 65536 })], [boundary(1)])
  expect(groups[0].versions.map(v => v.item.content.length)).toEqual([0, 65536])
})

test('默认新版是明确展示的交付版，不是后来发布；旧版只自动选同目标先前版本', () => {
  const items = [file(2), file(5), file(25)]
  const groups = comparisonGroups(items, [boundary(1)])
  expect(comparisonDefaults(groups, [items[1]])).toEqual({ filename: 'receipt.py', older: 2, newer: 5 })
})

test('跨目标或目标未确认时不擅自猜比较基线；伪造目标边界不计入', () => {
  const forged = boundary(4, { actor: { id: 'team', kind: 'agent', role: 'team' } })
  const groups = comparisonGroups([file(2), file(5)], [boundary(1), boundary(3), forged])
  expect(groups[0].versions.map(v => v.objective)).toEqual([1, 3])
  expect(comparisonDefaults(groups, [file(5)])).toEqual({ filename: 'receipt.py', older: 0, newer: 5 })
  expect(comparisonDefaults(comparisonGroups([file(2), file(5)], []), [file(5)])?.older).toBe(0)
  expect(comparisonDefaults([], [])).toBeUndefined()
})

test('缺失可比较交付版或当前目标无文件，必须明确选择，不拿后发布或旧目标代替', () => {
  const preferred = file(5, { content_bytes: 1 })
  const groups = comparisonGroups([file(2), preferred, file(25)], [boundary(1)])
  expect(comparisonDefaults(groups, [preferred])).toEqual({ filename: 'receipt.py', older: 0, newer: 0 })
  expect(comparisonDefaults(groups, [])).toEqual({ filename: 'receipt.py', older: 0, newer: 0 })
})

test('异常显示元数据不成为React子节点，轻量关闭入口不编码正文', () => {
  const object = {} as unknown as string
  const artifacts = [file(2, { filename: object }), file(5, { actor: object, content_sha256: object }), file(8)]
  const groups = comparisonGroups(artifacts, [])
  expect(groups).toHaveLength(1)
  expect(groups[0].versions[0].item.actor).toBeUndefined()
  expect(groups[0].versions[0].item.content_sha256).toBeUndefined()
  let encodes = 0
  const original = TextEncoder.prototype.encode
  TextEncoder.prototype.encode = function (...args) { encodes++; return original.apply(this, args) }
  try { expect(hasComparisonCandidates(artifacts)).toBe(true) } finally { TextEncoder.prototype.encode = original }
  expect(encodes).toBe(0)
})

test.each([
  ['alpha\nbeta\n', 'alpha\nfixed\nbeta\n'],
  ['old\n', ''], ['', 'new\n'], [' ', '  '], ['A\n', 'a\n'],
  ['a\r\nb\r\n', 'a\nb\n'], ['line', 'line\n'], ['line\n', 'line'],
  ['缅因🙂\ne\u0301\n', '三花🙂\né\n'],
  ['<script>bad()</script>\n', '<img src="https://example.invalid/pixel">\n'],
])('原始差异可正反还原，不忽略空白、Unicode、CRLF或EOF：%j → %j', (older, newer) => {
  const result = textComparison(older, newer)
  expect(result.kind).toBe('diff')
  if (result.kind !== 'diff') throw new Error('Expected complete patch')
  expect(applyPatch(older, result.patch)).toBe(newer)
  expect(applyPatch(newer, reversePatch(result.patch))).toBe(older)
})

test('相同仅据实际正文，不据发布hash或摘要', () => {
  expect(textComparison('same\n', 'same\n')).toEqual({ kind: 'equal' })
})

test('单行64KiB仍是完整差异，更大正文不进计算', () => {
  const result = textComparison('x'.repeat(65536), 'x'.repeat(65535) + 'y')
  expect(result.kind).toBe('diff')
  if (result.kind === 'diff') expect(applyPatch('x'.repeat(65536), result.patch)).toBe('x'.repeat(65535) + 'y')
  expect(textComparison('x'.repeat(65537), 'y')).toEqual({ kind: 'limited' })
})

test('行数预算与编辑距离中止不是无改动，不返回半份patch', () => {
  expect(textComparison('x\n'.repeat(4000), 'y\n')).toEqual({ kind: 'limited' })
  expect(textComparison(Array.from({ length: 900 }, (_, i) => `old ${i}\n`).join(''),
    Array.from({ length: 900 }, (_, i) => `new ${i}\n`).join(''))).toEqual({ kind: 'limited' })
})
