import { webcrypto } from 'node:crypto'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { CREATION_RECOVERY_KEY, readAccessToken } from '../src/creationRecovery'
import { loadPageRecovery, PAGE_RECOVERY_KEY, PAGE_RECOVERY_TTL, savePageRecovery } from '../src/pageRecovery'
import type { PageDraftSnapshot } from '../src/pageRecovery'

const NOW = 1792195200000
const MAX_BYTES = 2 * 1024 * 1024
const snapshot = (): PageDraftSnapshot => ({
  drafts: { $new: '  新草稿\n', task_0123456789: '普通聊天', '$question:t1': '追问', '$derive:t1': '衍生' },
  selectedId: 't1', questionMode: false, derivation: { id: 't1', goal: '原始目标' },
  project: { id: 'p1', name: '项目' }, workspaceId: 'frontend-execute_1',
})
const readRecord = () => JSON.parse(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)!)
const writeRecord = (value: unknown) => window.sessionStorage.setItem(PAGE_RECOVERY_KEY, JSON.stringify(value))
const setToken = (token: string) => window.sessionStorage.setItem('cater.access_token', token)
const save = (value = snapshot(), token = '') => savePageRecovery(value, token, () => true)

test('未知提交只恢复原文与幂等标识，普通/提问/衍生分别归属，不重新发送', async () => {
  const requestId = '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca'
  const value = { ...snapshot(), requests: {
    conversation: { t1: { text: '  保留原文  ', requestId } },
    questions: { '$question:t1': { text: '旧成果怎么打开', requestId } },
    derivations: { '$derive:t1': { text: '衍生目标', requestId } },
  } }
  expect(await save(value)).toBe('')
  expect((await loadPageRecovery('')).snapshot).toEqual(value)
})

