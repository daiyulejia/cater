import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import App from '../src/App'
import type { ChatItem } from '../src/App'
import { MessageDetails } from '../src/MessageDetails'
import { replyEvidenceDetailsFor, replyEvidenceFor } from '../src/replyEvidence'
import type { ReplyEvidence } from '../src/replyEvidence'
import { conversationActivity } from '../src/conversationActivityState'
import type { TeamEvent } from '../src/team'
import { detail, event, leader, members, summaries } from './native-handoff.fixtures'

const label = '查看答复依据'
const note = '这是答复引用的历史原文与工具记录，不认证观点、测试身份、覆盖率或成果正确性。摘录中的通过不代表整个命令成功；以记录的状态、退出码和完整性分别核对。'
const sourceHash = 'a1'.repeat(32)
const omittedWarning = '可选答复引用未通过校验，未保存；答复正文已发送，不代表内容已验证。'
function fixture(quote = '15 assertions passed\ncommand failed') {
  const binding = { role: 'dev', run_id: 'reply-run', message_seq: 1, revision: 0 }
  const input = event(1, 'team.message_received', { text: '检查结果是什么？', recipient: 'dev', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' })
  const run = event(2, 'team.run_started', { ...binding })
  const sourceStart = event(3, 'team.tool_started', { ...binding, name: 'workspace_run', call_id: 'source-call', arguments: { command: ['test'] } })
  const receipt: TeamEvent & ReturnType<typeof event> = { ...event(4, 'team.tool_finished', {
    started_seq: 3, run_id: binding.run_id, role: 'dev', name: 'workspace_run',
    result: { status: 'error', outcome: 'failed', exit_code: 1, complete: true, stdout: { excerpt: quote, utf8_valid: true } },
  }), hash: sourceHash }
  const source: ReplyEvidence['sources'][number] = { source_seq: 4, quote, kind: 'tool_receipt', source_hash: sourceHash,
    role: 'dev', tool: 'workspace_run', status: 'error', outcome: 'failed', exit_code: 1, complete: true, truncated: false, output_utf8_valid: { stdout: true } }
  const refs = [{ source_seq: 4, quote }]
  const args = { text: '  断言片段通过，但命令以 1 退出。  ', evidence: refs }
  const start = event(5, 'team.tool_started', { ...binding, name: 'reply_to_customer', call_id: 'reply-call', arguments: args,
    execution_contract: { version: 1, kind: 'atomic_local_events', receipt: 'same_transaction' } })
  const message = event(6, 'msg.said', { text: args.text.trim(), phase: 'team', run_id: binding.run_id, reply_to: 1,
    response_kind: 'reply', audience: 'customer', recipient: 'human', customer_reply_tool: 'reply_to_customer', reply_evidence_started_seq: 5 },
  { id: 'dev', kind: 'agent', role: 'dev' })
  const evidence: ReplyEvidence = { version: 1, sources: [source], note }
  const result = { status: 'ok', message_seq: 6, reply_to: 1, reply_evidence: evidence }
  const finish = event(7, 'team.tool_finished', { started_seq: 5, run_id: binding.run_id, role: 'dev', name: 'reply_to_customer', result })
  return { events: [input, run, sourceStart, receipt, start, message, finish], input, run, sourceStart, receipt, source, refs, args, start, message, evidence, result, finish }
}
type Fixture = ReturnType<typeof fixture>
const sourceField = (f: Fixture, key: string, value: unknown) => { Object.assign(f.source, { [key]: value }) }
const itemFor = (f: Fixture): ChatItem => {
  const details = replyEvidenceDetailsFor(f.events).get(6)
  return { id: 6, kind: 'agent', evidence: f.message,
    actor: 'dev', display: '米寇', role: 'dev', systemActor: false, category: 'collaboration', targets: [], text: String(f.message.payload.text),
    replyEvidence: details?.evidence, replyEvidenceWarning: details?.warning, auditName: () => '米寇' }
}

function omittedFixture(refs: unknown = [{ source_seq: 'not-an-event', quote: '<script>伪造引用</script>' }]) {
  const f = fixture()
  Object.assign(f.args, { evidence: refs })
  delete (f.result as Partial<typeof f.result>).reply_evidence
  Object.assign(f.result, { reply_evidence_status: 'omitted_invalid', reply_evidence_warning: omittedWarning })
  return f
}

const auditWrapper = (value: unknown) => ({ encoding: 'escaped_json', json: JSON.stringify(value) })

function wrappedFixture() {
  const f = omittedFixture([{ source_seq: 4, quote: '\ud800' }])
  f.start.payload.arguments = auditWrapper(f.args)
  return f
}

test('真实 HOST 原子答复绑定原 refs，乱序快照同样可用且不修改输入', () => {
  const f = fixture('  逐字保留\n引文  '), saved = JSON.stringify(f.events)
  expect(replyEvidenceFor(f.events).get(6)).toBe(f.evidence)
  expect(replyEvidenceFor([...f.events].reverse()).get(6)).toBe(f.evidence)
  expect(JSON.stringify(f.events)).toBe(saved)
})

test.each(['  原文  ', '\n原文\n\n', '\t 原文 \t', '\r\n 原文 \r\n'])('quote 原样保留前后空格和换行 %j', quote => {
  const f = fixture(quote)
  expect(replyEvidenceFor(f.events).get(6)?.sources[0].quote).toBe(quote)
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.querySelector('blockquote')?.textContent).toBe(quote)
  expect(container.querySelector('details')?.open).toBe(false)
})

test.each(['trim', 'trimStart', 'trimEnd'] as const)('normalized quote 被 %s 后不再匹配原 refs', method => {
  const f = fixture(' \n\t原文\t\n ')
  f.source.quote = f.source.quote[method]()
  expect(replyEvidenceFor(f.events).size).toBe(0)
})

test('只按传入 events 快照校验，完整快照不能掩盖另一个被篡改的快照', () => {
  const f = fixture(' \n原文\n ')
  const fetchMock = vi.fn(() => { throw new Error('Snapshot validation must not fetch') })
  vi.stubGlobal('fetch', fetchMock)
  expect(replyEvidenceFor(f.events).get(6)).toBe(f.evidence)
  const snapshot = JSON.parse(JSON.stringify(f.events)) as typeof f.events
  snapshot[4].payload.arguments = { ...f.args, evidence: [{ source_seq: 4, quote: f.refs[0].quote.trim() }] }
  expect(replyEvidenceFor(snapshot).size).toBe(0)
  expect(replyEvidenceFor(f.events).get(6)).toBe(f.evidence)
  const alteredHash = JSON.parse(JSON.stringify(f.events)) as typeof f.events
  Object.assign(alteredHash[3], { hash: 'b'.repeat(64) })
  expect(replyEvidenceFor(alteredHash).size).toBe(0)
  expect(fetchMock).not.toHaveBeenCalled()
})

const invalidReplyBindings: [string, (f: Fixture) => void][] = [
  ['角色自报 normalized', f => { f.message.payload.reply_evidence = f.evidence; f.finish.payload.result = { status: 'ok', message_seq: 6, reply_to: 1 } }],
  ['角色伪造 finished', f => { f.finish.actor = { id: 'dev', role: 'team', kind: 'system' } }],
  ['伪 HOST role', f => { f.start.actor.role = 'dev' }],
  ['伪 HOST kind', f => { f.run.actor.kind = 'agent' }],
  ['缺少原子合同', f => { delete f.start.payload.execution_contract }],
  ['错误原子合同', f => { f.start.payload.execution_contract = { version: 1, kind: 'atomic_local_events', receipt: 'later' } }],
  ['扩展原子合同', f => { Object.assign(f.start.payload.execution_contract!, { extra: true }) }],
  ['原子合同版本字符串', f => { Object.assign(f.start.payload.execution_contract!, { version: '1' }) }],
  ['缺少 run', f => { f.events.splice(1, 1) }],
  ['缺少输入', f => { f.events.splice(0, 1) }],
  ['非顾客输入', f => { f.input.actor.kind = 'agent' }],
  ['顾客身份错配', f => { f.input.actor.id = 'another-human' }],
  ['输入 kind 错配', f => { f.input.payload.kind = 'assignment' }],
  ['输入收件人错配', f => { f.input.payload.recipient = 'test' }],
  ['run role 错配', f => { f.run.payload.role = 'test' }],
  ['run 输入错配', f => { f.run.payload.message_seq = 99 }],
  ['run 晚于 started', f => { f.run.seq = 8 }],
  ['run 已结束', f => { f.events.push(event(8, 'team.run_finished', { run_id: 'reply-run', role: 'dev', message_seq: 1 })); f.finish.seq = 9 }],
  ['finished run 错配', f => { f.finish.payload.run_id = 'other' }],
  ['finished role 错配', f => { f.finish.payload.role = 'test' }],
  ['finished name 错配', f => { f.finish.payload.name = 'finish_work' }],
  ['finished call_id 错配', f => { f.finish.payload.call_id = 'other' }],
  ['finished started_seq 错配', f => { f.finish.payload.started_seq = 3 }],
  ['工具名错配', f => { f.start.payload.name = 'finish_work' }],
  ['缺少 call_id', f => { delete f.start.payload.call_id }],
  ['失败答复', f => { f.result.status = 'error' }],
  ['结果消息错配', f => { f.result.message_seq = 99 }],
  ['结果输入错配', f => { f.result.reply_to = 99 }],
  ['消息输入错配', f => { f.message.payload.reply_to = 99 }],
  ['消息 run 错配', f => { f.message.payload.run_id = 'other' }],
  ['消息正文错配', f => { f.message.payload.text = '另一答复' }],
  ['消息角色错配', f => { f.message.actor.id = 'test' }],
  ['消息类型错配', f => { f.message.type = 'team.message_received' }],
  ['消息 phase 错配', f => { f.message.payload.phase = 'other' }],
  ['消息 audience 错配', f => { f.message.payload.audience = 'team' }],
  ['消息收件人错配', f => { f.message.payload.recipient = 'dev' }],
  ['消息 response kind 错配', f => { f.message.payload.response_kind = 'progress' }],
  ['消息工具标记错配', f => { f.message.payload.customer_reply_tool = 'finish_work' }],
  ['消息晚于完成', f => { f.message.seq = 9; f.result.message_seq = 9 }],
  ['指针缺省', f => { delete f.message.payload.reply_evidence_started_seq }],
  ['指针字符串', f => { f.message.payload.reply_evidence_started_seq = '5' }],
  ['指针布尔', f => { f.message.payload.reply_evidence_started_seq = true }],
  ['指针未知', f => { f.message.payload.reply_evidence_started_seq = 99 }],
  ['原 args 多余字段', f => { Object.assign(f.args, { approved: true }) }],
  ['重复完成 pair', f => { f.events.push({ ...f.finish, seq: 8 }) }],
  ['重复 run', f => { f.events.push({ ...f.run, seq: 8 }) }],
  ['重复消息指针', f => { f.events.push({ ...f.message, seq: 8 }) }],
  ['重复事件 seq', f => { f.events.push({ ...f.receipt }) }],
  ['来源伪造输入身份', f => { f.input.actor.role = 'dev' }],
]

const invalidBindings: [string, (f: Fixture) => void][] = [
  ...invalidReplyBindings,
  ['原 refs 缺省', f => { delete (f.args as Partial<typeof f.args>).evidence }],
  ['原 refs 为空', f => { f.args.evidence = [] }],
  ['原 refs quote 错配', f => { f.refs[0].quote = '改写引文' }],
  ['原 refs seq 错配', f => { f.refs[0].source_seq = 3 }],
  ['原 refs 多余字段', f => { Object.assign(f.refs[0], { status: 'ok' }) }],
  ['原 refs 多余来源', f => { f.refs.push({ source_seq: 4, quote: '第二段' }) }],
  ['normalized 来源重复', f => { f.evidence.sources.push({ ...f.source }) }],
  ['来源缺失/其他群', f => { f.events.splice(3, 1) }],
  ['来源未结算', f => { f.receipt.type = 'team.tool_started' }],
  ['来源 started 缺失', f => { f.events.splice(2, 1) }],
  ['来源非 HOST', f => { f.receipt.actor.kind = 'agent' }],
  ['来源 started 非 HOST', f => { f.sourceStart.actor.id = 'dev' }],
  ['来源配对 run 错配', f => { f.receipt.payload.run_id = 'other' }],
  ['来源配对 call_id 错配', f => { f.receipt.payload.call_id = 'other' }],
  ['来源完成 pair 重复', f => { f.events.push({ ...f.receipt, seq: 8 }) }],
  ['来源缺少 hash', f => { delete f.receipt.hash }],
  ['来源 hash 错配', f => { f.receipt.hash = 'b'.repeat(64) }],
  ['不支持的转抄来源', f => { f.sourceStart.payload.name = 'read_receipts'; f.receipt.payload.name = 'read_receipts'; f.source.tool = 'read_receipts' }],
  ['来源角色错配', f => { f.source.role = 'test' }],
  ['来源工具错配', f => { f.source.tool = 'workspace_read' }],
  ['晚来源', f => { f.receipt.seq = 8; f.source.source_seq = 8; f.refs[0].source_seq = 8 }],
]
test.each(invalidBindings)('%s 不展示为答复依据', (_name, mutate) => {
  const f = fixture()
  mutate(f)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.textContent).not.toContain(label)
})

