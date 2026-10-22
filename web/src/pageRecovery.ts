import { accountBinding, readAccessToken } from './creationRecovery'

export const PAGE_RECOVERY_KEY = 'cater.page_recovery.v1'
export const PAGE_RECOVERY_TTL = 60 * 60 * 1000
export const NAVIGATION_MEMORY_KEY = 'cater.navigation.v1'

export type NavigationMemory = {
  selectedId?: string | null
  notice: string
  isCurrent: () => boolean
  remember: (selectedId: string | null) => string
}

export type PageDraftSnapshot = {
  drafts: Record<string, string>
  selectedId: string | null
  questionMode: boolean
  derivation: { id: string; goal: string } | null
  project: { id: string; name: string } | null
  workspaceId?: string | null
  requests?: {
    conversation: Record<string, { text: string; requestId: string }>
    questions: Record<string, { text: string; requestId: string }>
    derivations: Record<string, { text: string; requestId: string }>
  }
}

const MAX_BYTES = 2 * 1024 * 1024
const invalid = '页面草稿恢复记录已失效，请核对当前草稿后继续。'
const unavailable = '无法访问页面草稿存储，未能保存或恢复草稿，请保留当前页面。'
const changed = '页面、账号或恢复记录已变化，未保存旧草稿，请核对后重试。'
const oversized = '页面草稿超出恢复容量，无法保存，请先保留当前草稿。'
const navigationUnavailable = '暂时无法保留对话位置；当前页面仍可使用，刷新后请从任务列表选择。'
const navigationInvalid = '上次对话位置已失效或不适用当前连接，请从任务列表选择。'
const reservedIds = new Set(['prototype', ...Object.getOwnPropertyNames(Object.prototype)])

function object(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== 'object') return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

function fields(value: Record<string, unknown>, required: string[], optional: string[] = []): boolean {
  const keys = Reflect.ownKeys(value)
  return required.every((key) => Object.hasOwn(value, key)) && keys.every((key) =>
    typeof key === 'string' && (required.includes(key) || optional.includes(key))
    && Object.getOwnPropertyDescriptor(value, key)?.enumerable === true
    && Object.hasOwn(Object.getOwnPropertyDescriptor(value, key)!, 'value'))
}

function id(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(value) && !reservedIds.has(value)
}

function draftKey(key: string): boolean {
  if (key === '$new') return true
  if (key.startsWith('$question:')) return id(key.slice('$question:'.length))
  if (key.startsWith('$derive:')) return id(key.slice('$derive:'.length))
  return id(key)
}

