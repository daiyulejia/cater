import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import type { DetailEvent } from '../src/EventDetails'

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
