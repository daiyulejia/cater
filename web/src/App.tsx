import { useEffect, useMemo, useState } from 'react'
import {
  ArrowUp, Bot, Cat, CheckCircle2, ChevronDown, CircleDot,
  CornerDownRight, FileText, Menu, Plus, Send, ShieldCheck, Sparkles, Users,
} from 'lucide-react'
import catTrio from './assets/cat-trio.png'

type Assignment = { id: string; owner: string; role: string; lane: string; objective: string; acceptance?: string; deliverable?: string; status: string }
type Message = { seq: number; actor: string; role: string; type: string; claim?: string; text?: string; stance?: string; target?: string; reply_to?: number }
type Deliverable = { seq: number; title: string; summary: string; kind?: string; uri?: string; message_seq?: number }
type OrgRole = { id: string; name: string; role: string; charter?: string }
type Organization = { leader: OrgRole; cabinet: OrgRole[]; workers: OrgRole[]; mode?: 'local_demo' | 'external_driver' }
type Activity = { id?: string; action?: string; state: 'idle' | 'queued' | 'running' | 'completed' | 'failed'; active: boolean; error?: string }
type Task = {
  id: string; goal: string; acceptance: string[]; status: string; messages: Message[]; assignments: Assignment[]
  deliverables: Deliverable[]
  activity?: Activity
  decision?: { resolution?: string; reason?: string; source?: string }
  brief?: { dispute?: string; consensus?: string[] }; mission?: { goal?: string }; cabinet?: { status?: string; reason?: string }
  interpretation?: { summary?: string; objective?: string; scope?: string[]; risks?: string[]; basis?: string[]; complexity?: string }
  route?: { route?: string; reason?: string }
}
type Event = { seq: number; type: string; category: string; actor: { id: string; role: string; kind: string }; payload: Record<string, unknown> }
type TaskDetail = { task: Task; events: Event[]; review: { health?: string; summary?: string }; activity?: Activity }
type ChatItem = { id: number; actor: string; display: string; role: string; kind: 'human' | 'agent' | 'system'; title?: string; text: string; targets: string[]; replyTo?: number; evidence: Event }

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) } })
  if (!response.ok) throw new Error(await response.text())
  return response.json() as Promise<T>
}

const statusText = (value?: string) => ({
  pending: '待开始', running: '进行中', rework: '需返工', done: '已完成', escalated: '待处理',
  assigned: '已指派', recommended: '已建议', vetoed: '已否决', needs_supreme: '待最高决策', needs_human: '待人工处理',
}[value ?? ''] ?? value ?? '未知')

const actorText = (value: string) => ({ human: '你', orchestrator: '总控', cabinet: '内阁', supreme: '最高决策者', store_manager: '店长', reviewer: '复盘员', driver_gateway: '驱动网关', cabinet_product: '白露', cabinet_engineering: '阿程', cabinet_quality: '知夏', caretaker_product: '白露', caretaker_engineering: '阿程', caretaker_quality: '知夏', product: '月亮', dev: '米寇', test: '三角' }[value] ?? value)

const roleText = (value: string) => ({ human: '顾客', orchestrator: '任务编排', cabinet: '内阁', supreme: '最高决策', store_manager: '猫咖店长', developer: '开发工程师', tester: '测试工程师', product: '产品负责人' }[value] ?? value)

const activityText = (activity?: Activity) => {
  if (activity?.state === 'queued') return '协作已排队'
  if (activity?.state === 'running') return '正在协作'
  if (activity?.state === 'completed') return '协作完成'
  if (activity?.state === 'failed') return '协作失败'
  return ''
}

function reasonText(value: string): string {
  const veto = /^Veto role\(s\) raised concern: (.+)\.$/.exec(value)
  if (veto) return `否决角色提出了风险异议：${veto[1].split(', ').map((role) => `@${role}`).join('、')}。`
  if (value === 'Agents exchanged contributions; governance selected the next action when needed.') return '成员已交换观点；内阁在需要时选择了下一步行动。'
  if (value === 'Agents collaborated without material disagreement; governance was not needed.') return '成员协作中没有出现实质分歧，本轮无需启动治理。'
  const recommended = /^Option (.+) reached approval threshold (\d+)\.$/.exec(value)
  if (recommended) return `方案 ${recommended[1]} 达到 ${recommended[2]} 票通过门槛。`
  return value
}

