import { useEffect, useId, useRef, useSyncExternalStore } from 'react'
import { Check, ListChecks, LoaderCircle, MessageSquareText, RotateCw, SkipForward } from 'lucide-react'
import './questionnaire.css'

export type Questionnaire = {
  seq: number
  role_id: string
  title: string
  questions: {
    id: string
    question: string
    selection: 'single' | 'multiple'
    options: { id: string; label: string; recommended?: boolean }[]
  }[]
  status: 'pending' | 'answered' | 'stale'
  response?: { answers: QuestionnaireAnswer[] }
}
export type QuestionnaireAnswer = { question_id: string; option_ids: string[]; text: string; skipped: boolean }
export type QuestionnaireReply = { question_seq: number; answers: QuestionnaireAnswer[]; request_id: string }

type QuestionDraft = { mode: 'choice' | 'custom' | 'skip'; optionIds: string[]; text: string }
type Draft = {
  questions: Map<string, QuestionDraft>
  busy: boolean
  sent: boolean
  error: string
  attempt?: QuestionnaireReply
}
type Session = {
  serverStatus: Questionnaire['status']
  readonly initialDraft: Draft
  getSnapshot: () => Draft
  subscribe: (listener: () => void) => () => void
  update: (patch: Partial<Draft>) => void
}
export type QuestionnaireStore = Map<string, Session>
export type QuestionnaireControls = {
  store: QuestionnaireStore
  disabledReason?: string
  createRequestId: () => string
  submit: (taskId: string, reply: QuestionnaireReply) => Promise<void>
  onReady?: (element: HTMLFormElement) => void
}

type Question = Questionnaire['questions'][number]
const emptyDraft = (question: Question): QuestionDraft => ({ mode: question.options.length ? 'choice' : 'custom', optionIds: [], text: '' })

function sessionFor(store: QuestionnaireStore, key: string, questions: Question[], serverStatus: Questionnaire['status']): Session {
  const existing = store.get(key)
  if (existing) {
    existing.serverStatus = serverStatus
    return existing
  }
  const initialDraft: Draft = { questions: new Map(questions.map((question) => [question.id, emptyDraft(question)])), busy: false, sent: false, error: '' }
  let draft = initialDraft
  const listeners = new Set<() => void>()
  // Keep the submission and its listeners alive across chat navigation.
  const session: Session = {
    serverStatus,
    initialDraft,
    getSnapshot: () => draft,
    subscribe: (listener) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    update: (patch) => { draft = { ...draft, ...patch }; listeners.forEach((listener) => listener()) },
  }
  store.set(key, session)
  return session
}

function answerFor(question: Question, draft: QuestionDraft): QuestionnaireAnswer {
  if (draft.mode === 'skip') return { question_id: question.id, option_ids: [], text: '', skipped: true }
  const selected = draft.mode === 'choice' ? question.options.filter((option) => draft.optionIds.includes(option.id)).map((option) => option.id) : []
  return {
    question_id: question.id,
    option_ids: question.selection === 'single' ? selected.slice(0, 1) : selected,
    text: draft.text.trim(),
    skipped: false,
  }
}

const isComplete = (answer: QuestionnaireAnswer) => answer.skipped || answer.option_ids.length > 0 || answer.text.length > 0

