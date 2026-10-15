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
  const allEvents = () => [event(1, 'task.created', 'human', { goal: '核对聊天系统记录' }, 'human'), ...events]
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json({ tasks: [{ id: 'system-ui', goal: '核对聊天系统记录', status: 'pending', status_label: '待开始', conversation_count: allEvents().length, latest_seq: Math.max(...allEvents().map(entry => entry.seq)), version: allEvents().length }] })
    if (url === '/api/organization') return json([leader, member])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: [member] })
    if (url === '/api/tasks/system-ui') return json({ task: { id: 'system-ui', goal: '核对聊天系统记录', acceptance: [], status: 'pending', messages: [], assignments: [], deliverables: [] }, events: allEvents(), activity: { active: false, state: 'idle' }, review: {} })
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
  fireEvent(details, new Event('toggle'))
  expect(details.open).toBe(true)
}

test('317/320 无效店长回应保留为醒目的系统失败回执，关联真实消息且不冒充成员产出', async () => {
  await mount([
    event(316, 'conversation.received', 'human', { instruction: '月亮为什么没有往下交接？' }, 'human'),
    event(317, 'conversation.finished', 'conversation', { conversation_seq: 316, status: 'clarification',
      error_code: 'invalid_response', speaker_id: 'store_manager', reply: '这条消息已保留，但我这次没能得到有效回应。' }, 'system'),
    event(319, 'conversation.received', 'human', { instruction: '有人回应吗？' }, 'human'),
    event(320, 'conversation.finished', 'conversation', { conversation_seq: 319, status: 'clarification',
      error_code: 'invalid_response', speaker_id: 'store_manager', reply: '这条消息已保留，但我这次没能得到有效回应。' }, 'system'),
  ])
  for (const [seq, source] of [[317, 316], [320, 319]]) {
    const row = record(seq)
    expect(row.getAttribute('role')).toBe('alert')
    expect(row.classList.contains('needs-attention')).toBe(true)
    expect(row.querySelector('[data-actor="system"]')).toBeTruthy()
    expect(row.querySelector('[data-actor="store_manager"]')).toBeNull()
    expect(visibleText(row)).toContain('店长回应需要跟进')
    expect(visibleText(row)).toContain('这条消息已保存')
    expect(visibleText(row)).toContain('回应未通过格式校验')
    expect(visibleText(row)).toContain('未按这条消息更改安排')
    expect(visibleText(row)).toContain('你可以重新发送')
    expect(visibleText(row)).toContain(`对应你的消息 · 事件 #${source}`)
  }
  expect(screen.getByText('月亮为什么没有往下交接？')).toBeTruthy()
  expect(screen.getByText('有人回应吗？')).toBeTruthy()
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
})

test('无效店长回应优先保留后端 progress 对应的具体检查点原因', async () => {
  const reply = '这条消息已保存，但回应格式暂未通过。月亮已提交说明，当前停在复核回复校正，尚未交给米寇继续；不是没有产出。'
  await mount([
    event(321, 'conversation.received', 'human', { instruction: '现在究竟卡在哪里？' }, 'human'),
    event(322, 'conversation.finished', 'conversation', { conversation_seq: 321, status: 'clarification',
      error_code: 'invalid_response', speaker_id: 'store_manager', reply,
      progress: { state: 'waiting_review', summary: '当前停在复核回复校正' } }, 'system'),
  ])
  const row = record(322)
  expect(row.getAttribute('role')).toBe('alert')
  expect(row.classList.contains('needs-attention')).toBe(true)
  expect(visibleText(row)).toContain(reply)
  expect(visibleText(row)).not.toContain('你可以重新发送')
  expect(visibleText(row)).toContain('对应你的消息 · 事件 #321')
  expect(row.querySelector('[data-actor="system"]')).toBeTruthy()
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
})