function Portrait({ id, size = 'md' }: { id: string; size?: 'sm' | 'md' | 'lg' }) {
  const position = ({ product: '0% 50%', test: '50% 50%', dev: '100% 50%' }[id])
  const dimensions = ({ sm: 'size-7', md: 'size-9', lg: 'size-11' }[size])
  if (!position) return <span className={`grid ${dimensions} shrink-0 place-items-center rounded-full bg-[#e7ddd1] text-[#705545]`}><Cat size={size === 'lg' ? 22 : 16} /></span>
  return <span className={`${dimensions} shrink-0 rounded-full border-2 border-[#fffaf4] bg-cover shadow-sm`} style={{ backgroundImage: `url(${catTrio})`, backgroundSize: '300% 100%', backgroundPosition: position }} />
}

function conversationFor(events: Event[], identities: Record<string, OrgRole>): ChatItem[] {
  return events.flatMap((event) => {
    const payload = event.payload
    const actor = event.actor
    const message = (title: string | undefined, text: string, kind: ChatItem['kind']): ChatItem => ({
      id: event.seq, actor: actor.id, display: identities[actor.id]?.name ?? actorText(actor.id), role: actor.role, kind, title, text, evidence: event,
      targets: [
        ...(typeof payload.target === 'string' ? [payload.target] : []),
        ...(Array.isArray(payload.mentions) ? payload.mentions.filter((item): item is string => typeof item === 'string') : []),
      ],
      replyTo: typeof payload.reply_to === 'number' ? payload.reply_to : undefined,
    })
    const memberNames = (value: unknown) => Array.isArray(value)
      ? value.filter((id): id is string => typeof id === 'string').map((id) => identities[id]?.name ?? actorText(id))
      : []
    if (event.type === 'task.created') return [message('新任务', String(payload.goal ?? ''), 'human')]
    if (event.type === 'activity.queued') return [message('协作已排队', String(payload.action) === 'battle' ? '团队将进入内阁审议。' : '团队将开始下一轮协作。', 'system')]
    if (event.type === 'activity.completed') return [message('本轮协作完成', String(payload.action) === 'battle' ? '内阁审议已结束。' : '团队已完成这一轮协作。', 'system')]
    if (event.type === 'activity.failed') return [message('协作异常', String(payload.message ?? '后台协作未能完成。'), 'system')]
    if (event.type === 'human.commanded') return [message('方向更新', String(payload.instruction ?? ''), 'human')]
    if (event.type === 'task.interpreted') return [message('总控任务理解', `${String(payload.summary ?? '')}\n${String(payload.objective ?? '')}`, 'system')]
    if (event.type === 'task.routed') return [message('总控路由判断', reasonText(String(payload.reason ?? '已选择下一步协作路径。')), 'system')]
    if (event.type === 'cabinet.convened') {
      const names = memberNames(payload.members)
      return [message(names.length ? `店长召集 ${names.join('、')}` : '召集内阁', `${reasonText(String(payload.reason ?? '任务需要内阁校准。'))}${names.length ? `\n本次由 ${names.join('、')} 先行校准。` : ''}`, 'system')]
    }
    if (event.type === 'mission.created') return [message('任务使命已发布', String(payload.goal ?? ''), 'system')]
    if (event.type === 'task.decomposed') {
      const names = memberNames(payload.members)
      return [message(String(payload.source) === 'cabinet' ? `${names.join('、') || '内阁'}完成任务拆解` : '总控完成任务拆解', String(payload.strategy ?? '执行计划已就绪。'), 'system')]
    }
    if (event.type === 'subtask.assigned') return [message(`已指派给 @${String(payload.owner ?? '团队')}`, String(payload.objective ?? ''), 'system')]
    if (event.type === 'plan.proposed') return [message('执行计划已发布', reasonText(String(payload.reason ?? '')), 'system')]
    if (event.type === 'msg.said' || event.type === 'msg.challenge') return [message(event.type === 'msg.challenge' ? '提出质疑' : undefined, String(payload.text ?? payload.claim ?? ''), 'agent')]
    if (event.type === 'deliverable.published') return [message('发布成果', `${String(payload.title ?? '未命名成果')}\n${String(payload.summary ?? '')}`, 'agent')]
    if (event.type === 'cabinet.voted') {
      const tally = payload.tally as Record<string, unknown> | undefined
      return [message('内阁审议', reasonText(String(tally?.reason ?? '内阁已审查当前选项。')), 'system')]
    }
    if (event.type === 'agent.decided' || event.type === 'supreme.decided') return [message('自治决策', reasonText(String(payload.reason ?? '')), 'system')]
    if (event.type === 'brief.generated') {
      const brief = payload.brief as Record<string, unknown> | undefined
      return [message('本轮总结', reasonText(String(brief?.dispute ?? '团队已完成一轮协作。')), 'system')]
    }
    if (event.type === 'driver.failed') return [message('已隔离的驱动异常', String(payload.message ?? ''), 'system')]
    return []
  })
}

