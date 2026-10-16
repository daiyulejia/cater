import { act, fireEvent, render, screen } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ConversationActivity } from '../src/ConversationActivity'
import { conversationActivity } from '../src/conversationActivityState'
import type { ConversationActivityProps } from '../src/ConversationActivity'
import type { TeamEvent, TeamState } from '../src/team'

const now = Date.parse('2026-10-16T08:00:00Z')
const team: TeamState = { enabled: true, objective: '不要重复的目标正文', status: 'running', pending_count: 0,
  revision: 0, runs: [], assignments: [], checkpoint: '不要重复的交付正文' }
const event = (seq: number, type: string, payload: Record<string, unknown> = {}, actor = { id: 'team', kind: 'system', role: 'team' }, ts = new Date(now).toISOString()): TeamEvent & { ts: string } =>
  ({ seq, type, payload, actor, ts })
const human = (seq = 1, recipient = 'dev') => event(seq, 'team.message_received', { recipient, kind: 'message', text: '@开发 请回答' }, { id: 'human', kind: 'human', role: 'human' })
const start = (seq = 2, message = 1, role = 'dev', revision = 0, run = 'run') => event(seq, 'team.run_started', { role, message_seq: message, revision, run_id: run })
const end = (seq = 3, status = 'completed', message = 1, role = 'dev', run = 'run') => event(seq, 'team.run_finished', { role, message_seq: message, status, run_id: run })
const reply = (seq = 4, message = 1, role = 'dev') => event(seq, 'msg.said', { text: '答复正文不重复', phase: 'team', reply_to: message, run_id: 'run', audience: 'customer', recipient: 'human' }, { id: role, kind: 'agent', role })
const props = (extra: Partial<ConversationActivityProps> = {}): ConversationActivityProps => ({ team, events: [], names: { dev: '开发', test: '测试', manager: '店长' },
  leaderId: 'manager', sending: false, executionState: 'running', portrait: (id) => <span data-testid={`portrait-${id}`}>{id}</span>,
  onInspect: vi.fn(), onRefresh: vi.fn(), onPause: vi.fn(), ...extra })
const text = (extra: Partial<ConversationActivityProps>) => {
  const view = conversationActivity(props(extra), now)
  return [view.message, ...view.lines.map((line) => line.text), view.notice].filter(Boolean).join('\n')
}
const tool = (seq = 3, extra = {}) => event(seq, 'team.tool_started', { role: 'dev', run_id: 'run', message_seq: 1,
  name: 'workspace_read', call_id: 'read', arguments: { path: 'private-path' }, ...extra })
const receipt = (extra = {}) => event(4, 'team.tool_finished', { role: 'dev', run_id: 'run', name: 'workspace_read', started_seq: 3,
  result: { status: 'ok' }, ...extra })

test('来源材料的进行中读取在现有进展条显示中文工具名', () => {
  const output = text({ events: [human(), start(), tool(3, { name: 'read_background' })] })
  expect(output).toContain('读取来源材料')
  expect(output).not.toContain('read_background')
})

test('结构化提问表达等待顾客而非失联，直接聊天后不继续催旧问题', () => {
  const question = event(3, 'team.questions_requested', { role_id: 'dev', message_seq: 1, title: '补充背景' })
  const events = [human(), start(), question, end(4, 'waiting')]
  const view = conversationActivity(props({ events }), now)
  expect(view.message).toContain('在等你的补充')
  expect(view.lines.find((line) => line.role === 'dev')?.text).toBe('开发 · 等待你的补充')
  expect(view.lines.find((line) => line.role === 'dev')?.moving).toBe(false)
  expect(view.waitsForCustomer).toBe(true)
  render(<ConversationActivity {...props({ events })} />)
  const compact = screen.getByRole('region', { name: '当前工作进展' })
  expect(compact.textContent).toContain('待你补充')
  expect(compact.textContent).not.toContain('等待你的补充')
  expect(text({ events })).not.toMatch(/暂无执行|等待伙伴回复|尚未确认答复/)
  expect(text({ events: [...events, human(5)] })).not.toContain('在等你的补充')
  expect(text({ events: [...events, event(5, 'team.objective_updated', { goal: '新方向' })] })).not.toContain('等待你的补充')
  const fake = { ...question, actor: { id: 'dev', kind: 'agent', role: 'dev' } }
  expect(text({ events: [human(), start(), fake, end(4, 'waiting')] })).not.toContain('在等你的补充')
  const parallel = event(5, 'team.questions_requested', { role_id: 'test', message_seq: 1 })
  const answered = event(6, 'team.message_received', { recipient: 'test', questionnaire: { question_seq: 5 } }, { id: 'human', kind: 'human', role: 'human' })
  const state = conversationActivity(props({ events: [...events, parallel, answered] }), now)
  expect(state.lines.find((line) => line.role === 'dev')?.text).toContain('等待你的补充')
  expect(state.lines.find((line) => line.role === 'test')?.text ?? '').not.toContain('等待你的补充')
})

