import { isTeamHost, isTeamRecovery, teamMessageAudience, teamProgressMessages, teamSpeaker } from './team'
import type { TeamEvent, TeamRun, TeamState } from './team'

type TimedEvent = TeamEvent & { ts?: string }
export type ConversationActivityInput = {
  team: TeamState
  events: TeamEvent[]
  names: Record<string, string>
  leaderId: string
  sending: boolean
  activity?: { active: boolean; state: string; error?: string }
  executionState: string
  syncError?: string
  controlBusy?: boolean
}
type Line = { id: string; role?: string; text: string; moving?: boolean; warning?: boolean }
export type ConversationActivityView = {
  visible: boolean
  message?: string
  messageRole?: string
  lines: Line[]
  notice?: string
  nextCheckAt?: number
  waitsForCustomer?: boolean
}

const active = (status?: string) => status === 'running' || status === 'started'
const failed = (status?: string) => ['failed', 'blocked', 'interrupted', 'uncertain', 'cancelled'].includes(status ?? '')
const successful = (status?: string) => ['done', 'completed', 'finished', 'succeeded'].includes(status ?? '')
const string = (value: unknown) => typeof value === 'string' ? value : undefined
const object = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const timestamp = (event?: TimedEvent) => event?.ts ? Date.parse(event.ts) : NaN
const tools: Record<string, string> = {
  workspace_read: '读取文件', workspace_run: '运行命令', workspace_write: '写入文件', workspace_search: '查找文件',
  workspace_list: '列出目录', browser_check: '浏览器检查', delegate: '交接工作', send_message: '投递消息', read_artifact: '读取成果',
  read_receipts: '核对执行回执', read_messages: '补读历史消息', read_background: '读取来源材料', read_checkpoint: '读取工作检查点', read_delivery: '读取交付说明', publish_artifact: '发布成果', publish_workspace_file: '发布本地文件',
  set_objective: '更新目标', finish_work: '记录交付', wait_for_reply: '等待伙伴回复',
  request_questions: '发送补充问题', request_review: '请求复核', record_review: '记录复核结论',
  list_skills: '查看工作指引目录', read_skill: '阅读工作指引',
  list_mcp_tools: '查看外部工具目录', call_mcp_tool: '调用外部工具',
}

const skillCount = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const skillText = (value: unknown): value is string => typeof value === 'string' && Boolean(value.trim())
const skillHash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value)
export const isNativeMcpTool = (name: unknown) => name === 'list_mcp_tools' || name === 'call_mcp_tool'
export const nativeMcpToolTitle = (name: unknown) => name === 'list_mcp_tools' ? '查看外部工具目录' : '调用外部工具'

function pageValid(result: Record<string, unknown>, size: number) {
  return skillCount(result.offset) && skillCount(result.total) && skillCount(result.next_offset)
    && result.offset + size === result.next_offset && result.next_offset <= result.total
    && typeof result.has_more === 'boolean' && result.has_more === (result.next_offset < result.total)
    && (!result.has_more || size > 0)
}

function terminalDataValid(result: Record<string, unknown>): boolean {
  if (result.representation_complete === false && result.result_encoding === 'escaped_json') {
    const decoded = nativeMcpEscapedResult(result)
    return decoded !== undefined && decoded.status === result.status && decoded.complete === true && decoded.outcome === result.outcome
      && ['server_id', 'tool_name', 'effect', 'invocation_id', 'configuration_current'].every(key => decoded[key] === result[key])
      && decoded.result_encoding !== 'escaped_json' && terminalDataValid(decoded)
  }
  const data = object(result.mcp_result)
  if (!Array.isArray(data.content)
    || !data.content.every(value => object(value).type === 'text' && typeof object(value).text === 'string')
    || Object.hasOwn(data, 'isError') && typeof data.isError !== 'boolean'
    || (data.isError ?? false) !== (result.outcome === 'tool_error')) return false
  if (result.representation_complete === false) {
    return skillCount(result.result_bytes) && skillHash(result.result_sha256) && typeof result.result_excerpt === 'string'
      && (result.unsupported_content_types === undefined || Array.isArray(result.unsupported_content_types) && result.unsupported_content_types.every(skillText))
  }
  return result.representation_complete === undefined || result.representation_complete === true
}