test.each([
  { progress: null, reply: '没有检查点对象的回应' },
  { progress: [], reply: '数组不是检查点对象' },
  { progress: {}, reply: '   ' },
])('无效 progress 或空 reply 继续显示明确的旧回执兜底：%j', async (payload) => {
  await mount([event(317, 'conversation.finished', 'conversation', {
    status: 'clarification', error_code: 'invalid_response', ...payload,
  }, 'system')])
  expect(visibleText(record(317))).toContain('这条消息已保存，但店长的回应未通过格式校验')
})

test.each(['agent', 'system'])('response_format_error 即使记录在成员名下也显示醒目系统故障（kind=%s）', async (kind) => {
  const text = '月亮的回应未通过格式校验，已保留本次调用记录；当前等待重新核对响应。'
  await mount([event(318, 'msg.said', member.id, {
    phase: 'response_format_error', speech_source: 'system_receipt', error_code: 'invalid_response', text,
  }, kind)])
  const row = record(318)
  expect(row.getAttribute('role')).toBe('alert')
  expect(row.classList.contains('needs-attention')).toBe(true)
  expect(row.querySelector('.lucide-triangle-alert')).toBeTruthy()
  expect(row.querySelector('[data-actor="system"]')).toBeTruthy()
  expect(row.querySelector(`[data-actor="${member.id}"]`)).toBeNull()
  expect(within(row).queryByRole('img')).toBeNull()
  expect(visibleText(row)).toContain('成员回应格式异常')
  expect(visibleText(row)).toContain(text)
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
})

test.each([false, true])('缺少实际检查的checkpoint明确显示系统阻塞，不冒充店长或被旧决策隐藏（decision=%s）', async (decision) => {
  await mount([
    ...(decision ? [event(2, 'agent.decided', 'store_manager', { reason: '此前的决策' }, 'system')] : []),
    event(3, 'brief.generated', 'store_manager', { reason: 'work_checkpoint', brief: {
      checkpoint: { state: 'waiting_local_check' }, dispute: '米寇已提交俄罗斯方块，测试工作等待前置成果验收。',
    } }, 'system'),
  ])
  const summary = record(3)
  expect(summary.querySelector('[data-actor="system"]')).toBeTruthy()
  expect(summary.querySelector('[data-actor="store_manager"]')).toBeNull()
  expect(within(summary).queryByRole('img')).toBeNull()
  expect(visibleText(summary)).toContain('等待实际检查')
  expect(visibleText(summary)).toContain('当前没有完整检查回执')
  expect(visibleText(summary)).toContain('具体待查原因以检查记录为准')
  expect(visibleText(summary)).not.toContain('尚未接通自动实际检查')
  expect(visibleText(summary)).toContain('源码审查可以先进行')
  expect(visibleText(summary)).toContain('米寇已提交俄罗斯方块')
  expect(visibleText(summary)).not.toMatch(/本轮总结|团队已完成一轮协作/)
})

test.each(['review_response_correction', 'review'])('内部复核修复 %s 收进中性过程记录，但最终失败仍公开', async (phase) => {
  vi.useRealTimers()
  await mount([
    event(2, 'msg.said', 'dev', { phase, text: '这次卡在我的复核回复格式或引用上，还不能据此认定成果有错。我会按原成果重新整理一次回复。', tokens: 100 }, 'agent'),
    event(3, 'msg.said', 'dev', { phase: 'review', text: '复核仍未完成，引用需要核对。' }, 'agent'),
  ])
  const row = record(2)
  expect(visibleText(row)).toContain('复核记录整理中')
  expect(visibleText(row)).not.toContain('这次卡在')
  expect(row.querySelector('details')!.open).toBe(false)
  expect(within(row).queryByRole('img')).toBeNull()
  expect(visibleText(record(3))).toContain('复核仍未完成')
  openDetails(row.querySelector('details')!)
  openDetails(within(row).getByText('事件 #2 · 审计记录').closest('details')!)
  await act(async () => { await vi.dynamicImportSettled() })
  openDetails((await within(row).findByText('开发者详情')).closest('details')!)
  expect(visibleText(row)).toContain('100')
})