test('同角色替代问题已回答后不再催旧问题，其他角色的新问题仍独立等待', () => {
  const events = [human(), start(),
    event(3, 'team.questions_requested', { role_id: 'dev', message_seq: 1, title: '旧问题' }), end(4, 'waiting'),
    event(5, 'team.message_received', { recipient: 'dev', kind: 'message', revision: 0 }, { id: 'manager', kind: 'agent', role: 'manager' }),
    start(6, 5, 'dev', 0, 'replacement'),
    event(7, 'team.questions_requested', { role_id: 'dev', message_seq: 5, title: '替代问题' }), end(8, 'waiting', 5, 'dev', 'replacement')]
  const input = props({ events })
  const mounted = render(<ConversationActivity {...input} />)
  expect(conversationActivity(input, now).waitsForCustomer).toBe(true)
  expect(screen.getByRole('region', { name: '当前工作进展' }).textContent).toContain('待你补充')
  const answered = [...events,
    event(9, 'team.message_received', { recipient: 'dev', kind: 'message', questionnaire: { question_seq: 7 } }, { id: 'human', kind: 'human', role: 'human' }),
    start(10, 9), end(11, 'completed', 9), reply(12, 9)]
  const view = conversationActivity(props({ events: answered }), now)
  expect(view.waitsForCustomer).toBe(false)
  expect(view.message).toContain('已回应最新消息')
  expect(view.lines.some((line) => line.text.includes('等待你的补充'))).toBe(false)
  mounted.rerender(<ConversationActivity {...props({ events: answered })} />)
  expect(screen.getByRole('region', { name: '当前工作进展' }).textContent).not.toContain('待你补充')
  const parallel = event(13, 'team.questions_requested', { role_id: 'test', title: '测试的问题' })
  const parallelView = conversationActivity(props({ events: [...answered, parallel] }), now)
  expect(parallelView.waitsForCustomer).toBe(true)
  expect(parallelView.lines.find((line) => line.role === 'test')?.text).toContain('等待你的补充')
  expect(parallelView.lines.find((line) => line.role === 'dev')?.text).not.toContain('等待你的补充')
})

test('非宿主的同角色新问题不能使真实待答问题失效', () => {
  const events = [human(), start(), event(3, 'team.questions_requested', { role_id: 'dev' }), end(4, 'waiting'),
    event(5, 'team.questions_requested', { role_id: 'dev' }, { id: 'dev', kind: 'agent', role: 'dev' }),
    event(6, 'team.message_received', { recipient: 'dev', questionnaire: { question_seq: 5 } }, { id: 'human', kind: 'human', role: 'human' }),
    start(7, 6), end(8, 'completed', 6)]
  expect(conversationActivity(props({ events }), now).waitsForCustomer).toBe(true)
})

