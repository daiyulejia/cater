import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { QuestionnaireCard } from '../src/QuestionnaireCard'
import type { Questionnaire, QuestionnaireControls, QuestionnaireReply, QuestionnaireStore } from '../src/QuestionnaireCard'

const questionnaire = (): Questionnaire => ({
  seq: 7, role_id: 'dev', title: '一起确认这次安排', status: 'pending',
  questions: [
    { id: 'audience', question: '优先服务谁？', selection: 'single', options: [
      { id: 'new', label: '新顾客', recommended: true }, { id: 'returning', label: '老顾客' },
    ] },
    { id: 'channels', question: '通过哪些渠道？', selection: 'multiple', options: [
      { id: 'shop', label: '门店', recommended: true }, { id: 'online', label: '线上' }, { id: 'phone', label: '电话' },
    ] },
    { id: 'notes', question: '还有什么想法？', selection: 'single', options: [] },
    { id: 'timing', question: '什么时候开始？', selection: 'single', options: [{ id: 'now', label: '现在' }] },
  ],
})

function controlsFor(submit = vi.fn<QuestionnaireControls['submit']>().mockResolvedValue(undefined)): QuestionnaireControls {
  let sequence = 0
  const store: QuestionnaireStore = new Map()
  return { store, submit, createRequestId: vi.fn(() => `request-${++sequence}`) }
}
function deferred() {
  let resolve!: () => void
  let reject!: (error: unknown) => void
  const promise = new Promise<void>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
const group = (index: number) => within(screen.getByRole('group', { name: questionnaire().questions[index].question, exact: true }))
const form = () => screen.getByRole('form', { name: `补充信息：${questionnaire().title}` })
const submitAll = async () => { await act(async () => { fireEvent.submit(form()) }) }

test('首次显示通知聊天定位问题开头，填写和轮询不重复抢走阅读位置', () => {
  const controls = { ...controlsFor(), onReady: vi.fn() }
  const q = questionnaire()
  const mounted = render(<QuestionnaireCard taskId="a" questionnaire={q} controls={controls} />)
  expect(controls.onReady).toHaveBeenCalledExactlyOnceWith(form())
  fireEvent.click(group(0).getByRole('radio', { name: '新顾客' }))
  mounted.rerender(<QuestionnaireCard taskId="a" questionnaire={{ ...q }} controls={{ ...controls }} />)
  expect(controls.onReady).toHaveBeenCalledTimes(1)
})
function fillAll() {
  fireEvent.click(group(0).getByRole('radio', { name: '新顾客' }))
  fireEvent.click(group(1).getByRole('checkbox', { name: '门店' }))
  fireEvent.change(group(2).getByRole('textbox', { name: '你的补充' }), { target: { value: '保持轻松' } })
  fireEvent.click(group(3).getByRole('button', { name: '跳过此题' }))
}
function lastReply(controls: QuestionnaireControls): QuestionnaireReply {
  return vi.mocked(controls.submit).mock.calls.at(-1)![1]
}

test('推荐仅为建议，无默认选择；空白不会被默默跳过，每题都明确完成后才可提交', async () => {
  const controls = controlsFor()
  render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  expect(screen.getByText('推荐仅为建议，不等于同意。')).toBeTruthy()
  expect(screen.getAllByText('推荐')).toHaveLength(2)
  expect(screen.getByRole('radio', { name: '新顾客', description: '推荐 推荐仅为建议，不等于同意。' })).toBeTruthy()
  for (const input of screen.getAllByRole<HTMLInputElement>('radio').concat(screen.getAllByRole<HTMLInputElement>('checkbox'))) expect(input.checked).toBe(false)
  expect(screen.getByText('已完成 0/4 题')).toBeTruthy()
  expect((screen.getByRole('button', { name: '整组提交' }) as HTMLButtonElement).disabled).toBe(true)
  await submitAll()
  expect(screen.getByRole('alert').textContent).toContain('还有 4 题待回答')
  expect(controls.submit).not.toHaveBeenCalled()
  expect(controls.createRequestId).not.toHaveBeenCalled()
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: ' \n ' } })
  expect(screen.getByText('已完成 0/4 题')).toBeTruthy()
  fillAll()
  expect(controls.submit).not.toHaveBeenCalled()
  expect(screen.getByText('已完成 4/4 题')).toBeTruthy()
  expect((screen.getByRole('button', { name: '整组提交' }) as HTMLButtonElement).disabled).toBe(false)
})

