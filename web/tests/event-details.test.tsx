import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'

const event = (type: string, payload: Record<string, unknown> = {}, actor = { id: 'work_scheduler', role: 'work_scheduler', kind: 'system' }): DetailEvent => ({ seq: 124, type, actor, payload })
const names = (id: string) => ({ dev: '米寇', test: '复核伙伴', work_scheduler: '任务调度' }[id] ?? id)
const ordinary = (container: HTMLElement) => {
  const copy = container.cloneNode(true) as HTMLElement
  copy.querySelectorAll('details').forEach(element => element.remove())
  return copy.textContent ?? ''
}
const receipt = (payload: Record<string, unknown> = {}) => event('verification.completed', {
  source: 'explicit_local_cli', outcome: 'passed', exit_code: 0, command: ['python', 'check.py', '--name', 'two words', ''],
  cwd: 'C:\\project', duration_ms: 250, work_item_id: 'board', criterion_id: 'behavior', artifacts_unchanged: true,
  stdout: { bytes: 4, complete: true, truncated: false, excerpt: '通过三项检查' }, stderr: { bytes: 0, complete: true, truncated: false },
  ...payload,
}, { id: 'local_verifier', role: 'local_verifier', kind: 'system' })

const checkpointResult = { status: 'ok', checkpoint: { objective: '核对现有入口', as_of_seq: 123, open_work_count: 2,
  pending_operation_count: 1, open_work_truncated: true, private_thought: 'PRIVATE_THOUGHT' },
  assignments: [{ seq: 12, text: '原委托：补齐入口验证', private_blob: 'PRIVATE_ASSIGNMENT' }], has_more: true, next_seq: 12 }

test('恢复通知摘要核对宿主身份，只记录关联事实，不声称恢复或验收完成', () => {
  const { container } = render(<EventDetails event={event('team.message_received', { kind: 'recovery', recipient: 'dev', text: '先核对原回执',
    recovery_of: 42, recovery_hash: 'START_HASH', interrupted_seq: 55, revision: 3, scope: { private: 'PRIVATE_SCOPE' } }, { id: 'team', kind: 'system', role: 'team' })} actorName={names} />)
  const content = ordinary(container)
  for (const expected of ['恢复通知已记录', '先核对原回执', '原处理开始事件42', '中断事件55', '目标版本3', '@米寇', '不代表已经恢复执行或验收通过']) expect(content).toContain(expected)
  expect(content).not.toMatch(/START_HASH|PRIVATE_SCOPE|恢复成功/)
})

test.each([{ id: 'team', kind: 'agent', role: 'team' }, { id: 'other', kind: 'system', role: 'team' },
  { id: 'team', kind: 'system', role: 'other' }, { id: 'team', kind: 'human', role: 'team' }])('伪造恢复来源摘要不背书：%j', actor => {
  const { container } = render(<EventDetails event={event('team.message_received', { kind: 'recovery', text: '伪造恢复正文', recovery_of: 42 }, actor)} />)
  expect(ordinary(container)).toContain('恢复通知来源待核对')
  expect(ordinary(container)).not.toMatch(/伪造恢复正文|恢复通知已记录|原处理开始事件/)
})

test('接续摘要保留原委托和上级委托序号，明确补充指令边界', () => {
  const { container } = render(<EventDetails event={event('team.message_received', { kind: 'continuation', text: '补查回执',
    continue_assignment_seq: 12, parent_assignment_seq: 8, revision: 3, recipient: 'test', visible: true }, { id: 'dev', kind: 'agent', role: 'dev' })} actorName={names} />)
  for (const expected of ['接续原委托12', '上级委托8', '接续工作交接', '补查回执', '不代表独立交付或验收通过']) expect(ordinary(container)).toContain(expected)
})