function App() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [draft, setDraft] = useState('')
  const [acceptance, setAcceptance] = useState('')
  const [routing, setRouting] = useState<'auto' | 'direct'>('auto')
  const [roles, setRoles] = useState<OrgRole[]>([])
  const [organization, setOrganization] = useState<Organization | null>(null)
  const [directOwner, setDirectOwner] = useState('')
  const [composerMode, setComposerMode] = useState<'new' | 'message'>('message')
  const [message, setMessage] = useState('')
  const [isSending, setIsSending] = useState(false)
  const [showEvidence, setShowEvidence] = useState(false)
  const [error, setError] = useState('')

  const refresh = async (preferredId?: string) => {
    const nextTasks = await api<Task[]>('/api/tasks')
    setTasks(nextTasks)
    setSelectedId((current) => preferredId ?? current ?? nextTasks.at(-1)?.id ?? null)
  }

  useEffect(() => { void refresh().catch((err: Error) => setError(err.message)) }, [])
  useEffect(() => {
    void api<OrgRole[]>('/api/organization').then((nextRoles) => {
      setRoles(nextRoles)
      setDirectOwner((current) => current || nextRoles.find((role) => role.id === 'dev')?.id || nextRoles[0]?.id || '')
    }).catch((err: Error) => setError(err.message))
  }, [])
  useEffect(() => { void api<Organization>('/api/organization/profile').then(setOrganization).catch((err: Error) => setError(err.message)) }, [])
  useEffect(() => {
    if (!selectedId) { setDetail(null); return }
    void api<TaskDetail>(`/api/tasks/${selectedId}`).then(setDetail).catch((err: Error) => setError(err.message))
  }, [selectedId])
  useEffect(() => {
    if (!selectedId || !detail?.activity?.active) return
    const poll = () => void api<TaskDetail>(`/api/tasks/${selectedId}`).then((next) => {
      setDetail(next)
      setTasks((current) => current.map((item) => item.id === next.task.id ? { ...next.task, activity: next.activity } : item))
    }).catch((err: Error) => setError(err.message))
    poll()
    const timer = window.setInterval(poll, 800)
    return () => window.clearInterval(timer)
  }, [selectedId, detail?.activity?.active])

  const identities = useMemo(() => {
    const members = organization ? [organization.leader, ...organization.cabinet, ...organization.workers] : roles
    return Object.fromEntries(members.map((member) => [member.id, member]))
  }, [organization, roles])
  const conversation = useMemo(() => detail ? conversationFor(detail.events, identities) : [], [detail, identities])
  const task = detail?.task

  const createTask = async () => {
    const goal = draft.trim()
    if (!goal) return
    setIsSending(true); setError('')
    try {
      const result = await api<TaskDetail>('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          goal,
          acceptance: acceptance.split('\n').map((item) => item.trim()).filter(Boolean),
          run: true, background: true,
          requested_route: routing,
          ...(routing === 'direct' ? { assign_to: directOwner } : {}),
        }),
      })
      setDraft(''); setAcceptance(''); setComposerMode('message'); await refresh(result.task.id); setDetail(result)
    } catch (err) { setError(err instanceof Error ? err.message : '创建任务失败') }
    finally { setIsSending(false) }
  }

  const sendDirection = async () => {
    if (!selectedId || !message.trim()) return
    setIsSending(true); setError('')
    try {
      await api(`/api/tasks/${selectedId}/command`, { method: 'POST', body: JSON.stringify({ instruction: message.trim() }) })
      const result = await api<TaskDetail>(`/api/tasks/${selectedId}/run`, { method: 'POST', body: JSON.stringify({ background: true }) })
      setMessage(''); setDetail(result); await refresh(selectedId)
    } catch (err) { setError(err instanceof Error ? err.message : '发送指令失败') }
    finally { setIsSending(false) }
  }

  return (
    <div className="min-h-screen bg-[#f4eee5] text-stone-800">
      <header className="flex h-16 items-center justify-between border-b border-[#e5d8ca] bg-[#fffaf4] px-4 sm:px-6">
        <div className="flex items-center gap-3"><div className="grid size-9 place-items-center rounded-xl bg-[#cb6e4e] text-white shadow-sm"><Cat size={19} /></div><div><strong className="text-sm tracking-wide text-stone-800">CATER</strong><span className="ml-2 hidden text-xs text-stone-500 sm:inline">猫咖协作工作台</span></div></div>
        <div className="flex items-center gap-2 text-xs text-stone-500"><span className={`rounded-full px-2 py-1 text-[11px] font-medium ${organization?.mode === 'external_driver' ? 'bg-teal-50 text-teal-800' : 'bg-amber-50 text-amber-800'}`}>{organization?.mode === 'external_driver' ? '已接入外部驱动' : '本地演示模式'}</span><ShieldCheck size={15} className="text-teal-700" /> <span className="hidden sm:inline">事件链完整 · 本地工作区</span></div>
      </header>
      <main className="grid min-h-[calc(100vh-56px)] grid-cols-1 lg:grid-cols-[250px_minmax(0,1fr)_310px]">
        <aside className="border-b border-[#e5d8ca] bg-[#fffaf4] lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between px-4 pb-2 pt-4"><span className="text-xs font-semibold tracking-[0.12em] text-stone-500">顾客任务</span><button className="grid size-7 place-items-center rounded-md text-stone-500 hover:bg-[#f3e7da]" title="新建任务" onClick={() => setComposerMode('new')}><Plus size={17} /></button></div>
          <nav className="flex gap-1 overflow-x-auto px-2 pb-3 lg:block lg:space-y-1">{tasks.slice().reverse().map((item) => <button key={item.id} onClick={() => { setSelectedId(item.id); setComposerMode('message') }} className={`min-w-52 rounded-lg px-3 py-3 text-left transition lg:min-w-0 lg:w-full ${item.id === selectedId ? 'bg-[#f7dfcf] text-[#6b3424]' : 'hover:bg-[#f8f0e8]'}`}><div className="flex items-center gap-2"><CircleDot size={14} className={item.status === 'done' ? 'text-emerald-600' : 'text-[#cb6e4e]'} /><span className="truncate text-sm font-medium">{item.goal}</span></div><div className="mt-1 pl-5 text-xs text-stone-500">{item.messages.length} 条消息 · {statusText(item.status)}</div></button>)}</nav>
          <div className="hidden border-t border-[#eee2d6] px-4 py-4 lg:block"><div className="flex -space-x-1">{roles.map((role) => <Portrait key={role.id} id={role.id} size="sm" />)}</div><div className="mt-2 flex items-center gap-2 text-xs text-stone-500"><Users size={15} /> {roles.map((role) => role.name).join(' · ') || '执行猫咪'}</div></div>
        </aside>
        <section className="flex min-h-0 flex-col border-b border-[#e5d8ca] bg-[#fffdf9] lg:border-b-0">
          {task ? <>
            <div className="border-b border-[#eaded1] bg-[#fffaf4] px-5 py-4"><div className="flex items-start justify-between gap-4"><div className="flex items-start gap-3"><Portrait id="store_manager" /><div><p className="mb-1 text-xs font-medium tracking-[0.12em] text-teal-700">店长正在跟进</p><h1 className="text-lg font-semibold leading-snug">{task.goal}</h1></div></div><span className={`rounded-full px-2.5 py-1 text-xs font-medium ${detail?.activity?.active ? 'bg-teal-50 text-teal-800' : 'bg-[#f1e7dc] text-stone-600'}`}>{activityText(detail?.activity) || statusText(task.status)}</span></div></div>
            <div className="flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-7">{conversation.map((item) => <ChatBubble key={item.id} item={item} />)}</div>
            {composerMode === 'new' ? <TaskComposer draft={draft} acceptance={acceptance} routing={routing} roles={roles} directOwner={directOwner} isSending={isSending} onDraftChange={setDraft} onAcceptanceChange={setAcceptance} onRoutingChange={setRouting} onOwnerChange={setDirectOwner} onCreate={() => void createTask()} /> : <DirectionComposer message={message} isSending={isSending} onMessageChange={setMessage} onSend={() => void sendDirection()} />}
          </> : <><EmptyConversation /><TaskComposer draft={draft} acceptance={acceptance} routing={routing} roles={roles} directOwner={directOwner} isSending={isSending} onDraftChange={setDraft} onAcceptanceChange={setAcceptance} onRoutingChange={setRouting} onOwnerChange={setDirectOwner} onCreate={() => void createTask()} /></>}
        </section>
        <aside className="bg-[#fffaf4] p-4 lg:border-l lg:border-[#e5d8ca]">
          {task && <Workboard task={task} detail={detail} names={Object.fromEntries(Object.entries(identities).map(([id, member]) => [id, member.name]))} showEvidence={showEvidence} onToggleEvidence={() => setShowEvidence(!showEvidence)} />}
          {error && <p className="mt-4 rounded-lg bg-rose-50 p-3 text-xs text-rose-700">{error}</p>}
        </aside>
      </main>
    </div>
  )
}

