import { expect, test } from 'vitest'
import { consultationSources } from '../src/consultationSource'
import { conversationActivity } from '../src/conversationActivityState'
import { replyEvidenceDetailsFor, replyEvidenceFor } from '../src/replyEvidence'
import type { TeamEvent, TeamState } from '../src/team'

const host = { id: 'team', kind: 'system', role: 'team' }
const manager = { id: 'store_manager', kind: 'agent', role: '\u732b\u5496\u5e97\u957f' }
const worker = { id: 'product', kind: 'agent', role: '\u9700\u6c42\u4e0e\u4f53\u9a8c\u732b' }
const peer = { id: 'test', kind: 'agent', role: 'tester' }
const human = { id: 'human', kind: 'human', role: 'human' }
const contract = { version: 1, kind: 'atomic_local_events', receipt: 'same_transaction' }
const row = (seq: number, type: string, payload: Record<string, unknown>, actor = host): TeamEvent =>
  ({ seq, type, payload, actor: { ...actor } })
const argsOf = (event: TeamEvent) => event.payload.arguments as Record<string, unknown>
const resultOf = (event: TeamEvent) => event.payload.result as Record<string, unknown>
const wrapper = (value: unknown) => ({ encoding: 'escaped_json', json: JSON.stringify(value) })
const omittedWarning = '\u53ef\u9009\u7b54\u590d\u5f15\u7528\u672a\u901a\u8fc7\u6821\u9a8c\uff0c\u672a\u4fdd\u5b58\uff1b\u7b54\u590d\u6b63\u6587\u5df2\u53d1\u9001\uff0c\u4e0d\u4ee3\u8868\u5185\u5bb9\u5df2\u9a8c\u8bc1\u3002'

// Native offline TeamDriver trace (25 Oct): volatile run ids and unrelated recovery/path fields omitted.
function nativeFixture(mode: 'done' | 'running' | 'zero' = 'done') {
  const revision = mode === 'zero' ? 0 : 20
  const binding = (role: string, run_id: string, message_seq: number) => ({ role, run_id, message_seq, revision })
  const original = binding(manager.id, 'human-run', 24), child = binding(worker.id, 'worker-run', 28)
  const resumed = binding(manager.id, 'report-run', 39)
  const input = row(24, 'team.message_received', { kind: 'message', recipient: manager.id, request_id: null, text: 'Check existing evidence.' }, human)
  const run = row(25, 'team.run_started', original)
  const requestStart = row(27, 'team.tool_started', { ...original, name: 'delegate', call_id: 'call-0', execution_contract: { ...contract },
    arguments: { instruction: 'Inspect existing source and return evidence.', recipient: worker.id } })
  const request = row(28, 'team.message_received', { kind: 'assignment', parent_assignment_seq: null, recipient: worker.id,
    reply_to: null, revision, text: 'Inspect existing source and return evidence.' }, manager)
  const requestFinish = row(29, 'team.tool_finished', { name: 'delegate', role: manager.id, run_id: original.run_id, started_seq: 27,
    result: { message_seq: 28, recipient: worker.id, status: 'ok' } })
  const originalEnd = row(33, 'team.run_finished', { message_seq: 24, role: manager.id, run_id: original.run_id, status: 'waiting' })
  const childRun = row(34, 'team.run_started', child)
  const readStart = row(36, 'team.tool_started', { ...child, name: 'read_artifact', call_id: 'call-0', arguments: { artifact_seq: 9 } })
  const receipt = { ...row(37, 'team.tool_finished', { name: 'read_artifact', role: worker.id, run_id: child.run_id, started_seq: 36,
    result: { status: 'ok', seq: 9, filename: 'source.py', content: 'print(17 + 29)\n' } }),
    hash: '013c32db1d9a463f13aaab24d28b587ec07422d500c5c5a57b7f9a03a863430f' }
  const reportStart = row(38, 'team.tool_started', { ...child, name: 'return_work', call_id: 'call-1', execution_contract: { ...contract },
    arguments: { text: 'The existing source says 17 + 29 = 46.' } })
  const report = row(39, 'team.message_received', { kind: 'report', parent_assignment_seq: 28, recipient: manager.id, reply_to: 28,
    revision, text: 'The existing source says 17 + 29 = 46.', visible: true }, worker)
  const reportFinish = row(40, 'team.tool_finished', { name: 'return_work', role: worker.id, run_id: child.run_id, started_seq: 38,
    result: { message_seq: 39, status: 'ok' } })
  const childEnd = row(41, 'team.run_finished', { message_seq: 28, role: worker.id, run_id: child.run_id, status: 'completed' })
  const answerRun = row(42, 'team.run_started', resumed)
  const refs = [{ source_seq: 37, quote: 'print(17 + 29)' }]
  const answerStart = row(44, 'team.tool_started', { ...resumed, name: 'reply_to_customer', call_id: 'call-0', execution_contract: { ...contract },
    arguments: { evidence: refs, text: 'The source says 17 + 29 = 46.' } })
  const answer = row(45, 'msg.said', { audience: 'customer', customer_reply_tool: 'reply_to_customer', phase: 'team', recipient: 'human',
    reply_evidence_started_seq: 44, reply_to: 24, response_kind: 'reply', run_id: resumed.run_id, text: 'The source says 17 + 29 = 46.' }, manager)
  const evidence = { version: 1 as const, note: 'Historical excerpts do not certify correctness.', sources: [
    { kind: 'tool_receipt' as const, quote: refs[0].quote, role: worker.id, source_hash: receipt.hash, source_seq: 37, status: 'ok', tool: 'read_artifact' },
  ] }
  const answerFinish = row(46, 'team.tool_finished', { name: 'reply_to_customer', role: manager.id, run_id: resumed.run_id, started_seq: 44,
    result: { message_seq: 45, reply_evidence: evidence, reply_to: 24, status: 'ok' } })
  const answerEnd = row(47, 'team.run_finished', { message_seq: 39, role: manager.id, run_id: resumed.run_id, status: 'completed' })
  const events = [input, run, requestStart, request, requestFinish, originalEnd, childRun, readStart, receipt,
    reportStart, report, reportFinish, childEnd, answerRun, answerStart, answer, answerFinish, answerEnd]
  if (revision) events.unshift(row(20, mode === 'done' ? 'team.objective_completed' : 'team.objective_updated', { summary: 'Original delivery' }))
  return { events, revision, mode, input, run, requestStart, request, requestFinish, originalEnd, childRun, readStart, receipt,
    reportStart, report, reportFinish, childEnd, answerRun, answerStart, answer, answerFinish, answerEnd, evidence }
}
type Fixture = ReturnType<typeof nativeFixture>
const activity = (f: Fixture) => {
  const team: TeamState = { enabled: true, objective: 'Original delivery', status: f.mode === 'done' ? 'done' : 'running', revision: f.revision,
    pending_count: 0, runs: [], assignments: [], checkpoint: '' }
  return conversationActivity({ team, events: f.events, names: { [manager.id]: 'Manager' }, leaderId: manager.id,
    sending: false, executionState: 'running' })
}
const answered = (f: Fixture) => activity(f).message?.includes('\u5df2\u56de\u5e94\u6700\u65b0\u6d88\u606f') ?? false
const source = (f: Fixture) => consultationSources(f.events).customerSource(f.report, manager.id, f.answerStart.seq, manager.role)
function fallback(f: Fixture) {
  f.events = f.events.filter(event => event !== f.answerStart && event !== f.answerFinish)
  delete f.answer.payload.customer_reply_tool
  delete f.answer.payload.reply_evidence_started_seq
  return f
}

