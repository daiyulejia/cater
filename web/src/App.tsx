import { lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { hasBrowserReceipt, outputSummaryLines } from './outputPreview'
import { replyEvidenceFor } from './replyEvidence'
import type { ReplyEvidence } from './replyEvidence'
import { CommandOutputs } from './CommandOutputPreview'
import type { ComponentProps, ReactNode } from 'react'
import {
  ArrowDown, Cat, CheckCircle2, ChevronDown, CircleHelp,
  CornerDownRight, FileText, GitBranch, GitFork, Info, LoaderCircle, PawPrint, Plus, RefreshCw, Search, ShieldCheck, TriangleAlert,
} from 'lucide-react'
import catPortraits from './assets/cat-portraits.jpg'
import staffPortraits from './assets/staff-portraits.jpg'
import cafeWelcome from './assets/cafe-welcome-v2.jpg'
import type { Artifact } from './Artifact'
import { WorkboardPanel } from './WorkboardPanel'
import { MentionInput } from './MentionInput'
import { TeamDirectory } from './TeamDirectory'
import { TaskList } from './TaskList'
import { conversationTitle } from './conversationTitle'
import { InstanceInfo } from './InstanceInfo'
import { TaskGraphDialog } from './TaskGraphDialog'
import { RulesPanel } from './RulesPanel'
import { WorkItems } from './WorkItems'
import { ConversationMembers } from './ConversationMembers'
import { ChatConversation } from './ChatConversation'
import { ConversationOrigin, DerivationDraftBar } from './ConversationOrigin'
import type { ConversationBackground } from './ConversationOrigin'
import { QuestionComposerBar } from './QuestionComposerBar'
import { consultationMatches, conversationParticipants } from './participants'
import type { WorkState } from './WorkItems'
import type { TeamAvailability } from './TeamDirectory'
import type { Clarification, ClarificationControls, ClarificationStore } from './ClarificationCard'
import type { Questionnaire, QuestionnaireControls, QuestionnaireStore } from './QuestionnaireCard'
import type { QuestionBasis, QuestionContext } from './QuestionDetails'
import { collaborationProjection, workTransfers } from './collaborationProjection'
import type { WorkAssignment } from './collaborationProjection'
import { readJsonResponse, request } from './request'
import { TaskControlMenu } from './TaskControlMenu'
import { DeleteConversationDialog } from './DeleteConversationDialog'
import { ChatFilters } from './ChatFilters'
import type { ChatFilter } from './ChatFilters'
import { WorkspacePanel, WorkspaceToolProgress } from './WorkspacePanel'
import type { WorkspaceBinding, WorkspaceOption } from './WorkspacePanel'
import { controlLabels, executionLabel } from './executionControl'
import type { ControlAction, ExecutionControl } from './executionControl'
import { clearCreationRecovery, CREATION_RECOVERY_TTL, loadCreationRecovery, readAccessToken, saveCreationRecovery } from './creationRecovery'
import type { CreationAttempt } from './creationRecovery'
import { compareTeamSnapshots as summaryOrder, hasActiveTeamMessages, hasNewTeamObjective, isTeamHost, isTeamRecovery, resolveTeam, resolveTeamActivity, resolveTeamTask, teamDeliveries, teamMessageAudience, teamMessageEchoes, teamProgressMessages, teamRunStatus, teamSpeaker, teamStatus } from './team'
import type { TeamState, TeamSummary } from './team'
import { AsyncPanel } from './AsyncPanel'
import { ReloadContext } from './pageReloadContext'
import { loadPageRecovery, openNavigationMemory } from './pageRecovery'
import type { NavigationMemory, PageDraftSnapshot } from './pageRecovery'
import { reloadBlocker, reloadPage } from './pageReload'

const TaskGraph = lazy(() => import('./TaskGraph').then((module) => ({ default: module.TaskGraphContents })))
const LazyDeliveryPanel = lazy(() => import('./DeliveryPanel').then((module) => ({ default: module.DeliveryPanel })))
const EventDetails = lazy(() => import('./EventDetails').then((module) => ({ default: module.EventDetailsList })))
const BrowserReport = lazy(() => import('./browserReport').then((module) => ({ default: module.BrowserReport })))
const WorkProgressPanel = lazy(() => import('./WorkProgressPanel').then((module) => ({ default: module.WorkProgressPanel })))
const WorkAssignments = lazy(() => import('./WorkProgressPanel').then((module) => ({ default: module.WorkAssignments })))
const ClarificationCard = lazy(() => import('./ClarificationCard').then((module) => ({ default: module.ClarificationCard })))
const QuestionnaireCard = lazy(() => import('./QuestionnaireCard').then((module) => ({ default: module.QuestionnaireCard })))
type ChatControls = ClarificationControls & { questionnaire: QuestionnaireControls }
const QuestionDetails = lazy(() => import('./QuestionDetails').then((module) => ({ default: module.QuestionDetails })))
const LazyArtifactPreview = lazy(() => import('./Artifact').then((module) => ({ default: module.ArtifactPreview })))
const TeamWorkboard = lazy(() => import('./TeamProgressPanel').then((module) => ({ default: module.TeamWorkboard })))
const ConversationActivity = lazy(() => import('./ConversationActivity').then((module) => ({ default: module.ConversationActivity })))
const ChatMarkdown = lazy(() => import('./ChatMarkdown').then((module) => ({ default: module.ChatMarkdown })))
const MessageDetails = lazy(() => import('./MessageDetails').then((module) => ({ default: module.MessageDetails })))
const WelcomeView = lazy(() => import('./WelcomeView').then((module) => ({ default: module.WelcomeView })))

type Assignment = { id: string; owner: string; role: string; lane: string; objective: string; acceptance?: string; deliverable?: string; status: string }
type Message = { seq: number; actor: string; role: string; type: string; claim?: string; text?: string; stance?: string; target?: string; reply_to?: number }
type Deliverable = Artifact
type Diagnosis = { seq: number; status: 'hypothesis' | 'reported' | 'confirmed' | 'uncertain'; symptom: string; cause: string; observations?: string[]; alternatives?: string[]; impact?: string[]; verification_plan?: string[]; message_seq?: number }
type Handoff = { seq: number; summary: string; why: string; changed?: string[]; verification?: string[]; limits?: string[]; teach_back?: string[]; message_seq?: number }
type Verification = { seq: number; command: string[]; outcome: 'passed' | 'failed' | 'timeout' | 'launch_error' | 'unavailable'; exit_code: number | null; for_event?: number; git?: { head?: string | null; dirty?: boolean | null }; stdout?: { bytes?: number; sha256?: string; excerpt?: string }; stderr?: { bytes?: number; sha256?: string; excerpt?: string } }
type ChainStatus = { ok: boolean; checked: number; reason?: string; broken_at?: number }
type DisciplineFlag = { seq: number; role?: string; message_seq?: number; issues?: string[]; action?: string }
type MemoryItem = { seq: number; task_id?: string; actor?: string; title: string; summary: string; kind?: string }
type OrgRole = { id: string; name: string; role: string; charter?: string }
type DriverCoverage = { role_id: string; name: string; driver: string; mode: 'local_demo' | 'external' }
type Organization = { leader: OrgRole; cabinet: OrgRole[]; workers: OrgRole[]; mode?: 'local_demo' | 'mixed_driver' | 'external_driver'; worker_drivers?: DriverCoverage[]; availability?: TeamAvailability }
type Activity = { id?: string; action?: string; state: 'idle' | 'queued' | 'running' | 'completed' | 'failed'; active: boolean; revision?: number; instance_id?: string; error?: string; follow_up?: { action?: string } }
type TaskSummary = { id: string; goal: string; project_id?: string | null; status: string; status_label: string; conversation_count?: number; latest_seq: number; version: number; evaluated_at?: number; observation_order?: number; activity?: Activity; execution_control?: ExecutionControl; team?: TeamSummary | null }
type Task = {
  team?: TeamState | null
  work?: WorkState | null
  project_id?: string | null
  id: string; goal: string; acceptance: string[]; status: string; messages: Message[]; assignments: Assignment[]
  deliverables: Deliverable[]
  diagnoses?: Diagnosis[]; handoffs?: Handoff[]; verifications?: Verification[]
  discipline_flags?: DisciplineFlag[]
  budget_exhaustions?: { seq: number; driver?: string; budget?: number; used_tokens?: number }[]
  activity?: Activity
  decision?: { resolution?: string; reason?: string; source?: string }
  brief?: { dispute?: string; consensus?: string[] }; mission?: { goal?: string }; cabinet?: { status?: string; reason?: string }
  interpretation?: { summary?: string; objective?: string; scope?: string[]; risks?: string[]; basis?: string[]; complexity?: string }
  route?: { route?: string; reason?: string }
}
type Event = { seq: number; ts?: string; type: string; category: string; actor: { id: string; role: string; kind: string }; payload: Record<string, unknown> }
type QuestionResult = { question_seq?: number; answer?: string; evidence?: { source_seq: number; quote: string }[]; limits?: string[]; reason?: string; speaker_id?: string; driver?: string; tokens?: number; token_usage?: string }
type Question = QuestionResult & { seq: number; status: string; question: string; role_id: string; basis: QuestionBasis; outdated: boolean }
type ConversationReceipt = { seq: number; status: string }
export type TaskDetail = { task: Task; team?: TeamState | null; events: Event[]; questions?: Question[]; clarifications?: Clarification[]; questionnaires?: Questionnaire[]; conversation?: ConversationReceipt[]; summary?: TaskSummary; background?: ConversationBackground | null; organization_memory?: MemoryItem[]; review: { health?: string; summary?: string } | null; activity?: Activity; execution_control?: ExecutionControl; workspace?: WorkspaceBinding }
type DerivationSource = { id: string; goal: string }
export type ChatItem = { replyEvidence?: ReplyEvidence; internal?: boolean; historicalDelivery?: boolean; id: number; actor: string; display: string; role: string; kind: 'human' | 'agent' | 'system'; systemActor: boolean; category: 'collaboration' | 'decision' | 'deliverable' | 'system'; title?: string; text: string; details?: string[]; progress?: Event[]; question?: QuestionContext; clarification?: Clarification; questionnaire?: Questionnaire; notice?: boolean; joined?: string[]; assignments?: WorkAssignment[]; handoff?: { owners: string[]; reviewers: string[] }; targets: string[]; replyTo?: number; evidence: Event; retryStatus?: 'waiting' | 'succeeded' | 'failed' | 'cancelled'; auditEvents?: Event[]; auditName?: (id: string) => string }

const accessToken = readAccessToken()

const apiRead = <T,>(path: string, read: (response: Response) => Promise<T>, init?: RequestInit): Promise<T> => {
  const headers = new Headers({ 'Content-Type': 'application/json', ...(accessToken ? { 'X-Cater-Access-Token': accessToken } : {}) })
  new Headers(init?.headers).forEach((value, key) => headers.set(key, value))
  return request(path, { ...init, headers }, read)
}
const api = <T,>(path: string, init?: RequestInit): Promise<T> => apiRead(path, readJsonResponse<T>, init)
const loadWorkspaceOptions = () => api<WorkspaceOption[]>('/api/workspaces')
const loadInstance = () => api<unknown>('/api/instance')

function validateTaskDetail(value: TaskDetail, expectedId?: string): void {
  if (typeof value?.task?.id !== 'string' || !value.task.id || (expectedId && value.task.id !== expectedId)
    || typeof value.task.goal !== 'string' || typeof value.task.status !== 'string'
    || !Array.isArray(value.events) || !value.events.every(event => event && Number.isSafeInteger(event.seq) && event.seq > 0
      && typeof event.type === 'string' && event.actor && typeof event.actor.id === 'string'
      && (event.actor.kind === undefined || typeof event.actor.kind === 'string')
      && (event.actor.role === undefined || typeof event.actor.role === 'string')
      && event.payload && typeof event.payload === 'object' && !Array.isArray(event.payload))
    || !Array.isArray(value.task.messages)
    || !Array.isArray(value.task.assignments) || !Array.isArray(value.task.deliverables)
    || [value.questions, value.clarifications, value.questionnaires, value.conversation, value.organization_memory]
      .some(items => items !== undefined && !Array.isArray(items))
    || (value.summary != null && (value.summary.id !== value.task.id || typeof value.summary.goal !== 'string'
      || typeof value.summary.status !== 'string' || typeof value.summary.status_label !== 'string'
      || [value.summary.version, value.summary.latest_seq]
        .some(clock => !Number.isSafeInteger(clock) || clock < 0)
      || typeof value.summary.conversation_count !== 'number'
      || !Number.isSafeInteger(value.summary.conversation_count) || value.summary.conversation_count < 0
      || (value.summary.evaluated_at !== undefined && (!Number.isFinite(value.summary.evaluated_at) || value.summary.evaluated_at < 0))
      || (value.summary.activity?.revision !== undefined && (!Number.isSafeInteger(value.summary.activity.revision) || value.summary.activity.revision < 0))
      || (value.summary.team?.revision !== undefined && (!Number.isSafeInteger(value.summary.team.revision) || value.summary.team.revision < 0))))) {
    throw new Error('提交响应不完整或对话不匹配，提交结果未知。草稿已保留，请核对后手动重试。')
  }
}

function creationRequestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  // LAN workrooms may run over HTTP, where randomUUID is unavailable.
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64
  bytes[8] = (bytes[8] & 63) | 128
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function userFacingError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message.trim() : ''
  const known: Record<string, string> = {
    'access token is required': '这个工作台需要访问令牌。请使用带 #access_token=… 的地址重新打开。',
    'goal is required': '先告诉店长这次想完成什么，再发送。',
    'a collaboration activity is already running for this task': '这项任务正在协作中，已避免重复启动；可以继续补充方向。',
    'choose either run for normal collaboration or battle for forced governance': '一次只能选择正常协作或请伙伴复核其中一种方式。',
  }
  if (known[message]) return known[message]
  if (message.startsWith('unknown role ')) return '点名的伙伴不存在，请从当前猫咖成员中选择。'
  if (/^(goal|instruction|assignment objective|assignment acceptance) exceeds \d+ character limit$/.test(message)) return '这段内容太长了，请缩短后再发送。'
  if (message.startsWith('acceptance exceeds ')) return '验收要求太多了，请保留最重要的几条。'
  return message || fallback
}

const statusText = (value?: string) => ({
  pending: '待开始', running: '进行中', rework: '需返工', decided: '已定下', done: '已完成', escalated: '待处理',
  assigned: '已指派', accepted: '已定下', rejected: '未通过', recommended: '已建议', vetoed: '已否决', needs_supreme: '待最高决策', needs_human: '待人工处理',
}[value ?? ''] ?? value ?? '未知')

const actorText = (value: string) => ({ human: '你', system: '系统', orchestrator: '店长', leader: '店长', cabinet: '铲屎官们', supreme: '最高决策者', store_manager: '店长', reviewer: '复盘员', driver_gateway: '驱动网关', cabinet_product: '白露', cabinet_engineering: '阿程', cabinet_quality: '知夏', caretaker_product: '白露', caretaker_engineering: '阿程', caretaker_quality: '知夏', product: '月亮', design: '罐头', dev: '米寇', test: '三角' }[value] ?? value)

const activityText = (activity?: Activity) => {
  if (activity?.active && activity.follow_up?.action === 'run') return '已收下新方向，当前轮结束后继续'
  if (activity?.state === 'queued') return '协作已排队'
  if (activity?.state === 'running') return '正在协作'
  if (activity?.state === 'failed') return '协作失败'
  return ''
}

const taskListStatus = (task: Task, activity = task.activity) => {
  if (task.team?.enabled) return teamStatus(task.team, activity)
  const activeLabel = activityText(activity)
  if (activeLabel) return activeLabel
  if (task.work?.items.length) {
    if (task.work.items.some((item) => item.status === 'uncertain')) return '结果待核实'
    if (!task.work.scope_current) return '需要重新核对'
    if (task.work.delivery?.status === 'accepted') return '已交付'
    if (task.work.delivery?.status === 'uncertain') return '复核结果待确认'
    if (task.work.stage_accepted) return '阶段通过，待整体验收'
    if (task.work.items.some((item) => item.status === 'submitted')) return '等待验收'
    return '待补充成果'
  }
  return task.status === 'running' && task.brief ? '等待下一轮' : statusText(task.status)
}

const driverModeText = (mode?: Organization['mode']) => mode ? { local_demo: '本地演示模式', mixed_driver: '部分成员已接入外部驱动', external_driver: '已接入外部驱动' }[mode] : '模式待确认'
const driverModeStyle = (mode?: Organization['mode']) => mode ? { local_demo: 'bg-amber-50 text-amber-800', mixed_driver: 'bg-sky-50 text-sky-800', external_driver: 'bg-teal-50 text-teal-800' }[mode] : 'bg-stone-50 text-stone-600'

function reasonText(value: string): string {
  const veto = /^Veto role\(s\) raised concern: (.+)\.$/.exec(value)
  if (veto) return `有伙伴提出了风险异议：${veto[1].split(', ').map((role) => `@${actorText(role)}`).join('、')}。`
  if (value === 'Agents exchanged contributions; governance selected the next action when needed.') return '伙伴们交换了看法，并已确定下一步怎么推进。'
  if (value === 'Agents collaborated without material disagreement; governance was not needed.') return '伙伴们没有出现实质分歧，这一轮可以顺畅推进。'
  if (value === 'governance round forced by caller') return '顾客希望多请几位伙伴一起确认。'
  if (value === '任务可先由总控完成轻量分解，再由执行层协作推进。') return '店长先把任务安排好，再请伙伴们一起推进。'
  const recommended = /^Option (.+) reached approval threshold (\d+)\.$/.exec(value)
  if (recommended) return `方案 ${recommended[1]} 达到 ${recommended[2]} 票通过门槛。`
  return value
}