// The escaped envelope is data only; its status never replaces the host envelope.
export function nativeMcpEscapedResult(result: Record<string, unknown>): Record<string, unknown> | undefined {
  if (typeof result.result_json !== 'string' || result.result_json.length > 1024 * 1024) return undefined
  try { return object(JSON.parse(result.result_json)) } catch { return undefined }
}

export function nativeMcpReceiptStatus(event: TeamEvent, name = event.payload.name ?? event.payload.tool): string {
  if (!isTeamHost(event)) return '外部工具回执来源待核对'
  if (event.type === 'team.tool_started') return '已发起，等待回执'
  const result = object(event.payload.result), args = object(event.payload.arguments)
  const listing = name === 'list_mcp_tools'
  const uncertain = '外部工具结果待确认'
  if (event.type !== 'team.tool_finished' || !isNativeMcpTool(name)) return uncertain
  if (result.status === 'error' && result.complete === true && result.outcome === 'not_started') {
    return listing ? '未开始读取目录' : '未开始调用'
  }
  if (listing && result.status === 'error' && result.complete === true && result.outcome === 'discovery_failed') return '工具目录读取失败'
  if (!listing && result.status === 'error' && result.complete === false && result.outcome === 'effects_unknown') return '调用结果及实际影响待核对'
  let status: string
  if (listing) {
    if (result.status !== 'ok' || result.complete !== true || ![undefined, 'ok', 'completed', 'succeeded'].includes(event.payload.status as string | undefined)) return uncertain
    if (args.offset !== undefined && args.offset !== result.offset) return uncertain
    if (result.operation === 'servers') {
      const rows = result.servers
      if (args.server_id !== undefined || !Array.isArray(rows) || rows.length > 20 || !pageValid(result, rows.length)
        || !['ok', 'unavailable', 'not_configured'].includes(String(result.configuration_status))
        || rows.some(value => { const row = object(value); return !skillText(row.id) || !skillText(row.title) || !skillText(row.description) || !skillCount(row.tool_count) })
        || new Set(rows.map(value => object(value).id)).size !== rows.length) return uncertain
      return result.configuration_status === 'ok' ? '宿主服务目录已读取（未连接）' : '宿主服务配置不可用（未连接）'
    }
    const rows = result.tools
    if (result.operation !== 'list' || !Array.isArray(rows) || rows.length > 20 || !pageValid(result, rows.length)
      || rows.some(value => { const row = object(value); return !skillText(row.name) || !skillHash(row.schema_sha256)
        || row.input_schema === null || typeof row.input_schema !== 'object' || Array.isArray(row.input_schema) || !['read', 'write'].includes(String(row.effect))
        || row.title !== undefined && typeof row.title !== 'string' || row.description !== undefined && typeof row.description !== 'string' })
      || new Set(rows.map(value => object(value).name)).size !== rows.length) return uncertain
    status = '工具目录已读取'
  } else {
    if (result.complete !== true || !['returned', 'tool_error'].includes(String(result.outcome))
      || !['read', 'write'].includes(String(result.effect)) || !skillText(result.invocation_id)
      || result.status !== (result.outcome === 'tool_error' ? 'error' : 'ok')
      || result.outcome === 'returned' && ![undefined, 'ok', 'completed', 'succeeded'].includes(event.payload.status as string | undefined)
      || !terminalDataValid(result)) return uncertain
    status = result.outcome === 'tool_error' ? '调用已结束，工具报错' : '调用已返回'
    if (result.representation_complete === false) status += '（结果表示不完整）'
  }
  if (!skillText(result.server_id) || !skillText(result.tool_name) && !listing
    || args.server_id !== undefined && args.server_id !== result.server_id
    || !listing && args.tool_name !== undefined && args.tool_name !== result.tool_name
    || typeof result.configuration_current !== 'boolean') return uncertain
  if (!listing && typeof event.payload.started_seq === 'number' && typeof event.payload.call_id === 'string'
    && result.invocation_id !== `${event.payload.started_seq}:${event.payload.call_id}`) return uncertain
  return result.configuration_current === false ? `历史回执：${status}` : status
}