test.each([
  { conversation: {}, questions: {} },
  { conversation: { '../t1': { text: 'x', requestId: '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca' } }, questions: {}, derivations: {} },
  { conversation: {}, questions: { t1: { text: 'x', requestId: '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca' } }, derivations: {} },
  { conversation: { t1: { text: 'x', requestId: 'invalid' } }, questions: {}, derivations: {} },
  { conversation: { t1: { text: '', requestId: '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca' } }, questions: {}, derivations: {} },
  { conversation: { t1: { text: 'x', requestId: '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca', autoSend: true } }, questions: {}, derivations: {} },
])('无效提交恢复信息被拒绝，不以宽松读取丢掉原提交标识：%j', async requests => {
  expect(await save({ ...snapshot(), requests } as unknown as PageDraftSnapshot)).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}

async function delayDigest(token = 'account-a') {
  const hash = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(token))
  const pending = deferred<ArrayBuffer>()
  const digest = vi.fn(() => pending.promise)
  vi.stubGlobal('crypto', { subtle: { digest } })
  return { resolve: () => pending.resolve(hash), reject: () => pending.reject(new Error('digest failed')), digest }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
  readAccessToken()
  vi.stubGlobal('crypto', webcrypto)
  vi.spyOn(Date, 'now').mockReturnValue(NOW)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

test('只显式保存到当前 tab；保留原文和模式，恢复一次消费，不发网络请求', async () => {
  const fetch = vi.fn(() => { throw new Error('unexpected network') })
  vi.stubGlobal('fetch', fetch)
  const xhr = vi.spyOn(XMLHttpRequest.prototype, 'open')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
  const original = snapshot()
  expect(await save(original)).toBe('')
  expect(readRecord()).toEqual({ version: 1, account: 'anonymous', createdAt: NOW, snapshot: original })
  const result = await loadPageRecovery('')
  expect(result.snapshot).toEqual(original)
  expect(result.snapshot).not.toBe(original)
  expect(result.snapshot!.drafts).not.toBe(original.drafts)
  expect(result.snapshot!.derivation).not.toBe(original.derivation)
  expect(result.snapshot!.project).not.toBe(original.project)
  expect(result.notice).toContain('未发送')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
  expect(await loadPageRecovery('')).toEqual({ snapshot: null, notice: '' })
  expect(fetch).not.toHaveBeenCalled()
  expect(xhr).not.toHaveBeenCalled()
  expect(window.localStorage.length).toBe(0)
})

test.each([
  { selectedId: null, questionMode: false, derivation: null },
  { selectedId: 't1', questionMode: false, derivation: null },
  { selectedId: 't1', questionMode: true, derivation: null },
  { selectedId: 't1', questionMode: false, derivation: { id: 't1', goal: '' } },
])('恢复上下文保持模式，特殊草稿不变成聊天：%j', async (mode) => {
  const original = { ...snapshot(), ...mode }
  expect(await save(original)).toBe('')
  expect((await loadPageRecovery('')).snapshot).toEqual(original)
})

test('可省略 workspace；显式 undefined 规范化，空草稿和 null 项目合法', async () => {
  const original: PageDraftSnapshot = { drafts: {}, selectedId: null, questionMode: false, derivation: null, project: null, workspaceId: undefined }
  expect(await save(original)).toBe('')
  expect(readRecord().snapshot).not.toHaveProperty('workspaceId')
  expect((await loadPageRecovery('')).snapshot).toEqual({ drafts: {}, selectedId: null, questionMode: false, derivation: null, project: null })
})

test.each([null, '__managed__', 'frontend-execute_1'])('草稿恢复原样保留目录选择 %s', async (workspaceId) => {
  const original = { ...snapshot(), workspaceId }
  expect(await save(original)).toBe('')
  expect(readRecord().snapshot.workspaceId).toBe(workspaceId)
  expect((await loadPageRecovery('')).snapshot).toEqual(original)
})

test('单 tab 隔离，创建恢复 key 独立；只读取自身 key 和认证 key', async () => {
  window.sessionStorage.setItem(CREATION_RECOVERY_KEY, 'private creation record')
  window.sessionStorage.setItem('private.unrelated', 'private data')
  window.localStorage.setItem(PAGE_RECOVERY_KEY, 'other storage')
  const get = vi.spyOn(Storage.prototype, 'getItem')
  expect(await save()).toBe('')
  expect((await loadPageRecovery('')).snapshot).toEqual(snapshot())
  expect(get.mock.calls.every(([key]) => key === PAGE_RECOVERY_KEY || key === 'cater.access_token')).toBe(true)
  get.mockRestore()
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBe('private creation record')
  expect(window.sessionStorage.getItem('private.unrelated')).toBe('private data')
  expect(window.localStorage.getItem(PAGE_RECOVERY_KEY)).toBe('other storage')
  await save()
  const source = window.sessionStorage
  const other = new Map<string, string>()
  vi.spyOn(window, 'sessionStorage', 'get').mockReturnValue({
    getItem: (key) => other.get(key) ?? null, setItem: (key, value) => { other.set(key, value) },
    removeItem: (key) => { other.delete(key) }, clear: () => other.clear(), key: () => null, length: 0,
  })
  expect((await loadPageRecovery('')).snapshot).toBeNull()
  expect(source.getItem(PAGE_RECOVERY_KEY)).not.toBeNull()
})

test('100 个 draft、每份 100000 字符和 80 字符 workspace 的边界可保存', async () => {
  const value = snapshot()
  value.drafts = Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`t${i}`, i ? '' : '字'.repeat(100000)]))
  value.workspaceId = 'a'.repeat(80)
  expect(await save(value)).toBe('')
  expect((await loadPageRecovery('')).snapshot).toEqual(value)
})