test.each(invalidReplyBindings)('省略引用仍要求完整配对：%s', (_name, mutate) => {
  const f = omittedFixture()
  mutate(f)
  expect(replyEvidenceDetailsFor(f.events).size).toBe(0)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.textContent).not.toContain(omittedWarning)
  expect(container.querySelector('blockquote')).toBeNull()
})

test.each(invalidReplyBindings)('包装参数也要求完整 HOST 配对：%s', (_name, mutate) => {
  const f = omittedFixture([{ source_seq: 4, quote: '\ud800' }])
  mutate(f)
  f.start.payload.arguments = auditWrapper(f.start.payload.arguments)
  expect(replyEvidenceDetailsFor(f.events).size).toBe(0)
  expect(replyEvidenceFor(f.events).size).toBe(0)
})

test('完整 HOST 包装中的孤立代理字符只参与参数绑定，不展示引用或执行内容', () => {
  const f = wrappedFixture(), saved = JSON.stringify(f.events)
  const fetchMock = vi.fn(() => { throw new Error('Audit decoding must not fetch') })
  vi.stubGlobal('fetch', fetchMock)
  expect(replyEvidenceDetailsFor(f.events).get(6)).toEqual({ warning: omittedWarning })
  expect(replyEvidenceDetailsFor([...f.events].reverse()).get(6)).toEqual({ warning: omittedWarning })
  expect(replyEvidenceFor(f.events).size).toBe(0)
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.textContent).toContain(omittedWarning)
  expect(container.textContent).not.toMatch(/escaped_json|source_seq|\\ud800/)
  expect(container.querySelector('blockquote,script,iframe,a')).toBeNull()
  expect(container.querySelector('details')?.open).toBe(false)
  expect(JSON.stringify(f.events)).toBe(saved)
  expect(fetchMock).not.toHaveBeenCalled()
})

