import { fireEvent, render, within } from '@testing-library/react'
import { expect, test } from 'vitest'
import { EventDetails } from '../src/EventDetails'
import { TeamToolProgress } from '../src/TeamProgressPanel'
import { conversationActivity } from '../src/conversationActivityState'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const sha256 = 'a'.repeat(64)
const body = '<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n[链接](javascript:alert(1))\nhttps://example.invalid\nnpm run malicious\n😀'
const event = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent => ({ seq, type, payload, actor })
const skill = (extra = {}) => ({ id: 'workspace-check', title: '工作区验证', description: '核对真实工作区中的执行结果',
  source: 'builtin', requires: ['workspace_read'], sha256, bytes: 640, status: 'ready', ...extra })
const listing = (extra = {}) => ({ status: 'ok', skills: [skill(), skill({ id: 'local-guide', title: '本地检查', source: 'configured', status: 'unavailable', sha256: undefined, bytes: undefined })],
  total: 3, offset: 0, has_more: true, next_offset: 2, configuration_status: 'ok', ...extra })
const reading = (extra = {}) => ({ status: 'ok', skill: skill(), content: body, offset: 0, total_chars: Array.from(body).length + 2,
  has_more: true, next_offset: Array.from(body).length, note: '只读参考', ...extra })
const start = (name = 'read_skill', args = {}) => event(10, 'team.tool_started', { name, role: 'dev', run_id: 'run', message_seq: 1,
  call_id: 'skill', arguments: name === 'read_skill' ? { skill_id: 'workspace-check', expected_sha256: sha256, ...args } : args })
