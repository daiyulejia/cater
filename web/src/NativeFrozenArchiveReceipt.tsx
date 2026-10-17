import { frozenArchiveReadEvent } from './conversationActivityState'
import { NativeArtifactReceipt } from './NativeArtifactReceipt'
import type { TeamEvent } from './team'

export function NativeFrozenArchiveReceipt({ event }: { event: TeamEvent }) {
  const adapted = frozenArchiveReadEvent(event)
  if (!adapted) return null
  const result = event.payload.result as Record<string, unknown>
  const source = result.source as Record<string, unknown>, snapshot = result.snapshot as Record<string, unknown>
  const binding = JSON.stringify([event.seq, source, snapshot, adapted.payload.arguments, adapted.payload.result])
  return <div key={binding} className="min-w-0 space-y-2 [overflow-wrap:anywhere]">
    <p>冻结历史参考 · 来源群 {source.task_id as string} · 原成果 #{source.seq as number} · {source.filename as string}</p>
    <p>冻结来源群：{snapshot.source_task_id as string} · 冻结截止 #{snapshot.source_seq as number} · {snapshot.captured_at as string}</p>
    <p>沿用确切历史版本，不代表本群重新发布成果，也不继承原验收结论。</p>
    <details>
      <summary className="cursor-pointer font-semibold">冻结来源指纹</summary>
      <p className="font-mono">冻结截点 SHA-256：{snapshot.source_hash as string}</p>
      <p className="font-mono">原发布事件 SHA-256：{source.event_hash as string}</p>
    </details>
    <NativeArtifactReceipt event={adapted} />
  </div>
}