function disciplineText(issues?: string[]): string {
  const labels: Record<string, string> = {
    invalid_rule_proposal: '店规建议缺少有效依据或已存在',
    invalid_stance: '表达格式不完整', missing_claim: '没有写清结论', challenge_without_evidence: '质疑缺少可复查依据',
    invalid_target: '点名对象不存在', invalid_reply_to: '回复没有对应到可见发言', target_without_visible_reply: '点名缺少回复依据', reply_without_target: '回复缺少明确对象',
  }
  const values = [...new Set((issues ?? []).map((item) => labels[item] ?? '协作格式需要补充'))]
  return values.length ? values.join('、') : '协作格式需要补充'
}

const diagnosisStatusText = (status: Diagnosis['status']) => ({ hypothesis: '待证实假设', reported: '成员称已证实，待独立核验', confirmed: '成员称已证实，待独立核验', uncertain: '仍不确定' }[status])

function Portrait({ id, size = 'md' }: { id: string; size?: 'sm' | 'md' | 'lg' | 'xl' }) {
  const catPosition: Record<string, string> = { product: '0% 0%', test: '100% 0%', dev: '0% 100%', design: '100% 100%' }
  const staffPosition: Record<string, string> = {
    store_manager: '0% 0%', orchestrator: '0% 0%', leader: '0% 0%',
    caretaker_product: '100% 0%', cabinet_product: '100% 0%',
    caretaker_engineering: '0% 100%', cabinet_engineering: '0% 100%',
    caretaker_quality: '100% 100%', cabinet_quality: '100% 100%',
  }
  const dimensions = ({ sm: 'size-8', md: 'size-10', lg: 'size-14', xl: 'size-24' }[size])
  const image = catPosition[id] ? catPortraits : staffPosition[id] ? staffPortraits : null
  const position = catPosition[id] ?? staffPosition[id]
  if (!image) return <span className={`grid ${dimensions} shrink-0 place-items-center rounded-full border-2 border-white bg-[#dce9e4] text-[#247468] shadow-sm`} aria-hidden="true"><Cat size={size === 'lg' ? 22 : 16} /></span>
  return <span className={`inline-block ${dimensions} shrink-0 rounded-full border-2 border-white shadow-[0_2px_7px_rgba(42,56,48,.16)]`} role="img" aria-label={actorText(id)} style={{ backgroundImage: `url(${image})`, backgroundSize: '200% 200%', backgroundPosition: position }} />
}

function publicIdentity(actorId: string, organization: Organization | null): string {
  if (actorId === 'human') return '顾客'
  if (actorId === organization?.leader.id || actorId === 'store_manager' || actorId === 'orchestrator') return ''
  if (organization?.cabinet.some((member) => member.id === actorId)) return '铲屎官'
  if (organization?.workers.some((member) => member.id === actorId)) return '猫咪'
  return actorId === 'cabinet' ? '' : '猫咖'
}

const legacySystemActors = new Set(['system', 'discipline', 'local_verifier', 'explainer', 'driver_gateway', 'work_scheduler', 'rulebook', 'rooms', 'reviewer', 'cabinet', 'orchestrator'])
function isSystemActor(actor: Event['actor'], identities: Record<string, OrgRole>): boolean {
  return !Object.hasOwn(identities, actor.id) && (actor.kind === 'system' || (!actor.kind && legacySystemActors.has(actor.id)))
}

function isResponseFormatError(event: Event): boolean {
  return event.type === 'msg.said' && event.payload.phase === 'response_format_error'
    && event.payload.speech_source === 'system_receipt' && event.payload.error_code === 'invalid_response'
}

function isDriverGateway(event: Event): boolean {
  return event.actor.kind === 'system' && event.actor.id === 'driver_gateway' && event.actor.role === 'driver_gateway'
}