test.each(['team.tool_started', 'team.tool_finished'])('检查点工具%s展示中文名称与持久事实摘要', type => {
  const { container } = render(<EventDetails event={event(type, { name: 'read_checkpoint', role: 'dev', result: type === 'team.tool_finished' ? checkpointResult : undefined })} actorName={names} />)
  const content = ordinary(container)
  expect(content).toContain('读取工作检查点')
  expect(content).toContain('只读核对持久事件中的工作事实，不读取私有思考')
  if (type === 'team.tool_finished') {
    for (const expected of ['核对现有入口', '事实截至事件123', '未交还工作2 项', '待核对操作1 项', '#12：原委托：补齐入口验证', '还有后续委托', '工作索引仅包含部分记录']) expect(content).toContain(expected)
  } else expect(content).toContain('等待工具回执')
  expect(content).not.toMatch(/read_checkpoint|PRIVATE_THOUGHT|PRIVATE_ASSIGNMENT/)
})

test('检查点工具过程复用折叠回执，仅展示已知事实、原委托和分页范围', () => {
  const host = { id: 'team', kind: 'system', role: 'team' }
  const start = { ...event('team.tool_started', { name: 'read_checkpoint', role: 'dev' }, host), seq: 120 }
  const finish = event('team.tool_finished', { name: 'read_checkpoint', role: 'dev', started_seq: 120, result: checkpointResult }, host)
  const { container } = render(<TeamToolProgress events={[start, finish]} names={{ dev: '米寇' }} />)
  const fold = screen.getByText('工具过程 · 1 项').closest('details')!
  expect(fold.open).toBe(false)
  for (const expected of ['读取工作检查点', '工具已完成', '只读核对持久事件中的工作事实', '事实截至事件 #123', '未交还工作：2 项', '待核对操作：1 项', '原委托 #12：原委托：补齐入口验证', '下一页从 #12 之后读取', '工作索引仅显示部分记录']) expect(container.textContent).toContain(expected)
  expect(container.textContent).not.toMatch(/read_checkpoint|PRIVATE_THOUGHT|PRIVATE_ASSIGNMENT/)
})

test.each(['correcting', 'ready', 'unsupported', 'invalid', 'unavailable', 'stale'])('浏览器方案%s呈现中文记录与执行边界', (status) => {
  const { container } = render(<EventDetails event={event('verification.planned', { status,
    reason: '检查方案说明。', plan: { entrypoint: 'tetris.html', steps: [{ label: '检查暂停', expression: 'PRIVATE_SCRIPT' }] },
  })} />)
  expect(ordinary(container)).toContain('浏览器检查方案')
  expect(ordinary(container)).toContain('检查方案说明。')
  expect(ordinary(container)).toContain('tetris.html')
  expect(ordinary(container)).toContain('检查暂停')
  expect(ordinary(container)).not.toContain('PRIVATE_SCRIPT')
  expect(ordinary(container)).toContain(status === 'correcting' ? '检查方案正在校正，尚未执行' : status === 'ready' ? '方案不是执行回执' : '不能据此认定成果错误')
  if (status === 'correcting') expect(ordinary(container)).not.toMatch(/检查能力缺口|工具不可用|方案无效/)
  expect(screen.getByText('开发者详情').closest('details')!.open).toBe(false)
})

test('浏览器回执安全展示已知字段，工具不可用不是成果错误', () => {
  const { container } = render(<EventDetails event={receipt({ outcome: 'unavailable', exit_code: 2,
    browser_plan: { entrypoint: 'tetris.html', steps: [null, { label: '<script>alert(1)</script>', expression: 'PRIVATE_SCRIPT' }] },
    browser_report: { outcome: 'unavailable', error: '未找到浏览器执行器。', private: 'PRIVATE_REPORT' },
    stdout: { complete: true, excerpt: '{"private":"PRIVATE_REPORT"}' },
  })} />)
  expect(ordinary(container)).toContain('检查工具不可用')
  expect(ordinary(container)).toContain('不能据此认定成果错误')
  expect(ordinary(container)).toContain('未找到浏览器执行器。')
  expect(ordinary(container)).toContain('<script>alert(1)</script>')
  expect(ordinary(container)).not.toMatch(/PRIVATE_SCRIPT|PRIVATE_REPORT|命令失败/)
  expect(container.querySelector('script')).toBeNull()
  expect(screen.getByText('开发者详情').closest('details')!.open).toBe(false)
})

