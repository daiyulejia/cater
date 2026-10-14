import { useEffect } from 'react'
import type { ReactNode } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Coffee, X } from 'lucide-react'

export function WorkboardPanel({ open, onOpenChange, children }: { open: boolean; onOpenChange: (open: boolean) => void; children: ReactNode }) {
  useEffect(() => {
    const desktop = window.matchMedia('(min-width: 1024px)')
    const closeOnDesktop = () => { if (desktop.matches) onOpenChange(false) }
    closeOnDesktop()
    desktop.addEventListener('change', closeOnDesktop)
    return () => desktop.removeEventListener('change', closeOnDesktop)
  }, [onOpenChange])

  return <>
    <aside className="workboard-panel hidden min-h-0 overflow-y-auto p-5 lg:block" aria-label="成果与过程">
      <div className="board-heading mb-5"><p className="flex items-center gap-1.5 text-[11px] font-semibold"><Coffee size={14} /> 猫咖记事板</p><h2 className="mt-1 text-[16px] font-bold">成果与过程</h2></div>
      {children}
    </aside>
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="workboard-overlay" />
        <Dialog.Content className="workboard-dialog flex flex-col" onCloseAutoFocus={(event) => {
          const trigger = document.getElementById('workboard-trigger')
          if (trigger && trigger.getClientRects().length) { event.preventDefault(); trigger.focus() }
        }}>
          <header className="board-heading !m-0 flex shrink-0 items-center justify-between gap-3">
            <div className="min-w-0"><p className="flex items-center gap-1.5 text-[11px] font-semibold"><Coffee size={14} /> 猫咖记事板</p><Dialog.Title className="mt-1 text-base font-bold text-[#fffaf0]">成果与过程</Dialog.Title><Dialog.Description className="sr-only">当前任务的成果、参与伙伴与可检查的过程记录</Dialog.Description></div>
            <Dialog.Close asChild><button className="grid size-9 shrink-0 place-items-center rounded-md border border-[#789b8e] text-white hover:bg-[#456f60]" aria-label="关闭成果面板" title="回到聊天"><X size={18} /></button></Dialog.Close>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto p-5">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </>
}