test('发送与收到分别表达，同轮原位更新，直接 @ 成员不添加店长', () => {
  vi.useFakeTimers().setSystemTime(now)
  const initial = props({ sending: true })
  const mounted = render(<ConversationActivity {...initial} />)
  const region = screen.getByRole('region', { name: '当前工作进展' })
  expect(region.textContent).toContain('消息正在发送，尚未确认收到')
  expect(region.textContent).not.toContain('已收到')
  mounted.rerender(<ConversationActivity {...initial} sending={false} events={[human()]} />)
  expect(region.textContent).toContain('最新消息已收到，等待开发答复')
  mounted.rerender(<ConversationActivity {...initial} sending={false} events={[human(), start()]} />)
  expect(region.textContent).toContain('开发正在处理最新消息')
  expect(screen.getAllByRole('region', { name: '当前工作进展' })).toEqual([region])
  expect(screen.queryByTestId('portrait-manager')).toBeNull()
  expect(screen.getAllByTestId('portrait-dev').length).toBeGreaterThan(0)
  expect(region.textContent).not.toMatch(/不要重复|答复正文/)
})

test('多位角色只显示两项优先进展，失败与在途优先，完整投影仍保留', () => {
  const events = [human(), start(), human(3, 'test'), start(4, 3, 'test', 0, 'test-run'),
    human(5, 'manager'), start(6, 5, 'manager', 0, 'manager-run'), end(7, 'failed', 5, 'manager', 'manager-run')]
  const input = props({ events })
  expect(conversationActivity(input, now).lines).toHaveLength(3)
  render(<ConversationActivity {...input} />)
  const region = screen.getByRole('region', { name: '当前工作进展' })
  expect(region.textContent).toContain('店长 · 本轮处理未完成')
  expect(region.textContent).toContain('开发 ·')
  expect(region.textContent).not.toContain('测试 ·')
  expect(screen.getByRole('button', { name: '查看过程' }).title).toContain('另有 1 项进展')
})

test('答复完成与工作完成独立；缺少答复事件不猜已回答', () => {
  expect(text({ events: [human(), start(), end()] })).toContain('尚未确认答复')
  const content = text({ events: [human(), start(), end(), reply()] })
  expect(content).toContain('开发已回应最新消息')
  expect(content).toContain('当前暂无执行，尚未确认交付')
  expect(content).not.toContain('工作已完成')
  expect(content).not.toContain('答复正文不重复')
})

test('整体 done 仍显示后续顾客问题，所有答复完成后隐藏', () => {
  const completedTeam = { ...team, revision: 5, status: 'done' }
  const history = [event(5, 'team.objective_completed', { summary: '最终交付正文' }), human(6)]
  expect(conversationActivity(props({ team: completedTeam, events: history }), now).visible).toBe(true)
  expect(text({ team: completedTeam, events: history })).toContain('等待开发答复')
  const answered = [...history, start(7, 6, 'dev', 5), end(8, 'completed', 6), reply(9, 6)]
  expect(conversationActivity(props({ team: completedTeam, events: answered }), now).visible).toBe(false)
  const mounted = render(<ConversationActivity {...props({ team: completedTeam, events: answered })} />)
  expect(mounted.container.childElementCount).toBe(0)
})

test.each(['progress', undefined])('交付后的追问只有过程发言时仍等待答复，正式回复才隐藏进展（类型=%s）', (responseKind) => {
  const completedTeam = { ...team, revision: 5, status: 'done' }
  const progress = { ...reply(8, 6), payload: { ...reply(8, 6).payload, response_kind: responseKind } }
  const events = [event(5, 'team.objective_completed'), human(6), start(7, 6, 'dev', 5), progress,
    tool(9, { message_seq: 6, revision: 5 }),
    event(10, 'team.tool_finished', { role: 'dev', run_id: 'run', name: 'workspace_read', started_seq: 9, result: { status: 'ok' } }),
    tool(11, { message_seq: 6, revision: 5, name: 'wait_for_reply' }),
    event(12, 'team.tool_finished', { role: 'dev', run_id: 'run', name: 'wait_for_reply', started_seq: 11, result: { status: 'ok', waiting: true } }),
    end(13, 'waiting', 6)]
  const input = props({ team: completedTeam, events, activity: { active: false, state: 'completed' } })
  const view = conversationActivity(input, now)
  expect(view.visible).toBe(true)
  expect(view.message).toContain('最新消息尚待答复')
  expect(view.message).not.toContain('已回应')
  const mounted = render(<ConversationActivity {...input} />)
  expect(screen.getByRole('region', { name: '当前工作进展' }).textContent).toContain('等待伙伴回复')
  const answered = [...events.filter((event) => event.seq < 11),
    { ...reply(11, 6), payload: { ...reply(11, 6).payload, response_kind: 'reply' } }, end(12, 'completed', 6)]
  expect(conversationActivity({ ...input, events: answered }, now).visible).toBe(false)
  mounted.rerender(<ConversationActivity {...input} events={answered} />)
  expect(screen.queryByRole('region', { name: '当前工作进展' })).toBeNull()
})