test.each(['done', 'running', 'zero'] as const)('native %s consultation binds evidence and the resumed answer run', mode => {
  const f = nativeFixture(mode), saved = JSON.stringify(f.events)
  expect(source(f)).toBe(f.input)
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(answered(f)).toBe(true)
  expect(activity(f).visible).toBe(mode !== 'done')
  expect(replyEvidenceFor([...f.events].reverse()).get(45)).toBe(f.evidence)
  expect(JSON.stringify(f.events)).toBe(saved)
})

test('native ordinary final fallback answers from the report run without evidence', () => {
  const f = fallback(nativeFixture())
  expect(consultationSources(f.events).answerSource(f.answer)).toBe(f.input)
  expect(answered(f)).toBe(true)
  expect(activity(f).visible).toBe(false)
  expect(replyEvidenceFor(f.events).size).toBe(0)
})

test('a paired explicit reply without optional evidence still answers the customer', () => {
  const f = nativeFixture()
  delete argsOf(f.answerStart).evidence
  delete resultOf(f.answerFinish).reply_evidence
  delete f.answer.payload.reply_evidence_started_seq
  expect(answered(f)).toBe(true)
  expect(replyEvidenceDetailsFor(f.events).size).toBe(0)
})

test('send_message creates both an ordinary request and its report', () => {
  const f = nativeFixture()
  f.requestStart.payload.name = f.requestFinish.payload.name = 'send_message'
  f.requestStart.payload.arguments = { recipient: worker.id, text: f.request.payload.text, notify_only: false }
  f.request.payload.kind = 'message'
  f.reportStart.payload.name = f.reportFinish.payload.name = 'send_message'
  f.reportStart.payload.arguments = { recipient: manager.id, text: f.report.payload.text, reply_to: f.request.seq }
  expect(source(f)).toBe(f.input)
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(answered(f)).toBe(true)
})

