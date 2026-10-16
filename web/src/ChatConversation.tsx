import { lazy, Suspense } from 'react'
import type { ReactNode } from 'react'
import type { ChatItem } from './App'
import { groupTeamConversation, isTeamHost } from './team'
import type { TeamEvent } from './team'

const TeamToolProgress = lazy(() => import('./TeamProgressPanel').then((module) => ({ default: module.TeamToolProgress })))

export function ChatConversation({ items, events, names, renderItem }: {
  items: ChatItem[]; events: TeamEvent[]; names: Record<string, string>; renderItem: (item: ChatItem) => ReactNode
}) {
  return groupTeamConversation(items).map((group) => {
    const item = group.items[0]
    if (item.historicalDelivery) return <details key={`delivery:${item.id}`} className="delivery-history min-w-0 border-l-2 border-[#dce8e1] pl-3">
      <summary className="cursor-pointer text-xs font-semibold leading-5 text-[#52766d]">店长交付记录</summary>
      <div className="mt-4">{renderItem(item)}</div>
    </details>
    if (!group.internal) return renderItem(item)
    const sources = new Set(group.items.filter((entry) => entry.evidence.type === 'team.message_received').map((entry) => entry.id))
    const runs = new Set([
      ...group.items.map((entry) => entry.evidence.payload.run_id).filter((id): id is string => typeof id === 'string'),
      ...events.filter((event) => isTeamHost(event) && event.type === 'team.run_started'
        && typeof event.payload.message_seq === 'number' && sources.has(event.payload.message_seq)).map((event) => event.payload.run_id),
    ])
    const tools = events.filter((event) => typeof event.payload.run_id === 'string' && runs.has(event.payload.run_id))
    return <details key={`team:${item.id}`} className="team-collaboration">
      <summary>团队协作 · {group.items.length} 条</summary>
      <div className="mt-4 space-y-4">{group.items.map(renderItem)}
        <Suspense fallback={null}><TeamToolProgress events={tools} names={names} full /></Suspense>
      </div>
    </details>
  })
}