test.each(['字符串里的 "text": "重复键外观" 不是属性', '引号 \\" 与换行\n及冒号 :', '中文与完整代理对 😀', '<script>alert(1)</script>'])('包装正文精确绑定且字符串内 JSON 外观不误判：%s', body => {
  const f = omittedFixture({ '\ud800': body })
  f.args.text = `  ${body}  `
  f.message.payload.text = body
  f.start.payload.arguments = auditWrapper(f.args)
  expect(replyEvidenceDetailsFor(f.events).get(6)).toEqual({ warning: omittedWarning })
})

const invalidWrappers: [string, (f: Fixture) => unknown][] = [
  ['坏 JSON', () => ({ encoding: 'escaped_json', json: '{' })],
  ['JSON 非文本', f => ({ encoding: 'escaped_json', json: f.args })],
  ['编码不符', f => ({ ...auditWrapper(f.args), encoding: 'json' })],
  ['多余 wrapper 字段', f => ({ ...auditWrapper(f.args), evidence: null })],
  ['wrapper 为数组', f => [auditWrapper(f.args)]],
  ['重复 wrapper', f => auditWrapper(auditWrapper(f.args))],
  ['解码为 null', () => auditWrapper(null)],
  ['解码为数组', f => auditWrapper([f.args])],
  ['解码为文本', () => auditWrapper('not arguments')],
  ['解码正文缺失', () => auditWrapper({ evidence: null })],
  ['解码正文为空', () => auditWrapper({ text: ' ', evidence: null })],
  ['解码正文错型', () => auditWrapper({ text: 12, evidence: null })],
  ['解码正文不匹配', () => auditWrapper({ text: '另一答复', evidence: null })],
  ['解码多余字段', f => auditWrapper({ ...f.args, approved: true })],
  ['解码没有 evidence', f => auditWrapper({ text: f.args.text })],
  ['重复正文键', f => ({ encoding: 'escaped_json', json: `{"text":"wrong","text":${JSON.stringify(f.args.text)},"evidence":null}` })],
  ['转义的重复正文键', f => ({ encoding: 'escaped_json', json: `{"text":"wrong","\\u0074ext":${JSON.stringify(f.args.text)},"evidence":null}` })],
  ['嵌套重复引用键', f => ({ encoding: 'escaped_json', json: `{"text":${JSON.stringify(f.args.text)},"evidence":{"quote":"one","quote":"two"}}` })],
  ['非有限数字', f => ({ encoding: 'escaped_json', json: `{"text":${JSON.stringify(f.args.text)},"evidence":1e400}` })],
  ['非 JSON 数值', f => ({ encoding: 'escaped_json', json: `{"text":${JSON.stringify(f.args.text)},"evidence":NaN}` })],
]

