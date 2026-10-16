import { lazy, Suspense } from 'react'
import type { Artifact } from './Artifact'
import { isTeamHost, teamRuns, teamRunStatus, teamStatus } from './team'
import type { TeamEvent, TeamReview, TeamState } from './team'

const ArtifactPreview = lazy(() => import('./Artifact').then((module) => ({ default: module.ArtifactPreview })))

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const excerpt = (value: unknown, limit = 800): string => typeof value === 'string' ? value.slice(0, limit) + (value.length > limit ? '\n（仅显示部分内容）' : '') : ''
const toolNames: Record<string, string> = { workspace_run: '运行命令', workspace_read: '读取文件', workspace_write: '写入文件', workspace_search: '查找文件', workspace_list: '列出目录',
  delegate: '交接工作', send_message: '投递消息', read_artifact: '读取成果', read_background: '读取来源材料', read_receipts: '核对执行回执', read_messages: '补读历史消息', read_checkpoint: '读取工作检查点', read_delivery: '读取交付说明', publish_artifact: '发布成果', publish_workspace_file: '发布本地文件', set_objective: '更新目标', finish_work: '记录交付', wait_for_reply: '等待伙伴回复', request_questions: '发送补充问题', request_review: '请求复核', record_review: '记录复核结论' }

const reviewStatuses: Record<TeamReview['status'], string> = {
  pending: '待复核', changes_requested: '待修复', approved: '已复核通过', outdated: '新版待复核', stale: '历史复核',
}

