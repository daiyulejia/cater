import { useEffect, useReducer } from 'react'
import type { ReactNode } from 'react'
import { CircleAlert, Clock3, ListTree, LoaderCircle, Pause, RefreshCw } from 'lucide-react'
import { conversationActivity } from './conversationActivityState'
import type { ConversationActivityInput } from './conversationActivityState'

export type ConversationActivityProps = ConversationActivityInput & {
  portrait: (id: string) => ReactNode
  onInspect: () => void
  onRefresh: () => void
  onPause: () => void
}


export function ConversationActivity(props: ConversationActivityProps) {
  const [, tick] = useReducer((value: number) => value + 1, 0)
  const view = conversationActivity(props)
  const lines = [...view.lines].sort((a, b) => Number(Boolean(b.warning)) - Number(Boolean(a.warning))
    || Number(Boolean(b.moving)) - Number(Boolean(a.moving))).slice(0, 2)
  const deadline = view.visible ? view.nextCheckAt : undefined
  useEffect(() => {
    if (deadline === undefined) return
    const timer = window.setTimeout(tick, Math.max(1, deadline - Date.now() + 1))
    return () => window.clearTimeout(timer)
  }, [deadline])
  if (!view.visible) return null
  const canPause = props.activity?.active === true && props.executionState === 'running' && !props.controlBusy
  const buttonClass = 'inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-[#52766d] hover:bg-[#edf4ef] focus-visible:outline-2 focus-visible:outline-[#d77a6b] disabled:opacity-40'
  return <section aria-label="当前工作进展" className="min-w-0 border-t border-[#eaded1] py-3 text-xs leading-5 text-[#52766d] [overflow-wrap:anywhere]">
    <div className="flex min-w-0 items-center justify-between gap-2">
      <span role={view.waitsForCustomer ? 'status' : undefined} className="font-semibold text-[#294a40]">{view.waitsForCustomer ? '待你补充' : '当前工作进展'}</span>
      <div className="flex shrink-0 items-center gap-1">
        <button type="button" aria-label="查看过程" title={view.lines.length > lines.length ? `查看过程，另有 ${view.lines.length - lines.length} 项进展` : '查看过程'} className={buttonClass} onClick={props.onInspect}><ListTree size={16} aria-hidden="true" /></button>
        <button type="button" aria-label="刷新状态" title="刷新状态（仅查询）" className={buttonClass} onClick={props.onRefresh}><RefreshCw size={16} aria-hidden="true" /></button>
        {!view.waitsForCustomer && <button type="button" aria-label="暂停后续执行" title="暂停后续执行；在途调用仍可能返回" className={buttonClass} disabled={!canPause}
          onClick={() => { if (canPause) props.onPause() }}><Pause size={16} aria-hidden="true" /></button>}
      </div>
    </div>
    {!view.waitsForCustomer && <div role="status" aria-live="polite" aria-atomic="true" className="space-y-1.5">
      {view.message && <p className="flex min-w-0 items-start gap-2 text-[#294a40]">
        {view.messageRole && !lines.some((line) => line.role === view.messageRole) && <span aria-hidden="true" className="flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden">{props.portrait(view.messageRole)}</span>}
        <span className="min-w-0">{view.message}</span>
      </p>}
      {lines.map((line) => <p key={line.id} className={`flex min-w-0 items-start gap-2 ${line.warning ? 'text-[#b55f51]' : ''}`}>
        {line.role ? <span aria-hidden="true" className="flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden">{props.portrait(line.role)}</span>
          : <Clock3 size={14} className="mt-1 shrink-0" aria-hidden="true" />}
        <span className="min-w-0 flex-1">{line.text}</span>
        {line.moving && <LoaderCircle size={14} aria-label="有近期执行反馈" className="mt-1 shrink-0 motion-safe:animate-spin" />}
      </p>)}
      {view.notice && <p className="flex items-start gap-2 text-[#b55f51]"><CircleAlert size={14} className="mt-1 shrink-0" aria-hidden="true" /><span className="min-w-0">{view.notice}</span></p>}
    </div>}
  </section>
}