const finish = (result: unknown = reading(), started = start(), actor = host) => event(started.seq + 1, 'team.tool_finished', {
  ...started.payload, started_seq: started.seq, status: 'ok', result,
}, actor)
const team: TeamState = { enabled: true, objective: '验证实现', status: 'running', revision: 0, runs: [], assignments: [], pending_count: 0, checkpoint: '' }
const activity = (events: TeamEvent[], executionState = 'running') => conversationActivity({ team, events: [
  event(1, 'team.message_received', { recipient: 'dev', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' }),
  event(2, 'team.run_started', { role: 'dev', run_id: 'run', message_seq: 1, revision: 0 }), ...events,
], names: { dev: '开发' }, leaderId: 'manager', sending: false, executionState })
const surfaces = (result: unknown, started = start(), actor = host) => {
  const receipt = finish(result, started, actor)
  return [render(<TeamToolProgress events={[started, receipt]} names={{ dev: '开发' }} />), render(<EventDetails event={receipt} />)]
}

test('目录在现有折叠过程和审计展示用途、来源、版本、失效与分页', () => {
  const started = start('list_skills')
  for (const view of surfaces(listing({ content: '不应显示的索引正文', note: 'F:\\private\\config.json', path: '/private/config.json' }), started)) {
    const text = view.container.textContent ?? ''
    for (const expected of ['查看工作指引目录', '目录已读取', '工作区验证', '用途：核对真实工作区中的执行结果', '来源：内置',
      '来源：本地配置', '已失效，无法阅读', sha256, '640 字节', '本页起点：0', '目录共 3 项', '下一页起点 2', '历史参考记录']) expect(text).toContain(expected)
    expect(text).not.toMatch(/工具已完成|不应显示的索引正文|private|原始事件 JSON|开发者详情|configuration_status|list_skills/)
    expect(view.container.querySelector('a, button, input, img, script')).toBeNull()
    expect(view.container.querySelector('[aria-label="工作指引元数据"] .font-mono')?.className).toContain('[overflow-wrap:anywhere]')
  }
  const progress = render(<TeamToolProgress events={[started, finish(listing(), started)]} names={{}} />)
  expect(progress.container.querySelector('details')?.open).toBe(false)
  const line = activity([started, finish(listing(), started)]).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain('查看工作指引目录 · 目录已读取')
  expect(line.warning).toBe(false)
})

test('正文默认再折叠且为精确纯文本，HTML、链接和命令均无可执行节点', () => {
  const started = start(), receipt = finish()
  for (const view of [render(<TeamToolProgress events={[started, receipt]} names={{}} full />), render(<EventDetails event={receipt} />)]) {
    expect(view.container.textContent).toContain('阅读工作指引')
    expect(view.container.textContent).toContain('指引已读取')
    expect(view.container.textContent).toContain('不代表当前授权、实际工具执行或任务验收通过')
    const summary = within(view.container).getByText('工作指引正文 · 本页纯文本')
    const details = summary.closest('details')!
    expect(details.open).toBe(false)
    fireEvent.click(summary)
    expect(details.open).toBe(true)
    expect(details.querySelector('pre')?.textContent).toBe(body)
    expect(view.container.querySelector('a, img, script, iframe, button, input, form, [onclick], [onerror]')).toBeNull()
    expect(view.container.textContent).not.toMatch(/工具已完成|原始事件 JSON|开发者详情|total_chars|read_skill/)
  }
  expect(activity([started, receipt]).lines.find(line => line.role === 'dev')?.warning).toBe(false)
})

test('目录和正文后续页各自保留范围，旧版与新版回执不自动拼接', () => {
  const offset = Array.from(body).length
  const pageStart = start('read_skill', { offset })
  for (const view of surfaces(reading({ offset, content: '尾页', total_chars: offset + 2, has_more: false, next_offset: null }), pageStart)) {
    expect(view.container.textContent).toContain(`本页起点：${offset}`)
    expect(view.container.textContent).toContain('已到末页')
    expect(view.container.querySelector('pre')?.textContent).toBe('尾页')
    expect(view.container.textContent).not.toContain(body)
  }
  for (const view of surfaces(listing({ skills: [skill()], offset: 2, has_more: false, next_offset: null }), start('list_skills', { offset: 2 }))) {
    expect(view.container.textContent).toContain('本页起点：2')
    expect(view.container.textContent).toContain('已到末页')
  }
  const newHash = 'b'.repeat(64), newStart = { ...start('read_skill', { expected_sha256: newHash }), seq: 20 }
  const view = render(<TeamToolProgress events={[start(), finish(), newStart, finish(reading({ skill: skill({ sha256: newHash }), content: '新版', total_chars: 2, has_more: false, next_offset: null }), newStart)]} names={{}} />)
  const contents = Array.from(view.container.querySelectorAll('pre')).map(node => node.textContent)
  expect(contents).toEqual([body, '新版'])
  expect(view.container.textContent).toContain(sha256)
  expect(view.container.textContent).toContain(newHash)
})

test.each(['skill_changed', 'skill_version_changed', 'version_mismatch', 'sha256_mismatch'])('版本错误 %s 不受外层 ok 背书且不回显正文和路径', code => {
  const result = { status: 'error', code, message: 'F:\\private\\guide.md', content: '不可信正文' }
  for (const view of surfaces(result)) {
    expect(view.container.textContent).toContain('指引版本已变化，请重新核对目录')
    expect(view.container.textContent).not.toMatch(/工具已完成|指引已读取|不可信正文|private/)
    expect(view.container.querySelector('pre')).toBeNull()
  }
  expect(activity([start(), finish(result)]).lines.find(line => line.role === 'dev')?.text).toContain('指引版本已变化')
})

test('失效配置和不可用读取不阻塞整个任务，空目录保留正常目录语义', () => {
  for (const view of surfaces(listing({ configuration_status: 'unavailable' }), start('list_skills'))) {
    expect(view.container.textContent).toContain('自定义工作指引配置不可用，正常工作工具不受影响')
    expect(view.container.textContent).toContain('工作区验证')
  }
  for (const view of surfaces({ status: 'error', code: 'skill_unavailable', message: '文件缺失' })) {
    expect(view.container.textContent).toContain('工作指引不可用')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  for (const view of surfaces(listing({ skills: [], total: 0, has_more: false, next_offset: null }), start('list_skills'))) {
    expect(view.container.textContent).toContain('当前没有可提供的工作指引')
    expect(view.container.textContent).toContain('目录已读取')
    expect(view.container.textContent).not.toMatch(/工具已完成|工具未完成/)
  }
})

const malformedRead: [string, unknown][] = [
  ['空值', null], ['数组', []], ['空对象', {}], ['假成功', { status: 'ok' }],
  ['畸形错误', { status: 'error', code: {} }], ['不完整结果', reading({ complete: false })], ['截断结果', reading({ truncated: true })],
  ['正文类型', reading({ content: {} })], ['超长正文', reading({ content: 'x'.repeat(12001), total_chars: 12003, next_offset: 12001 })],
  ['元数据数组', reading({ skill: [] })], ['失效正文', reading({ skill: skill({ status: 'unavailable' }) })],
  ['无标题', reading({ skill: skill({ title: '' }) })], ['无用途', reading({ skill: skill({ description: null }) })],
  ['来源伪造', reading({ skill: skill({ source: 'remote' }) })], ['来源数组', reading({ skill: skill({ source: ['builtin'] }) })],
  ['无前置列表', reading({ skill: skill({ requires: null }) })], ['前置畸形', reading({ skill: skill({ requires: [{}] }) })],
  ['摘要过长', reading({ skill: skill({ sha256: 'a'.repeat(2000) }) })], ['负大小', reading({ skill: skill({ bytes: -1 }) })],
  ['负起点', reading({ offset: -1 })], ['小数起点', reading({ offset: 0.5 })], ['无总长', reading({ total_chars: undefined })],
  ['不安全整数', reading({ total_chars: Number.MAX_SAFE_INTEGER + 1 })], ['越界页', reading({ total_chars: 1 })],
  ['页状态类型', reading({ has_more: 'true' })], ['无下页', reading({ next_offset: null })],
  ['下页不一致', reading({ next_offset: 2 })], ['假末页', reading({ has_more: false })],
  ['错误标识', reading({ skill: skill({ id: 'other' }) })],
]
test.each(malformedRead)('畸形读取只显示待确认：%s', (_title, result) => {
  for (const view of surfaces(result)) {
    expect(view.container.textContent).toContain('指引结果待确认')
    expect(view.container.textContent).not.toMatch(/指引已读取|工具已完成|工作区验证|\[object Object\]/)
    expect(view.container.querySelector('pre')).toBeNull()
  }
  expect(activity([start(), finish(result)]).lines.find(line => line.role === 'dev')?.text).toContain('指引结果待确认')
})

test.each([
  listing({ skills: [null] }), listing({ skills: {} }), listing({ skills: [skill(), skill()] }),
  listing({ configuration_status: ['ok'] }), listing({ offset: '0' }), listing({ next_offset: 1 }),
  listing({ skills: Array.from({ length: 21 }, (_, id) => skill({ id: String(id) })), total: 30, next_offset: 21 }),
])('畸形目录不降级展示原始 JSON：%j', result => {
  for (const view of surfaces(result, start('list_skills'))) {
    expect(view.container.textContent).toContain('指引结果待确认')
    expect(view.container.textContent).not.toMatch(/目录已读取|工作区验证|原始事件 JSON|开发者详情/)
  }
})

test('固定版本与页起点不一致时不展示正文；外层错误不被读取 ok 覆盖', () => {
  for (const view of surfaces(reading({ skill: skill({ sha256: 'b'.repeat(64) }) }))) {
    expect(view.container.textContent).toContain('指引版本已变化')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  for (const view of surfaces(reading(), start('read_skill', { offset: 2 }))) {
    expect(view.container.textContent).toContain('指引结果待确认')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  const receipt = finish()
  receipt.payload.status = 'error'
  const audit = render(<EventDetails event={receipt} />)
  expect(audit.container.textContent).toContain('指引结果待确认')
  expect(audit.container.querySelector('pre')).toBeNull()
})

test('摘要参数允许大写；活动状态也继承发起回执中的固定版本', () => {
  for (const view of surfaces(reading(), start('read_skill', { expected_sha256: sha256.toUpperCase() }))) {
    expect(view.container.textContent).toContain('指引已读取')
    expect(view.container.querySelector('pre')?.textContent).toBe(body)
  }
  const receipt = finish(reading({ skill: skill({ sha256: 'b'.repeat(64) }) }))
  delete receipt.payload.arguments
  const line = activity([start(), receipt]).lines.find(line => line.role === 'dev')!
  expect(line.text).toContain('指引版本已变化')
  expect(line.warning).toBe(true)
})

test.each([
  { id: 'team', kind: 'agent', role: 'team' }, { id: 'team', kind: 'human', role: 'team' },
  { id: 'other', kind: 'system', role: 'team' }, { id: 'team', kind: 'system', role: 'other' },
])('伪造宿主身份不能背书或渲染正文：%j', actor => {
  const receipt = finish(reading(), start(), actor)
  for (const view of surfaces(reading(), start(), actor)) {
    expect(view.container.textContent).not.toMatch(/指引已读取|工作区验证|alert\(1\)|原始事件 JSON/)
    expect(view.container.querySelector('pre')).toBeNull()
  }
  const audit = render(<EventDetails event={receipt} />)
  expect(audit.container.textContent).toContain('指引回执来源待核对')
  expect(activity([start(), receipt]).lines.find(line => line.role === 'dev')?.text).toContain('已发起，等待回执')
  const fakeStart = { ...start(), actor }
  const hidden = render(<TeamToolProgress events={[fakeStart, receipt]} names={{}} />)
  expect(hidden.container.textContent).toBe('')
  expect(activity([fakeStart, receipt]).lines.find(line => line.role === 'dev')?.text).not.toContain('阅读工作指引')
})

test('仅发起、独立回执及省略工具名的配对保留原有过程兼容', () => {
  const started = start(), receipt = finish()
  for (const view of [render(<TeamToolProgress events={[started]} names={{}} />), render(<EventDetails event={started} />)]) {
    expect(view.container.textContent).toContain('阅读工作指引')
    expect(view.container.textContent).toContain('已发起，等待回执')
    expect(view.container.querySelector('pre')).toBeNull()
  }
  const orphan = render(<TeamToolProgress events={[receipt]} names={{}} />)
  expect(orphan.container.textContent).toContain('指引已读取')
  delete receipt.payload.name
  delete receipt.payload.arguments
  const paired = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(paired.container.textContent).toContain('工具过程 · 1 项')
  expect(paired.container.textContent).toContain('指引已读取')
  expect(activity([started, receipt]).lines.find(line => line.role === 'dev')?.text).toContain('阅读工作指引 · 指引已读取')
})

test('只读回执不完成任务、不答复顾客，也不恢复暂停中的活动', () => {
  const paused = activity([start(), finish()], 'paused')
  expect(paused.visible).toBe(true)
  expect(paused.notice).toContain('已暂停后续执行')
  expect(paused.message).toContain('答复待确认')
  expect(paused.lines.find(line => line.role === 'dev')?.moving).toBe(false)
  expect(paused.lines.map(line => line.text).join('\n')).not.toMatch(/已完成|已通过|已交付|已恢复/)
  expect(paused.nextCheckAt).toBeUndefined()
})

test('普通工具继续显示原有回执与开发者详情', () => {
  const started = start('workspace_read'), receipt = finish({ status: 'ok', content: '原有文件内容' }, started)
  const progress = render(<TeamToolProgress events={[started, receipt]} names={{}} />)
  expect(progress.container.textContent).toContain('读取文件 · 工具已完成')
  expect(progress.container.textContent).toContain('原有文件内容')
  const audit = render(<EventDetails event={receipt} />)
  expect(within(audit.container).getByText('开发者详情').closest('details')?.open).toBe(false)
  expect(activity([started, receipt]).lines.find(line => line.role === 'dev')?.text).toContain('读取文件 · 已完成')
})
