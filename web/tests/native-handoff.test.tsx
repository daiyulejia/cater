import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { ChatItem, TaskDetail } from '../src/App'
import { MessageDetails } from '../src/MessageDetails'
import { nativeHandoff, nativeHandoffWarning } from '../src/nativeHandoff'
import { completion, detail, event, handoff, leader, members, summaries } from './native-handoff.fixtures'

const label = '为什么这样做 · 如何运行'
const stream = () => within(screen.getByLabelText('聊天记录'))
beforeEach(() => window.sessionStorage.clear())

async function mount(initial: TaskDetail, other?: TaskDetail) {
  let current = initial
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetchMock = vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json(summaries(other ? [other, current] : [current]))
    if (url === `/api/tasks/${current.task.id}`) return json(current)
    if (other && url === `/api/tasks/${other.task.id}`) return json(other)
    if (url === '/api/organization') return json([leader, ...members])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  await screen.findByRole('region', { name: '团队进度' }, { timeout: 3000 })
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'))
  return { fetchMock, update: async (next: TaskDetail) => {
    current = next
    await act(async () => fireEvent(document, new Event('visibilitychange')))
  } }
}

test('可信交付只扩展原折叠区，展示三类精确摘录、版本与有序运行步骤，无副作用', async () => {
  const snapshot = completion()
  const saved = JSON.stringify(snapshot)
  const { fetchMock } = await mount(detail([snapshot]))
  const summary = await stream().findByText(label, { selector: 'summary' })
  const fold = summary.closest('details')!
  const bubble = fold.closest('.message-bubble')!
  expect(fold.open).toBe(false)
  expect(bubble.querySelectorAll('.message-details')).toHaveLength(1)
  expect(stream().queryByText('查看判断与依据')).toBeNull()
  expect(within(fold).getByText('原有验证范围 10')).toBeTruthy()
  const calls = fetchMock.mock.calls.length
  fireEvent.click(summary)
  expect(fold.open).toBe(true)
  for (const heading of ['为什么这样做', '改动', '影响', '取舍与其他考虑', '运行与复查', '未验证范围', '引用来源']) {
    expect(within(fold).getByRole('heading', { name: heading })).toBeTruthy()
  }
  expect(Array.from(fold.querySelectorAll('ol > li')).map((li) => li.textContent)).toEqual(handoff().reproduce)
  expect(Array.from(fold.querySelectorAll('blockquote')).map((quote) => quote.textContent)).toEqual(handoff().evidence.map((source) => source.quote))
  for (const text of ['成果原文 · 事件 #2', '历史工具回执 · 事件 #3', '伙伴复核意见 · 事件 #4',
    '文件：说明文件.html', `文件版本 SHA-256：${'b'.repeat(64)}`, '退出码：0', '回执采集完整',
    '这是当时的工具记录，不证明文件后来未变化。', '这是伙伴的复核意见，不代表工具执行或验收通过。']) {
    expect(within(fold).getByText(text)).toBeTruthy()
  }
  expect(fold.textContent).not.toMatch(/"source_seq"|"handoff"|所有检查已通过|已证明正确/)
  expect(fold.querySelector('button, a, script, main, iframe')).toBeNull()
  fireEvent.click(summary)
  fireEvent.click(summary)
  expect(fetchMock.mock.calls).toHaveLength(calls)
  expect(JSON.stringify(snapshot)).toBe(saved)
})

const omittedWarning = '可选交付说明未通过格式或引用核对，已省略；核心交付验收不受影响。'
function omittedCompletion(seq = 10) {
  const saved = completion(seq)
  delete saved.payload.handoff
  Object.assign(saved.payload, { handoff_status: 'omitted_invalid', handoff_warning: omittedWarning })
  return saved
}

