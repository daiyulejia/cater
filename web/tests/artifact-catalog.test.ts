import { expect, test } from 'vitest'
import type { Artifact } from '../src/Artifact'
import { teamArtifactCatalog } from '../src/artifactCatalog'
import type { TeamEvent, TeamState } from '../src/team'

const hash = 'a'.repeat(64)
const artifact = (seq: number, filename?: string, change: Partial<Artifact> = {}): Artifact => ({
  seq, filename, title: filename ?? '同一个标题', summary: '正文摘要', content: `版本${seq}`, content_sha256: hash, ...change,
})
const host = { id: 'team', role: 'team', kind: 'system' }
const event = (seq: number, type: string, payload: Record<string, unknown> = {}, actor = host): TeamEvent => ({ seq, type, payload, actor })
const started = event(1, 'team.started')
const state = (change: Partial<TeamState> = {}): TeamState => ({ enabled: true, objective: '完成网页', status: 'running',
  revision: 0, checkpoint: '', runs: [], assignments: [], pending_count: 0, ...change })
const done = (items: Artifact[], seq = 20, change: Record<string, unknown> = {}) => event(seq, 'team.objective_completed', {
  artifacts: items.map(item => ({ seq: item.seq, filename: item.filename, content_sha256: item.content_sha256 })), ...change,
})
const project = (items: Artifact[], events = [started], team = state()) => teamArtifactCatalog(team, items, events)
const ids = (items: Artifact[]) => items.map(item => item.seq)

test('工程ZIP没有内联正文仍绑定确切交付版本，不取后来的同名版本', () => {
  const first = artifact(3, 'project.zip', { format: 'archive', content: undefined, content_bytes: 200,
    archive: { version: 1, files: [{ path: 'src/main.py', bytes: 0, sha256: hash, executable: false }], total_bytes: 0 } })
  const later = { ...first, seq: 22, content_sha256: 'b'.repeat(64) }
  const catalog = project([first, later], [started, done([first])], state({ status: 'done', revision: 20 }))
  expect(catalog.kind).toBe('delivery')
  expect(ids(catalog.primary)).toEqual([3])
  expect(ids(catalog.later)).toEqual([22])
  expect(project([{ ...first, archive: undefined }], [started, done([first])], state({ status: 'done', revision: 20 })).kind).toBe('unresolved')
})

test('本轮同名文件只显示最新事件，乱序输入不修改原数据，历史仍完整', () => {
  const items = [artifact(8, 'app.js'), artifact(2, 'index.html'), artifact(6, 'index.html'), artifact(3, 'app.js')]
  const source = JSON.stringify(items)
  const value = project(items)
  expect(value.kind).toBe('current')
  expect(ids(value.primary)).toEqual([8, 6])
  expect(ids(value.history)).toEqual([3, 2])
  expect(value.later).toEqual([])
  expect(JSON.stringify(items)).toBe(source)
})

test('同hash不同文件保留，同名同hash的重新发布仍以最新事件为候选', () => {
  const value = project([artifact(2, 'one.js'), artifact(3, 'two.js'), artifact(4, 'one.js')])
  expect(ids(value.primary)).toEqual([4, 3])
  expect(ids(value.history)).toEqual([2])
})

test('独立资源不能按标题去重，大小写及Unicode文件名不悄悄合并', () => {
  const items = [artifact(2), artifact(3), artifact(4, 'index.html'), artifact(5, 'INDEX.html'),
    artifact(6, 'caf\u00e9.txt'), artifact(7, 'cafe\u0301.txt')]
  expect(ids(project(items).primary)).toEqual([7, 6, 5, 4, 3, 2])
})

test('最新版本正文错误不自动换回旧的可读正文冒充最新', () => {
  const value = project([artifact(2, 'note.txt'), artifact(4, 'note.txt', { content: undefined, content_error: 'too_large' })])
  expect(value.primary[0].content_error).toBe('too_large')
  expect(ids(value.history)).toEqual([2])
})

test('新目标尚未发布时不把旧目标的文件算成本轮', () => {
  const value = project([artifact(3, 'old.txt')], [started, event(8, 'team.objective_updated', { goal: '新任务' })], state({ revision: 8 }))
  expect(value.kind).toBe('current')
  expect(value.primary).toEqual([])
  expect(ids(value.history)).toEqual([3])
})

test('新目标只挑边界后的最新发布，不把旧版独立文件重新贴上当前标签', () => {
  const value = project([artifact(2, 'old.txt'), artifact(4, 'app.js'), artifact(9, 'app.js'), artifact(12, 'app.js')],
    [event(7, 'team.objective_updated'), started], state({ revision: 7 }))
  expect(ids(value.primary)).toEqual([12])
  expect(ids(value.history)).toEqual([9, 4, 2])
})