test.each([true, false])('复核显示具体判断和可展开的依据，兼容历史模板（legacy=%s）', async (legacy) => {
  await mount([
    event(2, 'work.reviewed', 'dev', { work_item_id: 'layout', status: 'accepted', checks: [
      { id: 'focus', status: 'passed', reason: '输入框保留焦点，按钮可以用键盘操作。', artifact_seq: 10, quote: 'tabindex="0"' },
    ] }, 'agent'),
    event(3, 'msg.said', 'dev', { phase: 'review', work_item_id: 'layout',
      ...(legacy ? {} : { review_seq: 2, speech_source: 'review_assessment' }),
      text: legacy ? '这一项的阶段标准通过了，可以交给后面的伙伴接着做；整件任务还需要最后核对。' : '输入框保留焦点，按钮可以用键盘操作。',
    }, 'agent'),
  ])
  const row = record(3)
  expect(visibleText(row)).toContain('输入框保留焦点，按钮可以用键盘操作。')
  expect(visibleText(row)).toContain('复核意见 · 阶段通过')
  expect(visibleText(row)).not.toContain('这一项的阶段标准通过了')
  openDetails(within(row).getByText('查看判断与依据').closest('details')!)
  expect(visibleText(row)).toContain('成果 #10')
  expect(visibleText(row)).toContain('tabindex="0"')
})

test.each([true, false])('模型不确定判断保留，系统强制检查理由仅在依据中显示（含确定判断=%s）', async (hasModel) => {
  await mount([
    event(2, 'work.reviewed', 'dev', { work_item_id: 'layout', status: 'submitted', checks: [
      ...(hasModel ? [{ source: 'model_assessment', status: 'passed', reason: '源码中的键盘事件绑定完整。' }] : []),
      { source: 'runtime_check', status: 'unknown', reason: '缺少浏览器实际检查回执。', artifact_seq: 10 },
      { source: 'runtime_check', status: 'failed', reason: '本机检查执行失败。', receipt_seq: 12 },
      { source: 'model_assessment', status: 'unknown', reason: '尚无足够实际观察。' },
      { source: 'model_assessment', status: 'unknown', response_issue: true, reason: '复核回复格式待校正。' },
    ] }, 'agent'),
    event(3, 'msg.said', 'dev', { phase: 'review', work_item_id: 'layout', review_seq: 2,
      speech_source: 'review_assessment', text: '缺少浏览器实际检查回执。',
    }, 'agent'),
  ])
  const row = record(3)
  expect(visibleText(row)).toContain('复核意见 · 仍有待确认项')
  expect(visibleText(row)).not.toMatch(/缺少浏览器实际检查回执|本机检查执行失败|复核回复格式待校正/)
  expect(visibleText(row)).toContain('尚无足够实际观察。')
  if (hasModel) expect(visibleText(row)).toContain('源码中的键盘事件绑定完整。')
  openDetails(within(row).getByText('查看判断与依据').closest('details')!)
  expect(visibleText(row)).toContain('系统检查依据：缺少浏览器实际检查回执。')
  expect(visibleText(row)).toContain('系统检查依据：本机检查执行失败。')
  expect(visibleText(row)).toContain('成果 #10')
  expect(visibleText(row)).toContain('检查 #12')
  expect(visibleText(row)).toContain('待确认依据：尚无足够实际观察。')
  expect(visibleText(row)).toContain('待确认依据：复核回复格式待校正。')
})