const skillMetadata = (value: unknown) => {
  const item = object(value)
  return skillText(item.id) && skillText(item.title) && skillText(item.description)
    && (item.source === 'builtin' || item.source === 'configured') && (item.status === 'ready' || item.status === 'unavailable')
    && Array.isArray(item.requires) && item.requires.every(skillText)
    && (item.sha256 === undefined || skillHash(item.sha256)) && (item.bytes === undefined || skillCount(item.bytes))
}

export function nativeSkillReceiptStatus(event: TeamEvent, name = event.payload.name ?? event.payload.tool): string {
  if (!isTeamHost(event)) return '指引回执来源待核对'
  if (event.type === 'team.tool_started') return '已发起，等待回执'
  const result = object(event.payload.result), args = object(event.payload.arguments)
  if (result.status === 'error') {
    if (!skillText(result.code) || !skillText(result.message)) return '指引结果待确认'
    return ['skill_changed', 'skill_version_changed', 'version_changed', 'version_mismatch', 'sha256_mismatch'].includes(result.code)
      ? '指引版本已变化，请重新核对目录' : '工作指引不可用'
  }
  if (result.status !== 'ok' || result.complete === false || result.truncated === true
    || ![undefined, 'ok', 'completed', 'succeeded'].includes(event.payload.status as string | undefined)) return '指引结果待确认'
  const listing = name === 'list_skills'
  const items = listing ? result.skills : [result.skill]
  if (!Array.isArray(items) || items.length > 20 || !items.every(skillMetadata)
    || new Set(items.map(item => object(item).id)).size !== items.length) return '指引结果待确认'
  const total = listing ? result.total : result.total_chars, offset = result.offset
  if (!listing && (typeof result.content !== 'string' || result.content.length > 24000 || object(result.skill).status !== 'ready')) return '指引结果待确认'
  const size = listing ? items.length : Array.from(result.content as string).length
  if (!skillCount(total) || !skillCount(offset) || offset > total || (!listing && size > 12000)
    || offset + size > total || typeof result.has_more !== 'boolean' || result.has_more !== (offset + size < total)
    || (result.has_more ? size === 0 || result.next_offset !== offset + size
      : result.next_offset !== undefined && result.next_offset !== null && result.next_offset !== offset + size)
    || (args.offset !== undefined && args.offset !== offset)) return '指引结果待确认'
  if (listing) {
    if (result.configuration_status !== 'ok' && result.configuration_status !== 'unavailable') return '指引结果待确认'
    return result.configuration_status === 'unavailable' ? '目录已读取，配置不可用' : '目录已读取'
  }
  const skill = object(result.skill)
  if (args.skill_id !== undefined && args.skill_id !== skill.id) return '指引结果待确认'
  if (args.expected_sha256 !== undefined) {
    if (!skillHash(args.expected_sha256)) return '指引结果待确认'
    if (!skillHash(skill.sha256) || args.expected_sha256.toLowerCase() !== skill.sha256.toLowerCase()) return '指引版本已变化，请重新核对目录'
  }
  return '指引已读取'
}

