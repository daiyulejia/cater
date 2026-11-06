import { isSafeAuditError } from './activityDiagnostics'

export const READ_TIMEOUT_MS = 8000
export const SUBMIT_TIMEOUT_MS = 30000

export class RequestTimeoutError extends Error {
  constructor() { super('请求超时，请稍后重试。'); this.name = 'RequestTimeoutError' }
}

export class SubmissionUnknownError extends Error {
  constructor(cause: unknown) {
    const reason = cause instanceof RequestTimeoutError ? '请求超时'
      : cause instanceof HttpResponseError ? `服务端返回 HTTP ${cause.status}` : '连接中断或响应不完整'
    super(`${reason}，提交结果未知。请核对最新记录；本次没有自动重发。`, { cause })
    this.name = 'SubmissionUnknownError'
  }
}

class HttpResponseError extends Error {
  readonly status: number
  constructor(message: string, status: number) { super(message); this.status = status }
}

// These are exact, fixed messages emitted by the host. Never interpolate an
// unrecognized response body into an error that can be displayed by the UI.
const safeHttpMessages = new Set([
  'access token is required',
  'goal is required',
  'a collaboration activity is already running for this task',
  'choose either run for normal collaboration or battle for forced governance',
  'execution is paused or cancelled; send a message to chat',
  '任务已暂停，不再启动下一步；已经发出的调用可能仍会返回，结果会保留。',
  '任务已取消，不再启动后续工作；已有成果和在途调用的返回记录会保留。',
  '执行已暂停或取消，恢复后再提交；仍可直接聊天。',
  '这项任务已经取消，需要另开任务继续新的工作。',
  '执行状态正在变化，请使用原请求标识重试。',
  '执行控制只支持暂停、恢复或取消。',
  '需要有效的执行控制请求标识。',
  '同一控制请求不能更换任务或动作。',
  '工作区配置暂不可用，请检查服务端配置文件；现有工具权限已停用。',
  '只能选择已配置的工作区。',
  '工作区版本无效。',
  '工作区绑定已变化，请刷新后重试。',
  '已有工作在途，需要结束或核对后再更换工作区。',
  '已有协作在途，结束或暂停并核对后再绑定工作区。',
  '同一创建请求不能更换目标或执行选项，请重新发起。',
  '同一创建请求不能更换目标或来源，请重新发起。',
  '同一提问请求不能更换问题或对象。',
  '创建请求正在更新，请使用原请求标识重试。',
  '同一请求标识已经用于不同内容。',
  '这组问题已回答或已有新的对话，请根据最新消息继续。',
  '请逐题回答或明确跳过，再整组提交。',
  '每题需要独立且不重复的回答。',
  '回答必须对应当前这一组问题。',
  '选择、自填或跳过内容无效。',
  '跳过不能同时提交选项或文字。',
  '空白回答需要明确选择跳过。',
  '单选题只能选一个选项，可另加文字。',
  '补充问题编号无效。',
  '请求标识过短。',
  '这组问题不属于当前对话。',
  '问题编号或回复方式无效。',
  '回复需要有效的请求标识。',
  '这个补充问题不属于当前对话。',
  '请选择这个问题中的一个选项。',
  '请填写不超过 2000 字的想法。',
  '跳过问题不能同时选择或填写答案。',
  '这次回复请求已用于不同内容，请刷新后重试。',
  '这个问题已回答或已随新方向失效，请刷新对话。',
  '对话正在更新，请读取最新问题后再回答。',
  '这段对话已从列表删除，请新建对话；本机记录与已有文件仍保留。',
  '同一删除请求不能更换对话或动作。',
  '对话记录正在更新，请沿用原请求标识重试。',
  'rules changed concurrently; refresh and retry',
  'rule version changed; refresh before editing',
  'an equivalent rule already exists in this scope',
  'active rules exceed scope context capacity; consolidate first',
  'too many candidate rules in this scope',
  'task already belongs to a project',
  'cannot bind a project while collaboration is active',
])

function safeHttpMessage(body: string, status: number): string {
  if (status < 500 && (safeHttpMessages.has(body) || isSafeAuditError(body))) return body
  if (status < 500 && /^unknown role [^\r\n]{1,300}$/.test(body)) return '点名的伙伴不存在，请从当前猫咖成员中选择。'
  if (status < 500 && /^(goal|instruction|assignment objective|assignment acceptance) exceeds \d+ character limit$/.test(body))
    return '这段内容太长了，请缩短后再发送。'
  if (status < 500 && /^acceptance exceeds \d+ item limit$/.test(body)) return '验收要求太多了，请保留最重要的几条。'
  return `请求未完成（HTTP ${status}），请核对后重试。`
}

export async function readJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) throw new HttpResponseError(safeHttpMessage(await response.text(), response.status), response.status)
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