test('浏览器审计显示runner实际步骤、错误、观察与截图摘要，不漏掉隐藏stdout的截断提示', () => {
  const { container } = render(<EventDetails event={receipt({ outcome: 'failed', exit_code: 1,
    browser_report: {
      outcome: 'failed', error: 'page reported uncaught JavaScript errors', truncated: true,
      steps: [
        { index: 1, action: 'assert_visible', label: '检查棋盘', status: 'passed', actual: true },
        { index: 2, action: 'assert_text', label: '检查提示', status: 'failed', actual: false, observed: '', error: '未找到预期文本' },
        { index: 3, action: 'press', label: '后续操作', status: 'not_run' },
      ],
      page_errors: ['<script>throw new Error()</script>'],
      blocked_requests: [{ kind: 'websocket', url: 'wss://blocked.invalid' }],
      console_messages: [{ type: 'error', text: '资源加载失败' }],
      counts: { page_errors: 7, blocked_requests: 2, console_messages: 1 },
      screenshot: { sha256: 'b'.repeat(64), size: 1234, path: 'TEMP_ONLY.png' },
      screenshot_error: '截图清理前记录异常', private: 'HIDDEN_REPORT',
    }, stdout: { excerpt: '{"HIDDEN_REPORT":true}', complete: false, truncated: true },
  })} />)
  const visible = ordinary(container)
  expect(visible).toContain('page reported uncaught JavaScript errors')
  expect(visible).toContain('检查棋盘')
  expect(visible).toContain('实际判断：满足')
  expect(visible).toContain('实际判断：不满足')
  expect(visible).toContain('实际观察：（空文本）')
  expect(visible).toContain('未找到预期文本')
  expect(visible).toContain('未执行')
  expect(visible).toContain('<script>throw new Error()</script>')
  expect(visible).toContain('wss://blocked.invalid')
  expect(visible).toContain('资源加载失败')
  expect(visible).toContain('已显示 1 条；其余内容已省略或截断')
  expect(visible).toContain('执行器报告已截断')
  expect(visible).toContain('采集未完成；输出已截断')
  expect(visible).toContain(`SHA-256：${'b'.repeat(64)}`)
  expect(visible).toContain('1234 字节')
  expect(visible).toContain('截图清理前记录异常')
  expect(visible).not.toMatch(/TEMP_ONLY|HIDDEN_REPORT/)
  expect(container.querySelector('img, script, a')).toBeNull()
  expect(screen.getByText('开发者详情').closest('details')!.open).toBe(false)
})

test('浏览器报告字段异常不崩溃，界面省略长内容时明确提示', () => {
  const { container } = render(<EventDetails event={receipt({ browser_report: {
    outcome: 'passed', steps: [null, { label: '步'.repeat(250), status: '__proto__', actual: {}, observed: {}, error: '错'.repeat(1300) },
      ...Array.from({ length: 20 }, () => ({ status: 'not_run' }))],
    page_errors: Array.from({ length: 9 }, () => '页面异常'), blocked_requests: [null],
    counts: { page_errors: 9 }, screenshot: { sha256: '<img src=x>', size: -1, path: 'TEMP_ONLY' },
  } })} />)
  const visible = ordinary(container)
  expect(visible).toContain('命令通过')
  expect(visible).toContain('状态待核对')
  expect(visible).toContain('显示已截断')
  expect(visible).toContain('仅显示前 20 项，共 22 项')
  expect(visible).toContain('已显示 8 条；其余内容已省略或截断')
  expect(visible).not.toMatch(/__proto__|\[object Object\]|<img src=x>|TEMP_ONLY/)
})

test('浏览器检查规划开始不意味着已经执行', () => {
  const { container } = render(<EventDetails event={event('verification.planning_started', { criterion_id: 'keyboard' })} />)
  expect(ordinary(container)).toContain('正在准备浏览器检查')
  expect(ordinary(container)).toContain('不代表浏览器已经执行')
})