test.each(['started', 'correcting', 'ready', 'unsupported', 'invalid', 'unavailable', 'stale'])('浏览器检查方案 %s 只作为系统记录展示', async (status) => {
  await mount([event(2, status === 'started' ? 'verification.planning_started' : 'verification.planned', 'store_manager', {
    status, reason: '本次检查安排说明。', criterion_id: 'keyboard',
    plan: { entrypoint: 'tetris.html', steps: [{ label: '检查键盘落块', expression: 'INTERNAL_SCRIPT' }] },
    speaker_id: 'dev',
  }, 'system')])
  const row = record(2)
  expect(row.querySelector('[data-actor="system"]')).toBeTruthy()
  expect(within(row).queryByRole('img')).toBeNull()
  const details = row.querySelector('details')!
  expect(details.open).toBe(!['started', 'correcting', 'ready'].includes(status))
  if (status === 'correcting') {
    expect(visibleText(row)).toContain('检查方案正在校正，尚未执行')
    expect(visibleText(row)).not.toMatch(/检查能力缺口|工具不可用|方案无效/)
    expect(row.classList.contains('needs-attention')).toBe(false)
    expect(row.querySelector('.lucide-triangle-alert')).toBeNull()
  }
  if (!details.open) openDetails(details)
  expect(visibleText(row)).toContain('本次检查安排说明。')
  expect(visibleText(row)).toContain('浏览器入口：tetris.html')
  expect(visibleText(row)).toContain('计划步骤 1：检查键盘落块')
  expect(visibleText(row)).not.toContain('INTERNAL_SCRIPT')
  if (['unsupported', 'invalid', 'unavailable'].includes(status)) {
    expect(visibleText(row)).toContain('检查能力缺口')
    expect(visibleText(row)).toContain('不能据此认定成果错误')
  }
  if (status === 'ready') expect(visibleText(row)).toContain('是否执行及通过以本机检查回执为准')
})

test.each(['passed', 'failed', 'unavailable'])('浏览器%s回执沿用系统检查详情，不直接显示报告JSON', async (outcome) => {
  await mount([event(2, 'verification.completed', 'local_verifier', {
    outcome, command: ['browser-check', '--plan', 'plan.json'], exit_code: outcome === 'passed' ? 0 : 2,
    browser_plan: { entrypoint: 'tetris.html', steps: [{ label: '<img src=x onerror=alert(1)>', expression: 'INTERNAL_SCRIPT' }] },
    browser_report: { outcome, error: '执行器实际回执说明。', internal: 'PRIVATE_REPORT' },
    stdout: { excerpt: '{"internal":"PRIVATE_REPORT"}' },
  }, 'system')])
  const row = record(2)
  expect(within(row).queryByRole('img')).toBeNull()
  if (outcome === 'passed') openDetails(row.querySelector('details')!)
  await act(async () => { await vi.dynamicImportSettled() })
  expect(visibleText(row)).toContain('浏览器入口：tetris.html')
  expect(visibleText(row)).toContain('执行器实际回执说明。')
  expect(visibleText(row)).toContain('<img src=x onerror=alert(1)>')
  expect(visibleText(row)).not.toMatch(/PRIVATE_REPORT|INTERNAL_SCRIPT/)
  if (outcome === 'unavailable') {
    expect(visibleText(row)).toContain('检查工具不可用')
    expect(visibleText(row)).toContain('不能据此认定成果错误')
    expect(visibleText(row)).not.toContain('命令失败')
  }
})

test('浏览器真实失败步骤和页面错误可见，截图只有摘要，截断明确提示', async () => {
  await mount([event(2, 'verification.completed', 'local_verifier', {
    outcome: 'failed', source: 'explicit_local_cli', command: ['browser-check'], exit_code: 1,
    browser_plan: { entrypoint: 'tetris.html', steps: [{ label: '方案描述不代表执行结果' }] },
    browser_report: {
      outcome: 'failed', error: 'one or more steps failed or were not run', truncated: true,
      steps: [
        { index: 1, action: 'click', label: '点击开始', status: 'passed' },
        { index: 2, action: 'assert_text', label: '检查分数', status: 'failed', actual: false, observed: '分数：0', error: '<img src=x onerror=alert(1)>' },
        { index: 3, action: 'press', label: '继续按键', status: 'not_run' },
      ],
      page_errors: ['ReferenceError: score is not defined'],
      blocked_requests: [{ kind: 'request', url: 'https://blocked.invalid/data' }],
      counts: { page_errors: 4, blocked_requests: 1, console_messages: 0 },
      screenshot: { sha256: 'a'.repeat(64), size: 456, path: 'TEMP_PRIVATE_SCREENSHOT.png' },
    },
    stdout: { complete: false, truncated: true, excerpt: '{"RAW_REPORT":"hidden"}' },
  }, 'system')])
  const row = record(2)
  await act(async () => { await vi.dynamicImportSettled() })
  const visible = visibleText(row)
  expect(visible).toContain('执行错误：one or more steps failed or were not run')
  expect(visible).toContain('执行步骤 1：通过')
  expect(visible).toContain('执行步骤 2：失败')
  expect(visible).toContain('检查分数')
  expect(visible).toContain('实际判断：不满足')
  expect(visible).toContain('实际观察：分数：0')
  expect(visible).toContain('错误：<img src=x onerror=alert(1)>')
  expect(visible).toContain('执行步骤 3：未执行')
  expect(visible).toContain('ReferenceError: score is not defined')
  expect(visible).toContain('页面错误总数：4')
  expect(visible).toContain('其余内容已省略或截断')
  expect(visible).toContain('https://blocked.invalid/data')
  expect(visible).toContain('执行器报告已截断')
  expect(visible).toContain('标准输出采集未完成')
  expect(visible).toContain('标准输出已截断')
  expect(visible).toContain(`SHA-256：${'a'.repeat(64)}`)
  expect(visible).toContain('大小：456 字节')
  expect(visible).toContain('临时文件会清理，不提供打开链接')
  expect(visible).not.toMatch(/TEMP_PRIVATE_SCREENSHOT|RAW_REPORT/)
  expect(row.querySelector('img, a[href*="SCREENSHOT"], a[href*="blocked.invalid"]')).toBeNull()
})