test('单选、多选、补充文字、自填和跳过合并为一次提交，顺序及空白规范化正确', async () => {
  const controls = controlsFor()
  render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  fireEvent.click(group(0).getByRole('radio', { name: '老顾客' }))
  expect((group(0).getByRole('radio', { name: '新顾客' }) as HTMLInputElement).checked).toBe(false)
  fireEvent.change(group(0).getByRole('textbox'), { target: { value: '  优先会员  ' } })
  fireEvent.click(group(1).getByRole('checkbox', { name: '线上' }))
  fireEvent.click(group(1).getByRole('checkbox', { name: '电话' }))
  fireEvent.click(group(1).getByRole('checkbox', { name: '门店' }))
  fireEvent.change(group(1).getByRole('textbox'), { target: { value: '  先小范围尝试  ' } })
  fireEvent.click(group(2).getByRole('button', { name: '自己填写' }))
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: '  保持轻松  ' } })
  expect(controls.submit).not.toHaveBeenCalled()
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '整组提交' })) })
  expect(controls.submit).toHaveBeenCalledTimes(1)
  expect(controls.submit).toHaveBeenCalledWith('a', {
    question_seq: 7, request_id: 'request-1', answers: [
      { question_id: 'audience', option_ids: ['returning'], text: '优先会员', skipped: false },
      { question_id: 'channels', option_ids: ['online', 'phone'], text: '先小范围尝试', skipped: false },
      { question_id: 'notes', option_ids: [], text: '保持轻松', skipped: false },
      { question_id: 'timing', option_ids: [], text: '', skipped: true },
    ],
  })
  expect(screen.getByRole('status').textContent).toBe('已发送，等待更新')
  expect(screen.queryByRole('form')).toBeNull()
})

test('每题自填可独立回答；选择与文字草稿经过跳过和模式切换仍保留，跳过不携带草稿', async () => {
  const controls = controlsFor()
  render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  fireEvent.change(group(0).getByRole('textbox'), { target: { value: '自己的答案' } })
  fireEvent.click(group(0).getByRole('button', { name: '自己填写' }))
  expect(group(0).queryByRole('radio')).toBeNull()
  fireEvent.click(group(0).getByRole('button', { name: '跳过此题' }))
  expect(group(0).queryByRole('textbox')).toBeNull()
  fireEvent.click(group(0).getByRole('button', { name: '选择选项' }))
  expect((group(0).getByRole('radio', { name: '新顾客' }) as HTMLInputElement).checked).toBe(true)
  expect((group(0).getByRole('textbox') as HTMLTextAreaElement).value).toBe('自己的答案')
  fireEvent.click(group(0).getByRole('button', { name: '自己填写' }))
  fireEvent.change(group(1).getByRole('textbox'), { target: { value: '不应发送' } })
  fireEvent.click(group(1).getByRole('button', { name: '跳过此题' }))
  await submitAll()
  expect(lastReply(controls).answers[0]).toEqual({ question_id: 'audience', option_ids: [], text: '自己的答案', skipped: false })
  expect(lastReply(controls).answers[1]).toEqual({ question_id: 'channels', option_ids: [], text: '', skipped: true })
})

test('有选项也可以只写文字，单选多选约束不影响独立文字回答，文本最多2000字', async () => {
  const controls = controlsFor()
  render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  for (let index = 0; index < 4; index++) {
    const input = group(index).getByRole('textbox')
    expect((input as HTMLTextAreaElement).maxLength).toBe(2000)
    fireEvent.change(input, { target: { value: index === 0 ? '字'.repeat(2001) : '只写文字' } })
  }
  expect((group(0).getByRole('textbox') as HTMLTextAreaElement).value).toHaveLength(2000)
  expect(screen.getByText('已完成 4/4 题')).toBeTruthy()
  await submitAll()
  expect(lastReply(controls).answers.every((answer) => !answer.option_ids.length && !answer.skipped && answer.text)).toBe(true)
  expect(lastReply(controls).answers[0].text).toHaveLength(2000)
})

test('跳过切回文字保留每题独立草稿；整组显式跳过也仅在最后提交一次', async () => {
  const controls = controlsFor()
  render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  for (let index = 0; index < 4; index++) {
    fireEvent.change(group(index).getByRole('textbox'), { target: { value: `第${index + 1}题的草稿` } })
    fireEvent.click(group(index).getByRole('button', { name: '跳过此题' }))
    fireEvent.click(group(index).getByRole('button', { name: '自己填写' }))
    expect((group(index).getByRole('textbox') as HTMLTextAreaElement).value).toBe(`第${index + 1}题的草稿`)
    fireEvent.click(group(index).getByRole('button', { name: '跳过此题' }))
  }
  expect(controls.submit).not.toHaveBeenCalled()
  expect(screen.getByText('已完成 4/4 题')).toBeTruthy()
  await submitAll()
  expect(controls.submit).toHaveBeenCalledTimes(1)
  expect(lastReply(controls).answers).toEqual(questionnaire().questions.map((question) => ({ question_id: question.id, option_ids: [], text: '', skipped: true })))
})