export function QuestionnaireCard({ taskId, questionnaire, controls }: {
  taskId: string
  questionnaire: Questionnaire
  controls: QuestionnaireControls
}) {
  const id = useId()
  const formRef = useRef<HTMLFormElement>(null)
  const entry = sessionFor(controls.store, JSON.stringify([taskId, questionnaire.seq]), questionnaire.questions, questionnaire.status)
  const draft = useSyncExternalStore(entry.subscribe, entry.getSnapshot, entry.getSnapshot)
  const answers = questionnaire.questions.map((question) => answerFor(question, draft.questions.get(question.id) ?? emptyDraft(question)))
  const completed = answers.filter(isComplete).length
  const locked = Boolean(controls.disabledReason) || draft.busy || draft.sent || questionnaire.status !== 'pending'
  const onReady = controls.onReady
  useEffect(() => {
    if (formRef.current && questionnaire.status === 'pending') onReady?.(formRef.current)
  }, [onReady, taskId, questionnaire.seq, questionnaire.status])

  const edit = (question: Question, patch: Partial<QuestionDraft>) => {
    const current = entry.getSnapshot()
    if (locked || current.busy || current.sent) return
    const previous = current.questions.get(question.id) ?? emptyDraft(question)
    const next = { ...previous, ...patch }
    if (previous.mode === next.mode && previous.text === next.text && JSON.stringify(previous.optionIds) === JSON.stringify(next.optionIds)) return
    entry.update({ questions: new Map(current.questions).set(question.id, next), error: '', attempt: undefined })
  }

  const respond = async () => {
    const current = entry.getSnapshot()
    if (controls.disabledReason || questionnaire.status !== 'pending' || current.busy || current.sent) return
    const nextAnswers = questionnaire.questions.map((question) => answerFor(question, current.questions.get(question.id) ?? emptyDraft(question)))
    const missing = nextAnswers.filter((answer) => !isComplete(answer)).length
    if (!nextAnswers.length || missing) {
      entry.update({ error: missing ? `还有 ${missing} 题待回答，请填写或明确跳过。` : '暂无可提交的问题。' })
      return
    }
    const retry = current.attempt && JSON.stringify(current.attempt.answers) === JSON.stringify(nextAnswers)
    const reply: QuestionnaireReply = {
      question_seq: questionnaire.seq,
      answers: nextAnswers,
      request_id: retry ? current.attempt!.request_id : controls.createRequestId(),
    }
    entry.update({ busy: true, error: '', attempt: reply })
    try {
      await controls.submit(taskId, reply)
      entry.update({ sent: true, busy: false })
    } catch (error) {
      entry.update({ busy: false, error: error instanceof Error && error.message.trim() ? error.message : '补充信息提交失败，请手动重试。' })
    }
  }

  if (questionnaire.status === 'answered') return <section className="questionnaire-response" aria-label={`补充信息：${questionnaire.title}`}>
    <p className="questionnaire-result" role="status"><Check size={14} aria-hidden="true" />已回答</p>
    <dl className="questionnaire-summary">
      {questionnaire.questions.map((question) => {
        const answer = questionnaire.response?.answers.find((item) => item.question_id === question.id)
        return <div key={question.id}>
          <dt>{question.question}</dt>
          <dd>{answer?.skipped ? '已跳过' : answer ? <>
            {answer.option_ids.length > 0 && <ul>{answer.option_ids.map((optionId) => <li key={optionId}>{question.options.find((option) => option.id === optionId)?.label ?? '已选选项（内容已更新）'}</li>)}</ul>}
            {answer.text && <p>{answer.text}</p>}
            {!answer.option_ids.length && !answer.text && '暂无回答内容'}
          </> : '暂无回答记录'}</dd>
        </div>
      })}
    </dl>
  </section>
  if (questionnaire.status === 'stale') return <p className="questionnaire-response" role="status">这组补充问题已失效</p>
  if (draft.sent) return <p className="questionnaire-response" role="status">已发送，等待更新</p>

  return <form ref={formRef} className="questionnaire-form" aria-label={`补充信息：${questionnaire.title}`} aria-busy={draft.busy}
    aria-describedby={`${id}-note${controls.disabledReason ? ` ${id}-disabled` : ''}${draft.error ? ` ${id}-error` : ''}`}
    onSubmit={(event) => { event.preventDefault(); void respond() }}>
    <div className="questionnaire-header">
      <strong>{questionnaire.title}</strong>
      <span role="status" aria-live="polite" className="questionnaire-progress">已完成 {completed}/{questionnaire.questions.length} 题</span>
    </div>
    <p id={`${id}-note`} className="questionnaire-sr-only">推荐仅为建议，不等于同意。</p>
    <fieldset className="questionnaire-fields" disabled={locked}>
      <legend className="questionnaire-sr-only">整组回答</legend>
      {questionnaire.questions.map((question, index) => {
        const current = draft.questions.get(question.id) ?? emptyDraft(question)
        const questionId = `${id}-question-${index}`
        return <fieldset className="questionnaire-question" key={question.id} aria-describedby={`${questionId}-state`}>
          <legend><span className="questionnaire-number" aria-hidden="true">{index + 1}.</span>{question.question}</legend>
          <div className="questionnaire-question-meta" id={`${questionId}-state`}>
            <span>{question.options.length ? question.selection === 'single' ? '单选' : '多选' : '自由填写'}</span>
            <span>{current.mode === 'skip' ? '已标记跳过' : isComplete(answers[index]) ? '已回答' : '待回答'}</span>
          </div>
          <div className="questionnaire-modes" role="group" aria-label={`第 ${index + 1} 题回复方式`}>
            {question.options.length > 0 && <button type="button" aria-pressed={current.mode === 'choice'} onClick={() => edit(question, { mode: 'choice' })}><ListChecks size={14} aria-hidden="true" />选择选项</button>}
            <button type="button" aria-pressed={current.mode === 'custom'} onClick={() => edit(question, { mode: 'custom' })}><MessageSquareText size={14} aria-hidden="true" />自己填写</button>
            <button type="button" aria-pressed={current.mode === 'skip'} onClick={() => edit(question, { mode: 'skip' })}><SkipForward size={14} aria-hidden="true" />跳过此题</button>
          </div>
          {current.mode === 'choice' && <div className="questionnaire-options" role={question.selection === 'single' ? 'radiogroup' : 'group'} aria-label={`第 ${index + 1} 题选项（${question.selection === 'single' ? '单选' : '多选'}）`}>
            {question.options.map((option, optionIndex) => <label className="questionnaire-option" key={option.id}>
              <input type={question.selection === 'single' ? 'radio' : 'checkbox'} name={questionId} value={option.id}
                checked={current.optionIds.includes(option.id)} aria-describedby={option.recommended ? `${questionId}-recommended-${optionIndex} ${id}-note` : undefined}
                onChange={(event) => edit(question, { optionIds: question.selection === 'single' ? [option.id] : event.target.checked ? [...current.optionIds, option.id] : current.optionIds.filter((value) => value !== option.id) })} />
              <span className="questionnaire-option-label">{option.label}{option.recommended && <small id={`${questionId}-recommended-${optionIndex}`} className="questionnaire-recommended" aria-hidden="true">推荐</small>}</span>
            </label>)}
          </div>}
          {current.mode === 'skip' ? <p className="questionnaire-skipped">此题将跳过</p> : <div className="questionnaire-text">
            <label htmlFor={`${questionId}-text`}>你的补充</label>
            <textarea id={`${questionId}-text`} rows={2} maxLength={2000} value={current.text} onChange={(event) => edit(question, { text: event.target.value.slice(0, 2000) })} />
            <span className="questionnaire-length" aria-hidden="true">{current.text.length}/2000</span>
          </div>}
        </fieldset>
      })}
      <div className="questionnaire-actions">
        <button type="submit" className="questionnaire-submit" disabled={locked || !answers.length || completed !== answers.length}>
          {draft.busy ? <LoaderCircle size={15} className="questionnaire-spinner" aria-hidden="true" /> : draft.error && draft.attempt ? <RotateCw size={15} aria-hidden="true" /> : <Check size={15} aria-hidden="true" />}整组提交
        </button>
        <span className="questionnaire-note">{draft.busy ? '正在提交…' : completed < answers.length ? `还有 ${answers.length - completed} 题待回答或跳过` : answers.length ? '已准备好' : '暂无问题'}</span>
      </div>
    </fieldset>
    {controls.disabledReason && <p id={`${id}-disabled`} className="questionnaire-disabled" role="status">{controls.disabledReason}</p>}
    {draft.error && <p id={`${id}-error`} className="questionnaire-error" role="alert">{draft.error}</p>}
  </form>
}
