import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDown, Cat, CheckCircle2, ChevronDown,
  CornerDownRight, FileText, LoaderCircle, PawPrint, Plus, RefreshCw, Search, ShieldCheck,
} from 'lucide-react'
import catPortraits from './assets/cat-portraits.jpg'
import staffPortraits from './assets/staff-portraits.jpg'
import cafeWelcome from './assets/cafe-welcome-v2.jpg'
import { ArtifactPreview } from './Artifact'
import type { Artifact } from './Artifact'
import { WorkboardPanel } from './WorkboardPanel'
import { MentionInput } from './MentionInput'
import { TeamDirectory } from './TeamDirectory'
import { TaskList } from './TaskList'
import { RulesPanel } from './RulesPanel'
import { WorkItems } from './WorkItems'
import type { WorkState } from './WorkItems'
import type { TeamAvailability } from './TeamDirectory'

type Assignment = { id: string; owner: string; role: string; lane: string; objective: string; acceptance?: string; deliverable?: string; status: string }
type Message = { seq: number; actor: string; role: string; type: string; claim?: string; text?: string; stance?: string; target?: string; reply_to?: number }
type Deliverable = Artifact
type Diagnosis = { seq: number; status: 'hypothesis' | 'reported' | 'confirmed' | 'uncertain'; symptom: string; cause: string; observations?: string[]; alternatives?: string[]; impact?: string[]; verification_plan?: string[]; message_seq?: number }
type Handoff = { seq: number; summary: string; why: string; changed?: string[]; verification?: string[]; limits?: string[]; teach_back?: string[]; message_seq?: number }
type Verification = { seq: number; command: string[]; outcome: 'passed' | 'failed' | 'timeout' | 'launch_error'; exit_code: number | null; for_event?: number; git?: { head?: string | null; dirty?: boolean | null }; stdout?: { bytes?: number; sha256?: string; excerpt?: string }; stderr?: { bytes?: number; sha256?: string; excerpt?: string } }
type ChainStatus = { ok: boolean; checked: number; reason?: string; broken_at?: number }
type DisciplineFlag = { seq: number; role?: string; message_seq?: number; issues?: string[]; action?: string }
type MemoryItem = { seq: number; task_id?: string; actor?: string; title: string; summary: string; kind?: string }
type OrgRole = { id: string; name: string; role: string; charter?: string }
type DriverCoverage = { role_id: string; name: string; driver: string; mode: 'local_demo' | 'external' }
type Organization = { leader: OrgRole; cabinet: OrgRole[]; workers: OrgRole[]; mode?: 'local_demo' | 'mixed_driver' | 'external_driver'; worker_drivers?: DriverCoverage[]; availability?: TeamAvailability }
type Activity = { id?: string; action?: string; state: 'idle' | 'queued' | 'running' | 'completed' | 'failed'; active: boolean; error?: string; follow_up?: { action?: string } }
type Task = {
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
type Event = { seq: number; type: string; category: string; actor: { id: string; role: string; kind: string }; payload: Record<string, unknown> }
type TaskDetail = { task: Task; events: Event[]; organization_memory?: MemoryItem[]; review: { health?: string; summary?: string }; activity?: Activity }
type ChatItem = { id: number; actor: string; display: string; role: string; kind: 'human' | 'agent' | 'system'; category: 'collaboration' | 'decision' | 'deliverable' | 'system'; title?: string; text: string; details?: string[]; targets: string[]; replyTo?: number; evidence: Event }

const accessToken = (() => {
  const fragment = new URLSearchParams(window.location.hash.slice(1))
  const supplied = fragment.get('access_token')
  if (supplied) {
    window.sessionStorage.setItem('cater.access_token', supplied)
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`)
    return supplied
  }
  return window.sessionStorage.getItem('cater.access_token') ?? ''
})()

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(accessToken ? { 'X-Cater-Access-Token': accessToken } : {}),
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) throw new Error(await response.text())
  return response.json() as Promise<T>
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

const actorText = (value: string) => ({ human: '你', orchestrator: '店长', leader: '店长', cabinet: '铲屎官们', supreme: '最高决策者', store_manager: '店长', reviewer: '复盘员', driver_gateway: '驱动网关', cabinet_product: '白露', cabinet_engineering: '阿程', cabinet_quality: '知夏', caretaker_product: '白露', caretaker_engineering: '阿程', caretaker_quality: '知夏', product: '月亮', design: '罐头', dev: '米寇', test: '三角' }[value] ?? value)

const activityText = (activity?: Activity) => {
  if (activity?.active && activity.follow_up?.action === 'run') return '已收下新方向，当前轮结束后继续'
  if (activity?.state === 'queued') return '协作已排队'
  if (activity?.state === 'running') return '正在协作'
  if (activity?.state === 'failed') return '协作失败'
  return ''
}

const taskListStatus = (task: Task) => {
  if (task.activity?.active) return activityText(task.activity) || '正在协作'
  if (task.work?.items.length) {
    if (task.work.items.some((item) => item.status === 'uncertain')) return '结果待核实'
    if (!task.work.scope_current) return '需要重新核对'
    if (task.work.stage_accepted) return '阶段通过，待整体验收'
    if (task.work.items.some((item) => item.status === 'submitted')) return '等待验收'
    return '待补充成果'
  }
  return task.status === 'running' && task.brief ? '等待下一轮' : statusText(task.status)
}

const driverModeText = (mode?: Organization['mode']) => ({ local_demo: '本地演示模式', mixed_driver: '部分成员已接入外部驱动', external_driver: '已接入外部驱动' }[mode ?? 'local_demo'] ?? '本地演示模式')
const driverModeStyle = (mode?: Organization['mode']) => ({ local_demo: 'bg-amber-50 text-amber-800', mixed_driver: 'bg-sky-50 text-sky-800', external_driver: 'bg-teal-50 text-teal-800' }[mode ?? 'local_demo'] ?? 'bg-amber-50 text-amber-800')

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
  const values = (issues ?? []).map((item) => labels[item] ?? item)
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

function conversationFor(events: Event[], identities: Record<string, OrgRole>, organization: Organization | null): ChatItem[] {
  const hasAutonomousDecision = events.some((event) => event.type === 'agent.decided' || event.type === 'supreme.decided')
  const hasFinishedActivity = events.some((event) => event.type === 'activity.completed' || event.type === 'activity.failed')
  return events.flatMap((event) => {
    const payload = event.payload
    const actor = event.actor
    const message = (title: string | undefined, text: string, kind: ChatItem['kind'], category: ChatItem['category'] = kind === 'agent' || kind === 'human' ? 'collaboration' : 'system', details?: string[]): ChatItem => ({
      id: event.seq, actor: actor.id, display: identities[actor.id]?.name ?? actorText(actor.id), role: publicIdentity(actor.id, organization), kind, category, title, text, evidence: event,
      details: details?.filter(Boolean),
      targets: [
        ...(typeof payload.target === 'string' ? [payload.target] : []),
        ...(Array.isArray(payload.mentions) ? payload.mentions.filter((item): item is string => typeof item === 'string') : []),
      ].map((id) => identities[id]?.name ?? actorText(id)),
      replyTo: typeof payload.reply_to === 'number' ? payload.reply_to : undefined,
    })
    const memberNames = (value: unknown) => Array.isArray(value)
      ? value.filter((id): id is string => typeof id === 'string').map((id) => identities[id]?.name ?? actorText(id))
      : []
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
    if (event.type === 'human.commanded') return [message(payload.intent === 'status' ? '查看进度' : payload.intent === 'resume' ? '继续推进' : '方向更新', String(payload.instruction ?? ''), 'human')]
    if (event.type === 'task.progress_reported') return [message('当前进展', String(payload.text ?? ''), 'system', 'collaboration')]
    if (event.type === 'planning.started') return [message('店长在整理安排', '我先核对这次的要求、已有成果和前后依赖，再把具体工作交给伙伴。', 'system', 'collaboration')]
    if (event.type === 'planning.finished') return [message(payload.status === 'installed' ? '这次这样安排' : '计划先不下发', String(payload.reason ?? ''), 'system', 'decision')]
    if (event.type === 'msg.said' && payload.phase === 'planning') return []
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
      const assignees = subtasks.map((item) => identities[String(item.owner ?? '')]?.name ?? actorText(String(item.owner ?? ''))).filter(Boolean)
      const details = subtasks.map((item) => {
        const owner = String(item.owner ?? '')
        const name = identities[owner]?.name ?? actorText(owner)
        const lane = String(item.lane ?? '执行')
        const acceptance = String(item.acceptance ?? '')
        return `@${name} 负责${lane}${acceptance ? `，完成时：${acceptance}` : ''}`
      })
      const isCabinetPlan = String(payload.source) === 'cabinet'
      const planner = isCabinetPlan ? names.join('、') || '几位伙伴' : '店长'
      const invitation = assignees.length === 1
        ? `这次先请 @${assignees[0]} 接住这一单。`
        : assignees.length > 1
          ? `这次请 ${assignees.map((name) => `@${name}`).join('、')} 一起接力。`
          : '这一单已经安排妥当，大家会依次接力推进。'
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
    if (event.type === 'subtask.assigned' || event.type === 'plan.proposed' || event.type === 'agent.working') return []
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
    if (event.type === 'deliverable.published') return [message('发布成果', `${String(payload.title ?? '未命名成果')}\n${String(payload.summary ?? '')}`, 'agent', 'deliverable')]
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
    if (event.type === 'verification.completed') {
      const command = Array.isArray(payload.command) ? payload.command.filter((item): item is string => typeof item === 'string') : []
      const outcome = String(payload.outcome ?? '')
      const label = ({ passed: '命令通过', failed: '命令失败', timeout: '执行超时', launch_error: '无法启动' } as Record<string, string>)[outcome] ?? '检查结束'
      const git = payload.git as Record<string, unknown> | undefined
      const details = [
        `退出码：${payload.exit_code ?? '无'}`,
        typeof payload.for_event === 'number' ? `关联自报事件 #${payload.for_event}；并不自动证实其结论` : '',
        git?.head ? `Git 提交：${String(git.head).slice(0, 12)}${git.dirty ? '（工作区有未提交改动，检查未绑定具体文件内容）' : ''}` : '未识别 Git 状态',
        '仅表示这条本地命令的观察结果，不代表根因或整项任务已证实。',
      ]
      return [message(`本机检查 · ${label}`, command.join(' '), 'system', 'decision', details)]
    }
    if (event.type === 'cabinet.voted') {
      const tally = payload.tally as Record<string, unknown> | undefined
      return [message('伙伴们给出建议', reasonText(String(tally?.reason ?? '几位伙伴已经看过当前选项。')), 'system', 'decision')]
    }
    if (event.type === 'agent.decided' || event.type === 'supreme.decided') return [message('自治决策', reasonText(String(payload.reason ?? '')), 'system', 'decision')]
    if (event.type === 'brief.generated' && !hasAutonomousDecision) {
      const brief = payload.brief as Record<string, unknown> | undefined
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
  const [newProject, setNewProject] = useState<{ id: string; name: string } | null>(null)
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [roles, setRoles] = useState<OrgRole[]>([])
  const [organization, setOrganization] = useState<Organization | null>(null)
  const [pending, setPending] = useState<Set<string>>(new Set())
  const [showEvidence, setShowEvidence] = useState(false)
  const [showMobileBoard, setShowMobileBoard] = useState(false)
  const [tasksCollapsed, setTasksCollapsed] = useState(false)
  const [taskSearchOpen, setTaskSearchOpen] = useState(false)
  const [membersCollapsed, setMembersCollapsed] = useState(false)
  const [teamOpen, setTeamOpen] = useState(false)
  const [teamRefreshing, setTeamRefreshing] = useState(false)
  const [teamError, setTeamError] = useState('')
  const profileBusy = useRef(false)
  const profileAbort = useRef<AbortController | null>(null)
  const profileMounted = useRef(true)
  const [chainStatus, setChainStatus] = useState<ChainStatus | null>(null)
  const [checkingChain, setCheckingChain] = useState(false)
  const [chatFilter, setChatFilter] = useState<'all' | ChatItem['category']>('all')
  const [error, setError] = useState('')
  const [sendErrors, setSendErrors] = useState<Record<string, string>>({})
  const [loadErrors, setLoadErrors] = useState<Record<string, string>>({})
  const [reloadToken, setReloadToken] = useState(0)
  const [unread, setUnread] = useState(0)
  const streamRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const seenRef = useRef({ view: '', latest: 0, count: 0 })
  const selectedRef = useRef<string | null>(null)
  const navigationRef = useRef(0)
  const pendingRef = useRef(new Set<string>())
  const versionsRef = useRef<Record<string, number>>({})
  const newKey = '$new'
  const composerKey = selectedId ?? newKey
  const draft = drafts[newKey] ?? ''
  const message = selectedId ? drafts[selectedId] ?? '' : ''
  const isSending = pending.has(composerKey)

  const updateDraft = (key: string, value: string | ((current: string) => string)) => {
    setDrafts((current) => ({ ...current, [key]: typeof value === 'function' ? value(current[key] ?? '') : value }))
  }
  const setDraft = (value: string | ((current: string) => string)) => updateDraft(newKey, value)
  const setMessage = (value: string) => { if (selectedId) updateDraft(selectedId, value) }

  const chooseTask = useCallback((id: string | null) => {
    selectedRef.current = id
    navigationRef.current += 1
    followRef.current = true
    seenRef.current = { view: '', latest: 0, count: 0 }
    setUnread(0)
    setSelectedId(id)
    setChatFilter('all')
    setShowMobileBoard(false)
  }, [])

  const acceptDetail = useCallback((next: TaskDetail) => {
    const id = next.task.id
    const seq = next.events.at(-1)?.seq ?? 0
    if (seq < (versionsRef.current[id] ?? 0)) return
    versionsRef.current[id] = seq
    setTasks((current) => {
      const item = { ...next.task, activity: next.activity }
      return current.some((task) => task.id === id) ? current.map((task) => task.id === id ? item : task) : [...current, item]
    })
    if (selectedRef.current === id) setDetail(next)
  }, [])

  useEffect(() => {
    let cancelled = false
    void api<Task[]>('/api/tasks').then((nextTasks) => {
      if (cancelled) return
      setTasks((current) => [...nextTasks.filter((task) => !current.some((item) => item.id === task.id)), ...current])
      if (navigationRef.current === 0) chooseTask(nextTasks.at(-1)?.id ?? null)
    }).catch((err: unknown) => {
      if (!cancelled) setError(userFacingError(err, '暂时无法读取任务。'))
    })
    return () => { cancelled = true }
  }, [chooseTask])
  useEffect(() => {
    void api<OrgRole[]>('/api/organization').then((nextRoles) => {
      setRoles(nextRoles)
    }).catch((err: unknown) => setError(userFacingError(err, '暂时无法读取团队信息。')))
  }, [])
  const refreshOrganization = useCallback(async () => {
    if (profileBusy.current) return
    profileBusy.current = true
    const controller = new AbortController()
    profileAbort.current = controller
    const timeout = window.setTimeout(() => controller.abort(), 8000)
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
      window.clearTimeout(timeout)
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
      let delay = 4000
      try {
        const next = await api<TaskDetail>(`/api/tasks/${selectedId}`, { signal: controller.signal })
        if (cancelled) return
        acceptDetail(next)
        setLoadErrors((current) => ({ ...current, [selectedId]: '' }))
        delay = next.activity?.active ? 800 : 4000
      } catch (err) {
        if (!cancelled) setLoadErrors((current) => ({ ...current, [selectedId]: userFacingError(err, '暂时无法读取任务详情。') }))
      } finally {
        fetching = false
        if (!cancelled && pageVisible()) timer = window.setTimeout(() => void poll(), delay)
      }
    }
    const visibilityChanged = () => {
      window.clearTimeout(timer)
      if (pageVisible()) void poll()
    }
    void poll()
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      controller?.abort()
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [selectedId, reloadToken, acceptDetail])

  const identities = useMemo(() => {
    const members = organization ? [organization.leader, ...organization.cabinet, ...organization.workers] : roles
    return Object.fromEntries(members.map((member) => [member.id, member]))
  }, [organization, roles])
  const currentDetail = detail?.task.id === selectedId ? detail : null
  const conversation = useMemo(() => currentDetail ? conversationFor(currentDetail.events, identities, organization) : [], [currentDetail, identities, organization])
  const visibleConversation = useMemo(() => chatFilter === 'all' ? conversation : conversation.filter((item) => item.category === chatFilter), [chatFilter, conversation])
  const task = selectedId ? currentDetail?.task ?? tasks.find((item) => item.id === selectedId) : undefined
  const loading = selectedId !== null && currentDetail === null
  const loadError = selectedId ? loadErrors[selectedId] ?? '' : ''
  const visibleError = sendErrors[composerKey] || error

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

  const createTask = async () => {
    const submitted = draft
    const goal = submitted.trim()
    if (!goal || selectedRef.current !== null || pendingRef.current.has(newKey)) return
    const navigation = navigationRef.current
    pendingRef.current.add(newKey); setPending(new Set(pendingRef.current))
    setSendErrors((current) => ({ ...current, [newKey]: '' }))
    try {
      const result = await api<TaskDetail>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          goal,
          project_id: newProject?.id,
          run: true, background: true,
        }),
      })
      setDrafts((current) => current[newKey] === submitted ? { ...current, [newKey]: '' } : current)
      if (navigationRef.current === navigation && selectedRef.current === null) chooseTask(result.task.id)
      acceptDetail(result)
    } catch (err) { setSendErrors((current) => ({ ...current, [newKey]: userFacingError(err, '创建任务失败') })) }
    finally { pendingRef.current.delete(newKey); setPending(new Set(pendingRef.current)) }
  }

  const sendDirection = async () => {
    const id = selectedId
    const submitted = message
    if (!id || selectedRef.current !== id || loading || !submitted.trim() || pendingRef.current.has(id)) return
    pendingRef.current.add(id); setPending(new Set(pendingRef.current))
    setSendErrors((current) => ({ ...current, [id]: '' }))
    try {
      const result = await api<TaskDetail>(`/api/tasks/${id}/command`, { method: 'POST', body: JSON.stringify({ instruction: submitted.trim(), continue: true }) })
      setDrafts((current) => current[id] === submitted ? { ...current, [id]: '' } : current)
      acceptDetail(result)
    } catch (err) { setSendErrors((current) => ({ ...current, [id]: userFacingError(err, '发送指令失败') })) }
    finally { pendingRef.current.delete(id); setPending(new Set(pendingRef.current)) }
  }

  const team = organization ? [...organization.workers, organization.leader, ...organization.cabinet] : roles
  const names = Object.fromEntries(Object.entries(identities).map(([id, member]) => [id, member.name]))
  const rulesControl = <RulesPanel key={`rules:${selectedId ?? '$new'}`} request={api} taskId={selectedId ?? undefined} projectId={task ? task.project_id : newProject?.id} draft={task ? message : draft} busy={Boolean(loading || isSending || currentDetail?.activity?.active)} onProjectChange={async (project) => {
    if (selectedId) {
      if (!project) return
      const result = await api<TaskDetail>(`/api/tasks/${selectedId}/project`, { method: 'POST', body: JSON.stringify({ project_id: project.id }) })
      acceptDetail(result)
    } else setNewProject(project)
  }} onRecorded={(text) => { const key = selectedId ?? newKey; setDrafts((current) => current[key]?.trim() === text ? { ...current, [key]: '' } : current) }} onUpdated={() => setReloadToken((value) => value + 1)} />
  return (
    <div className="cater-app text-[#253d38]">
      <header className="app-header flex h-[72px] items-center justify-between px-4 sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <div className="brand-mark grid size-11 shrink-0 place-items-center rounded-lg text-white"><Cat size={23} strokeWidth={2.2} /></div>
          <div className="min-w-0"><strong className="block text-[19px] font-extrabold leading-5 text-[#713f34]">Cater</strong><span className="mt-1 block text-[11px] font-semibold text-[#a9604f]">有猫的工作间</span></div>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <TeamDirectory members={team} availability={organization?.availability} open={teamOpen} onOpenChange={setTeamOpen} refreshing={teamRefreshing} error={teamError} onRefresh={() => void refreshOrganization()} portrait={(id) => <Portrait id={id} size="xl" />} onMention={(name) => { if (task) setMessage(`${message.trim()}${message.trim() ? ' ' : ''}@${name} `); else setDraft((current) => `${current.trim()}${current.trim() ? ' ' : ''}@${name} `) }} />
          <span className={`max-w-32 truncate rounded-md px-2.5 py-1.5 text-[11px] font-medium sm:max-w-none ${driverModeStyle(organization?.mode)}`} title="具体成员状态可在工作板查看。">{driverModeText(organization?.mode)}</span>
          <button type="button" onClick={() => void verifyChain()} disabled={checkingChain} className={`inline-flex size-8 items-center justify-center rounded-md hover:bg-[#e7f0ec] sm:w-auto sm:gap-1 sm:px-2 ${chainStatus && !chainStatus.ok ? 'text-rose-700' : 'text-[#34786e]'}`} aria-label={checkingChain ? '正在核验事件链' : chainStatus ? chainStatus.ok ? `上次核验通过，共 ${chainStatus.checked} 条事件` : `事件链异常，事件 #${chainStatus.broken_at ?? '?'}` : '核验事件链'} title={chainStatus?.reason || '点击核验事件链；显示的是上次核验结果'}><ShieldCheck size={17} /><span className="hidden sm:inline">{checkingChain ? '正在核验' : chainStatus ? chainStatus.ok ? `已核验 ${chainStatus.checked} 条` : `链异常 · #${chainStatus.broken_at ?? '?'}` : '核验事件链'}</span></button>
        </div>
      </header>
      <main className={`app-layout grid grid-cols-1 ${task ? 'lg:grid-cols-[252px_minmax(0,1fr)_318px]' : 'lg:grid-cols-[252px_minmax(0,1fr)]'}`}>
        <aside className="task-sidebar min-h-0 lg:flex lg:flex-col lg:overflow-y-auto">
          <div className="flex items-center justify-between px-4 pb-2 pt-4">
            <div><span className="text-[11px] font-semibold text-[#a56b56]">正在聊</span><h2 className="flex items-center gap-1.5 text-base font-bold"><PawPrint size={17} className="text-[#c36550]" /> 我的任务 <span className="text-xs font-medium text-[#a98e81]">{tasks.length}</span></h2></div>
            <div className="flex gap-1">
              <button type="button" onClick={() => { setTaskSearchOpen(!(taskSearchOpen && !tasksCollapsed)); setTasksCollapsed(false) }} aria-label={taskSearchOpen && !tasksCollapsed ? '收起任务查找' : '查找任务'} title={taskSearchOpen && !tasksCollapsed ? '收起任务查找' : '查找任务'} aria-controls="task-search" aria-expanded={taskSearchOpen && !tasksCollapsed} className="grid size-9 place-items-center rounded-md text-[#a9604f] hover:bg-[#fff5eb] lg:hidden"><Search size={17} /></button>
              <button type="button" onClick={() => setTasksCollapsed(!tasksCollapsed)} aria-controls="task-list" aria-expanded={!tasksCollapsed} aria-label={tasksCollapsed ? '展开任务列表' : '收起任务列表'} title={tasksCollapsed ? '展开任务列表' : '收起任务列表'} className="grid size-9 place-items-center rounded-md text-[#a9604f] hover:bg-[#fff5eb] lg:hidden"><ChevronDown size={18} className={tasksCollapsed ? '' : 'rotate-180'} /></button>
              <button className="grid size-9 place-items-center rounded-md border border-[#ead2c5] bg-white text-[#b15342] shadow-sm hover:bg-[#fff0e9]" title="新建任务" aria-label="新建任务" onClick={() => chooseTask(null)}><Plus size={18} /></button>
            </div>
          </div>
          <TaskList tasks={tasks} selectedId={selectedId} drafts={drafts} collapsed={tasksCollapsed} searchOpen={taskSearchOpen} onChoose={chooseTask} statusLabel={taskListStatus} />
          <div className="hidden lg:block"><TeamRoster members={team} compact onOpen={() => { setTeamOpen(true); void refreshOrganization() }} /></div>
        </aside>
        <section className="chat-pane flex min-h-0 flex-col">
          {task ? <>
            <div className="chat-heading flex items-start justify-between gap-3 px-4 py-3 sm:px-7 sm:py-4" style={{ backgroundImage: `linear-gradient(90deg, #fffaf5 0%, rgba(255,250,245,.98) 55%, rgba(255,250,245,.78) 100%), url(${cafeWelcome})` }}>
              <div className="flex min-w-0 items-start gap-3"><Portrait id={organization?.leader.id ?? 'store_manager'} /><div className="min-w-0"><p className="mb-1 text-[11px] font-semibold text-[#a55642]">店长 · 这件事我在跟进</p><h1 className="line-clamp-2 text-[16px] font-bold leading-snug sm:text-lg">{task.goal}</h1></div></div>
              <div className="flex shrink-0 items-center gap-2"><span className={`status-pill hidden sm:inline-flex ${currentDetail?.activity?.active ? 'is-running' : ''}`}>{loading ? '接入对话中' : activityText(currentDetail?.activity) || taskListStatus(task)}</span><button id="workboard-trigger" onClick={() => setShowMobileBoard(true)} aria-label="打开成果与过程" title="打开成果与过程" aria-haspopup="dialog" aria-expanded={showMobileBoard} className="rounded-md border border-[#d3dfd9] bg-white px-2.5 py-1.5 text-xs font-semibold text-[#24766b] lg:hidden">成果</button></div>
            </div>
            <div className="member-bar flex shrink-0 items-center gap-2 px-4 sm:px-7">
              <button type="button" onClick={() => setMembersCollapsed(!membersCollapsed)} aria-controls="member-seats" aria-expanded={!membersCollapsed} aria-label={membersCollapsed ? '展开伙伴栏' : '收起伙伴栏'} title={membersCollapsed ? '展开伙伴栏' : '收起伙伴栏'} className={`flex shrink-0 items-center gap-1 text-[#5f8172] lg:hidden ${membersCollapsed ? 'h-8' : 'h-12'}`}><Cat size={17} />{membersCollapsed && <span className="text-[11px]">{team.length} 位伙伴</span>}<ChevronDown size={13} className={membersCollapsed ? '' : 'rotate-180'} /></button>
              <div id="member-seats" className={`member-seats flex min-w-0 flex-1 gap-2 overflow-x-auto py-2 ${membersCollapsed ? 'mobile-collapsed' : ''}`} role="group" aria-label="点名伙伴">{team.map((member) => <button key={member.id} type="button" className="member-seat flex h-10 shrink-0 items-center gap-2 px-2" aria-label={`点名 ${member.name}`} title={`在当前对话中点名 ${member.name}`} onClick={() => { setMessage(`${message.trim()}${message.trim() ? ' ' : ''}@${member.name} `); document.querySelector<HTMLTextAreaElement>('textarea[aria-label="向团队发送消息"]')?.focus() }}><Portrait id={member.id} size="sm" /><span className="max-w-24 truncate text-xs font-semibold">{member.name}</span></button>)}</div>
            </div>
            <div className="filter-strip px-4 py-2 sm:px-7"><div className="flex gap-1 overflow-x-auto" role="group" aria-label="对话筛选">{([{ id: 'all', label: '全部' }, { id: 'collaboration', label: '协作' }, { id: 'decision', label: '决策' }, { id: 'deliverable', label: '成果' }] as const).map((filter) => <button key={filter.id} onClick={() => setChatFilter(filter.id)} aria-pressed={chatFilter === filter.id} className={`filter-button shrink-0 px-3 py-1.5 text-xs font-semibold ${chatFilter === filter.id ? 'is-active' : ''}`}>{filter.label}</button>)}</div></div>
            <div className="relative flex min-h-0 flex-1 flex-col">
              <div ref={streamRef} className="chat-stream min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-7" aria-label="聊天记录" aria-busy={loading} onScroll={(event) => { const stream = event.currentTarget; followRef.current = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 60; if (followRef.current) setUnread(0) }}>{loading ? <ChatWaiting error={loadError} onRetry={() => setReloadToken((current) => current + 1)} /> : visibleConversation.length ? visibleConversation.map((item) => <ChatBubble key={item.id} item={item} />) : <p className="py-10 text-center text-sm text-[#8ba09a]">这一栏暂时还没有新消息。</p>}</div>
              {!loading && unread > 0 && <button onClick={jumpToLatest} className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-2 whitespace-nowrap rounded-md border border-[#d9b9a5] bg-[#fff7eb] px-3 py-2 text-xs font-semibold text-[#915340] shadow-md" title="返回最新消息"><ArrowDown size={15} /> 查看新消息 · {unread}</button>}
            </div>
            {rulesControl}
            <DirectionComposer key={task.id} members={team} message={message} isSending={isSending} isLoading={loading} onMessageChange={setMessage} onSend={() => void sendDirection()} />
          </> : <><EmptyConversation onChoose={setDraft} members={team} />{rulesControl}<TaskComposer members={team} draft={draft} isSending={isSending} onDraftChange={setDraft} onCreate={() => void createTask()} /></>}
          {(visibleError || (!loading && loadError)) && <p role="alert" className="mx-4 mb-3 rounded-md border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700 sm:mx-7">{visibleError || loadError}</p>}
        </section>
        {task && <WorkboardPanel open={showMobileBoard} onOpenChange={setShowMobileBoard}>
          {currentDetail ? <><WorkItems work={task.work} names={names} /><VerificationBoard items={task.verifications ?? []} /><Workboard task={task} detail={currentDetail} names={names} driverCoverage={organization?.worker_drivers} showEvidence={showEvidence} onToggleEvidence={() => setShowEvidence(!showEvidence)} />{Boolean(currentDetail.organization_memory?.length) && <details className="mt-5 border-t border-[#dce8e1] pt-4"><summary className="cursor-pointer text-xs font-semibold text-[#52766d]">猫咖记忆 · {currentDetail.organization_memory?.length} 项</summary><div className="mt-3"><MemoryBoard items={currentDetail.organization_memory ?? []} names={names} /></div></details>}</> : <p className="text-xs leading-6 text-[#74897d]">正在读取这件事的成果与过程。</p>}
        </WorkboardPanel>}
      </main>
    </div>
  )
}

function ChatWaiting({ error, onRetry }: { error: string; onRetry: () => void }) {
  return <div className="flex items-start gap-3 py-5"><Portrait id="store_manager" /><div className="min-w-0 text-sm leading-6 text-[#6e8175]"><p role={error ? 'alert' : 'status'} className="flex items-center gap-2">{!error && <LoaderCircle size={16} className="shrink-0 motion-safe:animate-spin" />}{error || '正在接上这段对话'}</p>{error && <button onClick={onRetry} className="mt-3 inline-flex items-center gap-2 rounded-md border border-[#dec8b8] bg-white px-3 py-1.5 text-xs text-[#9e5946]"><RefreshCw size={14} /> 重新连接</button>}</div></div>
}

function DirectionComposer({ members, message, isSending, isLoading, onMessageChange, onSend }: { members: OrgRole[]; message: string; isSending: boolean; isLoading: boolean; onMessageChange: (value: string) => void; onSend: () => void }) {
  return <div className="composer-shell p-3 sm:p-4"><div className="composer-box mx-auto flex max-w-3xl gap-2 p-2"><MentionInput value={message} label="向团队发送消息" placeholder="继续说吧，补充想法、改目标，或直接 @一位伙伴" members={members} onChange={onMessageChange} onSubmit={onSend} portrait={(id) => <Portrait id={id} size="sm" />} /><button disabled={isSending || isLoading || !message.trim()} onClick={onSend} className="send-button grid size-10 shrink-0 place-items-center self-end text-white disabled:bg-[#d4ddd8]" title={isLoading ? '正在接上对话' : isSending ? '正在发送消息' : '发送消息并安排下一轮协作'} aria-label="发送消息" aria-busy={isSending}>{isSending ? <LoaderCircle size={20} className="motion-safe:animate-spin" /> : <PawPrint size={20} fill="currentColor" strokeWidth={1.8} />}</button></div></div>
}

function TaskComposer({ members, draft, isSending, onDraftChange, onCreate }: { members: OrgRole[]; draft: string; isSending: boolean; onDraftChange: (value: string) => void; onCreate: () => void }) {
  return <div className="composer-shell p-3 sm:p-4"><div className="composer-box mx-auto flex max-w-3xl gap-2 p-2"><MentionInput id="new-task" value={draft} label="发起任务对话" placeholder="和店长说说，今天想做什么？" members={members} onChange={onDraftChange} onSubmit={onCreate} portrait={(id) => <Portrait id={id} size="sm" />} /><button disabled={isSending || !draft.trim()} onClick={onCreate} className="send-button grid size-10 shrink-0 place-items-center self-end text-white disabled:bg-[#d4ddd8]" title={isSending ? '正在发送新任务' : '发送新任务'} aria-label="发送新任务" aria-busy={isSending}>{isSending ? <LoaderCircle size={20} className="motion-safe:animate-spin" /> : <PawPrint size={20} fill="currentColor" strokeWidth={1.8} />}</button></div></div>
}

function ChatBubble({ item }: { item: ChatItem }) {
  const system = item.kind === 'system'; const human = item.kind === 'human'
  return <article className={`flex items-start gap-3 ${human ? 'justify-end' : ''}`}>
    {!human && <Portrait id={item.actor} />}
    <div className={`message-bubble max-w-[min(86%,700px)] px-4 py-3 ${human ? 'is-human text-white' : system ? 'is-system' : 'is-agent'}`} data-actor={item.actor}>
      <div className={`mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs ${human ? 'text-[#fff0e8]' : 'text-[#6e8981]'}`}><strong className={human ? 'text-white' : 'text-[#28433d]'}>{human ? '你' : item.display}</strong><span>{item.role}</span>{item.title && <span className={`text-[11px] font-semibold ${human ? 'text-[#fff4d9]' : 'text-[#a56049]'}`}>· {item.title}</span>}</div>
      <p className="whitespace-pre-wrap break-words text-[13px] leading-[1.75] sm:text-sm">{item.text}</p>
      {item.evidence.type === 'deliverable.published' && <ArtifactPreview item={{ ...item.evidence.payload, seq: item.id } as Artifact} publisher={item.display} showSummary={false} />}
      {(item.targets.length > 0 || item.replyTo) && <div className={`mt-2 flex flex-wrap gap-2 text-xs ${human ? 'text-[#fde9dd]' : 'text-teal-700'}`}>{item.targets.map((target) => <span key={target}>@{target}</span>)}{item.replyTo && <span className="inline-flex items-center gap-1"><CornerDownRight size={13} /> 回复 #{item.replyTo}</span>}</div>}
      {item.details?.length ? <details className={`mt-3 border-t pt-2 text-xs ${human ? 'border-[#e99c80] text-[#fde9dd]' : 'border-[#dfeae5] text-[#52766d]'}`}><summary className="cursor-pointer font-semibold">查看判断与依据</summary><ul className="mt-2 space-y-1.5 leading-5">{item.details.map((detail, index) => <li key={`${item.id}-${index}`}>{detail}</li>)}</ul></details> : null}
      <details className={`mt-2 text-[11px] ${human ? 'text-[#fde9dd]' : 'text-[#94a9a2]'}`}><summary className="cursor-pointer">事件 #{item.id} · 审计记录</summary><pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-[#233833] p-2 text-[10px] text-stone-100">{JSON.stringify(item.evidence.payload, null, 2)}</pre></details>
    </div>
  </article>
}

function TeamRoster({ members, compact = false, onMention, onOpen }: { members: OrgRole[]; compact?: boolean; onMention?: (name: string) => void; onOpen?: () => void }) {
  const identity = (id: string) => id === 'store_manager' || id === 'orchestrator' || id === 'leader' ? '店长' : id.startsWith('caretaker_') || id.startsWith('cabinet_') ? '铲屎官' : '猫咪'
  const cats = members.filter((member) => identity(member.id) === '猫咪')
  const staff = members.filter((member) => identity(member.id) !== '猫咪')
  return <section className={compact ? 'team-roster-compact p-4' : 'team-roster-full'}>
    <button type="button" onClick={onOpen} className="mb-3 flex w-full items-center gap-2 text-[#a85643]" aria-label="打开伙伴页"><Cat size={18} /><span className="text-[13px] font-bold">今天在店里</span><span className="ml-auto text-[11px] text-[#a99080]">{members.length} 位伙伴</span></button>
    {compact ? <><div className="flex flex-wrap gap-1.5">{[...cats, ...staff].map((member) => <span key={member.id} title={`${member.name} · ${identity(member.id)}`}><Portrait id={member.id} size="sm" /></span>)}</div><p className="mt-2 text-[11px] leading-5 text-[#92796b]">{cats.map((member) => member.name).join(' · ') || '猫咪们正在准备'}</p></> : <><div className="grid grid-cols-1 gap-2">{cats.map((member) => <button key={member.id} type="button" onClick={() => onMention?.(member.name)} className="cat-member flex items-center gap-3 px-3 py-2 text-left" title={`在新任务中点名 ${member.name}`} aria-label={`点名 ${member.name}`}><Portrait id={member.id} size="lg" /><div className="min-w-0"><strong className="block text-[13px]">{member.name}</strong><span className="text-[11px] text-[#9d7869]">猫咪 · 点名聊聊</span></div></button>)}</div>{staff.length > 0 && <div className="mt-5 border-t border-[#e3e2d7] pt-3"><h3 className="mb-1 text-[11px] font-semibold text-[#93796b]">店里还有</h3>{staff.map((member) => <div key={member.id} className="flex items-center gap-3 border-b border-[#e6e9dd] py-2 last:border-0"><Portrait id={member.id} size="sm" /><strong className="text-[12px]">{member.name}</strong><span className="ml-auto text-[11px] text-[#9d8e7f]">{identity(member.id)}</span></div>)}</div>}</>}
  </section>
}

function MemoryBoard({ items, names }: { items: MemoryItem[]; names: Record<string, string> }) {
  if (!items.length) return null
  return <section className="mb-5 border-b border-[#eaded1] pb-5"><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">猫咖记忆</h2><span className="text-xs text-stone-400">已发布成果</span></div><div className="space-y-2">{items.map((item) => <div key={item.seq} className="border-l-2 border-sky-600 pl-3 py-1"><strong className="block text-xs text-stone-800">{item.title}</strong><p className="mt-1 text-xs leading-5 text-stone-600">{item.summary}</p><span className="mt-1 block text-[11px] text-stone-400">{names[item.actor ?? ''] ?? actorText(item.actor ?? '猫咖')} · 事件 #{item.seq}</span></div>)}</div></section>
}

function VerificationBoard({ items }: { items: Verification[] }) {
  if (!items.length) return null
  return <section className="mb-5 border-b border-[#eaded1] pb-5"><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">本机检查</h2><span className="text-xs text-stone-400">显式运行 · {items.length} 次</span></div><div className="space-y-3">{[...items].reverse().slice(0, 3).map((item) => <div key={item.seq} className={`border-l-2 pl-3 py-1 ${item.outcome === 'passed' ? 'border-emerald-600' : 'border-rose-600'}`}><strong className="text-xs text-stone-800">{item.outcome === 'passed' ? '命令通过' : item.outcome === 'timeout' ? '执行超时' : item.outcome === 'launch_error' ? '无法启动' : '命令失败'}</strong><p className="mt-1 break-all font-mono text-[11px] leading-5 text-stone-600">{item.command.join(' ')}</p><p className="mt-1 text-[11px] text-stone-500">事件 #{item.seq}{item.for_event ? ` · 关联自报 #${item.for_event}` : ''} · 退出码 {item.exit_code ?? '无'}</p>{item.git?.dirty && <p className="mt-1 text-[11px] text-amber-700">工作区有未提交改动，未绑定具体文件内容</p>}</div>)}</div><p className="mt-2 text-[11px] leading-5 text-stone-500">单条命令通过，不代表诊断或整项任务已证实。</p></section>
}

function Workboard({ task, detail, names, driverCoverage, showEvidence, onToggleEvidence }: { task: Task; detail: TaskDetail | null; names: Record<string, string>; driverCoverage?: DriverCoverage[]; showEvidence: boolean; onToggleEvidence: () => void }) {
  const assignments = [...task.assignments].reverse().filter((item, index, all) => all.findIndex((other) => other.owner === item.owner) === index).filter((item) => item.status === undefined || item.status === 'assigned')
  const participants = assignments.length
    ? assignments.map((item) => item.owner)
    : [...new Set(task.messages.map((item) => item.actor).filter((actor) => actor !== 'human'))]
  const deliverables = [...(task.deliverables ?? [])].reverse()
  const diagnoses = [...(task.diagnoses ?? [])].reverse()
  const handoffs = [...(task.handoffs ?? [])].reverse()
  return <div className="space-y-5 border-t border-[#eaded1] pt-5">{driverCoverage && <section><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">协作通道</h2><span className="text-xs text-stone-400">不展示密钥</span></div><div className="space-y-1.5">{driverCoverage.map((item) => <div key={item.role_id} className="flex items-center justify-between gap-2 text-xs"><span className="truncate text-stone-600">{item.name}</span><span className={item.mode === 'external' ? 'text-teal-700' : 'text-amber-700'}>{item.mode === 'external' ? '外部模型' : '本地演示'}</span></div>)}</div></section>}<section><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">本轮参与</h2><span className="text-xs text-stone-400">{participants.length} 位伙伴</span></div><div className="space-y-2">{participants.map((owner) => <div key={owner} className="flex items-center gap-2 border-l-2 border-[#cb6e4e] py-1 pl-3"><Portrait id={owner} size="sm" /><strong className="text-xs">{names[owner] ?? actorText(owner)}</strong><span className="text-xs text-stone-400">{assignments.length ? '已接下这一轮安排' : '已参与本轮协作'}</span></div>)}</div></section><section><div className="mb-2 flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">已发布成果</h2><span className="text-xs text-stone-400">{deliverables.length} 项</span></div>{deliverables.length ? <div className="space-y-2">{deliverables.map((item) => <div key={item.seq} className="border-l-2 border-emerald-600 pl-3 py-1"><ArtifactPreview item={item} publisher={names[item.actor ?? ''] ?? actorText(item.actor ?? '猫咖')} /></div>)}</div> : <p className="text-xs leading-5 text-stone-400">猫咪们尚未发布可复用的成果。</p>}</section><section><div className="mb-2 flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">自证与交接</h2><span className="text-xs text-stone-400">{diagnoses.length + handoffs.length} 条</span></div>{diagnoses.length || handoffs.length ? <div className="space-y-3">{diagnoses.map((item) => <div key={`diagnosis-${item.seq}`} className="border-l-2 border-amber-500 pl-3 py-1"><strong className="block text-xs text-stone-800">诊断 · {diagnosisStatusText(item.status)}</strong><p className="mt-1 text-xs leading-5 text-stone-600">{item.symptom}</p><p className="mt-1 text-xs leading-5 text-stone-500">{item.cause}</p></div>)}{handoffs.map((item) => <div key={`handoff-${item.seq}`} className="border-l-2 border-sky-600 pl-3 py-1"><strong className="block text-xs text-stone-800">交接卡</strong><p className="mt-1 text-xs leading-5 text-stone-600">{item.summary}</p><p className="mt-1 text-xs leading-5 text-stone-500">为什么：{item.why}</p></div>)}</div> : <p className="text-xs leading-5 text-stone-400">尚未留下诊断或交接卡；完成关键改动时，伙伴会说明证据、验证与未覆盖范围。</p>}</section><section><div className="mb-2 flex items-center gap-2"><CheckCircle2 size={16} className="text-teal-700" /><h2 className="text-sm font-semibold">当前结论</h2></div><p className="text-sm leading-6 text-stone-600">{reasonText(task.decision?.reason ?? task.brief?.dispute ?? '团队正在形成下一步可交付结果。')}</p>{task.decision && <span className="mt-2 inline-block rounded-full bg-teal-50 px-2 py-1 text-xs text-teal-800">{actorText(task.decision.source ?? '团队')} · {statusText(task.decision.resolution)}</span>}</section><section className="border-t border-[#eaded1] pt-4"><button onClick={onToggleEvidence} className="flex w-full items-center justify-between text-left text-sm font-semibold"><span className="flex items-center gap-2"><FileText size={16} className="text-stone-500" /> 过程证据</span><ChevronDown size={16} className={showEvidence ? 'rotate-180 transition' : 'transition'} /></button>{showEvidence && <div className="mt-3 max-h-52 overflow-auto rounded-lg bg-[#fcf5ee] p-3 text-xs text-stone-500"><p>{detail?.events.length ?? 0} 条不可变事件已记录。</p><a className="mt-2 inline-block text-teal-700 hover:underline" href={`/tasks/${task.id}`}>打开完整审计回放</a></div>}</section></div>
}

function EmptyConversation({ onChoose, members }: { onChoose: (value: string) => void; members: OrgRole[] }) {
  const examples = ['帮我梳理一页活动说明', '帮我复盘一个难定位的 Bug', '聊聊一个新产品想法']
  return <div className="welcome-view flex-1 overflow-y-auto">
    <div className="welcome-scene"><img src={cafeWelcome} alt="月亮、三角、米寇和罐头在温暖的猫咖里" /></div>
    <div className="welcome-content mx-auto max-w-2xl px-5 pb-6 pt-5 sm:px-8">
      <div className="flex items-start gap-3"><Portrait id="store_manager" size="lg" /><div className="min-w-0"><span className="text-[11px] font-bold text-[#2a8075]">店长</span><h1 className="mt-1 text-xl font-bold leading-snug text-[#23433b] sm:text-2xl">欢迎来猫咖，今天想聊什么？</h1><p className="mt-2 text-[13px] leading-6 text-[#5e796e]">先说说你心里那件事。月亮、罐头、米寇和三角都在。</p></div></div>
      <div className="mt-5 flex gap-2 overflow-x-auto pb-2 sm:flex-wrap" aria-label="任务示例">{examples.map((example) => <button key={example} onClick={() => { onChoose(example); document.getElementById('new-task')?.focus() }} className="example-prompt shrink-0 border border-[#c9ddd5] bg-white px-3 py-2 text-left text-xs font-medium text-[#356d61] shadow-sm">{example}</button>)}</div>
      {members.length > 0 && <div className="mt-6 flex items-center gap-2 border-t border-[#e5d7c8] pt-4"><div className="flex -space-x-2">{members.filter((member) => ['product', 'design', 'dev', 'test'].includes(member.id)).map((member) => <Portrait key={member.id} id={member.id} size="sm" />)}</div><span className="text-[11px] text-[#92796b]">月亮、罐头、米寇和三角都在</span></div>}
    </div>
  </div>
}

export default App

