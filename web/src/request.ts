export const READ_TIMEOUT_MS = 8000
export const SUBMIT_TIMEOUT_MS = 30000

export class RequestTimeoutError extends Error {
  constructor() { super('请求超时，请稍后重试。'); this.name = 'RequestTimeoutError' }
}

export class SubmissionUnknownError extends Error {
  constructor(cause: unknown) {
    const reason = cause instanceof RequestTimeoutError ? '请求超时'
      : cause instanceof HttpResponseError ? `服务端返回 HTTP ${cause.status}（${cause.message}）` : '连接中断或响应不完整'
    super(`${reason}，提交结果未知。请核对最新记录；本次没有自动重发。`, { cause })
    this.name = 'SubmissionUnknownError'
  }
}

class HttpResponseError extends Error {
  readonly status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}

export async function readJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) throw new HttpResponseError(await response.text() || `请求失败（HTTP ${response.status}）。`, response.status)
  return response.json() as Promise<T>
}

// Keep the deadline and cancellation active until the entire response body is read.
export async function request<T>(path: string, init: RequestInit, read: (response: Response) => Promise<T>): Promise<T> {
  const controller = new AbortController()
  const writing = !['GET', 'HEAD'].includes((init.method ?? 'GET').toUpperCase())
  const cancel = () => controller.abort(init.signal?.reason)
  let rejectAbort!: () => void
  const aborted = new Promise<never>((_, reject) => {
    rejectAbort = () => reject(controller.signal.reason)
    controller.signal.addEventListener('abort', rejectAbort, { once: true })
  })
  init.signal?.addEventListener('abort', cancel, { once: true })
  if (init.signal?.aborted) cancel()
  const timer = window.setTimeout(() => controller.abort(new RequestTimeoutError()), writing ? SUBMIT_TIMEOUT_MS : READ_TIMEOUT_MS)
  try {
    const receive = async () => {
      controller.signal.throwIfAborted()
      const response = await fetch(path, { ...init, signal: controller.signal })
      controller.signal.throwIfAborted()
      return read(response)
    }
    // Also settle callers when a transport/body implementation ignores abort.
    return await Promise.race([receive(), aborted])
  } catch (cause) {
    if (writing && (!(cause instanceof HttpResponseError) || cause.status >= 500)) throw new SubmissionUnknownError(cause)
    throw cause
  } finally {
    window.clearTimeout(timer)
    init.signal?.removeEventListener('abort', cancel)
    controller.signal.removeEventListener('abort', rejectAbort)
  }
}