test('咨询请求只显示实际问题和对象，不假称对方收到或执行', () => {
  const { container } = render(<EventDetails actorName={names} event={event('collaboration.requested', {
    requester: 'dev', recipient: 'test', question: '这项约束应如何检查？', scope: { private: 'INTERNAL_SCOPE' },
  }, { id: 'dev', role: 'dev', kind: 'agent' })} />)
  expect(ordinary(container)).toContain('@复核伙伴')
  expect(ordinary(container)).toContain('这项约束应如何检查？')
  expect(ordinary(container)).not.toMatch(/INTERNAL_SCOPE|对方已收到|工具已执行/)
})

test('真实咨询回复证据默认收起，恶意文本不会变成HTML', () => {
  const { container } = render(<EventDetails actorName={names} event={event('collaboration.finished', {
    request_seq: 100, status: 'answered', recipient: 'test', speaker_id: 'test', reply: '需要对照棋盘源码。',
    evidence: [{ artifact_seq: 20, quote: '<img src=x onerror=alert(1)>' }],
  }, { id: 'collaboration', role: 'collaboration', kind: 'system' })} />)
  expect(ordinary(container)).toContain('需要对照棋盘源码。')
  const evidence = screen.getByText('咨询引用依据').closest('details')!
  expect(evidence.open).toBe(false)
  fireEvent.click(evidence.querySelector('summary')!)
  expect(evidence.textContent).toContain('成果 #20')
  expect(evidence.textContent).toContain('<img src=x onerror=alert(1)>')
  expect(container.querySelector('img')).toBeNull()
  expect(screen.getByText('开发者详情').closest('details')!.open).toBe(false)
})

test.each(['invalid', 'unavailable', 'stale'])('咨询%s的无效reply只留在开发者记录', (status) => {
  const { container } = render(<EventDetails event={event('collaboration.finished', {
    status, recipient: 'dev', speaker_id: 'dev', reply: 'UNTRUSTED_REPLY', reason: '咨询未完成',
  }, { id: 'collaboration', role: 'collaboration', kind: 'system' })} />)
  expect(ordinary(container)).toContain('咨询未完成')
  expect(ordinary(container)).not.toContain('UNTRUSTED_REPLY')
})

test('具体工作目标与负责人一一对应，不复制完整工作契约', () => {
  const { container } = render(<EventDetails actorName={names} event={event('work.plan_created', {
    items: [{ id: 'board', owner: 'dev', reviewer: 'test', objective: '实现五子棋棋盘', secret: 'NESTED_PRIVATE' },
      { id: 'rules', owner: 'test', objective: '验证连珠判定' }], scope: { digest: 'PRIVATE_SCOPE' }, prompt: 'PRIVATE_PROMPT',
  })} />)
  expect(screen.getByRole('heading', { name: '工作计划已建立' })).toBeTruthy()
  const first = screen.getByText('工作 1').parentElement!
  expect(first.textContent).toContain('实现五子棋棋盘')
  expect(first.textContent).toContain('负责人：@米寇')
  expect(first.textContent).toContain('复核人：@复核伙伴')
  expect(screen.getByText('工作 2').parentElement!.textContent).toContain('验证连珠判定')
  expect(ordinary(container)).not.toMatch(/NESTED_PRIVATE|PRIVATE_SCOPE|PRIVATE_PROMPT/)
})

test('单项指派显示实际目标、负责人和验收要求', () => {
  render(<EventDetails actorName={names} event={event('subtask.assigned', { owner: 'dev', objective: '补齐悔棋功能', acceptance: '可撤销最后一步', status: 'assigned' })} />)
  expect(screen.getByText('补齐悔棋功能')).toBeTruthy()
  expect(screen.getByText('@米寇')).toBeTruthy()
  expect(screen.getByText('可撤销最后一步')).toBeTruthy()
  expect(screen.getByText('已指派')).toBeTruthy()
})