export function nativeBrowserReceiptStatus(event: TeamEvent): string {
  if (!isTeamHost(event)) return '检查回执来源待核对'
  if (event.type === 'team.tool_started') return '已发起，等待回执'
  const result = object(event.payload.result), report = object(result.browser_report), plan = object(result.browser_plan)
  if (result.scope_current === false) return '历史回执，不能作为当前成果依据'
  if (result.outcome === 'unavailable') return '检查不可用'
  if (result.outcome === 'timeout') return '检查超时'
  if (result.outcome === 'launch_error') return '检查启动失败'
  const incomplete = [result, report, object(result.stdout), object(result.stderr)].some((value) => value.complete === false || value.truncated === true)
  if (incomplete) return '回执不完整，结果待确认'
  if (result.outcome === 'failed') return '检查未通过'
  const artifacts = Array.isArray(result.checked_artifacts) ? result.checked_artifacts.map(object) : []
  const planned = Array.isArray(plan.steps) ? plan.steps.map(object) : []
  const steps = Array.isArray(report.steps) ? report.steps.map(object) : []
  // A successful tool call is not evidence that the planned assertions passed.
  const passed = result.status === 'ok' && result.scope_current === true && result.outcome === 'passed' && report.outcome === 'passed'
    && !['error', 'failed', 'timeout', 'launch_error'].includes(String(event.payload.status))
    && (result.exit_code === undefined || result.exit_code === 0) && result.artifacts_unchanged !== false
    && artifacts.length > 0 && artifacts.every((item) => typeof item.seq === 'number' && Number.isSafeInteger(item.seq) && item.seq > 0
      && typeof item.filename === 'string' && item.filename.trim() && typeof item.content_sha256 === 'string' && /^[a-f0-9]{64}$/i.test(item.content_sha256))
    && typeof plan.entrypoint === 'string' && artifacts.some((item) => item.filename === plan.entrypoint)
    && planned.length > 0 && planned.length <= 128 && planned.length === steps.length
    && planned.every((step) => typeof step.label === 'string' && step.label.trim())
    && planned.some((step) => ['assert_visible', 'assert_text', 'assert_js'].includes(String(step.action)))
    && object(report.counts).page_errors === 0 && (report.page_errors === undefined || Array.isArray(report.page_errors) && report.page_errors.length === 0)
    && steps.every((step, index) => step.index === index + 1 && step.status === 'passed' && step.action === planned[index].action
      && ['click', 'press', 'click_at', 'fill', 'wait', 'evaluate', 'assert_visible', 'assert_text', 'assert_js'].includes(String(step.action))
      && (!String(step.action).startsWith('assert_') || step.actual === true))
  return passed ? '检查通过' : '回执不完整或不一致，结果待确认'
}

function receiptFor(start: TeamEvent, events: TeamEvent[]) {
  return events.find((event) => isTeamHost(event) && event.type === 'team.tool_finished' && event.seq > start.seq
    && (typeof event.payload.started_seq === 'number' ? event.payload.started_seq === start.seq
      : typeof start.payload.call_id === 'string' && event.payload.call_id === start.payload.call_id)
    && ['run_id', 'role', 'name', 'message_seq', 'call_id'].every((key) =>
      event.payload[key] === undefined || start.payload[key] === undefined || event.payload[key] === start.payload[key]))
}

function toolOutcome(event: TeamEvent, name = event.payload.name) {
  if (isNativeMcpTool(name)) return nativeMcpReceiptStatus(event, name)
  if (name === 'browser_check') return nativeBrowserReceiptStatus(event)
  if (name === 'list_skills' || name === 'read_skill') return nativeSkillReceiptStatus(event, name)
  const result = object(event.payload.result)
  if ([event.payload.status, result.status, result.outcome].some((status) =>
    ['error', 'failed', 'timeout', 'launch_error', 'blocked', 'cancelled'].includes(String(status)))
    || typeof result.exit_code === 'number' && result.exit_code !== 0) return '未完成'
  if (result.complete === false || object(result.stdout).complete === false || object(result.stderr).complete === false) return '结果待确认'
  return [event.payload.status, result.status, result.outcome].some((status) =>
    ['ok', 'done', 'completed', 'succeeded', 'passed', 'success'].includes(String(status))) ? '已完成' : '结果待确认'
}

