import * as Dialog from '@radix-ui/react-dialog'
import { Trash2 } from 'lucide-react'
import { useRef } from 'react'

export function DeleteConversationDialog({ title, busy, error, onClose, onConfirm }: {
  title: string; busy: boolean; error?: string; onClose: () => void; onConfirm: () => void
}) {
  const returnFocus = useRef<HTMLElement | null>(null)
  return <Dialog.Root open onOpenChange={open => { if (!open && !busy) onClose() }}>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-50 bg-[#183e35]/30" />
      <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100%_-_32px)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-[#e5d7ce] bg-[#fffdf9] p-6 text-sm leading-6 text-[#294a40] shadow-xl [overflow-wrap:anywhere]"
        onOpenAutoFocus={() => { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null }}
        onCloseAutoFocus={event => {
          event.preventDefault()
          requestAnimationFrame(() => {
            if (returnFocus.current?.isConnected) returnFocus.current.focus()
            else document.querySelector<HTMLTextAreaElement>('.composer-box textarea')?.focus()
          })
        }}>
        <Dialog.Title className="flex items-center gap-2 text-base font-semibold"><Trash2 size={18} aria-hidden="true" />删除这段对话？</Dialog.Title>
        <p className="my-3 max-h-24 overflow-y-auto font-semibold">{title}</p>
        <Dialog.Description className="text-xs leading-6 text-[#62786e]">对话将从列表移除，并取消后续工作。已发出的调用可能仍返回，磁盘工程文件不会被删除。聊天、成果与审计记录仍保留在本机数据库中。</Dialog.Description>
        {error && <p role="alert" className="mt-3 text-xs text-[#a94f43]">{error}</p>}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" disabled={busy} onClick={onClose} className="rounded-md border border-[#ccdcd3] px-4 py-2 text-xs disabled:opacity-50">保留对话</button>
          <button type="button" disabled={busy} onClick={onConfirm} className="inline-flex items-center gap-2 rounded-md bg-[#ad5145] px-4 py-2 text-xs font-semibold text-white disabled:opacity-50"><Trash2 size={14} aria-hidden="true" />{busy ? '正在删除' : '删除对话'}</button>
        </div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
