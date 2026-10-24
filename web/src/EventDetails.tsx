import { BrowserReport, NativeBrowserReceipt } from './browserReport'
import { isTeamHost, isTeamRecovery, teamRunStatus } from './team'
import { isNativeMcpTool, nativeBrowserReceiptStatus, nativeSkillReceiptStatus, nativeMcpReceiptStatus, nativeMcpToolTitle, toolTitle, nativeProjectHistoryReceiptStatus, nativeProjectRestoreReceiptStatus, projectHistoryReadArguments } from './conversationActivityState'
import { NativeSkillReceipt } from './TeamProgressPanel'
import { usageSummary } from './modelUsage'
import { containedNote, responseDiagnostics, responseDiagnosticFields, requestFailureFields, stageNote } from './responseDiagnostics'
import { CommandOutputs } from './CommandOutputPreview'
import { hasBrowserReceipt } from './outputPreview'
import { NativeMcpReceipt } from './NativeMcpReceipt'
import { NativeProjectHistoryReceipt } from './NativeProjectHistoryReceipt'
import { NativeProjectRestoreReceipt } from './NativeProjectRestoreReceipt'
import { NativeArtifactReceipt } from './NativeArtifactReceipt'
import { NativeArtifactVersionsReceipt } from './NativeArtifactVersionsReceipt'
import { nativeArtifactReceiptStatus } from './conversationActivityState'
import { isFrozenArchiveReceipt, nativeFrozenArchiveReceiptStatus, teamToolReceipt } from './conversationActivityState'
import { NativeFrozenArchiveReceipt } from './NativeFrozenArchiveReceipt'
import { NativeReceiptRead } from './NativeReceiptRead'
import { hasReceiptReadMetadata, receiptReadProvenance } from './receiptReadStatus'
import { receiptReadArguments } from './conversationActivityState'

export type DetailEvent = {
  seq: number
  type: string
  actor: { id: string; role: string; kind: string }
  payload: Record<string, unknown>
}

export type EventDetailsProps = { event: DetailEvent; actorName?: (id: string) => string; contextEvents?: DetailEvent[] }

function readArguments(event: DetailEvent, events: DetailEvent[]): DetailEvent {
  if (hasReceiptReadMetadata(event)) return receiptReadArguments(event, events)
  const name = event.payload.name ?? event.payload.tool
  if (!isTeamHost(event) || event.type !== 'team.tool_finished'
    || !(name === 'read_artifact' || name === 'read_background' && isFrozenArchiveReceipt(event))
    || Object.hasOwn(event.payload, 'arguments') || !Number.isSafeInteger(event.payload.started_seq)) return event
  const matches = events.filter(start => start.seq === event.payload.started_seq)
  if (matches.length !== 1) return event
  const start = matches[0]
  const args = start.payload.arguments
  if (!isTeamHost(start) || start.type !== 'team.tool_started' || start.seq >= event.seq
    || (start.payload.name ?? start.payload.tool) !== name
    || ['role', 'run_id'].some(key => typeof start.payload[key] !== 'string' || !start.payload[key]
      || start.payload[key] !== event.payload[key])
    || typeof start.payload.call_id !== 'string' || !start.payload.call_id
    || typeof start.payload.message_seq !== 'number' || !Number.isSafeInteger(start.payload.message_seq)
    || start.payload.message_seq < 1 || teamToolReceipt(start, [event]) !== event
    || args === null || typeof args !== 'object' || Array.isArray(args)) return event
  return { ...event, payload: { ...event.payload, arguments: args } }
}

export function EventDetailsList({ events, actorName }: { events: DetailEvent[]; actorName?: (id: string) => string }) {
  return <>{events.map(event => <EventDetails key={event.seq} event={event} actorName={actorName} contextEvents={events} />)}</>
}
type Field = { label: string; value: string }

const titles: Record<string, string> = {
  'team.message_received': '团队收到消息', 'team.questions_requested': '等待顾客补充', 'team.run_started': '开始处理消息', 'team.run_finished': '消息处理结果',
  'team.objective_updated': '团队目标已更新', 'team.objective_completed': '店长交付结论',
  'team.model_called': '团队模型用量', 'team.tool_started': '团队工具已发起', 'team.tool_finished': '团队工具回执',
  'team.response_correction_requested': '工具响应纠正安排',
  'team.review_followup_requested': '复核结论补登记提醒',
  'team.tool_reconciled': '发布影响已核对',
  'task.created': '任务已创建', 'task.started': '任务已启动', 'task.interpreted': '任务理解',
  'task.routed': '协作安排', 'mission.created': '工作目标', 'task.decomposed': '任务分工',
  'subtask.assigned': '工作指派', 'plan.proposed': '协作计划', 'plan.amended': '计划调整',
  'planning.requested': '请求规划', 'planning.started': '开始规划', 'planning.finished': '规划结果',
  'work.plan_created': '工作计划已建立', 'work.claimed': '工作已领取', 'work.submitted': '成果已提交',
  'work.reviewed': '阶段复核', 'work.review_deferred': '阶段复核暂缓', 'work.failed': '工作未正常结束',
  'work.invalidated': '工作需要重新核对', 'work.check_started': '本机检查已开始',
  'delivery.prepared': '交付清单已准备', 'delivery.review_started': '整体验收已开始',
  'delivery.review_finished': '整体验收结果', 'delivery.check_started': '整体检查已开始',
  'verification.completed': '本机验证回执', 'checks.contract_revised': '检查要求已调整',
  'verification.planning_started': '正在准备浏览器检查', 'verification.planned': '浏览器检查方案',
  'agent.working': '成员正在处理', 'msg.said': '成员发言', 'msg.challenge': '成员提出疑问',
  'deliverable.published': '成果已发布', 'diagnosis.recorded': '诊断记录', 'handoff.published': '交接说明',
  'human.commanded': '用户指令', 'human.decided': '人工决定', 'task.progress_reported': '进度记录',
  'question.asked': '用户提问', 'question.started': '开始讲解', 'question.finished': '讲解结果',
  'conversation.received': '收到聊天消息', 'conversation.started': '正在理解消息', 'conversation.finished': '消息处理结果',
  'collaboration.requested': '成员发起咨询', 'collaboration.finished': '定向咨询结果',
  'clarification.requested': '等待补充信息', 'activity.queued': '协作已排队',
  'activity.completed': '本轮协作已结束', 'activity.failed': '本轮协作失败',
  'driver.failed': '模型调用异常', 'driver.budget_exhausted': '模型预算已用尽',
  'driver.retry_scheduled': '模型自动续试安排', 'driver.retry_finished': '模型自动续试结果',
  'cabinet.convened': '联合讨论', 'cabinet.voted': '讨论建议', 'decision.proposed': '决策建议',
  'agent.decided': '协作决定', 'supreme.decided': '最终协作决定', 'brief.generated': '本轮总结',
  'escalation.triggered': '需要进一步处理', 'review.generated': '任务复查',
  'agent.discipline_flagged': '发言需要核对', 'rule.recorded': '工作规则已记录',
  'rule.updated': '工作规则已更新', 'rules.applied': '工作规则已绑定',
  'project.created': '项目已创建', 'task.project_bound': '任务已关联项目',
  'room.created': '会话已创建', 'room.task_attached': '会话已关联任务', 'room.message_received': '收到会话消息',
}

