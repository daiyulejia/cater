import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'
import type { ChatItem, TaskDetail } from '../src/App'
import { MessageDetails } from '../src/MessageDetails'
import { nativeHandoff } from '../src/nativeHandoff'
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
  await screen.findByRole('region', { name: '团队进度' })
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