test.each([10, 22])('old role titles in unrelated history at #%s do not poison a new consistent consultation', seq => {
  const f = nativeFixture()
  f.events.push(row(seq, 'msg.said', { phase: 'team', text: 'Previous work.' }, { ...manager, role: 'Previous manager title' }),
    row(seq + 1, 'team.message_received', { kind: 'message', recipient: peer.id, text: 'Previous discussion.' },
      { ...worker, role: 'Previous worker title' }))
  expect(source(f)).toBe(f.input)
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(answered(f)).toBe(true)
  fallback(f)
  expect(consultationSources(f.events).answerSource(f.answer)).toBe(f.input)
  expect(answered(f)).toBe(true)
  f.request.actor.role = 'A mismatched title in the current root request'
  expect(source(f)).toBeUndefined()
  expect(consultationSources(f.events).answerSource(f.answer)).toBeUndefined()
  expect(answered(f)).toBe(false)
})

test.each(['team', 'human'])('configured agent role title %s does not grant or remove provenance', title => {
  const f = nativeFixture()
  f.request.actor.role = f.answer.actor.role = title
  f.report.actor.role = title
  expect(consultationSources(f.events).customerSource(f.report, manager.id, 44, title)).toBe(f.input)
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(answered(f)).toBe(true)
  expect(answered(fallback(f))).toBe(true)
})

test.each(['team', 'human'])('configured agent id %s remains distinct from HOST and Human actor kinds', id => {
  const f = nativeFixture()
  for (const event of f.events) {
    if (event.actor.id === worker.id && event.actor.kind === 'agent') event.actor.id = id
    if (event.payload.role === worker.id) event.payload.role = id
    if (event.payload.recipient === worker.id) event.payload.recipient = id
  }
  argsOf(f.requestStart).recipient = id
  resultOf(f.requestFinish).recipient = id
  f.evidence.sources[0].role = id
  expect(source(f)).toBe(f.input)
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(answered(f)).toBe(true)
  expect(answered(fallback(f))).toBe(true)
})