function label(value: unknown, labels: Record<string, string>, fallback = '状态待核对'): string {
  return typeof value === 'string' && Object.hasOwn(labels, value) ? labels[value] : fallback
}

function text(value: unknown, limit = 1200): string {
  if (typeof value !== 'string') return ''
  const trimmed = value.trim()
  return trimmed.length > limit ? `${trimmed.slice(0, limit)}…（已省略）` : trimmed
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

const skillTool = (event: DetailEvent) => ['team.tool_started', 'team.tool_finished'].includes(event.type)
  && ['list_skills', 'read_skill'].includes((event.payload.name ?? event.payload.tool) as string)

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string' && Boolean(entry.trim())) : []
}

function lines(values: string[], max = 8): string {
  return [...values.slice(0, max).map(value => text(value)), ...(values.length > max ? [`另有 ${values.length - max} 项未展开。`] : [])].join('\n')
}

function resolvedName(id: string, actorName?: (id: string) => string): string {
  const name = actorName?.(id)?.trim()
  return name && name !== id ? text(name, 100) : ''
}

const reviewStates = { accepted: '阶段验收通过', submitted: '等待补齐验收依据', rework: '需要返工' }
const deliveryStates = { accepted: '整体验收通过', needs_evidence: '等待补齐验收依据', rework: '需要返工',
  stale: '版本已变化', invalid: '复核记录无效', blocked: '复核受阻', uncertain: '复核结果待确认' }

// This event carries an issue code, but no response protocol or provider diagnostics.
const correctionIssues = {
  calls_not_array: '工具请求列表不是数组', invalid_call_shape: '工具请求结构无效',
  unsupported_call_type: '工具请求类型不受支持', invalid_function: '工具函数定义无效',
  invalid_call_id: '工具请求标识无效', duplicate_call_id: '工具请求标识重复',
  invalid_tool_name: '工具名称无效', unregistered_tool: '请求了本轮未提供的工具',
  arguments_not_text: '工具参数未使用 JSON 文本', invalid_arguments_json: '工具参数不是有效且无歧义的 JSON',
  arguments_not_object: '工具参数不是对象', invalid_arguments_value: '工具参数值无效',
}

const own = (payload: Record<string, unknown>, key: string): unknown => Object.hasOwn(payload, key) ? payload[key] : undefined
const positiveSeq = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const correctionHost = (event: DetailEvent) => ['id', 'role', 'kind'].every(key => Object.hasOwn(event.actor, key)) && isTeamHost(event)
const hasCorrectionReference = (event: DetailEvent) => ['team.model_called', 'team.run_finished'].includes(event.type)
  && ['correction_of', 'correction_attempt'].some(key => Object.hasOwn(event.payload, key))

function correctionReference(event: DetailEvent) {
  const original = own(event.payload, 'correction_of')
  return correctionHost(event) && positiveSeq(event.seq) && positiveSeq(original) && original < event.seq
    && own(event.payload, 'correction_attempt') === 1 ? { correction_of: original, correction_attempt: 1 } : undefined
}

function correctionRequest(event: DetailEvent) {
  const p = event.payload
  const role = own(p, 'role'), run = own(p, 'run_id'), message = own(p, 'message_seq')
  const revision = own(p, 'revision'), original = own(p, 'model_call_seq')
  if (!correctionHost(event) || !positiveSeq(event.seq) || !positiveSeq(original) || original >= event.seq
    || !positiveSeq(message) || message >= original || own(p, 'attempt') !== 1
    || typeof role !== 'string' || !role.trim() || typeof run !== 'string' || !run.trim()
    || typeof revision !== 'number' || !Number.isSafeInteger(revision) || revision < 0) return undefined
  return { role, message, revision, original, issue: label(own(p, 'issue'), correctionIssues, '') }
}

const correctionReferenceNote = '纠正关联仅为原模型调用的引用，不代表纠正成功、工具执行或任务完成，也不代表免除计费。'

