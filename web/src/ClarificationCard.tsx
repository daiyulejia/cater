import { useSyncExternalStore } from 'react'
import { Check, LoaderCircle, SkipForward } from 'lucide-react'
import './clarification.css'

export type Clarification = {
  seq: number; role_id: string; question: string; options: string[]
  status: 'pending' | 'answered' | 'stale'
  response?: { kind: 'choice' | 'custom' | 'skip'; option_index?: number; answer: string }
}
export type ClarificationReply = { question_seq: number; kind: 'choice' | 'custom' | 'skip'; option_index?: number; answer?: string; request_id: string }
type Draft = { mode: 'choice' | 'custom'; option?: number; custom: string; busy?: boolean; sent?: boolean; error?: string; attempt?: ClarificationReply }
type Session = { getSnapshot: () => Draft; subscribe: (listener: () => void) => () => void; update: (patch: Partial<Draft>) => void }
export type ClarificationStore = Map<string, Session>
export type ClarificationControls = {
  store: ClarificationStore
  disabledReason?: string
  createRequestId: () => string
  submit: (taskId: string, reply: ClarificationReply) => Promise<void>
}

function sessionFor(store: ClarificationStore, key: string, mode: Draft['mode']): Session {
  const existing = store.get(key)
  if (existing) return existing
  let draft: Draft = { mode, custom: '' }
  const listeners = new Set<() => void>()
  // Subscribers let a remounted question observe an in-flight response after navigation.
  const session: Session = {
    getSnapshot: () => draft,
    subscribe: (listener) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    update: (patch) => { draft = { ...draft, ...patch }; listeners.forEach((listener) => listener()) },
  }
  store.set(key, session)
  return session
}

export function ClarificationCard({ taskId, question, controls }: { taskId: string; question: Clarification; controls: ClarificationControls }) {
  const key = JSON.stringify([taskId, question.seq])
  const entry = sessionFor(controls.store, key, question.options.length ? 'choice' : 'custom')
  const draft = useSyncExternalStore(entry.subscribe, entry.getSnapshot)
  const update = entry.update
  const edit = (patch: Partial<Draft>) => {
    const current = entry.getSnapshot()
    if (current.busy || !Object.entries(patch).some(([key, value]) => current[key as keyof Draft] !== value)) return
    update({ ...patch, error: '', attempt: undefined })
  }
  const respond = async (kind: ClarificationReply['kind']) => {
    const current = entry.getSnapshot()
    if (controls.disabledReason || question.status !== 'pending' || current.busy || current.sent) return
    if ((kind === 'choice' && current.option === undefined) || (kind === 'custom' && !current.custom.trim())) return
    const reply: ClarificationReply = { question_seq: question.seq, kind,
      ...(kind === 'choice' ? { option_index: current.option } : kind === 'custom' ? { answer: current.custom.trim() } : {}),
      request_id: current.attempt?.kind === kind ? current.attempt.request_id : controls.createRequestId() }
    update({ busy: true, error: '', attempt: reply })
    try {
      await controls.submit(taskId, reply)
      update({ sent: true })
    } catch (error) {
      update({ error: error instanceof Error && error.message.trim() ? error.message : '补充信息发送失败，请重试。' })
    } finally {
      update({ busy: false })
    }
  }
  if (question.status === 'answered') return <div className="clarification-response" role="status">
    <span>{question.response?.kind === 'skip' ? '已跳过' : '已答'}</span>
    {question.response?.kind !== 'skip' && <p>{question.response?.answer}</p>}
  </div>
  if (question.status === 'stale') return <p className="clarification-response">此问题已失效</p>
  if (draft.sent) return <p className="clarification-response" role="status">已发送，等待更新</p>
  return <form className="clarification-form" aria-label={`补充信息：${question.question}`} aria-busy={Boolean(draft.busy)} onSubmit={(event) => { event.preventDefault(); void respond(draft.mode) }}>
    <fieldset disabled={draft.busy || Boolean(controls.disabledReason)} title={controls.disabledReason}>
      <legend className="sr-only">回复方式</legend>
      {question.options.length > 0 && <div className="clarification-modes" role="group" aria-label="回复方式">
        <button type="button" aria-pressed={draft.mode === 'choice'} onClick={() => edit({ mode: 'choice' })}>选择选项</button>
        <button type="button" aria-pressed={draft.mode === 'custom'} onClick={() => edit({ mode: 'custom' })}>自己填写</button>
      </div>}
      {draft.mode === 'choice' ? <div className="clarification-options" role="radiogroup" aria-label="补充选项">
        {question.options.map((option, index) => <label key={index}>
          <input type="radio" name={key} value={index} checked={draft.option === index} onChange={() => edit({ option: index })} />
          <span>{option}</span>
        </label>)}
      </div> : <label className="clarification-custom">你的补充
        <textarea value={draft.custom} rows={3} maxLength={2000} onChange={(event) => edit({ custom: event.target.value })} />
      </label>}
      <div className="clarification-actions">
        <button type="submit" className="clarification-confirm" disabled={draft.mode === 'choice' ? draft.option === undefined : !draft.custom.trim()}>
          {draft.busy ? <LoaderCircle size={14} className="motion-safe:animate-spin" /> : <Check size={14} />}确认补充
        </button>
        <button type="button" onClick={() => void respond('skip')}><SkipForward size={14} />跳过</button>
      </div>
    </fieldset>
    {draft.error && <p role="alert">{draft.error}</p>}
  </form>
}