const invalidChains: [string, (f: Fixture) => void][] = [
  ['request receipt absent', f => { f.events = f.events.filter(e => e !== f.requestFinish) }],
  ['report receipt absent', f => { f.events = f.events.filter(e => e !== f.reportFinish) }],
  ['request tool absent', f => { f.events = f.events.filter(e => e !== f.requestStart) }],
  ['report tool absent', f => { f.events = f.events.filter(e => e !== f.reportStart) }],
  ['request run absent', f => { f.events = f.events.filter(e => e !== f.run) }],
  ['report run absent', f => { f.events = f.events.filter(e => e !== f.childRun) }],
  ['request success claimed by agent', f => { f.requestFinish.actor = { ...manager } }],
  ['report fake HOST', f => { f.reportFinish.actor.role = 'other' }],
  ['request fake HOST', f => { f.requestStart.actor.kind = 'agent' }],
  ['report fake run actor', f => { f.childRun.actor.id = 'other' }],
  ['request sender role mismatch', f => { f.request.actor.role = 'imposter' }],
  ['report sender role empty', f => { f.report.actor.role = '' }],
  ['report sender id mismatch', f => { f.report.actor.id = peer.id }],
  ['report recipient mismatch', f => { f.report.payload.recipient = peer.id }],
  ['request recipient mismatch', f => { f.request.payload.recipient = peer.id }],
  ['request arguments recipient mismatch', f => { argsOf(f.requestStart).recipient = peer.id }],
  ['request text mismatch', f => { argsOf(f.requestStart).instruction = 'different' }],
  ['report text mismatch', f => { argsOf(f.reportStart).text = 'different' }],
  ['request not atomic', f => { delete f.requestStart.payload.execution_contract }],
  ['report contract expanded', f => { f.reportStart.payload.execution_contract = { ...contract, extra: true } }],
  ['report contract bad scalar', f => { f.reportStart.payload.execution_contract = { ...contract, version: true } }],
  ['request run wrong revision', f => { f.run.payload.revision = 0 }],
  ['report run wrong revision', f => { f.childRun.payload.revision = 0 }],
  ['request revision boolean', f => { f.request.payload.revision = true }],
  ['report revision string', f => { f.report.payload.revision = '20' }],
  ['report result message seq string', f => { resultOf(f.reportFinish).message_seq = '39' }],
  ['report result message seq boolean', f => { resultOf(f.reportFinish).message_seq = true }],
  ['report started seq string', f => { f.reportFinish.payload.started_seq = '38' }],
  ['request input seq boolean', f => { f.requestStart.payload.message_seq = true }],
  ['report run id disagreement', f => { f.reportFinish.payload.run_id = 'other' }],
  ['report call id disagreement', f => { f.reportFinish.payload.call_id = 'other' }],
  ['report finish revision disagreement', f => { f.reportFinish.payload.revision = 0 }],
  ['report finish input disagreement', f => { f.reportFinish.payload.message_seq = 24 }],
  ['report run input disagreement', f => { f.childRun.payload.message_seq = 24 }],
  ['request finished after its run', f => { f.originalEnd.seq = 26 }],
  ['report finished after its run', f => { f.childEnd.seq = 35 }],
  ['run ends between message and receipt', f => { f.childEnd.seq = 40; f.reportFinish.seq = 41 }],
  ['run end binding disagrees', f => { f.childEnd.payload.role = manager.id }],
  ['request receipt later than receiving run', f => { f.requestFinish.seq = 35 }],
  ['report receipt later than reply prefix', f => { f.reportFinish.seq = 48; f.childEnd.seq = 49 }],
  ['request created before tool', f => { f.requestStart.seq = 30; f.requestFinish.payload.started_seq = 30 }],
  ['report created after receipt', f => { f.reportFinish.seq = 38; f.reportStart.seq = 40; f.reportFinish.payload.started_seq = 40 }],
  ['request input after run', f => { f.run.seq = 23 }],
  ['report duplicate run', f => { f.events.push({ ...f.childRun, seq: 35 }) }],
  ['report duplicate finish', f => { f.events.push({ ...f.reportFinish, seq: 43 }) }],
  ['duplicate seq', f => { f.events.push({ ...f.request }) }],
  ['duplicate message receipt via another start', f => { f.events.push(row(43, 'team.tool_finished', { ...f.reportFinish.payload, started_seq: 36 })) }],
  ['formal review request marker', f => { f.request.payload.review_seq = 10 }],
  ['formal review malformed marker', f => { f.request.payload.review_seq = false }],
  ['formal review arguments', f => { argsOf(f.requestStart).review = {} }],
  ['formal review report marker', f => { f.report.payload.review_seq = 10 }],
  ['progress instead of report', f => { f.report.payload.kind = 'progress' }],
  ['notification instead of report', f => { f.report.payload.kind = 'notification' }],
  ['unreturned request instead of report', f => { f.report.payload.kind = 'assignment' }],
  ['notify_only not ordinary', f => { f.reportStart.payload.name = f.reportFinish.payload.name = 'send_message'; f.reportStart.payload.arguments = { recipient: manager.id, text: f.report.payload.text, reply_to: 28, notify_only: true } }],
  ['continuation not ordinary', f => { f.request.payload.continue_assignment_seq = 11 }],
  ['child active assignment', f => { f.request.payload.parent_assignment_seq = 10 }],
  ['bad parent scalar', f => { f.request.payload.parent_assignment_seq = false }],
  ['return_work bound to unrelated input', f => { f.reportStart.payload.message_seq = f.input.seq; f.childRun.payload.message_seq = f.input.seq; f.childEnd.payload.message_seq = f.input.seq }],
  ['new Human to same role', f => { f.events.push(row(43, 'team.message_received', { kind: 'message', recipient: manager.id, text: 'New question' }, human)) }],
  ['new objective', f => { f.events.push(row(43, 'team.objective_updated', { goal: 'New goal' })) }],
  ['new completion revision', f => { f.events.push(row(43, 'team.objective_completed', { summary: 'New delivery' })) }],
  ['Human before version established', f => { f.events.push(row(26, 'team.objective_updated', { goal: 'New goal' })); for (const e of f.events) if ('revision' in e.payload) e.payload.revision = 26 }],
  ['foreign group', f => { for (const e of f.events) Object.assign(e, { task_id: 'group-a' }); Object.assign(f.report, { task_id: 'group-b' }) }],
  ['fake Human identity', f => { f.input.actor.role = 'other' }],
]

test.each(invalidChains)('%s cannot create citation authority or answer the customer', (_name, mutate) => {
  const f = nativeFixture()
  mutate(f)
  expect(source(f)).toBeUndefined()
  expect(replyEvidenceDetailsFor(f.events).size).toBe(0)
  expect(answered(f)).toBe(false)
  expect(answered(fallback(f))).toBe(false)
})

test('atomic audit arguments decode strictly without granting authority', () => {
  const f = nativeFixture()
  f.requestStart.payload.arguments = wrapper(argsOf(f.requestStart))
  f.reportStart.payload.arguments = wrapper(argsOf(f.reportStart))
  expect(source(f)).toBe(f.input)
  expect(answered(f)).toBe(true)
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
})

