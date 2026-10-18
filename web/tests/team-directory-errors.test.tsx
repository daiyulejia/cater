import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'

const json = (body: unknown) => new Response(JSON.stringify(body))
const profile = { leader: { id: 'store_manager', name: '店长', role: 'manager' }, cabinet: [],
  workers: [{ id: 'dev', name: '米寇', role: 'dev' }] }

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

function install(directory: (init: RequestInit) => Promise<Response>, full = async () => json(profile)) {
  const fetch = vi.fn((url: string, init: RequestInit = {}) => {
    if (url === '/api/organization') return directory(init)
    if (url === '/api/organization/profile') return full()
    if (url === '/api/task-summaries') return Promise.resolve(json({ tasks: [] }))
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

test('备用名单迟到失败不污染已就绪伙伴资料或聊天错误', async () => {
  const old = deferred<Response>()
  install(() => old.promise)
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: '今天在店里' }))
  const dialog = await screen.findByRole('dialog', { name: '今天在店里' })
  await within(dialog).findByRole('button', { name: '点名 米寇' })
  await act(async () => old.resolve(new Response('暂时无法读取备用名单', { status: 503 })))
  expect(screen.queryByRole('alert')).toBeNull()
  fireEvent.click(within(dialog).getByRole('button', { name: '关闭伙伴页' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByRole('alert')).toBeNull()
})

test('两个名单均失败只在伙伴页提示，刷新成功后消除旧故障', async () => {
  let recovered = false
  install(async () => new Response('备用名单未就绪', { status: 503 }), async () => {
    if (!recovered) throw new Error('offline')
    return json(profile)
  })
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: '今天在店里' }))
  const dialog = await screen.findByRole('dialog', { name: '今天在店里' })
  await within(dialog).findByRole('alert')
  expect(screen.getAllByRole('alert')).toHaveLength(1)
  recovered = true
  const refresh = within(dialog).getByRole('button', { name: '刷新伙伴状态' })
  await waitFor(() => expect((refresh as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(refresh)
  await within(dialog).findByRole('button', { name: '点名 米寇' })
  expect(screen.queryByRole('alert')).toBeNull()
  fireEvent.click(within(dialog).getByRole('button', { name: '关闭伙伴页' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(screen.queryByRole('alert')).toBeNull()
})

test('完整资料失败仍保留备用伙伴名单，错误不冒充聊天提交失败', async () => {
  const fetch = install(async () => json(profile.workers), async () => { throw new Error('offline') })
  render(<App />)
  fireEvent.click(screen.getByRole('button', { name: '今天在店里' }))
  const dialog = await screen.findByRole('dialog', { name: '今天在店里' })
  await within(dialog).findByRole('button', { name: '点名 米寇' })
  await within(dialog).findByRole('alert')
  expect(screen.getAllByRole('alert')).toHaveLength(1)
  expect(fetch.mock.calls.every(([, init]) => !init.method || init.method === 'GET')).toBe(true)
})

test('离开页面中止备用名单请求，不等待旧响应改写已卸载页面', async () => {
  const old = deferred<Response>()
  let signal: AbortSignal | undefined
  install(init => { signal = init.signal as AbortSignal; return old.promise })
  const view = render(<App />)
  await waitFor(() => expect(signal).toBeDefined())
  expect(signal!.aborted).toBe(false)
  view.unmount()
  expect(signal!.aborted).toBe(true)
  old.resolve(json(profile.workers))
})
