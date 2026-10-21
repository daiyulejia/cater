import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'

type Member = { id: string; name: string }
type Props = {
  id?: string; value: string; label: string; placeholder: string; members: Member[]
  onChange: (value: string) => void; onSubmit: () => void; portrait: (id: string) => ReactNode
}

export function MentionInput({ id, value, label, placeholder, members, onChange, onSubmit, portrait }: Props) {
  const input = useRef<HTMLTextAreaElement>(null)
  const listId = useId()
  const [selection, setSelection] = useState({ start: 0, end: 0 })
  const [focused, setFocused] = useState(false)
  const [composing, setComposing] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const [index, setIndex] = useState(0)
  const prefix = value.slice(0, selection.start)
  const match = selection.start === selection.end ? /@([^\s@]{0,40})$/u.exec(prefix) : null
  const start = match?.index ?? 0
  const isEmail = start > 0 && /[A-Za-z0-9._%+-]/.test(value[start - 1])
  const visible = focused && !composing && !dismissed && Boolean(match) && !isEmail
  const candidates = members.filter((member) => member.name.toLocaleLowerCase().includes((match?.[1] ?? '').toLocaleLowerCase())).slice(0, 8)
  const active = Math.min(index, Math.max(0, candidates.length - 1))

  useEffect(() => {
    if (visible) document.getElementById(`${listId}-${active}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [visible, active, listId])

  const readSelection = (target: HTMLTextAreaElement) => {
    setSelection({ start: target.selectionStart, end: target.selectionEnd })
  }
  const choose = (member: Member) => {
    if (!match) return
    const before = value.slice(0, start)
    const separator = before && /[\p{L}\p{N}_-]$/u.test(before) ? ' ' : ''
    const insertion = `${before}${separator}@${member.name} `
    onChange(insertion + value.slice(selection.end))
    setDismissed(true)
    requestAnimationFrame(() => {
      input.current?.focus()
      input.current?.setSelectionRange(insertion.length, insertion.length)
    })
  }

  return <div className="relative min-w-0 flex-1">
    {visible && <div className="mention-menu absolute bottom-full left-0 z-20 mb-3 w-full">
      <p className="border-b border-[#dfebe5] px-3 py-2 text-[11px] font-semibold text-[#8e604c]">点名伙伴</p>
      <div id={listId} role="listbox" aria-label="点名建议" className="max-h-[min(300px,40dvh)] overflow-y-auto p-1">
        {candidates.map((member, option) => <button key={member.id} id={`${listId}-${option}`} type="button" tabIndex={-1} role="option" aria-label={member.name} aria-selected={option === active} className={`mention-option flex w-full items-center gap-2 px-2 py-1.5 text-left ${option === active ? 'is-active' : ''}`} onMouseDown={(event) => event.preventDefault()} onClick={() => choose(member)}>
          {portrait(member.id)}<span className="min-w-0 break-words text-xs font-semibold">{member.name}</span>
        </button>)}
        {!candidates.length && <p className="px-3 py-3 text-xs text-[#87988c]">没有匹配的伙伴</p>}
      </div>
    </div>}
    <textarea ref={input} id={id} value={value} aria-label={label} placeholder={placeholder} rows={2} aria-autocomplete="list" aria-controls={visible ? listId : undefined} aria-activedescendant={visible && candidates.length ? `${listId}-${active}` : undefined}
      className="block min-h-12 w-full resize-none border-0 bg-transparent px-2 py-1 text-sm leading-6 outline-none placeholder:text-[#a8958d]"
      onFocus={(event) => { setFocused(true); readSelection(event.currentTarget) }}
      onBlur={(event) => { if (!event.relatedTarget || !event.currentTarget.parentElement?.contains(event.relatedTarget)) setFocused(false) }}
      onChange={(event) => { onChange(event.target.value); readSelection(event.target); setDismissed(false); setIndex(0) }}
      onSelect={(event) => readSelection(event.currentTarget)}
      onCompositionStart={() => setComposing(true)} onCompositionEnd={(event) => { setComposing(false); readSelection(event.currentTarget) }}
      onKeyDown={(event) => {
        if (composing || event.nativeEvent.isComposing || event.keyCode === 229) return
        if (event.key === 'Enter' && !event.shiftKey) {
          event.preventDefault()
          if (event.repeat) return
          if (visible && candidates.length) choose(candidates[active])
          else onSubmit()
          return
        }
        if (!visible) return
        if (event.key === 'Escape') { event.preventDefault(); setDismissed(true) }
        else if (candidates.length && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { event.preventDefault(); setIndex((active + (event.key === 'ArrowDown' ? 1 : -1) + candidates.length) % candidates.length) }
      }} />
  </div>
}
