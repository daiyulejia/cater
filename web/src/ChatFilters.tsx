import { useId, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { ListFilter, X } from 'lucide-react'

export type ChatFilter = 'all' | 'collaboration' | 'decision' | 'deliverable'

const filters = [
  { id: 'all', label: '全部' },
  { id: 'collaboration', label: '协作' },
  { id: 'decision', label: '决策' },
  { id: 'deliverable', label: '成果' },
] as const

export function ChatFilters({ value, onChange }: { value: ChatFilter; onChange: (value: ChatFilter) => void }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  const desktopSelection = useRef<HTMLButtonElement>(null)
  const currentLabel = filters.find((filter) => filter.id === value)!.label
  const choose = (next: ChatFilter) => {
    onChange(next)
    setOpen(false)
  }

  return <>
    <div className="hidden gap-1 overflow-x-auto sm:flex" role="group" aria-label="对话筛选">
      {filters.map((filter) => <button key={filter.id} ref={value === filter.id ? desktopSelection : undefined} type="button" onClick={() => onChange(filter.id)} aria-pressed={value === filter.id} className={`filter-button shrink-0 px-3 py-1.5 text-xs font-semibold ${value === filter.id ? 'is-active' : ''}`}>{filter.label}</button>)}
    </div>
    <Dialog.Root open={open} onOpenChange={setOpen}>
      {/* Keep responsive visibility outside the unlayered origin-tool display rule. */}
      <span className="inline-flex shrink-0 sm:hidden">
        <Dialog.Trigger asChild>
          <button type="button" className="origin-tool relative sm:hidden" aria-label={`筛选对话：${currentLabel}`} title={`筛选对话：${currentLabel}`}>
            <ListFilter size={17} aria-hidden="true" />
            {value !== 'all' && <span aria-hidden="true" className="absolute right-1 top-1 size-1.5 rounded-full bg-[#c9745a]" />}
          </button>
        </Dialog.Trigger>
      </span>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[60] bg-[rgba(34,53,45,.32)]" />
        <Dialog.Content aria-describedby={undefined} onCloseAutoFocus={event => {
          if (desktopSelection.current?.getClientRects().length) {
            event.preventDefault()
            desktopSelection.current.focus()
          }
        }} className="fixed left-1/2 top-1/2 z-[61] max-h-[calc(100dvh-32px)] w-[calc(100vw-32px)] max-w-xs -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border-t-[3px] border-[#c9745a] bg-[#fffcf8] p-4 text-[#2d493e] shadow-xl">
          <header className="mb-3 flex items-center justify-between gap-3">
            <Dialog.Title className="text-sm font-bold">筛选对话</Dialog.Title>
            <Dialog.Close asChild><button type="button" className="origin-tool" aria-label="关闭筛选对话" title="关闭筛选对话"><X size={18} aria-hidden="true" /></button></Dialog.Close>
          </header>
          <div role="radiogroup" aria-label="对话筛选">
            {filters.map((filter) => <label key={filter.id} htmlFor={`${id}-${filter.id}`} className="flex cursor-pointer items-center gap-3 rounded-md px-3 py-3 text-sm hover:bg-[#f6ede5]">
              <input id={`${id}-${filter.id}`} name={`${id}-filter`} type="radio" value={filter.id} checked={value === filter.id} className="size-4 shrink-0 accent-[#34786e]" onChange={() => choose(filter.id)} onClick={() => {
                // A checked radio does not emit change, but activating it still closes the dialog.
                if (value === filter.id) choose(filter.id)
              }} />
              <span>{filter.label}</span>
            </label>)}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </>
}