test.each([
  ['work.claimed', {}, '已领取', '尚无本次执行结果'],
  ['work.submitted', { artifact_seqs: [120] }, '待验收', '不代表运行通过'],
  ['work.check_started', { criterion_id: 'launch' }, '检查中', '尚无本次检查的完成回执'],
  ['agent.working', { summary: '正在整理实现路径' }, '处理中', '不是工具执行回执'],
  ['activity.completed', { action: 'run' }, '本轮结束', '不代表工作已验收'],
])('生命周期 %s 不冒充执行通过', (type, payload, status, boundary) => {
  const { container } = render(<EventDetails event={event(type, payload)} />)
  expect(screen.getByText(status)).toBeTruthy()
  expect(ordinary(container)).toContain(boundary)
  expect(ordinary(container)).not.toContain('命令通过')
})

test('真实本机回执展示参数边界、退出码、耗时和采集正文', () => {
  const { container } = render(<EventDetails event={receipt()} />)
  expect(screen.getByText('命令通过')).toBeTruthy()
  expect(screen.getByText('python')).toBeTruthy()
  expect(screen.getByText(/3\. two words/).textContent).toContain('4. （空参数）')
  expect(screen.getByText('退出码').parentElement!.textContent).toBe('退出码0')
  expect(screen.getByText('250 毫秒')).toBeTruthy()
  expect(screen.getByText('通过三项检查')).toBeTruthy()
  expect(screen.getByText('无输出。')).toBeTruthy()
  expect(ordinary(container)).toContain('不代表应用已部署')
  expect(container.querySelectorAll('pre')).toHaveLength(1)
})

test.each([
  [{ outcome: 'timeout', exit_code: null }, '执行超时'],
  [{ outcome: 'launch_error', launch_error: '找不到解释器', exit_code: null }, '无法启动'],
  [{ outcome: 'failed', exit_code: 1 }, '命令失败'],
  [{ outcome: 'new_status', exit_code: null }, '检查结果待核对'],
  [{ exit_code: null }, '回执信息不足或不一致'],
  [{ exit_code: true }, '回执信息不足或不一致'],
  [{ artifacts_unchanged: false }, '回执信息不足或不一致'],
  [{ cleanup_error: '子进程未清理' }, '回执信息不足或不一致'],
  [{ stdout: { complete: false, truncated: true } }, '回执信息不足或不一致'],
  [{ command: undefined }, '回执信息不足或不一致'],
])('矛盾或失败回执不会显示命令通过：%j', (payload, status) => {
  render(<EventDetails event={receipt(payload)} />)
  expect(screen.getByText(status)).toBeTruthy()
  expect(screen.queryByText('命令通过')).toBeNull()
})

test('成员自报和缺失来源不能伪装成系统验证回执', () => {
  const fake = { ...receipt(), actor: { id: 'dev', role: 'worker', kind: 'agent' } }
  const { rerender } = render(<EventDetails event={fake} />)
  expect(screen.getByText('回执来源待核对')).toBeTruthy()
  rerender(<EventDetails event={receipt({ source: undefined })} />)
  expect(screen.getByText('回执来源待核对')).toBeTruthy()
  expect(screen.queryByText('命令通过')).toBeNull()
})

test('失败原因与未知执行结果分别展示', () => {
  const { rerender } = render(<EventDetails event={event('work.failed', { uncertain: true, reason: '调用中断，无法确认是否执行' })} />)
  expect(screen.getByText('执行结果待确认')).toBeTruthy()
  expect(screen.getByText('调用中断，无法确认是否执行')).toBeTruthy()
  rerender(<EventDetails event={event('driver.failed', { message: '模型连接超时', circuit_open: true, error_type: 'SECRET_INTERNAL_STACK' })} />)
  expect(screen.getByText('模型连接超时')).toBeTruthy()
  expect(screen.getByText('该模型通道已暂停调用')).toBeTruthy()
})

