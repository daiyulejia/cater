import { useEffect, useId, useRef, useState } from 'react'
import { LoaderCircle, MoreHorizontal, Pause, Play, RefreshCw, Square } from 'lucide-react'
import { controlLabels } from './executionControl'
import type { ControlAction, ExecutionControl } from './executionControl'

export function TaskControlMenu({ state, busy, disabled, retry, onAction }: {
  state: ExecutionControl['state']; busy: boolean; disabled: boolean; retry?: ControlAction; onAction: (action: ControlAction) => void
}) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [open])
  const action = state === 'paused' ? 'resume' : 'pause'
  const choose = (next: ControlAction) => { setOpen(false); trigger.current?.focus(); onAction(next) }
  const buttonClass = 'flex min-h-9 w-full items-center gap-2 rounded-md px-3 py-2 text-left text-xs hover:bg-[#e7f0ec] disabled:opacity-45'
  return <div ref={root} className="relative" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false) }}>
    <button ref={trigger} type="button" className="origin-tool" aria-label="更多任务操作" title="更多任务操作" aria-expanded={open} aria-controls={menuId} onClick={() => setOpen(!open)}>
      {busy ? <LoaderCircle size={17} className="motion-safe:animate-spin" aria-hidden="true" /> : <MoreHorizontal size={17} aria-hidden="true" />}
    </button>
    {open && <div id={menuId} role="group" aria-label="任务操作" className="absolute right-0 top-full z-40 mt-1 w-52 max-w-[calc(100vw-32px)] rounded-md border border-[#d3dfd9] bg-white p-1 text-[#34786e] shadow-md">
      <button type="button" className={buttonClass} disabled={disabled || busy || state === 'cancelled'} onClick={() => choose(action)} title={action === 'resume' ? '恢复正常后台协作' : '阻止后续执行；在途调用仍可能返回'}>
        {action === 'resume' ? <Play size={16} aria-hidden="true" /> : <Pause size={16} aria-hidden="true" />}{controlLabels[action]}
      </button>
      <button type="button" className={buttonClass} disabled={disabled || busy || state === 'cancelled'} onClick={() => choose('cancel')} title="取消后不可恢复，需新建任务；在途调用仍可能返回">
        <Square size={16} aria-hidden="true" />{controlLabels.cancel}
      </button>
      {retry && <button type="button" className={buttonClass} disabled={disabled || busy || (state === 'cancelled' && retry !== 'cancel')} onClick={() => choose(retry)} title="沿用原请求标识核对提交结果">
        <RefreshCw size={16} aria-hidden="true" />重试{controlLabels[retry]}
      </button>}
    </div>}
  </div>
}
