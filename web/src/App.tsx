import { useEffect, useMemo, useState } from 'react'
import {
  ArrowUp, Bot, CheckCircle2, ChevronDown, CircleDot,
  Command, CornerDownRight, FileText, Menu, Plus, Send, ShieldCheck, Sparkles, Users,
} from 'lucide-react'

type Assignment = { id: string; owner: string; role: string; lane: string; objective: string; acceptance?: string; deliverable?: string; status: string }
type Message = { seq: number; actor: string; role: string; type: string; claim?: string; text?: string; stance?: string; target?: string; reply_to?: number }
type Deliverable = { seq: number; title: string; summary: string; kind?: string; uri?: string; message_seq?: number }
type Task = {
  id: string; goal: string; acceptance: string[]; status: string; messages: Message[]; assignments: Assignment[]
  deliverables: Deliverable[]
  decision?: { resolution?: string; reason?: string; source?: string }
  brief?: { dispute?: string; consensus?: string[] }; mission?: { goal?: string }; cabinet?: { status?: string; reason?: string }
}
type Event = { seq: number; type: string; category: string; actor: { id: string; role: string; kind: string }; payload: Record<string, unknown> }
type TaskDetail = { task: Task; events: Event[]; review: { health?: string; summary?: string } }
type ChatItem = { id: number; actor: string; role: string; kind: 'human' | 'agent' | 'system'; title?: string; text: string; targets: string[]; replyTo?: number; evidence: Event }

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) } })
  if (!response.ok) throw new Error(await response.text())
  return response.json() as Promise<T>
}

const statusText = (value?: string) => ({
  pending: '待开始', running: '进行中', rework: '需返工', done: '已完成', escalated: '待处理',
  assigned: '已指派', recommended: '已建议', vetoed: '已否决', needs_supreme: '待最高决策', needs_human: '待人工处理',
}[value ?? ''] ?? value ?? '未知')

const actorText = (value: string) => ({ human: '你', orchestrator: '总控', cabinet: '内阁', supreme: '最高决策者', reviewer: '复盘员', driver_gateway: '驱动网关', product: '产品', dev: '开发', test: '测试' }[value] ?? value)

const roleText = (value: string) => ({ human: '人类', orchestrator: '任务编排', cabinet: '内阁', supreme: '最高决策', developer: '开发工程师', tester: '测试工程师', product: '产品负责人' }[value] ?? value)

function reasonText(value: string): string {
  const veto = /^Veto role\(s\) raised concern: (.+)\.$/.exec(value)
  if (veto) return `否决角色提出了风险异议：${veto[1].split(', ').map((role) => `@${role}`).join('、')}。`
  if (value === 'Agents exchanged contributions; governance selected the next action when needed.') return '成员已交换观点；内阁在需要时选择了下一步行动。'
  if (value === 'Agents collaborated without material disagreement; governance was not needed.') return '成员协作中没有出现实质分歧，本轮无需启动治理。'
  const recommended = /^Option (.+) reached approval threshold (\d+)\.$/.exec(value)
  if (recommended) return `方案 ${recommended[1]} 达到 ${recommended[2]} 票通过门槛。`
  return value
}