test('another role receiving a new Human preserves the original reply without answering that new question', () => {
  const f = nativeFixture()
  const other = row(43, 'team.message_received', { kind: 'message', recipient: peer.id, text: 'An independent question' }, human)
  f.events.push(other)
  expect(source(f)).toBe(f.input)
  expect(consultationSources(f.events).answerSource(f.answer)).toBe(f.input)
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(answered(f)).toBe(false)
  expect(activity(f).messageRole).toBe(peer.id)
  expect(activity(f).visible).toBe(true)
  fallback(f)
  expect(consultationSources(f.events).answerSource(f.answer)).toBe(f.input)
  expect(answered(f)).toBe(false)
})

test.each(['{"text":"bad","text":"The existing source says 17 + 29 = 46."}', '{"text":"x","extra":1e999}',
  '{"text":"x","extra":{"a":1,"a":2}}', '[1]', 'null', '{'])('invalid audit JSON is not a consultation: %s', json => {
  const f = nativeFixture()
  f.reportStart.payload.arguments = { encoding: 'escaped_json', json }
  expect(source(f)).toBeUndefined()
  expect(replyEvidenceFor(f.events).size).toBe(0)
  expect(answered(f)).toBe(false)
})

test('omitted evidence warning survives a genuine consultation with wrapped reply arguments', () => {
  const f = nativeFixture()
  const args = { text: f.answer.payload.text, evidence: [{ source_seq: 37, quote: '\ud800' }] }
  f.answerStart.payload.arguments = wrapper(args)
  delete resultOf(f.answerFinish).reply_evidence
  Object.assign(resultOf(f.answerFinish), { reply_evidence_status: 'omitted_invalid', reply_evidence_warning: omittedWarning })
  expect(replyEvidenceDetailsFor(f.events).get(45)).toEqual({ warning: omittedWarning })
  expect(answered(f)).toBe(true)
})

test('later Human and objective changes preserve historical citations but retire current consultation authority', () => {
  const f = nativeFixture()
  f.events.push(row(48, 'team.message_received', { kind: 'message', recipient: peer.id, text: 'Next question' }, human),
    row(49, 'team.objective_updated', { goal: 'Next goal' }))
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(consultationSources(f.events).answerSource(f.answer)).toBeUndefined()
  expect(consultationSources(f.events).answerSource(f.answer, false)).toBe(f.input)
})

test.each([manager.id, peer.id])('a later Human to %s never erases committed evidence or inherits the old answer', recipient => {
  const f = nativeFixture()
  f.events.push(row(48, 'team.message_received', { kind: 'message', recipient, text: 'A later question.' }, human))
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(consultationSources(f.events).answerSource(f.answer)).toBe(recipient === manager.id ? undefined : f.input)
  expect(answered(f)).toBe(false)
  expect(activity(f).messageRole).toBe(recipient)
})

test.each(['team.objective_updated', 'team.message_received'])('historical evidence uses the reply tool prefix, before later %s', type => {
  const f = nativeFixture()
  f.answer.seq = 48
  f.answerFinish.seq = 49
  f.answerEnd.seq = 50
  resultOf(f.answerFinish).message_seq = 48
  f.events.push(type === 'team.message_received'
    ? row(45, type, { kind: 'message', recipient: manager.id, text: 'Later input' }, human)
    : row(45, type, { goal: 'Later goal' }))
  expect(replyEvidenceFor(f.events).get(48)).toBe(f.evidence)
  expect(consultationSources(f.events).answerSource(f.answer)).toBeUndefined()
})

const invalidAnswers: [string, (f: Fixture) => void][] = [
  ['reply from another role', f => { f.answer.actor = { ...peer } }],
  ['reply actor role mismatch', f => { f.answer.actor.role = 'imposter' }],
  ['HOST success prose', f => { f.answer.actor = { ...host }; f.answer.payload.speaker = manager.id }],
  ['reply run wrong revision', f => { f.answerRun.payload.revision = 0 }],
  ['reply finish wrong call', f => { f.answerFinish.payload.call_id = 'other' }],
  ['reply finish wrong input', f => { f.answerFinish.payload.message_seq = 24 }],
  ['reply finish absent', f => { f.events = f.events.filter(e => e !== f.answerFinish) }],
  ['reply not atomic', f => { delete f.answerStart.payload.execution_contract }],
  ['reply result wrong Human', f => { resultOf(f.answerFinish).reply_to = 28 }],
  ['reply args text differs', f => { argsOf(f.answerStart).text = 'Other answer' }],
  ['reply run duplicate', f => { f.events.push({ ...f.answerRun, seq: 43 }) }],
  ['reply has progress semantics', f => { f.answer.payload.response_kind = 'progress' }],
]
test.each(invalidAnswers)('%s does not turn a report run into a customer answer', (_name, mutate) => {
  const f = nativeFixture()
  mutate(f)
  expect(replyEvidenceFor(f.events).size).toBe(0)
  expect(answered(f)).toBe(false)
})