test('总 JSON 按 UTF-8 字节计量，包含外层字段，恰好 2MiB 可保存，超 1 字节拒绝并保留旧记录', async () => {
  const value = snapshot()
  value.drafts = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`t${i}`, 'x'.repeat(100000)]))
  value.drafts.$new = ''
  const base = JSON.stringify({ version: 1, account: 'anonymous', createdAt: NOW, snapshot: value })
  value.drafts.$new = 'x'.repeat(MAX_BYTES - new TextEncoder().encode(base).byteLength)
  expect(await save(value)).toBe('')
  const old = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)!
  expect(new TextEncoder().encode(old).byteLength).toBe(MAX_BYTES)
  value.drafts.$new += 'x'
  expect(await save(value)).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(old)
  expect((await loadPageRecovery('')).snapshot!.drafts.$new).toHaveLength(value.drafts.$new.length - 1)
})

test('多字节或转义后的 JSON 超出总量时拒绝', async () => {
  const value = snapshot()
  value.drafts = Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`t${i}`, '字'.repeat(100000)]))
  expect(await save(value)).not.toBe('')
  value.drafts = Object.fromEntries(Array.from({ length: 4 }, (_, i) => [`t${i}`, '\u0000'.repeat(100000)]))
  expect(await save(value)).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

const invalidSnapshots: [string, (value: PageDraftSnapshot) => unknown][] = [
  ['超过 100 份', (v) => ({ ...v, drafts: Object.fromEntries(Array.from({ length: 101 }, (_, i) => [`t${i}`, ''])) })],
  ['超过 100000 字符', (v) => ({ ...v, drafts: { t1: 'x'.repeat(100001) } })],
  ['草稿不是字符串', (v) => ({ ...v, drafts: { t1: 7 } })],
  ['草稿数组', (v) => ({ ...v, drafts: [] })],
  ['草稿 null', (v) => ({ ...v, drafts: null })],
  ['缺字段', (v) => { const { project: _project, ...rest } = v; return rest }],
  ['未知顶层授权', (v) => ({ ...v, authorized: true })],
  ['未知衍生字段', (v) => ({ ...v, derivation: { ...v.derivation, send: true } })],
  ['未知项目字段', (v) => ({ ...v, project: { ...v.project, token: 'secret' } })],
  ['衍生与选中任务不同', (v) => ({ ...v, derivation: { id: 't2', goal: '' } })],
  ['衍生不能同时追问', (v) => ({ ...v, questionMode: true })],
  ['追问必须选中任务', (v) => ({ ...v, selectedId: null, derivation: null, questionMode: true })],
  ['衍生必须选中任务', (v) => ({ ...v, selectedId: null })],
  ['模式必须布尔值', (v) => ({ ...v, questionMode: 'false' })],
  ['目标必须字符串', (v) => ({ ...v, derivation: { id: 't1', goal: 0 } })],
  ['项目名必须字符串', (v) => ({ ...v, project: { id: 'p1', name: null } })],
  ['项目 ID 非法', (v) => ({ ...v, project: { id: '../p1', name: '' } })],
  ...['', 7, {}, 'F:/any-root', '../root', 'has space', 'x'.repeat(81)].map((workspaceId): [string, (v: PageDraftSnapshot) => unknown] =>
    [`非法 workspace ${JSON.stringify(workspaceId)}`, (v) => ({ ...v, workspaceId })]),
]