test('阶段核对保持未知检查，不能扩展成整体验收', () => {
  const { container } = render(<EventDetails event={event('work.reviewed', { status: 'submitted', reason: '等待实际回执',
    checks: [{ id: 'launch', kind: 'local_check', status: 'unknown', reason: '尚无可用执行器' }] })} />)
  expect(screen.getByText('等待补齐验收依据')).toBeTruthy()
  expect(ordinary(container)).toContain('尚无可用执行器')
  expect(ordinary(container)).toContain('依据不足')
  expect(ordinary(container)).not.toContain('整体验收通过')
})

test.each([
  ['planning.finished', { status: 'needs_input', reason: '需要补充目标' }, '等待补充信息', '需要补充目标'],
  ['question.finished', { status: 'insufficient', answer: '尚无启动方式依据', limits: ['没有执行记录'] }, '依据不足', '尚无启动方式依据'],
  ['delivery.review_finished', { status: 'rework', reason: '集成标准未满足' }, '需要返工', '集成标准未满足'],
  ['human.commanded', { instruction: '我怎么运行', intent: 'direction' }, '调整要求', '我怎么运行'],
])('按记录展示 %s，不重写历史语义', (type, payload, status, content) => {
  render(<EventDetails event={event(type, payload)} />)
  expect(screen.getByText(status)).toBeTruthy()
  expect(screen.getByText(content)).toBeTruthy()
})

test('成果只展示元数据，不把正文或内部字段复制成摘要', () => {
  const { container } = render(<EventDetails event={event('deliverable.published', {
    title: '五子棋网页', summary: '可下载的独立源码', filename: 'gomoku.html', content: '<canvas>PRIVATE_BODY</canvas>',
    uri: 'https://private.invalid', source: 'external', tokens: 99, api_key: 'PRIVATE_KEY',
  })} />)
  expect(screen.getByText('gomoku.html')).toBeTruthy()
  expect(screen.getByText('可下载的独立源码')).toBeTruthy()
  expect(ordinary(container)).not.toMatch(/PRIVATE_BODY|private.invalid|PRIVATE_KEY|tokens/)
  expect(container.querySelector('a')).toBeNull()
})

test.each(['future.unknown', '__proto__', 'constructor'])('未知事件 %s 使用中文兜底，不倾倒 payload', type => {
  const { container } = render(<EventDetails event={event(type, { summary: 'SECRET_ENGLISH '.repeat(1000), reason: 'PRIVATE_REASON', status: 'passed' })} />)
  expect(screen.getByRole('heading', { name: '事件记录' })).toBeTruthy()
  expect(ordinary(container)).toContain('暂无对应的中文摘要')
  expect(ordinary(container)).not.toMatch(/SECRET_ENGLISH|PRIVATE_REASON|passed|future\.unknown|__proto__|constructor/)
})

test('未知身份和名称解析失败使用中文兜底，不显示内部英文 ID 或角色', () => {
  const current = event('future.internal', { summary: 'PRIVATE_SUMMARY' }, { id: 'internal_daemon', role: 'internal_role', kind: 'unexpected_kind' })
  const { container, rerender } = render(<EventDetails actorName={id => id} event={current} />)
  expect(ordinary(container)).toContain('记录来源待核对')
  expect(ordinary(container)).not.toMatch(/internal_daemon|internal_role|unexpected_kind|PRIVATE_SUMMARY|future.internal/)
  rerender(<EventDetails event={event('subtask.assigned', { owner: 'unknown_worker', reviewer: 'test', objective: '整理验证说明' })} />)
  expect(screen.getByText('@未命名成员')).toBeTruthy()
  expect(screen.getByText('@验证成员')).toBeTruthy()
  expect(ordinary(container)).not.toContain('unknown_worker')
})