function copySnapshot(value: unknown): PageDraftSnapshot | null {
  if (!object(value) || !fields(value, ['drafts', 'selectedId', 'questionMode', 'derivation', 'project'], ['workspaceId', 'requests'])) return null
  if (!object(value.drafts)) return null
  const keys = Reflect.ownKeys(value.drafts)
  if (keys.length > 100) return null
  const drafts: Record<string, string> = {}
  for (const key of keys) {
    if (typeof key !== 'string' || !draftKey(key)) return null
    const descriptor = Object.getOwnPropertyDescriptor(value.drafts, key)!
    if (!Object.hasOwn(descriptor, 'value') || !descriptor.enumerable
      || typeof descriptor.value !== 'string' || descriptor.value.length > 100000) return null
    drafts[key] = descriptor.value
  }
  if ((value.selectedId !== null && !id(value.selectedId)) || typeof value.questionMode !== 'boolean') return null
  if (value.questionMode && value.selectedId === null) return null
  let derivation: PageDraftSnapshot['derivation'] = null
  if (value.derivation !== null) {
    if (!object(value.derivation) || !fields(value.derivation, ['id', 'goal'])
      || !id(value.derivation.id) || typeof value.derivation.goal !== 'string'
      || value.derivation.id !== value.selectedId || value.questionMode) return null
    derivation = { id: value.derivation.id, goal: value.derivation.goal }
  }
  let project: PageDraftSnapshot['project'] = null
  if (value.project !== null) {
    if (!object(value.project) || !fields(value.project, ['id', 'name'])
      || !id(value.project.id) || typeof value.project.name !== 'string') return null
    project = { id: value.project.id, name: value.project.name }
  }
  if (value.workspaceId !== undefined && value.workspaceId !== null
    && (typeof value.workspaceId !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/.test(value.workspaceId))) return null
  let requests: PageDraftSnapshot['requests']
  if (value.requests !== undefined) {
    if (!object(value.requests) || !fields(value.requests, ['conversation', 'questions', 'derivations'])) return null
    requests = { conversation: {}, questions: {}, derivations: {} }
    let count = 0
    for (const kind of ['conversation', 'questions', 'derivations'] as const) {
      const source = value.requests[kind]
      if (!object(source)) return null
      for (const key of Reflect.ownKeys(source)) {
        if (++count > 100 || typeof key !== 'string') return null
        const prefix = kind === 'questions' ? '$question:' : kind === 'derivations' ? '$derive:' : ''
        if (!key.startsWith(prefix) || !id(key.slice(prefix.length))) return null
        const descriptor = Object.getOwnPropertyDescriptor(source, key)!
        const attempt = descriptor.value
        if (!Object.hasOwn(descriptor, 'value') || !descriptor.enumerable || !object(attempt)
          || !fields(attempt, ['text', 'requestId']) || typeof attempt.text !== 'string' || !attempt.text.trim() || attempt.text.length > 100000
          || typeof attempt.requestId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(attempt.requestId)) return null
        requests[kind][key] = { text: attempt.text, requestId: attempt.requestId }
      }
    }
  }
  return {
    drafts, selectedId: value.selectedId, questionMode: value.questionMode, derivation, project,
    ...(value.workspaceId !== undefined ? { workspaceId: value.workspaceId as string | null } : {}),
    ...(requests ? { requests } : {}),
  }
}

function fits(raw: string): boolean {
  return raw.length <= MAX_BYTES && new TextEncoder().encode(raw).byteLength <= MAX_BYTES
}

function fresh(createdAt: unknown): createdAt is number {
  const now = Date.now()
  return typeof createdAt === 'number' && Number.isSafeInteger(createdAt)
    && createdAt >= 0 && createdAt <= now && now - createdAt < PAGE_RECOVERY_TTL
}

export async function openNavigationMemory(token: string, current: () => boolean = () => true): Promise<NavigationMemory> {
  const disabled = (): NavigationMemory => ({ selectedId: null, notice: navigationUnavailable, isCurrent: () => false, remember: () => navigationUnavailable })
  try {
    if (!current()) return disabled()
    const storage = window.sessionStorage
    const raw = storage.getItem(NAVIGATION_MEMORY_KEY)
    const account = await accountBinding(token)
    if (!current() || window.sessionStorage !== storage || readAccessToken() !== token
      || storage.getItem(NAVIGATION_MEMORY_KEY) !== raw) return disabled()
    let expected = raw
    const isCurrent = () => {
      try {
        return current() && window.sessionStorage === storage && readAccessToken() === token
          && storage.getItem(NAVIGATION_MEMORY_KEY) === expected
      } catch { return false }
    }
    // Binding is prepared once; a selection write completes before the next user event.
    const remember = (selectedId: string | null): string => {
      if (!current()) return ''
      try {
        if (!isCurrent() || selectedId !== null && !id(selectedId)) return navigationUnavailable
        const value = JSON.stringify({ version: 1, account, selectedId })
        storage.setItem(NAVIGATION_MEMORY_KEY, value)
        if (storage.getItem(NAVIGATION_MEMORY_KEY) !== value) return navigationUnavailable
        expected = value
        return ''
      } catch { return navigationUnavailable }
    }
    if (raw === null) return { notice: '', isCurrent, remember }
    let record: unknown
    try { record = raw.length <= 512 ? JSON.parse(raw) : null } catch { record = null }
    if (!object(record) || !fields(record, ['version', 'account', 'selectedId']) || record.version !== 1
      || record.account !== account || record.selectedId !== null && !id(record.selectedId)) {
      if (isCurrent()) {
        storage.removeItem(NAVIGATION_MEMORY_KEY)
        expected = null
      }
      return { selectedId: null, notice: navigationInvalid, isCurrent, remember }
    }
    return { selectedId: record.selectedId as string | null, notice: '', isCurrent, remember }
  } catch { return disabled() }
}