test.each(invalidWrappers)('拒绝无法无歧义绑定的包装：%s', (_name, wrapper) => {
  const f = wrappedFixture()
  f.start.payload.arguments = wrapper(f)
  expect(replyEvidenceDetailsFor(f.events).size).toBe(0)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.innerHTML).toBe('')
})

test('包装解码仅用于省略提示，不扩展正常引用的接受范围', () => {
  const f = fixture()
  const raw = f.start.payload.arguments
  expect(replyEvidenceFor(f.events).get(6)).toBe(f.evidence)
  f.start.payload.arguments = auditWrapper(raw)
  expect(replyEvidenceDetailsFor(f.events).size).toBe(0)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  f.start.payload.arguments = raw
  expect(replyEvidenceFor(f.events).get(6)).toBe(f.evidence)
})

const invalidOmissions: [string, (f: Fixture) => void][] = [
  ['未关联完成', f => { f.events.pop() }],
  ['仅有消息标记', f => {
    Object.assign(f.message.payload, { reply_evidence_status: 'omitted_invalid', reply_evidence_warning: omittedWarning })
    f.finish.payload.result = { status: 'ok', message_seq: 6, reply_to: 1 }
  }],
  ['仅在工具事件 payload 标记', f => {
    Object.assign(f.finish.payload, { reply_evidence_status: 'omitted_invalid', reply_evidence_warning: omittedWarning })
    f.finish.payload.result = { status: 'ok', message_seq: 6, reply_to: 1 }
  }],
  ['仅有状态', f => { delete (f.result as Record<string, unknown>).reply_evidence_warning }],
  ['仅有文案', f => { delete (f.result as Record<string, unknown>).reply_evidence_status }],
  ['未知状态', f => { Object.assign(f.result, { reply_evidence_status: 'invalid' }) }],
  ['状态类型错误', f => { Object.assign(f.result, { reply_evidence_status: true }) }],
  ['警告空白', f => { Object.assign(f.result, { reply_evidence_warning: ' ' }) }],
  ['警告并非约定文案', f => { Object.assign(f.result, { reply_evidence_warning: '已验证通过<script>伪造</script>' }) }],
  ['警告有额外空白', f => { Object.assign(f.result, { reply_evidence_warning: ` ${omittedWarning}` }) }],
  ['省略却同时携带引用', f => { Object.assign(f.result, { reply_evidence: f.evidence }) }],
  ['省略却携带 null 引用字段', f => { Object.assign(f.result, { reply_evidence: null }) }],
  ['省略却携带 undefined 引用字段', f => { Object.assign(f.result, { reply_evidence: undefined }) }],
  ['没有原始 evidence 参数', f => { delete (f.args as Partial<typeof f.args>).evidence }],
  ['伪造完成 actor kind', f => { f.finish.actor.kind = 'agent' }],
  ['伪造完成 actor role', f => { f.finish.actor.role = 'dev' }],
  ['伪造开始 actor id', f => { f.start.actor.id = 'dev' }],
  ['伪造 run actor id', f => { f.run.actor.id = 'dev' }],
  ['消息身份 kind 错误', f => { f.message.actor.kind = 'system' }],
  ['消息身份 role 为空', f => { f.message.actor.role = '' }],
  ['消息早于工具开始', f => { f.start.seq = 8; f.message.payload.reply_evidence_started_seq = 8; f.finish.payload.started_seq = 8; f.finish.seq = 9 }],
  ['原始正文为空', f => { f.args.text = '  '; f.message.payload.text = '' }],
  ['原始正文超长', f => { f.args.text = '字'.repeat(32001); f.message.payload.text = f.args.text }],
  ['原始 args 非对象', f => { f.start.payload.arguments = [] }],
  ['缺少输入指针', f => { delete f.start.payload.message_seq }],
  ['输入指针类型错误', f => { f.start.payload.message_seq = '1' }],
  ['重复输入 seq', f => { f.events.push({ ...f.input }) }],
]