test('没有字段展示契约的已知事件不默认展示内部 summary 或 reason', () => {
  const { container } = render(<EventDetails actorName={id => id} event={event('task.started', {
    summary: 'INTERNAL_SUMMARY '.repeat(100), reason: 'INTERNAL_REASON', organization_fingerprint: 'INTERNAL_HASH',
  })} />)
  expect(screen.getByRole('heading', { name: '任务已启动' })).toBeTruthy()
  expect(ordinary(container)).not.toMatch(/INTERNAL_|work_scheduler/)
})

test('恶意字符串只作为文本展示，不创建 HTML、链接或脚本', () => {
  const malicious = '<img src="https://evil.invalid" onerror="alert(1)"><script>alert(1)</script> [点击](javascript:alert(1))'
  const current = event('work.failed', { reason: malicious, uncertain: false, hidden: '<iframe src="https://evil.invalid">' })
  const { container } = render(<EventDetails actorName={() => malicious} event={current} />)
  expect(ordinary(container)).toContain(malicious)
  expect(container.querySelector('img, script, iframe, a, [onerror]')).toBeNull()
  expect(container.querySelector('pre')!.textContent).toBe(JSON.stringify(current, null, 2))
})

test('异常字段类型和未知状态不会崩溃或生成对象文本', () => {
  const { container, rerender } = render(<EventDetails event={event('work.plan_created', { items: [null, 42, [], { objective: { secret: 'HIDDEN' }, owner: [] }] })} />)
  expect(ordinary(container)).not.toMatch(/HIDDEN|\[object Object\]/)
  rerender(<EventDetails event={event('work.reviewed', { status: '__proto__', reason: ['HIDDEN'], checks: [null] })} />)
  expect(within(container.querySelector('header')!).getByText('状态待核对')).toBeTruthy()
  expect(ordinary(container)).not.toMatch(/HIDDEN|\[object Object\]/)
  rerender(<EventDetails event={receipt({ command: ['python', { secret: 'HIDDEN' }], stderr: ['HIDDEN'] })} />)
  expect(screen.getByText('命令未记录或格式无效。')).toBeTruthy()
  expect(ordinary(container)).not.toContain('HIDDEN')
})

test('长文本有界，完整原始事件仅在默认关闭的开发者详情中', () => {
  const current = event('driver.failed', { message: '故障'.repeat(3000), internal_trace: 'PRIVATE_TRACE' })
  const { container } = render(<EventDetails event={current} />)
  const details = screen.getByText('开发者详情').closest('details')!
  expect(details.open).toBe(false)
  expect(details.hasAttribute('open')).toBe(false)
  expect(within(details).getByLabelText('原始事件 JSON').textContent).toBe(JSON.stringify(current, null, 2))
  expect(ordinary(container).length).toBeLessThan(1600)
  expect(ordinary(container)).toContain('已省略')
  expect(ordinary(container)).not.toContain('PRIVATE_TRACE')
  fireEvent.click(screen.getByText('开发者详情'))
  expect(details.open).toBe(true)
})

test('长目标不会挤掉负责人，过多工作项有明确省略提示', () => {
  const { container } = render(<EventDetails actorName={names} event={event('work.plan_created', {
    items: Array.from({ length: 20 }, () => ({ objective: '很长的目标'.repeat(1000), owner: 'dev', reviewer: 'test' })),
  })} />)
  expect(screen.getByText('工作 1').parentElement!.textContent).toContain('负责人：@米寇')
  expect(screen.getByText('工作 1').parentElement!.textContent).toContain('复核人：@复核伙伴')
  expect(ordinary(container)).toContain('另有 12 项工作未展开')
  expect(ordinary(container).length).toBeLessThan(8000)
})

test('切换事件时开发者详情重新收起', () => {
  const { rerender } = render(<EventDetails event={event('task.started')} />)
  fireEvent.click(screen.getByText('开发者详情'))
  expect(screen.getByText('开发者详情').closest('details')!.open).toBe(true)
  rerender(<EventDetails event={{ ...event('work.claimed'), seq: 125 }} />)
  expect(screen.getByText('开发者详情').closest('details')!.open).toBe(false)
})

const driverActor = { id: 'driver_gateway', role: 'driver_gateway', kind: 'system' }