function conversationFor(events: Event[], identities: Record<string, OrgRole>, organization: Organization | null, questions: Question[] = [], clarifications: Clarification[] = [], receipts: ConversationReceipt[] = [], teamEnabled = false, questionnaires: Questionnaire[] = []): ChatItem[] {
  const humanMessages = events.filter((event) => event.type === 'team.message_received' && event.actor.kind === 'human'
    && !['recovery', 'continuation'].includes(String(event.payload.kind)))
  const initialHuman = humanMessages[0]
  const humanRequests = new Set(humanMessages.map((event) => event.payload.request_id).filter((id) => typeof id === 'string' && id))
  const shownRequests = new Set<string>()
  const teamEchoes = teamMessageEchoes(events)
  const teamProgress = teamProgressMessages(events)
  const replyEvidence = replyEvidenceFor(events)
  const leader = organization?.leader.id ?? 'store_manager'
  const deliveries = teamDeliveries(events, leader)
  const latestTeamObjective = events.filter((event) => isTeamHost(event)
    && ['team.objective_updated', 'team.objective_completed'].includes(event.type)).sort((a, b) => b.seq - a.seq)[0]
  const hasAutonomousDecision = events.some((event) => event.type === 'agent.decided' || event.type === 'supreme.decided')
  const hasFinishedActivity = events.some((event) => event.type === 'activity.completed' || event.type === 'activity.failed')
  const questionRows = new Map(questions.map((question) => [question.seq, question]))
  const askedEvents = new Map(events.filter((event) => event.type === 'question.asked').map((event) => [event.seq, event]))
  const finishedQuestions = new Set(events.filter((event) => event.type === 'question.finished').map((event) => event.payload.question_seq))
  const conversationRows = new Map(receipts.map((row) => [row.seq, row]))
  const consultationRequests = new Map(events.filter((event) => event.type === 'collaboration.requested').map((event) => [event.seq, event]))
  const explainedMessages = new Set(events.filter((event) => event.type === 'conversation.finished').map((event) => event.payload.question_seq))
  const clarificationRows = new Map(clarifications.map((question) => [question.seq, question]))
  const questionnaireRows = new Map(questionnaires.map((question) => [question.seq, question]))
  const collaboration = collaborationProjection(events, organization?.leader.id ?? 'store_manager')
  const transfers = workTransfers(events)
  const displayName = (id: string) => identities[id]?.name ?? actorText(id)
  const retries = new Map<number, { scheduled: Event; failure?: Event; finished?: Event }>()
  const retryRecords = new Set<number>()
  const bySeq = new Map(events.map((event) => [event.seq, event]))
  // Bind in sequence order, even when a snapshot arrives out of order. The first valid terminal receipt wins.
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    if (!isDriverGateway(event)) continue
    const p = event.payload
    if (event.type === 'driver.retry_scheduled' && typeof p.role === 'string' && p.role.trim()
      && typeof p.driver === 'string' && p.driver.trim() && p.retry_index === 1 && p.retry_limit === 1
      && typeof p.delay_seconds === 'number' && Number.isFinite(p.delay_seconds) && p.delay_seconds >= 0
      && typeof p.failure_seq === 'number' && Number.isInteger(p.failure_seq) && p.failure_seq > 0 && p.failure_seq < event.seq) {
      const source = bySeq.get(p.failure_seq)
      const failure = source?.type === 'driver.failed' && isDriverGateway(source)
        && source.payload.role === p.role && source.payload.driver === p.driver ? source : undefined
      retries.set(event.seq, { scheduled: event, failure })
      if (failure) retryRecords.add(failure.seq)
    } else if (event.type === 'driver.retry_finished' && typeof p.request_seq === 'number') {
      const retry = retries.get(p.request_seq)
      if (retry && !retry.finished && event.seq > retry.scheduled.seq
        && p.role === retry.scheduled.payload.role && p.driver === retry.scheduled.payload.driver
        && typeof p.status === 'string' && ['succeeded', 'failed', 'cancelled'].includes(p.status)) {
        retry.finished = event
        retryRecords.add(event.seq)
      }
    }
  }
  return events.flatMap((event) => {
    const payload = event.payload
    const actor = event.actor
    const projection = collaboration.get(event.seq)
    const message = (title: string | undefined, text: string, kind: ChatItem['kind'], category: ChatItem['category'] = kind === 'agent' || kind === 'human' ? 'collaboration' : 'system', details?: string[]): ChatItem => ({
      id: event.seq, actor: actor.id, display: identities[actor.id]?.name ?? actorText(actor.id), role: publicIdentity(actor.id, organization), kind, systemActor: isSystemActor(actor, identities), category, title, text, evidence: event,
      details: details?.filter(Boolean),
      joined: projection?.joined.map(displayName),
      assignments: projection?.assignments?.map((item) => ({ ...item, owner: displayName(item.owner), reviewer: item.reviewer ? displayName(item.reviewer) : undefined })),
      handoff: projection?.handoff ? { owners: projection.handoff.owners.map(displayName), reviewers: projection.handoff.reviewers.map(displayName) } : undefined,
      targets: [
        ...(typeof payload.target === 'string' ? [payload.target] : []),
        ...(Array.isArray(payload.mentions) ? payload.mentions.filter((item): item is string => typeof item === 'string') : []),
      ].map((id) => identities[id]?.name ?? actorText(id)),
      replyTo: typeof payload.reply_to === 'number' ? payload.reply_to : undefined,
    })
    const memberNames = (value: unknown) => Array.isArray(value)
      ? value.filter((id): id is string => typeof id === 'string').map((id) => identities[id]?.name ?? actorText(id))
      : []
    if (teamEnabled && (event.type.startsWith('work.') || event.type.startsWith('planning.') || event.type.startsWith('plan.')
      || event.type.startsWith('delivery.') || event.type === 'task.decomposed' || event.type === 'subtask.assigned'
      || (event.type === 'brief.generated' && payload.reason === 'work_checkpoint'))) return []
    if (event.type === 'team.message_received') {
      if (payload.kind === 'recovery') {
        if (!isTeamRecovery(event) || typeof payload.text !== 'string' || !payload.text.trim()) return []
        return [{ ...message('恢复通知', payload.text, 'system'), actor: 'system', display: '系统', role: '',
          systemActor: true, internal: true, targets: [], replyTo: undefined }]
      }
      if (payload.kind === 'continuation' && actor.kind !== 'agent') return []
      if (actor.kind !== 'human' && (actor.kind !== 'agent' || (!['assignment', 'message', 'notification'].includes(String(payload.kind))
        && !(['report', 'continuation'].includes(String(payload.kind)) && payload.visible === true)))) return []
      if (typeof payload.text !== 'string' || !payload.text.trim()) return []
      if (actor.kind === 'human' && typeof payload.request_id === 'string' && payload.request_id) {
        if (shownRequests.has(payload.request_id)) return []
        shownRequests.add(payload.request_id)
      }
      const item = message(undefined, payload.text, actor.kind === 'human' ? 'human' : 'agent', 'collaboration')
      item.systemActor = false
      item.internal = payload.kind === 'continuation' || teamEnabled && teamMessageAudience(event, bySeq).audience === 'team'
      item.targets = typeof payload.recipient === 'string' ? [displayName(payload.recipient)] : []
      if (actor.kind === 'agent' && ['assignment', 'notification', 'continuation'].includes(String(payload.kind))) {
        item.notice = true
        item.text = `@${displayName(actor.id)} → ${item.targets.length ? `@${item.targets[0]}` : '伙伴'} · ${payload.kind === 'notification' ? '进展知会' : payload.kind === 'continuation' ? '接续工作交接' : '工作交接'}`
        item.details = [payload.text]
        item.targets = []
      } else if (actor.kind === 'human') {
        const run = events.filter((entry) => isTeamHost(entry) && ['team.run_started', 'team.run_finished'].includes(entry.type) && entry.payload.message_seq === event.seq).at(-1)
        if (run) item.details = [teamRunStatus(typeof run.payload.status === 'string' ? run.payload.status : run.type === 'team.run_started' ? 'running' : undefined)]
      }
      return [item]
    }
    if (event.type === 'team.questions_requested' && isTeamHost(event)) {
      const row = questionnaireRows.get(event.seq)
      if (!row) return []
      const item = message('补充信息', '', 'agent', 'collaboration')
      item.actor = row.role_id
      item.display = displayName(row.role_id)
      item.role = publicIdentity(row.role_id, organization)
      item.systemActor = false
      item.questionnaire = row
      return [item]
    }
    if (event.type === 'team.run_finished' && isTeamHost(event) && payload.status === 'failed') {
      const item = message('这次回复未完成', typeof payload.reason === 'string' ? payload.reason : '已有成果保留，可以继续发消息；本次没有确认完成。', 'system')
      item.actor = 'system'
      item.display = '系统'
      item.role = ''
      item.systemActor = true
      return [item]
    }
    if (event.type === 'team.objective_updated' || event.type === 'team.objective_completed') {
      if (!isTeamHost(event)) return []
      const complete = event.type === 'team.objective_completed'
      const final = complete ? deliveries.messages.get(event.seq) : undefined
      const title = complete ? event.seq === latestTeamObjective?.seq ? '店长最终交付' : '店长交付记录' : '任务目标已更新'
      const item = message(title, final ? String(final.payload.text) : typeof payload[complete ? 'summary' : 'goal'] === 'string' ? String(payload[complete ? 'summary' : 'goal']) : '', complete ? 'agent' : 'system', complete ? 'deliverable' : 'collaboration')
      item.systemActor = !complete
      if (complete) {
        item.historicalDelivery = event.seq !== latestTeamObjective?.seq
        item.actor = leader
        item.display = displayName(leader)
        item.auditName = displayName
        item.role = publicIdentity(leader, organization)
        item.targets = []
        item.replyTo = undefined
        if (typeof payload.verification === 'string') item.details = [payload.verification]
        if (final) item.auditEvents = [event, final]
      }
      return [item]
    }
    if (event.type === 'task.created' && initialHuman?.payload.text === payload.goal) return []
    if (['conversation.received', 'human.commanded'].includes(event.type) && humanRequests.has(payload.request_id)) return []
    if (event.type === 'msg.said' && payload.phase === 'team') {
      if (teamEchoes.has(event.seq) || deliveries.consumed.has(event.seq)) return []
      const speaker = replyEvidence.has(event.seq) ? actor.id : teamSpeaker(event)
      const audience = teamMessageAudience(event, bySeq)
      const item = message(undefined, typeof payload.text === 'string' ? payload.text : '', 'agent')
      item.actor = speaker
      item.display = displayName(speaker)
      item.role = publicIdentity(speaker, organization)
      item.systemActor = false
      item.internal = teamEnabled && (audience.audience === 'team' || teamProgress.has(event.seq))
      item.replyEvidence = replyEvidence.get(event.seq)
      if (item.replyEvidence) item.auditName = displayName
      if (audience.recipient) item.targets = [displayName(audience.recipient)]
      return item.text ? [item] : []
    }
    if (retryRecords.has(event.seq)) return []
    if (event.type === 'driver.retry_scheduled' || event.type === 'driver.retry_finished') {
      const retry = retries.get(event.seq)
      const role = retry?.scheduled.payload.role
      const name = typeof role === 'string' ? displayName(role).trim() : ''
      const partner = name && name !== role ? `@${name}` : '一位伙伴'
      const state = retry?.finished?.payload.status as ChatItem['retryStatus'] | undefined
      const status = state ?? 'waiting'
      const texts = {
        waiting: `${partner}的模型暂时连接不稳定，已安排一次自动续试（至少间隔 ${Math.ceil(Number(payload.delay_seconds))} 秒）。结果返回后会在这里更新；期间你仍可发消息或暂停任务。`,
        succeeded: `${partner}的模型调用已恢复。后续进展以新的工作记录为准。`,
        failed: `${partner}的模型续试仍失败，已保留检查点。你可以补充消息，或稍后继续。`,
        cancelled: `${partner}的自动续试等待已取消，已有成果保留。`,
      }
      const item = message(retry ? ({ waiting: '正在自动恢复连接', succeeded: '模型调用已恢复', failed: '续试仍失败', cancelled: '续试等待已取消' })[status] : '续试回执待核对',
        retry ? texts[status] : '这条续试记录的来源或关联信息不完整，请以已核对的工作记录为准。', 'system')
      item.actor = 'system'
      item.display = '系统'
      item.role = ''
      item.systemActor = true
      item.targets = []
      item.replyTo = undefined
      if (retry) {
        item.retryStatus = status
        item.auditEvents = [retry.failure, retry.scheduled, retry.finished].filter((entry): entry is Event => Boolean(entry))
        item.auditName = displayName
      }
      return [item]
    }
    const transfer = transfers.get(event.seq)
    if (transfer) {
      const owner = `@${displayName(transfer.owner)}`
      const text = transfer.kind === 'submitted'
        ? `${owner} 已提交成果${transfer.reviewer ? `，交给 @${displayName(transfer.reviewer)} 复核` : '，等待安排复核'}。`
        : transfer.from.length ? `${owner} 已接手，接着 ${transfer.from.map((id) => `@${displayName(id)}`).join('、')} 的成果继续做。` : `${owner} 已接手这项工作。`
      const item = message(undefined, text, 'system', 'collaboration', [transfer.objective])
      if (transfer.kind === 'started') {
        item.actor = transfer.owner
        item.display = displayName(transfer.owner)
        item.role = publicIdentity(transfer.owner, organization)
        item.kind = 'agent'
        item.systemActor = false
        item.title = '接手回执'
        item.progress = events
        item.details = undefined
      } else item.notice = true
      return [item]
    }
    if (event.type === 'clarification.requested') {
      const item = message('请你补充', String(payload.question ?? ''), 'agent')
      item.targets = []
      item.clarification = clarificationRows.get(event.seq)
      return [item]
    }
    if (event.type === 'work.plan_created' || event.type === 'subtask.assigned') {
      if (!projection) return []
      const item = message(undefined, '', 'system', 'collaboration')
      item.notice = true
      return [item]
    }
    if (event.type === 'collaboration.requested') {
      const item = message('定向咨询', typeof payload.question === 'string' ? payload.question : '咨询问题未记录。', 'agent')
      item.targets = typeof payload.recipient === 'string' ? [displayName(payload.recipient)] : []
      item.replyTo = undefined
      return [item]
    }
    if (event.type === 'collaboration.finished') {
      const request = consultationRequests.get(Number(payload.request_seq))
      const bound = consultationMatches(request, event)
      const speaker = typeof payload.speaker_id === 'string' ? payload.speaker_id : undefined
      const validReply = bound && actor.kind === 'system' && actor.id === 'collaboration' && actor.role === 'collaboration'
        && (payload.status === 'answered' || payload.status === 'declined') && speaker === payload.recipient
        && typeof payload.reply === 'string' && Boolean(payload.reply.trim())
      const labels: Record<string, string> = { invalid: '咨询回复未通过核对', unavailable: '咨询暂不可用', stale: '咨询依据已变化' }
      const item = message(validReply ? payload.status === 'declined' ? '咨询回应 · 无法提供意见' : '咨询回应'
        : bound ? labels[String(payload.status)] ?? '咨询回执待核对' : '咨询关联待核对',
      validReply ? String(payload.reply) : typeof payload.reason === 'string' && payload.reason.trim() ? payload.reason : '本次咨询未取得有效回复。',
      validReply ? 'agent' : 'system', 'collaboration')
      item.actor = validReply ? speaker! : 'system'
      item.display = validReply ? displayName(speaker!) : '系统'
      item.role = publicIdentity(item.actor, organization)
      item.systemActor = !validReply
      item.targets = validReply && typeof payload.requester === 'string' ? [displayName(payload.requester)] : []
      item.replyTo = bound ? request!.seq : undefined
      if (validReply) {
        const evidence = Array.isArray(payload.evidence) ? payload.evidence.filter((entry): entry is { artifact_seq: number; quote: string } => Boolean(entry)
          && typeof entry === 'object' && Number.isInteger(entry.artifact_seq) && entry.artifact_seq > 0 && typeof entry.quote === 'string') : []
        item.details = ['只读咨询意见，不代表工具执行或验收通过。', ...evidence.map((entry) => `成果 #${entry.artifact_seq}\n原文：${entry.quote}`)]
      }
      return [item]
    }
    if (event.type === 'question.started' || event.type === 'conversation.started') return []
    if (event.type === 'conversation.received') {
      const state = conversationRows.get(event.seq)?.status
      const progress = ({ pending: '已收到，等待理解', answering: '正在结合上下文理解', uncertain: '这条消息的处理结果待确认' } as Record<string, string>)[state ?? '']
      return [message(undefined, String(payload.instruction ?? ''), 'human', 'collaboration', progress ? [progress] : undefined)]
    }
    if (event.type === 'conversation.finished') {
      if (payload.action === 'status_task' && events.some((record) => record.seq === payload.progress_seq && record.type === 'task.progress_reported')) return []
      const question = questionRows.get(Number(payload.question_seq))
      const routed = payload.status === 'finished' && ['update_task', 'resume_task', 'explain_task'].includes(String(payload.action))
      const speaker = !routed && payload.status === 'finished' && typeof payload.speaker_id === 'string' ? payload.speaker_id : undefined
      const receipt = payload.action === 'update_task' ? '补充要求已记入当前任务。'
        : payload.action === 'resume_task' ? '继续请求已记入，沿用当前要求。'
          : `已将这个问题交给 @${displayName(question?.role_id ?? organization?.leader.id ?? 'store_manager')}，核对现有成果后回答。`
      const invalidResponse = payload.error_code === 'invalid_response'
      const needsAttention = ['clarification', 'outdated', 'invalid', 'blocked', 'uncertain'].includes(String(payload.status))
      const hasCheckpointReply = payload.progress !== null && typeof payload.progress === 'object' && !Array.isArray(payload.progress)
        && typeof payload.reply === 'string' && Boolean(payload.reply.trim())
      const reply = invalidResponse && !hasCheckpointReply
        ? '这条消息已保存，但店长的回应未通过格式校验，暂未形成有效回复，也未按这条消息更改安排。你可以重新发送，或用上方“提问”入口追问已有成果。'
        : String(payload.reply ?? '')
      const item = message(needsAttention ? '店长回应需要跟进' : undefined, routed ? receipt : reply, speaker ? 'agent' : 'system', 'collaboration')
      item.actor = speaker ?? 'system'
      item.display = speaker ? displayName(speaker) : '系统'
      item.role = publicIdentity(item.actor, organization)
      item.systemActor = !speaker
      if (needsAttention && typeof payload.conversation_seq === 'number') item.details = [`对应你的消息 · 事件 #${payload.conversation_seq}`]
      if (question && !finishedQuestions.has(question.seq)) {
        const progress = ({ pending: '等待回答', answering: '正在核对成果并回答', uncertain: '回答结果待确认' } as Record<string, string>)[question.status]
        if (progress) item.details = [progress]
      }
      return [item]
    }
    if (event.type === 'question.asked' || event.type === 'question.finished') {
      if (event.type === 'question.asked' && explainedMessages.has(event.seq)) return []
      const asked = event.type === 'question.asked'
      const questionSeq = asked ? event.seq : typeof payload.question_seq === 'number' ? payload.question_seq : undefined
      const row = questionSeq === undefined ? undefined : questionRows.get(questionSeq)
      const basis = row?.basis ?? (asked ? payload.basis : askedEvents.get(questionSeq ?? -1)?.payload.basis) as QuestionBasis | undefined
      const labels: Record<string, string> = { answered: '讲解回答', insufficient: '依据不足', blocked: '提问受阻', invalid: '问题未能处理', uncertain: '回答待确认' }
      const reason = typeof payload.reason === 'string' ? payload.reason : undefined
      const hasAnswer = typeof payload.answer === 'string' && Boolean(payload.answer.trim())
      const speaker = !asked && hasAnswer && typeof payload.speaker_id === 'string' && payload.speaker_id ? payload.speaker_id : undefined
      const system = isSystemActor(actor, identities)
      const item = message(asked ? '提问' : labels[String(payload.status)] ?? '提问结果',
        asked ? String(payload.question ?? '') : hasAnswer ? String(payload.answer) : reason || '暂无回答。',
        asked ? 'human' : speaker || !system ? 'agent' : 'system', 'collaboration')
      if (!asked && (speaker || system)) {
        item.actor = speaker ?? 'system'
        item.display = speaker ? identities[speaker]?.name ?? actorText(speaker) : '系统'
        item.role = publicIdentity(item.actor, organization)
        item.systemActor = !speaker
      }
      const recipient = row?.role_id ?? (asked ? payload.role_id : askedEvents.get(questionSeq ?? -1)?.payload.role_id)
      const progress = asked && row && !finishedQuestions.has(event.seq)
        ? ({ pending: '等待回答', answering: '正在回答', uncertain: '回答结果待确认' } as Record<string, string>)[row.status] : undefined
      item.question = { basis, outdated: row?.outdated, questionSeq, recipient: typeof recipient === 'string' ? identities[recipient]?.name ?? actorText(recipient) : undefined,
        progress,
        evidence: !asked && Array.isArray(payload.evidence) ? payload.evidence.filter((entry): entry is { source_seq: number; quote: string } => Boolean(entry) && typeof entry === 'object' && typeof entry.source_seq === 'number' && typeof entry.quote === 'string') : [],
        limits: !asked && Array.isArray(payload.limits) ? payload.limits.filter((limit): limit is string => typeof limit === 'string') : [], reason: asked ? undefined : reason }
      // Only public answer fields belong in the question audit shown in chat.
      const fields = asked ? ['question', 'role_id', 'request_id', 'basis'] : ['question_seq', 'status', 'answer', 'evidence', 'limits', 'reason', 'speaker_id', 'driver', 'tokens', 'token_usage']
      item.evidence = { ...event, payload: Object.fromEntries(fields.filter((field) => field in payload).map((field) => [field, payload[field]])) }
      return [item]
    }
    if (event.type === 'task.created') return [message('新任务', String(payload.goal ?? ''), 'human')]
    if (event.type === 'rule.recorded' || event.type === 'rule.updated') {
      const scope = ({ workspace: '猫咖通用', project: '当前项目', task: '本次任务' } as Record<string, string>)[String(payload.scope)] ?? '当前范围'
      const state = ({ active: '已采用', candidate: '候选，待采用', retired: '已停用' } as Record<string, string>)[String(payload.status)] ?? '已记录'
      return [message(`店规 · ${state}`, String(payload.text ?? ''), actor.kind === 'human' ? 'human' : 'agent', 'decision', [`适用范围：${scope}`, `版本：${payload.revision}`, '目前作为上下文要求，不代表文件保护或验收已经通过。'])]
    }
    if (event.type === 'activity.queued') {
      if (hasFinishedActivity) return []
      const followUp = typeof payload.after_activity_id === 'string'
      const changed = typeof payload.replaces_action === 'string'
      return [message(changed ? '店长更新了排队安排' : followUp ? '新方向已收下' : '协作已排队', changed ? (String(payload.action) === 'battle' ? '店长会在当前这一轮结束后，先请伙伴复核最新取舍。' : '店长会在当前这一轮结束后，按最新方向直接继续推进。') : followUp ? '店长会在当前这一轮结束后，按你刚补充的方向继续安排。' : String(payload.action) === 'battle' ? '店长请几位铲屎官一起看看。' : '团队将开始下一轮协作。', 'system')]
    }
    // The header already carries live activity state. Completion, routing and
    // plan bookkeeping remain in the audit trail but should not crowd out the
    // conversation people actually want to read.
    if (event.type === 'activity.completed') return []
    if (event.type === 'activity.failed') return [message('协作异常', String(payload.message ?? '后台协作未能完成。'), 'system')]
    if (event.type === 'human.commanded') {
      if (payload.conversation_seq) {
        if (events.some((record) => record.type === 'conversation.finished' && record.payload.conversation_seq === payload.conversation_seq)) return []
        return [message('安排回执待核对', '这条消息已转为工作安排，但后续处理回执尚不完整。', 'system', 'collaboration')]
      }
      return [message(payload.intent === 'status' ? '查看进度' : payload.intent === 'resume' ? '继续推进' : '方向更新', String(payload.instruction ?? ''), 'human')]
    }
    if (event.type === 'task.progress_reported') return [message('当前进展', String(payload.text ?? ''), 'system', 'collaboration')]
    if (event.type === 'planning.started') return [message('店长在整理安排', '我先核对这次的要求、已有成果和前后依赖，再把具体工作交给伙伴。', 'system', 'collaboration')]
    if (event.type === 'planning.finished') {
      const item = message(payload.status === 'installed' ? '这次这样安排' : '计划先不下发', String(payload.reason ?? ''), 'system', 'decision')
      return [item]
    }
    if (isResponseFormatError(event)) {
      const item = message('成员回应格式异常', typeof payload.text === 'string' && payload.text.trim()
        ? payload.text : '成员的回应未通过格式校验，当前工作结果仍需核对。', 'system')
      item.actor = 'system'
      item.display = '系统'
      item.role = ''
      item.systemActor = true
      return [item]
    }
    if (event.type === 'msg.said' && ['planning', 'delivery_receipt', 'clarification_receipt', 'collaboration_request'].includes(String(payload.phase))) return []
    if (event.type === 'msg.said' && (payload.phase === 'review_response_correction'
      || (payload.phase === 'review' && payload.text === '这次卡在我的复核回复格式或引用上，还不能据此认定成果有错。我会按原成果重新整理一次回复。'))) {
      const item = message('复核记录整理中', '正在校正复核回复的格式与引用，不改动成果，也不代表验收已经通过。', 'system')
      item.systemActor = true
      return [item]
    }
    if (event.type === 'msg.said' && payload.phase === 'review') {
      const legacyApproval = payload.text === '这一项的阶段标准通过了，可以交给后面的伙伴接着做；整件任务还需要最后核对。'
      const previous = events.filter((entry) => entry.seq < event.seq).at(-1)
      const review = events.filter((entry) => entry.type === 'work.reviewed' && entry.seq < event.seq
        && entry.actor.id === actor.id && typeof payload.work_item_id === 'string'
        && entry.payload.work_item_id === payload.work_item_id
        && (typeof payload.review_seq === 'number' ? entry.seq === payload.review_seq
          : legacyApproval && entry.seq === previous?.seq && entry.payload.status === 'accepted')).at(-1)
      const checks = review && Array.isArray(review.payload.checks)
        ? review.payload.checks.filter((check): check is Record<string, unknown> => Boolean(check) && typeof check === 'object') : []
      if (payload.speech_source === 'system_receipt' || (legacyApproval && !review)) {
        const item = message('复核回执', legacyApproval && !review
          ? '历史回执待核对：无法明确关联这条消息对应的复核依据。' : String(payload.text ?? ''), 'system')
        item.systemActor = true
        return [item]
      }
      if (review && (payload.speech_source === 'review_assessment' || legacyApproval)) {
        const reasons = checks.filter((check) => check.source !== 'runtime_check' && !check.response_issue && typeof check.reason === 'string').map((check) => String(check.reason))
        const label = ({ accepted: '阶段通过', rework: '需要修改', submitted: '仍有待确认项' } as Record<string, string>)[String(review.payload.status)] ?? '待核对'
        return [message(`复核意见 · ${label}`, [...new Set(reasons)].join('\n\n'), 'agent', 'decision', [
          `依据复核记录 #${review.seq}；阶段结论不代表整项任务已经交付。`,
          ...checks.map((check) => `${({ passed: '符合', failed: '不符合', unknown: '待确认' } as Record<string, string>)[String(check.status)] ?? '待确认'}${typeof check.artifact_seq === 'number' ? ` · 成果 #${check.artifact_seq}` : ''}${typeof check.receipt_seq === 'number' ? ` · 检查 #${check.receipt_seq}` : ''}${typeof check.reason === 'string' ? `\n${check.source === 'runtime_check' ? '系统检查依据' : check.status === 'unknown' || (check.source !== undefined && check.source !== 'model_assessment') ? '待确认依据' : '判断依据'}：${check.reason}` : ''}${typeof check.quote === 'string' && check.quote ? `\n原文：${check.quote}` : ''}`),
        ])]
      }
    }
    if (event.type === 'delivery.review_started') return [message('最后一起核对', '各项成果已经过阶段验收。我再检查它们合起来是否满足你现在的要求，再整理交付和复查说明。', 'agent', 'collaboration')]
    if (event.type === 'delivery.review_finished') {
      const handoff = payload.handoff && typeof payload.handoff === 'object' ? payload.handoff as Record<string, unknown> : null
      const steps = handoff && Array.isArray(handoff.reproduce) ? handoff.reproduce.filter((s): s is string => typeof s === 'string') : []
      return [message(payload.status === 'accepted' ? '这次的成果整理好了' : '最后核对还有一处要处理',
        payload.status === 'accepted' && handoff ? `${String(handoff.summary)}\n${String(handoff.why)}` : String(payload.reason ?? '还需要补齐整体验收依据。'),
        'agent', payload.status === 'accepted' ? 'deliverable' : 'decision', steps)]
    }
    if (event.type === 'task.interpreted') {
      const scope = Array.isArray(payload.scope) ? payload.scope.filter((item): item is string => typeof item === 'string') : []
      const risks = Array.isArray(payload.risks) ? payload.risks.filter((item): item is string => typeof item === 'string') : []
      const acceptance = Array.isArray(payload.acceptance) ? payload.acceptance.filter((item): item is string => typeof item === 'string') : []
      return [message('店长先把这单听明白', `${String(payload.summary ?? '')}\n${String(payload.objective ?? '')}`, 'system', 'decision', [
        ...scope.map((item) => `这次会覆盖：${item}`),
        ...acceptance.map((item) => `完成时要满足：${item}`),
        ...risks.map((item) => `店长会留意：${item}`),
      ])]
    }
    if (event.type === 'task.routed') return []
    if (event.type === 'cabinet.convened') {
      const names = memberNames(payload.members)
      return [message(names.length ? `店长请 ${names.join('、')} 一起看看` : '店长请伙伴一起看看', `${reasonText(String(payload.reason ?? '任务需要更多伙伴一起确认。'))}${names.length ? `\n这次由铲屎官 ${names.join('、')} 先行商量。` : ''}`, 'system', 'decision')]
    }
    if (event.type === 'mission.created') return []
    if (event.type === 'task.decomposed') {
      const names = memberNames(payload.members)
      const subtasks = Array.isArray(payload.subtasks) ? payload.subtasks.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object') : []
      const details = subtasks.filter((item) => item.acceptance).map((item) => `@${displayName(String(item.owner))} 验收：${String(item.acceptance)}`)
      const isCabinetPlan = String(payload.source) === 'cabinet'
      const planner = isCabinetPlan ? names.join('、') || '几位伙伴' : '店长'
      const invitation = subtasks.some((item) => typeof item.objective === 'string')
        ? subtasks.map((item) => `${String(item.objective ?? item.lane ?? '这项工作')}，交给 @${displayName(String(item.owner ?? ''))}。`).join('\n')
        : '具体分工已记录，伙伴会按依赖接力。'
      return [message(`${planner}把工作分好了`, invitation, 'system', 'decision', details)]
    }
    if (event.type === 'plan.amended') {
      const participants = memberNames(payload.participants)
      const added = memberNames(payload.added)
      const removed = memberNames(payload.removed)
      const changes = [
        added.length ? `新加入：${added.map((name) => `@${name}`).join('、')}` : '',
        removed.length ? `暂缓：${removed.map((name) => `@${name}`).join('、')}` : '',
        String(payload.next_action) === 'battle' ? '下一轮会先请伙伴一起复核关键取舍。' : '下一轮会带着这条新方向继续推进。',
      ].filter(Boolean)
      const roster = participants.length ? `下一轮由 ${participants.map((name) => `@${name}`).join('、')} 跟进。` : '店长会先根据这条新方向重新安排。'
      return [message('店长更新了下一轮安排', `${roster}\n${reasonText(String(payload.reason ?? '已记录最新方向。'))}`, 'system', 'decision', changes)]
    }
    if (event.type === 'plan.proposed' || event.type === 'agent.working') return []
    if (event.type === 'msg.said' || event.type === 'msg.challenge') return [message(event.type === 'msg.challenge' ? '提出质疑' : undefined, String(payload.text ?? payload.claim ?? ''), 'agent')]
    if (event.type === 'diagnosis.recorded') {
      const observations = Array.isArray(payload.observations) ? payload.observations.filter((item): item is string => typeof item === 'string') : []
      const alternatives = Array.isArray(payload.alternatives) ? payload.alternatives.filter((item): item is string => typeof item === 'string') : []
      const impact = Array.isArray(payload.impact) ? payload.impact.filter((item): item is string => typeof item === 'string') : []
      const verification = Array.isArray(payload.verification_plan) ? payload.verification_plan.filter((item): item is string => typeof item === 'string') : []
      const status = String(payload.status ?? '') as Diagnosis['status']
      return [message(`诊断卡 · ${diagnosisStatusText(status) ?? '待核验'}`, `${String(payload.symptom ?? '')}\n判断：${String(payload.cause ?? '')}`, 'agent', 'decision', [
        ...observations.map((item) => `观察到：${item}`),
        ...alternatives.map((item) => `仍需排除：${item}`),
        ...impact.map((item) => `可能影响：${item}`),
        ...verification.map((item) => `验证计划：${item}`),
      ])]
    }
    if (event.type === 'deliverable.published') {
      const internal = payload.phase === 'team' || payload.source === 'team'
      const item = message(internal ? '阶段成果' : '发布成果', `${String(payload.title ?? '未命名成果')}\n${String(payload.summary ?? '')}`, 'agent', 'deliverable')
      item.internal = internal
      return [item]
    }
    if (event.type === 'handoff.published') {
      const changed = Array.isArray(payload.changed) ? payload.changed.filter((item): item is string => typeof item === 'string') : []
      const verification = Array.isArray(payload.verification) ? payload.verification.filter((item): item is string => typeof item === 'string') : []
      const limits = Array.isArray(payload.limits) ? payload.limits.filter((item): item is string => typeof item === 'string') : []
      const teachBack = Array.isArray(payload.teach_back) ? payload.teach_back.filter((item): item is string => typeof item === 'string') : []
      return [message('交接卡', `${String(payload.summary ?? '')}\n为什么：${String(payload.why ?? '')}`, 'agent', 'deliverable', [
        ...changed.map((item) => `改了：${item}`),
        ...verification.map((item) => `成员报告的检查：${item}`),
        ...limits.map((item) => `尚未覆盖：${item}`),
        ...teachBack.map((item) => `你可以追问：${item}`),
      ])]
    }
    if (event.type === 'verification.planning_started' || event.type === 'verification.planned') {
      const started = event.type === 'verification.planning_started'
      const ready = payload.status === 'ready'
      const correcting = payload.status === 'correcting'
      const stale = payload.status === 'stale'
      const title = started ? '正在准备浏览器检查' : correcting ? '检查方案正在校正，尚未执行' : ready ? '浏览器检查方案已准备' : stale ? '检查依据已变化' : '检查能力缺口'
      const text = started ? '正在根据本次提交与检查要求准备方案，尚无实际执行回执。'
        : correcting ? '检查方案正在校正，尚未执行。请以后续方案及实际检查回执为准。'
        : ready ? '方案已准备，是否执行及通过以本机检查回执为准。'
        : stale ? '成果或要求已变化，未执行这份旧检查方案。'
        : '本次浏览器检查未能安排；这是检查能力或方案缺口，不能据此认定成果错误。'
      const details = [typeof payload.reason === 'string' ? payload.reason : '',
        typeof payload.criterion_id === 'string' ? `检查项：${payload.criterion_id}` : '',
        ...browserCheckDetails(payload.plan)].filter(Boolean)
      const item = message(title, text, 'system', 'decision', details)
      item.systemActor = true
      return [item]
    }
    if (event.type === 'verification.completed') {
      const command = Array.isArray(payload.command) ? payload.command.filter((item): item is string => typeof item === 'string') : []
      const outcome = String(payload.outcome ?? '')
      const label = ({ passed: '命令通过', failed: '命令失败', timeout: '执行超时', launch_error: '无法启动', unavailable: '检查工具不可用' } as Record<string, string>)[outcome] ?? '检查结束'
      const git = payload.git as Record<string, unknown> | undefined
      const details = [
        `退出码：${payload.exit_code ?? '无'}`,
        typeof payload.for_event === 'number' ? `关联自报事件 #${payload.for_event}；并不自动证实其结论` : '',
        git?.head ? `Git 提交：${String(git.head).slice(0, 12)}${git.dirty ? '（工作区有未提交改动，检查未绑定具体文件内容）' : ''}` : '未识别 Git 状态',
        '仅表示这条本地命令的观察结果，不代表根因或整项任务已证实。',
        ...browserCheckDetails(payload.browser_plan),
      ]
      if (outcome === 'unavailable') details.push('检查工具不可用或回执不完整，不能据此认定成果错误。')
      for (const [key, label] of [['stdout', '标准输出'], ['stderr', '错误输出']] as const) {
        details.push(...outputSummaryLines(label, payload[key], 800, true))
      }
      if (typeof payload.launch_error === 'string') details.push(`启动错误：${payload.launch_error}`)
      if (typeof payload.cleanup_error === 'string') details.push(`清理错误：${payload.cleanup_error}`)
      const item = message(`本机检查 · ${label}`, command.join(' '), 'system', 'decision', details)
      item.systemActor = true
      return [item]
    }
    if (event.type === 'cabinet.voted') {
      const tally = payload.tally as Record<string, unknown> | undefined
      return [message('伙伴们给出建议', reasonText(String(tally?.reason ?? '几位伙伴已经看过当前选项。')), 'system', 'decision')]
    }
    if (event.type === 'agent.decided' || event.type === 'supreme.decided') return [message('自治决策', reasonText(String(payload.reason ?? '')), 'system', 'decision')]
    if (event.type === 'brief.generated') {
      const brief = payload.brief as Record<string, unknown> | undefined
      const checkpoint = brief?.checkpoint as { state?: string } | undefined
      if (payload.reason === 'work_checkpoint' && checkpoint?.state === 'waiting_local_check') {
        const summary = typeof brief?.dispute === 'string' ? brief.dispute : '已有成果提交，当前实际检查回执尚未补齐。'
        const item = message('等待实际检查', `当前没有完整检查回执，具体待查原因以检查记录为准。源码审查可以先进行，但不能替代浏览器等实际运行验证；补齐当前成果的检查回执后才能放行后续工作。\n${summary}`, 'system')
        item.actor = 'system'
        item.display = '系统'
        item.role = ''
        item.systemActor = true
        return [item]
      }
      if (hasAutonomousDecision) return []
      // Routine handoffs belong to the worker/reviewer, not another manager speech.
      if (payload.reason === 'work_checkpoint' && ['running', 'waiting_check', 'delivery_checking', 'delivery_reviewing', 'ready', 'planned', 'accepted'].includes(checkpoint?.state ?? '')) return []
      return [message('本轮总结', reasonText(String(brief?.dispute ?? '团队已完成一轮协作。')), 'system')]
    }
    if (event.type === 'agent.discipline_flagged') {
      const issues = Array.isArray(payload.issues) ? payload.issues.filter((item): item is string => typeof item === 'string') : []
      return [message('协作提醒', `这条发言需要补充：${disciplineText(issues)}。它不会直接影响团队决策。`, 'system', 'decision')]
    }
    if (event.type === 'driver.budget_exhausted') return [message('协作预算提醒', `通道 ${String(payload.driver ?? '')} 已使用 ${String(payload.used_tokens ?? 0)} / ${String(payload.budget ?? 0)} 个已报告 token，本轮不再继续调用它。`, 'system', 'decision')]
    if (event.type === 'driver.failed') return [message('已隔离的驱动异常', String(payload.message ?? ''), 'system')]
    return []
  })
}