test.each(invalidSnapshots)('拒绝无效输入或记录并保留旧有效保存：%s', async (_name, change) => {
  await save()
  const old = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  const malformed = change(snapshot())
  expect(await save(malformed as PageDraftSnapshot)).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(old)
  const record = readRecord()
  record.snapshot = malformed
  writeRecord(record)
  expect((await loadPageRecovery('')).snapshot).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test.each(['__proto__', 'constructor', 'prototype', 'toString', '__defineGetter__', '../task', 'a/b', 'a\\b', 'a.b', 'a\n', 'a\u0000', '', '$new:x', '$control:t1', '$question:', '$derive:', 'a'.repeat(81)])('拒绝污染或非法 draft key 和 ID：%j', async (key) => {
  for (const candidate of [key, `$question:${key}`, `$derive:${key}`]) {
    const value = { ...snapshot(), drafts: { [candidate]: 'text' } }
    expect(await save(value)).not.toBe('')
    const record = { version: 1, account: 'anonymous', createdAt: NOW, snapshot: value }
    writeRecord(record)
    expect((await loadPageRecovery('')).snapshot).toBeNull()
    expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
  }
  expect(await save({ ...snapshot(), selectedId: key, derivation: null })).not.toBe('')
  expect({}).not.toHaveProperty('polluted')
})

test('保存只接受数据字段；拒绝 getter、symbol、继承字段和 toJSON，不执行它们', async () => {
  const getter = vi.fn(() => 'secret')
  const value = snapshot()
  Object.defineProperty(value.drafts, 't2', { get: getter, enumerable: true })
  expect(await save(value)).not.toBe('')
  expect(getter).not.toHaveBeenCalled()
  expect(await save({ ...snapshot(), [Symbol('private')]: 'hidden' })).not.toBe('')
  const inherited = Object.assign(Object.create({ authorized: true }), snapshot())
  expect(await save(inherited)).not.toBe('')
  const toJSON = vi.fn(() => snapshot())
  expect(await save({ ...snapshot(), toJSON } as PageDraftSnapshot)).not.toBe('')
  expect(toJSON).not.toHaveBeenCalled()
})

test.each([PAGE_RECOVERY_TTL, PAGE_RECOVERY_TTL + 1, -1, NaN, Infinity, 0.5, NOW + 1])('过期、未来或非法时间丢弃：%s', async (age) => {
  await save()
  const record = readRecord()
  record.createdAt = NOW - age
  writeRecord(record)
  expect((await loadPageRecovery('')).snapshot).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test('TTL 内最后 1ms 可恢复', async () => {
  await save()
  const record = readRecord()
  record.createdAt = NOW - PAGE_RECOVERY_TTL + 1
  writeRecord(record)
  expect((await loadPageRecovery('')).snapshot).toEqual(snapshot())
})

test.each(['', '{', 'null', '[]', '"text"', '{"version":1}', 'x'.repeat(MAX_BYTES + 1)])('畸形或过大 JSON 丢弃：%#', async (raw) => {
  window.sessionStorage.setItem(PAGE_RECOVERY_KEY, raw)
  const result = await loadPageRecovery('')
  expect(result.snapshot).toBeNull()
  expect(result.notice).toMatch(/[\u4e00-\u9fff]/)
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test.each([{ version: 2 }, { account: 'raw-token' }, { account: '' }, { authorized: true }, { createdAt: 'today' }])('外层严格版本和白名单：%j', async (change) => {
  await save()
  writeRecord({ ...readRecord(), ...change })
  expect((await loadPageRecovery('')).snapshot).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test('账号 SHA 绑定，不原文保存 token；切换账号、匿名不能读取', async () => {
  const token = 'private-access-token'
  setToken(token)
  expect(await save(snapshot(), token)).toBe('')
  const raw = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)!
  expect(raw).not.toContain(token)
  expect(readRecord().account).toMatch(/^[0-9a-f]{64}$/)
  expect((await loadPageRecovery(token)).snapshot).toEqual(snapshot())
  for (const next of ['another-account', '']) {
    setToken(token)
    await save(snapshot(), token)
    setToken(next)
    expect((await loadPageRecovery(next)).snapshot).toBeNull()
    expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
  }
  setToken(token)
  expect(await save(snapshot(), '')).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test('无 crypto 匿名仍可用；有 token 失败，不降级且保存失败保留旧记录', async () => {
  vi.stubGlobal('crypto', undefined)
  expect(await save()).toBe('')
  const old = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  setToken('account-a')
  expect(await save(snapshot(), 'account-a')).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(old)
  expect((await loadPageRecovery('account-a')).snapshot).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
  setToken('')
  expect(await save()).toBe('')
  expect((await loadPageRecovery('')).snapshot).toEqual(snapshot())
})

test.each(['getItem', 'setItem'] as const)('保存 storage %s 异常有中文错误并保留旧记录', async (method) => {
  await save()
  const old = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  const spy = vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new Error('private error details') })
  const result = await save()
  expect(result).toMatch(/[\u4e00-\u9fff]/)
  expect(result).not.toContain('private error details')
  spy.mockRestore()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(old)
})

test.each(['getItem', 'removeItem'] as const)('恢复 storage %s 异常不返回 snapshot 且中文提示', async (method) => {
  await save()
  vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new Error('storage denied') })
  const result = await loadPageRecovery('')
  expect(result.snapshot).toBeNull()
  expect(result.notice).toMatch(/[\u4e00-\u9fff]/)
  expect(result.notice).not.toContain('storage denied')
})

test('sessionStorage getter 不可用也明确提示', async () => {
  vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new Error('denied') })
  expect(await save()).toMatch(/[\u4e00-\u9fff]/)
  expect((await loadPageRecovery('')).notice).toMatch(/[\u4e00-\u9fff]/)
})

test('storage 静默不写或不消费时不能谎称成功', async () => {
  const writing = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {})
  expect(await save()).not.toBe('')
  writing.mockRestore()
  await save()
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {})
  expect((await loadPageRecovery('')).snapshot).toBeNull()
})

test('SHA 前已失效的页面和账号不能写入，也不开始摘要', async () => {
  setToken('account-a')
  const pending = await delayDigest()
  expect(await savePageRecovery(snapshot(), 'account-a', () => false)).not.toBe('')
  expect(await save(snapshot(), 'account-b')).not.toBe('')
  expect(pending.digest).not.toHaveBeenCalled()
})

test.each(['current', 'token', 'draft', 'derivation', 'project', 'workspace', 'ttl'] as const)('SHA 期间 %s 变化拒绝迟到保存并保留旧记录', async (change) => {
  setToken('account-a')
  await save(snapshot(), 'account-a')
  const old = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  const pending = await delayDigest()
  const value = snapshot()
  let current = true
  const saving = savePageRecovery(value, 'account-a', () => current)
  if (change === 'current') current = false
  if (change === 'token') setToken('account-b')
  if (change === 'draft') value.drafts.$new = '新编辑'
  if (change === 'derivation') value.derivation!.goal = '新目标'
  if (change === 'project') value.project!.name = '新项目'
  if (change === 'workspace') value.workspaceId = 'other'
  if (change === 'ttl') vi.spyOn(Date, 'now').mockReturnValue(NOW + PAGE_RECOVERY_TTL)
  pending.resolve()
  expect(await saving).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(old)
})

test.each(['replace', 'delete'] as const)('SHA 期间记录被 %s，旧保存和旧恢复都不能覆盖新状态', async (change) => {
  setToken('account-a')
  await save(snapshot(), 'account-a')
  const pending = await delayDigest()
  const saving = save(snapshot(), 'account-a')
  const loading = loadPageRecovery('account-a')
  const next = change === 'replace' ? JSON.stringify({ ...readRecord(), createdAt: NOW - 1 }) : null
  if (next === null) window.sessionStorage.removeItem(PAGE_RECOVERY_KEY)
  else window.sessionStorage.setItem(PAGE_RECOVERY_KEY, next)
  pending.resolve()
  expect(await saving).not.toBe('')
  expect((await loading).snapshot).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(next)
})

test.each(['token', 'ttl', 'future', 'digest'] as const)('恢复 SHA 期间 %s 变化不返回旧数据且丢弃', async (change) => {
  setToken('account-a')
  await save(snapshot(), 'account-a')
  const pending = await delayDigest()
  const loading = loadPageRecovery('account-a')
  if (change === 'token') setToken('account-b')
  if (change === 'ttl') vi.spyOn(Date, 'now').mockReturnValue(NOW + PAGE_RECOVERY_TTL)
  if (change === 'future') vi.spyOn(Date, 'now').mockReturnValue(NOW - 1)
  if (change === 'digest') pending.reject()
  else pending.resolve()
  expect((await loading).snapshot).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test('异常清理不能删除摘要期间写入的新记录', async () => {
  setToken('account-a')
  await save(snapshot(), 'account-a')
  const pending = await delayDigest()
  const loading = loadPageRecovery('account-a')
  const next = JSON.stringify({ ...readRecord(), createdAt: NOW - 1 })
  window.sessionStorage.setItem(PAGE_RECOVERY_KEY, next)
  pending.reject()
  expect((await loading).snapshot).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(next)
})

test('并发恢复最多返回一次 snapshot', async () => {
  setToken('account-a')
  await save(snapshot(), 'account-a')
  const pending = await delayDigest()
  const first = loadPageRecovery('account-a')
  const second = loadPageRecovery('account-a')
  pending.resolve()
  const results = await Promise.all([first, second])
  expect(results.filter((result) => result.snapshot !== null)).toHaveLength(1)
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test.each(['', 'account-a'])('首轮 effect 取消后，并行恢复仅将 snapshot 交给有效后轮：%j', async (token) => {
  setToken(token)
  await save(snapshot(), token)
  const pending = await delayDigest(token)
  let firstCurrent = true
  const first = loadPageRecovery(token, () => firstCurrent)
  firstCurrent = false
  const second = loadPageRecovery(token, () => true)
  pending.resolve()
  expect(await first).toEqual({ snapshot: null, notice: '' })
  expect((await second).snapshot).toEqual(snapshot())
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
})

test.each(['', 'account-a'])('取消恢复保留有效 record，后续默认接口仍能一次消费：%j', async (token) => {
  setToken(token)
  await save(snapshot(), token)
  const raw = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  const pending = await delayDigest(token)
  let current = true
  const loading = loadPageRecovery(token, () => current)
  current = false
  pending.resolve()
  expect(await loading).toEqual({ snapshot: null, notice: '' })
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(raw)
  expect((await loadPageRecovery(token)).snapshot).toEqual(snapshot())
  expect((await loadPageRecovery(token)).snapshot).toBeNull()
})

test.each([false, true])('取消后的摘要异常也不删除旧有效记录或更晚记录，替换=%s', async (replace) => {
  setToken('account-a')
  await save(snapshot(), 'account-a')
  const pending = await delayDigest()
  let current = true
  const loading = loadPageRecovery('account-a', () => current)
  current = false
  if (replace) {
    const next = readRecord()
    next.snapshot.drafts.$new = '更新的草稿'
    writeRecord(next)
  }
  const raw = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  pending.reject()
  expect(await loading).toEqual({ snapshot: null, notice: '' })
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(raw)
})

test('已取消的调用不访问 storage、不开始摘要', async () => {
  const pending = await delayDigest()
  const get = vi.spyOn(Storage.prototype, 'getItem')
  expect(await loadPageRecovery('account-a', () => false)).toEqual({ snapshot: null, notice: '' })
  expect(get).not.toHaveBeenCalled()
  expect(pending.digest).not.toHaveBeenCalled()
})

test('先完成的新保存阻止迟到旧保存覆盖（摘要逆序完成）', async () => {
  setToken('account-a')
  const hash = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode('account-a'))
  const first = deferred<ArrayBuffer>()
  const second = deferred<ArrayBuffer>()
  vi.stubGlobal('crypto', { subtle: { digest: vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise) } })
  const older = save(snapshot(), 'account-a')
  const latest = snapshot()
  latest.drafts.$new = '最新草稿'
  const newer = save(latest, 'account-a')
  second.resolve(hash)
  expect(await newer).toBe('')
  const stored = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  first.resolve(hash)
  expect(await older).not.toBe('')
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(stored)
  expect(readRecord().snapshot.drafts.$new).toBe('最新草稿')
})
