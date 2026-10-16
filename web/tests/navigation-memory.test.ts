import { webcrypto } from 'node:crypto'
import { beforeEach, expect, test, vi } from 'vitest'
import { accountBinding, readAccessToken } from '../src/creationRecovery'
import { NAVIGATION_MEMORY_KEY, openNavigationMemory } from '../src/pageRecovery'

const record = (selectedId: unknown, account = 'anonymous', extra = {}) => JSON.stringify({ version: 1, account, selectedId, ...extra })
const saved = () => window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)
const token = (value: string) => window.sessionStorage.setItem('cater.access_token', value)

beforeEach(() => {
  window.sessionStorage.clear()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
  readAccessToken()
  vi.stubGlobal('crypto', webcrypto)
})

test('位置写入同步完成，只保存身份绑定和会话ID，不保存正文、标题或凭据', async () => {
  token('private-access-credential')
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const xhr = vi.spyOn(XMLHttpRequest.prototype, 'open')
  const memory = await openNavigationMemory('private-access-credential')
  expect(memory.selectedId).toBeUndefined()
  expect(memory.remember('task_alpha')).toBe('')
  const raw = saved()!
  expect(JSON.parse(raw)).toEqual({ version: 1, account: await accountBinding('private-access-credential'), selectedId: 'task_alpha' })
  expect(raw).not.toContain('private-access-credential')
  expect(raw.length).toBeLessThan(256)
  expect((await openNavigationMemory('private-access-credential')).selectedId).toBe('task_alpha')
  expect(memory.remember(null)).toBe('')
  expect(JSON.parse(saved()!).selectedId).toBeNull()
  expect((await openNavigationMemory('private-access-credential')).selectedId).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
  expect(xhr).not.toHaveBeenCalled()
  expect(window.localStorage.length).toBe(0)
})

test.each(['', '../task', 'constructor', '__proto__', 'prototype', 'a'.repeat(81), 7, false])('无效位置%j不会成为可恢复选择，也不回显原记录', async selectedId => {
  window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY, record(selectedId))
  const memory = await openNavigationMemory('')
  expect(memory.selectedId).toBeNull()
  expect(memory.notice).toContain('已失效')
  expect(saved()).toBeNull()
})

test.each(['{', 'x'.repeat(513), record('task_a', 'anonymous', { version: 2 }),
  record('task_a', 'anonymous', { text: 'PRIVATE_BODY' })])('损坏、超长或未知字段记录被清除，不能意外执行：%s', async raw => {
  window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY, raw)
  const memory = await openNavigationMemory('')
  expect(memory.selectedId).toBeNull()
  expect(memory.notice).not.toContain('PRIVATE_BODY')
  expect(saved()).toBeNull()
})

test('跨身份不恢复位置，也不改写访问凭据', async () => {
  token('account-a')
  const first = await openNavigationMemory('account-a')
  first.remember('task_alpha')
  const original = saved()
  token('account-b')
  expect(first.remember('task_beta')).not.toBe('')
  expect(saved()).toBe(original)
  const second = await openNavigationMemory('account-b')
  expect(second.selectedId).toBeNull()
  expect(second.notice).toContain('不适用')
  expect(readAccessToken()).toBe('account-b')
  expect(saved()).toBeNull()
})

test('页面结束后旧句柄不覆盖新记录', async () => {
  let current = true
  const memory = await openNavigationMemory('', () => current)
  memory.remember('task_alpha')
  current = false
  window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY, record('task_beta'))
  expect(memory.remember('task_old')).toBe('')
  expect(JSON.parse(saved()!).selectedId).toBe('task_beta')
})

test('句柄不会恢复或覆盖初始化后由其他操作更新的位置', async () => {
  window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY, record('task_alpha'))
  const memory = await openNavigationMemory('')
  expect(memory.isCurrent()).toBe(true)
  expect(memory.remember('task_beta')).toBe('')
  expect(memory.isCurrent()).toBe(true)
  window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY, record('task_newer'))
  expect(memory.isCurrent()).toBe(false)
  expect(memory.remember('task_old')).not.toBe('')
  expect(JSON.parse(saved()!).selectedId).toBe('task_newer')
})

test('存储不可读、拒绝写入或静默失败均明确降级，不阻塞调用者', async () => {
  const memory = await openNavigationMemory('')
  const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  expect(memory.remember('task_a')).toContain('当前页面仍可使用')
  set.mockImplementation(() => {})
  expect(memory.remember('task_a')).not.toBe('')
  set.mockRestore()
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
  const failed = await openNavigationMemory('')
  expect(failed.selectedId).toBeNull()
  expect(failed.remember('task_b')).not.toBe('')
})

test('身份哈希期间的新记录和页面取消不能被旧初始化覆盖', async () => {
  token('account-a')
  const digest = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode('account-a'))
  let resolve!: (value: ArrayBuffer) => void
  vi.stubGlobal('crypto', { subtle: { digest: () => new Promise<ArrayBuffer>(done => { resolve = done }) } })
  const pending = openNavigationMemory('account-a')
  const newer = record('task_new', 'other-binding')
  window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY, newer)
  resolve(digest)
  const memory = await pending
  expect(saved()).toBe(newer)
  expect(memory.remember('task_old')).not.toBe('')
  expect(saved()).toBe(newer)
  expect((await openNavigationMemory('account-a', () => false)).selectedId).toBeNull()
  expect(saved()).toBe(newer)
})

test('认证环境缺少安全哈希时不保存裸凭据或不受绑定的位置', async () => {
  token('account-a')
  vi.stubGlobal('crypto', {})
  const memory = await openNavigationMemory('account-a')
  expect(memory.selectedId).toBeNull()
  expect(memory.notice).toBeTruthy()
  expect(memory.remember('task_a')).not.toBe('')
  expect(saved()).toBeNull()
})