export async function loadPageRecovery(token: string, current: () => boolean = () => true): Promise<{ snapshot: PageDraftSnapshot | null; notice: string }> {
  let storage: Storage | undefined
  let raw: string | null = null
  const discard = () => {
    if (current() && storage && raw !== null && window.sessionStorage === storage && storage.getItem(PAGE_RECOVERY_KEY) === raw) {
      storage.removeItem(PAGE_RECOVERY_KEY)
    }
  }
  try {
    if (!current()) return { snapshot: null, notice: '' }
    storage = window.sessionStorage
    raw = storage.getItem(PAGE_RECOVERY_KEY)
    if (raw === null) return { snapshot: null, notice: '' }
    if (!fits(raw) || readAccessToken() !== token) {
      discard()
      return { snapshot: null, notice: invalid }
    }
    const record: unknown = JSON.parse(raw)
    if (!object(record) || !fields(record, ['version', 'account', 'createdAt', 'snapshot'])
      || record.version !== 1 || typeof record.account !== 'string'
      || !/^(anonymous|[0-9a-f]{64})$/.test(record.account) || !fresh(record.createdAt)) {
      discard()
      return { snapshot: null, notice: invalid }
    }
    const snapshot = copySnapshot(record.snapshot)
    if (!snapshot) {
      discard()
      return { snapshot: null, notice: invalid }
    }
    const account = await accountBinding(token)
    // A cancelled effect leaves the record for the next active recovery attempt.
    if (!current()) return { snapshot: null, notice: '' }
    if (window.sessionStorage !== storage || storage.getItem(PAGE_RECOVERY_KEY) !== raw) {
      return { snapshot: null, notice: changed }
    }
    if (readAccessToken() !== token || record.account !== account || !fresh(record.createdAt)) {
      discard()
      return { snapshot: null, notice: invalid }
    }
    // Only the verified, unchanged record is consumed; recovery never submits anything.
    storage.removeItem(PAGE_RECOVERY_KEY)
    if (storage.getItem(PAGE_RECOVERY_KEY) === raw) return { snapshot: null, notice: unavailable }
    return { snapshot, notice: '已恢复未发送的页面草稿，请核对后手动发送。' }
  } catch {
    try {
      if (!current()) return { snapshot: null, notice: '' }
      discard()
    } catch { /* Storage can also reject cleanup. */ }
    return { snapshot: null, notice: unavailable }
  }
}

export async function savePageRecovery(snapshot: PageDraftSnapshot, token: string, current: () => boolean): Promise<string> {
  try {
    const storage = window.sessionStorage
    const previous = storage.getItem(PAGE_RECOVERY_KEY)
    if (!current() || readAccessToken() !== token) return changed
    const copied = copySnapshot(snapshot)
    if (!copied) return invalid
    const serialized = JSON.stringify(copied)
    if (!fits(serialized)) return oversized
    const createdAt = Date.now()
    const account = await accountBinding(token)
    // Compare both the page revision and the caller's data after asynchronous hashing.
    if (window.sessionStorage !== storage || storage.getItem(PAGE_RECOVERY_KEY) !== previous
      || !current() || readAccessToken() !== token || !fresh(createdAt)) return changed
    const latest = copySnapshot(snapshot)
    if (!latest || JSON.stringify(latest) !== serialized) return changed
    const raw = JSON.stringify({ version: 1, account, createdAt, snapshot: copied })
    if (!fits(raw)) return oversized
    storage.setItem(PAGE_RECOVERY_KEY, raw)
    return storage.getItem(PAGE_RECOVERY_KEY) === raw ? '' : unavailable
  } catch { return unavailable }
}
