import { Cat, CheckCircle2, CircleHelp, CircleX, Clock3, FilePenLine, MessageCircle, Pause, PawPrint, Search, TriangleAlert, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { ExecutionControl } from './executionControl'
import { executionLabel } from './executionControl'
import type { TeamSummary } from './team'
import { conversationTitle } from './conversationTitle'
import './TaskList.css'

type ListedTask = {
  id: string
  goal: string
  status: string
  activity?: { active: boolean; state?: string }
  execution_control?: Pick<ExecutionControl, 'state'>
  team?: Pick<TeamSummary, 'enabled' | 'status'> & Partial<TeamSummary> | null
}
type Filter = 'all' | 'unfinished' | 'done'

type ActivityState = 'running' | 'queued' | 'waiting' | 'paused' | 'cancelled' | 'failed' | 'blocked' | 'uncertain' | 'done' | 'idle'

function listActivity(task: ListedTask, control?: ExecutionControl['state']): ActivityState {
  if (control === 'paused' || control === 'cancelled') return control
  const activity = task.activity
  const team = task.team?.enabled ? task.team : undefined
  const effects = team?.effects_pending_count
  if (effects !== undefined && (effects === null || !Number.isSafeInteger(effects) || effects < 0)) return 'uncertain'
  if (activity?.active && activity.state === 'queued') return 'queued'
  if (activity?.active && activity.state === 'running') return 'running'
  if (effects !== undefined && effects > 0) return 'uncertain'
  if (activity?.state === 'failed') return 'failed'
  // A live consumer with no reliable state cannot confirm old run records.
  if (activity?.active) return 'uncertain'

  const runs = (team?.runs ?? []).filter(run => run.revision === undefined || run.revision === team?.revision)
  // Match teamStatus: inactive consumers make leftover running records uncertain.
  if (runs.some(run => run.status === 'running' || run.status === 'started')) return activity?.active === false ? 'uncertain' : 'running'
  const latest = runs.at(-1)?.status
  if (latest === 'failed') return 'failed'
  if ((team?.pending_count ?? 0) > 0) return 'waiting'
  if (team?.status === 'done' || (!team && task.status === 'done' && !activity?.active)) return 'done'
  if (team?.status === 'failed' || task.status === 'failed') return 'failed'
  if (team?.status === 'blocked' || latest === 'blocked') return 'blocked'
  if (latest === 'uncertain') return 'uncertain'
  if (latest === 'interrupted') return 'uncertain'
  if (latest === 'cancelled') return 'cancelled'
  if (latest === 'waiting') return 'waiting'
  return 'idle'
}

const activityIcons = {
  running: Cat, queued: Clock3, waiting: MessageCircle, paused: Pause, cancelled: CircleX,
  failed: TriangleAlert, blocked: TriangleAlert, uncertain: CircleHelp, done: CheckCircle2, idle: Cat,
}

function TaskActivity({ state }: { state: ActivityState }) {
  const Icon = activityIcons[state]
  return <span className="task-list-activity" data-state={state} aria-hidden="true">
    <Icon size={15} className="task-list-activity-icon" />
    {state === 'running' && <>
      <span className="task-list-activity-light" />
      <PawPrint size={5} className="task-list-activity-paw task-list-activity-paw-left" />
      <PawPrint size={5} className="task-list-activity-paw task-list-activity-paw-right" />
    </>}
  </span>
}

export function TaskList<T extends ListedTask>({ tasks, selectedId, drafts, collapsed, searchOpen, onChoose, statusLabel, executionState }: {
  tasks: T[]
  selectedId: string | null
  drafts: Record<string, string>
  collapsed: boolean
  searchOpen: boolean
  onChoose: (id: string) => void
  statusLabel: (task: T) => string
  executionState?: (task: T) => ExecutionControl['state'] | undefined
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const matches = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return tasks.filter((task) => {
      const done = task.team?.enabled ? task.team.status === 'done' : task.status === 'done' && !task.activity?.active
      return (filter === 'all' || (filter === 'done' ? done : !done))
        && (!keyword || task.goal.toLowerCase().includes(keyword) || task.id.toLowerCase().includes(keyword))
    }).slice().reverse()
  }, [tasks, query, filter])
  const narrowed = Boolean(query.trim()) || filter !== 'all'

  return <div id="task-list" className={`min-h-0 lg:flex lg:flex-1 lg:flex-col ${collapsed ? 'mobile-collapsed' : ''}`}>
    <div id="task-search" className={`shrink-0 px-3 pb-2 ${searchOpen ? '' : 'hidden lg:block'}`}>
      <div className="task-search flex h-9 items-center gap-2 px-2">
        <Search size={14} className="shrink-0 text-[#7c918b]" />
        <input type="search" aria-label="搜索任务标题或编号" placeholder="搜索标题或编号" value={query} onChange={(event) => setQuery(event.target.value)} className="min-w-0 flex-1 bg-transparent text-xs outline-none" />
        {query && <button type="button" onClick={() => setQuery('')} aria-label="清除任务搜索" title="清除搜索" className="grid size-6 shrink-0 place-items-center rounded text-[#7c918b]"><X size={14} /></button>}
      </div>
      <div className="mt-2 grid grid-cols-3 gap-1" role="group" aria-label="任务状态筛选">
        {([{ id: 'all', label: '全部' }, { id: 'unfinished', label: '未完成' }, { id: 'done', label: '已完成' }] as const).map((item) => <button key={item.id} type="button" onClick={() => setFilter(item.id)} aria-pressed={filter === item.id} className={`task-filter h-7 text-[11px] font-semibold ${filter === item.id ? 'is-active' : ''}`}>{item.label}</button>)}
      </div>
    </div>
    {narrowed && <p role="status" className="shrink-0 px-3 pb-2 text-[11px] text-[#72877c]">找到 {matches.length} / {tasks.length} 件事</p>}
    <nav className="flex gap-2 overflow-x-auto px-3 pb-3 lg:min-h-0 lg:flex-1 lg:block lg:space-y-2 lg:overflow-x-hidden lg:overflow-y-auto" aria-label="任务列表">
      {matches.map((item) => {
        const control = executionState?.(item) ?? item.execution_control?.state
        const state = listActivity(item, control)
        const label = executionLabel(control) || statusLabel(item)
        const title = conversationTitle(item.goal)
        return <button key={item.id} onClick={() => onChoose(item.id)} aria-current={item.id === selectedId ? 'page' : undefined} className={`task-item w-56 shrink-0 px-3 py-3 text-left lg:w-full ${item.id === selectedId ? 'is-active' : ''}`}>
          <div className="flex items-start gap-2"><TaskActivity state={state} /><span aria-label={title.excerpt ? `目标节选：${title.label}` : undefined} className="line-clamp-2 min-w-0 [overflow-wrap:anywhere] text-[13px] font-semibold leading-5">{title.label}</span></div>
          <div className="mt-2 flex items-center gap-2 pl-[23px] text-[11px] text-[#7c918b]"><span className="task-list-state-label min-w-0 break-words" data-state={state}>{label}</span>{drafts[item.id]?.trim() && <span title="有未发送内容" role="img" aria-label="有未发送内容" className="shrink-0 text-[#b65f49]"><FilePenLine size={13} /></span>}</div>
        </button>
      })}
      {!matches.length && <div className="w-full py-3 text-xs leading-5 text-[#7b8a7a]">
        <p>{tasks.length ? '没有匹配的任务。' : '还没有任务，先和店长聊聊吧。'}</p>
        {narrowed && <button type="button" onClick={() => { setQuery(''); setFilter('all') }} className="mt-1 text-[#a55642] underline underline-offset-4">恢复全部任务</button>}
      </div>}
    </nav>
  </div>
}