test.each(invalidOmissions)('不显示不可信省略警告：%s', (_name, mutate) => {
  const f = omittedFixture()
  mutate(f)
  const saved = JSON.stringify(f.events)
  expect(replyEvidenceDetailsFor(f.events).size).toBe(0)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  expect(JSON.stringify(f.events)).toBe(saved)
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.innerHTML).toBe('')
})

test.each([null, '伪造引用', {}, [{ source_seq: true, quote: 1 }], [{ source_seq: 999, quote: '<script>伪造引用</script>' }],
  Array.from({ length: 9 }, () => ({ source_seq: 4, quote: '未经验证' }))])('宿主省略非法 refs %j 后，只展示固定提示', refs => {
  const f = omittedFixture(refs), saved = JSON.stringify(f.events)
  // A discarded citation must not resolve missing history or turn source claims into quotes.
  f.events.splice(2, 2)
  const withoutSources = JSON.stringify(f.events)
  expect(replyEvidenceDetailsFor(f.events).get(6)).toEqual({ warning: omittedWarning })
  expect(replyEvidenceDetailsFor([...f.events].reverse()).get(6)).toEqual({ warning: omittedWarning })
  expect(replyEvidenceFor(f.events).size).toBe(0)
  expect(JSON.stringify(f.events)).toBe(withoutSources)
  expect(saved).not.toBe(withoutSources)
  const { container } = render(<MessageDetails item={{ ...itemFor(f), details: ['原有详情'] }} />)
  expect(container.querySelectorAll('.message-details')).toHaveLength(1)
  expect(container.querySelector('summary')?.textContent).toBe('查看判断与依据')
  expect(container.querySelector('details')?.open).toBe(false)
  expect(container.textContent).toContain('原有详情')
  expect(container.textContent).toContain(omittedWarning)
  expect(container.textContent).not.toMatch(/伪造引用|未经验证|查看答复依据|来源事件哈希/)
  expect(container.querySelector('blockquote,script,a')).toBeNull()
})

test('警告也只读取当前快照，不修改输入、不缓存跨群结果或额外请求', () => {
  const f = omittedFixture(), saved = JSON.stringify(f.events)
  const fetchMock = vi.fn(() => { throw new Error('Snapshot validation must not fetch') })
  vi.stubGlobal('fetch', fetchMock)
  expect(replyEvidenceDetailsFor(f.events).get(6)?.warning).toBe(omittedWarning)
  const other = JSON.parse(saved) as typeof f.events
  other[6].payload.result = { status: 'ok', message_seq: 6, reply_to: 1 }
  expect(replyEvidenceDetailsFor(other).size).toBe(0)
  expect(replyEvidenceDetailsFor(f.events).get(6)?.warning).toBe(omittedWarning)
  expect(JSON.stringify(f.events)).toBe(saved)
  expect(fetchMock).not.toHaveBeenCalled()
})

test('新映射保留合法引用，消息自报省略标记不能覆盖可信引用', () => {
  const f = fixture()
  Object.assign(f.message.payload, { reply_evidence_status: 'omitted_invalid', reply_evidence_warning: omittedWarning })
  expect(replyEvidenceDetailsFor(f.events).get(6)).toEqual({ evidence: f.evidence })
  expect(replyEvidenceFor(f.events).get(6)).toBe(f.evidence)
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.querySelector('blockquote')?.textContent).toBe(f.source.quote)
  expect(container.textContent).not.toContain(omittedWarning)
})

test('省略提示不把答复变成任务完成、失败或复核结论', () => {
  const f = omittedFixture(), snapshot = detail(f.events)
  const team = { ...snapshot.task.team!, status: 'running' as const }
  const input = { team, events: f.events, names: { dev: '米寇' }, leaderId: 'store_manager', sending: false, executionState: 'idle' }
  const saved = JSON.stringify(input)
  const previous = conversationActivity(input)
  expect(replyEvidenceDetailsFor(f.events).get(6)?.warning).toBe(omittedWarning)
  render(<MessageDetails item={itemFor(f)} />)
  expect(JSON.stringify(input)).toBe(saved)
  expect(conversationActivity(input)).toEqual(previous)
  f.finish.payload.result = { status: 'ok', message_seq: 6, reply_to: 1 }
  expect(conversationActivity(input)).toEqual(previous)
})

