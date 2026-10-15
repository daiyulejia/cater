import { beforeEach, expect, test, vi } from 'vitest'
import { READ_TIMEOUT_MS, SUBMIT_TIMEOUT_MS, readJsonResponse, request, RequestTimeoutError, SubmissionUnknownError } from '../src/request'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
const json = () => new Response('{"ok":true}')
const read = (path: string, init: RequestInit = {}) => request(path, init, readJsonResponse)

beforeEach(() => { vi.useFakeTimers() })

test.each(['fetch', 'json', 'text', 'blob'])('deadline includes a stalled %s even when abort is ignored', async (stage) => {
  const pending = deferred<Response>()
  const response = stage === 'text' ? new Response('error', { status: 503 }) : json()
  if (stage !== 'fetch') vi.spyOn(response, stage as 'json').mockImplementation(() => new Promise(() => {}))
  const fetchMock = vi.fn((_path: string, _init: RequestInit) => pending.promise)
  vi.stubGlobal('fetch', fetchMock)
  const result = request('/test', {}, stage === 'blob' ? (value) => value.blob() : readJsonResponse).catch((error) => error)
  await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS - 1000)
  if (stage !== 'fetch') pending.resolve(response)
  await vi.advanceTimersByTimeAsync(1000)
  expect(await result).toBeInstanceOf(RequestTimeoutError)
  expect(fetchMock.mock.calls).toHaveLength(1)
  expect(fetchMock.mock.calls[0][1].signal?.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
})

test('timeout aborts the transport, leaves the caller signal intact and cannot cancel a later request', async () => {
  const caller = new AbortController()
  const pending = deferred<Response>()
  const fetchMock = vi.fn((_path: string, _init: RequestInit) => pending.promise)
  vi.stubGlobal('fetch', fetchMock)
  const result = read('/slow', { signal: caller.signal }).catch((error) => error)
  await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS)
  expect(await result).toBeInstanceOf(RequestTimeoutError)
  expect(fetchMock.mock.calls[0][1].signal?.aborted).toBe(true)
  expect(caller.signal.aborted).toBe(false)
  fetchMock.mockImplementation(async () => json())
  await expect(read('/retry', { signal: caller.signal })).resolves.toEqual({ ok: true })
  pending.resolve(json())
  await vi.advanceTimersByTimeAsync(READ_TIMEOUT_MS)
  expect(fetchMock.mock.calls[1][1].signal?.aborted).toBe(false)
  expect(vi.getTimerCount()).toBe(0)
})

test('caller cancellation during body reading settles promptly and does not affect another request', async () => {
  const first = new AbortController()
  const second = new AbortController()
  const body = deferred<unknown>()
  const response = json()
  vi.spyOn(response, 'json').mockReturnValue(body.promise)
  const fetchMock = vi.fn(async () => response)
  vi.stubGlobal('fetch', fetchMock)
  const old = read('/old', { signal: first.signal }).catch((error) => error)
  await vi.advanceTimersByTimeAsync(0)
  const next = read('/next', { signal: second.signal })
  first.abort()
  expect((await old).name).toBe('AbortError')
  expect(second.signal.aborted).toBe(false)
  body.resolve({ ok: true })
  await expect(next).resolves.toEqual({ ok: true })
  expect(vi.getTimerCount()).toBe(0)
})

test('already cancelled callers do not fetch; successful and rejected requests remove abort listeners and deadlines', async () => {
  const caller = new AbortController()
  const added = vi.spyOn(caller.signal, 'addEventListener')
  const removed = vi.spyOn(caller.signal, 'removeEventListener')
  const fetchMock = vi.fn(async () => json())
  vi.stubGlobal('fetch', fetchMock)
  await read('/ok', { signal: caller.signal })
  fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
  await expect(read('/fail', { signal: caller.signal })).rejects.toThrow('Failed to fetch')
  caller.abort()
  await expect(read('/cancelled', { signal: caller.signal })).rejects.toHaveProperty('name', 'AbortError')
  expect(fetchMock).toHaveBeenCalledTimes(2)
  expect(removed.mock.calls.map((call) => call.slice(0, 2))).toEqual(added.mock.calls.map((call) => call.slice(0, 2)))
  expect(vi.getTimerCount()).toBe(0)
})

test.each(['fetch', 'body', 'network', 'invalid-json'])('write %s failure is an unknown submission, with no automatic replay', async (stage) => {
  const fetchMock = vi.fn(async () => {
    if (stage === 'fetch') return new Promise<Response>(() => {})
    if (stage === 'network') throw new TypeError('Failed to fetch')
    if (stage === 'invalid-json') return new Response('truncated {')
    const response = json()
    vi.spyOn(response, 'json').mockImplementation(() => new Promise(() => {}))
    return response
  })
  vi.stubGlobal('fetch', fetchMock)
  const result = read('/api/tasks', { method: 'POST', body: '{"request_id":"same-key"}' }).catch((error) => error)
  await vi.advanceTimersByTimeAsync(SUBMIT_TIMEOUT_MS)
  const error = await result
  expect(error).toBeInstanceOf(SubmissionUnknownError)
  expect(error.message).toContain('提交结果未知')
  expect(error.message).toContain('没有自动重发')
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test.each([400, 401, 403, 409, 422, 429])('HTTP %i remains a known rejection, including its server explanation', async (status) => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('服务端明确拒绝', { status })))
  const error = await read('/api/tasks', { method: 'POST' }).catch((error) => error)
  expect(error).not.toBeInstanceOf(SubmissionUnknownError)
  expect(error.message).toBe('服务端明确拒绝')
  expect(vi.getTimerCount()).toBe(0)
})

test.each([500, 502, 503, 504])('HTTP %i after a write is unknown and never automatically repeated', async (status) => {
  const fetchMock = vi.fn(async () => new Response('持久化后发生异常', { status }))
  vi.stubGlobal('fetch', fetchMock)
  const error = await read('/api/tasks', { method: 'POST' }).catch((error) => error)
  expect(error).toBeInstanceOf(SubmissionUnknownError)
  expect(error.message).toContain(`HTTP ${status}`)
  expect(error.message).toContain('持久化后发生异常')
  expect(error.message).toContain('提交结果未知')
  await vi.advanceTimersByTimeAsync(SUBMIT_TIMEOUT_MS)
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('HTTP 503 for a read remains a read failure rather than an unknown submission', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('读取失败', { status: 503 })))
  const error = await read('/api/task-summaries').catch((error) => error)
  expect(error).not.toBeInstanceOf(SubmissionUnknownError)
  expect(error.message).toBe('读取失败')
})