function describe(event: DetailEvent, actorName?: (id: string) => string) {
  const p = event.payload
  const fields: Field[] = []
  let status = '已记录'
  let note = ''
  const add = (name: string, value: unknown) => { const content = text(value); if (content) fields.push({ label: name, value: content }) }
  const number = (name: string, value: unknown, suffix = '') => {
    if (typeof value === 'number' && Number.isFinite(value)) fields.push({ label: name, value: `${value}${suffix}` })
  }
  const list = (name: string, value: unknown) => add(name, lines(strings(value)))
  const member = (value: unknown) => typeof value === 'string' && value.trim()
    ? `@${resolvedName(value, actorName) || label(value, { dev: '开发成员', test: '验证成员', design: '设计成员', product: '需求成员' }, '未命名成员')}` : ''
  const assignments = (value: unknown) => {
    if (!Array.isArray(value)) return
    value.slice(0, 8).forEach((entry, index) => {
      const item = record(entry)
      const parts = [text(item.objective, 700), member(item.owner) && `负责人：${member(item.owner)}`,
        member(item.reviewer) && `复核人：${member(item.reviewer)}`, text(item.acceptance) && `验收要求：${text(item.acceptance, 150)}`]
      add(`工作 ${index + 1}`, parts.filter(Boolean).join('\n'))
    })
    if (value.length > 8) add('其余分工', `另有 ${value.length - 8} 项工作未展开。`)
  }
  const checks = (value: unknown) => {
    if (!Array.isArray(value)) return
    value.slice(0, 8).forEach((entry, index) => {
      const check = record(entry)
      add(`核对项 ${index + 1}`, [text(check.text, 600) || text(check.id, 100),
        label(check.status, { passed: '通过', failed: '未通过', unknown: '依据不足', superseded: '已由后续要求替代' }), text(check.reason, 450)].filter(Boolean).join('\n'))
    })
    if (value.length > 8) add('其余核对项', `另有 ${value.length - 8} 项未展开。`)
  }
  const browserPlan = (value: unknown) => {
    const plan = record(value)
    add('浏览器入口', plan.entrypoint)
    if (Array.isArray(plan.steps)) list('计划步骤', plan.steps.slice(0, 20).map((step, index) => {
      const title = text(record(step).label, 200)
      return title ? `${index + 1}. ${title}` : ''
    }))
  }

  // Only known event contracts select fields; unrecognized payloads stay in developer details.
  if (!Object.hasOwn(titles, event.type)) return { fields, status, note: '已保存这条事件记录，暂无对应的中文摘要。' }
  if (event.type !== 'team.response_correction_requested' && !hasCorrectionReference(event)) add('工作项', p.work_item_id)
  switch (event.type) {
    case 'team.review_followup_requested': {
      const review = own(p, 'review_seq'), message = own(p, 'message_seq'), original = own(p, 'model_call_seq')
      const role = own(p, 'role'), run = own(p, 'run_id')
      status = '补登记提醒待核对'
      if (!correctionHost(event) || !positiveSeq(event.seq) || !positiveSeq(review) || !positiveSeq(message)
        || !positiveSeq(original) || !(review < message && message < original && original < event.seq)
        || typeof role !== 'string' || !role.trim() || typeof run !== 'string' || !run.trim()) {
        note = '来源或关联信息未通过核对，不能作为已安排提醒的依据。'
        break
      }
      status = '已记录一次补登记提醒'
      add('复核成员', member(role)); number('复核请求', review); number('原复核委托', message)
      number('关联模型返回事件', original)
      note = '原复核仍待正式结论；这不是新任务、批准或代签，也不证明后续模型请求已经发出。'
      break
    }
    case 'team.response_correction_requested': {
      const request = correctionRequest(event)
      status = '纠正安排待核对'
      if (!request) {
        note = '来源或关联信息未通过核对，不能作为纠正安排依据。'
        break
      }
      status = '已安排一次纠正'
      add('处理成员', member(request.role)); number('关联消息', request.message); number('目标版本', request.revision)
      number('被拒绝的模型调用事件', request.original); number('纠正次数', 1)
      add('工具请求诊断', request.issue)
      note = '仅安排一次纠正，不代表模型请求已发送或已成功返回，也不代表工具执行或任务完成；远端处理和计费仍需核对回执与服务商账单。'
      break
    }
    case 'team.message_received':
      if (p.kind === 'recovery') {
        if (!isTeamRecovery(event)) {
          status = '恢复通知来源待核对'
          note = '来源未通过团队系统身份核对，不能作为恢复依据。'
          break
        }
        status = '恢复通知已记录'
        add('消息内容', p.text); add('接收成员', member(p.recipient))
        number('原处理开始事件', p.recovery_of); number('中断事件', p.interrupted_seq); number('目标版本', p.revision)
        note = '这是内部恢复通知，不代表已经恢复执行或验收通过。'
        break
      }
      if (p.kind === 'continuation') {
        if (event.actor.kind !== 'agent') {
          status = '接续消息来源待核对'
          break
        }
        status = '接续指令已记录'
        number('接续原委托', p.continue_assignment_seq); number('上级委托', p.parent_assignment_seq); number('目标版本', p.revision)
        note = '这是原委托的补充指令，关联以原消息为准，不代表独立交付或验收通过。'
      }
      add('消息内容', p.text); add('接收成员', member(p.recipient)); number('回复消息', p.reply_to)
      add('消息类型', label(p.kind, { message: '聊天消息', assignment: '工作交接', report: '内部报告', continuation: '接续工作交接' }, '消息'))
      break
    case 'team.run_started': case 'team.run_finished': {
      const diagnostic = event.type === 'team.run_finished' && isTeamHost(event) ? responseDiagnostics(p, true) : {}
      const hasDiagnostic = event.type === 'team.run_finished' && ['response_status', 'failure_stage', 'external_failure_code', 'external_rpc_code', 'external_rpc_method'].some(key => Object.hasOwn(p, key))
      status = event.type === 'team.run_started' ? '正在处理消息' : label(p.status, { done: '消息处理完成', completed: '消息处理完成', finished: '消息处理完成', succeeded: '消息处理完成', waiting: teamRunStatus('waiting'), interrupted: '消息处理中断', failed: '消息处理失败', blocked: '消息处理受阻', cancelled: '消息处理已取消' }, '消息处理结果待确认')
      number('关联消息', p.message_seq); add('处理成员', member(p.role))
      if (!hasDiagnostic && !hasCorrectionReference(event) && Object.hasOwn(p, 'reason')) add('原因', p.reason)
      if (diagnostic.responseStatus === 'contained') status = '响应未通过完整性检查'
      for (const [name, value] of responseDiagnosticFields(diagnostic)) add(name, value)
      const requestFailure = event.type === 'team.run_finished' && isTeamHost(event) ? requestFailureFields(p) : []
      for (const [name, value] of requestFailure) add(name, value)
      note = '消息处理结束不代表任务已交付。'
      if (diagnostic.responseStatus === 'contained') note += containedNote
      if (diagnostic.failureStage) note += stageNote
      if (requestFailure.length) note += '已有成果和操作记录保留；远端是否已处理或计费需核对回执与服务商账单，诊断不代表允许重试。'
      if (event.type === 'team.run_finished') {
        const correction = correctionReference(event)
        if (correction) {
          number('纠正原模型调用事件', correction.correction_of); number('纠正次数', correction.correction_attempt)
          note += correctionReferenceNote
        }
      }
      break
    }
    case 'team.tool_reconciled':
      status = '实际影响已核对'; number('原操作', p.started_seq); add('核对依据', p.reason)
      note = '原始操作结果不改写，后续不重放原请求。'
      break
    case 'team.objective_updated': add('当前目标', p.goal); break
    case 'team.objective_completed':
      status = '店长确认可交付'; add('交付摘要', p.summary); add('验证范围', p.verification)
      note = '这是店长记录的交付结论，实际验证范围按原文保留。'
      break
    case 'team.model_called': {
      const call = usageSummary([event]).calls[0]
      if (!call) {
        status = '用量来源待核对'
        note = '来源未通过团队系统身份核对，不能作为用量依据。'
        break
      }
      status = { reported: '用量已报告', estimated: '估算用量', unavailable: '用量未报告', unspecified: '用量来源未注明' }[call.source]
      if (call.responseStatus === 'protocol_error') {
        add('用量状态', status)
        status = '响应格式错误'
      }
      if (call.responseStatus === 'contained') {
        add('用量状态', status)
        status = '响应未通过完整性检查'
      }
      for (const [name, value] of responseDiagnosticFields(call)) add(name, value)
      add('调用成员', member(call.role))
      if (call.tokens !== null) number(call.source === 'reported' ? '已报告用量' : call.source === 'estimated' ? '估算值' : '未注明来源的记录值', call.tokens, ' token')
      number(call.inputKind === 'uncached' ? '输入（不含缓存）' : '输入（含缓存）', call.input, ' token')
      number('输出用量', call.output, ' token'); number('缓存读取', call.cacheRead, ' token')
      number('缓存写入', call.cacheWrite, ' token'); number('推理细项', call.reasoning, ' token')
      number('提示正文字节', call.requestSizes.prompt); number('历史 JSON 字节', call.requestSizes.history)
      number('工具定义 JSON 字节', call.requestSizes.tools)
      note = '缓存和推理细项不重复叠加到总量；未报告不等于未消耗。字节不是 token，实际费用以服务商账单为准。'
      if (call.responseStatus === 'protocol_error') note = `响应格式无效，工作未继续；已报告用量仍保留。${note}`
      if (call.responseStatus === 'contained') note = `${containedNote}${note}`
      const correction = correctionReference(event)
      if (correction) {
        number('纠正原模型调用事件', correction.correction_of); number('纠正次数', correction.correction_attempt)
        note += correctionReferenceNote
      }
      break
    }
    case 'team.tool_started': case 'team.tool_finished':
      if (hasReceiptReadMetadata(event)) {
        status = receiptReadProvenance(event)!.status
        add('工具', '核对已存执行回执')
        if (isTeamHost(event)) add('读取成员', member(p.role))
        break
      }
      if (isFrozenArchiveReceipt(event)) {
        status = nativeFrozenArchiveReceiptStatus(event)
        add('工具', '读取来源材料')
        if (isTeamHost(event)) add('读取成员', member(p.role))
        break
      }
      if ((p.name ?? p.tool) === 'read_artifact') {
        status = nativeArtifactReceiptStatus(event)
        add('工具', '读取成果')
        if (isTeamHost(event)) add('读取成员', member(p.role))
        break
      }
      if ((p.name ?? p.tool) === 'read_project_history') {
        status = nativeProjectHistoryReceiptStatus(event)
        add('工具', '读取同项目历史')
        if (isTeamHost(event)) add('读取成员', member(p.role))
        break
      }
      if ((p.name ?? p.tool) === 'workspace_restore_project_file') {
        status = nativeProjectRestoreReceiptStatus(event)
        add('工具', '恢复历史工程文件')
        if (isTeamHost(event)) add('执行成员', member(p.role))
        break
      }
      if (isNativeMcpTool(p.name ?? p.tool)) {
        status = nativeMcpReceiptStatus(event)
        add('工具', nativeMcpToolTitle(p.name ?? p.tool))
        if (isTeamHost(event)) {
          add('执行成员', member(p.role)); number('关联消息', p.message_seq); number('发起事件', p.started_seq)
        } else note = '来源未通过团队系统身份核对，不能作为外部工具调用依据。'
        break
      }
      if (skillTool(event)) {
        status = nativeSkillReceiptStatus(event)
        add('工具', (p.name ?? p.tool) === 'list_skills' ? '查看工作指引目录' : '阅读工作指引')
        if (isTeamHost(event)) {
          add('阅读成员', member(p.role)); number('关联消息', p.message_seq); number('发起事件', p.started_seq)
        } else note = '来源未通过团队系统身份核对，不能作为工作指引读取依据。'
        break
      }
      if ((p.name ?? p.tool) === 'browser_check') {
        status = nativeBrowserReceiptStatus(event)
        add('工具', '浏览器检查')
        if (isTeamHost(event)) {
          add('执行成员', member(p.role)); number('关联消息', p.message_seq); number('发起事件', p.started_seq)
        } else note = '来源未通过团队系统身份核对，不能作为宿主检查依据。'
        break
      }
      status = event.type === 'team.tool_started' ? '等待工具回执' : label(p.status ?? record(p.result).status, { ok: '工具已完成', completed: '工具已完成', succeeded: '工具已完成', error: '工具未完成', failed: '工具未完成' }, '工具结果待确认')
      add('工具', toolTitle(p.name ?? p.tool)); add('执行成员', member(p.role)); add('摘要', p.summary ?? record(p.result).summary)
      number('关联消息', p.message_seq); number('发起事件', p.started_seq); number('退出码', record(p.result).exit_code)
      add('原因', p.reason ?? record(record(p.result).error).message)
      note = '工具回执仅说明这次操作的结果。'
      if ((p.name ?? p.tool) === 'read_checkpoint') {
        const result = record(p.result), checkpoint = record(result.checkpoint)
        add('当前目标', checkpoint.objective); number('事实截至事件', checkpoint.as_of_seq)
        number('未交还工作', checkpoint.open_work_count, ' 项'); number('待核对操作', checkpoint.pending_operation_count, ' 项')
        if (Array.isArray(result.assignments)) add('本页原委托', lines(result.assignments.map(value => {
          const assignment = record(value)
          return typeof assignment.seq === 'number' ? `#${assignment.seq}：${text(assignment.text, 500)}` : ''
        }).filter(Boolean)))
        if (result.has_more === true) { add('读取范围', '还有后续委托，本页不代表全部工作。'); number('下页起始委托之后', result.next_seq) }
        if (checkpoint.open_work_truncated === true) add('索引范围', '工作索引仅包含部分记录。')
        note = '只读核对持久事件中的工作事实，不读取私有思考，也不代表恢复成功或验收通过。'
      }
      break
    case 'collaboration.requested':
      status = '已发起咨询'
      add('咨询成员', member(p.requester)); add('咨询对象', member(p.recipient)); add('问题', p.question)
      note = '只读咨询，不自动修改任务，也不代表接收成员已经回应。'
      break
    case 'collaboration.finished':
      status = label(p.status, { answered: '已回应', declined: '未提供意见', invalid: '回复未通过核对', unavailable: '咨询暂不可用', stale: '咨询依据已变化' })
      number('咨询事件', p.request_seq)
      add('咨询成员', member(p.requester)); add('咨询对象', member(p.recipient))
      if (event.actor.kind === 'system' && event.actor.id === 'collaboration' && event.actor.role === 'collaboration'
        && (p.status === 'answered' || p.status === 'declined') && typeof p.speaker_id === 'string' && p.speaker_id === p.recipient && text(p.reply)) {
        add('回应成员', member(p.speaker_id)); add('真实回应', p.reply)
      } else if (p.status === 'answered' || p.status === 'declined') status = '咨询回执待核对'
      add('原因', p.reason)
      note = '咨询意见不代表工具执行或验收通过。引用依据可在下方展开。'
      break
    case 'conversation.received':
      add('顾客消息', p.instruction)
      note = '收到消息不代表已修改目标或启动执行。'
      break
    case 'conversation.started':
      number('关联消息', p.conversation_seq)
      note = '结合上下文判断如何回应，不执行任务工具。'
      break
    case 'conversation.finished':
      status = label(p.status, { answered: '已回应', finished: '已处理', clarification: '需要澄清', applied: '已处理', resolved: '已理解', outdated: '背景已变化', invalid: '需要澄清', blocked: '暂未处理', uncertain: '处理结果待确认' })
      add('回应', p.reply)
      number('关联消息', p.conversation_seq)
      break
    case 'task.created': case 'mission.created':
      add('工作目标', p.goal); add('执行目标', p.objective); list('验收要求', p.acceptance); break
    case 'task.interpreted':
      add('任务理解', p.summary); add('工作目标', p.objective); list('工作范围', p.scope); list('验收要求', p.acceptance); list('待关注问题', p.risks); break
    case 'task.decomposed': case 'plan.proposed': case 'plan.amended':
      add('调整原因', p.reason); assignments(p.subtasks); break
    case 'work.plan_created':
      status = '已安排'; assignments(p.items); note = '工作计划不代表成员已执行或成果已验收。'; break
    case 'subtask.assigned':
      status = label(p.status ?? 'assigned', { assigned: '已指派', removed: '已移出本轮安排' })
      add('工作目标', p.objective); add('负责人', member(p.owner)); add('复核人', member(p.reviewer)); add('验收要求', p.acceptance); break
    case 'planning.started': case 'planning.requested':
      status = event.type === 'planning.started' ? '规划中' : '等待规划'; add('规划原因', p.reason); break
    case 'planning.finished':
      status = label(p.status, { installed: '计划已下发', needs_input: '等待补充信息', stale: '计划版本已过期', invalid: '计划未通过检查', blocked: '规划受阻', uncertain: '规划结果待确认' })
      add('结果说明', p.reason); number('关联计划事件', p.plan_seq); break
    case 'work.claimed':
      status = '已领取'; note = '已领取工作，尚无本次执行结果。'; break
    case 'work.submitted':
      status = '待验收'; note = '成果已提交，不代表运行通过或验收完成。'
      if (Array.isArray(p.artifact_seqs)) add('成果事件', lines(p.artifact_seqs.filter(seq => typeof seq === 'number' && Number.isInteger(seq) && seq > 0).map(seq => `#${seq}`)))
      break
    case 'work.reviewed':
      status = label(p.status, reviewStates); add('复核说明', p.reason); checks(p.checks)
      note = '这是该工作项的阶段结论，不代表整件任务已交付。'; break
    case 'work.failed':
      status = p.uncertain === true ? '执行结果待确认' : p.uncertain === false ? '执行失败' : '结果待核对'
      add('失败原因', p.reason); break
    case 'work.invalidated': case 'work.review_deferred':
      status = event.type === 'work.invalidated' ? '需重新核对' : '复核暂缓'; add('原因', p.reason); list('受影响工作项', p.affected); break
    case 'work.check_started': case 'delivery.check_started':
      status = '检查中'; add('检查项', p.criterion_id); note = '检查已开始，尚无本次检查的完成回执。'; break
    case 'delivery.prepared':
      status = '待整体验收'; add('整体复核人', member(p.reviewer))
      number('交付文件数', Array.isArray(record(p.manifest).files) ? (record(p.manifest).files as unknown[]).length : undefined)
      break
    case 'delivery.review_started': status = '整体验收中'; break
    case 'delivery.review_finished': {
      status = label(p.status, deliveryStates); add('验收说明', p.reason); checks(p.checks)
      const handoff = record(p.handoff)
      add('交付说明', handoff.summary); list('复查步骤', handoff.reproduce); list('未覆盖范围', handoff.limits); break
    }
    case 'verification.planning_started':
      status = '准备检查方案'; add('检查项', p.criterion_id)
      note = '方案准备不代表浏览器已经执行，也不代表成果通过。'; break
    case 'verification.planned':
      status = label(p.status, { correcting: '方案校正中', ready: '方案已准备', unsupported: '检查能力缺口', invalid: '检查方案无效', unavailable: '检查规划不可用', stale: '检查依据已变化' })
      add('检查项', p.criterion_id); add('方案说明', p.reason); browserPlan(p.plan)
      note = p.status === 'correcting' ? '检查方案正在校正，尚未执行。请以后续方案及实际检查回执为准。'
        : p.status === 'ready' ? '方案不是执行回执；实际结果以本机检查为准。' : '本次未按该方案执行检查，不能据此认定成果错误。'
      break
    case 'verification.completed': {
      const trusted = event.actor.kind === 'system' && event.actor.id === 'local_verifier' && event.actor.role === 'local_verifier' && p.source === 'explicit_local_cli'
      const complete = record(p.stdout).complete === true && record(p.stderr).complete === true
      const bound = Boolean(p.work_item_id || p.delivery_digest)
      const command = p.command
      const validCommand = Array.isArray(command) && command.length > 0 && command.every(arg => typeof arg === 'string') && Boolean(command[0].trim())
      const passed = p.outcome === 'passed' && p.exit_code === 0 && validCommand && complete && !p.launch_error && !p.cleanup_error && (!bound || p.artifacts_unchanged === true)
      status = !trusted ? '回执来源待核对' : passed ? '命令通过' : p.outcome === 'passed' ? '回执信息不足或不一致'
        : label(p.outcome, { failed: '命令失败', timeout: '执行超时', launch_error: '无法启动', unavailable: '检查工具不可用' }, '检查结果待核对')
      note = p.outcome === 'unavailable' ? '检查工具不可用或回执不完整，不能据此认定成果错误。' : '仅说明这条命令的记录结果，不代表整项任务通过，也不代表应用已部署。'
      browserPlan(p.browser_plan)
      if (validCommand) {
        add('命令程序', command[0]); add('命令参数', lines(command.slice(1).map((arg, i) => `${i + 1}. ${arg || '（空参数）'}`), 16))
      } else add('命令', '命令未记录或格式无效。')
      add('工作目录', p.cwd); add('检查项', p.criterion_id)
      if (typeof p.exit_code === 'number' && Number.isInteger(p.exit_code)) number('退出码', p.exit_code)
      else add('退出码', p.exit_code === null ? '未取得退出码' : '未记录有效退出码')
      number('耗时', p.duration_ms, ' 毫秒')
      add('启动失败原因', p.launch_error); add('进程清理异常', p.cleanup_error)
      if (typeof p.artifacts_unchanged === 'boolean') add('成果正文', p.artifacts_unchanged ? '检查期间未改变' : '检查期间发生变化')
      for (const [key, name] of [['stdout', '标准输出'], ['stderr', '错误输出']]) {
        const output = record(p[key])
        if (output.complete === false || output.truncated === true) add(`${name}采集`, [output.complete === false ? '采集未完成' : '', output.truncated === true ? '输出已截断' : ''].filter(Boolean).join('；'))
      }
      break
    }
    case 'agent.working': status = '处理中'; add('当前工作', p.summary); note = '这是一条进度记录，不是工具执行回执。'; break
    case 'msg.said': case 'msg.challenge':
      add('成员说明', text(p.text) || p.claim); add('沟通对象', member(p.target)); note = '成员自述不等于实际执行或验收结果。'; break
    case 'deliverable.published':
      status = '已发布'; add('成果名称', p.title); add('成果摘要', p.summary); add('文件名', p.filename)
      add('成果来源', label(p.source, { local_demo: '演示样例', external: '成员发布' }, '来源未记录'))
      note = '发布正文不代表程序已经运行或部署。'; break
    case 'diagnosis.recorded':
      add('观察到的问题', p.symptom); add('原因判断', p.cause); list('观察依据', p.observations); list('验证计划', p.verification_plan)
      note = '诊断与验证计划不等于已确认的执行结果。'; break
    case 'handoff.published':
      add('交接摘要', p.summary); add('处理原因', p.why); list('改动', p.changed); list('成员报告的检查', p.verification); list('尚未覆盖', p.limits); break
    case 'human.commanded':
      add('用户要求', p.instruction); add('新工作目标', p.goal); list('验收要求', p.acceptance)
      status = label(p.intent, { status: '查询进度', resume: '请求继续', direction: '调整要求' }, '已记录指令'); break
    case 'human.decided':
      status = label(p.resolution, { done: '人工确认完成', rework: '要求返工', pause: '要求暂停' }, '人工决定已记录'); add('决定原因', p.reason); break
    case 'task.progress_reported': add('进度说明', p.text); break
    case 'question.asked': case 'clarification.requested':
      status = '等待回复'; add('问题', p.question); add('相关成员', member(p.role_id)); break
    case 'question.started': status = '讲解中'; number('提问事件', p.question_seq); break
    case 'question.finished':
      status = label(p.status, { answered: '已回答', insufficient: '依据不足', blocked: '讲解受阻', uncertain: '回答结果待确认', invalid: '回答未通过来源核对' })
      add('回答', p.answer); add('原因', p.reason); list('回答边界', p.limits); number('提问事件', p.question_seq); break
    case 'activity.queued': case 'activity.completed': case 'activity.failed':
      status = event.type === 'activity.queued' ? '排队中' : event.type === 'activity.failed' ? '本轮失败' : '本轮结束'
      add('协作安排', label(p.action, { run: '推进工作', battle: '联合讨论' }, '安排待核对')); add('失败原因', p.message)
      if (event.type === 'activity.completed') note = '本轮结束不代表工作已验收或应用已部署。'
      break
    case 'driver.failed':
      status = '调用失败'; add('失败原因', p.message); number('调用次数', p.attempt)
      if (p.circuit_open === true) add('后续调用', '该模型通道已暂停调用')
      break
    case 'driver.retry_scheduled': case 'driver.retry_finished': {
      const trusted = event.actor.kind === 'system' && event.actor.id === 'driver_gateway' && event.actor.role === 'driver_gateway'
      status = !trusted ? '回执来源待核对' : event.type === 'driver.retry_scheduled' ? '等待自动续试'
        : label(p.status, { succeeded: '模型调用已恢复', failed: '续试仍失败，已保留检查点', cancelled: '续试等待已取消' })
      add('相关伙伴', member(p.role)); add('模型通道', p.driver); add('原因', p.reason)
      if (event.type === 'driver.retry_scheduled') {
        number('原失败事件', p.failure_seq); number('本次续试', p.retry_index); number('续试上限', p.retry_limit)
        number('等待时长', p.delay_seconds, ' 秒')
        if (typeof p.not_before === 'number' && Number.isFinite(p.not_before)) {
          const date = new Date(p.not_before * 1000)
          if (Number.isFinite(date.getTime())) add('最早续试时间', date.toLocaleString('zh-CN', { hour12: false }))
        }
        add('调用阶段', label(p.phase, { work: '执行工作', planning: '整理计划', review: '阶段复核', delivery: '整体交付', delivery_review: '整体验收', conversation: '回应消息', question: '回答提问', explanation: '讲解成果' }, '其他调用阶段'))
        note = '等待期间仍可发消息或暂停任务；安排不代表已经发出续试请求。'
      } else {
        number('续试安排事件', p.request_seq)
        note = '仅记录本次模型续试结果，不代表任务完成或验收通过；关联安排以审计记录为准。'
      }
      break
    }
    case 'driver.budget_exhausted': status = '调用受阻'; number('预算', p.budget); number('已使用', p.used_tokens); break
    case 'brief.generated': add('本轮分歧', record(p.brief).dispute); break
    case 'cabinet.voted': add('建议原因', record(p.tally).reason); break
    case 'rule.recorded': case 'rule.updated': add('规则内容', p.text); break
    case 'project.created': case 'room.created': add('名称', p.name); break
    default: break
  }
  return { fields, status, note }
}