test('旧群启用新协作前的成果保留为历史', () => {
  const value = project([artifact(2, 'old.txt'), artifact(9, 'new.txt')], [event(8, 'team.started')])
  expect(ids(value.primary)).toEqual([9])
  expect(ids(value.history)).toEqual([2])
})

test.each([{ events: [] }, { events: [event(1, 'team.objective_updated', {}, { ...host, kind: 'agent' })] }])('没有可信本轮边界只叫已发布，不假装已核对当前范围', ({ events }) => {
  const value = project([artifact(4, 'app.js')], events, state({ revision: 1 }))
  expect(value.kind).toBe('published')
  expect(value.deliverySeq).toBeUndefined()
})

test('交付区按冻结清单选择，后来的同名文件不能覆盖下载版本', () => {
  const old = artifact(2, 'index.html'), current = artifact(5, 'index.html'), other = artifact(7, 'app.js'), later = artifact(25, 'index.html')
  const value = project([later, old, other, current], [done([current, other]), started], state({ status: 'done', revision: 20 }))
  expect(value.kind).toBe('delivery')
  expect(value.deliverySeq).toBe(20)
  expect(ids(value.primary)).toEqual([5, 7])
  expect(ids(value.history)).toEqual([2])
  expect(ids(value.later)).toEqual([25])
})

test('合法空交付不把已有的历史文件塞进正式交付', () => {
  const value = project([artifact(2, 'old.txt')], [started, done([])], state({ status: 'done', revision: 20 }))
  expect(value.kind).toBe('delivery')
  expect(value.primary).toEqual([])
  expect(ids(value.history)).toEqual([2])
})

test('新目标完成不把跨目标的默认旧文件当作当前交付，明确沿用才允许', () => {
  const file = artifact(5, 'app.js'), boundary = event(10, 'team.objective_updated')
  const old = project([file], [started, boundary, done([file])], state({ status: 'done', revision: 20 }))
  expect(old.kind).toBe('unresolved')
  expect(old.deliverySeq).toBeUndefined()
  const explicit = project([file], [started, boundary, done([file], 20, { artifact_selection: 'explicit', objective_seq: 10 })], state({ status: 'done', revision: 20 }))
  expect(explicit.kind).toBe('delivery')
  expect(ids(explicit.primary)).toEqual([5])
  const wrongScope = project([file], [started, boundary, done([file], 20, { artifact_selection: 'explicit', objective_seq: 1 })], state({ status: 'done', revision: 20 }))
  expect(wrongScope.kind).toBe('unresolved')
})

test.each([undefined, null, {}, [5], [{ seq: true }], [{ seq: 30, filename: 'app.js', content_sha256: hash }],
  [{ seq: 5, filename: 'app.js', content_sha256: 'wrong' }]])('畸形冻结清单不回退到最新冒充正式交付：%j', refs => {
  const value = project([artifact(5, 'app.js')], [done([], 20, { artifacts: refs })], state({ status: 'done', revision: 20 }))
  expect(value.kind).toBe('unresolved')
  expect(value.issue).toBeTruthy()
  expect(value.deliverySeq).toBeUndefined()
})

test.each([
  { content_sha256: undefined }, { content_sha256: 'b'.repeat(64) }, { filename: 'elsewhere.js' },
  { content: undefined }, { content_error: 'too_large' },
])('冻结文件元数据或正文不完整不能当作正式交付：%j', change => {
  const file = artifact(5, 'app.js')
  expect(project([{ ...file, ...change }], [done([file])], state({ status: 'done', revision: 20 })).kind).toBe('unresolved')
})

test('不存在或重复的成果事件都不能静默替代交付引用', () => {
  const file = artifact(5, 'app.js')
  for (const items of [[], [file, { ...file }]]) {
    expect(project(items, [done([file])], state({ status: 'done', revision: 20 })).kind).toBe('unresolved')
  }
})

test.each([['app.js', 'app.js'], ['index.html', 'INDEX.html'], ['caf\u00e9.js', 'cafe\u0301.js']])('交付清单重复或等价名称不静默合并：%s %s', (one, two) => {
  const items = [artifact(5, one), artifact(7, two)]
  expect(project(items, [done(items)], state({ status: 'done', revision: 20 })).kind).toBe('unresolved')
})

test('重复冻结序号不显示成两份正式文件', () => {
  const item = artifact(5, 'app.js')
  expect(project([item], [done([item, item])], state({ status: 'done', revision: 20 })).kind).toBe('unresolved')
})

test('旧完成、伪造来源、新目标在完成后均不构成当前交付', () => {
  const file = artifact(5, 'app.js')
  for (const events of [[done([file], 19)], [{ ...done([file]), actor: { ...host, kind: 'agent' } }],
    [done([file]), event(21, 'team.objective_updated')]]) {
    const value = project([file], events, state({ status: 'done', revision: 20 }))
    expect(value.kind).toBe('unresolved')
    expect(value.deliverySeq).toBeUndefined()
  }
})
