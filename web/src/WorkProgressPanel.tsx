import { useMemo } from 'react'
import { workProgress } from './workProgress'
import type { WorkAssignment } from './collaborationProjection'

export function WorkAssignments({ items }: { items: WorkAssignment[] }) {
  return <>{items.map((assignment, index) => <div key={index} className="py-1.5">
    <p>{assignment.objective}，交给 <strong>@{assignment.owner}</strong>。</p>
    {assignment.reviewer && <p className="text-xs opacity-75">提交后由 @{assignment.reviewer} 复核。</p>}
  </div>)}</>
}

export function WorkProgressPanel({ events, claim, name }: { events: Parameters<typeof workProgress>[0]; claim: number; name: string }) {
  const progress = useMemo(() => workProgress(events).get(claim), [events, claim])
  if (!progress) return null
  return <details className="work-progress mt-3 border-t border-[#dfeae5] pt-2 text-xs text-[#52766d]" open={progress.state === '处理中'}>
    <summary className="cursor-pointer font-semibold">工作过程 · {progress.state}</summary>
    <div className="mt-2 max-h-56 overflow-y-auto break-words pr-2" aria-label={`${name}的工作过程`}>
      <p className="mb-3 leading-5">{progress.objective}</p>
      <ol className="space-y-2 border-l border-[#c7d8ce] pl-3">{progress.steps.map((step) => <li key={step.seq} className="leading-5">{step.text}</li>)}</ol>
    </div>
  </details>
}
