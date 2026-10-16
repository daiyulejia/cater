import { StrictMode } from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ClarificationCard } from '../src/ClarificationCard'
import type { Clarification, ClarificationControls } from '../src/ClarificationCard'
import { QuestionnaireCard } from '../src/QuestionnaireCard'
import type { Questionnaire, QuestionnaireControls } from '../src/QuestionnaireCard'
import { reloadBlocker, reloadPage } from '../src/pageReload'
import * as storage from '../src/pageRecovery'
import type { PageDraftSnapshot } from '../src/pageRecovery'

const kinds = ['questionnaire', 'clarification'] as const
type Kind = typeof kinds[number]
type Status = Clarification['status']

function setup(kind: Kind, options = false) {
  const submit = vi.fn().mockResolvedValue(undefined)
  let sequence = 0
  const createRequestId = vi.fn(() => `request-${++sequence}`)
  const short: ClarificationControls = { store: new Map(), submit, createRequestId }
  const forms: QuestionnaireControls = { store: new Map(), submit, createRequestId }
  const question: Clarification = { seq: 7, role_id: 'dev', question: '待确认的问题', options: options ? ['选项一'] : [], status: 'pending' }
  const questionnaire: Questionnaire = {
    seq: 7, role_id: 'dev', title: '待确认的问卷', status: 'pending',
    questions: [{ id: 'q', question: question.question, selection: 'single', options: options ? [{ id: 'one', label: '选项一' }] : [] }],
  }
  const card = (status: Status = 'pending', taskId = 'a') => <StrictMode>{kind === 'clarification'
    ? <ClarificationCard taskId={taskId} question={{ ...question, status }} controls={short} />
    : <QuestionnaireCard taskId={taskId} questionnaire={{ ...questionnaire, status }} controls={forms} />}</StrictMode>
  const blocked = () => reloadBlocker(false, short.store, forms.store)
  const entry = (taskId = 'a') => (kind === 'clarification' ? short.store : forms.store).get(JSON.stringify([taskId, 7]))!
  const skip = () => fireEvent.click(screen.getByRole('button', { name: kind === 'clarification' ? '跳过' : '跳过此题', exact: true }))
  const send = () => fireEvent.submit(screen.getByRole('form'))
  const snapshot = (): PageDraftSnapshot => ({ drafts: { $new: '聊天草稿' }, selectedId: null, questionMode: false, derivation: null, project: null })
  const reload = vi.fn()
  const save = vi.spyOn(storage, 'savePageRecovery').mockResolvedValue('')
  return { card, blocked, entry, skip, send, submit, createRequestId, reload, save,
    refresh: () => reloadPage(snapshot, '', blocked, reload) }
}

function deferred() {
  let resolve!: () => void
  let reject!: (error: Error) => void
  const promise = new Promise<void>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

test.each(kinds)('%s：真实渲染无选项默认空白可刷新，填写及空白字符受保护，清空后恢复', async kind => {
  const h = setup(kind)
  render(h.card())
  expect(h.blocked()).toBe('')
  expect(await h.refresh()).toBe('')
  expect(h.reload).toHaveBeenCalledTimes(1)
  expect(h.save).toHaveBeenCalledTimes(1)
  for (const value of ['已回答，跳过，停止，刷新', ' ']) {
    fireEvent.change(screen.getByRole('textbox', { name: '你的补充' }), { target: { value } })
    expect(h.blocked()).toContain('未提交')
    expect(await h.refresh()).toContain('未提交')
    expect(h.reload).toHaveBeenCalledTimes(1)
    expect(h.save).toHaveBeenCalledTimes(1)
  }
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '' } })
  expect(h.blocked()).toBe('')
  expect(h.submit).not.toHaveBeenCalled()
})

test.each(kinds)('%s：选项与显式切换自填均受保护，恢复初始模式后可刷新', kind => {
  const h = setup(kind, true)
  render(h.card())
  expect(h.blocked()).toBe('')
  fireEvent.click(screen.getByRole('button', { name: '自己填写' }))
  expect(h.blocked()).toContain('未提交')
  fireEvent.click(screen.getByRole('button', { name: '选择选项' }))
  expect(h.blocked()).toBe('')
  fireEvent.click(screen.getByRole('radio', { name: '选项一' }))
  expect(h.blocked()).toContain('未提交')
})

test.each(kinds.flatMap(kind => (['answered', 'stale'] as const).map(status => ({ kind, status }))))(
  '$kind：首次渲染$status历史卡的session不误挡刷新', async ({ kind, status }) => {
    const h = setup(kind)
    render(h.card(status))
    expect(h.entry().getSnapshot().sent).toBeFalsy()
    expect(h.entry().serverStatus).toBe(status)
    expect(await h.refresh()).toBe('')
    expect(h.reload).toHaveBeenCalledTimes(1)
  },
)

