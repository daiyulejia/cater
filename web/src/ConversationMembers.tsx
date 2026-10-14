import { useRef, useState } from 'react'
import type { ReactNode } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { ArrowUpRight, AtSign, Cat, ChevronRight, X } from 'lucide-react'

export type ConversationMembersProps = {
  participants: string[]
  members: Array<{ id: string; name: string }>
  portrait: (id: string, size: 'sm' | 'md' | 'lg' | 'xl') => ReactNode
  onMention: (id: string) => void
  onOpenDirectory: () => void
}

export function ConversationMembers({ participants, members, portrait, onMention, onOpenDirectory }: ConversationMembersProps) {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const action = useRef<{ kind: 'mention'; id: string } | { kind: 'directory' } | null>(null)
  const team = new Map<string, { id: string; name: string }>()
  for (const member of members) if (!team.has(member.id)) team.set(member.id, member)
  const ids = [...new Set(participants.filter((id) => id.trim().length > 0))]
  const preview = ids.filter((id) => team.has(id)).slice(0, 3)

  return <Dialog.Root open={open} onOpenChange={(next) => {
    if (next) action.current = null
    setOpen(next)
  }}>
    <Dialog.Trigger asChild>
      <button ref={trigger} type="button" aria-label={`本次伙伴，${ids.length} 位`} title="查看本次伙伴" className="inline-flex min-h-11 max-w-full items-center gap-2 rounded-md px-2 text-[#45665c] hover:bg-[#fff0e8]">
        <Cat size={17} aria-hidden="true" className="shrink-0 text-[#a8624a]" />
        {preview.length > 0 && <span aria-hidden="true" className="flex shrink-0 -space-x-2">{preview.map((id) => <span key={id} className="flex size-8 shrink-0">{portrait(id, 'sm')}</span>)}</span>}
        <span className="min-w-0 truncate text-xs font-semibold">本次伙伴 <span className="font-normal text-[#748b81]">· {ids.length}</span></span>
        <ChevronRight size={14} aria-hidden="true" className="shrink-0 text-[#748b81]" />
      </button>
    </Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-[60] bg-[rgba(34,53,45,.32)]" />
      <Dialog.Content className="fixed left-1/2 top-1/2 z-[61] flex max-h-[calc(100dvh-32px)] w-[calc(100vw-32px)] max-w-sm -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-lg border-t-[3px] border-[#c9745a] bg-[#fffcf8] text-[#2d493e] shadow-[0_16px_64px_rgba(39,56,44,.24)]" onCloseAutoFocus={(event) => {
        const chosen = action.current
        action.current = null
        if (!chosen) return
        event.preventDefault()
        // Hand off actions after Radix releases the modal's focus scope.
        if (chosen.kind === 'directory') {
          trigger.current?.focus()
          onOpenDirectory()
          return
        }
        onMention(chosen.id)
        const textareas = document.querySelectorAll<HTMLTextAreaElement>('textarea:not(:disabled)')
        const composer = trigger.current?.closest('section')?.querySelector<HTMLTextAreaElement>('textarea:not(:disabled)')
          ?? document.querySelector<HTMLTextAreaElement>('.composer-box textarea:not(:disabled)')
          ?? (textareas.length === 1 ? textareas[0] : null)
        if (composer) composer.focus()
        else trigger.current?.focus()
      }}>
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[#dce7e0] px-4 py-3">
          <div className="min-w-0">
            <Dialog.Title className="text-sm font-bold">本次伙伴</Dialog.Title>
            <Dialog.Description className="mt-1 text-xs text-[#748b81]">{ids.length} 位参与者</Dialog.Description>
          </div>
          <Dialog.Close asChild><button type="button" aria-label="关闭本次伙伴" title="关闭本次伙伴" className="grid size-9 shrink-0 place-items-center rounded-md text-[#6e8778] hover:bg-[#edf3ef]"><X size={18} aria-hidden="true" /></button></Dialog.Close>
        </header>
        <div className="min-h-0 overflow-y-auto px-4">
          {ids.length ? <ul aria-label="当前对话成员" className="divide-y divide-[#e4e9e2]">
            {ids.map((id) => {
              const member = team.get(id)
              return <li key={id} className="flex min-w-0 items-center gap-3 py-3">
                <span aria-hidden="true" className="flex size-8 shrink-0 items-center justify-center">{member ? portrait(id, 'sm') : <Cat size={19} className="text-[#8b9b93]" />}</span>
                <div className="min-w-0 flex-1"><span className="block break-words text-sm font-semibold [overflow-wrap:anywhere]">{member?.name || id}</span>{!member && <span className="mt-0.5 block text-[11px] text-[#829080]">身份待确认</span>}</div>
                {member && <button type="button" aria-label={`点名 ${member.name || id}`} title={`点名 ${member.name || id}`} className="grid size-9 shrink-0 place-items-center rounded-md text-[#a8624a] hover:bg-[#fff0e8]" onClick={() => { action.current = { kind: 'mention', id }; setOpen(false) }}><AtSign size={17} aria-hidden="true" /></button>}
              </li>
            })}
          </ul> : <p className="py-6 text-center text-xs text-[#748b81]">暂无对话成员</p>}
        </div>
        <footer className="shrink-0 border-t border-[#dce7e0] px-4 py-2">
          <button type="button" className="flex min-h-10 w-full items-center justify-between gap-2 rounded-md px-1 text-xs font-semibold text-[#52766d] hover:bg-[#edf3ef]" onClick={() => { action.current = { kind: 'directory' }; setOpen(false) }}>猫咖伙伴簿<ArrowUpRight size={16} aria-hidden="true" /></button>
        </footer>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
