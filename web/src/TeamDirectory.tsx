import { useRef } from 'react'
import type { ReactNode } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { AtSign, Cat, RefreshCw, X } from 'lucide-react'

export type TeamAvailability = { checked_at: string; all_available: boolean; members: { role_id: string; status: string; reason: string }[] }
type Member = { id: string; name: string }
const labels: Record<string, string> = { available: '营业', demo: '演示中', on_leave: '休假', unverified: '待确认' }
const reasons: Record<string, string> = { local_policy: '本地调度就绪', local_demo: '本地演示，未调用真实模型', budget_exhausted: 'Cater 的已报告用量预算已耗尽', invalid_budget: '预算配置需要修正', missing_key: '尚未提供模型凭据', not_configured: '尚未配置连接', circuit_open: '连接连续失败，已暂停调用', recent_failure: '最近一次调用未成功', recent_success: '近五分钟内调用成功', not_recently_checked: '尚无近期调用证据' }

function teamSummary(availability?: TeamAvailability): string {
  if (!availability?.members.length) return '状态待确认'
  if (availability.all_available && availability.members.every((item) => item.status === 'available')) return '全员营业'
  const leave = availability.members.filter((item) => item.status === 'on_leave').length
  if (leave) return `${leave} 位休假`
  if (availability.members.some((item) => item.status === 'unverified')) return '状态待确认'
  return availability.members.some((item) => item.status === 'demo') ? '演示营业' : '状态待确认'
}

export function TeamDirectory({ members, availability, open, onOpenChange, refreshing, error, onRefresh, onMention, portrait }: {
  members: Member[]; availability?: TeamAvailability; open: boolean; onOpenChange: (open: boolean) => void
  refreshing: boolean; error: string; onRefresh: () => void; onMention: (name: string) => void; portrait: (id: string) => ReactNode
}) {
  const mentionChosen = useRef(false)
  const openingFocus = useRef<HTMLElement | null>(null)
  const states = new Map(availability?.members.map((member) => [member.role_id, member]) ?? [])
  const unique = [...new Map(members.map((member) => [member.id, member])).values()]
  return <Dialog.Root open={open} onOpenChange={(next) => { onOpenChange(next); if (next) onRefresh() }}>
    <Dialog.Trigger asChild><button id="team-directory-trigger" className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-[#a45c48] hover:bg-[#fff0e5]" aria-label="今天在店里" title={`今天在店里 · ${teamSummary(availability)}`}><Cat size={18} /><span className="hidden text-[11px] font-semibold sm:inline">{teamSummary(availability)}</span></button></Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="artifact-overlay" />
      <Dialog.Content className="team-directory flex flex-col" onOpenAutoFocus={() => { openingFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null }} onCloseAutoFocus={(event) => {
        if (mentionChosen.current) { event.preventDefault(); mentionChosen.current = false; document.querySelector<HTMLTextAreaElement>('textarea')?.focus() }
        else if (openingFocus.current?.isConnected && openingFocus.current.getClientRects().length) { event.preventDefault(); openingFocus.current.focus() }
      }}>
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[#d3e3d9] px-5 py-5 sm:px-8">
          <div className="min-w-0"><p className="text-[11px] font-semibold text-[#ac6650]">Cater · 伙伴簿</p><Dialog.Title className="mt-1 text-xl font-bold text-[#305749]">今天在店里</Dialog.Title><Dialog.Description className="mt-2 text-xs leading-5 text-[#6e8778]">{teamSummary(availability)}{availability && <> · 最近读取 {new Date(availability.checked_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}</>}</Dialog.Description></div>
          <div className="flex shrink-0 gap-1"><button onClick={onRefresh} disabled={refreshing} className="artifact-tool" aria-label="刷新伙伴状态" title="刷新伙伴状态"><RefreshCw size={18} className={refreshing ? 'motion-safe:animate-spin' : ''} /></button><Dialog.Close asChild><button className="artifact-tool" aria-label="关闭伙伴页" title="回到聊天"><X size={20} /></button></Dialog.Close></div>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-8">
          {error && <p role="alert" className="mb-4 text-xs text-rose-700">{error}</p>}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {unique.map((member) => {
              const state = states.get(member.id)
              const status = state?.status ?? 'unverified'
              return <article key={member.id} className={`team-member team-member-${status} flex min-w-0 flex-col items-center px-3 py-4 text-center`}>
                {portrait(member.id)}<h2 className="mt-3 w-full break-words text-sm font-bold">{member.name}</h2>
                <span className={`team-state mt-1 text-[11px] font-semibold team-state-${status}`}>{labels[status] ?? '待确认'}</span>
                <p className="mt-2 min-h-10 text-[11px] leading-5 text-[#829080]">{reasons[state?.reason ?? 'not_recently_checked'] ?? '尚无可确认的状态'}</p>
                <button type="button" onClick={() => { mentionChosen.current = true; onMention(member.name); onOpenChange(false) }} className="mt-3 inline-flex min-w-0 max-w-full items-center gap-1 text-xs font-semibold text-[#a8624a] hover:text-[#7c4033]" aria-label={`点名 ${member.name}`} title={`点名 ${member.name}`}><AtSign size={14} className="shrink-0" /><span className="min-w-0 truncate">{member.name}</span></button>
              </article>
            })}
          </div>
          {!unique.length && <p className="py-10 text-center text-sm text-[#829080]">正在读取伙伴名单。</p>}
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