test.each(kinds.flatMap(kind => (['answered', 'stale'] as const).map(status => ({ kind, status }))))(
  '$kind：已有草稿转$status释放保护，其他聊天未提交草稿仍受保护，卸载不代表结束', async ({ kind, status }) => {
    const h = setup(kind)
    const view = render(h.card())
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '甲的草稿' } })
    const session = h.entry()
    const snapshot = session.getSnapshot()
    const initial = session.initialDraft
    const listener = vi.fn()
    const unsubscribe = session.subscribe(listener)
    view.rerender(h.card(status))
    expect(h.entry()).toBe(session)
    expect(session.getSnapshot()).toBe(snapshot)
    expect(session.initialDraft).toBe(initial)
    expect(listener).not.toHaveBeenCalled()
    expect(h.blocked()).toBe('')
    unsubscribe()

    view.rerender(h.card('pending', 'b'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '乙的草稿' } })
    view.rerender(h.card(status))
    expect(h.blocked()).toContain('未提交')
    view.unmount()
    expect(await h.refresh()).toContain('未提交')
    expect(h.entry('b').serverStatus).toBe('pending')
    expect(h.reload).not.toHaveBeenCalled()
  },
)

test.each(kinds)('%s：空输入显式跳过的busy和未知attempt保留保护及重试ID，stale确认后释放', async kind => {
  const h = setup(kind)
  const request = deferred()
  h.submit.mockReturnValueOnce(request.promise).mockRejectedValue(new Error('提交结果未知'))
  const view = render(h.card())
  h.skip()
  if (kind === 'questionnaire') {
    expect(h.blocked()).toContain('未提交')
    h.send()
  }
  expect(h.blocked()).toContain('正在提交')
  expect(await h.refresh()).toContain('正在提交')
  const attempt = h.entry().getSnapshot().attempt
  expect(attempt?.request_id).toBe('request-1')
  await act(async () => { request.reject(new Error('连接中断')) })
  expect(h.entry().getSnapshot().busy).toBe(false)
  expect(h.blocked()).toContain('尚未确认')
  view.unmount()
  expect(await h.refresh()).toContain('尚未确认')
  expect(h.reload).not.toHaveBeenCalled()
  expect(h.save).not.toHaveBeenCalled()

  const remounted = render(h.card())
  await act(async () => { if (kind === 'clarification') h.skip(); else h.send() })
  expect(h.submit).toHaveBeenCalledTimes(2)
  expect(h.submit.mock.calls[1][1]).toEqual(attempt)
  expect(h.createRequestId).toHaveBeenCalledTimes(1)
  remounted.rerender(h.card('stale'))
  expect(h.entry().getSnapshot().attempt).toEqual(attempt)
  expect(await h.refresh()).toBe('')
  expect(h.reload).toHaveBeenCalledTimes(1)
})

test.each(kinds.flatMap(kind => (['resolve', 'reject'] as const).map(outcome => ({ kind, outcome }))))(
  '$kind：server转stale时本地busy仍保护，迟到$outcome后不永久阻挡', async ({ kind, outcome }) => {
    const h = setup(kind)
    const request = deferred()
    h.submit.mockReturnValue(request.promise)
    const view = render(h.card())
    h.skip()
    if (kind === 'questionnaire') h.send()
    view.rerender(h.card('stale'))
    expect(h.blocked()).toContain('正在提交')
    await act(async () => { if (outcome === 'resolve') request.resolve(); else request.reject(new Error('迟到失败')) })
    expect(h.entry().serverStatus).toBe('stale')
    expect(await h.refresh()).toBe('')
    expect(h.reload).toHaveBeenCalledTimes(1)
  },
)

test.each(kinds)('%s：本地成功但server仍pending时不阻挡，snapshot更新仍通知订阅者', async kind => {
  const h = setup(kind)
  render(h.card())
  const session = h.entry()
  const initial = session.initialDraft
  const listener = vi.fn()
  const unsubscribe = session.subscribe(listener)
  await act(async () => { h.skip(); if (kind === 'questionnaire') h.send() })
  expect(session.serverStatus).toBe('pending')
  expect(session.getSnapshot().sent).toBe(true)
  expect(session.initialDraft).toBe(initial)
  expect(session.getSnapshot()).not.toBe(initial)
  expect(listener).toHaveBeenCalled()
  expect(await h.refresh()).toBe('')
  unsubscribe()
})
