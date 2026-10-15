export type QuestionBasis = { seq: number; anchor: unknown; source_seqs: { seq: number; kind: string }[]; history_seqs: number[] }
export type QuestionContext = { basis?: QuestionBasis; outdated?: boolean; questionSeq?: number; recipient?: string; progress?: string; evidence?: { source_seq: number; quote: string }[]; limits?: string[]; reason?: string }

export function QuestionDetails({ question, human }: { question: QuestionContext; human: boolean }) {
  const { basis, questionSeq, recipient, evidence, limits, reason } = question
  const sourceNames: Record<string, string> = { task: '任务背景', goal: '目标', requirement: '顾客要求', member_artifact: '成员成果', execution_record: '实际检查', member_review: '成员说明', work_state: '工作状态' }
  return <>
    {question.progress && <p role="status" className="question-progress">{question.progress}</p>}
    {question.outdated && !human && <p className="question-outdated">历史版本 · 回答基于提问时的任务版本</p>}
    <details className="question-details">
      <summary>查看引用、版本与限制</summary>
      <div className="question-context">
        {questionSeq !== undefined && <p>对应提问 · 事件 #{questionSeq}</p>}
        {recipient && <p>提问对象：@{recipient}</p>}
        {basis && <div>
          <p>提问时版本 · 截至事件 #{basis.seq}</p>
          <p>来源事件：{basis.source_seqs?.map((source) => `#${source.seq}（${sourceNames[source.kind] ?? '来源记录'}）`).join('、') || '无'}</p>
          <p>历史问答事件：{basis.history_seqs?.map((seq) => `#${seq}`).join('、') || '无'}</p>
        </div>}
        {evidence?.map((entry, index) => <div key={index}><p className="font-semibold">原文引用 · 来源事件 #{entry.source_seq}</p><blockquote>{entry.quote}</blockquote></div>)}
        {limits?.length ? <div><p className="font-semibold">限制</p><ul>{limits.map((limit, index) => <li key={index}>{limit}</li>)}</ul></div> : null}
        {reason && <p>说明：{reason}</p>}
      </div>
    </details>
  </>
}