test.each<[string, unknown]>([
  ['source_seq', 0], ['source_seq', -1], ['source_seq', 1.5], ['source_seq', '4'], ['source_seq', true], ['source_seq', Number.MAX_SAFE_INTEGER + 1],
  ['quote', ''], ['quote', '  '], ['quote', 'x'.repeat(2001)], ['quote', 1], ['kind', 'artifact'], ['kind', 'peer_review'],
  ['source_hash', 'a'], ['source_hash', 'g'.repeat(64)], ['source_hash', null], ['status', 'approved'], ['status', ['ok']],
  ['outcome', 'approved'], ['outcome', null], ['exit_code', '1'], ['exit_code', 1.5], ['complete', 'true'], ['truncated', 1],
  ['output_utf8_valid', { stdout: 1 }], ['output_utf8_valid', { other: true }], ['unknown', true],
])('拒绝非法 source %s=%j', (key, value) => {
  const f = fixture()
  sourceField(f, key, value)
  expect(replyEvidenceFor(f.events).size).toBe(0)
})

test.each([undefined, null, {}, { version: 2, sources: [], note }, { version: 1, sources: [], note }])('缺省或空 normalized 不改变旧消息 %j', value => {
  const f = fixture()
  f.finish.payload.result = { ...f.result, reply_evidence: value }
  f.message.payload.reply_evidence = f.evidence
  const item = { ...itemFor(f), details: ['原有依据'] }
  const { container } = render(<MessageDetails item={item} />)
  expect(container.querySelector('summary')?.textContent).toBe('查看判断与依据')
  expect(container.textContent).toContain('原有依据')
  expect(container.querySelector('blockquote')).toBeNull()
})

test.each([undefined, []])('无引用的历史答复维持原样 %j', refs => {
  const f = fixture()
  delete f.message.payload.reply_evidence_started_seq
  f.start.payload.arguments = { text: f.args.text, ...(refs ? { evidence: refs } : {}) }
  f.finish.payload.result = { status: 'ok', message_seq: 6, reply_to: 1 }
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.innerHTML).toBe('')
})

test('原始重复 pair 按后端规范去重，normalized 不得重复', () => {
  const f = fixture('  原文\n ')
  f.refs.push({ ...f.refs[0] })
  expect(replyEvidenceFor(f.events).get(6)?.sources).toHaveLength(1)
})

function setQuotes(f: Fixture, quotes: string[]) {
  f.args.evidence = quotes.map(quote => ({ source_seq: 4, quote }))
  f.evidence.sources = quotes.map(quote => ({ ...f.source, quote }))
  Object.assign(f.receipt.payload.result!, { stdout: { excerpt: quotes.join('\n'), utf8_valid: true } })
}
test('只有空白不同的原 refs 不合并，normalized 必须保留两条逐字引文', () => {
  const f = fixture()
  setQuotes(f, ['原文', '  原文\n '])
  expect(replyEvidenceFor(f.events).get(6)?.sources.map(source => source.quote)).toEqual(['原文', '  原文\n '])
  f.evidence.sources.pop()
  expect(replyEvidenceFor(f.events).size).toBe(0)
})
test('normalized 必须保持原 refs 的来源与引文顺序', () => {
  const f = fixture()
  setQuotes(f, ['第一段', '第二段'])
  expect(replyEvidenceFor(f.events).size).toBe(1)
  f.evidence.sources.reverse()
  expect(replyEvidenceFor(f.events).size).toBe(0)
})

test('允许同群另一角色早先 run 的真实回执，该 run 可在回执后结束', () => {
  const f = fixture()
  f.input.seq = 10
  f.run.seq = 11
  f.run.payload.message_seq = 10
  f.start.seq = 12
  f.start.payload.message_seq = 10
  f.message.seq = 13
  f.message.payload.reply_to = 10
  f.message.payload.reply_evidence_started_seq = 12
  f.finish.seq = 14
  f.finish.payload.started_seq = 12
  f.result.message_seq = 13
  f.result.reply_to = 10
  Object.assign(f.sourceStart.payload, { role: 'test', run_id: 'history-run' })
  Object.assign(f.receipt.payload, { role: 'test', run_id: 'history-run' })
  f.source.role = 'test'
  f.events.push(event(1, 'team.message_received', { text: '之前的检查', recipient: 'test', kind: 'message' }, { id: 'human', role: 'human', kind: 'human' }),
    event(2, 'team.run_started', { message_seq: 1, role: 'test', run_id: 'history-run', revision: 0 }),
    event(9, 'team.run_finished', { message_seq: 1, role: 'test', run_id: 'history-run', status: 'waiting' }))
  expect(replyEvidenceFor(f.events).get(13)).toBe(f.evidence)
  f.sourceStart.seq = 4
  f.receipt.seq = 5
  f.receipt.payload.started_seq = 4
  f.source.source_seq = 5
  f.refs[0].source_seq = 5
  f.events.at(-1)!.seq = 3
  expect(replyEvidenceFor(f.events).size).toBe(0)
})
test('8 个引用与 2000 Unicode 字符有效，第 9 个或超长原 refs 无效', () => {
  const f = fixture('😀'.repeat(2000))
  expect(replyEvidenceFor(f.events).size).toBe(1)
  f.refs[0].quote += 'x'
  expect(replyEvidenceFor(f.events).size).toBe(0)
  setQuotes(f, Array.from({ length: 8 }, (_, i) => `片段 ${i}`))
  expect(replyEvidenceFor(f.events).size).toBe(1)
  setQuotes(f, Array.from({ length: 9 }, (_, i) => `片段 ${i}`))
  expect(replyEvidenceFor(f.events).size).toBe(0)
})