test('系统复核失败不冒充角色发言，也不挪用其他角色或工作项的判断', async () => {
  await mount([
    event(2, 'work.reviewed', 'store_manager', { work_item_id: 'other', status: 'accepted', checks: [
      { status: 'passed', reason: '这是另一项工作，不应显示在这里。' },
    ] }, 'agent'),
    event(3, 'msg.said', 'dev', { phase: 'review', work_item_id: 'layout', speech_source: 'system_receipt', review_seq: 2,
      text: '复核调用没有完整返回，需要核对。' }, 'agent'),
  ])
  const row = record(3)
  expect(within(row).queryByRole('img')).toBeNull()
  expect(visibleText(row)).not.toContain('另一项工作')
  expect(visibleText(row)).toContain('复核调用没有完整返回')
})

test.each([true, false])('历史通过回执缺少唯一关联时不借用理由（interleaved=%s）', async (interleaved) => {
  await mount([
    ...(interleaved ? [
      event(2, 'work.reviewed', 'dev', { work_item_id: 'layout', status: 'accepted', checks: [{ status: 'passed', reason: '旧版本理由' }] }, 'agent'),
      event(3, 'work.submitted', 'work_scheduler', { work_item_id: 'layout', plan_seq: 10 }),
    ] : []),
    event(4, 'msg.said', 'dev', { phase: 'review', work_item_id: 'layout',
      text: '这一项的阶段标准通过了，可以交给后面的伙伴接着做；整件任务还需要最后核对。' }, 'agent'),
  ])
  expect(visibleText(record(4))).toContain('历史回执待核对')
  expect(visibleText(record(4))).not.toMatch(/旧版本理由|阶段标准通过了/)
})