test('可选说明省略警告只进入既有折叠详情，交付正文和完成状态保持不变', async () => {
  const saved = omittedCompletion()
  const before = JSON.stringify(saved)
  const { fetchMock } = await mount(detail([saved]))
  const summary = await stream().findByText('查看判断与依据', { selector: 'summary' })
  const fold = summary.closest('details')!
  const bubble = fold.closest('.message-bubble')!
  expect(fold.open).toBe(false)
  expect(bubble.querySelectorAll('.message-details')).toHaveLength(1)
  expect(stream().getByText('交付摘要 10')).toBeTruthy()
  expect(stream().getByText('· 店长最终交付')).toBeTruthy()
  const body = bubble.cloneNode(true) as HTMLElement
  body.querySelector('.message-details')!.remove()
  expect(body.textContent).not.toContain(omittedWarning)
  expect(body.textContent).not.toMatch(/失败|未完成|省略/)
  const calls = fetchMock.mock.calls.length
  fireEvent.click(summary)
  expect(fold.open).toBe(true)
  expect(within(fold).getByText('原有验证范围 10')).toBeTruthy()
  expect(within(fold).getByText(`可选交付说明：${omittedWarning}`)).toBeTruthy()
  expect(fold.querySelector('blockquote, ol, button, a, [role="alert"]')).toBeNull()
  expect(stream().queryByText(label)).toBeNull()
  expect(fetchMock.mock.calls).toHaveLength(calls)
  expect(JSON.stringify(saved)).toBe(before)
})