test('总大小按 UTF8 24000 字节限制，包含 note 和元数据', () => {
  const f = fixture()
  setQuotes(f, Array.from({ length: 4 }, (_, i) => `${i}${'中'.repeat(1800)}`))
  expect(replyEvidenceFor(f.events).size).toBe(1)
  setQuotes(f, Array.from({ length: 5 }, (_, i) => `${i}${'中'.repeat(1800)}`))
  expect(JSON.stringify(f.evidence).length).toBeLessThan(24000)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  setQuotes(f, ['short'])
  f.evidence.note = '中'.repeat(8000)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  f.evidence.note = ''
  const bytes = new TextEncoder().encode(JSON.stringify(f.evidence)).byteLength
  f.evidence.note = 'x'.repeat(24000 - bytes)
  expect(replyEvidenceFor(f.events).size).toBe(1)
  f.evidence.note += 'x'
  expect(replyEvidenceFor(f.events).size).toBe(0)
})

test.each(['workspace_read', 'workspace_write', 'workspace_run', 'workspace_list', 'workspace_search', 'browser_check', 'read_artifact'])('支持真实 %s 回执', tool => {
  const f = fixture()
  f.sourceStart.payload.name = tool
  f.receipt.payload.name = tool
  f.source.tool = tool
  expect(replyEvidenceFor(f.events).get(6)).toBe(f.evidence)
})

test.each(['passed', 'failed', 'timeout', 'launch_error', 'effects_unknown', 'not_started'] as const)('展示真实 outcome=%s', outcome => {
  const f = fixture()
  f.source.outcome = outcome
  if (outcome === 'passed') Object.assign(f.source, { exit_code: 0, status: 'ok' })
  Object.assign(f.receipt.payload.result!, { outcome, exit_code: f.source.exit_code, status: f.source.status })
  const labels = { passed: '检查通过', failed: '执行失败', timeout: '超时', launch_error: '启动失败', effects_unknown: '执行影响待核实', not_started: '未开始执行' }
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.textContent).toContain(`执行结果：${labels[outcome]}`)
})

test('默认折叠，非零退出醒目且不把引文 OK 当整体成功，不要求读取 JSON', () => {
  const f = fixture('OK: 15 assertions passed')
  f.source.truncated = true
  f.source.complete = false
  f.source.output_utf8_valid = { stdout: false, stderr: null }
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  const summary = screen.getByText(label, { selector: 'summary' }), fold = summary.closest('details')!
  expect(fold.open).toBe(false)
  fireEvent.click(summary)
  expect(fold.open).toBe(true)
  expect(fold.textContent).toContain('伙伴：米寇 · 工具：运行命令 · 记录状态：返回错误')
  expect(fold.textContent).toContain('退出码：1 · 命令未成功（非零退出）')
  expect(within(fold).getByText(/命令未成功/).className).toContain('text-red-700')
  expect(fold.textContent).toContain('退出状态不表示引文中的每项业务断言都失败。')
  expect(fold.textContent).toContain('回执采集未完成')
  expect(fold.textContent).toContain('回执正文仅保留部分内容。')
  expect(fold.textContent).toContain('不认证测试身份、覆盖范围或验收通过')
  expect(fold.querySelector('blockquote')?.textContent).toBe(f.source.quote)
  expect(fold.textContent).not.toMatch(/"source_seq"|approved|命令执行成功|断言全部失败/)
  expect(container.querySelectorAll('.message-details')).toHaveLength(1)
})

test('HTML、长单词、换行与 hash 均纯文本显示并允许窄屏换行', () => {
  const quote = `<img src=x onerror=alert(1)>\n<script>window.pwned=true</script>\n${'W'.repeat(1800)}`
  const f = fixture(quote)
  f.evidence.note = '<iframe src="javascript:alert(1)"></iframe>'
  const { container } = render(<MessageDetails item={itemFor(f)} />)
  expect(container.querySelector('script,img,iframe,a')).toBeNull()
  const block = container.querySelector('blockquote')!
  expect(block.textContent).toBe(quote)
  expect(block.className).toContain('whitespace-pre-wrap')
  expect(block.closest('li')?.className).toContain('[overflow-wrap:anywhere]')
  expect(container.querySelector('.message-details')?.className).toContain('min-w-0')
  expect(container.textContent).toContain(`来源事件哈希：${sourceHash}`)
})