test.each([undefined, 'system'])('纪律记录（kind=%s）不再冒充成员，常规提醒默认收起且未知码只进入审计', async (kind) => {
  vi.useRealTimers()
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
  await act(async () => { await vi.dynamicImportSettled() })
  expect(visibleText(row)).not.toContain('unknown_internal_issue')
  openDetails((await within(audit).findByText('开发者详情')).closest('details')!)
  expect(visibleText(row)).toContain('discipline')
  expect(visibleText(row)).toContain('unknown_internal_issue')
  expect(JSON.parse(audit.querySelector('pre')!.textContent!)).toMatchObject({ seq: 2, type: 'agent.discipline_flagged', actor: { id: 'discipline' } })
  const humanAudit = within(record(1)).getByText('事件 #1 · 审计记录').closest('details')!
  openDetails(humanAudit)
  await act(async () => { await vi.dynamicImportSettled() })
  await within(humanAudit).findByText('开发者详情')
  expect(JSON.parse(humanAudit.querySelector('pre')!.textContent!)).toMatchObject({ payload: { goal: '核对聊天系统记录' } })
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

const gateway = (seq: number, type: string, payload: Record<string, unknown>): FixtureEvent => ({
  seq, type, actor: { id: 'driver_gateway', role: 'driver_gateway', kind: 'system' }, payload,
})
const retryFailure = () => gateway(2, 'driver.failed', { role: 'dev', driver: 'external_model', message: 'RAW_CONNECTION_ERROR', attempt: 1 })
const scheduledRetry = (payload = {}) => gateway(3, 'driver.retry_scheduled', {
  role: 'dev', driver: 'external_model', failure_seq: 2, retry_index: 1, retry_limit: 1, delay_seconds: 5,
  not_before: 1792022405, phase: 'work', work_item_id: 'board', scope: { digest: 'INTERNAL_SCOPE' }, reason: '等待后自动续试一次。', ...payload,
})
const finishedRetry = (status: string, payload = {}) => gateway(4, 'driver.retry_finished', {
  request_seq: 3, role: 'dev', driver: 'external_model', status, reason: '续试调用结果已记录。', ...payload,
})

test('自动续试等待沿用轻量系统提示，显示伙伴名并保持聊天和暂停可用', async () => {
  const stream = await mount([retryFailure(), scheduledRetry()])
  const row = record(3)
  expect(stream.querySelectorAll('article')).toHaveLength(2)
  expect(row.classList.contains('system-record-row')).toBe(true)
  expect(row.classList.contains('needs-attention')).toBe(false)
  expect(row.querySelector('[data-actor="system"]')).toBeTruthy()
  expect(within(row).queryByRole('img')).toBeNull()
  expect(visibleText(row)).toContain('@米寇的模型暂时连接不稳定')
  expect(visibleText(row)).toContain('至少间隔 5 秒')
  expect(visibleText(row)).toContain('结果返回后会在这里更新')
  expect(visibleText(row)).toContain('仍可发消息或暂停任务')
  expect(visibleText(stream)).not.toMatch(/RAW_CONNECTION_ERROR|external_model|INTERNAL_SCOPE|dev|任务完成/)
  expect(row.querySelectorAll('details')).toHaveLength(1)
  expect(row.querySelector('details')!.open).toBe(false)
  fireEvent.change(screen.getByRole('textbox', { name: '向团队发送消息' }), { target: { value: '请继续保留现有成果' } })
  expect((screen.getByRole('button', { name: '发送消息' }) as HTMLButtonElement).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '更多任务操作' }))
  expect((screen.getByRole('button', { name: '暂停执行' }) as HTMLButtonElement).disabled).toBe(false)
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
})