test.each(['audience', 'recipient', 'phase', 'response_kind', 'run_id', 'reply_to'])('fallback requires its native %s binding', key => {
  const f = fallback(nativeFixture())
  f.answer.payload[key] = 'other'
  expect(answered(f)).toBe(false)
})

test.each(['revision', 'role', 'message_seq', 'run_id'])('fallback rejects a malformed run %s', key => {
  const f = fallback(nativeFixture())
  f.answerRun.payload[key] = key === 'revision' ? 0 : 'other'
  expect(answered(f)).toBe(false)
})

test('fallback cannot drop its report run id and inherit the legacy direct Human path', () => {
  const f = fallback(nativeFixture())
  delete f.answer.payload.run_id
  expect(answered(f)).toBe(false)
})

// Build longer native-shaped conversations while retaining the real trace's source receipt.
function extension(f: Fixture) {
  let next = Math.max(...f.events.map(e => e.seq)) + 1
  const emit = (type: string, payload: Record<string, unknown>, actor = host) => {
    const event = row(next++, type, payload, actor)
    f.events.push(event)
    return event
  }
  const run = (input: TeamEvent, actor: typeof manager) => emit('team.run_started', {
    role: actor.id, run_id: `run-${next}`, message_seq: input.seq, revision: f.revision,
  })
  const end = (run: TeamEvent, status = 'completed') => emit('team.run_finished', { ...run.payload, status })
  const send = (run: TeamEvent, actor: typeof manager, recipient: string, name: string, kind: string,
    replyTo: number | null, parent: number | null = null) => {
    const text = `Evidence consultation ${next}`
    const args = name === 'delegate' ? { recipient, instruction: text } : name === 'return_work' ? { text }
      : { recipient, text, ...(replyTo !== null ? { reply_to: replyTo } : {}) }
    const start = emit('team.tool_started', { ...run.payload, name, call_id: `call-${next}`, arguments: args, execution_contract: { ...contract } })
    const message = emit('team.message_received', { kind, text, revision: f.revision, recipient, reply_to: replyTo, parent_assignment_seq: parent }, actor)
    emit('team.tool_finished', { started_seq: start.seq, name, role: actor.id, run_id: run.payload.run_id,
      result: { status: 'ok', message_seq: message.seq } })
    return message
  }
  const answer = (input: TeamEvent, actor = manager, explicit = false) => {
    const started = run(input, actor)
    const tool = explicit ? emit('team.tool_started', { ...started.payload, name: 'reply_to_customer', call_id: `call-${next}`,
      execution_contract: { ...contract }, arguments: { text: 'Source inspected.', evidence: [{ source_seq: 37, quote: 'print(17 + 29)' }] } }) : undefined
    const message = emit('msg.said', { text: 'Source inspected.', phase: 'team', reply_to: f.input.seq,
      run_id: started.payload.run_id, audience: 'customer', recipient: 'human', response_kind: 'reply',
      ...(tool ? { customer_reply_tool: 'reply_to_customer', reply_evidence_started_seq: tool.seq } : {}) }, actor)
    if (tool) emit('team.tool_finished', { started_seq: tool.seq, name: 'reply_to_customer', role: actor.id,
      run_id: started.payload.run_id, result: { status: 'ok', message_seq: message.seq, reply_to: f.input.seq, reply_evidence: f.evidence } })
    end(started)
    return message
  }
  return { emit, run, end, send, answer }
}

function anotherRequest(f: Fixture) {
  const text = 'A separate consultation between the same two roles.'
  const start = row(30, 'team.tool_started', { ...f.requestStart.payload, call_id: 'second-call',
    arguments: { recipient: worker.id, instruction: text } })
  const request = row(31, 'team.message_received', { ...f.request.payload, text }, manager)
  const finish = row(32, 'team.tool_finished', { ...f.requestFinish.payload, started_seq: 30,
    result: { status: 'ok', message_seq: 31, recipient: worker.id } })
  f.events.push(start, request, finish)
  return request
}

test.each([false, true])('a real return_work cannot be retargeted to another consultation (parent also changed: %s)', changeParent => {
  const f = nativeFixture(), other = anotherRequest(f)
  expect(source(f)).toBe(f.input)
  const pair = JSON.stringify([f.reportStart, f.reportFinish])
  f.report.payload.reply_to = other.seq
  if (changeParent) f.report.payload.parent_assignment_seq = other.seq
  expect(JSON.stringify([f.reportStart, f.reportFinish])).toBe(pair)
  expect(source(f)).toBeUndefined()
  expect(replyEvidenceFor(f.events).size).toBe(0)
  expect(answered(f)).toBe(false)
  expect(answered(fallback(f))).toBe(false)
})