test('busy同步阻止重复submit和编辑，成功等待服务器状态时也不重发', async () => {
  const request = deferred()
  const controls = controlsFor(vi.fn().mockReturnValue(request.promise))
  const view = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  const activeForm = form()
  fireEvent.submit(activeForm)
  fireEvent.submit(activeForm)
  expect(controls.submit).toHaveBeenCalledTimes(1)
  expect(activeForm.getAttribute('aria-busy')).toBe('true')
  expect(group(2).getByRole('textbox').matches(':disabled')).toBe(true)
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: '不应覆盖' } })
  expect((group(2).getByRole('textbox') as HTMLTextAreaElement).value).toBe('保持轻松')
  await act(async () => { request.resolve() })
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  expect(screen.getByRole('status').textContent).toBe('已发送，等待更新')
  expect(controls.submit).toHaveBeenCalledTimes(1)
})

test('失败不自动重发，手动重试使用同ID及payload，编辑后即使改回也换新ID', async () => {
  const submit = vi.fn<QuestionnaireControls['submit']>().mockRejectedValue(new Error('网络中断，请手动重试'))
  const controls = controlsFor(submit)
  const view = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  await submitAll()
  const first = lastReply(controls)
  expect(screen.getByRole('alert').textContent).toBe('网络中断，请手动重试')
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  await act(async () => { await Promise.resolve() })
  expect(submit).toHaveBeenCalledTimes(1)
  await submitAll()
  expect(lastReply(controls)).toEqual(first)
  expect(controls.createRequestId).toHaveBeenCalledTimes(1)
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: '已修改' } })
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: '保持轻松' } })
  expect(screen.queryByRole('alert')).toBeNull()
  await submitAll()
  expect(lastReply(controls)).toEqual({ ...first, request_id: 'request-2' })
  fireEvent.click(group(0).getByRole('button', { name: '跳过此题' }))
  await submitAll()
  expect(lastReply(controls).request_id).toBe('request-3')
  expect(lastReply(controls).answers[0].skipped).toBe(true)
  await submitAll()
  expect(lastReply(controls).request_id).toBe('request-3')
})

test('切聊天及卸载保留每题草稿，taskId和seq隔离；进行中的提交不重复', async () => {
  const request = deferred()
  const controls = controlsFor(vi.fn().mockReturnValue(request.promise))
  const view = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  fireEvent.change(group(0).getByRole('textbox'), { target: { value: '甲的补充' } })
  view.rerender(<QuestionnaireCard taskId="b" questionnaire={questionnaire()} controls={controls} />)
  expect(screen.getByText('已完成 0/4 题')).toBeTruthy()
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: '乙的草稿' } })
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={{ ...questionnaire(), seq: 8 }} controls={controls} />)
  expect(screen.getByText('已完成 0/4 题')).toBeTruthy()
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  expect((group(0).getByRole('textbox') as HTMLTextAreaElement).value).toBe('甲的补充')
  expect((group(0).getByRole('radio', { name: '新顾客' }) as HTMLInputElement).checked).toBe(true)
  expect(group(3).getByRole('button', { name: '跳过此题' }).getAttribute('aria-pressed')).toBe('true')
  fireEvent.submit(form())
  view.unmount()
  const next = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  expect(form().getAttribute('aria-busy')).toBe('true')
  fireEvent.submit(form())
  expect(controls.submit).toHaveBeenCalledTimes(1)
  next.rerender(<QuestionnaireCard taskId="b" questionnaire={questionnaire()} controls={controls} />)
  await act(async () => { request.reject(new Error('甲的失败')) })
  expect(screen.queryByRole('alert')).toBeNull()
  expect((group(2).getByRole('textbox') as HTMLTextAreaElement).value).toBe('乙的草稿')
  next.rerender(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  expect(screen.getByRole('alert').textContent).toBe('甲的失败')
  await submitAll()
  expect(vi.mocked(controls.submit).mock.calls[1][1]).toEqual(vi.mocked(controls.submit).mock.calls[0][1])
})

test('切聊天后迟到成功仅更新原会话，回到原聊天显示等待更新而不重发', async () => {
  const request = deferred()
  const controls = controlsFor(vi.fn().mockReturnValue(request.promise))
  const view = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  fireEvent.submit(form())
  view.rerender(<QuestionnaireCard taskId="b" questionnaire={questionnaire()} controls={controls} />)
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: '乙的草稿' } })
  await act(async () => { request.resolve() })
  expect((group(2).getByRole('textbox') as HTMLTextAreaElement).value).toBe('乙的草稿')
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  expect(screen.getByRole('status').textContent).toBe('已发送，等待更新')
  expect(controls.submit).toHaveBeenCalledTimes(1)
})