test('明确答复即使后续调用工具也仍算已回应', () => {
  const answer = { ...reply(3), payload: { ...reply(3).payload, response_kind: 'reply' } }
  expect(text({ events: [human(), start(), answer, tool(4)] })).toContain('已回应最新消息')
})

test('交付后新追问排队时，不把上一次成员答复完成误显示为本轮进展', () => {
  const completedTeam = { ...team, revision: 5, status: 'done', pending_count: 1 }
  const events = [event(5, 'team.objective_completed'), human(6), start(7, 6, 'dev', 5),
    tool(8, { message_seq: 6 }), event(9, 'team.tool_finished', { role: 'dev', run_id: 'run', name: 'workspace_read', started_seq: 8, result: { status: 'ok' } }),
    end(10, 'completed', 6), reply(11, 6), human(12)]
  const content = text({ team: completedTeam, events, activity: { active: true, state: 'queued' } })
  expect(content).toContain('等待开发答复')
  expect(content).not.toMatch(/本轮消息处理已结束|读取文件/)
})

test('真实完成事件可结束旧 snapshot，新目标撤销旧 done 和失败', () => {
  expect(conversationActivity(props({ events: [event(5, 'team.objective_completed')] }), now).visible).toBe(false)
  const events = [human(), start(), end(3, 'failed'), event(5, 'team.objective_completed'), event(10, 'team.objective_updated', { goal: '新目标' }),
    event(11, 'team.tool_started', { role: 'dev', name: 'workspace_run', run_id: 'run', message_seq: 1 }), end(12, 'failed')]
  const content = text({ team: { ...team, status: 'done', revision: 5 }, events })
  expect(content).toContain('尚未确认交付')
  expect(content).not.toMatch(/本轮处理未完成|运行命令|开发|新目标/)
})

test('当前 revision 过滤 snapshot 和旧委托迟到回执，保留当前真实角色动作', () => {
  const events = [event(10, 'team.objective_updated'), event(11, 'team.message_received', { recipient: 'test', revision: 0, kind: 'assignment' }, { id: 'manager', kind: 'agent', role: 'manager' }),
    start(12, 11, 'test', 0, 'old'), event(13, 'team.tool_started', { role: 'test', run_id: 'old', message_seq: 11, name: 'workspace_run' }),
    human(14), start(15, 14, 'dev', 10), tool(16, { message_seq: 14 })]
  const content = text({ team: { ...team, revision: 10, runs: [{ role: 'test', message_seq: 1, revision: 0, status: 'failed' }] }, events })
  expect(content).toContain('开发 · 读取文件')
  expect(content).not.toMatch(/测试|运行命令|未完成/)
})

test('目标采用发生在顾客 run 内时，继续显示该消息与动作', () => {
  const events = [human(1, 'manager'), start(2, 1, 'manager'), tool(3, { role: 'manager', name: 'set_objective' }),
    event(4, 'team.objective_updated'), event(5, 'team.tool_finished', { role: 'manager', name: 'set_objective', started_seq: 3, run_id: 'run', result: { status: 'ok' } }),
    tool(6, { role: 'manager', name: 'delegate' })]
  const content = text({ team: { ...team, revision: 4 }, events })
  expect(content).toContain('店长正在处理最新消息')
  expect(content).toContain('交接工作')
})