function conversationFor(events: Event[]): ChatItem[] {
  return events.flatMap((event) => {
    const payload = event.payload
    const actor = event.actor
    const message = (title: string | undefined, text: string, kind: ChatItem['kind']): ChatItem => ({
      id: event.seq, actor: actor.id, role: actor.role, kind, title, text, evidence: event,
      targets: [
        ...(typeof payload.target === 'string' ? [payload.target] : []),
        ...(Array.isArray(payload.mentions) ? payload.mentions.filter((item): item is string => typeof item === 'string') : []),
      ],
      replyTo: typeof payload.reply_to === 'number' ? payload.reply_to : undefined,
    })
    if (event.type === 'task.created') return [message('新任务', String(payload.goal ?? ''), 'human')]
    if (event.type === 'human.commanded') return [message('方向更新', String(payload.instruction ?? ''), 'human')]
    if (event.type === 'mission.created') return [message('总控已理解任务', String(payload.goal ?? ''), 'system')]
    if (event.type === 'task.decomposed') return [message('内阁完成任务拆解', String(payload.strategy ?? '执行计划已就绪。'), 'system')]
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
    if (!selectedId) { setDetail(null); return }
    void api<TaskDetail>(`/api/tasks/${selectedId}`).then(setDetail).catch((err: Error) => setError(err.message))
  }, [selectedId])

  const conversation = useMemo(() => detail ? conversationFor(detail.events) : [], [detail])
  const task = detail?.task

  const createTask = async () => {
    const goal = draft.trim()
    if (!goal) return
    setIsSending(true); setError('')
    try {
      const result = await api<TaskDetail>('/api/tasks', { method: 'POST', body: JSON.stringify({ goal, acceptance: acceptance.split('\n').map((item) => item.trim()).filter(Boolean), dispatch: true, run: true }) })
      setDraft(''); setAcceptance(''); await refresh(result.task.id); setDetail(result)
    } catch (err) { setError(err instanceof Error ? err.message : '创建任务失败') }
    finally { setIsSending(false) }
  }

  const sendDirection = async () => {
    if (!selectedId || !message.trim()) return
    setIsSending(true); setError('')
    try {
      await api(`/api/tasks/${selectedId}/command`, { method: 'POST', body: JSON.stringify({ instruction: message.trim() }) })
      const result = await api<TaskDetail>(`/api/tasks/${selectedId}/run`, { method: 'POST', body: '{}' })
      setMessage(''); setDetail(result); await refresh(selectedId)
    } catch (err) { setError(err instanceof Error ? err.message : '发送指令失败') }
    finally { setIsSending(false) }
  }

  return (
    <div className="min-h-screen bg-[#f7f7f5] text-slate-900">
      <header className="flex h-14 items-center justify-between border-b border-slate-200 bg-white px-4 sm:px-6">
        <div className="flex items-center gap-3"><div className="grid size-8 place-items-center rounded-lg bg-teal-700 text-white"><Command size={17} /></div><div><strong className="text-sm tracking-wide">CATER</strong><span className="ml-2 hidden text-xs text-slate-400 sm:inline">自治工作台</span></div></div>
        <div className="flex items-center gap-2 text-xs text-slate-500"><ShieldCheck size={15} className="text-teal-700" /> 事件链完整 <span className="hidden sm:inline">· 本地工作区</span></div>
      </header>
      <main className="grid min-h-[calc(100vh-56px)] grid-cols-1 lg:grid-cols-[250px_minmax(0,1fr)_310px]">
        <aside className="border-b border-slate-200 bg-white lg:border-b-0 lg:border-r">
          <div className="flex items-center justify-between px-4 pb-2 pt-4"><span className="text-xs font-semibold tracking-[0.12em] text-slate-500">任务空间</span><button className="grid size-7 place-items-center rounded-md text-slate-500 hover:bg-slate-100" title="新建任务" onClick={() => document.getElementById('new-task')?.focus()}><Plus size={17} /></button></div>
          <nav className="flex gap-1 overflow-x-auto px-2 pb-3 lg:block lg:space-y-1">{tasks.slice().reverse().map((item) => <button key={item.id} onClick={() => setSelectedId(item.id)} className={`min-w-52 rounded-lg px-3 py-3 text-left transition lg:min-w-0 lg:w-full ${item.id === selectedId ? 'bg-teal-50 text-teal-950' : 'hover:bg-slate-50'}`}><div className="flex items-center gap-2"><CircleDot size={14} className={item.status === 'done' ? 'text-emerald-600' : 'text-amber-600'} /><span className="truncate text-sm font-medium">{item.goal}</span></div><div className="mt-1 pl-5 text-xs text-slate-500">{item.messages.length} 条消息 · {statusText(item.status)}</div></button>)}</nav>
          <div className="hidden border-t border-slate-100 px-4 py-4 lg:block"><div className="flex items-center gap-2 text-xs text-slate-500"><Users size={15} /> 产品 · 开发 · 测试</div></div>
        </aside>
        <section className="flex min-h-0 flex-col border-b border-slate-200 bg-[#fbfbfa] lg:border-b-0">
          {task ? <>
            <div className="border-b border-slate-200 bg-white px-5 py-4"><div className="flex items-start justify-between gap-4"><div><p className="mb-1 text-xs font-medium tracking-[0.12em] text-teal-700">当前任务</p><h1 className="text-lg font-semibold leading-snug">{task.goal}</h1></div><span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">{statusText(task.status)}</span></div></div>
            <div className="flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-7">{conversation.map((item) => <ChatBubble key={item.id} item={item} />)}</div>
            <div className="border-t border-slate-200 bg-white p-3 sm:p-4"><div className="mx-auto flex max-w-3xl gap-2 rounded-xl border border-slate-300 bg-white p-2 shadow-sm focus-within:border-teal-600 focus-within:ring-2 focus-within:ring-teal-100"><textarea value={message} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void sendDirection() } }} placeholder="向团队发送消息" aria-label="向团队发送消息" rows={2} className="min-h-12 flex-1 resize-none border-0 bg-transparent px-2 py-1 text-sm outline-none placeholder:text-slate-400" /><button disabled={isSending || !message.trim()} onClick={() => void sendDirection()} className="grid size-10 shrink-0 place-items-center self-end rounded-lg bg-teal-700 text-white disabled:bg-slate-200" title="发送指令并启动下一轮协作"><Send size={17} /></button></div></div>
          </> : <EmptyConversation />}
        </section>
        <aside className="bg-white p-4 lg:border-l lg:border-slate-200">
          <section className="mb-6"><div className="mb-3 flex items-center gap-2"><Sparkles size={16} className="text-teal-700" /><h2 className="text-sm font-semibold">发起任务</h2></div><textarea id="new-task" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="希望组织完成什么任务？" rows={3} className="w-full resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal-600 focus:ring-2 focus:ring-teal-100" /><textarea value={acceptance} onChange={(event) => setAcceptance(event.target.value)} placeholder="验收标准，每行一项（可选）" rows={2} className="mt-2 w-full resize-none rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-teal-600 focus:ring-2 focus:ring-teal-100" /><button disabled={isSending || !draft.trim()} onClick={() => void createTask()} className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-teal-700 px-3 py-2.5 text-sm font-semibold text-white disabled:bg-slate-200"><ArrowUp size={16} /> 交给 Cater</button></section>
          {task && <Workboard task={task} detail={detail} showEvidence={showEvidence} onToggleEvidence={() => setShowEvidence(!showEvidence)} />}
          {error && <p className="mt-4 rounded-lg bg-rose-50 p-3 text-xs text-rose-700">{error}</p>}
        </aside>
      </main>
    </div>
  )
}

