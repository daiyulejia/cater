import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, expect, test, vi } from 'vitest'
import App from '../src/App'

type FixtureEvent = { seq: number; type: string; actor: { id: string; kind?: string; role?: string }; payload: Record<string, unknown> }
const member = { id: 'dev', name: '米寇', role: 'dev' }
const leader = { id: 'store_manager', name: '店长', role: 'leader' }
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const event = (seq: number, type: string, id: string, payload: Record<string, unknown>, kind?: string): FixtureEvent => ({ seq, type, actor: { id, ...(kind ? { kind } : {}) }, payload })

beforeEach(() => { vi.useFakeTimers() })

async function mount(events: FixtureEvent[]) {
  const allEvents = [event(1, 'task.created', 'human', { goal: '核对聊天系统记录' }, 'human'), ...events]
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'system-ui', goal: '核对聊天系统记录', status: 'pending', status_label: '待开始', conversation_count: allEvents.length, latest_seq: allEvents.at(-1)!.seq, version: allEvents.length }] })
    if (url === '/api/organization') return json([leader, member])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [member] })
    if (url === '/api/tasks/system-ui') return json({ task: { id: 'system-ui', goal: '核对聊天系统记录', acceptance: [], status: 'pending', messages: [], assignments: [], deliverables: [] }, events: allEvents, activity: { active: false, state: 'idle' }, review: {} })
    throw new Error(`Unexpected request ${url}`)
  }))
  await act(async () => { render(<App />) })
  return screen.getByLabelText('聊天记录')
}

function record(seq: number): HTMLElement {
  return within(screen.getByLabelText('聊天记录')).getByText(`事件 #${seq} · 审计记录`).closest('article')!
}

// Match the browser's native closed-details visibility, including nested audit details.
function visibleText(element: HTMLElement): string {
  const clone = element.cloneNode(true) as HTMLElement
  clone.querySelectorAll('details:not([open])').forEach((details) => {
    Array.from(details.children).forEach((child) => { if (child.tagName !== 'SUMMARY') child.remove() })
  })
  return clone.textContent ?? ''
}

function openDetails(details: HTMLDetailsElement) {
  fireEvent.click(details.querySelector('summary')!)
  expect(details.open).toBe(true)
}

test.each([undefined, 'system'])('纪律记录（kind=%s）不再冒充成员，常规提醒默认收起且未知码只进入审计', async (kind) => {
  await mount([event(2, 'agent.discipline_flagged', 'discipline', { issues: ['missing_claim', 'unknown_internal_issue'], message_seq: 1 }, kind)])
  const row = record(2)
  expect(within(row).queryByRole('img')).toBeNull()
  expect(row.querySelector('.lucide-cat')).toBeNull()
  expect(row.querySelector('.lucide-info')).toBeTruthy()
  expect(visibleText(row)).toContain('协作提醒')
  expect(visibleText(row)).not.toMatch(/discipline|猫咖|unknown_internal_issue|没有写清结论/)
  const details = row.querySelector('details')!
  expect(details.open).toBe(false)
  openDetails(details)
  expect(visibleText(row)).toContain('没有写清结论、协作格式需要补充')
  expect(visibleText(row)).not.toMatch(/discipline|unknown_internal_issue/)
  const audit = within(row).getByText('事件 #2 · 审计记录').closest('details')!
  openDetails(audit)
  expect(visibleText(row)).toContain('discipline')
  expect(visibleText(row)).toContain('unknown_internal_issue')
  expect(JSON.parse(audit.querySelector('pre')!.textContent!)).toMatchObject({ seq: 2, type: 'agent.discipline_flagged', actor: { id: 'discipline' } })
  const humanAudit = within(record(1)).getByText('事件 #1 · 审计记录').closest('details')!
  openDetails(humanAudit)
  expect(JSON.parse(humanAudit.querySelector('pre')!.textContent!)).toEqual({ goal: '核对聊天系统记录' })
})

test.each([undefined, 'system'])('通过的本机检查（kind=%s）默认收起，展开可查实际命令及依据', async (kind) => {
  const command = ['python', '-m', 'unittest', 'tests.test_runtime']
  const stream = await mount([event(2, 'verification.completed', 'local_verifier', {
    command, outcome: 'passed', exit_code: 0, for_event: 1, git: { head: '1234567890abcdef', dirty: true },
    stdout: { excerpt: 'Ran 12 tests\nOK' },
  }, kind)])
  expect(stream.querySelectorAll('article')).toHaveLength(2)
  const row = record(2)
  expect(within(row).queryByRole('img')).toBeNull()
  expect(row.querySelector('.lucide-cat')).toBeNull()
  expect(row.querySelector('.lucide-circle-check-2, .lucide-check-circle-2')).toBeTruthy()
  expect(visibleText(row)).toContain('本机检查 · 命令通过')
  expect(visibleText(row)).not.toMatch(/local_verifier|猫咖|python|Ran 12 tests/)
  openDetails(row.querySelector('details')!)
  expect(visibleText(row)).toContain(command.join(' '))
  expect(visibleText(row)).toContain('退出码：0')
  expect(visibleText(row)).toContain('关联自报事件 #1；并不自动证实其结论')
  expect(visibleText(row)).toContain('工作区有未提交改动')
  expect(visibleText(row)).toContain('不代表根因或整项任务已证实')
  expect(visibleText(row)).toContain('Ran 12 tests\nOK')
  expect(visibleText(row)).not.toContain('local_verifier')
})

