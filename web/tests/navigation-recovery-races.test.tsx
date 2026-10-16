import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { webcrypto } from 'node:crypto'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import { accountBinding, readAccessToken } from '../src/creationRecovery'
import { NAVIGATION_MEMORY_KEY, PAGE_RECOVERY_KEY, openNavigationMemory, savePageRecovery } from '../src/pageRecovery'

const recoveryRace = vi.hoisted(() => ({
  navigationWait: undefined as Promise<void> | undefined,
  navigationEntered: undefined as (() => void) | undefined,
  navigationLoads: [] as Promise<unknown>[],
  pageLoads: [] as Promise<unknown>[],
}))

vi.mock('../src/pageRecovery', async importOriginal => {
  const original = await importOriginal<typeof import('../src/pageRecovery')>()
  return {
    ...original,
    openNavigationMemory: (...args: Parameters<typeof original.openNavigationMemory>) => {
      const wait = recoveryRace.navigationWait
      const pending = (async () => {
        if (wait) {
          recoveryRace.navigationEntered?.()
          await wait
        }
        return original.openNavigationMemory(...args)
      })()
      recoveryRace.navigationLoads.push(pending)
      return pending
    },
    loadPageRecovery: (...args: Parameters<typeof original.loadPageRecovery>) => {
      const pending = original.loadPageRecovery(...args)
      recoveryRace.pageLoads.push(pending)
      return pending
    },
  }
})

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

const leader = { id: 'store_manager', name: '店长' }
const control = { state: 'paused', seq: 4, reason: '保持暂停' }
const summary = (id: string) => ({
  id, goal: id, status: 'running', status_label: '已暂停', conversation_count: 1, latest_seq: 4, version: 4,
  execution_control: control,
  team: { enabled: true, objective: id, status: 'running', pending_count: 2, revision: 1,
    runs: [], assignments: [], checkpoint: '' },
})
const detail = (id: string) => ({
  task: { ...summary(id), acceptance: [], messages: [], assignments: [], deliverables: [] },
  summary: summary(id), team: summary(id).team, execution_control: control,
  activity: { active: false, state: 'completed' }, review: {},
  events: [
    { seq: 1, type: 'task.created', actor: { id: 'human', kind: 'human' }, payload: { goal: id } },
    { seq: 4, type: 'task.execution_controlled', actor: { id: 'human', kind: 'human' },
      payload: { action: 'pause', state: 'paused' } },
  ],
})
const json = (value: unknown) => new Response(JSON.stringify(value))
const summaries = () => json({ tasks: [summary('task_alpha'), summary('task_beta')] })
const rawNavigation = () => window.sessionStorage.getItem(NAVIGATION_MEMORY_KEY)
const remember = (selectedId: string | null, account = 'anonymous') => {
  window.sessionStorage.setItem(NAVIGATION_MEMORY_KEY, JSON.stringify({ version: 1, account, selectedId }))
}
const selected = () => screen.getByRole('navigation', { name: '任务列表' }).querySelector('[aria-current="page"]')
const newDraft = () => screen.getByRole('textbox', { name: '发起任务对话' }) as HTMLTextAreaElement
let summaryResponse: Promise<Response> | undefined
let fetchMock: ReturnType<typeof vi.fn>
const mutations = () => fetchMock.mock.calls.filter(([, init]) => init?.method && init.method !== 'GET')

