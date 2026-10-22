export const CREATION_RECOVERY_KEY = 'cater.pending_creation.v1'
export const CREATION_RECOVERY_TTL = 24 * 60 * 60 * 1000

export type CreationAttempt = {
  text: string
  projectId?: string
  workspaceId?: string | null
  requestId: string
  createdAt: number
}

const unavailable = '无法保存待确认创建记录，刷新后可能无法恢复。请先核对任务列表，避免重复创建。'
const invalid = '待确认创建记录已失效，请先核对任务列表，避免重复创建。'

// Authentication still uses its existing session slot; recovery never stores the token.
let memoryToken = ''
let unsavedToken: { token: string; previous: string | null } | null = null
export function readAccessToken(): string {
  const supplied = new URLSearchParams(window.location.hash.slice(1)).get('access_token')
  if (supplied) {
    memoryToken = supplied
    let previous: string | null = null
    try {
      previous = window.sessionStorage.getItem('cater.access_token')
      window.sessionStorage.setItem('cater.access_token', supplied)
      unsavedToken = null
    } catch { unsavedToken = { token: supplied, previous } }
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
    return supplied
  }
  try {
    const stored = window.sessionStorage.getItem('cater.access_token')
    if (unsavedToken && stored === unsavedToken.previous) return unsavedToken.token
    unsavedToken = null
    return stored ?? ''
  } catch { return memoryToken }
}

export async function accountBinding(token: string): Promise<string> {
  if (!token) return 'anonymous'
  // Authenticated HTTP contexts without Web Crypto cannot safely persist recovery.
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token))
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function validAttempt(value: unknown): value is CreationAttempt {
  if (!value || typeof value !== 'object') return false
  const item = value as CreationAttempt
  return typeof item.text === 'string' && Boolean(item.text.trim()) && item.text.length <= 100000
    && (item.projectId === undefined || (typeof item.projectId === 'string' && item.projectId.length > 0 && item.projectId.length <= 1000))
    && (item.workspaceId === undefined || item.workspaceId === null || (typeof item.workspaceId === 'string' && /^[A-Za-z0-9_-]{1,80}$/.test(item.workspaceId)))
    && typeof item.requestId === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(item.requestId)
    && Number.isSafeInteger(item.createdAt) && item.createdAt <= Date.now()
    && Date.now() - item.createdAt < CREATION_RECOVERY_TTL
}

export function clearCreationRecovery(requestId?: string): boolean {
  try {
    const storage = window.sessionStorage
    if (requestId) {
      const raw = storage.getItem(CREATION_RECOVERY_KEY)
      if (!raw || JSON.parse(raw)?.attempt?.requestId !== requestId) return true
    }
    storage.removeItem(CREATION_RECOVERY_KEY)
    return true
  } catch { return false }
}

export async function loadCreationRecovery(token: string): Promise<{ attempt: CreationAttempt | null; notice: string }> {
  let raw: string | null = null
  const discard = () => {
    try { if (window.sessionStorage.getItem(CREATION_RECOVERY_KEY) === raw) clearCreationRecovery() } catch { /* Storage is unavailable. */ }
  }
  try {
    raw = window.sessionStorage.getItem(CREATION_RECOVERY_KEY)
    if (!raw) return { attempt: null, notice: '' }
    const record = JSON.parse(raw)
    if (record?.version !== 1 || !validAttempt(record.attempt)
      || record.account !== await accountBinding(token) || readAccessToken() !== token) {
      discard()
      return { attempt: null, notice: invalid }
    }
    if (window.sessionStorage.getItem(CREATION_RECOVERY_KEY) !== raw || !validAttempt(record.attempt)) return { attempt: null, notice: invalid }
    const { text, projectId, workspaceId, requestId, createdAt } = record.attempt as CreationAttempt
    return { attempt: { text, projectId, ...(workspaceId !== undefined ? { workspaceId } : {}), requestId, createdAt }, notice: '' }
  } catch {
    discard()
    return { attempt: null, notice: invalid }
  }
}

export async function saveCreationRecovery(attempt: CreationAttempt, token: string, current: () => boolean): Promise<string> {
  try {
    const account = await accountBinding(token)
    if (!current()) return ''
    if (readAccessToken() !== token) return unavailable
    if (!validAttempt(attempt)) return invalid
    const { text, projectId, workspaceId, requestId, createdAt } = attempt
    window.sessionStorage.setItem(CREATION_RECOVERY_KEY, JSON.stringify({
      version: 1, account, attempt: { text, projectId, ...(workspaceId !== undefined ? { workspaceId } : {}), requestId, createdAt },
    }))
    return ''
  } catch { return unavailable }
}
