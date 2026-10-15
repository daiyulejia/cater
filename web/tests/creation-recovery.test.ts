import { webcrypto } from 'node:crypto'
import { beforeEach, expect, test, vi } from 'vitest'
import {
  clearCreationRecovery, CREATION_RECOVERY_KEY, CREATION_RECOVERY_TTL,
  loadCreationRecovery, readAccessToken, saveCreationRecovery,
} from '../src/creationRecovery'

const attempt = () => ({ text: '  原始草稿  ', projectId: 'p1', requestId: '77b0e8fd-5112-49db-813f-33ddd70ac047', createdAt: Date.now() })
beforeEach(() => {
  window.sessionStorage.clear()
  window.localStorage.clear()
  vi.stubGlobal('crypto', webcrypto)
})

test('账户使用 SHA-256 绑定，记录不含密钥；同账户可恢复，切换账户即丢弃', async () => {
  const token = 'private-access-token'
  window.sessionStorage.setItem('cater.access_token', token)
  const original = attempt()
  expect(await saveCreationRecovery(original, token, () => true)).toBe('')
  const raw = window.sessionStorage.getItem(CREATION_RECOVERY_KEY)!
  expect(raw).not.toContain(token)
  expect(raw).not.toContain('access_token')
  expect(JSON.parse(raw).account).toMatch(/^[0-9a-f]{64}$/)
  expect((await loadCreationRecovery(token)).attempt).toEqual(original)
  window.sessionStorage.setItem('cater.access_token', 'another-account')
  expect((await loadCreationRecovery('another-account')).attempt).toBeNull()
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  expect(window.localStorage.length).toBe(0)
})

test('不同标签页存储不共享恢复记录', async () => {
  await saveCreationRecovery(attempt(), '', () => true)
  const source = window.sessionStorage
  const tab = new Map<string, string>()
  vi.spyOn(window, 'sessionStorage', 'get').mockReturnValue({
    getItem: (key) => tab.get(key) ?? null, setItem: (key, value) => { tab.set(key, value) },
    removeItem: (key) => { tab.delete(key) }, clear: () => tab.clear(), key: () => null, length: 0,
  })
  expect((await loadCreationRecovery('')).attempt).toBeNull()
  expect(source.getItem(CREATION_RECOVERY_KEY)).not.toBeNull()
})

test.each([
  { text: '' }, { projectId: 5 }, { requestId: 'invalid' }, { createdAt: 0 }, { createdAt: Date.now() + CREATION_RECOVERY_TTL },
])('损坏或过期字段不进入恢复草稿：%j', async (change) => {
  await saveCreationRecovery(attempt(), '', () => true)
  const record = JSON.parse(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)!)
  Object.assign(record.attempt, change)
  window.sessionStorage.setItem(CREATION_RECOVERY_KEY, JSON.stringify(record))
  expect((await loadCreationRecovery('')).attempt).toBeNull()
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
})

test('无法安全计算账户摘要时不落盘，不把 token 作为替代标识', async () => {
  window.sessionStorage.setItem('cater.access_token', 'secret')
  vi.stubGlobal('crypto', { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) })
  expect(await saveCreationRecovery(attempt(), 'secret', () => true)).toContain('无法保存')
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
})

test('异步摘要完成前草稿或账户变化不能写入旧记录', async () => {
  const original = attempt()
  window.sessionStorage.setItem('cater.access_token', 'account-a')
  let current = true
  const saving = saveCreationRecovery(original, 'account-a', () => current)
  current = false
  await saving
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
  const switched = saveCreationRecovery(original, 'account-a', () => true)
  window.sessionStorage.setItem('cater.access_token', 'account-b')
  await switched
  expect(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)).toBeNull()
})

test('旧请求迟到清理不能删除新请求记录，恢复期间替换记录也不能读出旧草稿', async () => {
  const original = attempt()
  window.sessionStorage.setItem('cater.access_token', 'account')
  await saveCreationRecovery(original, 'account', () => true)
  const reading = loadCreationRecovery('account')
  const record = JSON.parse(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)!)
  record.attempt.requestId = '87b0e8fd-5112-49db-813f-33ddd70ac047'
  window.sessionStorage.setItem(CREATION_RECOVERY_KEY, JSON.stringify(record))
  expect((await reading).attempt).toBeNull()
  clearCreationRecovery(original.requestId)
  expect(JSON.parse(window.sessionStorage.getItem(CREATION_RECOVERY_KEY)!).attempt.requestId).toBe(record.attempt.requestId)
})

test('访问令牌存储写入失败时保留页面内认证，仍清除 URL 中的令牌', () => {
  window.history.replaceState(null, '', '#access_token=page-only-secret')
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  expect(readAccessToken()).toBe('page-only-secret')
  expect(readAccessToken()).toBe('page-only-secret')
  expect(window.location.hash).toBe('')
})