test('采用目标不能丢掉同期另一位成员收到的顾客新消息或其失败', () => {
  const events = [human(1, 'manager'), start(2, 1, 'manager', 0, 'manager-run'), human(3),
    tool(4, { role: 'manager', name: 'set_objective', run_id: 'manager-run' }), event(5, 'team.objective_updated'),
    event(6, 'team.tool_finished', { role: 'manager', name: 'set_objective', started_seq: 4, run_id: 'manager-run', result: { status: 'ok' } }),
    end(7, 'waiting', 1, 'manager', 'manager-run')]
  const nextTeam = { ...team, revision: 5 }
  expect(text({ team: nextTeam, events })).toContain('等待开发答复')
  const failedEvents = [...events, start(8, 3, 'dev', 5), end(9, 'failed', 3)]
  expect(text({ team: nextTeam, events: failedEvents })).toContain('最新消息答复待确认 · 开发本轮处理未完成')
})

test('采用目标的旧 revision run 可由无 run_id 的进程回收终态结束，不能继续旋转', () => {
  const events = [human(1, 'manager'), start(2, 1, 'manager'), tool(3, { role: 'manager', name: 'set_objective' }),
    event(4, 'team.objective_updated'), event(5, 'team.tool_finished', { role: 'manager', name: 'set_objective', started_seq: 3, run_id: 'run', result: { status: 'ok' } }),
    event(6, 'team.run_finished', { message_seq: 1, role: 'manager', status: 'interrupted' })]
  const view = conversationActivity(props({ team: { ...team, revision: 4,
    runs: [{ message_seq: 1, role: 'manager', revision: 0, status: 'interrupted' }] }, events }), now)
  expect(view.message).toContain('本轮处理未完成')
  expect(view.lines.some((line) => line.moving)).toBe(false)
  expect(view.nextCheckAt).toBeUndefined()
})

test.each([true, false])('交付结清采用为目标的顾客请求，不要求最终答复直接reply_to顾客（显式采用=%s）', (adopted) => {
  const initial = [human(1, 'manager'), start(2, 1, 'manager')]
  const adoption = adopted ? [tool(3, { role: 'manager', name: 'set_objective' }), event(4, 'team.objective_updated'),
    event(5, 'team.tool_finished', { role: 'manager', name: 'set_objective', started_seq: 3, run_id: 'run', result: { status: 'ok' } })] : []
  const events = [...initial, ...adoption, end(6, 'waiting', 1, 'manager'),
    event(8, 'team.objective_completed', { summary: '已完成目标', run_id: 'delivery-run' }), reply(9, 7, 'manager')]
  const completedTeam = { ...team, revision: 8, status: 'done' }
  expect(conversationActivity(props({ team: completedTeam, events }), now).visible).toBe(false)
  expect(text({ team: completedTeam, events: [...events, human(10)] })).toContain('等待开发答复')
})

test('另一角色活跃时保留其动作，失败仅属于失败角色', () => {
  const events = [human(), start(), end(3, 'failed'), event(4, 'team.message_received', { recipient: 'test', kind: 'assignment', revision: 0 }, { id: 'manager', kind: 'agent', role: 'manager' }),
    start(5, 4, 'test', 0, 'test-run'), tool(6, { role: 'test', run_id: 'test-run', message_seq: 4, name: 'workspace_run' })]
  const view = conversationActivity(props({ events, activity: { active: true, state: 'failed' } }), now)
  expect(view.lines.find((line) => line.role === 'dev')?.warning).toBe(true)
  expect(view.lines.find((line) => line.role === 'test')?.moving).toBe(true)
  expect(view.lines.find((line) => line.role === 'test')?.text).toContain('运行命令')
  expect(view.lines.map((line) => line.text).join('')).not.toMatch(/全任务停止|后台协作异常|当前暂无执行/)
})

test.each(['team.run_started', 'team.tool_started', 'team.run_finished', 'team.objective_updated', 'team.objective_completed'])(
  '伪造 actor 的 %s 不改变投影', (type) => {
    const fake = event(9, type, { role: 'dev', message_seq: 1, name: 'workspace_run', status: 'failed', goal: '伪造目标' }, { id: 'team', kind: 'agent', role: 'team' })
    expect(conversationActivity(props({ events: [human(), fake] }), now)).toEqual(conversationActivity(props({ events: [human()] }), now))
  })