test.each([
  ['failed', '命令失败', 1], ['timeout', '执行超时', null], ['launch_error', '无法启动', null],
])('%s 检查默认展开，命令、退出码与错误内容可见', async (outcome, label, exitCode) => {
  await mount([event(2, 'verification.completed', 'local_verifier', {
    command: ['python', 'check.py'], outcome, exit_code: exitCode,
    stderr: { excerpt: '检查未完成：依赖无法读取' }, ...(outcome === 'launch_error' ? { launch_error: '可执行文件不存在' } : {}),
  }, 'system')])
  const row = record(2)
  expect(row.querySelector('details')!.open).toBe(true)
  expect(row.querySelector('.lucide-triangle-alert')).toBeTruthy()
  expect(visibleText(row)).toContain(`本机检查 · ${label}`)
  expect(visibleText(row)).toContain('python check.py')
  expect(visibleText(row)).toContain(`退出码：${exitCode ?? '无'}`)
  expect(visibleText(row)).toContain('检查未完成：依赖无法读取')
  if (outcome === 'launch_error') expect(visibleText(row)).toContain('启动错误：可执行文件不存在')
  expect(visibleText(row)).not.toContain('local_verifier')
})

test.each([
  ['planning.started', {}, '店长在整理安排'],
  ['planning.finished', { status: 'installed', reason: '先核对需求，再安排执行。' }, '这次这样安排'],
  ['task.progress_reported', { text: '还有一项等待复查。' }, '当前进展'],
  ['brief.generated', { brief: { dispute: '本轮已完成核对。' } }, '本轮总结'],
  ['agent.decided', { reason: '依据已齐，继续推进。' }, '自治决策'],
])('实际店长 kind=system 的 %s 保留成员姓名与头像', async (type, payload, title) => {
  await mount([event(2, type, 'store_manager', payload, 'system')])
  const row = record(2)
  expect(within(row).getByRole('img', { name: '店长' })).toBeTruthy()
  expect(within(row).getByText('店长', { exact: true })).toBeTruthy()
  expect(visibleText(row)).toContain(title)
  expect(row.querySelector('[data-actor="store_manager"].is-agent')).toBeTruthy()
  expect(row.querySelector('[data-actor="system"]')).toBeNull()
})

test('真实执行成员的 system 类型总结保留身份，未注册的系统 actor 使用中性记录', async () => {
  await mount([
    event(2, 'task.progress_reported', 'dev', { text: '成员的进度说明。' }, 'system'),
    event(3, 'task.progress_reported', 'internal_monitor_v2', { text: '系统的进度记录。' }, 'system'),
  ])
  expect(within(record(2)).getByRole('img', { name: '米寇' })).toBeTruthy()
  expect(record(2).querySelector('[data-actor="dev"].is-agent')).toBeTruthy()
  expect(within(record(3)).queryByRole('img')).toBeNull()
  expect(visibleText(record(3))).not.toMatch(/internal_monitor_v2|猫咖/)
  expect(visibleText(record(3))).toContain('系统的进度记录。')
})

test('explainer 的 question.finished 按实际 speaker 保留成员身份与引用详情', async () => {
  vi.useRealTimers()
  await mount([
    event(2, 'question.asked', 'human', { question: '为何这样安排？', role_id: 'dev', basis: { seq: 1, source_seqs: [{ seq: 1, kind: 'goal' }], history_seqs: [] } }, 'human'),
    event(3, 'question.finished', 'explainer', { question_seq: 2, status: 'answered', speaker_id: 'dev', answer: '我依据这次要求安排。', evidence: [{ source_seq: 1, quote: '核对聊天系统记录' }], limits: ['仅基于当时的版本。'] }, 'system'),
  ])
  const row = record(3)
  expect(within(row).getByRole('img', { name: '米寇' })).toBeTruthy()
  expect(row.querySelector('[data-actor="dev"].is-agent')).toBeTruthy()
  expect(visibleText(row)).toContain('我依据这次要求安排。')
  openDetails((await within(row).findByText('查看引用、版本与限制')).closest('details')!)
  expect(visibleText(row)).toContain('原文引用 · 来源事件 #1')
  expect(visibleText(row)).toContain('仅基于当时的版本。')
})

test('没有成员回答的系统提问通知保留 QuestionDetails', async () => {
  vi.useRealTimers()
  await mount([event(2, 'question.finished', 'explainer', { question_seq: 1, status: 'insufficient', reason: '目前引用依据不足。', limits: ['等待可引用的成果。'] }, 'system')])
  const row = record(2)
  expect(within(row).queryByRole('img')).toBeNull()
  expect(visibleText(row)).toContain('目前引用依据不足。')
  openDetails((await within(row).findByText('查看引用、版本与限制')).closest('details')!)
  expect(visibleText(row)).toContain('对应提问 · 事件 #1')
  expect(visibleText(row)).toContain('等待可引用的成果。')
})

test.each([
  ['driver.failed', 'driver_gateway', { message: '模型通道暂时不可用。' }, '模型通道暂时不可用。'],
  ['activity.failed', 'orchestrator', { message: '后台协作未能完成。' }, '后台协作未能完成。'],
  ['driver.budget_exhausted', 'driver_gateway', { driver: '外部模型', used_tokens: 120, budget: 100 }, '本轮不再继续调用它。'],
])('%s 保持异常内容可见，不默认折叠', async (type, actor, payload, text) => {
  await mount([event(2, type, actor, payload, 'system')])
  const row = record(2)
  expect(visibleText(row)).toContain(text)
  expect(row.querySelector('.lucide-triangle-alert')).toBeTruthy()
  expect(within(row).queryByRole('img')).toBeNull()
  expect(visibleText(row)).not.toContain(actor)
  expect(row.querySelectorAll('details')).toHaveLength(1)
  expect(row.querySelector('details')!.querySelector('summary')!.textContent).toContain('审计记录')
})
