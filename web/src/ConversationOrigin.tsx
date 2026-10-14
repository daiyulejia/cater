import { useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { ArrowUpRight, GitFork, History, X } from 'lucide-react'
import './ConversationOrigin.css'

export type ConversationBackground = {
  version: 1
  source_task_id: string
  source_goal: string
  source_seq: number
  source_hash: string
  captured_at: string
  sources: { task_id: string; id: string; seq: number; text: string }[]
  task_rules?: { id: string; text: string; revision: number; seq: number }[]
  artifacts: {
    task_id: string; seq: number; title: string; summary: string
    content?: string; format?: string; filename?: string; content_sha256?: string; verification?: string
  }[]
  omitted_artifacts: number
}

export function DerivationDraftBar({ goal, onCancel }: { goal: string; onCancel: () => void }) {
  return <div className="derivation-draft-bar" role="region" aria-label="新任务草稿来源">
    <GitFork size={16} aria-hidden="true" />
    <span className="derivation-draft-label"><strong>新任务草稿</strong><span title={goal}>基于：{goal}</span></span>
    <button type="button" onClick={onCancel} className="origin-tool" aria-label="取消新任务草稿" title="取消，返回原任务草稿"><X size={17} /></button>
  </div>
}

export function ConversationOrigin({ background, onNavigate }: { background: ConversationBackground; onNavigate: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const navigateOnClose = useRef(false)
  const captured = new Date(background.captured_at)
  const capturedLabel = Number.isNaN(captured.getTime()) ? background.captured_at : captured.toLocaleString('zh-CN', { hour12: false })

  return <Dialog.Root open={open} onOpenChange={(next) => { if (next) navigateOnClose.current = false; setOpen(next) }}>
    <Dialog.Trigger asChild><button type="button" className="origin-trigger" title="查看创建时的来源背景" aria-label="查看对话来源"><History size={15} aria-hidden="true" /><span>来源</span></button></Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="origin-overlay" />
      <Dialog.Content className="origin-dialog" onCloseAutoFocus={(event) => {
        if (!navigateOnClose.current) return
        navigateOnClose.current = false
        event.preventDefault()
        onNavigate(background.source_task_id)
      }}>
        <header className="origin-header">
          <div>
            <Dialog.Title>对话来源</Dialog.Title>
            <Dialog.Description>仅在创建时继承背景，之后两段对话各自独立。未复制验收结论，成果需在新任务中重新验收。任务店规已独立复制，项目通用店规仍适用。</Dialog.Description>
          </div>
          <Dialog.Close asChild><button type="button" className="origin-tool" aria-label="关闭对话来源" title="回到聊天"><X size={18} /></button></Dialog.Close>
        </header>
        <div className="origin-body">
          <section aria-label="来源快照">
            <h3>当时的目标</h3>
            <p className="origin-text">{background.source_goal}</p>
            <p className="origin-meta">创建时快照 · <time dateTime={background.captured_at}>{capturedLabel}</time></p>
            <p className="origin-meta">来源任务 {background.source_task_id} · 截至事件 #{background.source_seq}</p>
          </section>
          <section aria-label="来源文本">
            <h3>当时的文本 · {background.sources.length}</h3>
            {background.sources.length ? <ul>{background.sources.map((source) => <li key={`${source.task_id}:${source.id}`}>
              <p className="origin-meta">{source.task_id} · 事件 #{source.seq}</p>
              <p className="origin-text">{source.text}</p>
            </li>)}</ul> : <p className="origin-meta">没有复制的来源文本。</p>}
          </section>
          {Boolean(background.task_rules?.length) && <section aria-label="来源任务店规">
            <h3>当时的任务店规 · {background.task_rules?.length}</h3>
            <ul>{background.task_rules?.map((rule) => <li key={rule.id}>
              <p className="origin-text">{rule.text}</p>
              <p className="origin-meta">版本 {rule.revision} · 事件 #{rule.seq}</p>
            </li>)}</ul>
          </section>}
          <section aria-label="来源成果">
            <h3>当时的成果 · {background.artifacts.length}</h3>
            <p className="origin-meta">未复制成果：{background.omitted_artifacts} 项</p>
            {background.artifacts.length ? <ul>{background.artifacts.map((artifact) => <li key={`${artifact.task_id}:${artifact.seq}`}>
              <h4>{artifact.title}</h4>
              <p className="origin-verification">{artifact.verification === 'accepted_at_capture' ? '来源阶段当时通过（新任务待验收）' : artifact.verification === 'unverified' ? '来源未验证' : '待核对'}</p>
              <p className="origin-text">{artifact.summary}</p>
              <p className="origin-meta">{artifact.task_id} · 事件 #{artifact.seq}{artifact.filename ? ` · ${artifact.filename}` : ''}</p>
              {typeof artifact.content === 'string' && artifact.content.length > 0
                ? <details><summary>查看成果正文</summary><pre className="origin-text">{artifact.content}</pre></details>
                : <p className="origin-meta">仅保留摘要，未复制正文。</p>}
            </li>)}</ul> : <p className="origin-meta">没有复制的来源成果。</p>}
          </section>
        </div>
        <footer className="origin-footer"><button type="button" className="origin-jump" onClick={() => { navigateOnClose.current = true; setOpen(false) }}><ArrowUpRight size={16} aria-hidden="true" />前往原对话</button></footer>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