function App() {
  // API authentication is fixed for this page; detect changes to its session context too.
  const [creationSessionToken] = useState(readAccessToken)
  const [recoveryReady, setRecoveryReady] = useState(false)
  const [pageReady, setPageReady] = useState(false)
  const [recovery, setRecovery] = useState<CreationAttempt | null>(null)
  const [recoveryNotice, setRecoveryNotice] = useState('')
  const creationMounted = useRef(true)
  const [newProject, setNewProject] = useState<{ id: string; name: string } | null>(null)
  const [newWorkspaceId, setNewWorkspaceId] = useState<string | undefined>()
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const removedTasks = useRef(new Set<string>())
  const removalRequests = useRef<Record<string, string>>({})
  const [removeTarget, setRemoveTarget] = useState<{ id: string; title: string } | null>(null)
  const [removeBusy, setRemoveBusy] = useState(false)
  const [removeError, setRemoveError] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [derivation, setDerivation] = useState<DerivationSource | null>(null)
  const [questionMode, setQuestionMode] = useState(false)
  const questionModeRef = useRef(false)
  const questionRequests = useRef<Record<string, { text: string; requestId: string }>>({})
  const conversationRequests = useRef<Record<string, { text: string; requestId: string }>>({})
  const creationRequest = useRef<CreationAttempt | null>(null)
  const [controlRequests, setControlRequests] = useState<Record<string, { action: ControlAction; requestId: string }>>({})
  const controlsRef = useRef<Record<string, ExecutionControl>>({})
  const [executionControls, setExecutionControls] = useState<Record<string, ExecutionControl>>({})
  const [clarificationStore] = useState<ClarificationStore>(() => new Map())
  const [questionnaireStore] = useState<QuestionnaireStore>(() => new Map())
  const derivationRef = useRef<DerivationSource | null>(null)
  const derivationRequests = useRef<Record<string, { text: string; requestId: string }>>({})
  const focusComposerRef = useRef(false)
  const focusTaskHeadingRef = useRef(false)
  const chatPaneRef = useRef<HTMLElement>(null)
  const [roles, setRoles] = useState<OrgRole[]>([])
  const [rolesError, setRolesError] = useState('')
  const [organization, setOrganization] = useState<Organization | null>(null)
  const [pending, setPending] = useState<Set<string>>(new Set())
  const [showEvidence, setShowEvidence] = useState(false)
  const [showMobileBoard, setShowMobileBoard] = useState(false)
  const [goalExpanded, setGoalExpanded] = useState(false)
  const [taskListPreference, setTasksCollapsed] = useState<boolean>()
  const tasksCollapsed = taskListPreference ?? selectedId !== null
  const [taskSearchOpen, setTaskSearchOpen] = useState(false)
  const [graphOpen, setGraphOpen] = useState(false)
  const [graphLoaded, setGraphLoaded] = useState(false)
  const [teamOpen, setTeamOpen] = useState(false)
  const [teamRefreshing, setTeamRefreshing] = useState(false)
  const [teamError, setTeamError] = useState('')
  const profileBusy = useRef(false)
  const profileAbort = useRef<AbortController | null>(null)
  const profileMounted = useRef(true)
  const [chainStatus, setChainStatus] = useState<ChainStatus | null>(null)
  const [checkingChain, setCheckingChain] = useState(false)
  const [chatFilter, setChatFilter] = useState<ChatFilter>('all')
  const [error, setError] = useState('')
  const [taskListError, setTaskListError] = useState('')
  const [sendErrors, setSendErrors] = useState<Record<string, string>>({})
  const [loadErrors, setLoadErrors] = useState<Record<string, string>>({})
  const [reloadToken, setReloadToken] = useState(0)
  const [unread, setUnread] = useState(0)
  const streamRef = useRef<HTMLDivElement>(null)
  const graphTriggerRef = useRef<HTMLButtonElement>(null)
  const followRef = useRef(true)
  const seenRef = useRef({ view: '', latest: 0, count: 0 })
  const selectedRef = useRef<string | null>(null)
  const navigationRef = useRef(0)
  const navigationMemory = useRef<NavigationMemory | null>(null)
  const initialSelection = useRef<string | null | undefined>(undefined)
  const pendingRef = useRef(new Set<string>())
  const versionsRef = useRef<Record<string, { summary: TaskSummary; order: number }>>({})
  const detailOrderRef = useRef(0)
  const newKey = '$new'
  const teamChat = Boolean(detail?.task.id === selectedId && (detail.team ?? detail.task.team)?.enabled)
  const legacyQuestionMode = questionMode && !teamChat
  const composerKey = derivation ? `$derive:${derivation.id}` : legacyQuestionMode && selectedId ? `$question:${selectedId}` : selectedId ?? newKey
  const draft = drafts[newKey] ?? ''
  const message = selectedId ? drafts[composerKey] ?? '' : ''
  const isSending = pending.has(composerKey)
  const pageSnapshot = useRef<PageDraftSnapshot>({ drafts: {}, selectedId: null, questionMode: false, derivation: null, project: null })
  useLayoutEffect(() => {
    pageSnapshot.current = { drafts, selectedId, questionMode: legacyQuestionMode, derivation, project: newProject, workspaceId: newWorkspaceId,
      requests: { conversation: conversationRequests.current, questions: questionRequests.current, derivations: derivationRequests.current } }
  })

  const updateDraft = (key: string, value: string | ((current: string) => string)) => {
    setDrafts((current) => ({ ...current, [key]: typeof value === 'function' ? value(current[key] ?? '') : value }))
  }
  const setDraft = (value: string | ((current: string) => string)) => {
    const next = typeof value === 'function' ? value(draft) : value
    navigationRef.current += 1
    const notice = navigationMemory.current?.remember(null)
    if (notice) setRecoveryNotice(current => current.includes(notice) ? current : [current, notice].filter(Boolean).join(' '))
    if (next !== draft) {
      if (creationRequest.current) clearCreationRecovery(creationRequest.current.requestId)
      creationRequest.current = null
      setSendErrors((current) => ({ ...current, [newKey]: '' }))
    }
    updateDraft(newKey, next)
  }
  const setMessage = (value: string) => {
    if (!selectedId) return
    if (derivation && value !== message) {
      navigationRef.current += 1
      delete derivationRequests.current[composerKey]
      setSendErrors((current) => ({ ...current, [composerKey]: '' }))
    }
    if (legacyQuestionMode && value !== message) {
      delete questionRequests.current[composerKey]
      setSendErrors((current) => ({ ...current, [composerKey]: '' }))
    }
    updateDraft(composerKey, value)
  }

  const selectDraftWorkspace = (workspaceId: string | undefined) => {
    if (workspaceId === newWorkspaceId) return
    navigationRef.current += 1
    if (creationRequest.current) clearCreationRecovery(creationRequest.current.requestId)
    creationRequest.current = null
    setSendErrors((current) => ({ ...current, [newKey]: '' }))
    setRecoveryNotice('')
    setNewWorkspaceId(workspaceId)
  }

  const chooseTask = useCallback((id: string | null, remember = true) => {
    if (id && removedTasks.current.has(id)) id = null
    focusTaskHeadingRef.current = false
    questionModeRef.current = false
    setQuestionMode(false)
    derivationRef.current = null
    setDerivation(null)
    focusComposerRef.current = false
    selectedRef.current = id
    navigationRef.current += 1
    followRef.current = true
    seenRef.current = { view: '', latest: 0, count: 0 }
    setUnread(0)
    setSelectedId(id)
    setChatFilter('all')
    setShowMobileBoard(false)
    setGoalExpanded(false)
    setGraphOpen(false)
    const notice = remember ? navigationMemory.current?.remember(id) : ''
    if (notice) setRecoveryNotice(current => current.includes(notice) ? current : [current, notice].filter(Boolean).join(' '))
  }, [])

  useEffect(() => {
    let cancelled = false
    creationMounted.current = true
    void loadCreationRecovery(accessToken).then(({ attempt, notice }) => {
      if (cancelled) return
      setRecovery(attempt)
      if (notice) setRecoveryNotice(current => [current, notice].filter(Boolean).join(' '))
      setRecoveryReady(true)
    })
    return () => { cancelled = true; creationMounted.current = false }
  }, [])

  useEffect(() => {
    let cancelled = false
    const navigation = navigationRef.current
    void openNavigationMemory(creationSessionToken, () => !cancelled).then(async memory => {
      if (cancelled) return
      navigationMemory.current = memory
      // Consume the explicit snapshot only after navigation setup, without another pending hash before application.
      const page = await loadPageRecovery(creationSessionToken, () => !cancelled && readAccessToken() === creationSessionToken)
      if (cancelled || readAccessToken() !== creationSessionToken) return
      if (navigationRef.current === navigation) {
        initialSelection.current = memory.selectedId
        if (!page.snapshot && memory.notice) setRecoveryNotice(current => [current, memory.notice].filter(Boolean).join(' '))
      } else {
        const notice = memory.remember(selectedRef.current)
        if (notice) setRecoveryNotice(current => [current, notice].filter(Boolean).join(' '))
      }
      if (page.notice) setRecoveryNotice(current => [current, page.notice].filter(Boolean).join(' '))
      if (page.snapshot) {
        const saved = page.snapshot
        setDrafts(current => ({ ...saved.drafts, ...current }))
        if (saved.requests) {
          conversationRequests.current = { ...saved.requests.conversation, ...conversationRequests.current }
          questionRequests.current = { ...saved.requests.questions, ...questionRequests.current }
          derivationRequests.current = { ...saved.requests.derivations, ...derivationRequests.current }
        }
        if (navigationRef.current === navigation && !pendingRef.current.size) {
          setNewProject(saved.project)
          setNewWorkspaceId(saved.workspaceId)
          chooseTask(saved.selectedId)
          questionModeRef.current = saved.questionMode
          setQuestionMode(saved.questionMode)
          derivationRef.current = saved.derivation
          setDerivation(saved.derivation)
        }
      }
      setPageReady(true)
    })
    return () => { cancelled = true; navigationMemory.current = null }
  }, [creationSessionToken, chooseTask])

  const restoreCreation = async () => {
    const navigation = navigationRef.current
    const { attempt, notice } = await loadCreationRecovery(accessToken)
    if (!creationMounted.current || navigationRef.current !== navigation) return
    setRecovery(null)
    setRecoveryNotice(notice)
    if (!attempt) return
    creationRequest.current = attempt
    updateDraft(newKey, attempt.text)
    setNewProject(attempt.projectId ? { id: attempt.projectId, name: attempt.projectId } : null)
    setNewWorkspaceId(attempt.workspaceId)
    setSendErrors((current) => ({ ...current, [newKey]: '上次新建任务的提交结果未知。原草稿已恢复，可核对任务列表后手动重试。' }))
    chooseTask(null)
  }

  useLayoutEffect(() => {
    if (focusTaskHeadingRef.current) {
      chatPaneRef.current?.querySelector<HTMLHeadingElement>('h1')?.focus()
      focusTaskHeadingRef.current = false
    }
  }, [selectedId])

  useLayoutEffect(() => {
    if (!focusComposerRef.current) return
    focusComposerRef.current = false
    chatPaneRef.current?.querySelector<HTMLTextAreaElement>('.composer-box textarea')?.focus()
  }, [composerKey])

  const requestDetail = useCallback(async (path: string, init?: RequestInit) => {
    const order = ++detailOrderRef.current
    const next = await api<TaskDetail>(path, init)
    validateTaskDetail(next)
    return { next, order }
  }, [])

  const acceptControls = useCallback((items: { id: string; execution_control?: ExecutionControl }[]) => {
    const updates: Record<string, ExecutionControl> = {}
    for (const item of items) {
      const control = item.execution_control
      const previous = updates[item.id] ?? controlsRef.current[item.id]
      if (control && (!previous || (control.seq ?? 0) > (previous.seq ?? 0))) updates[item.id] = control
    }
    if (!Object.keys(updates).length) return
    controlsRef.current = { ...controlsRef.current, ...updates }
    setExecutionControls((current) => ({ ...current, ...updates }))
  }, [])

  const acceptDetail = useCallback((next: TaskDetail, order: number, activate = false) => {
    const team = resolveTeam(next.team ?? next.task.team, next.events)
    if (team?.enabled) next = { ...next, task: { ...next.task, team, work: undefined,
      goal: team.objective || next.task.goal, status: team.status === 'done' ? 'done' : 'running' } }
    const id = next.task.id
    if (removedTasks.current.has(id)) return
    const seq = next.events.at(-1)?.seq ?? 0
    const version = next.summary?.version ?? seq
    const base = { ...(next.summary ?? { id, goal: next.task.goal, project_id: next.task.project_id,
      status: next.task.status, status_label: taskListStatus({ ...next.task, activity: next.activity }),
      activity: next.activity, latest_seq: seq, version }), observation_order: order }
    next = { ...next, summary: base }
    const item = team?.enabled ? { ...base, team, goal: next.task.goal, status: next.task.status, status_label: teamStatus(team, next.activity) } : base
    const previous = versionsRef.current[id]
    const comparison = previous ? summaryOrder(item, previous.summary) : 1
    if (activate) chooseTask(id)
    acceptControls([{ id, execution_control: next.execution_control }, { id, execution_control: next.summary?.execution_control }])
    if (comparison < 0 || (comparison === 0 && previous && order < previous.order)) return
    versionsRef.current[id] = { summary: item, order }
    setTasks((current) => {
      return current.some((task) => task.id === id)
        ? current.map((task) => task.id === id && summaryOrder(item, task) >= 0 ? item : task) : [...current, item]
    })
    if (selectedRef.current === id) setDetail(next)
  }, [acceptControls, chooseTask])

  useEffect(() => {
    if (!recoveryReady || !pageReady) return
    let cancelled = false
    let fetching = false
    let initialized = false
    let etag = ''
    let timer: number | undefined
    let controller: AbortController | undefined
    const pageVisible = () => document.visibilityState !== 'hidden'
    const poll = async () => {
      if (cancelled || fetching || !pageVisible()) return
      fetching = true
      const order = ++detailOrderRef.current
      controller = new AbortController()
      const signal = controller.signal
      try {
        const result = await apiRead('/api/task-summaries', async (response) => {
          if (response.status === 304) return null
          return { next: await readJsonResponse<{ tasks: TaskSummary[]; removed?: { id: string; seq: number }[] }>(response), etag: response.headers.get('ETag') ?? '' }
        }, { signal, cache: 'no-store',
          headers: etag ? { 'If-None-Match': etag } : {} })
        if (cancelled || signal.aborted || !pageVisible()) return
        if (!result) { setTaskListError(''); return }
        const { next } = result
        if (!Array.isArray(next.tasks)) throw new Error('暂时无法读取任务列表，请稍后重试。')
        if (next.removed !== undefined && (!Array.isArray(next.removed) || next.removed.some(item => !item || typeof item.id !== 'string'
          || !Number.isSafeInteger(item.seq) || item.seq < 1))) throw new Error('对话列表状态暂时无法核对，请刷新。')
        for (const item of next.removed ?? []) removedTasks.current.add(item.id)
        if (selectedRef.current && removedTasks.current.has(selectedRef.current)) chooseTask(null)
        const receivedTasks = next.tasks.filter(task => !removedTasks.current.has(task.id)).map(task => ({ ...task, observation_order: order }))
        etag = result.etag
        acceptControls(receivedTasks)
        setTaskListError('')
        setTasks((current) => {
          const received = new Map(receivedTasks.map((task) => [task.id, task]))
          return [...current.filter(task => !removedTasks.current.has(task.id)).map((task) => {
            const item = received.get(task.id)
            received.delete(task.id)
            if (!item) return task
            const merged = !item.team && item.version === task.version && task.team?.enabled ? { ...item, team: task.team,
              goal: task.goal, status: task.status, status_label: task.status_label }
              : item.team?.enabled && task.team?.enabled && item.latest_seq === task.latest_seq && item.team.revision === task.team.revision
                && item.team.status === task.team.status && item.team.pending_count === task.team.pending_count
                ? { ...item, team: { ...task.team, ...item.team,
                  effects_pending_count: item.team.effects_pending_count } } : item
            return summaryOrder(merged, task) >= 0 ? merged : task
          }), ...received.values()]
        })
        if (!initialized && navigationRef.current === 0) {
          const current = navigationMemory.current?.isCurrent() ?? false
          const saved = current ? initialSelection.current : null
          const missing = saved !== undefined && saved !== null && !next.tasks.some(task => task.id === saved)
          chooseTask(missing ? null : saved === undefined ? next.tasks.at(-1)?.id ?? null : saved, current && !missing)
          if (!current) setRecoveryNotice(previous => [previous, '对话位置记录已变化或无法访问，请从任务列表选择。'].filter(Boolean).join(' '))
          if (missing) setRecoveryNotice(current => [current, '上次的对话当前不可用，请从任务列表选择；未启动新任务。'].filter(Boolean).join(' '))
        }
        initialized = true
      } catch (err) {
        if (!cancelled && !signal.aborted && !initialized) setTaskListError(userFacingError(err, '暂时无法读取任务。'))
      } finally {
        fetching = false
        if (!cancelled && pageVisible()) timer = window.setTimeout(() => void poll(), 4000)
      }
    }
    const visibilityChanged = () => {
      window.clearTimeout(timer)
      if (pageVisible()) void poll()
      else controller?.abort()
    }
    void poll()
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => {
      cancelled = true; controller?.abort(); window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [chooseTask, acceptControls, recoveryReady, pageReady])
  useEffect(() => {
    const controller = new AbortController()
    void api<OrgRole[]>('/api/organization', { signal: controller.signal }).then((nextRoles) => {
      if (!controller.signal.aborted) { setRoles(nextRoles); setRolesError('') }
    }).catch(() => {
      if (!controller.signal.aborted) setRolesError('暂时无法读取备用伙伴名单，请稍后重试。')
    })
    return () => controller.abort()
  }, [])
  const refreshOrganization = useCallback(async () => {
    if (profileBusy.current) return
    profileBusy.current = true
    const controller = new AbortController()
    profileAbort.current = controller
    setTeamRefreshing(true)
    try {
      const next = await api<Organization>('/api/organization/profile', { signal: controller.signal })
      if (profileMounted.current) { setOrganization(next); setTeamError('') }
    } catch {
      if (profileMounted.current) {
        setTeamError('暂时无法读取伙伴状态，请稍后重试。')
        setOrganization((current) => current ? { ...current, availability: undefined } : current)
      }
    } finally {
      profileBusy.current = false
      if (profileMounted.current) setTeamRefreshing(false)
    }
  }, [])
  useEffect(() => {
    profileMounted.current = true
    const refreshVisible = () => { if (document.visibilityState !== 'hidden') void refreshOrganization() }
    const initial = window.setTimeout(refreshVisible, 0)
    const timer = window.setInterval(refreshVisible, 60000)
    document.addEventListener('visibilitychange', refreshVisible)
    return () => { profileMounted.current = false; profileAbort.current?.abort(); window.clearTimeout(initial); window.clearInterval(timer); document.removeEventListener('visibilitychange', refreshVisible) }
  }, [refreshOrganization])
  useEffect(() => {
    if (!selectedId) return
    let cancelled = false
    let fetching = false
    let timer: number | undefined
    let controller: AbortController | undefined
    const pageVisible = () => document.visibilityState !== 'hidden'
    const poll = async () => {
      if (cancelled || fetching || !pageVisible()) return
      fetching = true
      controller = new AbortController()
      const signal = controller.signal
      let delay = 4000
      try {
        const { next, order } = await requestDetail(`/api/tasks/${selectedId}`, { signal })
        if (cancelled || signal.aborted || !pageVisible()) return
        validateTaskDetail(next, selectedId)
        acceptDetail(next, order)
        setLoadErrors((current) => ({ ...current, [selectedId]: '' }))
        const team = resolveTeam(next.team ?? next.task.team, next.events)
        delay = next.activity?.active || (team?.enabled && hasActiveTeamMessages(team))
          || next.conversation?.some((row) => row.status === 'pending' || row.status === 'answering') ? 800 : 4000
      } catch (err) {
        if (!cancelled && !signal.aborted) setLoadErrors((current) => ({ ...current, [selectedId]: userFacingError(err, '暂时无法读取任务详情。') }))
      } finally {
        fetching = false
        if (!cancelled && pageVisible()) timer = window.setTimeout(() => void poll(), delay)
      }
    }
    const visibilityChanged = () => {
      window.clearTimeout(timer)
      if (pageVisible()) void poll()
      else controller?.abort()
    }
    void poll()
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      controller?.abort()
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [selectedId, reloadToken, acceptDetail, requestDetail])

  const identities = useMemo(() => {
    const members = organization ? [organization.leader, ...organization.cabinet, ...organization.workers] : roles
    return Object.fromEntries(members.map((member) => [member.id, member]))
  }, [organization, roles])
  const currentDetail = detail?.task.id === selectedId ? detail : null
  const selectedSummary = tasks.find((item) => item.id === selectedId)
  const syncingObjective = hasNewTeamObjective(currentDetail?.task.team, selectedSummary?.team)
  const currentActivity = resolveTeamActivity(currentDetail?.activity, selectedSummary?.activity, syncingObjective,
    currentDetail?.summary, selectedSummary)
  const conversation = useMemo(() => currentDetail ? conversationFor(currentDetail.events, identities, organization, currentDetail.questions, currentDetail.clarifications, currentDetail.conversation, Boolean(currentDetail.task.team?.enabled), currentDetail.questionnaires)
    .map(item => syncingObjective && item.evidence.type === 'team.objective_completed' ? { ...item, historicalDelivery: true } : item) : [], [currentDetail, identities, organization, syncingObjective])
  const visibleConversation = useMemo(() => chatFilter === 'all' ? conversation : conversation.filter((item) => item.category === chatFilter), [chatFilter, conversation])
  const baseTask: Task | undefined = selectedId ? currentDetail?.task ?? (selectedSummary
    ? { ...selectedSummary, team: selectedSummary.team ? { objective: selectedSummary.goal, assignments: [], checkpoint: '', runs: [], ...selectedSummary.team } : undefined,
      acceptance: [], messages: [], assignments: [], deliverables: [] } : undefined) : undefined
  const task = resolveTeamTask(baseTask, selectedSummary, currentDetail?.summary?.version ?? currentDetail?.events.at(-1)?.seq ?? 0)
  const heading = conversationTitle(task?.goal ?? '')
  const loading = selectedId !== null && currentDetail === null
  const workspaceActive = task?.team?.enabled ? hasActiveTeamMessages(task.team)
    : Boolean(currentDetail?.activity?.active || task?.activity?.active || task?.work?.items.some((item) =>
    'in_flight' in item ? item.in_flight === true : item.status === 'running' || item.status === 'uncertain' || item.check_in_flight))
  const loadError = selectedId ? loadErrors[selectedId] ?? '' : ''
  const controlKey = `$control:${selectedId}`
  const controlBusy = pending.has(controlKey)
  const executionControl = selectedId ? executionControls[selectedId] : undefined
  const executionState = executionControl?.state ?? 'running'
  const currentStatusLabel = task ? loading ? '读取对话' : executionLabel(executionState) || taskListStatus(task, currentActivity) : undefined
  const clarificationBlocked = controlBusy || executionState !== 'running'
  const clarificationBlockedReason = executionState === 'cancelled' ? '任务已取消，继续执行需新建任务。仍可直接聊天。'
    : executionState === 'paused' ? '任务已暂停，提交澄清前需恢复执行。仍可直接聊天。' : '正在确认执行控制请求，请稍候再提交澄清。仍可直接聊天。'
  const visibleError = sendErrors[controlKey] || sendErrors[composerKey] || error || taskListError
  const revealQuestion = useCallback((element: HTMLFormElement) => {
    const stream = streamRef.current
    if (stream && stream.contains(element) && followRef.current) {
      const bubble = element.closest('[data-actor]') ?? element
      stream.scrollTop += bubble.getBoundingClientRect().top - stream.getBoundingClientRect().top - 12
    }
  }, [])
  const clarificationControls: ChatControls = {
    store: clarificationStore, createRequestId: creationRequestId,
    questionnaire: {
      store: questionnaireStore, createRequestId: creationRequestId, onReady: revealQuestion,
      disabledReason: clarificationBlocked ? clarificationBlockedReason : undefined,
      submit: async (id, reply) => {
        if (selectedRef.current !== id || !currentDetail?.questionnaires?.some((question) => question.seq === reply.question_seq && question.status === 'pending')) throw new Error('请读取当前对话的最新问题后再回答。')
        if (pendingRef.current.has(`$control:${id}`) || (controlsRef.current[id]?.state ?? 'running') !== 'running') throw new Error(clarificationBlockedReason)
        const { next, order } = await requestDetail(`/api/tasks/${encodeURIComponent(id)}/questionnaires`, { method: 'POST', body: JSON.stringify(reply) })
        acceptDetail(next, order)
      },
    },
    disabledReason: clarificationBlocked ? clarificationBlockedReason : undefined,
    submit: async (id, reply) => {
      if (selectedRef.current !== id || !currentDetail?.clarifications?.some((question) => question.seq === reply.question_seq && question.status === 'pending')) throw new Error('请读取当前对话的最新问题后再回答。')
      if (pendingRef.current.has(`$control:${id}`) || (controlsRef.current[id]?.state ?? 'running') !== 'running') throw new Error(clarificationBlockedReason)
      const { next, order } = await requestDetail(`/api/tasks/${encodeURIComponent(id)}/clarifications`, { method: 'POST', body: JSON.stringify(reply) })
      acceptDetail(next, order)
    },
  }

  const jumpToLatest = () => {
    const stream = streamRef.current
    if (stream) stream.scrollTop = stream.scrollHeight
    followRef.current = true
    setUnread(0)
  }

  useLayoutEffect(() => {
    const stream = streamRef.current
    if (!stream || loading) return
    const view = `${selectedId}:${chatFilter}`
    const latest = visibleConversation.at(-1)?.id ?? 0
    const previous = seenRef.current
    if (previous.view !== view || followRef.current) {
      stream.scrollTop = stream.scrollHeight
      followRef.current = true
      setUnread(0)
    } else if (latest > previous.latest) {
      setUnread((current) => current + Math.max(1, visibleConversation.length - previous.count))
    }
    seenRef.current = { view, latest, count: visibleConversation.length }
  }, [selectedId, chatFilter, visibleConversation, loading])

  const verifyChain = async () => {
    setCheckingChain(true)
    try { setChainStatus(await api<ChainStatus>('/api/chain')) }
    catch { setChainStatus(null); setError('暂时无法核验事件链。') }
    finally { setCheckingChain(false) }
  }

  const openTaskGraph = () => {
    if (!selectedId) return
    setGraphLoaded(true)
    setGraphOpen(true)
  }

  const createTask = async () => {
    const submitted = draft
    const goal = submitted.trim()
    if (!goal || selectedRef.current !== null || pendingRef.current.has(newKey) || !recoveryReady || !pageReady || recovery) return
    if (readAccessToken() !== creationSessionToken) {
      clearCreationRecovery()
      creationRequest.current = null
      setRecoveryNotice('账户已变化，请刷新后核对任务列表；本次没有发送。')
      return
    }
    const navigation = navigationRef.current
    const previous = creationRequest.current
    if (previous && Date.now() - previous.createdAt >= CREATION_RECOVERY_TTL) {
      clearCreationRecovery(previous.requestId)
      creationRequest.current = null
      setRecoveryNotice('待确认创建记录已过期，请先核对任务列表；本次没有发送。')
      return
    }
    const attempt = previous?.text === submitted && previous.projectId === newProject?.id && previous.workspaceId === newWorkspaceId
      ? previous : { text: submitted, projectId: newProject?.id, workspaceId: newWorkspaceId, requestId: creationRequestId(), createdAt: Date.now() }
    creationRequest.current = attempt
    pendingRef.current.add(newKey); setPending(new Set(pendingRef.current))
    setSendErrors((current) => ({ ...current, [newKey]: '' }))
    try {
      const current = () => creationMounted.current && creationRequest.current === attempt
      const notice = await saveCreationRecovery(attempt, accessToken, current)
      if (!current() || readAccessToken() !== creationSessionToken) return
      setRecoveryNotice(notice)
      const { next: result, order } = await requestDetail('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          goal: attempt.text.trim(),
          project_id: attempt.projectId,
          ...(attempt.workspaceId ? { workspace_id: attempt.workspaceId } : {}),
          request_id: attempt.requestId,
          collaboration: 'team',
          run: true, background: true,
        }),
      })
      if (!creationMounted.current || readAccessToken() !== creationSessionToken) return
      validateTaskDetail(result)
      acceptDetail(result, order, navigationRef.current === navigation && selectedRef.current === null)
      clearCreationRecovery(attempt.requestId)
      if (creationRequest.current === attempt) {
        creationRequest.current = null
        setNewWorkspaceId(undefined)
        setRecoveryNotice('')
        setDrafts((current) => current[newKey] === submitted ? { ...current, [newKey]: '' } : current)
      }
    } catch (err) {
      if (creationMounted.current && creationRequest.current === attempt) setSendErrors((current) => ({ ...current, [newKey]: userFacingError(err, '创建任务失败') }))
    }
    finally { pendingRef.current.delete(newKey); if (creationMounted.current) setPending(new Set(pendingRef.current)) }
  }

  const beginDerivation = () => {
    if (!currentDetail || !selectedId || derivationRef.current) return
    const source = { id: selectedId, goal: currentDetail.task.goal }
    questionModeRef.current = false
    setQuestionMode(false)
    derivationRef.current = source
    navigationRef.current += 1
    focusComposerRef.current = true
    setDerivation(source)
  }

  const cancelDerivation = () => {
    derivationRef.current = null
    navigationRef.current += 1
    focusComposerRef.current = true
    setDerivation(null)
  }

  const toggleQuestion = () => {
    if (!currentDetail || !selectedId || teamChat) return
    derivationRef.current = null
    setDerivation(null)
    questionModeRef.current = !questionModeRef.current
    setQuestionMode(questionModeRef.current)
    navigationRef.current += 1
    focusComposerRef.current = true
  }

  const sendQuestion = async () => {
    const id = selectedId
    const submitted = message
    const key = composerKey
    if (!id || teamChat || !questionModeRef.current || derivationRef.current || selectedRef.current !== id || loading || !submitted.trim() || pendingRef.current.has(key)) return
    setChatFilter('all')
    const previous = questionRequests.current[key]
    const attempt = previous?.text === submitted ? previous : { text: submitted, requestId: creationRequestId() }
    questionRequests.current[key] = attempt
    pendingRef.current.add(key); setPending(new Set(pendingRef.current))
    setSendErrors((current) => ({ ...current, [key]: '' }))
    try {
      const { next: result, order } = await requestDetail(`/api/tasks/${encodeURIComponent(id)}/questions`, {
        method: 'POST', body: JSON.stringify({ question: submitted.trim(), request_id: attempt.requestId }),
      })
      validateTaskDetail(result, id)
      acceptDetail(result, order)
      if (questionRequests.current[key] === attempt) {
        delete questionRequests.current[key]
        setDrafts((current) => current[key] === submitted ? { ...current, [key]: '' } : current)
      }
    } catch (err) {
      if (questionRequests.current[key] === attempt) setSendErrors((current) => ({ ...current, [key]: userFacingError(err, '提问发送失败，请重试。') }))
    } finally {
      pendingRef.current.delete(key); setPending(new Set(pendingRef.current))
    }
  }

  const deriveTask = async () => {
    const source = derivation
    const submitted = message
    const goal = submitted.trim()
    if (!source || derivationRef.current !== source || selectedRef.current !== source.id || !goal || loading || pendingRef.current.has(composerKey)) return
    const key = composerKey
    const navigation = navigationRef.current
    const previous = derivationRequests.current[key]
    const attempt = previous?.text === submitted ? previous : { text: submitted, requestId: creationRequestId() }
    derivationRequests.current[key] = attempt
    pendingRef.current.add(key); setPending(new Set(pendingRef.current))
    setSendErrors((current) => ({ ...current, [key]: '' }))
    try {
      const { next: result, order } = await requestDetail(`/api/tasks/${encodeURIComponent(source.id)}/derive`, {
        method: 'POST', body: JSON.stringify({ goal, request_id: attempt.requestId, collaboration: 'team', run: true, background: true }),
      })
      validateTaskDetail(result)
      if (result.task.id === source.id) throw new Error('新对话尚未确认，草稿已保留，请核对任务列表后手动重试。')
      acceptDetail(result, order, navigationRef.current === navigation && derivationRef.current === source && selectedRef.current === source.id)
      // Edits invalidate this attempt, even when the user later restores the same text.
      if (derivationRequests.current[key] === attempt) {
        delete derivationRequests.current[key]
        setDrafts((current) => current[key] === submitted ? { ...current, [key]: '' } : current)
      }
    } catch (err) {
      if (derivationRequests.current[key] === attempt) setSendErrors((current) => ({ ...current, [key]: userFacingError(err, '创建新任务失败，请重试。') }))
    } finally {
      pendingRef.current.delete(key); setPending(new Set(pendingRef.current))
    }
  }

  const sendDirection = async () => {
    const id = selectedId
    const submitted = message
    if (!id || (questionModeRef.current && !teamChat) || derivationRef.current || selectedRef.current !== id || loading || !submitted.trim() || pendingRef.current.has(id)) return
    setChatFilter('all')
    const previous = conversationRequests.current[id]
    const attempt = previous?.text === submitted ? previous : { text: submitted, requestId: creationRequestId() }
    conversationRequests.current[id] = attempt
    pendingRef.current.add(id); setPending(new Set(pendingRef.current))
    setSendErrors((current) => ({ ...current, [id]: '' }))
    try {
      const { next: result, order } = await requestDetail(`/api/tasks/${id}/command`, { method: 'POST', body: JSON.stringify({ instruction: submitted.trim(), collaboration: 'team', continue: true, request_id: attempt.requestId }) })
      validateTaskDetail(result, id)
      acceptDetail(result, order)
      if (conversationRequests.current[id] === attempt) delete conversationRequests.current[id]
      setDrafts((current) => current[id] === submitted ? { ...current, [id]: '' } : current)
    } catch (err) { setSendErrors((current) => ({ ...current, [id]: userFacingError(err, '发送指令失败') })) }
    finally { pendingRef.current.delete(id); setPending(new Set(pendingRef.current)) }
  }

  const controlTask = async (action: ControlAction) => {
    const id = selectedId
    if (!id || selectedRef.current !== id || loading) return
    const key = `$control:${id}`
    if (pendingRef.current.has(key)) return
    const state = controlsRef.current[id]?.state ?? 'running'
    const previous = controlRequests[id]
    const retrying = previous?.action === action
    if (state === 'cancelled' && !(action === 'cancel' && retrying)) return
    if (!retrying && ((action === 'resume' && state !== 'paused') || (action === 'pause' && state !== 'running'))) return
    const attempt = retrying ? previous : { action, requestId: creationRequestId() }
    setControlRequests((current) => ({ ...current, [id]: attempt }))
    pendingRef.current.add(key); setPending(new Set(pendingRef.current))
    setSendErrors((current) => ({ ...current, [key]: '' }))
    try {
      const { next, order } = await requestDetail(`/api/tasks/${encodeURIComponent(id)}/control`, {
        method: 'POST', body: JSON.stringify({ action: attempt.action, request_id: attempt.requestId }),
      })
      acceptDetail(next, order)
      setControlRequests((current) => {
        if (current[id] !== attempt) return current
        const remaining = { ...current }
        delete remaining[id]
        return remaining
      })
    } catch (err) {
      setSendErrors((current) => ({ ...current, [key]: userFacingError(err, '执行控制请求失败，请重试。') }))
    } finally {
      pendingRef.current.delete(key); setPending(new Set(pendingRef.current))
    }
  }

  const removeConversation = async () => {
    const target = removeTarget
    if (!target) return
    const key = `$remove:${target.id}`
    if (pendingRef.current.has(key)) return
    const requestId = removalRequests.current[target.id] ?? creationRequestId()
    removalRequests.current[target.id] = requestId
    pendingRef.current.add(key)
    setRemoveBusy(true); setRemoveError('')
    try {
      const result = await api<{ id: string; removed: boolean; removed_seq: number }>(`/api/tasks/${encodeURIComponent(target.id)}/remove`, {
        method: 'POST', body: JSON.stringify({ request_id: requestId }),
      })
      if (result.id !== target.id || result.removed !== true || !Number.isSafeInteger(result.removed_seq) || result.removed_seq < 1) throw new Error('删除结果尚未确认，请沿用本次请求重试。')
      removedTasks.current.add(target.id)
      delete versionsRef.current[target.id]
      delete conversationRequests.current[target.id]
      setTasks(current => current.filter(task => !removedTasks.current.has(task.id)))
      setDrafts(current => Object.fromEntries(Object.entries(current).filter(([id]) => ![target.id, `$question:${target.id}`, `$derive:${target.id}`].includes(id))))
      if (selectedRef.current === target.id) { chooseTask(null); setDetail(null) }
      delete removalRequests.current[target.id]
      setRemoveTarget(null)
    } catch (err) {
      setRemoveError(userFacingError(err, '删除结果尚未确认，请重试核对。'))
    } finally {
      pendingRef.current.delete(key)
      setRemoveBusy(false)
    }
  }

  const team = organization ? [...organization.workers, organization.leader, ...organization.cabinet] : roles
  const names = Object.fromEntries(Object.entries(identities).map(([id, member]) => [id, member.name]))
  const participants = task ? conversationParticipants(task, organization?.leader.id ?? 'store_manager', team.map((member) => member.id), [...(currentDetail?.questions ?? []), ...(currentDetail?.clarifications ?? [])].map((question) => question.role_id), currentDetail?.events) : []
  const rulesControl = <RulesPanel key={`rules:${selectedId ?? '$new'}`} request={api} taskId={selectedId ?? undefined} projectId={task ? task.project_id : newProject?.id} draft={task ? message : draft} busy={Boolean(loading || isSending || currentDetail?.activity?.active)} onProjectChange={async (project) => {
    if (selectedId) {
      if (!project) return
      const { next: result, order } = await requestDetail(`/api/tasks/${selectedId}/project`, { method: 'POST', body: JSON.stringify({ project_id: project.id }) })
      acceptDetail(result, order)
    } else {
      if (project?.id !== newProject?.id) {
        navigationRef.current += 1
        if (creationRequest.current) clearCreationRecovery(creationRequest.current.requestId)
        creationRequest.current = null
        setSendErrors((current) => ({ ...current, [newKey]: '' }))
      }
      setNewProject(project)
    }
  }} onRecorded={(text) => { const key = selectedId ?? newKey; setDrafts((current) => current[key]?.trim() === text ? { ...current, [key]: '' } : current) }} onUpdated={() => setReloadToken((value) => value + 1)} />
  return (
    <ReloadContext.Provider value={() => reloadPage(() => pageSnapshot.current, creationSessionToken, () => reloadBlocker(Boolean(pendingRef.current.size), clarificationStore, questionnaireStore))}>
    <div className="cater-app text-[#253d38]">
      <header className="app-header flex h-[72px] items-center justify-between px-4 sm:px-6">
        <button type="button" onClick={() => chooseTask(null)} aria-label="Cater · 返回新对话" title="返回新对话" className="brand-home flex min-w-0 items-center gap-3 rounded-lg text-left">
          <span className="brand-mark grid size-11 shrink-0 place-items-center rounded-lg text-white"><Cat size={23} strokeWidth={2.2} /></span>
          <span className="min-w-0"><strong className="block text-[19px] font-extrabold leading-5 text-[#713f34]">Cater</strong><span className="mt-1 block text-[11px] font-semibold text-[#a9604f]">有猫的工作间</span></span>
        </button>
        <div className="flex items-center gap-1 text-xs sm:gap-2">
          <TeamDirectory members={team} availability={organization?.availability} open={teamOpen} onOpenChange={setTeamOpen} refreshing={teamRefreshing} error={teamError || (!organization ? rolesError : '')} onRefresh={() => void refreshOrganization()} portrait={(id) => <Portrait id={id} size="xl" />} onMention={(name) => { if (task) setMessage(`${message.trim()}${message.trim() ? ' ' : ''}@${name} `); else setDraft((current) => `${current.trim()}${current.trim() ? ' ' : ''}@${name} `) }} />
          <span className={`channel-mode max-w-32 shrink-0 rounded-md px-2.5 py-1.5 text-[11px] font-medium sm:max-w-none ${driverModeStyle(organization?.mode)}`} title={driverModeText(organization?.mode)}><span className="sr-only sm:not-sr-only">{driverModeText(organization?.mode)}</span><span className="sm:hidden" aria-hidden="true">{organization?.mode === 'external_driver' ? '外接' : organization?.mode === 'mixed_driver' ? '混合' : organization?.mode === 'local_demo' ? '演示' : '待确认'}</span></span>
          <button type="button" onClick={() => void verifyChain()} disabled={checkingChain} className={`inline-flex size-8 items-center justify-center rounded-md hover:bg-[#e7f0ec] sm:w-auto sm:gap-1 sm:px-2 ${chainStatus && !chainStatus.ok ? 'text-rose-700' : 'text-[#34786e]'}`} aria-label={checkingChain ? '正在核验事件链' : chainStatus ? chainStatus.ok ? `上次核验通过，共 ${chainStatus.checked} 条事件` : `事件链异常，事件 #${chainStatus.broken_at ?? '?'}` : '核验事件链'} title={chainStatus?.reason || '点击核验事件链；显示的是上次核验结果'}><ShieldCheck size={17} /><span className="hidden sm:inline">{checkingChain ? '正在核验' : chainStatus ? chainStatus.ok ? `已核验 ${chainStatus.checked} 条` : `链异常 · #${chainStatus.broken_at ?? '?'}` : '核验事件链'}</span></button>
          <InstanceInfo loadInstance={loadInstance} />
        </div>
      </header>
      <main data-conversation={Boolean(task)} className={`app-layout grid grid-cols-1 ${task ? 'lg:grid-cols-[252px_minmax(0,1fr)_318px]' : 'lg:grid-cols-[252px_minmax(0,1fr)]'}`}>
        <aside className="task-sidebar min-h-0 lg:flex lg:flex-col lg:overflow-y-auto" onKeyDown={event => {
          if (task && !tasksCollapsed && event.key === 'Escape') {
            event.preventDefault()
            setTasksCollapsed(true)
            event.currentTarget.querySelector<HTMLButtonElement>('[aria-controls="task-list"]')?.focus()
          }
        }}>
          <div className="task-sidebar-heading flex items-center justify-between px-4 pb-2 pt-4">
            <div><span className="task-sidebar-eyebrow text-[11px] font-semibold text-[#a56b56]">正在聊</span><h2 className="flex items-center gap-1.5 text-base font-bold"><PawPrint size={17} className="text-[#c36550]" /> 我的任务 <span className="text-xs font-medium text-[#a98e81]">{tasks.length}</span></h2></div>
            <div className="flex gap-1">
              <button type="button" onClick={() => { setTaskSearchOpen(!(taskSearchOpen && !tasksCollapsed)); setTasksCollapsed(false) }} aria-label={taskSearchOpen && !tasksCollapsed ? '收起任务查找' : '查找任务'} title={taskSearchOpen && !tasksCollapsed ? '收起任务查找' : '查找任务'} aria-controls="task-search" aria-expanded={taskSearchOpen && !tasksCollapsed} className="grid size-9 place-items-center rounded-md text-[#a9604f] hover:bg-[#fff5eb] lg:hidden"><Search size={17} /></button>
              <button type="button" onClick={() => setTasksCollapsed(!tasksCollapsed)} aria-controls="task-list" aria-expanded={!tasksCollapsed} aria-label={tasksCollapsed ? '展开任务列表' : '收起任务列表'} title={tasksCollapsed ? '展开任务列表' : '收起任务列表'} className="grid size-9 place-items-center rounded-md text-[#a9604f] hover:bg-[#fff5eb] lg:hidden"><ChevronDown size={18} className={tasksCollapsed ? '' : 'rotate-180'} /></button>
              <button className="grid size-9 place-items-center rounded-md border border-[#ead2c5] bg-white text-[#b15342] shadow-sm hover:bg-[#fff0e9]" title="新建任务" aria-label="新建任务" onClick={() => chooseTask(null)}><Plus size={18} /></button>
            </div>
          </div>
          <TaskList tasks={tasks} executionState={item => executionControls[item.id]?.state ?? item.execution_control?.state} selectedId={selectedId} drafts={drafts} collapsed={tasksCollapsed} searchOpen={taskSearchOpen} onChoose={id => { chooseTask(id); setTasksCollapsed(true); focusTaskHeadingRef.current = id !== selectedId }} statusLabel={(item) => executionLabel(executionControls[item.id]?.state) || (item.team?.enabled ? teamStatus(item.team, item.activity) : item.status_label)} />
        </aside>
        <section ref={chatPaneRef} className="chat-pane flex min-h-0 flex-col">
          {recovery && <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[#eaded1] px-4 py-2 text-xs text-[#52766d] sm:px-7" role="status">
            <span>有一项新建任务结果待确认，可能已经创建。</span>
            <button type="button" className="inline-flex items-center gap-1 font-semibold text-[#24766b]" onClick={() => void restoreCreation()}><RefreshCw size={14} />恢复待确认任务</button>
            <button type="button" className="text-stone-500" onClick={() => { clearCreationRecovery(recovery.requestId); setRecovery(null); setRecoveryNotice('已放弃恢复，请先核对任务列表，避免重复创建。') }}>放弃恢复</button>
          </div>}
          {task ? <>
            <div className="chat-heading flex items-start justify-between gap-3 px-4 py-3 sm:px-7 sm:py-4" style={{ backgroundImage: `linear-gradient(90deg, #fffaf5 0%, rgba(255,250,245,.98) 55%, rgba(255,250,245,.78) 100%), url(${cafeWelcome})` }}>
              <div className="flex min-w-0 flex-1 items-start gap-3"><Portrait id={organization?.leader.id ?? 'store_manager'} /><div className="min-w-0 flex-1"><p className="mb-1 text-[11px] font-semibold text-[#a55642]">店长 · 这件事我在跟进</p><div className="flex min-w-0 items-start gap-1"><h1 tabIndex={-1} className="line-clamp-2 min-w-0 [overflow-wrap:anywhere] text-[16px] font-bold leading-snug sm:text-lg">{heading.label}</h1><button type="button" onClick={() => setGoalExpanded(value => !value)} aria-label={goalExpanded ? '收起完整目标' : '查看完整目标'} title={goalExpanded ? '收起完整目标' : '查看完整目标'} aria-expanded={goalExpanded} aria-controls="conversation-goal" className="grid size-8 shrink-0 place-items-center rounded-md text-[#34786e] hover:bg-[#e7f0ec]"><ChevronDown size={17} className={goalExpanded ? 'rotate-180' : ''} /></button></div>{heading.excerpt && <span className="text-[10px] text-[#72877c]">目标节选</span>}</div></div>
              <div className="flex shrink-0 items-center gap-2"><span className={`status-pill hidden sm:inline-flex ${executionState === 'running' && currentActivity?.active && currentActivity.state === executionState ? 'is-running' : ''}`}>{currentStatusLabel}</span><button id="workboard-trigger" onClick={() => setShowMobileBoard(true)} aria-label="打开成果与过程" title="打开成果与过程" aria-haspopup="dialog" aria-expanded={showMobileBoard} className="rounded-md border border-[#d3dfd9] bg-white px-2.5 py-1.5 text-xs font-semibold text-[#24766b] lg:hidden">成果</button></div>
            </div>
            {goalExpanded && <section id="conversation-goal" aria-label="当前完整目标" className="shrink-0 border-b border-[#d3dfd9] px-4 py-2 sm:px-7">
              <div className="mb-1 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-[11px] text-[#52766d]"><h2 className="font-semibold">当前完整目标</h2><span className="min-w-0 [overflow-wrap:anywhere]">任务编号：{task.id}</span></div>
              <p className="overflow-y-auto whitespace-pre-wrap text-xs leading-5 [overflow-wrap:anywhere]" style={{ maxHeight: 'min(32dvh, 16rem)' }}>{task.goal}</p>
            </section>}
            <div className="filter-strip flex shrink-0 flex-wrap items-center justify-between gap-2 px-4 py-2 sm:px-7"><ChatFilters value={chatFilter} onChange={setChatFilter} />
              <div className="chat-tools flex max-w-full flex-wrap items-center gap-2">
                <ConversationMembers key={task.id} members={team} participants={participants} portrait={(id, size) => <Portrait id={id} size={size} />} onMention={(id) => setMessage(`${message.trim()}${message.trim() ? ' ' : ''}@${names[id] ?? id} `)} onOpenDirectory={() => { setTeamOpen(true); void refreshOrganization() }} />
                {currentDetail?.background && <ConversationOrigin key={`origin:${task.id}`} background={currentDetail.background} onNavigate={(id) => { chooseTask(id); focusComposerRef.current = true }} />}
                <WorkspacePanel taskId={task.id} workspace={currentDetail?.workspace} disabled={loading || isSending || controlBusy}
                  active={workspaceActive}
                  loadOptions={loadWorkspaceOptions} onBind={async (change) => {
                    const { next, order } = await requestDetail(`/api/tasks/${encodeURIComponent(task.id)}/workspace`, { method: 'POST', body: JSON.stringify(change) })
                    acceptDetail(next, order)
                    return next.workspace
                  }} onRefresh={async () => {
                    const { next, order } = await requestDetail(`/api/tasks/${encodeURIComponent(task.id)}`)
                    acceptDetail(next, order)
                    return next.workspace
                  }} />
                {!task.team?.enabled && <button type="button" className="origin-tool" aria-label="提问" title={questionMode ? '取消提问，返回协作草稿' : '向当前对话提问'} aria-pressed={questionMode} disabled={loading} onClick={toggleQuestion}><CircleHelp size={17} aria-hidden="true" /></button>}
                <button type="button" className="origin-tool" aria-label="基于此对话开新任务" title="基于此对话开新任务" aria-pressed={Boolean(derivation)} disabled={loading || Boolean(derivation)} onClick={beginDerivation}><GitFork size={17} aria-hidden="true" /></button>
                <TaskControlMenu key={`control:${task.id}`} state={executionState} busy={controlBusy} disabled={loading} retry={sendErrors[controlKey] ? controlRequests[task.id]?.action : undefined} onAction={(action) => void controlTask(action)} onDelete={() => { setRemoveError(''); setRemoveTarget({ id: task.id, title: task.goal }) }} />
                {task.team?.enabled || task.work?.items.length ? <button ref={graphTriggerRef} id="task-graph-trigger" type="button" aria-label="任务脉络" title="查看任务脉络" aria-haspopup="dialog" aria-expanded={graphOpen} onClick={openTaskGraph} className="origin-tool"><GitBranch size={17} aria-hidden="true" /></button> : null}
              </div>
            </div>
            {!loading && (executionState !== 'running' || controlBusy) && <div className="shrink-0 break-words px-4 py-2 text-xs leading-5 text-[#52766d] sm:px-7">
              <p role="status" aria-label="执行状态">
              {executionState === 'paused' ? '已暂停后续执行；在途调用仍可能返回。仍可直接聊天。'
                : executionState === 'cancelled' ? '已取消后续执行，不可恢复；在途调用仍可能返回。继续执行需新建任务，仍可直接聊天。' : ''}
              {controlBusy && controlRequests[task.id] ? ` ${controlLabels[controlRequests[task.id].action]}请求待确认。` : ''}
              </p>
              {executionState !== 'running' && executionControl?.reason && <details key={`reason:${task.id}:${executionControl.seq}`} className="mt-1">
                <summary className="cursor-pointer font-semibold">{executionState === 'paused' ? '暂停说明' : '取消说明'}</summary>
                <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap [overflow-wrap:anywhere]">{executionControl.reason}</p>
              </details>}
            </div>}
            {graphLoaded && (task.team?.enabled || task.work?.items.length) ? <TaskGraphDialog open={graphOpen} onOpenChange={setGraphOpen} returnFocusRef={graphTriggerRef}
              description={task.team?.enabled ? '当前团队的只读消息与复核关系' : `当前任务 · ${task.work!.items.length} 个工作项`}>
              <AsyncPanel fallback={<p role="status" className="task-graph-loading"><LoaderCircle size={20} aria-hidden="true" className="shrink-0 motion-safe:animate-spin" />正在读取任务脉络…</p>}><TaskGraph key={task.id} {...(task.team?.enabled ? { team: task.team, events: currentDetail?.events ?? [], statusLabel: currentStatusLabel, motionAllowed: executionState === 'running' && currentActivity?.active === true } : { work: task.work! })} actorText={(id) => names[id] ?? actorText(id)} portrait={(id, size) => <Portrait id={id} size={size} />} /></AsyncPanel>
            </TaskGraphDialog> : null}
            <div className="relative flex min-h-0 flex-1 flex-col">
              <div ref={streamRef} className="chat-stream min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-7" aria-label="聊天记录" aria-busy={loading} onScroll={(event) => { const stream = event.currentTarget; followRef.current = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60; if (followRef.current) setUnread(0) }}>
                {loading ? <ChatWaiting error={loadError} onRetry={() => setReloadToken((current) => current + 1)} /> : <>
                  {visibleConversation.length ? <AsyncPanel fallback={<p role="status" className="text-xs text-[#52766d]">正在读取对话…</p>}><ChatConversation key={task.id} items={visibleConversation} events={currentDetail?.events ?? []} names={names} renderItem={(item) => <ChatBubble key={item.id} item={item} artifact={task.deliverables?.find((artifact) => artifact.seq === item.evidence.seq)} taskId={task.id} controls={clarificationControls} />} /></AsyncPanel>
                    : <p className="py-10 text-center text-sm text-[#8ba09a]">这一栏暂时还没有新消息。</p>}
                </>}
              </div>
              {!loading && unread > 0 && <button onClick={jumpToLatest} className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-2 whitespace-nowrap rounded-md border border-[#d9b9a5] bg-[#fff7eb] px-3 py-2 text-xs font-semibold text-[#915340] shadow-md" title="返回最新消息"><ArrowDown size={15} /> 查看新消息 · {unread}</button>}
            </div>
            {!loading && currentDetail && task.team?.enabled && <div className="shrink-0 px-4 sm:px-7"><AsyncPanel fallback={<p role="status" className="py-2 text-xs text-[#52766d]">正在读取工作进展…</p>}><ConversationActivity key={task.id} team={task.team} events={currentDetail.events} names={names} leaderId={organization?.leader.id ?? 'store_manager'} sending={pending.has(task.id)} activity={currentActivity} executionState={executionState} syncError={loadError} controlBusy={controlBusy} portrait={(id) => <Portrait id={id} size="sm" />} onInspect={() => setShowMobileBoard(true)} onRefresh={() => setReloadToken((current) => current + 1)} onPause={() => void controlTask('pause')} /></AsyncPanel></div>}
            {derivation ? <DerivationDraftBar goal={derivation.goal} onCancel={cancelDerivation} /> : legacyQuestionMode ? <QuestionComposerBar onCancel={toggleQuestion} /> : rulesControl}
            <DirectionComposer key={composerKey} members={team} message={message} isSending={isSending} isLoading={loading} deriving={Boolean(derivation)} questioning={legacyQuestionMode} onMessageChange={setMessage} onSend={() => void (derivation ? deriveTask() : legacyQuestionMode ? sendQuestion() : sendDirection())} />
          </> : <><EmptyConversation onChoose={setDraft} />{isSending && <div className="px-4 sm:px-7"><ChatWaiting text="正在发送给店长，等待接收回执" error="" onRetry={() => {}} /></div>}{rulesControl}<TaskComposer members={team} draft={draft} isSending={isSending} disabled={!recoveryReady || !pageReady || Boolean(recovery)} workspaceControl={<WorkspacePanel draft workspaceId={newWorkspaceId} disabled={!recoveryReady || !pageReady || Boolean(recovery)} loadOptions={loadWorkspaceOptions} onSelect={selectDraftWorkspace} />} onDraftChange={setDraft} onCreate={() => void createTask()} /></>}
          {recoveryNotice && <p role="status" className="shrink-0 px-4 pb-2 text-xs text-amber-800 sm:px-7">{recoveryNotice}</p>}
          {(visibleError || (!loading && loadError)) && <p role="alert" className="mx-4 mb-3 rounded-md border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700 sm:mx-7">{visibleError || loadError}</p>}
        </section>
        {task && <WorkboardPanel taskId={task.id} open={showMobileBoard} onOpenChange={setShowMobileBoard}>
          {currentDetail ? <>{!task.team?.enabled && <><WorkItems work={task.work} names={names} /><DeliveryPanel key={task.id} taskId={task.id} delivery={task.work?.delivery} names={names} accessToken={accessToken} /><VerificationBoard items={task.verifications ?? []} /></>}<Workboard task={task} detail={currentDetail} activity={currentActivity} names={names} accessToken={accessToken} executionState={executionState} driverCoverage={organization?.worker_drivers} showEvidence={showEvidence} onToggleEvidence={() => setShowEvidence(!showEvidence)} />{Boolean(currentDetail.organization_memory?.length) && <details className="mt-5 border-t border-[#dce8e1] pt-4"><summary className="cursor-pointer text-xs font-semibold text-[#52766d]">猫咖记忆 · {currentDetail.organization_memory?.length} 项</summary><div className="mt-3"><MemoryBoard items={currentDetail.organization_memory ?? []} names={names} /></div></details>}</> : <p className="text-xs leading-6 text-[#74897d]">正在读取这件事的成果与过程。</p>}
        </WorkboardPanel>}
      </main>
      {removeTarget && <DeleteConversationDialog title={removeTarget.title} busy={removeBusy} error={removeError}
        onClose={() => setRemoveTarget(null)} onConfirm={() => void removeConversation()} />}
    </div>
    </ReloadContext.Provider>
  )
}

function ChatWaiting({ error, onRetry, text = '正在接上这段对话' }: { error: string; onRetry: () => void; text?: string }) {
  return <div className="flex items-start gap-3 py-5"><Portrait id="store_manager" /><div className="min-w-0 text-sm leading-6 text-[#6e8175]"><p role={error ? 'alert' : 'status'} className="flex items-center gap-2">{!error && <LoaderCircle size={16} className="shrink-0 motion-safe:animate-spin" />}{error || text}</p>{error && <button onClick={onRetry} className="mt-3 inline-flex items-center gap-2 rounded-md border border-[#dec8b8] bg-white px-3 py-1.5 text-xs text-[#9e5946]"><RefreshCw size={14} /> 重新连接</button>}</div></div>
}

function DirectionComposer({ members, message, isSending, isLoading, deriving = false, questioning = false, onMessageChange, onSend }: { members: OrgRole[]; message: string; isSending: boolean; isLoading: boolean; deriving?: boolean; questioning?: boolean; onMessageChange: (value: string) => void; onSend: () => void }) {
  const sendLabel = deriving ? '创建衍生任务' : questioning ? '发送提问' : '发送消息'
  return <div className="composer-shell p-3 sm:p-4"><div className="composer-box mx-auto flex max-w-3xl gap-2 p-2"><MentionInput value={message} label={deriving ? '衍生任务目标' : questioning ? '向当前对话提问' : '向团队发送消息'} placeholder={deriving ? '说说新任务要完成什么，可直接 @一位伙伴' : questioning ? '想了解什么？' : '继续说吧，补充想法、改目标，或直接 @一位伙伴'} members={members} onChange={onMessageChange} onSubmit={onSend} portrait={(id) => <Portrait id={id} size="sm" />} /><button disabled={isSending || isLoading || !message.trim()} onClick={onSend} className="send-button grid size-10 shrink-0 place-items-center self-end text-white disabled:bg-[#d4ddd8]" title={isLoading ? '正在接上对话' : isSending ? '正在发送' : sendLabel} aria-label={sendLabel} aria-busy={isSending}>{isSending ? <LoaderCircle size={20} className="motion-safe:animate-spin" /> : <PawPrint size={20} fill="currentColor" strokeWidth={1.8} />}</button></div></div>
}

function TaskComposer({ members, draft, isSending, disabled, workspaceControl, onDraftChange, onCreate }: { members: OrgRole[]; draft: string; isSending: boolean; disabled: boolean; workspaceControl: ReactNode; onDraftChange: (value: string) => void; onCreate: () => void }) {
  return <div className="composer-shell p-3 sm:p-4"><div className="composer-box mx-auto flex max-w-3xl gap-2 p-2"><MentionInput id="new-task" value={draft} label="发起任务对话" placeholder="和店长说说，今天想做什么？" members={members} onChange={onDraftChange} onSubmit={onCreate} portrait={(id) => <Portrait id={id} size="sm" />} /><div className="flex shrink-0 items-center gap-1 self-end">{workspaceControl}<button disabled={disabled || isSending || !draft.trim()} onClick={onCreate} className="send-button grid size-10 shrink-0 place-items-center self-end text-white disabled:bg-[#d4ddd8]" title={isSending ? '正在发送新任务' : '发送新任务'} aria-label="发送新任务" aria-busy={isSending}>{isSending ? <LoaderCircle size={20} className="motion-safe:animate-spin" /> : <PawPrint size={20} fill="currentColor" strokeWidth={1.8} />}</button></div></div></div>
}

function MessageContext({ item, taskId, controls }: { item: ChatItem; taskId: string; controls: ChatControls }) {
  return <>
    {item.handoff && <div className="handoff-line" aria-label="工作交接">
      {item.assignments?.length ? <AsyncPanel fallback={null}><WorkAssignments items={item.assignments} /></AsyncPanel> : <>
        {item.handoff.owners.length > 0 && <p>接手：{item.handoff.owners.map((name) => `@${name}`).join('、')}</p>}
        {item.handoff.reviewers.length > 0 && <p>复核：{item.handoff.reviewers.map((name) => `@${name}`).join('、')}</p>}
      </>}
    </div>}
    {item.joined?.length ? <p className="collaboration-joined" aria-label="协作通知">{item.joined.join('、')} 加入协作</p> : null}
    {item.question && <AsyncPanel fallback={<p className="question-progress">正在读取提问详情…</p>}><QuestionDetails question={item.question} human={item.kind === 'human'} /></AsyncPanel>}
    {item.questionnaire && <AsyncPanel fallback={<p role="status" className="question-progress">正在读取补充问题…</p>}><QuestionnaireCard taskId={taskId} questionnaire={item.questionnaire} controls={controls.questionnaire} /></AsyncPanel>}
    {item.evidence.type === 'clarification.requested' && <>
      <p className="mt-2 text-xs text-teal-700">@你</p>
      {item.clarification ? <AsyncPanel fallback={<p role="status" className="question-progress">正在读取补充问题…</p>}><ClarificationCard taskId={taskId} question={item.clarification} controls={controls} /></AsyncPanel> : <p className="question-progress">此条历史问题暂无可提交状态</p>}
    </>}
  </>
}

function AuditTrail({ item }: { item: ChatItem }) {
  const [open, setOpen] = useState(false)
  return <details className="chat-audit" onToggle={(event) => { if (event.target === event.currentTarget) setOpen(event.currentTarget.open) }}>
    <summary>事件 #{item.id} · 审计记录</summary>
    {open && <AsyncPanel fallback={<p>正在读取过程记录…</p>}><EventDetails events={item.auditEvents ?? [item.evidence]} actorName={item.auditName ?? actorText} /></AsyncPanel>}
  </details>
}

function ArtifactPreview(props: { item: Artifact; publisher?: string; showSummary?: boolean; taskId?: string; accessToken?: string }) {
  return <AsyncPanel fallback={<p role="status" className="question-progress">正在读取成果…</p>}><LazyArtifactPreview {...props} /></AsyncPanel>
}

function browserCheckDetails(planValue: unknown): string[] {
  const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
  const plan = record(planValue)
  const details: string[] = []
  if (typeof plan.entrypoint === 'string') details.push(`浏览器入口：${plan.entrypoint.slice(0, 500)}`)
  if (Array.isArray(plan.steps)) plan.steps.slice(0, 20).forEach((step, index) => {
    const label = record(step).label
    if (typeof label === 'string') details.push(`计划步骤 ${index + 1}：${label.slice(0, 200)}`)
  })
  return details
}

function SystemRecord({ item, taskId, controls }: { item: ChatItem; taskId: string; controls: ChatControls }) {
  const checking = item.evidence.type === 'verification.completed'
  const passed = checking && item.evidence.payload.outcome === 'passed'
  const planning = ['verification.planning_started', 'verification.planned'].includes(item.evidence.type)
  const technical = checking || planning || item.evidence.type === 'agent.discipline_flagged' || item.title === '复核记录整理中'
  const failed = (checking && !passed) || (item.evidence.type === 'verification.planned' && !['ready', 'correcting'].includes(String(item.evidence.payload.status)))
  const [expanded, setExpanded] = useState(failed)
  const responseAttention = isResponseFormatError(item.evidence) || (item.evidence.type === 'conversation.finished'
    && ['clarification', 'outdated', 'invalid', 'blocked', 'uncertain'].includes(String(item.evidence.payload.status)))
  const attention = responseAttention || failed || item.retryStatus === 'failed' || ['activity.failed', 'driver.failed', 'driver.budget_exhausted'].includes(item.evidence.type)
  const Icon = attention ? TriangleAlert : passed || item.retryStatus === 'succeeded' ? CheckCircle2 : Info
  const heading = <><strong>系统</strong>{item.title && <span> · {item.title}</span>}</>
  const body = <>
    <p className={checking ? 'font-mono' : undefined}>{item.text}</p>
    {item.details?.length ? <ul>{item.details.map((detail, index) => <li key={index} className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{detail}</li>)}</ul> : null}
    {checking && <CommandOutputs stdout={hasBrowserReceipt(item.evidence.payload) ? undefined : item.evidence.payload.stdout} stderr={item.evidence.payload.stderr} limit={800} />}
    {checking && expanded && hasBrowserReceipt(item.evidence.payload) && <AsyncPanel fallback={<p>正在读取浏览器执行回执…</p>}>
      <BrowserReport report={item.evidence.payload.browser_report} />
    </AsyncPanel>}
    <MessageContext item={item} taskId={taskId} controls={controls} />
    <AuditTrail item={item} />
  </>
  return <article role={responseAttention || item.retryStatus === 'failed' ? 'alert' : undefined} className={`system-record-row${attention ? ' needs-attention' : ''}`}>
    <Icon size={16} aria-hidden="true" />
    <div data-actor="system" className="message-bubble is-system system-record">
      {technical ? <details open={failed} onToggle={(event) => setExpanded(event.currentTarget.open)}>
        <summary>{heading}</summary>
        <div>{body}</div>
      </details> : <><header>{heading}</header>{body}</>}
    </div>
  </article>
}

function untrustedArtifact(event: Event): Artifact {
  const item = { ...event.payload, seq: event.seq } as Artifact
  delete item.local_path
  delete item.local_save_error
  return item
}

function ChatBubble({ item, artifact, taskId, controls }: { item: ChatItem; artifact?: Artifact; taskId: string; controls: ChatControls }) {
  if (item.notice) return <article className="collaboration-notice">
    {item.text && <p>{item.text}</p>}
    {item.details?.length ? <details><summary>{item.evidence.payload.kind === 'notification' ? '这条知会的内容' : '这次接力的工作'}</summary>{item.details.map((detail, index) => <p key={index}>{detail}</p>)}</details> : null}
    <MessageContext item={item} taskId={taskId} controls={controls} />
    <AuditTrail item={item} />
  </article>
  if (item.systemActor) return <SystemRecord item={item} taskId={taskId} controls={controls} />
  const human = item.kind === 'human'
  return <article className={`flex items-start gap-3 ${human ? 'justify-end' : ''}`}>
    {!human && <Portrait id={item.actor} />}
    <div className={`message-bubble min-w-0 max-w-[min(86%,700px)] px-4 py-3 ${item.questionnaire ? 'w-full' : ''} ${human ? 'is-human text-white' : 'is-agent'}`} data-actor={item.actor}>
      <div className={`mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs ${human ? 'text-[#fff0e8]' : 'text-[#6e8981]'}`}><strong className={human ? 'text-white' : 'text-[#28433d]'}>{human ? '你' : item.display}</strong><span>{item.role}</span>{item.title && <span className={`text-[11px] font-semibold ${human ? 'text-[#fff4d9]' : 'text-[#a56049]'}`}>· {item.title}</span>}</div>
      {human ? <p className="whitespace-pre-wrap break-words text-[13px] leading-[1.75] sm:text-sm">{item.text}</p>
        : <AsyncPanel fallback={<p className="whitespace-pre-wrap break-words text-[13px] leading-[1.75] sm:text-sm">{item.text}</p>}><ChatMarkdown text={item.text} /></AsyncPanel>}
      <MessageContext item={item} taskId={taskId} controls={controls} />
      {item.progress && <AsyncPanel fallback={null}><WorkProgressPanel events={item.progress} claim={item.id} name={item.display} /></AsyncPanel>}
      {item.progress && <WorkspaceToolProgress events={item.progress} claim={item.id} name={item.display} />}
      {item.evidence.type === 'deliverable.published' && <ArtifactPreview item={artifact ?? untrustedArtifact(item.evidence)} publisher={item.display} showSummary={false} taskId={taskId} accessToken={accessToken} />}
      {(item.targets.length > 0 || item.replyTo) && <div className={`mt-2 flex flex-wrap gap-2 text-xs ${human ? 'text-[#fde9dd]' : 'text-teal-700'}`}>{item.internal && item.targets.length > 0 && <span>收件人：</span>}{item.targets.map((target) => <span key={target}>@{target}</span>)}{item.replyTo && <span className="inline-flex items-center gap-1"><CornerDownRight size={13} /> 回复 #{item.replyTo}</span>}</div>}
      {(item.replyEvidence || item.details?.length || item.evidence.type === 'team.objective_completed') ? <AsyncPanel fallback={null}><MessageDetails item={item} /></AsyncPanel> : null}
      <AuditTrail item={item} />
    </div>
  </article>
}

function DeliveryPanel(props: ComponentProps<typeof LazyDeliveryPanel>) {
  return <AsyncPanel fallback={<p className="text-xs text-stone-500" role="status">正在读取交付记录…</p>}><LazyDeliveryPanel {...props} /></AsyncPanel>
}

function MemoryBoard({ items, names }: { items: MemoryItem[]; names: Record<string, string> }) {
  if (!items.length) return null
  return <section className="mb-5 border-b border-[#eaded1] pb-5"><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">猫咖记忆</h2><span className="text-xs text-stone-400">已发布成果</span></div><div className="space-y-2">{items.map((item) => <div key={item.seq} className="border-l-2 border-sky-600 pl-3 py-1"><strong className="block text-xs text-stone-800">{item.title}</strong><p className="mt-1 text-xs leading-5 text-stone-600">{item.summary}</p><span className="mt-1 block text-[11px] text-stone-400">{names[item.actor ?? ''] ?? actorText(item.actor ?? '猫咖')} · 事件 #{item.seq}</span></div>)}</div></section>
}

function VerificationBoard({ items }: { items: Verification[] }) {
  if (!items.length) return null
  return <section className="mb-5 border-b border-[#eaded1] pb-5"><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">本机检查</h2><span className="text-xs text-stone-400">显式运行 · {items.length} 次</span></div><div className="space-y-3">{[...items].reverse().slice(0, 3).map((item) => <div key={item.seq} className={`border-l-2 pl-3 py-1 ${item.outcome === 'passed' ? 'border-emerald-600' : 'border-rose-600'}`}><strong className="text-xs text-stone-800">{item.outcome === 'passed' ? '命令通过' : item.outcome === 'timeout' ? '执行超时' : item.outcome === 'launch_error' ? '无法启动' : item.outcome === 'unavailable' ? '检查工具不可用' : '命令失败'}</strong><p className="mt-1 break-all font-mono text-[11px] leading-5 text-stone-600">{item.command.join(' ')}</p><p className="mt-1 text-[11px] text-stone-500">事件 #{item.seq}{item.for_event ? ` · 关联自报 #${item.for_event}` : ''} · 退出码 {item.exit_code ?? '无'}</p>{item.outcome === 'unavailable' && <p className="mt-1 text-[11px] text-amber-700">工具不可用，不能据此认定成果错误。</p>}{item.git?.dirty && <p className="mt-1 text-[11px] text-amber-700">工作区有未提交改动，未绑定具体文件内容</p>}</div>)}</div><p className="mt-2 text-[11px] leading-5 text-stone-500">单条命令通过，不代表诊断或整项任务已证实。</p></section>
}

function Workboard({ task, detail, activity, names, accessToken, executionState, driverCoverage, showEvidence, onToggleEvidence }: { task: Task; detail: TaskDetail | null; activity?: Activity; names: Record<string, string>; accessToken?: string; executionState: ExecutionControl['state']; driverCoverage?: DriverCoverage[]; showEvidence: boolean; onToggleEvidence: () => void }) {
  if (task.team?.enabled) return <AsyncPanel fallback={<p role="status" className="text-xs text-[#52766d]">正在读取团队进度…</p>}><TeamWorkboard team={task.team} taskId={task.id} artifacts={task.deliverables ?? []} events={detail?.events ?? []} names={names} accessToken={accessToken} executionState={executionState} activity={activity} /></AsyncPanel>
  const assignments = [...task.assignments].reverse().filter((item, index, all) => all.findIndex((other) => other.owner === item.owner) === index).filter((item) => item.status === undefined || item.status === 'assigned')
  const participants = assignments.length
    ? assignments.map((item) => item.owner)
    : [...new Set(task.messages.map((item) => item.actor).filter((actor) => actor !== 'human'))]
  const deliverables = [...(task.deliverables ?? [])].reverse()
  const diagnoses = [...(task.diagnoses ?? [])].reverse()
  const finalHandoff = task.work?.delivery?.status === 'accepted' ? task.work.delivery.handoff : undefined
  const handoffs = [...(task.handoffs ?? []), ...(finalHandoff ? [{ seq: task.work?.delivery?.review_seq ?? 0,
    summary: finalHandoff.summary, why: finalHandoff.why }] : [])].reverse()
  return <div className="space-y-5 border-t border-[#eaded1] pt-5">{driverCoverage && <section><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">协作通道</h2><span className="text-xs text-stone-400">不展示密钥</span></div><div className="space-y-1.5">{driverCoverage.map((item) => <div key={item.role_id} className="flex items-center justify-between gap-2 text-xs"><span className="truncate text-stone-600">{item.name}</span><span className={item.mode === 'external' ? 'text-teal-700' : 'text-amber-700'}>{item.mode === 'external' ? '外部模型' : '本地演示'}</span></div>)}</div></section>}<section><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">本轮参与</h2><span className="text-xs text-stone-400">{participants.length} 位伙伴</span></div><div className="space-y-2">{participants.map((owner) => <div key={owner} className="flex items-center gap-2 border-l-2 border-[#cb6e4e] py-1 pl-3"><Portrait id={owner} size="sm" /><strong className="text-xs">{names[owner] ?? actorText(owner)}</strong><span className="text-xs text-stone-400">{assignments.length ? '已接下这一轮安排' : '已参与本轮协作'}</span></div>)}</div></section><section><div className="mb-2 flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">已发布成果</h2><span className="text-xs text-stone-400">{deliverables.length} 项</span></div>{deliverables.length ? <div className="space-y-2">{deliverables.map((item) => <div key={item.seq} className="border-l-2 border-emerald-600 pl-3 py-1"><ArtifactPreview item={item} publisher={names[item.actor ?? ''] ?? actorText(item.actor ?? '猫咖')} /></div>)}</div> : <p className="text-xs leading-5 text-stone-400">猫咪们尚未发布可复用的成果。</p>}</section><section><div className="mb-2 flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">自证与交接</h2><span className="text-xs text-stone-400">{diagnoses.length + handoffs.length} 条</span></div>{diagnoses.length || handoffs.length ? <div className="space-y-3">{diagnoses.map((item) => <div key={`diagnosis-${item.seq}`} className="border-l-2 border-amber-500 pl-3 py-1"><strong className="block text-xs text-stone-800">诊断 · {diagnosisStatusText(item.status)}</strong><p className="mt-1 text-xs leading-5 text-stone-600">{item.symptom}</p><p className="mt-1 text-xs leading-5 text-stone-500">{item.cause}</p></div>)}{handoffs.map((item) => <div key={`handoff-${item.seq}`} className="border-l-2 border-sky-600 pl-3 py-1"><strong className="block text-xs text-stone-800">交接卡</strong><p className="mt-1 text-xs leading-5 text-stone-600">{item.summary}</p><p className="mt-1 text-xs leading-5 text-stone-500">为什么：{item.why}</p></div>)}</div> : <p className="text-xs leading-5 text-stone-400">尚未留下诊断或交接卡；完成关键改动时，伙伴会说明证据、验证与未覆盖范围。</p>}</section><section><div className="mb-2 flex items-center gap-2"><CheckCircle2 size={16} className="text-teal-700" /><h2 className="text-sm font-semibold">当前结论</h2></div><p className="text-sm leading-6 text-stone-600">{reasonText(task.decision?.reason ?? task.brief?.dispute ?? '团队正在形成下一步可交付结果。')}</p>{task.decision && <span className="mt-2 inline-block rounded-full bg-teal-50 px-2 py-1 text-xs text-teal-800">{actorText(task.decision.source ?? '团队')} · {statusText(task.decision.resolution)}</span>}</section><section className="border-t border-[#eaded1] pt-4"><button onClick={onToggleEvidence} className="flex w-full items-center justify-between text-left text-sm font-semibold"><span className="flex items-center gap-2"><FileText size={16} className="text-stone-500" /> 过程证据</span><ChevronDown size={16} className={showEvidence ? 'rotate-180 transition' : 'transition'} /></button>{showEvidence && <div className="mt-3 max-h-52 overflow-auto rounded-lg bg-[#fcf5ee] p-3 text-xs text-stone-500"><p>{detail?.events.length ?? 0} 条不可变事件已记录。</p><a className="mt-2 inline-block text-teal-700 hover:underline" href={`/tasks/${task.id}`}>打开完整审计回放</a></div>}</section></div>
}

function EmptyConversation({ onChoose }: { onChoose: (value: string) => void }) {
  return <AsyncPanel fallback={<p role="status" className="p-5 text-sm text-[#52766d]">正在打开猫咖…</p>}><WelcomeView onChoose={onChoose} portrait={<Portrait id="store_manager" size="lg" />} /></AsyncPanel>
}

export default App