function DirectionComposer({ message, isSending, onMessageChange, onSend }: { message: string; isSending: boolean; onMessageChange: (value: string) => void; onSend: () => void }) {
  return <div className="border-t border-[#e5d8ca] bg-[#fffaf4] p-3 sm:p-4"><div className="mx-auto flex max-w-3xl gap-2 rounded-xl border border-[#ddccbc] bg-white p-2 shadow-sm focus-within:border-[#cb6e4e] focus-within:ring-2 focus-within:ring-[#f7dfcf]"><textarea value={message} onChange={(event) => onMessageChange(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); onSend() } }} placeholder="向店长和猫咪们补充方向" aria-label="向团队发送消息" rows={2} className="min-h-12 flex-1 resize-none border-0 bg-transparent px-2 py-1 text-sm outline-none placeholder:text-stone-400" /><button disabled={isSending || !message.trim()} onClick={onSend} className="grid size-10 shrink-0 place-items-center self-end rounded-lg bg-[#cb6e4e] text-white disabled:bg-stone-200" title="发送指令并启动下一轮协作"><Send size={17} /></button></div></div>
}

function TaskComposer({ draft, acceptance, routing, roles, directOwner, isSending, onDraftChange, onAcceptanceChange, onRoutingChange, onOwnerChange, onCreate }: { draft: string; acceptance: string; routing: 'auto' | 'direct'; roles: OrgRole[]; directOwner: string; isSending: boolean; onDraftChange: (value: string) => void; onAcceptanceChange: (value: string) => void; onRoutingChange: (value: 'auto' | 'direct') => void; onOwnerChange: (value: string) => void; onCreate: () => void }) {
  return <div className="border-t border-[#e5d8ca] bg-[#fffaf4] p-3 sm:p-4"><div className="mx-auto max-w-3xl rounded-xl border border-[#ddccbc] bg-white p-3 shadow-sm focus-within:border-[#cb6e4e] focus-within:ring-2 focus-within:ring-[#f7dfcf]"><div className="mb-2 flex items-center gap-2"><Sparkles size={16} className="text-[#cb6e4e]" /><h2 className="text-sm font-semibold">向猫咖下单</h2></div><textarea id="new-task" value={draft} onChange={(event) => onDraftChange(event.target.value)} placeholder="希望店长和猫咪们完成什么任务？" rows={3} className="w-full resize-none border-0 px-1 py-1 text-sm outline-none placeholder:text-stone-400" /><textarea value={acceptance} onChange={(event) => onAcceptanceChange(event.target.value)} placeholder="验收标准，每行一项（可选）" rows={2} className="mt-2 w-full resize-none rounded-lg border border-[#eaded1] px-3 py-2 text-sm outline-none focus:border-[#cb6e4e]" /><div className="mt-3 flex flex-wrap items-center gap-2"><div className="grid grid-cols-2 rounded-lg border border-[#eaded1] bg-[#fcf5ee] p-1" role="group" aria-label="任务路径"><button type="button" onClick={() => onRoutingChange('auto')} className={`rounded-md px-3 py-2 text-xs font-medium ${routing === 'auto' ? 'bg-white text-[#8b4732] shadow-sm' : 'text-stone-500'}`}>交给店长判断</button><button type="button" onClick={() => onRoutingChange('direct')} className={`rounded-md px-3 py-2 text-xs font-medium ${routing === 'direct' ? 'bg-white text-[#8b4732] shadow-sm' : 'text-stone-500'}`}>优先找猫咪</button></div>{routing === 'direct' && <select aria-label="执行角色偏好" value={directOwner} onChange={(event) => onOwnerChange(event.target.value)} className="min-w-36 rounded-lg border border-[#ddccbc] bg-white px-2 py-2 text-sm text-stone-800 outline-none focus:border-[#cb6e4e]">{roles.map((role) => <option key={role.id} value={role.id}>{role.name} · {role.role}</option>)}</select>}<button disabled={isSending || !draft.trim() || (routing === 'direct' && !directOwner)} onClick={onCreate} className="ml-auto inline-flex items-center justify-center gap-2 rounded-lg bg-[#cb6e4e] px-3 py-2 text-sm font-semibold text-white disabled:bg-stone-200"><ArrowUp size={16} /> 交给店长</button></div></div></div>
}