test.each([
  { id: 'intruder', kind: 'agent', role: 'dev' }, { id: 'dev', kind: 'human', role: 'dev' }, { id: 'team', kind: 'system', role: 'intruder' },
])('伪造答复 actor %j 不能声称已回答', (actor) => {
  const forged = { ...reply(), actor, payload: { ...reply().payload, speaker: 'dev' } }
  expect(text({ events: [human(), start(), forged] })).not.toContain('已回应')
})

test.each([{ reply_to: 0 }, { audience: 'team' }, { recipient: 'someone-else' }, { run_id: 'another-run' }, { text: '  ' }])('答复必须关联最新顾客消息 %j', (extra) => {
  const wrong = { ...reply(), payload: { ...reply().payload, ...extra } }
  expect(text({ events: [human(), start(), wrong] })).not.toContain('已回应')
})

test('旧答复与本轮完成无法回答新问题，后续同角色成功不重放旧失败', () => {
  const events = [human(), start(), end(3, 'failed'), reply(), human(5), start(6, 5), end(7, 'completed', 5), reply(8, 5)]
  const content = text({ events })
  expect(content).toContain('已回应最新消息')
  expect(content).not.toContain('本轮处理未完成')
  expect(text({ events: [...events, human(9)] })).toContain('等待开发答复')
})

test('工具动作必须有宿主开始记录；只有匹配回执可声明结束', () => {
  expect(text({ events: [receipt()] })).not.toContain('读取文件')
  const events = [human(), start(), tool()]
  expect(text({ events })).toContain('读取文件 · 已发起，等待回执')
  expect(text({ events: [...events, receipt()] })).toContain('读取文件 · 已完成')
  expect(text({ events: [...events, receipt({ result: {} })] })).toContain('读取文件 · 结果待确认')
})

test.each([{ started_seq: 99 }, { run_id: 'old' }, { role: 'test' }, { name: 'workspace_run' }, { message_seq: 99 }, { call_id: 'other' }])('错配回执不结束工具 %j', (extra) => {
  expect(text({ events: [human(), start(), tool(), receipt(extra)] })).toContain('读取文件 · 已发起，等待回执')
})

test('call_id 兼容回执仍核对 run，失败与不完整结果不能冒充成功', () => {
  expect(text({ events: [human(), start(), tool(), receipt({ started_seq: undefined, call_id: 'read' })] })).toContain('读取文件 · 已完成')
  for (const result of [{ status: 'ok', outcome: 'failed' }, { status: 'ok', exit_code: 1 }, { status: 'ok', complete: false }, { status: 'ok', stdout: { complete: false } }]) {
    expect(text({ events: [human(), start(), tool(), receipt({ result })] })).not.toContain('读取文件 · 已完成')
  }
})

test('等待、协作故障与无执行状态均有明确文案，不因 completed activity 推断交付', () => {
  expect(text({ events: [human(), start(), end(3, 'waiting')] })).toContain('等待伙伴回复')
  expect(text({ activity: { active: true, state: 'queued' } })).toContain('后台协作已排队')
  expect(text({ activity: { active: false, state: 'failed', error: 'raw detail' } })).toContain('后台协作异常')
  expect(text({ activity: { active: false, state: 'completed' } })).toContain('尚未确认交付')
  expect(text({ events: [human(), start(), end(), reply()], activity: { active: true, state: 'running' } })).toContain('尚未确认交付')
  expect(text({ events: [human(), start(), end()], activity: { active: false, state: 'completed' } })).toContain('尚未确认答复')
})

test.each(['paused', 'cancelled'])('%s 保留聊天与在途语义，撤销动画且无计时器', (executionState) => {
  vi.useFakeTimers().setSystemTime(now)
  render(<ConversationActivity {...props({ events: [human(), start(), tool()], executionState, activity: { active: true, state: 'running' } })} />)
  expect(screen.getByRole('status').textContent).toContain('在途调用仍可能返回')
  expect(screen.getByRole('status').textContent).toContain('仍可直接聊天')
  expect(screen.queryByLabelText('有近期执行反馈')).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
  fireEvent.click(screen.getByRole('button', { name: '暂停后续执行' }))
})