function ChatBubble({ item }: { item: ChatItem }) {
  const system = item.kind === 'system'; const human = item.kind === 'human'
  return <article className={`flex gap-3 ${human ? 'justify-end' : ''}`}>
    {!human && <div className={`mt-1 grid size-8 shrink-0 place-items-center rounded-full text-xs font-bold ${system ? 'bg-slate-200 text-slate-600' : 'bg-teal-100 text-teal-800'}`}>{system ? <Bot size={16} /> : item.actor.slice(0, 1).toUpperCase()}</div>}
    <div className={`max-w-[82%] ${human ? 'rounded-2xl rounded-tr-sm bg-teal-700 px-4 py-3 text-white' : 'rounded-2xl rounded-tl-sm border border-slate-200 bg-white px-4 py-3 shadow-[0_1px_2px_rgba(15,23,42,.04)]'}`}><div className={`mb-1 flex items-center gap-2 text-xs ${human ? 'text-teal-100' : 'text-slate-500'}`}><strong className={human ? 'text-white' : 'text-slate-800'}>{human ? '你' : actorText(item.actor)}</strong><span>{roleText(item.role)}</span>{item.title && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600">{item.title}</span>}</div><p className="whitespace-pre-wrap text-sm leading-6">{item.text}</p>{(item.targets.length > 0 || item.replyTo) && <div className={`mt-2 flex flex-wrap gap-2 text-xs ${human ? 'text-teal-100' : 'text-teal-700'}`}>{item.targets.map((target) => <span key={target}>@{target}</span>)}{item.replyTo && <span className="inline-flex items-center gap-1"><CornerDownRight size={13} /> 回复 #{item.replyTo}</span>}</div>}<details className={`mt-2 text-xs ${human ? 'text-teal-100' : 'text-slate-400'}`}><summary className="cursor-pointer">证据 · 事件 #{item.id}</summary><pre className="mt-2 max-h-36 overflow-auto whitespace-pre-wrap rounded bg-slate-950 p-2 text-[10px] text-slate-100">{JSON.stringify(item.evidence.payload, null, 2)}</pre></details></div>
  </article>
}

function Workboard({ task, detail, showEvidence, onToggleEvidence }: { task: Task; detail: TaskDetail | null; showEvidence: boolean; onToggleEvidence: () => void }) {
  const assignments = [...task.assignments].reverse().filter((item, index, all) => all.findIndex((other) => other.owner === item.owner) === index)
  const deliverables = [...(task.deliverables ?? [])].reverse()
  return <div className="space-y-5 border-t border-slate-100 pt-5"><section><div className="mb-2 flex items-center justify-between"><h2 className="text-sm font-semibold">工作分解</h2><span className="text-xs text-slate-400">{assignments.length} 条执行线</span></div><div className="space-y-2">{assignments.map((item) => <div key={item.id} className="border-l-2 border-teal-600 pl-3 py-1"><div className="flex items-center justify-between gap-2"><strong className="text-xs">@{item.owner}</strong><span className="text-[11px] text-slate-400">{item.lane}</span></div><p className="mt-1 text-xs leading-5 text-slate-600">{item.objective}</p></div>)}</div></section><section><div className="mb-2 flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">已发布成果</h2><span className="text-xs text-slate-400">{deliverables.length} 项</span></div>{deliverables.length ? <div className="space-y-2">{deliverables.map((item) => <div key={item.seq} className="border-l-2 border-emerald-600 pl-3 py-1"><strong className="block text-xs text-slate-800">{item.title}</strong><p className="mt-1 text-xs leading-5 text-slate-600">{item.summary}</p>{item.uri && <span className="mt-1 block truncate text-[11px] text-teal-700">{item.uri}</span>}</div>)}</div> : <p className="text-xs leading-5 text-slate-400">团队尚未发布可复用的成果。</p>}</section><section><div className="mb-2 flex items-center gap-2"><CheckCircle2 size={16} className="text-teal-700" /><h2 className="text-sm font-semibold">当前结论</h2></div><p className="text-sm leading-6 text-slate-600">{reasonText(task.decision?.reason ?? task.brief?.dispute ?? '团队正在形成下一步可交付结果。')}</p>{task.decision && <span className="mt-2 inline-block rounded-full bg-teal-50 px-2 py-1 text-xs text-teal-800">{actorText(task.decision.source ?? '团队')} · {statusText(task.decision.resolution)}</span>}</section><section className="border-t border-slate-100 pt-4"><button onClick={onToggleEvidence} className="flex w-full items-center justify-between text-left text-sm font-semibold"><span className="flex items-center gap-2"><FileText size={16} className="text-slate-500" /> 过程证据</span><ChevronDown size={16} className={showEvidence ? 'rotate-180 transition' : 'transition'} /></button>{showEvidence && <div className="mt-3 max-h-52 overflow-auto rounded-lg bg-slate-50 p-3 text-xs text-slate-500"><p>{detail?.events.length ?? 0} 条不可变事件已记录。</p><a className="mt-2 inline-block text-teal-700 hover:underline" href={`/tasks/${task.id}`}>打开完整审计回放</a></div>}</section></div>
}

function EmptyConversation() { return <div className="grid flex-1 place-items-center px-6 text-center"><div><div className="mx-auto mb-4 grid size-12 place-items-center rounded-2xl bg-teal-100 text-teal-800"><Menu size={22} /></div><h1 className="text-xl font-semibold">暂无进行中的任务</h1><p className="mx-auto mt-2 max-w-md text-sm leading-6 text-slate-500">从右侧发起一个任务。</p></div></div> }

export default App