// Revision is an objective event sequence, including completion; it is not a round counter.
export function conversationActivity(input: ConversationActivityInput, now = Date.now()): ConversationActivityView {
  const { team, names, leaderId, activity, executionState, syncError, sending, controlBusy } = input
  const events = [...input.events].sort((a, b) => a.seq - b.seq)
  const bySeq = new Map(events.map((event) => [event.seq, event]))
  const progress = teamProgressMessages(events)
  const hosts = events.filter(isTeamHost)
  const update = hosts.filter((event) => event.type === 'team.objective_updated').at(-1)
  const completion = hosts.filter((event) => event.type === 'team.objective_completed').at(-1)
  const completed = completion && completion.seq >= team.revision && completion.seq > (update?.seq ?? 0)
  const done = Boolean(completed || team.status === 'done' && (update?.seq ?? 0) <= team.revision)
  const floor = update && (update.seq >= team.revision || done && completion && completion.seq >= team.revision)
    ? update.seq : done && completion && completion.seq >= team.revision ? 0 : team.revision
  const revisions = new Set([floor])
  if (done) revisions.add(completed ? completion.seq : team.revision)
  const label = (role: string) => names[role] ?? role
  const humans = events.filter((event) => event.type === 'team.message_received' && event.actor.kind === 'human'
    && !['recovery', 'continuation'].includes(String(event.payload.kind)))
  const latestHuman = humans.at(-1)
  const pendingQuestions = new Map<string, TeamEvent>()
  for (const question of hosts.filter((event) => event.type === 'team.questions_requested')) {
    const role = string(question.payload.role_id)
    if (role && !events.some((event) => event.seq > question.seq && (
      humans.includes(event)
      && (typeof object(event.payload.questionnaire).question_seq !== 'number'
        || object(event.payload.questionnaire).question_seq === question.seq)
      || isTeamHost(event) && (['team.objective_updated', 'team.objective_completed'].includes(event.type)
        || event.type === 'team.questions_requested' && event.payload.role_id === role)))) pendingQuestions.set(role, question)
  }
  const finishFor = (start: TeamEvent) => hosts.find((event) => event.type === 'team.run_finished' && event.seq > start.seq
    && event.payload.message_seq === start.payload.message_seq && event.payload.role === start.payload.role
    && (event.payload.run_id === undefined || start.payload.run_id === undefined || event.payload.run_id === start.payload.run_id))

  // A manager can adopt a new objective inside the run that received the customer's request.
  const adoptingRuns = new Set(hosts.filter((event) => event.type === 'team.tool_started' && event.payload.name === 'set_objective'
    && event.payload.role === leaderId && update && event.seq < update.seq
    && (receiptFor(event, hosts)?.seq ?? 0) > update.seq).map((event) => event.payload.run_id).filter((id) => typeof id === 'string'))
  const currentSource = (source?: TeamEvent) => Boolean(source
    && (source.payload.kind !== 'recovery' || isTeamRecovery(source))
    && (source.payload.kind !== 'continuation' || source.actor.kind === 'agent') && (source.seq >= floor
    && (source.actor.kind === 'human' || source.payload.revision === undefined || revisions.has(Number(source.payload.revision)))))
  const humanStarts = latestHuman ? hosts.filter((event) => event.type === 'team.run_started'
    && event.payload.message_seq === latestHuman.seq && event.payload.role === (latestHuman.payload.recipient ?? leaderId)) : []
  const currentHuman = latestHuman && (currentSource(latestHuman) || !humanStarts.length || humanStarts.some((start) =>
    adoptingRuns.has(string(start.payload.run_id) ?? '') || start.seq >= floor || !finishFor(start) || finishFor(start)!.seq >= floor)) ? latestHuman : undefined
  const starts = hosts.filter((event) => {
    if (event.type !== 'team.run_started' || typeof event.payload.role !== 'string' || typeof event.payload.message_seq !== 'number') return false
    const source = bySeq.get(event.payload.message_seq)
    if (source && source.payload.recipient !== undefined && source.payload.recipient !== event.payload.role) return false
    if (currentHuman && event.payload.message_seq === currentHuman.seq) return true
    return adoptingRuns.has(string(event.payload.run_id) ?? '') || (event.seq >= floor
      && (event.payload.revision === undefined ? !source || currentSource(source) : revisions.has(Number(event.payload.revision)))
      && (!source || currentSource(source)))
  })
  type Run = TeamRun & { start?: TeamEvent; end?: TeamEvent }
  const runs = new Map<string, Run>()
  const key = (message: unknown, role: unknown) => `${message}:${role}`
  for (const run of team.runs) {
    const source = typeof run.message_seq === 'number' ? bySeq.get(run.message_seq) : undefined
    if ((run.revision !== undefined && !revisions.has(run.revision)) || source && !currentSource(source)
      || run.revision === undefined && !source && floor > 0) continue
    runs.set(key(run.message_seq, run.role), { ...run })
  }
  for (const start of starts) {
    const p = start.payload, id = key(p.message_seq, p.role), snapshot = runs.get(id)
    const end = finishFor(start)
    runs.set(id, { message_seq: Number(p.message_seq), role: String(p.role), start, end,
      status: end ? string(end.payload.status) ?? 'uncertain' : snapshot?.status ?? 'running' })
  }
  const allRuns = [...runs.values()]
  const deliveredAt = done ? completed ? completion.seq : team.revision : undefined
  const recipient = currentHuman ? string(currentHuman.payload.recipient) ?? leaderId : undefined
  const customerRun = currentHuman ? runs.get(key(currentHuman.seq, recipient)) : undefined
  const answer = currentHuman && events.findLast((event) => {
    if (event.seq <= currentHuman.seq || event.type !== 'msg.said' || progress.has(event.seq) || event.payload.reply_to !== currentHuman.seq
      || teamSpeaker(event) !== recipient || typeof event.payload.text !== 'string' || !event.payload.text.trim()
      || !((event.actor.kind === 'agent' && event.actor.id === recipient) || isTeamHost(event))) return false
    const audience = teamMessageAudience(event, bySeq)
    return audience.audience === 'customer' && (audience.recipient === undefined || audience.recipient === currentHuman.actor.id)
      && (event.payload.run_id === undefined || customerRun?.start?.payload.run_id === undefined
        || event.payload.run_id === customerRun.start.payload.run_id)
  })
  const deliveredResponse = Boolean(done && deliveredAt !== undefined && currentHuman && currentHuman.seq < deliveredAt
    && recipient === leaderId && (humanStarts.some((start) => adoptingRuns.has(string(start.payload.run_id) ?? ''))
      || !update && currentHuman === humans[0]))
  const customerQuestion = recipient ? pendingQuestions.get(recipient) : undefined
  const responded = Boolean(answer || deliveredResponse || customerQuestion)
  let message = sending ? '消息正在发送，尚未确认收到' : currentHuman
    ? customerQuestion ? `${label(recipient!)}在等你的补充` : deliveredResponse ? `${label(recipient!)}已确认本次交付` : answer ? `${label(recipient!)}已回应最新消息`
      : active(customerRun?.status) ? `${label(recipient!)}正在处理最新消息`
        : failed(customerRun?.status) ? `最新消息答复待确认 · ${label(recipient!)}本轮处理未完成`
          : customerRun?.status === 'waiting' ? `最新消息尚待答复 · ${label(recipient!)}等待伙伴回复`
            : successful(customerRun?.status) ? '最新消息本轮处理已结束，尚未确认答复'
              : `最新消息已收到，等待${label(recipient!)}答复` : undefined
  const lines: Line[] = []
  let nextCheckAt: number | undefined
  const canMove = !syncError && executionState === 'running' && !controlBusy
  const freshness = (records: TeamEvent[]) => {
    const last = records.at(-1)
    const time = timestamp(last)
    const deadline = time + 60_000
    const fresh = canMove && Number.isFinite(time) && time <= now && now < deadline
    if (fresh) nextCheckAt = Math.min(nextCheckAt ?? Infinity, deadline)
    return fresh
  }
  const roleRuns = new Map<string, Run>()
  for (const run of allRuns.sort((a, b) => (a.message_seq ?? 0) - (b.message_seq ?? 0))) {
    if (deliveredAt !== undefined && (run.message_seq ?? 0) < deliveredAt && !active(run.status)) continue
    if (deliveredAt !== undefined && currentHuman && currentHuman.seq > deliveredAt
      && (run.message_seq ?? 0) < currentHuman.seq && !active(run.status)) continue
    if (run.role) {
      const previous = roleRuns.get(run.role)
      if (!previous || !active(previous.status) || active(run.status)) roleRuns.set(run.role, run)
    }
  }
  const currentTools = hosts.filter((event) => {
    if (event.type !== 'team.tool_started' || typeof event.payload.role !== 'string' || event.seq < floor) return false
    if (event.payload.revision !== undefined && !revisions.has(Number(event.payload.revision))) return false
    if (deliveredAt !== undefined && !roleRuns.has(event.payload.role)) return false
    if (typeof event.payload.run_id === 'string') return starts.some((start) => start.payload.run_id === event.payload.run_id
      && start.payload.role === event.payload.role && start.seq < event.seq
      && (deliveredAt === undefined || start.payload.message_seq as number >= deliveredAt || active(runs.get(key(start.payload.message_seq, start.payload.role))?.status))
      && (event.payload.message_seq === undefined || event.payload.message_seq === start.payload.message_seq))
    const source = typeof event.payload.message_seq === 'number' ? bySeq.get(event.payload.message_seq) : undefined
    return event.payload.message_seq === undefined || currentSource(source)
  })
  const latestTools = new Map<string, TeamEvent>()
  for (const tool of currentTools) latestTools.set(String(tool.payload.role), tool)
  const roles = new Set([...roleRuns.keys(), ...latestTools.keys(), ...pendingQuestions.keys()])
  let anyOpenTool = false
  for (const role of roles) {
    const run = roleRuns.get(role), tool = latestTools.get(role)
    const toolName = tool?.payload.name ?? (isNativeMcpTool(tool?.payload.tool) ? tool?.payload.tool : undefined)
    const receipt = tool ? receiptFor(tool, hosts) : undefined
    const finished = receipt && tool && (['list_skills', 'read_skill'].includes(String(tool.payload.name)) || isNativeMcpTool(toolName))
      ? { ...receipt, payload: { ...tool.payload, ...receipt.payload } } : receipt
    const toolBelongs = tool && (!run?.start || tool.seq > run.start.seq
      && (run.start.payload.run_id === undefined || tool.payload.run_id === run.start.payload.run_id))
    const openTool = toolBelongs && !finished && (!run || active(run.status))
    const running = active(run?.status) || Boolean(openTool)
    if (openTool) anyOpenTool = true
    const roleTools = currentTools.filter((event) => event.payload.role === role && (!run?.start
      || run.start.payload.run_id !== undefined && event.payload.run_id === run.start.payload.run_id
      || event.payload.message_seq === run.message_seq))
    const records = [run?.start, run?.end, ...roleTools, ...roleTools.map((event) => receiptFor(event, hosts)),
      ...hosts.filter((event) => event.type === 'team.model_called' && event.payload.role === role && run?.start
        && typeof run.start.payload.run_id === 'string' && event.payload.run_id === run.start.payload.run_id)]
      .filter((event): event is TeamEvent => Boolean(event)).sort((a, b) => a.seq - b.seq)
    const awaitingCustomer = pendingQuestions.has(role) && !running
    const moving = !awaitingCustomer && running && freshness(records)
    const action = toolBelongs ? tools[string(toolName) ?? ''] ?? '工具调用' : undefined
    const handling = bySeq.get(run?.message_seq ?? -1)?.actor.kind === 'human' ? '正在处理消息' : '正在处理工作'
    let text = awaitingCustomer ? '等待你的补充' : running ? moving ? handling : '暂无新反馈，执行状态待确认'
      : run?.status === 'waiting' ? '等待伙伴回复'
        : failed(run?.status) ? '本轮处理未完成，需核对过程'
          : successful(run?.status) ? '本轮消息处理已结束' : '执行结果待确认'
    if (action && tool?.payload.name !== 'wait_for_reply' && !awaitingCustomer) text = `${action} · ${finished ? toolOutcome(finished, toolName) : '已发起，等待回执'}；${text}`
    lines.push({ id: role, role, text: `${label(role)} · ${text}`, moving,
      warning: failed(run?.status) || Boolean(toolBelongs && finished && !['已完成', '检查通过', '目录已读取', '指引已读取', '宿主服务目录已读取（未连接）', '工具目录已读取', '调用已返回'].includes(toolOutcome(finished, toolName))) })
  }
  const reported = new Set(events.filter((event) => event.type === 'team.message_received' && event.actor.kind === 'agent'
    && event.payload.kind === 'report' && (() => {
      const source = bySeq.get(Number(event.payload.reply_to))
      return source && source.seq < event.seq && event.actor.id === source.payload.recipient && event.payload.recipient === source.actor.id
    })()).map((event) => event.payload.reply_to))
  const received = events.filter((event) => event.type === 'team.message_received' && event.payload.kind !== 'notification'
    && event.payload.kind !== 'recovery' && (event.actor.kind === 'agent' || humans.includes(event)))
  const knownPending = received.filter((event) => !reported.has(event.seq)
    && !hosts.some((start) => start.type === 'team.run_started' && start.payload.message_seq === event.seq)
    && !team.runs.some((run) => run.message_seq === event.seq))
  const pendingInScope = (event: TeamEvent) => currentSource(event) && (deliveredAt === undefined || event.seq > deliveredAt)
  const retiredPending = knownPending.filter((event) => !pendingInScope(event)).length
  const customerPending = knownPending.filter((event) => event.actor.kind === 'human' && pendingInScope(event)).length
  const pending = Math.max(knownPending.filter((event) => event.actor.kind === 'agent' && pendingInScope(event)).length,
    team.pending_count - retiredPending - customerPending, 0)
  const anyRunning = allRuns.some((run) => active(run.status)) || anyOpenTool
  const fault = lines.some((line) => line.warning)
  if (pending > 0) lines.push({ id: 'pending', text: `后台有 ${pending} 条消息等待处理` })
  if (activity?.active && !anyRunning && !pending) lines.push({ id: 'activity', text: activity.state === 'queued'
    ? `后台协作已排队，等待执行记录${done ? '' : '；尚未确认交付'}`
    : `后台暂无新反馈，等待执行记录${done ? '' : '；尚未确认交付'}` })
  if (!anyRunning && !pending && !activity?.active) {
    if (activity?.state === 'failed' && !done) lines.push({ id: 'failure', text: '后台协作异常，尚未确认交付；需查看过程核对', warning: true })
    else if (['failed', 'blocked'].includes(team.status) && !done) lines.push({ id: 'failure', text: '后台工作需要跟进，尚未确认交付', warning: true })
    else if (!done && !pendingQuestions.size) lines.push({ id: 'delivery', text: '当前暂无执行，尚未确认交付' })
  }
  const notice = syncError ? '同步失败，状态待确认；可刷新核对。'
    : executionState === 'paused' ? '已暂停后续执行；在途调用仍可能返回。仍可直接聊天。'
      : executionState === 'cancelled' ? '已取消后续执行；在途调用仍可能返回。仍可直接聊天，继续执行需新建任务。'
        : controlBusy ? '执行控制请求待确认。仍可直接聊天。'
          : executionState !== 'running' ? '执行控制状态待确认。仍可直接聊天。' : undefined
  if (!sending && currentHuman && !responded && active(customerRun?.status) && !lines.find((line) => line.role === recipient)?.moving)
    message = `最新消息已收到 · ${label(recipient!)}暂无新反馈，答复待确认`
  return { visible: team.enabled && Boolean(sending || currentHuman && !responded || !done || anyRunning || pending || pendingQuestions.size || activity?.active || fault || notice),
    message, messageRole: sending ? undefined : recipient, lines, notice, nextCheckAt,
    waitsForCustomer: Boolean(pendingQuestions.size && !sending && !anyRunning && !pending && !activity?.active && !fault && !notice) }
}
