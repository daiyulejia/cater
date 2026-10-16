import type { ClarificationStore } from './ClarificationCard'
import type { QuestionnaireStore } from './QuestionnaireCard'
import { savePageRecovery } from './pageRecovery'
import type { PageDraftSnapshot } from './pageRecovery'

export const pageNavigation = { reload: () => window.location.reload() }

export function reloadBlocker(pending: boolean, clarifications: ClarificationStore, questionnaires: QuestionnaireStore): string {
  const short = [...clarifications.values()]
  const forms = [...questionnaires.values()]
  if (pending || [...short, ...forms].some(entry => entry.getSnapshot().busy)) return '还有消息或问题卡正在提交，尚未刷新。'
  if ([...short, ...forms].some(entry => entry.serverStatus === 'pending' && !entry.getSnapshot().sent && entry.getSnapshot().attempt)) {
    return '问题卡提交结果尚未确认，尚未刷新，请先重试或等待状态更新。'
  }
  if (short.some(entry => {
    const draft = entry.getSnapshot()
    return entry.serverStatus === 'pending' && !draft.sent
      && (draft.mode !== entry.initialDraft.mode || draft.option !== undefined || Boolean(draft.custom))
  }) || forms.some(entry => {
    const draft = entry.getSnapshot()
    return entry.serverStatus === 'pending' && !draft.sent && [...draft.questions].some(([id, question]) =>
      question.mode !== entry.initialDraft.questions.get(id)?.mode || question.optionIds.length > 0 || Boolean(question.text))
  })) {
    return '问题卡里还有未提交的填写，尚未刷新；聊天输入也仍保留。'
  }
  return ''
}

export async function reloadPage(getSnapshot: () => PageDraftSnapshot, token: string, blocked: () => string,
  reload = pageNavigation.reload): Promise<string> {
  const reason = blocked()
  if (reason) return reason
  const snapshot = getSnapshot()
  const captured = JSON.stringify(snapshot)
  const current = () => !blocked() && JSON.stringify(getSnapshot()) === captured
  const notice = await savePageRecovery(snapshot, token, current)
  if (notice) return notice
  if (!current()) return '输入或页面已变化，尚未刷新。'
  reload()
  return ''
}