test('续试安排用中文展示伙伴、延迟和关联信息，范围原文收进开发者详情', () => {
  const current = event('driver.retry_scheduled', {
    role: 'dev', driver: 'external_model', failure_seq: 120, retry_index: 1, retry_limit: 1,
    delay_seconds: 5, not_before: 1792022405, phase: 'work', work_item_id: 'board',
    scope: { digest: 'PRIVATE_SCOPE' }, reason: '连接暂时不稳定，等待后续试。',
  }, driverActor)
  const { container } = render(<EventDetails event={current} actorName={names} />)
  expect(screen.getByRole('heading', { name: '模型自动续试安排' })).toBeTruthy()
  expect(screen.getByText('@米寇')).toBeTruthy()
  expect(screen.getByText('5 秒')).toBeTruthy()
  expect(screen.getByText('原失败事件').parentElement!.textContent).toContain('120')
  expect(screen.getByText('本次续试').parentElement!.textContent).toContain('1')
  expect(screen.getByText('续试上限').parentElement!.textContent).toContain('1')
  expect(screen.getByText('最早续试时间').parentElement!.textContent).toMatch(/2026/)
  expect(screen.getByText('执行工作')).toBeTruthy()
  expect(screen.getByText('board')).toBeTruthy()
  expect(ordinary(container)).toContain('仍可发消息或暂停任务')
  expect(ordinary(container)).not.toMatch(/PRIVATE_SCOPE|\{"|driver.retry_scheduled/)
  expect(screen.getByText('开发者详情').closest('details')!.open).toBe(false)
  expect(screen.getByLabelText('原始事件 JSON').textContent).toBe(JSON.stringify(current, null, 2))
})

test.each([
  ['succeeded', '模型调用已恢复'], ['failed', '续试仍失败，已保留检查点'], ['cancelled', '续试等待已取消'], ['unknown', '状态待核对'],
])('续试结果 %s 显示中文状态与原因，不表示验收完成', (status, expected) => {
  const { container } = render(<EventDetails actorName={names} event={event('driver.retry_finished', {
    request_seq: 123, role: 'test', driver: 'external_model', status, reason: '已有成果保留。',
  }, driverActor)} />)
  expect(screen.getByRole('heading', { name: '模型自动续试结果' })).toBeTruthy()
  expect(screen.getByText(expected)).toBeTruthy()
  expect(screen.getByText('@复核伙伴')).toBeTruthy()
  expect(screen.getByText('续试安排事件').parentElement!.textContent).toContain('123')
  expect(screen.getByText('已有成果保留。')).toBeTruthy()
  expect(ordinary(container)).toContain('不代表任务完成或验收通过')
})

test.each([
  { id: 'dev', role: 'driver_gateway', kind: 'agent' },
  { id: 'driver_gateway', role: 'worker', kind: 'system' },
  { id: 'dev', role: 'driver_gateway', kind: 'system' },
])('非网关回执不能在详情中冒充已恢复：%j', actor => {
  const { container } = render(<EventDetails event={event('driver.retry_finished', { status: 'succeeded', request_seq: 123, role: 'dev' }, actor)} />)
  expect(screen.getByText('回执来源待核对')).toBeTruthy()
  expect(ordinary(container)).not.toContain('已恢复')
})

test('续试详情的未知角色和异常字段不会泄漏机器名或生成 JSON 正文', () => {
  const { container } = render(<EventDetails actorName={id => id} event={event('driver.retry_scheduled', {
    role: 'internal_worker', driver: { secret: 'PRIVATE_DRIVER' }, delay_seconds: '5', not_before: 1e100,
    reason: { secret: 'PRIVATE_REASON' }, phase: 'internal_phase', scope: { secret: 'PRIVATE_SCOPE' },
  }, driverActor)} />)
  expect(ordinary(container)).toContain('@未命名成员')
  expect(ordinary(container)).not.toMatch(/internal_worker|internal_phase|PRIVATE_|\[object Object\]/)
})
