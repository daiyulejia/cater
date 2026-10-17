import { useRef } from 'react'
import type { ReactNode, RefObject } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import './TaskGraphDialog.css'

export function TaskGraphDialog({ open, onOpenChange, returnFocusRef, description, children }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  returnFocusRef?: RefObject<HTMLElement | null>
  description: string
  children: ReactNode
}) {
  const openingFocus = useRef<HTMLElement | null>(null)
  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal><Dialog.Overlay className="task-graph-overlay" /><Dialog.Content className="task-graph-dialog"
      onOpenAutoFocus={() => { openingFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null }}
      onCloseAutoFocus={event => {
        event.preventDefault()
        const target = returnFocusRef?.current ?? openingFocus.current
        if (target?.isConnected) target.focus()
      }}>
      <header className="task-graph-header"><div><Dialog.Title>任务脉络</Dialog.Title><Dialog.Description className="task-graph-sr-only">{description}</Dialog.Description></div>
        <Dialog.Close asChild><button type="button" className="task-graph-close" aria-label="关闭任务脉络" title="关闭任务脉络"><X size={20} /></button></Dialog.Close>
      </header>
      {children}
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>
}