test('省略状态屏蔽夹带的 handoff 与未验证引文，没有 verification 时仍可展开警告', () => {
  const evidence = omittedCompletion()
  evidence.payload.handoff = handoff('不得显示的未验证说明')
  delete evidence.payload.verification
  expect(nativeHandoff(evidence)).toBeUndefined()
  expect(nativeHandoffWarning(evidence)).toBe(omittedWarning)
  const { container } = render(<MessageDetails item={{ id: 10, kind: 'agent', evidence } as ChatItem} />)
  const fold = screen.getByText('查看判断与依据').closest('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(fold.querySelector('summary')!)
  expect(within(fold).getByText(`可选交付说明：${omittedWarning}`)).toBeTruthy()
  expect(container.querySelector('blockquote, ol, h3')).toBeNull()
  expect(container.textContent).not.toContain('不得显示的未验证说明')
  for (const source of handoff().evidence) expect(container.textContent).not.toContain(source.quote)
})

test('引用省略仍保留讲解和运行步骤，但夹带的引文与命令记录不能显示', async () => {
  const evidence = completion()
  const warning = '交付讲解已保留；引用核对失败，未保存为验证证据。'
  Object.assign(evidence.payload, { handoff_status: 'references_omitted_invalid', handoff_warning: warning })
  const raw = evidence.payload.handoff as Record<string, unknown>
  raw.command_receipts = [{ private: '不可显示的未核对命令' }]
  const before = JSON.stringify(evidence)
  const { fetchMock } = await mount(detail([evidence]))
  const summary = await stream().findByText(label, { selector: 'summary' })
  const fold = summary.closest('details')!
  expect(fold.open).toBe(false)
  const calls = fetchMock.mock.calls.length
  fireEvent.click(summary)
  expect(within(fold).getByText(`可选交付说明：${warning}`)).toBeTruthy()
  expect(within(fold).getByText(handoff().why)).toBeTruthy()
  expect(Array.from(fold.querySelectorAll('ol > li')).map(node => node.textContent)).toEqual(handoff().reproduce)
  expect(within(fold).getByRole('heading', { name: '未验证范围' })).toBeTruthy()
  expect(fold.querySelector('blockquote, button')).toBeNull()
  expect(fold.textContent).not.toContain('不可显示的未核对命令')
  expect(within(fold).queryByRole('heading', { name: '引用来源' })).toBeNull()
  expect(within(fold).queryByText('成员建议的运行与复查')).toBeNull()
  expect(fetchMock.mock.calls).toHaveLength(calls)
  expect(nativeHandoff(evidence)?.evidence).toEqual([])
  expect(JSON.stringify(evidence)).toBe(before)
})

test('引用省略只信宿主完成记录，正文无效仍不渲染', () => {
  const evidence = completion()
  Object.assign(evidence.payload, { handoff_status: 'references_omitted_invalid', handoff_warning: '引用未核对' })
  const fake = { ...evidence, actor: { id: 'team', kind: 'agent', role: 'team' } }
  expect(nativeHandoff(fake)).toBeUndefined()
  expect(nativeHandoffWarning(fake)).toBeUndefined()
  ;(evidence.payload.handoff as Record<string, unknown>).reproduce = '不是步骤列表'
  expect(nativeHandoff(evidence)).toBeUndefined()
  expect(nativeHandoffWarning(evidence)).toBe('引用未核对')
})

test.each([
  { handoff_status: undefined, handoff_warning: omittedWarning },
  { handoff_status: 'valid', handoff_warning: omittedWarning },
  { handoff_status: {}, handoff_warning: omittedWarning },
  ...[undefined, null, '', '  ', [], {}, 7].map(handoff_warning => ({ handoff_status: 'omitted_invalid', handoff_warning })),
])('缺少有效省略标记或警告时不生成空详情或推断失败：%j', (payload) => {
  const evidence = { ...omittedCompletion(), payload }
  expect(nativeHandoffWarning(evidence)).toBeUndefined()
  const { container } = render(<MessageDetails item={{ id: 10, kind: 'agent', evidence } as ChatItem} />)
  expect(container.childElementCount).toBe(0)
})

test('普通发言和非宿主来源不能提供省略警告，警告内容仅作为纯文本', () => {
  for (const evidence of [
    { ...omittedCompletion(), type: 'msg.said' },
    ...[
      { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
      { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
    ].map(actor => ({ ...omittedCompletion(), actor })),
  ]) {
    expect(nativeHandoffWarning(evidence)).toBeUndefined()
    const view = render(<MessageDetails item={{ id: 10, kind: 'agent', evidence } as ChatItem} />)
    expect(view.container.childElementCount).toBe(0)
    view.unmount()
  }
  const evidence = omittedCompletion()
  evidence.payload.handoff_warning = '<img src=x onerror=alert(1)> [说明](javascript:alert(1))'
  const { container } = render(<MessageDetails item={{ id: 10, kind: 'agent', evidence } as ChatItem} />)
  expect(container.textContent).toContain(evidence.payload.handoff_warning)
  expect(container.querySelector('img, script, a, [role="alert"]')).toBeNull()
})

test('新交付有效说明不继承旧省略警告，后续发言不能替换宿主保存的警告', async () => {
  const omitted = omittedCompletion()
  const spoken = event(11, 'msg.said', { phase: 'team', audience: 'customer', recipient: 'human',
    delivery_seq: 10, run_id: 'run-10', text: '核心成果已交付', handoff_status: 'omitted_invalid',
    handoff_warning: '不可信的自报警告', handoff: handoff('不可信的自报引文') },
  { id: 'store_manager', kind: 'agent', role: 'store_manager' })
  const app = await mount(detail([omitted, spoken]))
  const final = await stream().findByText('核心成果已交付')
  const current = final.closest('article')!
  await waitFor(() => expect(within(current).getByText(`可选交付说明：${omittedWarning}`)).toBeTruthy())
  expect(stream().queryByText(/不可信的自报/)).toBeNull()
  await app.update(detail([omitted, spoken, completion(20)]))
  const next = await stream().findByText('交付摘要 20')
  const nextFold = (await within(next.closest('article')!).findByText(label, { selector: 'summary' })).closest('details')!
  expect(within(nextFold).getByText(handoff().why)).toBeTruthy()
  expect(nextFold.textContent).not.toContain(omittedWarning)
  const history = stream().getByText('店长交付记录', { selector: 'summary' }).closest('details')!
  expect(history.open).toBe(false)
  expect(within(history).getByText(`可选交付说明：${omittedWarning}`)).toBeTruthy()
})

test('采集完成与正文截断分别显示，伙伴和工具使用中文身份', async () => {
  const value = handoff()
  value.evidence[1].truncated = true
  await mount(detail([completion(10, value)]))
  const summary = await stream().findByText(label, { selector: 'summary' })
  fireEvent.click(summary)
  const fold = summary.closest('details')!
  expect(within(fold).getByText('回执采集完整')).toBeTruthy()
  expect(within(fold).getByText('回执正文仅保留部分内容。')).toBeTruthy()
  expect(fold.textContent).toContain('伙伴：米寇 · 工具：运行命令')
  expect(fold.textContent).toContain('伙伴：三角')
  expect(fold.textContent).not.toContain('workspace_run')
  value.evidence[1].truncated = 'invalid' as unknown as boolean
  expect(nativeHandoff(completion(10, value))).toBeUndefined()
})

test('交付版本跟随各自 completion，内部接力仍分组，店长后续发言不覆盖保存说明', async () => {
  const first = completion(10, handoff('初版原因'))
  const second = completion(20, handoff('新版原因'))
  const app = await mount(detail([first]))
  fireEvent.click(await stream().findByText(label, { selector: 'summary' }))
  await app.update(detail([first,
    event(11, 'team.message_received', { kind: 'assignment', recipient: 'dev', text: '请补充测试' }, { id: 'store_manager', kind: 'agent', role: 'store_manager' }),
    event(12, 'msg.said', { phase: 'team', audience: 'team', recipient: 'store_manager', reply_to: 11, text: '正在补充' }, { id: 'dev', kind: 'agent', role: 'dev' }),
    second, event(21, 'msg.said', { phase: 'team', audience: 'customer', recipient: 'human', delivery_seq: 20, run_id: 'run-20',
      text: '面向顾客的最终正文', handoff: handoff('发言自报原因') }, { id: 'store_manager', kind: 'agent', role: 'store_manager' }),
  ]))
  const final = await stream().findByText('面向顾客的最终正文')
  const history = stream().getByText('店长交付记录', { selector: 'summary' }).closest('details')!
  expect(history.open).toBe(false)
  expect(within(history).getByText('初版原因')).toBeTruthy()
  const current = final.closest('article')!
  const currentDetails = within(current).getByText(label, { selector: 'summary' }).closest('details')!
  expect(currentDetails.open).toBe(false)
  expect(within(currentDetails).getByText('新版原因')).toBeTruthy()
  expect(within(currentDetails).getByText('原有验证范围 20')).toBeTruthy()
  expect(within(currentDetails).queryByText('初版原因')).toBeNull()
  expect(stream().queryByText('发言自报原因')).toBeNull()
  expect(final.closest('.team-collaboration')).toBeNull()
  expect(stream().getByText('团队协作 · 2 条').closest('details')!.open).toBe(false)
})

test('旧 verification 与新版说明并存，更新目标后都成为各自历史记录', async () => {
  const old = completion(10, undefined)
  delete old.payload.handoff
  const latest = completion(20)
  const app = await mount(detail([old, latest]))
  expect((await stream().findByText('查看判断与依据')).closest('details')!.open).toBe(false)
  expect(await stream().findByText(label, { selector: 'summary' })).toBeTruthy()
  await app.update(detail([old, latest, event(30, 'team.objective_updated', { goal: '核对新目标' })]))
  await waitFor(() => expect(stream().queryByText('· 店长最终交付')).toBeNull())
  const history = stream().getAllByText('店长交付记录', { selector: 'summary' }).map((summary) => summary.closest('details')!)
  expect(history).toHaveLength(2)
  for (const fold of history) expect(fold.open).toBe(false)
  expect(within(history[0]).queryByText(label)).toBeNull()
  expect(within(history[0]).getByText('原有验证范围 10')).toBeTruthy()
  expect(within(history[1]).getByText(label, { selector: 'summary' })).toBeTruthy()
})

test('切换任务清除展开状态，同序号交付不会串用说明', async () => {
  await mount(detail(), detail([completion(10, handoff('另一任务的原因'))], 'other-ui'))
  fireEvent.click(await stream().findByText(label, { selector: 'summary' }))
  fireEvent.click(screen.getByRole('button', { name: /另一件任务/ }))
  await waitFor(() => expect(stream().queryByText(handoff().why)).toBeNull())
  const next = (await stream().findByText(label, { selector: 'summary' })).closest('details')!
  expect(next.open).toBe(false)
  expect(within(next).getByText('另一任务的原因')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /原生交付测试/ }))
  await waitFor(() => expect(stream().queryByText('另一任务的原因')).toBeNull())
  expect((await stream().findByText(label, { selector: 'summary' })).closest('details')!.open).toBe(false)
})

const invalid = [null, [], '原始字符串', {}, { ...handoff(), version: 2 }, { ...handoff(), why: ' ' },
  ...[null, undefined, false, [], 'false', { stdout: 'false' }, { stderr: 0 }, { stdout: undefined },
    { stdout: false, stderr: [] }, { stdout: false, other: true }].map(output_utf8_valid => ({
      ...handoff(), evidence: [{ ...handoff().evidence[1], output_utf8_valid }],
    })),
  ...['changed', 'impact', 'alternatives', 'reproduce', 'limits', 'evidence'].flatMap((key) => [
    { ...handoff(), [key]: undefined }, { ...handoff(), [key]: {} }, { ...handoff(), [key]: [null] },
  ]),
  ...[{ source_seq: 0 }, { source_seq: 10 }, { source_seq: 1.5 }, { quote: 23 }, { source_hash: '' }, { kind: 'unknown' },
    { kind: { toString: null } }, { kind: ['artifact'] },
    { filename: {} }, { content_sha256: [] }, { role: null }, { tool: false }, { status: 0 }, { exit_code: '0' }, { complete: 'yes' }]
    .map((patch) => ({ ...handoff(), evidence: [{ ...handoff().evidence[0], ...patch }] })),
]

test.each(invalid.map((value, index) => [index, value] as const))('非法说明 %s 回退原验证文字，不抛异常、不展示部分讲解', (_, value) => {
  const evidence = completion(10, value)
  expect(nativeHandoff(evidence)).toBeUndefined()
  const item = { id: 10, kind: 'agent', evidence, details: ['保留验证'] } as ChatItem
  const { container } = render(<MessageDetails item={item} />)
  expect(screen.getByText('查看判断与依据').closest('details')!.open).toBe(false)
  expect(screen.getByText('保留验证')).toBeTruthy()
  expect(screen.queryByText(label)).toBeNull()
  expect(container.querySelector('blockquote, ol, h3')).toBeNull()
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('不可信 completion 来源不能打开交付说明：%j', async (actor) => {
  const forged = { ...completion(), actor }
  expect(nativeHandoff(forged)).toBeUndefined()
  await mount(detail([forged]))
  expect(stream().queryByText(label)).toBeNull()
  expect(stream().queryByText('交付摘要 10')).toBeNull()
})

test('普通发言自报 handoff 不当成交付；没有旧验证的非法说明不产生空折叠区', async () => {
  const said = event(10, 'msg.said', { phase: 'team', audience: 'customer', text: '普通回复', handoff: handoff() },
    { id: 'store_manager', kind: 'agent', role: 'store_manager' })
  expect(nativeHandoff(said)).toBeUndefined()
  await mount(detail([said, completion(20, { version: 2 })]))
  expect(stream().getByText('普通回复')).toBeTruthy()
  expect(stream().queryByText(label)).toBeNull()
  expect(await stream().findByText('查看判断与依据')).toBeTruthy()
})

test('空数组不伪造运行步骤或未验证结论；HTML、链接和命令均为纯文本', () => {
  const saved = { version: 1, why: '<img src=x onerror=alert(1)>', changed: [], impact: [], alternatives: [],
    reproduce: ['[运行](javascript:alert(1))\n$(Remove-Item *)'], limits: [], evidence: [] }
  const item = { id: 10, kind: 'agent', evidence: completion(10, saved) } as ChatItem
  const { container, rerender } = render(<MessageDetails item={item} />)
  expect(screen.getByText(saved.why)).toBeTruthy()
  expect(container.querySelector('ol > li')?.textContent).toBe(saved.reproduce[0])
  expect(container.querySelector('a, button, img, script')).toBeNull()
  expect(screen.queryByText('未验证范围')).toBeNull()
  rerender(<MessageDetails item={{ ...item, evidence: completion(10, null) }} />)
  expect(container.childElementCount).toBe(0)
})

test('普通对话仍用原详情文案、列表和人类消息配色', () => {
  const item = { id: 5, kind: 'human', evidence: event(5, 'team.message_received'), details: ['正在处理消息', '原有依据'] } as ChatItem
  const { container } = render(<MessageDetails item={item} />)
  const fold = screen.getByText('查看判断与依据').closest('details')!
  expect(fold.open).toBe(false)
  expect(fold.className).toContain('border-[#e99c80] text-[#fde9dd]')
  expect(Array.from(container.querySelectorAll('li')).map((li) => li.textContent)).toEqual(item.details)
})

test('历史失败与不完整工具记录照实展示，不因伙伴同意而改写为检查通过', () => {
  const saved = handoff()
  Object.assign(saved.evidence[1], { status: 'error', exit_code: 1, complete: false })
  saved.evidence[2].status = 'approved'
  const item = { id: 10, kind: 'agent', evidence: completion(10, saved) } as ChatItem
  const { container } = render(<MessageDetails item={item} />)
  expect(screen.getByText('退出码：1')).toBeTruthy()
  expect(screen.getByText('回执采集未完成')).toBeTruthy()
  expect(screen.getByText(/记录状态：返回错误/)).toBeTruthy()
  expect(screen.getByText(/记录状态：伙伴同意/)).toBeTruthy()
  expect(screen.getByText('这是伙伴的复核意见，不代表工具执行或验收通过。')).toBeTruthy()
  expect(container.textContent).not.toMatch(/记录状态：执行成功|所有检查已通过/)
})

test.each([
  { stdout: false, stderr: null }, { stdout: null, stderr: false },
  { stdout: true, stderr: false }, { stderr: null }, {},
] as const)('交付按流展示编码限制，保留引用与执行状态：%j', output_utf8_valid => {
  const saved = handoff()
  Object.assign(saved.evidence[1], { output_utf8_valid, truncated: true, quote: 'count=13/32，合法替换字符：\uFFFD' })
  const evidence = completion(10, saved)
  expect(nativeHandoff(evidence)).toBe(saved)
  const snapshot = JSON.stringify(evidence)
  const { container } = render(<MessageDetails item={{ id: 10, kind: 'agent', evidence } as ChatItem} />)
  const fold = container.querySelector('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(fold.querySelector('summary')!)
  const source = container.querySelectorAll('blockquote')[1].closest('li')!
  for (const [key, name] of [['stdout', '标准输出'], ['stderr', '错误输出']] as const) {
    const state = (output_utf8_valid as Record<string, unknown>)[key]
    const hint = state === false ? '已采集输出含无效 UTF-8；不代表当前引用片段已损坏。'
      : state === null ? '输出流的 UTF-8 有效性未知。' : undefined
    if (hint) expect(within(source).getAllByText(`${name}：${hint}`)).toHaveLength(1)
    else expect(source.textContent).not.toContain(`${name}：`)
  }
  expect(source.querySelector('blockquote')?.textContent).toBe(saved.evidence[1].quote)
  expect(source.textContent).toContain('退出码：0')
  expect(source.textContent).toContain('回执采集完整')
  expect(source.textContent).toContain('回执正文仅保留部分内容。')
  expect(source.textContent).not.toMatch(/进程失败|执行失败|GBK|CP936/)
  expect(JSON.stringify(evidence)).toBe(snapshot)
})

test('历史缺失编码字段不回填，合法替换字符不触发警告', () => {
  const saved = handoff()
  saved.evidence[1].quote = '合法字符：\uFFFD'
  const evidence = completion(10, saved)
  expect(nativeHandoff(evidence)?.evidence[1]).not.toHaveProperty('output_utf8_valid')
  const { container } = render(<MessageDetails item={{ id: 10, kind: 'agent', evidence } as ChatItem} />)
  expect(container.textContent).not.toContain('UTF-8')
})

test('无效UTF-8与未完成采集同时展示，不暗示已取得完整输出', () => {
  const saved = handoff()
  Object.assign(saved.evidence[1], { complete: false, output_utf8_valid: { stdout: false } })
  const evidence = completion(10, saved)
  const { container } = render(<MessageDetails item={{ id: 10, kind: 'agent', evidence } as ChatItem} />)
  fireEvent.click(container.querySelector('summary')!)
  const source = container.querySelectorAll('blockquote')[1].closest('li')!
  expect(within(source).getByText('标准输出：已采集输出含无效 UTF-8；不代表当前引用片段已损坏。')).toBeTruthy()
  expect(within(source).getByText('回执采集未完成')).toBeTruthy()
  expect(source.textContent).not.toMatch(/完整输出流|回执采集完整|执行失败/)
  expect(source.querySelector('blockquote')?.textContent).toBe(saved.evidence[1].quote)
})
