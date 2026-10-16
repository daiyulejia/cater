import { expect, test, vi } from 'vitest'
import { reloadBlocker, reloadPage } from '../src/pageReload'
import * as storage from '../src/pageRecovery'
import type { PageDraftSnapshot } from '../src/pageRecovery'
import type { ClarificationStore } from '../src/ClarificationCard'
import type { QuestionnaireStore } from '../src/QuestionnaireCard'

const snapshot = (): PageDraftSnapshot => ({ drafts: { $new: '还没发的文字' }, selectedId: null, questionMode: false, derivation: null, project: null })
type ShortDraft = ReturnType<NonNullable<ReturnType<ClarificationStore['get']>>['getSnapshot']>
type FormDraft = ReturnType<NonNullable<ReturnType<QuestionnaireStore['get']>>['getSnapshot']>
const form = <T,>(initialDraft: T, patch: Partial<T> = {}, serverStatus: 'pending' | 'answered' | 'stale' = 'pending') => {
  const draft = { ...initialDraft, ...patch }
  return { initialDraft, serverStatus, getSnapshot: () => draft, subscribe: () => () => {}, update: () => {} }
}
const shortForm = (patch: Partial<ShortDraft> = {}) => form<ShortDraft>({ mode: 'choice', custom: '' }, patch)
const questionForm = (patch: Partial<FormDraft> = {}) => form<FormDraft>({
  sent: false, busy: false, error: '', questions: new Map([['q', { mode: 'choice', optionIds: [], text: '' }]]),
}, patch)

test('仅显式动作保存后刷新，不发送网络请求', async () => {
  const save = vi.spyOn(storage, 'savePageRecovery').mockResolvedValue('')
  const reload = vi.fn(), fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const state = snapshot()
  expect(reload).not.toHaveBeenCalled()
  expect(await reloadPage(() => state, '', () => '', reload)).toBe('')
  expect(save).toHaveBeenCalledWith(state, '', expect.any(Function))
  expect(reload).toHaveBeenCalledTimes(1)
  expect(fetch).not.toHaveBeenCalled()
})

test('在途提交、保存失败不刷新，也不改变输入', async () => {
  const state = snapshot(), reload = vi.fn()
  const save = vi.spyOn(storage, 'savePageRecovery').mockResolvedValue('存储不可用')
  expect(await reloadPage(() => state, '', () => '正在提交', reload)).toBe('正在提交')
  expect(save).not.toHaveBeenCalled()
  expect(await reloadPage(() => state, '', () => '', reload)).toBe('存储不可用')
  expect(reload).not.toHaveBeenCalled()
  expect(state.drafts.$new).toBe('还没发的文字')
})

test.each(['输入', '选中任务', '工作目录', '模式', '提交开始'])('异步保存后%s变化不刷新', async mode => {
  let resolve!: (value: string) => void
  vi.spyOn(storage, 'savePageRecovery').mockReturnValue(new Promise<string>(done => { resolve = done }))
  const state = snapshot(), reload = vi.fn()
  let busy = false
  const result = reloadPage(() => state, '', () => busy ? '正在提交' : '', reload)
  if (mode === '输入') state.drafts.$new = '新输入'
  if (mode === '选中任务') state.selectedId = 'task_other'
  if (mode === '工作目录') state.workspaceId = 'other'
  if (mode === '模式') { state.selectedId = 'task_other'; state.questionMode = true }
  if (mode === '提交开始') busy = true
  resolve('')
  expect(await result).toContain('尚未刷新')
  expect(reload).not.toHaveBeenCalled()
})

test('问题卡默认未填写不阻止刷新，显式跳过、选择、自定义和在途提交均保护', () => {
  const short = new Map() as ClarificationStore, forms = new Map() as QuestionnaireStore
  expect(reloadBlocker(false, short, forms)).toBe('')
  short.set('a', shortForm())
  forms.set('b', questionForm())
  expect(reloadBlocker(false, short, forms)).toBe('')
  expect(reloadBlocker(true, short, forms)).toContain('正在提交')
  for (const patch of [{ option: 0 }, { custom: ' ' }, { mode: 'custom' as const }, { busy: true }]) {
    short.set('a', shortForm(patch))
    expect(reloadBlocker(false, short, forms)).not.toBe('')
  }
  short.clear()
  for (const patch of [{ mode: 'skip' as const }, { mode: 'custom' as const }, { optionIds: ['one'] }, { text: '文字' }]) {
    forms.set('b', questionForm({ questions: new Map([['q', { mode: 'choice', optionIds: [], text: '', ...patch }]]) }))
    expect(reloadBlocker(false, short, forms)).toContain('未提交')
  }
  forms.set('b', questionForm({ sent: true, questions: new Map([['q', { mode: 'skip', optionIds: [], text: '已交回' }]]) }))
  expect(reloadBlocker(false, short, forms)).toBe('')
})