test.each(['continue_work', 'send_message'])('%s authenticates continuation input and restores its original active request', name => {
  const f = nativeFixture(), other = anotherRequest(f)
  f.events = f.events.filter(e => e.seq <= 37)
  const x = extension(f)
  x.end(f.childRun, 'waiting')
  const followup = x.emit('team.message_received', { kind: 'message', text: 'Continue that consultation.', recipient: manager.id }, human)
  const sender = x.run(followup, manager), text = 'Inspect the original source again.'
  const start = x.emit('team.tool_started', { ...sender.payload, name, call_id: 'continue-call', execution_contract: { ...contract },
    arguments: name === 'continue_work' ? { assignment_seq: f.request.seq, instruction: text }
      : { recipient: worker.id, text, continue_assignment_seq: f.request.seq } })
  const continued = x.emit('team.message_received', { kind: 'continuation', recipient: worker.id, text, revision: f.revision,
    reply_to: null, continue_assignment_seq: f.request.seq, parent_assignment_seq: null, visible: true }, manager)
  x.emit('team.tool_finished', { started_seq: start.seq, name, role: manager.id, run_id: sender.payload.run_id,
    result: { status: 'ok', message_seq: continued.seq } })
  x.end(sender, 'waiting')
  const resumed = x.run(continued, worker)
  const report = x.send(resumed, worker, manager.id, 'return_work', 'report', f.request.seq, f.request.seq)
  x.end(resumed)
  const resolver = consultationSources(f.events)
  expect(resolver.activeAssignment(continued, worker.id, report.seq - 1)).toBe(f.request)
  expect(resolver.messageInput(report)).toBe(continued)
  report.payload.reply_to = report.payload.parent_assignment_seq = other.seq
  expect(consultationSources(f.events).messageInput(report)).toBeUndefined()
  report.payload.reply_to = report.payload.parent_assignment_seq = f.request.seq
  continued.payload.continue_assignment_seq = other.seq
  expect(consultationSources(f.events).messageInput(report)).toBeUndefined()
})

function questionnaireFixture(structured: boolean) {
  const f = nativeFixture(), other = anotherRequest(f)
  f.events = f.events.filter(e => e.seq <= 37)
  const x = extension(f), title = 'Scope', questions = [{ id: 'scope', question: 'What scope?', selection: 'single', options: [] }]
  const start = x.emit('team.tool_started', { ...f.childRun.payload, name: 'request_questions', call_id: 'question-call',
    execution_contract: { ...contract }, arguments: { title, questions } })
  const question = x.emit('team.questions_requested', { title, questions, role_id: worker.id, run_id: f.childRun.payload.run_id,
    revision: f.revision, message_seq: f.request.seq, assignment_seq: f.request.seq })
  const finish = x.emit('team.tool_finished', { started_seq: start.seq, name: 'request_questions', role: worker.id,
    run_id: f.childRun.payload.run_id, result: { status: 'ok', question_seq: question.seq, waiting_for_customer: true } })
  x.end(f.childRun, 'waiting')
  const response = x.emit('team.message_received', { kind: 'message', recipient: worker.id, text: 'Source only.',
    ...(structured ? { questionnaire: { question_seq: question.seq, request_id: 'response-1', answers: [
      { question_id: 'scope', option_ids: [], text: 'Source only.', skipped: false },
    ] }, revision: f.revision } : {}) }, human)
  return { f, x, start, question, finish, response, other }
}

test.each([false, true])('Human questionnaire resumption preserves the real assignment (structured: %s)', structured => {
  const { f, x, response } = questionnaireFixture(structured)
  const resumed = x.run(response, worker)
  const report = x.send(resumed, worker, manager.id, 'return_work', 'report', f.request.seq, f.request.seq)
  x.end(resumed)
  const resolver = consultationSources(f.events)
  expect(resolver.activeAssignment(response, worker.id, report.seq - 1)).toBe(f.request)
  expect(resolver.messageInput(report)).toBe(response)
  expect(resolver.customerSource(report, manager.id)).toBe(f.input)
  const answer = x.answer(report, manager, true)
  expect(replyEvidenceFor(f.events).get(answer.seq)).toBe(f.evidence)
  expect(answered(f)).toBe(false)
  expect(activity(f).messageRole).toBe(worker.id)
})