export function EventDetails({ event: original, actorName, contextEvents = [] }: EventDetailsProps) {
  const event = projectHistoryReadArguments(readArguments(original, contextEvents), contextEvents)
  const { fields, status, note } = describe(event, actorName)
  const displaySeq = positiveSeq(event.seq) ? event.seq : '待核对'
  const developerBase = hasCorrectionReference(event) ? { seq: positiveSeq(event.seq) ? event.seq : null, type: event.type } : event
  // Model and run diagnostics never expose provider metadata or raw exceptions, even in developer details.
  const developerEvent = ['team.response_correction_requested', 'team.review_followup_requested'].includes(event.type)
    ? { seq: positiveSeq(event.seq) ? event.seq : null, type: event.type, payload: { fields, status, note } }
    : event.type === 'team.model_called' ? { ...developerBase, payload: { ...usageSummary([event]).calls[0], ...correctionReference(event) } }
    : event.type === 'team.run_finished' ? { ...developerBase, payload: isTeamHost(event) ? { ...responseDiagnostics(event.payload, true), requestFailure: requestFailureFields(event.payload), ...correctionReference(event) } : {} } : event
  const title = label(event.type, titles, '事件记录')
  const recorder = resolvedName(event.actor.id, actorName) || label(event.actor.kind, { human: '用户', system: '系统', agent: '成员' }, '记录来源待核对')
  const nativeBrowser = ['team.tool_started', 'team.tool_finished'].includes(event.type) && (event.payload.name ?? event.payload.tool) === 'browser_check'
  const nativeMcp = ['team.tool_started', 'team.tool_finished'].includes(event.type) && isNativeMcpTool(event.payload.name ?? event.payload.tool)
  const projectHistory = ['team.tool_started', 'team.tool_finished'].includes(event.type) && (event.payload.name ?? event.payload.tool) === 'read_project_history'
  const projectRestore = ['team.tool_started', 'team.tool_finished'].includes(event.type) && (event.payload.name ?? event.payload.tool) === 'workspace_restore_project_file'
  const artifactRead = ['team.tool_started', 'team.tool_finished'].includes(event.type) && (event.payload.name ?? event.payload.tool) === 'read_artifact'
  const frozenArchive = ['team.tool_started', 'team.tool_finished'].includes(event.type) && isFrozenArchiveReceipt(event)
  const receiptRead = hasReceiptReadMetadata(event)
  const browser = nativeBrowser || event.type === 'verification.completed' && hasBrowserReceipt(event.payload)
  const outputs = event.type === 'verification.completed' ? event.payload
    : ['team.tool_finished', 'workspace.tool_finished'].includes(event.type) && (event.payload.name ?? event.payload.tool) === 'workspace_run' ? record(event.payload.result) : undefined
  return <section aria-label={`事件 #${displaySeq} 详情`} className="min-w-0 space-y-3 text-xs leading-5 text-[#52695e] [overflow-wrap:anywhere]">
    <header className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0"><h3 className="text-sm font-semibold text-[#294a40]">{title}</h3><p className="text-[#728679]">事件 #{displaySeq} · {recorder}</p></div>
      <span className="shrink-0 font-semibold">{status}</span>
    </header>
    {fields.length > 0 && <dl className="space-y-2">{fields.map((field, index) => <div key={index} className="min-w-0 sm:grid sm:grid-cols-[7rem_minmax(0,1fr)] sm:gap-3">
      <dt className="font-semibold">{field.label}</dt><dd className="min-w-0 whitespace-pre-wrap">{field.value}</dd>
    </div>)}</dl>}
    {event.type === 'verification.completed' && browser && <BrowserReport report={event.payload.browser_report} />}
    {outputs && <CommandOutputs stdout={browser ? undefined : outputs.stdout} stderr={nativeBrowser ? undefined : outputs.stderr} />}
    <NativeArtifactVersionsReceipt key={event.seq} event={event} />
    {event.type === 'team.tool_finished' && (event.payload.name ?? event.payload.tool) === 'browser_check' && <NativeBrowserReceipt event={event} />}
    {skillTool(event) && event.type === 'team.tool_finished' && <NativeSkillReceipt event={event} />}
    {nativeMcp && event.type === 'team.tool_finished' && <NativeMcpReceipt key={event.seq} event={event} />}
    {projectHistory && event.type === 'team.tool_finished' && <NativeProjectHistoryReceipt key={event.seq} event={event} />}
    {projectRestore && event.type === 'team.tool_finished' && <NativeProjectRestoreReceipt key={event.seq} event={event} />}
    {artifactRead && event.type === 'team.tool_finished' && <NativeArtifactReceipt key={event.seq} event={event} />}
    {frozenArchive && event.type === 'team.tool_finished' && <NativeFrozenArchiveReceipt event={event} />}
    {receiptRead && event.type === 'team.tool_finished' && <NativeReceiptRead event={event} />}
    {note && <p className="text-[#728679]">{note}</p>}
    {event.type === 'collaboration.finished' && ['answered', 'declined'].includes(String(event.payload.status))
      && event.actor.kind === 'system' && event.actor.id === 'collaboration' && event.actor.role === 'collaboration'
      && typeof event.payload.speaker_id === 'string' && event.payload.speaker_id === event.payload.recipient && Boolean(text(event.payload.reply)) && Array.isArray(event.payload.evidence)
      && event.payload.evidence.length > 0 && <details>
        <summary className="cursor-pointer font-semibold">咨询引用依据</summary>
        {event.payload.evidence.filter((entry) => entry && Number.isInteger(entry.artifact_seq) && entry.artifact_seq > 0 && typeof entry.quote === 'string')
          .map((entry, index) => <blockquote key={index} className="mt-2 whitespace-pre-wrap">成果 #{entry.artifact_seq}：{entry.quote}</blockquote>)}
      </details>}
    {!skillTool(event) && !browser && !nativeMcp && !projectHistory && !projectRestore && !artifactRead && !frozenArchive && !receiptRead && <details key={`${displaySeq}:${event.type}`} className="border-t border-[#dce8e1] pt-2">
      <summary className="cursor-pointer text-[#728679]">开发者详情</summary>
      <pre tabIndex={0} aria-label={developerEvent === event ? '原始事件 JSON' : '安全事件 JSON'} className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px]">{JSON.stringify(developerEvent, null, 2)}</pre>
    </details>}
  </section>
}