test('按钮仅触发传入回调，暂停受 active、running、busy 限制', () => {
  const initial = props({ activity: { active: true, state: 'running' } })
  const mounted = render(<ConversationActivity {...initial} />)
  fireEvent.click(screen.getByRole('button', { name: '查看过程' }))
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  fireEvent.click(screen.getByRole('button', { name: '暂停后续执行' }))
  expect(initial.onInspect).toHaveBeenCalledTimes(1)
  expect(initial.onRefresh).toHaveBeenCalledTimes(1)
  expect(initial.onPause).toHaveBeenCalledTimes(1)
  for (const extra of [{ controlBusy: true }, { executionState: 'paused' }, { activity: { active: false, state: 'running' } }]) {
    mounted.rerender(<ConversationActivity {...initial} {...extra} />)
    fireEvent.click(screen.getByRole('button', { name: '暂停后续执行' }))
  }
  expect(initial.onPause).toHaveBeenCalledTimes(1)
  for (const button of screen.getAllByRole('button')) expect(button.title).toBeTruthy()
})

test('60 秒无执行新记录撤销动画，刷新事件时间后重启单次计时并卸载清理', () => {
  vi.useFakeTimers().setSystemTime(now)
  const initial = props({ events: [human(), start(), tool()], activity: { active: true, state: 'running' } })
  const mounted = render(<ConversationActivity {...initial} />)
  expect(screen.getByLabelText('有近期执行反馈')).toBeTruthy()
  expect(vi.getTimerCount()).toBe(1)
  act(() => vi.advanceTimersByTime(60_001))
  expect(screen.queryByLabelText('有近期执行反馈')).toBeNull()
  expect(screen.getByRole('status').textContent).toContain('暂无新反馈')
  expect(vi.getTimerCount()).toBe(0)
  const newer = { ...receipt(), ts: new Date(Date.now()).toISOString() }
  mounted.rerender(<ConversationActivity {...initial} events={[...initial.events, newer]} />)
  expect(screen.getByLabelText('有近期执行反馈')).toBeTruthy()
  expect(vi.getTimerCount()).toBe(1)
  mounted.unmount()
  expect(vi.getTimerCount()).toBe(0)
})

test('顾客新消息与伪造执行不能延长旧执行动画，缺失或无效时间不无限运行', () => {
  vi.useFakeTimers().setSystemTime(now + 61_000)
  const forged = { ...tool(5), ts: new Date(now + 61_000).toISOString(), actor: { id: 'dev', kind: 'agent', role: 'dev' } }
  render(<ConversationActivity {...props({ events: [human(), start(), tool(), human(4), forged] })} />)
  expect(screen.queryByLabelText('有近期执行反馈')).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
  for (const ts of [undefined, 'invalid', new Date(now + 120_000).toISOString()]) {
    expect(conversationActivity(props({ events: [human(), { ...start(), ts } as TeamEvent] }), now).nextCheckAt).toBeUndefined()
  }
})

test('同步失败立即撤销动画并清理定时器，仍保留手动刷新', () => {
  vi.useFakeTimers().setSystemTime(now)
  const initial = props({ events: [human(), start()] })
  const mounted = render(<ConversationActivity {...initial} />)
  expect(vi.getTimerCount()).toBe(1)
  mounted.rerender(<ConversationActivity {...initial} syncError="network failure" />)
  expect(screen.getByRole('status').textContent).toContain('同步失败，状态待确认')
  expect(screen.queryByLabelText('有近期执行反馈')).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  expect(initial.onRefresh).toHaveBeenCalledOnce()
})

test('静态终态和禁用团队不创建计时器', () => {
  vi.useFakeTimers().setSystemTime(now)
  const initial = props()
  const mounted = render(<ConversationActivity {...initial} />)
  expect(vi.getTimerCount()).toBe(0)
  mounted.rerender(<ConversationActivity {...initial} team={{ ...team, enabled: false }} events={[human(), start()]} />)
  expect(screen.queryByRole('region', { name: '当前工作进展' })).toBeNull()
  expect(vi.getTimerCount()).toBe(0)
})