beforeEach(() => {
  window.sessionStorage.clear()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
  readAccessToken()
  vi.stubGlobal('crypto', webcrypto)
  recoveryRace.navigationWait = undefined
  recoveryRace.navigationEntered = undefined
  recoveryRace.navigationLoads = []
  recoveryRace.pageLoads = []
  summaryResponse = undefined
  fetchMock = vi.fn((url: string, init: RequestInit = {}) => {
    if (init.method && init.method !== 'GET') throw new Error('Recovery must not mutate server state')
    if (url === '/api/organization') return json([leader])
    if (url === '/api/organization/profile') return json({ leader, workers: [], cabinet: [] })
    if (url === '/api/task-summaries') return summaryResponse ?? summaries()
    if (/^\/api\/tasks\/task_(alpha|beta)$/.test(url)) return json(detail(url.split('/')[3]))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
})

const privateDraft = 'PRIVATE_ACCOUNT_A_UNSENT_DRAFT'
const requestId = '74f1b5c6-e10c-40c3-9464-4f1f71f3a1ca'

async function prepareSnapshot() {
  window.sessionStorage.setItem('cater.access_token', 'account-a')
  remember('task_beta', await accountBinding('account-a'))
  expect(await savePageRecovery({
    drafts: { $new: privateDraft, task_alpha: 'UNKNOWN_SUBMISSION' }, selectedId: null,
    questionMode: false, derivation: null, project: null,
    requests: { conversation: { task_alpha: { text: 'UNKNOWN_SUBMISSION', requestId } }, questions: {}, derivations: {} },
  }, 'account-a', () => true)).toBe('')
  const raw = window.sessionStorage.getItem(PAGE_RECOVERY_KEY)
  expect(raw).not.toBeNull()
  return raw
}

async function renderWithNavigationPending() {
  const entered = deferred<void>()
  const release = deferred<void>()
  recoveryRace.navigationEntered = () => entered.resolve()
  recoveryRace.navigationWait = release.promise
  const app = render(<App />)
  await act(async () => {
    await entered.promise
    // Drain any concurrent snapshot read; serial recovery may not have started one.
    await Promise.all(recoveryRace.pageLoads)
  })
  return {
    app,
    release: async () => {
      await act(async () => {
        release.resolve()
        await Promise.all(recoveryRace.navigationLoads)
        await Promise.all(recoveryRace.pageLoads)
      })
      recoveryRace.navigationWait = undefined
    },
  }
}

test('导航绑定等待期间取消，不吞掉未应用的显式快照及未知提交编号', async () => {
  const raw = await prepareSnapshot()
  const pending = await renderWithNavigationPending()
  expect(newDraft().value).toBe('')
  pending.app.unmount()
  await pending.release()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(raw)
  expect(JSON.parse(raw!).snapshot.requests.conversation.task_alpha.requestId).toBe(requestId)

  render(<App />)
  await waitFor(() => expect(newDraft().value).toBe(privateDraft))
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBeNull()
  expect(selected()).toBeNull()
  expect(mutations()).toHaveLength(0)
})

test('导航绑定等待期间换账号，不迟到应用旧账号的显式草稿', async () => {
  const raw = await prepareSnapshot()
  const navigation = rawNavigation()
  const pending = await renderWithNavigationPending()
  window.sessionStorage.setItem('cater.access_token', 'account-b')
  await pending.release()
  expect(newDraft().value).toBe('')
  expect(selected()).toBeNull()
  expect(window.sessionStorage.getItem(PAGE_RECOVERY_KEY)).toBe(raw)
  expect(rawNavigation()).toBe(navigation)
  expect(readAccessToken()).toBe('account-b')
  expect(mutations()).toHaveLength(0)
})

test('句柄已准备但首份列表等待时，外部新位置不被迟到选择覆盖', async () => {
  remember('task_alpha')
  const list = deferred<Response>()
  summaryResponse = list.promise
  render(<App />)
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === '/api/task-summaries')).toBe(true))
  remember('task_beta')
  const newer = rawNavigation()
  await act(async () => list.resolve(summaries()))
  await screen.findByRole('button', { name: /^task_alpha/ })
  expect(selected()).toBeNull()
  expect(rawNavigation()).toBe(newer)
  expect(fetchMock.mock.calls.some(([url]) => url === '/api/tasks/task_alpha')).toBe(false)
  expect(mutations()).toHaveLength(0)
})

test('位置被外部替换后，旧句柄的校验和写入均拒绝覆盖新记录', async () => {
  remember('task_alpha')
  const memory = await openNavigationMemory('')
  expect(memory.isCurrent()).toBe(true)
  expect(memory.remember(null)).toBe('')
  expect(memory.isCurrent()).toBe(true)
  remember('task_beta')
  const newer = rawNavigation()
  expect(memory.isCurrent()).toBe(false)
  expect(memory.remember('task_alpha')).not.toBe('')
  expect(rawNavigation()).toBe(newer)
  expect(mutations()).toHaveLength(0)
})

test('首份列表等待时直接输入，不点击新建也同步保存null，刷新不回旧群', async () => {
  remember('task_alpha')
  const list = deferred<Response>()
  summaryResponse = list.promise
  const app = render(<App />)
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => url === '/api/task-summaries')).toBe(true))
  fireEvent.change(newDraft(), { target: { value: 'PRIVATE_DIRECT_INPUT' } })
  expect(JSON.parse(rawNavigation()!).selectedId).toBeNull()
  expect(rawNavigation()).not.toContain('PRIVATE_DIRECT_INPUT')
  await act(async () => list.resolve(summaries()))
  await screen.findByRole('button', { name: /^task_alpha/ })
  expect(selected()).toBeNull()
  expect(newDraft().value).toBe('PRIVATE_DIRECT_INPUT')
  expect(JSON.parse(rawNavigation()!).selectedId).toBeNull()

  app.unmount()
  summaryResponse = undefined
  render(<App />)
  await screen.findByRole('button', { name: /^task_alpha/ })
  expect(selected()).toBeNull()
  expect(newDraft().value).toBe('')
  expect(JSON.parse(rawNavigation()!).selectedId).toBeNull()
  expect(mutations()).toHaveLength(0)
})