test('disabledReason锁定所有入口并展示原因，恢复后保留草稿', async () => {
  const controls = controlsFor()
  const view = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={{ ...controls, disabledReason: '任务已暂停' }} />)
  expect(screen.getByText('任务已暂停')).toBeTruthy()
  for (const element of form().querySelectorAll('input, textarea, button')) expect(element.matches(':disabled')).toBe(true)
  fireEvent.change(group(2).getByRole('textbox'), { target: { value: '不应写入' } })
  await submitAll()
  expect(controls.submit).not.toHaveBeenCalled()
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  expect((group(2).getByRole('textbox') as HTMLTextAreaElement).value).toBe('保持轻松')
  await submitAll()
  expect(controls.submit).toHaveBeenCalledTimes(1)
})

test.each(['resolve', 'reject'] as const)('提交中失效后迟到%s仍只显示stale，不再有表单', async (outcome) => {
  const request = deferred()
  const controls = controlsFor(vi.fn().mockReturnValue(request.promise))
  const view = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  fireEvent.submit(form())
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={{ ...questionnaire(), status: 'stale' }} controls={controls} />)
  expect(screen.queryByRole('form')).toBeNull()
  expect(screen.getByRole('status').textContent).toBe('这组补充问题已失效')
  await act(async () => { if (outcome === 'resolve') request.resolve(); else request.reject(new Error('迟到失败')) })
  expect(screen.getByRole('status').textContent).toBe('这组补充问题已失效')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(controls.submit).toHaveBeenCalledTimes(1)
})

test('answered摘要仅显示服务器response，映射选项为文字，不展示本地草稿或raw JSON', () => {
  const controls = controlsFor()
  const view = render(<QuestionnaireCard taskId="a" questionnaire={questionnaire()} controls={controls} />)
  fillAll()
  fireEvent.change(group(0).getByRole('textbox'), { target: { value: '不要展示的本地草稿' } })
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={{ ...questionnaire(), status: 'answered', response: { answers: [
    { question_id: 'timing', option_ids: ['now'], text: '', skipped: false },
    { question_id: 'notes', option_ids: [], text: '<script>服务器补充</script>', skipped: false },
    { question_id: 'channels', option_ids: ['shop', 'online'], text: '服务器渠道备注', skipped: false },
    { question_id: 'audience', option_ids: [], text: '', skipped: true },
  ] } }} controls={controls} />)
  expect(screen.queryByRole('form')).toBeNull()
  expect(screen.getByRole('status').textContent).toBe('已回答')
  expect(screen.getByText('已跳过')).toBeTruthy()
  expect(screen.getByText('现在')).toBeTruthy()
  expect(screen.getByText('门店')).toBeTruthy()
  expect(screen.getByText('线上')).toBeTruthy()
  expect(screen.getByText('服务器渠道备注')).toBeTruthy()
  expect(screen.getByText('<script>服务器补充</script>')).toBeTruthy()
  expect(screen.queryByText('不要展示的本地草稿')).toBeNull()
  expect(document.querySelector('script, pre')).toBeNull()
  expect(controls.submit).not.toHaveBeenCalled()
})

test('题目和选项安全渲染为纯文本，推荐不改变选项可访问名称', () => {
  const data = questionnaire()
  data.questions[0].question = '<img src=x onerror=alert(1)>'
  data.questions[0].options[0].label = '<script>alert(1)</script>'
  render(<QuestionnaireCard taskId="a" questionnaire={data} controls={controlsFor()} />)
  expect(screen.getByRole('group', { name: '<img src=x onerror=alert(1)>', exact: true })).toBeTruthy()
  expect(screen.getByRole('radio', { name: '<script>alert(1)</script>', exact: true })).toBeTruthy()
  expect(document.querySelector('script, img')).toBeNull()
})

test('空问题组不提交，answered无服务器response时不伪造草稿摘要', async () => {
  const controls = controlsFor()
  const view = render(<QuestionnaireCard taskId="a" questionnaire={{ ...questionnaire(), questions: [] }} controls={controls} />)
  expect((screen.getByRole('button', { name: '整组提交' }) as HTMLButtonElement).disabled).toBe(true)
  await submitAll()
  expect(controls.submit).not.toHaveBeenCalled()
  expect(screen.getByRole('alert').textContent).toBe('暂无可提交的问题。')
  view.rerender(<QuestionnaireCard taskId="a" questionnaire={{ ...questionnaire(), status: 'answered' }} controls={controls} />)
  expect(screen.getAllByText('暂无回答记录')).toHaveLength(4)
  expect(screen.queryByRole('form')).toBeNull()
})