test('交付后的历史失败与旧委托不覆盖完成状态，在途角色仍可显示', () => {
  const events = [event(1, 'team.objective_updated'), human(2), start(3, 2, 'dev', 1), end(4, 'failed', 2),
    event(5, 'team.message_received', { recipient: 'test', revision: 1, kind: 'assignment' }, { id: 'manager', kind: 'agent', role: 'manager' }),
    event(6, 'team.objective_completed')]
  const completed = { ...team, revision: 6, status: 'done', pending_count: 1 }
  expect(conversationActivity(props({ team: completed, events: [...events, reply(7, 2)] }), now).visible).toBe(false)
  const inFlight = [...events.slice(0, -1), start(6, 5, 'test', 1, 'test-run'), event(7, 'team.objective_completed')]
  const view = conversationActivity(props({ team: { ...completed, revision: 7, pending_count: 0 }, events: inFlight }), now)
  expect(view.visible).toBe(true)
  expect(view.lines.find((line) => line.role === 'test')?.moving).toBe(true)
  expect(view.lines.find((line) => line.role === 'dev')).toBeUndefined()
})

test('更高 revision 事件优先于旧 snapshot，不纳入晚到旧委托', () => {
  const events = [event(10, 'team.objective_updated'), event(11, 'team.message_received', { recipient: 'test', revision: 0, kind: 'assignment' }, { id: 'manager', kind: 'agent', role: 'manager' }),
    start(12, 11, 'test', 0, 'old'), tool(13, { role: 'test', message_seq: 11, run_id: 'old', name: 'workspace_run' })]
  expect(text({ events })).not.toMatch(/测试|运行命令/)
})

test('缺少旧记录时保留 snapshot 待处理数量，不猜后台消息全部处理完', () => {
  const events = [event(5, 'team.objective_completed'), human(6), start(7, 6, 'dev', 5), end(8, 'completed', 6), reply(9, 6)]
  const partial = props({ events, team: { ...team, revision: 5, status: 'done', pending_count: 2 } })
  const view = conversationActivity(partial, now)
  expect(view.visible).toBe(true)
  expect(view.lines.some((line) => line.text === '后台有 2 条消息等待处理')).toBe(true)
})

test('伪造或不匹配工具回执不会延长陈旧反馈', () => {
  const lateReceipt = { ...receipt({ started_seq: 99 }), ts: new Date(now + 61_000).toISOString() }
  const view = conversationActivity(props({ events: [human(), start(), tool(), lateReceipt] }), now + 61_000)
  expect(view.lines.some((line) => line.moving)).toBe(false)
  expect(view.nextCheckAt).toBeUndefined()
})

test('无 run_id 的宿主开始记录仍支持 60 秒陈旧检测', () => {
  const legacy = { ...start(), payload: { role: 'dev', message_seq: 1 } }
  expect(conversationActivity(props({ events: [human(), legacy] }), now).nextCheckAt).toBe(now + 60_000)
  expect(conversationActivity(props({ events: [human(), legacy] }), now + 61_000).nextCheckAt).toBeUndefined()
})

test('完成前已处理的失败工具回执不让已交付进展常驻，后续已答也不重放旧警告', () => {
  const events = [human(), start(), tool(), receipt({ result: { status: 'error' } }), end(5), reply(6), event(7, 'team.objective_completed')]
  const completed = { ...team, revision: 7, status: 'done' }
  const initial = props({ team: completed, events })
  const mounted = render(<ConversationActivity {...initial} />)
  expect(screen.queryByRole('region', { name: '当前工作进展' })).toBeNull()
  const legacyTool = { ...tool(), payload: { role: 'dev', name: 'workspace_read', call_id: 'read' } }
  const followup = [human(), start(), legacyTool, receipt({ result: { status: 'error' } }), end(5), reply(6), event(7, 'team.objective_completed'),
    human(8), start(9, 8, 'dev', 7, 'followup'), end(10, 'completed', 8, 'dev', 'followup'),
    { ...reply(11, 8), payload: { ...reply(11, 8).payload, run_id: 'followup' } }]
  mounted.rerender(<ConversationActivity {...initial} events={followup} />)
  expect(screen.queryByRole('region', { name: '当前工作进展' })).toBeNull()
})