function ChatBubble({ item }: { item: ChatItem }) {
  const system = item.kind === 'system'; const human = item.kind === 'human'
  return <article className={`flex gap-3 ${human ? 'justify-end' : ''}`}>
    {!human && (system ? <div className="mt-1 grid size-9 shrink-0 place-items-center rounded-full bg-[#efe3d7] text-[#8a614b]"><Bot size={16} /></div> : <Portrait id={item.actor} />)}
    <div className={`max-w-[82%] ${human ? 'rounded-2xl rounded-tr-sm bg-[#cb6e4e] px-4 py-3 text-white' : 'rounded-2xl rounded-tl-sm border border-[#eaded1] bg-white px-4 py-3 shadow-[0_1px_2px_rgba(97,62,40,.08)]'}`}><div className={`mb-1 flex items-center gap-2 text-xs ${human ? 'text-[#fde9dd]' : 'text-stone-500'}`}><strong className={human ? 'text-white' : 'text-stone-800'}>{human ? '你' : item.display}</strong><span>{roleText(item.role)}</span>{item.title && <span className="rounded bg-[#f5ece4] px-1.5 py-0.5 text-[10px] font-medium text-[#80533f]">{item.title}</span>}</div><p className="whitespace-pre-wrap text-sm leading-6">{item.text}</p>{(item.targets.length > 0 || item.replyTo) && <div className={`mt-2 flex flex-wrap gap-2 text-xs ${human ? 'text-[#fde9dd]' : 'text-teal-700'}`}>{item.targets.map((target) => <span key={target}>@{target}</span>)}{item.replyTo && <span className="inline-flex items-center gap-1"><CornerDownRight size={13} /> 回复 #{item.replyTo}</span>}</div>}<details className={`mt-2 text-xs ${human ? 'text-[#fde9dd]' : 'text-stone-400'}`}><summary className="cursor-pointer">决策依据 · 事件 #{item.id}</summary><pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-stone-950 p-2 text-[10px] text-stone-100">{JSON.stringify(item.evidence.payload, null, 2)}</pre></details></div>
  </article>
}

function Workboard({ task, detail, names, showEvidence, onToggleEvidence }: { task: Task; detail: TaskDetail | null; names: Record<string, string>; showEvidence: boolean; onToggleEvidence: () => void }) {
  const assignments = [...task.assignments].reverse().filter((item, index, all) => all.findIndex((other) => other.owner === item.owner) === index)
  const deliverables = [...(task.deliverables ?? [])].reverse()
  return <div className="space-y-5 border-t border-[#eaded1] pt-5"><section><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">猫咪分工</h2><span className="text-xs text-stone-400">{assignments.length} 条执行线</span></div><div className="space-y-2">{assignments.map((item) => <div key={item.id} className="border-l-2 border-[#cb6e4e] pl-3 py-1"><div className="flex items-center justify-between gap-2"><span className="flex items-center gap-2"><Portrait id={item.owner} size="sm" /><strong className="text-xs">{names[item.owner] ?? item.owner}</strong></span><span className="text-[11px] text-stone-400">{item.lane}</span></div><p className="mt-1 text-xs leading-5 text-stone-600">{item.objective}</p></div>)}</div></section><section><div className="mb-2 flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">已发布成果</h2><span className="text-xs text-stone-400">{deliverables.length} 项</span></div>{deliverables.length ? <div className="space-y-2">{deliverables.map((item) => <div key={item.seq} className="border-l-2 border-emerald-600 pl-3 py-1"><strong className="block text-xs text-stone-800">{item.title}</strong><p className="mt-1 text-xs leading-5 text-stone-600">{item.summary}</p>{item.uri && <span className="mt-1 block truncate text-[11px] text-teal-700">{item.uri}</span>}</div>)}</div> : <p className="text-xs leading-5 text-stone-400">猫咪们尚未发布可复用的成果。</p>}</section><section><div className="mb-2 flex items-center gap-2"><CheckCircle2 size={16} className="text-teal-700" /><h2 className="text-sm font-semibold">当前结论</h2></div><p className="text-sm leading-6 text-stone-600">{reasonText(task.decision?.reason ?? task.brief?.dispute ?? '团队正在形成下一步可交付结果。')}</p>{task.decision && <span className="mt-2 inline-block rounded-full bg-teal-50 px-2 py-1 text-xs text-teal-800">{actorText(task.decision.source ?? '团队')} · {statusText(task.decision.resolution)}</span>}</section><section className="border-t border-[#eaded1] pt-4"><button onClick={onToggleEvidence} className="flex w-full items-center justify-between text-left text-sm font-semibold"><span className="flex items-center gap-2"><FileText size={16} className="text-stone-500" /> 过程证据</span><ChevronDown size={16} className={showEvidence ? 'rotate-180 transition' : 'transition'} /></button>{showEvidence && <div className="mt-3 max-h-52 overflow-auto rounded-lg bg-[#fcf5ee] p-3 text-xs text-stone-500"><p>{detail?.events.length ?? 0} 条不可变事件已记录。</p><a className="mt-2 inline-block text-teal-700 hover:underline" href={`/tasks/${task.id}`}>打开完整审计回放</a></div>}</section></div>
}

function EmptyConversation() { return <div className="grid flex-1 place-items-center px-6 text-center"><div><div className="mx-auto mb-4 grid size-12 place-items-center rounded-2xl bg-teal-100 text-teal-800"><Menu size={22} /></div><h1 className="text-xl font-semibold">暂无进行中的任务</h1><p className="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-500">从下方发起一个任务。</p></div></div> }

export default App