test.each([
  ['succeeded', '模型调用已恢复', false],
  ['failed', '模型续试仍失败，已保留检查点', true],
  ['cancelled', '自动续试等待已取消', false],
] as const)('轮询到 %s 后原位更新等待提示，不遗留即将续试或永久故障', async (status, expected, attention) => {
  const events = [retryFailure(), scheduledRetry()]
  const stream = await mount(events)
  const row = record(3)
  expect(visibleText(row)).toContain('至少间隔 5 秒')
  events.push(finishedRetry(status))
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  expect(record(3)).toBe(row)
  expect(stream.querySelectorAll('article')).toHaveLength(2)
  expect(visibleText(row)).toContain(expected)
  expect(visibleText(row)).not.toMatch(/至少间隔|正在自动恢复连接|即将|RAW_CONNECTION_ERROR|任务已完成|验收通过/)
  expect(row.classList.contains('needs-attention')).toBe(attention)
  expect(Boolean(row.querySelector('.lucide-triangle-alert'))).toBe(attention)
  expect(vi.mocked(fetch).mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
})

test('重试审计可展开原始失败、安排和结束记录，并使用显示名', async () => {
  vi.useRealTimers()
  await mount([retryFailure(), scheduledRetry(), finishedRetry('succeeded')])
  const row = record(3)
  openDetails(row.querySelector('details')!)
  await act(async () => { await vi.dynamicImportSettled() })
  expect(within(row).getByRole('heading', { name: '模型自动续试安排' })).toBeTruthy()
  expect(within(row).getByRole('heading', { name: '模型自动续试结果' })).toBeTruthy()
  expect(within(row).getAllByText('@米寇')).toHaveLength(2)
  expect(visibleText(row)).toContain('RAW_CONNECTION_ERROR')
  expect(visibleText(row)).toContain('续试调用结果已记录。')
  expect(visibleText(row)).not.toContain('INTERNAL_SCOPE')
  const raw = within(row).getAllByText('开发者详情')
  expect(raw).toHaveLength(3)
  raw.forEach(summary => expect(summary.closest('details')!.open).toBe(false))
  openDetails(raw[1].closest('details')!)
  expect(visibleText(row)).toContain('INTERNAL_SCOPE')
})

test('快照数组乱序仍按事件序号关联恢复', async () => {
  const stream = await mount([finishedRetry('succeeded'), scheduledRetry(), retryFailure()])
  expect(visibleText(record(3))).toContain('@米寇的模型调用已恢复')
  expect(visibleText(stream)).not.toContain('至少间隔')
  expect(stream.querySelectorAll('article')).toHaveLength(2)
})

test.each([
  { actor: { id: 'dev', role: 'driver_gateway', kind: 'agent' } },
  { actor: { id: 'dev', role: 'driver_gateway', kind: 'system' } },
  { actor: { id: 'driver_gateway', role: 'worker', kind: 'system' } },
  { actor: { id: 'driver_gateway', role: 'driver_gateway', kind: 'agent' } },
  { actor: { id: 'driver_gateway', role: 'driver_gateway' } },
  { seq: 2 },
  { payload: { request_seq: 999, role: 'dev', driver: 'external_model', status: 'succeeded' } },
  { payload: { request_seq: '3', role: 'dev', driver: 'external_model', status: 'succeeded' } },
  { payload: { request_seq: 3, role: 'test', driver: 'external_model', status: 'succeeded' } },
  { payload: { request_seq: 3, role: 'dev', driver: 'other_model', status: 'succeeded' } },
  { payload: { request_seq: 3, role: 'dev', driver: 'external_model', status: ['succeeded'] } },
])('来源或关联不正确的回执不能伪装恢复：%j', async (overrides) => {
  const stream = await mount([scheduledRetry(), { ...finishedRetry('succeeded'), ...overrides }])
  expect(visibleText(record(3))).toContain('至少间隔 5 秒')
  expect(visibleText(stream)).not.toContain('已恢复')
  expect(visibleText(stream)).toContain('续试回执待核对')
  expect(stream.querySelector('[data-actor="dev"]')).toBeNull()
})

test('取消后到达的矛盾成功回执不能把等待改为恢复', async () => {
  const stream = await mount([scheduledRetry(), finishedRetry('cancelled'), { ...finishedRetry('succeeded'), seq: 5 }])
  expect(visibleText(record(3))).toContain('等待已取消')
  expect(visibleText(stream)).not.toContain('已恢复')
})

test('没有安排的成功回执和伪造成员安排不能展示恢复或自动续试承诺', async () => {
  const stream = await mount([finishedRetry('succeeded'), { ...scheduledRetry(), actor: { id: 'dev', kind: 'agent', role: 'worker' } }])
  expect(visibleText(stream)).not.toMatch(/已恢复|至少间隔/)
  expect(within(stream).getAllByText(/续试回执待核对/)).toHaveLength(2)
  expect(stream.querySelector('[data-actor="dev"]')).toBeNull()
})

test('未知伙伴使用自然中文兜底，不泄漏机器角色名或吞掉无关失败', async () => {
  const stream = await mount([retryFailure(), scheduledRetry({ role: 'worker_internal_7' })])
  expect(visibleText(record(3))).toContain('一位伙伴的模型暂时连接不稳定')
  expect(visibleText(stream)).not.toContain('worker_internal_7')
  expect(visibleText(record(2))).toContain('RAW_CONNECTION_ERROR')
})