function TeamReviewStatus({ team, names }: { team: TeamState; names: Record<string, string> }) {
  const reviews = team.reviews ?? []
  if (!reviews.length) return null
  const active = reviews.filter((review) => review.status !== 'approved' && review.status !== 'stale')
  const closed = reviews.filter((review) => review.status === 'approved' || review.status === 'stale')
  const rows = (items: TeamReview[]) => <ul className="mt-2 space-y-2">{items.map((review) => <li key={`${review.root_seq}:${review.request_seq}`}>
    <p><strong>{reviewStatuses[review.status]}</strong> · @{names[review.author] ?? review.author} 修改 · @{names[review.reviewer] ?? review.reviewer} 复核</p>
    <details>
      <summary className="cursor-pointer">文件与反馈 · {review.artifacts.length} 份</summary>
      <ul className="mt-1 space-y-1">{review.artifacts.map((artifact) => <li key={artifact.seq}>{artifact.filename} · 成果 #{artifact.seq}</li>)}</ul>
      {review.feedback && <p className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap">{review.feedback}</p>}
    </details>
  </li>)}</ul>
  return <section aria-label="修复与复核" className="border-t border-[#eaded1] pt-3">
    <h3 className="font-semibold">修复与复核</h3>
    {active.length > 0 && rows(active)}
    {closed.length > 0 && <details className="mt-2">
      <summary className="cursor-pointer font-semibold">已通过与历史 · {closed.length} 项</summary>
      {rows(closed)}
    </details>}
  </section>
}

function ToolReceipt({ event, full = false }: { event: TeamEvent; full?: boolean }) {
  const detail = (value: unknown) => excerpt(value, full ? Number.MAX_SAFE_INTEGER : 800)
  const p = event.payload
  const args = record(p.arguments)
  const result = record(p.result)
  const stdout = record(result.stdout), stderr = record(result.stderr)
  const output = [detail(result.content), detail(stdout.preview ?? stdout.excerpt ?? result.stdout), detail(stderr.preview ?? stderr.excerpt ?? result.stderr)].filter(Boolean).join('\n')
  const rawCommand = args.command ?? result.command
  const command = Array.isArray(rawCommand) ? rawCommand.filter((part): part is string => typeof part === 'string').join(' ') : rawCommand
  const reason = detail(p.reason ?? result.reason ?? result.message ?? record(result.error).message ?? result.error)
  const background = p.name === 'read_background'
  const section = String(args.section ?? result.section ?? '')
  const backgroundSections: Record<string, string> = { index: '材料索引', goal: '来源目标', source: '来源对话', artifact: '来源成果' }
  const source = record(result.source)
  const checkpoint = record(result.checkpoint)
  return <div className="mt-1 space-y-1 whitespace-pre-wrap [overflow-wrap:anywhere]">
    {p.name === 'read_delivery' && <>
      {typeof record(result.delivery).summary === 'string' && <p>交付摘要：{detail(record(result.delivery).summary)}</p>}
      {typeof record(result.delivery).seq === 'number' && <p>交付记录 #{String(record(result.delivery).seq)}</p>}
      {typeof record(result.delivery).verification === 'string' && <p>验证范围：{detail(record(result.delivery).verification)}</p>}
      {result.historical === true && <p>这是历史交付，不代表当前目标已完成。</p>}
    </>}
    {p.name === 'read_checkpoint' && <>
      <p>只读核对持久事件中的工作事实，不读取私有思考，也不代表恢复成功或验收通过。</p>
      {detail(checkpoint.objective) && <p>当前目标：{detail(checkpoint.objective)}</p>}
      {typeof checkpoint.as_of_seq === 'number' && <p>事实截至事件 #{checkpoint.as_of_seq}</p>}
      {typeof checkpoint.open_work_count === 'number' && <p>未交还工作：{checkpoint.open_work_count} 项</p>}
      {typeof checkpoint.pending_operation_count === 'number' && <p>待核对操作：{checkpoint.pending_operation_count} 项</p>}
      {Array.isArray(result.assignments) && result.assignments.slice(0, full ? result.assignments.length : 8).map((value, index) => {
        const assignment = record(value)
        return <p key={index}>原委托 #{typeof assignment.seq === 'number' ? assignment.seq : '待核对'}：{detail(assignment.text)}</p>
      })}
      {(checkpoint.open_work_truncated === true || Array.isArray(result.assignments) && !full && result.assignments.length > 8) && <p>工作索引仅显示部分记录。</p>}
      {result.has_more === true && <p>还有后续委托{typeof result.next_seq === 'number' ? ` · 下一页从 #${result.next_seq} 之后读取` : ''}。</p>}
    </>}
    {background && <>
      <p>读取范围：{backgroundSections[section] ?? '来源材料'} · 创建时冻结的历史参考</p>
      {detail(source.filename) && <p>来源文件：{detail(source.filename)}</p>}
      {section === 'source' && detail(args.source_id ?? source.id) && <p>来源引用：{detail(args.source_id ?? source.id)}</p>}
      {section === 'artifact' && typeof (args.artifact_seq ?? source.seq) === 'number' && <p>来源成果：#{String(args.artifact_seq ?? source.seq)}</p>}
      {typeof result.offset === 'number' && <p>本页起点：{result.offset}{typeof result.total_chars === 'number' ? ` · 全文 ${result.total_chars} 字符` : ''}</p>}
      {result.has_more === true && <p>还有后续材料{typeof result.next_offset === 'number' ? ` · 下一页起点 ${result.next_offset}` : ''}，本页不代表全文。</p>}
    </>}
    {detail(args.path ?? result.path ?? command) && <p className="font-mono">{detail(args.path ?? result.path ?? command)}</p>}
    {detail(args.query) && <p>查找：{detail(args.query)}</p>}
    {detail(p.summary ?? result.summary) && <p>{detail(p.summary ?? result.summary)}</p>}
    {detail(result.filename) && <p>成果文件：{detail(result.filename)}</p>}
    {typeof result.artifact_seq === 'number' && <p>成果记录：#{result.artifact_seq}</p>}
    {typeof result.message_seq === 'number' && <p>已投递消息 #{result.message_seq}</p>}
    {detail(result.goal) && <p>当前目标：{detail(result.goal)}</p>}
    {detail(result.note) && <p>{detail(result.note)}</p>}
    {result.waiting === true && <p>已进入等待，收到新消息后继续。</p>}
    {typeof result.exit_code === 'number' && <p>退出码：{result.exit_code}</p>}
    {typeof result.bytes === 'number' && <p>已返回 {result.bytes} 字节</p>}
    {Array.isArray(result.entries) && <p>返回 {result.entries.length} 项{background ? '来源材料索引' : '目录内容'}</p>}
    {Array.isArray(result.matches) && <p>找到 {result.matches.length} 处匹配</p>}
    {reason && <p>{reason}</p>}
    {output && <pre className="max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px]">{output}</pre>}
    {(result.truncated === true || stdout.truncated === true || stderr.truncated === true || result.complete === false || stdout.complete === false || stderr.complete === false) && <p>回执内容不完整。</p>}
  </div>
}

export function TeamToolProgress({ events, names, full = false }: { events: TeamEvent[]; names: Record<string, string>; full?: boolean }) {
  const receipts = events.filter((event) => isTeamHost(event) && (event.type === 'team.tool_started' || event.type === 'team.tool_finished'))
  const starts = receipts.filter((event) => event.type === 'team.tool_started')
  const finishFor = (start: TeamEvent) => receipts.find((event) => event.type === 'team.tool_finished' && event.seq > start.seq
    && (typeof event.payload.started_seq === 'number' ? event.payload.started_seq === start.seq
      : typeof start.payload.call_id === 'string' && event.payload.call_id === start.payload.call_id)
    && (!event.payload.run_id || !start.payload.run_id || event.payload.run_id === start.payload.run_id)
    && (!event.payload.name || !start.payload.name || event.payload.name === start.payload.name)
    && (!event.payload.role || !start.payload.role || event.payload.role === start.payload.role)
    && (!event.payload.message_seq || !start.payload.message_seq || event.payload.message_seq === start.payload.message_seq))
  const paired = new Set(starts.map((event) => finishFor(event)?.seq))
  const rows = receipts.filter((event) => event.type === 'team.tool_started' || !paired.has(event.seq))
  if (!rows.length) return null
  return <details className="border-t border-[#eaded1] pt-3 text-xs leading-5 text-[#52766d]">
    <summary className="cursor-pointer font-semibold">工具过程 · {rows.length} 项</summary>
    <ol aria-label="团队工具过程" className="mt-3 max-h-80 space-y-3 overflow-auto [overflow-wrap:anywhere]">{rows.map((event) => {
      const finished = event.type === 'team.tool_finished' ? event : finishFor(event)
      const p = finished?.payload ?? event.payload, result = record(p.result)
      const name = excerpt(event.payload.name ?? event.payload.tool, 100) || '工具调用'
      const role = typeof p.role === 'string' ? p.role : event.actor.id
      const status = p.status ?? result.status ?? result.outcome
      const label = !finished ? '已发起，等待回执'
        : result.complete === false || record(result.stdout).complete === false || record(result.stderr).complete === false ? '工具结果待确认'
        : [status, result.outcome].some((value) => ['error', 'failed', 'timeout', 'launch_error', 'blocked'].includes(String(value))) ? '工具未完成'
        : ['ok', 'done', 'completed', 'succeeded', 'passed', 'success'].includes(String(status)) ? '工具已完成' : '工具结果待确认'
      return <li key={event.seq}>
        <p><strong>{toolNames[name] ?? name}</strong> · {label} · @{names[role] ?? role}</p>
        <ToolReceipt event={{ ...event, payload: { ...event.payload, ...p } }} full={full} />
      </li>
    })}</ol>
  </details>
}

export function TeamProgressPanel({ team, events, names }: { team: TeamState; events: TeamEvent[]; names: Record<string, string> }) {
  const runs = teamRuns(team, events)
  const calls = events.filter((event) => isTeamHost(event) && event.type === 'team.model_called')
  return <section aria-label="团队进度" className="mb-5 space-y-3 text-xs leading-5 text-[#52766d] [overflow-wrap:anywhere]">
    <h2 className="text-sm font-semibold text-[#294a40]">团队进度</h2>
    <p className="font-semibold">{teamStatus({ ...team, runs })}</p>
    {team.status === 'done' && <p>店长的交付结论，验证范围以交付说明与实际回执为准。</p>}
    <p className="whitespace-pre-wrap">{team.objective}</p>
    {team.pending_count > 0 && <p>待处理消息：{team.pending_count}</p>}
    {team.checkpoint && team.checkpoint !== team.objective && (team.checkpoint.length > 180
      ? <details><summary className="cursor-pointer font-semibold">交付说明</summary><p className="mt-2 whitespace-pre-wrap">{team.checkpoint}</p></details>
      : <p className="whitespace-pre-wrap">{team.checkpoint}</p>)}
    <TeamReviewStatus team={team} names={names} />
    {runs.length > 0 && <details><summary className="cursor-pointer font-semibold">消息处理 · {runs.length} 项</summary>
      <ul className="mt-2 space-y-2">{runs.map((run, index) => <li key={`${run.message_seq}:${run.role}:${index}`}>
        <p>@{names[run.role ?? ''] ?? run.role ?? '伙伴'} · {teamRunStatus(run.status)}{run.message_seq ? ` · 消息 #${run.message_seq}` : ''}</p>
        {run.reason && <p className="whitespace-pre-wrap">{run.reason}</p>}
      </li>)}</ul>
    </details>}
    {calls.length > 0 && <details><summary className="cursor-pointer font-semibold">模型用量 · {calls.length} 次调用</summary>
      <ul className="mt-2 space-y-1">{calls.map((call) => {
        const p = call.payload, usage = record(p.usage)
        const tokens = p.token_usage === 'unavailable' ? undefined : p.tokens ?? usage.total_tokens
        const input = usage.input_tokens ?? usage.prompt_tokens, output = usage.output_tokens ?? usage.completion_tokens
        const role = typeof p.role === 'string' ? p.role : call.actor.id
        return <li key={call.seq}>@{names[role] ?? role}{typeof tokens === 'number' ? ` · ${tokens} token` : typeof input === 'number' || typeof output === 'number'
          ? `${typeof input === 'number' ? ` · 输入 ${input} token` : ''}${typeof output === 'number' ? ` · 输出 ${output} token` : ''}` : ' · 用量未报告'}</li>
      })}</ul>
    </details>}
    <TeamToolProgress events={events} names={names} />
  </section>
}

export function TeamWorkboard({ team, taskId, artifacts, events, names }: {
  team: TeamState; taskId: string; artifacts: Artifact[]; events: TeamEvent[]; names: Record<string, string>
}) {
  return <div className="space-y-5">
    <TeamProgressPanel team={team} events={events} names={names} />
    <section className="border-t border-[#eaded1] pt-4"><h2 className="mb-3 text-sm font-semibold">已发布成果</h2>
      {artifacts.length ? <Suspense fallback={<p role="status" className="text-xs text-[#52766d]">正在读取成果…</p>}>{[...artifacts].reverse().map((item) => <div key={item.seq} className="mb-3 border-l-2 border-emerald-600 py-1 pl-3"><ArtifactPreview item={item} publisher={names[item.actor ?? ''] ?? item.actor ?? '伙伴'} /></div>)}</Suspense> : <p className="text-xs text-[#728679]">尚未发布成果。</p>}
    </section>
    <details className="border-t border-[#eaded1] pt-4 text-xs text-[#52766d]"><summary className="cursor-pointer font-semibold">过程证据 · {events.length} 条记录</summary><a className="mt-2 inline-block underline" href={`/tasks/${encodeURIComponent(taskId)}`}>打开完整审计回放</a></details>
  </div>
}
