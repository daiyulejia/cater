import { CircleDot, FilePenLine, Search, X } from 'lucide-react'
import { useMemo, useState } from 'react'

type ListedTask = {
  id: string
  goal: string
  status: string
  conversation_count?: number
  activity?: { active: boolean }
}
type Filter = 'all' | 'unfinished' | 'done'

export function TaskList<T extends ListedTask>({ tasks, selectedId, drafts, collapsed, searchOpen, onChoose, statusLabel }: {
  tasks: T[]
  selectedId: string | null
  drafts: Record<string, string>
  collapsed: boolean
  searchOpen: boolean
  onChoose: (id: string) => void
  statusLabel: (task: T) => string
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const matches = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    return tasks.filter((task) => {
      const done = task.status === 'done' && !task.activity?.active
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
      {matches.map((item) => <button key={item.id} onClick={() => onChoose(item.id)} aria-current={item.id === selectedId ? 'page' : undefined} className={`task-item w-56 shrink-0 px-3 py-3 text-left lg:w-full ${item.id === selectedId ? 'is-active' : ''}`}>
        <div className="flex items-start gap-2"><CircleDot size={15} className={`mt-0.5 shrink-0 ${item.status === 'done' && !item.activity?.active ? 'text-emerald-600' : 'text-[#c2634d]'}`} /><span className="line-clamp-2 min-w-0 break-words text-[13px] font-semibold leading-5">{item.goal}</span></div>
        <div className="mt-2 flex items-center gap-2 pl-[23px] text-[11px] text-[#7c918b]"><span>{item.conversation_count === undefined ? '对话待同步' : `${item.conversation_count} 条对话`} · {statusLabel(item)}</span>{drafts[item.id]?.trim() && <span title="有未发送内容" role="img" aria-label="有未发送内容" className="text-[#b65f49]"><FilePenLine size={13} /></span>}</div>
      </button>)}
      {!matches.length && <div className="w-full py-3 text-xs leading-5 text-[#7b8a7a]">
        <p>{tasks.length ? '没有匹配的任务。' : '还没有任务，先和店长聊聊吧。'}</p>
        {narrowed && <button type="button" onClick={() => { setQuery(''); setFilter('all') }} className="mt-1 text-[#a55642] underline underline-offset-4">恢复全部任务</button>}
      </div>}
    </nav>
  </div>
}