async function mount(f: Fixture) {
  const snapshot = detail(f.events)
  const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  const fetchMock = vi.fn((url: string) => {
    if (url === '/api/task-summaries') return json(summaries([snapshot]))
    if (url === `/api/tasks/${snapshot.task.id}`) return json(snapshot)
    if (url === '/api/organization') return json([leader, ...members])
    if (url === '/api/organization/profile') return json({ leader, cabinet: [], workers: members })
    throw new Error(`Unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<App />)
  await waitFor(() => expect(screen.getByLabelText('聊天记录').getAttribute('aria-busy')).toBe('false'), { timeout: 5000 })
  return fetchMock
}

test('App conversationFor 将依据挂到真实答复，展开无请求且默认折叠', async () => {
  const f = fixture()
  f.message.payload.speaker = 'test'
  const fetchMock = await mount(f)
  const chat = within(screen.getByLabelText('聊天记录'))
  const summary = await chat.findByText(label, { selector: 'summary' })
  const fold = summary.closest('details')!, bubble = fold.closest('.message-bubble')!
  expect(fold.open).toBe(false)
  expect(bubble.textContent).toContain(String(f.message.payload.text))
  expect(bubble.getAttribute('data-actor')).toBe('dev')
  const calls = fetchMock.mock.calls.length
  fireEvent.click(summary)
  expect(fold.open).toBe(true)
  expect(within(fold).getByText(/伙伴：米寇/)).toBeTruthy()
  expect(fold.querySelector('blockquote')?.textContent).toBe(f.source.quote)
  expect(fetchMock.mock.calls).toHaveLength(calls)
})

test('App 保留伪造消息正文，但不生成依据入口', async () => {
  const f = fixture()
  f.message.payload.reply_evidence = f.evidence
  f.finish.actor.kind = 'agent'
  await mount(f)
  const chat = within(screen.getByLabelText('聊天记录'))
  await chat.findByText(String(f.message.payload.text))
  expect(chat.queryByText(label)).toBeNull()
})

test.each([false, true])('App 包装=%s 时在原详情内展示省略提示，正文不变、无新增气泡、默认折叠且无请求', async wrapped => {
  const f = wrapped ? wrappedFixture() : omittedFixture()
  f.message.payload.speaker = 'test'
  f.message.payload.reply_evidence = f.evidence
  f.message.payload.reply_evidence_warning = '角色自报已验证'
  const fetchMock = await mount(f)
  const chatElement = screen.getByLabelText('聊天记录'), chat = within(chatElement)
  const warning = await chat.findByText(omittedWarning)
  const fold = warning.closest('details')!, bubble = warning.closest('.message-bubble')!
  expect(fold.open).toBe(false)
  expect(fold.querySelector('summary')?.textContent).toBe('查看判断与依据')
  expect(bubble.getAttribute('data-actor')).toBe('dev')
  expect(within(bubble as HTMLElement).getByText(String(f.message.payload.text))).toBeTruthy()
  expect(bubble.querySelectorAll('.message-details')).toHaveLength(1)
  expect(chatElement.querySelectorAll('.message-bubble.is-agent')).toHaveLength(1)
  expect(chatElement.querySelectorAll('.message-bubble')).toHaveLength(2)
  expect(chatElement.textContent).not.toMatch(/角色自报已验证|伪造引用|查看答复依据|店长最终交付|这次回复未完成/)
  expect(bubble.querySelector('blockquote')).toBeNull()
  const calls = fetchMock.mock.calls.length
  fireEvent.click(fold.querySelector('summary')!)
  expect(fold.open).toBe(true)
  expect(fetchMock.mock.calls).toHaveLength(calls)
})

test.each(['message', 'agent_finish', 'no_finish'] as const)('App 不展示 %s 伪造或未关联的省略提示，仍显示原答复', async mode => {
  const f = omittedFixture()
  Object.assign(f.message.payload, { reply_evidence_status: 'omitted_invalid', reply_evidence_warning: omittedWarning, reply_evidence: f.evidence })
  if (mode === 'message') f.finish.payload.result = { status: 'ok', message_seq: 6, reply_to: 1 }
  if (mode === 'agent_finish') f.finish.actor.kind = 'agent'
  if (mode === 'no_finish') f.events.pop()
  await mount(f)
  const chatElement = screen.getByLabelText('聊天记录'), chat = within(chatElement)
  const body = await chat.findByText(String(f.message.payload.text))
  const bubble = body.closest('.message-bubble')!
  expect(chatElement.querySelectorAll('.message-bubble.is-agent')).toHaveLength(1)
  expect(bubble.querySelector('.message-details')).toBeNull()
  expect(chat.queryByText(omittedWarning)).toBeNull()
  expect(chat.queryByText(label)).toBeNull()
  expect(bubble.querySelector('blockquote')).toBeNull()
})

test('App 无引用的旧答复不增加详情或更改正文', async () => {
  const f = fixture()
  delete f.message.payload.reply_evidence_started_seq
  f.start.payload.arguments = { text: f.args.text }
  f.finish.payload.result = { status: 'ok', message_seq: 6, reply_to: 1 }
  await mount(f)
  const chatElement = screen.getByLabelText('聊天记录'), chat = within(chatElement)
  const body = await chat.findByText(String(f.message.payload.text))
  expect(body.closest('.message-bubble')?.querySelector('.message-details')).toBeNull()
  expect(chatElement.querySelectorAll('.message-bubble.is-agent')).toHaveLength(1)
  expect(chat.queryByText(omittedWarning)).toBeNull()
})