test.each(['question target', 'report target', 'question receipt', 'question run input', 'question actor', 'bad questionnaire scalar'])('questionnaire inheritance rejects a changed %s', invalid => {
  const { f, x, question, finish, start, response, other } = questionnaireFixture(true)
  const resumed = x.run(response, worker)
  const report = x.send(resumed, worker, manager.id, 'return_work', 'report', f.request.seq, f.request.seq)
  x.end(resumed)
  expect(consultationSources(f.events).messageInput(report)).toBe(response)
  if (invalid === 'question target') question.payload.assignment_seq = other.seq
  if (invalid === 'report target') report.payload.reply_to = report.payload.parent_assignment_seq = other.seq
  if (invalid === 'question receipt') f.events = f.events.filter(e => e !== finish)
  if (invalid === 'question run input') start.payload.message_seq = other.seq
  if (invalid === 'question actor') question.actor = { ...worker }
  if (invalid === 'bad questionnaire scalar') response.payload.questionnaire = true
  expect(consultationSources(f.events).messageInput(report)).toBeUndefined()
})

test.each(['completed', 'waiting', 'failed', 'interrupted'])('a %s Human run controls whether later text inherits the same question', status => {
  const { f, x, response } = questionnaireFixture(false)
  const first = x.run(response, worker)
  x.end(first, status)
  const later = x.emit('team.message_received', { kind: 'message', recipient: worker.id, text: 'Another supplement.' }, human)
  expect(consultationSources(f.events).activeAssignment(later, worker.id)).toBe(
    ['completed', 'waiting'].includes(status) ? undefined : f.request)
})

test('a second consultation can start from an earlier report and answer the original Human', () => {
  const f = nativeFixture()
  f.events = f.events.filter(e => e.seq <= 41)
  const x = extension(f), resumed = x.run(f.report, manager)
  const request = x.send(resumed, manager, peer.id, 'send_message', 'message', null)
  x.end(resumed, 'waiting')
  const child = x.run(request, peer)
  const report = x.send(child, peer, manager.id, 'send_message', 'report', request.seq, request.seq)
  x.end(child)
  const answer = x.answer(report, manager, true)
  expect(consultationSources(f.events).answerSource(answer)).toBe(f.input)
  expect(answered(f)).toBe(true)
  expect(consultationSources(f.events).customerSource(report, manager.id)).toBe(f.input)
  expect(replyEvidenceFor(f.events).get(answer.seq)).toBe(f.evidence)
  f.request.actor.role = 'A mismatched role in this consultation chain'
  expect(consultationSources(f.events).customerSource(report, manager.id)).toBeUndefined()
  expect(replyEvidenceFor(f.events).size).toBe(0)
  expect(answered(f)).toBe(false)
})

test('two-hop return_work accepts the manager reply but rejects the child role answering a customer', () => {
  const f = nativeFixture(), other = anotherRequest(f)
  f.events = f.events.filter(e => e.seq <= 37)
  const x = extension(f)
  const request = x.send(f.childRun, worker, peer.id, 'delegate', 'assignment', null, f.request.seq)
  x.end(f.childRun, 'waiting')
  const child = x.run(request, peer)
  const childReport = x.send(child, peer, worker.id, 'return_work', 'report', request.seq, request.seq)
  x.end(child)
  const snapshot = [...f.events]
  const childAnswer = x.answer(childReport, worker)
  expect(consultationSources(f.events).answerSource(childAnswer)).toBeUndefined()
  f.events = snapshot
  const resumed = x.run(childReport, worker)
  const report = x.send(resumed, worker, manager.id, 'return_work', 'report', f.request.seq, f.request.seq)
  x.end(resumed)
  const answer = x.answer(report, manager, true)
  expect(consultationSources(f.events).answerSource(answer)).toBe(f.input)
  expect(answered(f)).toBe(true)
  expect(replyEvidenceFor(f.events).get(answer.seq)).toBe(f.evidence)
  request.payload.parent_assignment_seq = other.seq
  report.payload.reply_to = report.payload.parent_assignment_seq = other.seq
  expect(consultationSources(f.events).customerSource(report, manager.id)).toBeUndefined()
  expect(replyEvidenceFor(f.events).size).toBe(0)
  expect(answered(f)).toBe(false)
})

test('the existing direct Human reply and evidence path remains intact', () => {
  const f = nativeFixture()
  f.answerStart.payload.message_seq = f.input.seq
  f.answerStart.payload.run_id = f.run.payload.run_id
  f.answer.payload.run_id = f.run.payload.run_id
  f.answerFinish.payload.run_id = f.run.payload.run_id
  f.events = f.events.filter(e => ![f.originalEnd, f.answerRun, f.answerEnd].includes(e))
  expect(replyEvidenceFor(f.events).get(45)).toBe(f.evidence)
  expect(answered(f)).toBe(true)
})
