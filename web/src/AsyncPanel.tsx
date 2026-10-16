import { Component, isValidElement, Suspense, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { RefreshCw } from 'lucide-react'

import { ReloadContext } from './pageReloadContext'

function LoadFailure() {
  const reload = useContext(ReloadContext)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const alive = useRef(true)
  const pending = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const refresh = async () => {
    if (pending.current) return
    pending.current = true
    setBusy(true)
    try {
      const result = reload ? await reload() : '当前无法保存草稿，尚未刷新。'
      if (alive.current) setNotice(result)
    } catch {
      if (alive.current) setNotice('未能保存草稿，尚未刷新。')
    } finally {
      pending.current = false
      if (alive.current) setBusy(false)
    }
  }
  return <section role="alert" className="my-2 min-w-0 border-l-2 border-[#bd735c] bg-[#fff5ef] px-3 py-2 text-xs leading-5 text-[#814e40] [overflow-wrap:anywhere]">
    <p className="font-semibold">这部分暂时打不开</p>
    <p>聊天和输入仍保留，后台任务状态未因此改变。</p>
    <button type="button" disabled={busy} aria-busy={busy} onClick={() => void refresh()} title="保留草稿并刷新页面" className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-[#dec8b8] bg-white px-2 py-1 disabled:opacity-60">
      <RefreshCw size={14} aria-hidden="true" className={busy ? 'motion-safe:animate-spin' : ''} />{busy ? '正在保留草稿…' : '保留草稿并刷新'}
    </button>
    {notice && <p className="mt-1" role="status">{notice}</p>}
  </section>
}

class PanelBoundary extends Component<{ children: ReactNode; onFailure?: () => void }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch() { this.props.onFailure?.() }
  render() { return this.state.failed ? <LoadFailure /> : this.props.children }
}

export function AsyncPanel({ children, fallback, onFailure }: { children: ReactNode; fallback: ReactNode; onFailure?: () => void }) {
  return <PanelBoundary key={isValidElement(children) ? children.key : undefined} onFailure={onFailure}><Suspense fallback={fallback}>{children}</Suspense></PanelBoundary>
}
