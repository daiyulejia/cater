import { useState } from 'react'
import { LucideCopy } from 'lucide-react'
import { CommandOutputs } from './CommandOutputPreview'
import { isTeamHost } from './team'
import type { TeamEvent } from './team'

type Data = Record<string, unknown>
type Artifact = { seq: number; filename: string; content_sha256: string; directory?: string }
type Output = { excerpt?: string; bytes?: number | null; sha256?: string | null; truncated?: boolean; complete?: boolean | null; utf8_valid?: boolean | null }
type Receipt = {
  version: 1; source_seq: number; source_hash: string; started_seq: number; started_hash: string; role: string; tool: 'workspace_run'
  request: Data | null; request_complete: boolean; stdin: { bytes: number; sha256: string } | null; argv: string[] | null
  authorized_root: string | null; cwd: string | null; environment: string | null; status: 'ok' | 'error'
  outcome: keyof typeof outcomes | null; exit_code: number | null; complete: boolean | null; scope_current: boolean | null
  artifacts_unchanged: boolean | null; artifact_check_valid: boolean | null; selected_versions_current: boolean | null
  stdout: Output | null; stderr: Output | null; checked_artifacts: Artifact[] | null; delivery_artifact_seqs: number[]
}

const outcomes = { passed: '记录为正常结束', failed: '执行失败', timeout: '执行超时', launch_error: '启动失败',
  effects_unknown: '执行影响未知', not_started: '未开始执行' }
const record = (value: unknown): value is Data => value !== null && typeof value === 'object' && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value))
const only = (value: Data, keys: readonly string[]) => Object.keys(value).every(key => keys.includes(key))
const size = (value: string) => new TextEncoder().encode(value).length
const text = (value: unknown, maximum: number, empty = false): value is string => typeof value === 'string'
  && (empty || value.trim().length > 0) && value.length <= maximum && size(value) <= maximum && !value.includes('\0')
const seq = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const nullable = (value: unknown, check: (value: unknown) => boolean) => value === null || check(value)
const boolean = (value: unknown) => typeof value === 'boolean'
const sequenceList = (value: unknown, maximum = 128, distinct = true): value is number[] => Array.isArray(value)
  && value.length <= maximum && value.every(seq) && (!distinct || new Set(value).size === value.length)
const jsonSize = (value: unknown) => size(JSON.stringify(value))

function request(value: unknown): value is Data {
  if (!record(value) || !only(value, ['command', 'args', 'cwd', 'timeout', 'artifact_seqs', 'artifact_versions']) || !text(value.command, 1024)) return false
  if ('cwd' in value && !text(value.cwd, 1024, true)
    || 'args' in value && (!Array.isArray(value.args) || value.args.length > 128 || !value.args.every(item => text(item, 1024, true)))
    || 'timeout' in value && (typeof value.timeout !== 'number' || !Number.isFinite(value.timeout) || value.timeout <= 0 || value.timeout > 86400)
    || 'artifact_seqs' in value && (!sequenceList(value.artifact_seqs, 128, false) || !value.artifact_seqs.length)) return false
  if ('artifact_versions' in value && ('artifact_seqs' in value || !Array.isArray(value.artifact_versions)
    || !value.artifact_versions.length || value.artifact_versions.length > 128
    || !value.artifact_versions.every(item => record(item) && only(item, ['artifact_seq', 'directory']) && seq(item.artifact_seq) && text(item.directory, 1024)))) return false
  return jsonSize(value) <= 1024
}

function output(value: unknown): value is Output {
  return record(value) && Object.keys(value).length > 0 && only(value, ['excerpt', 'bytes', 'sha256', 'truncated', 'complete', 'utf8_valid'])
    && (!('excerpt' in value) || typeof value.excerpt === 'string' && value.excerpt.length <= 512 && size(value.excerpt) <= 512)
    && (!('bytes' in value) || nullable(value.bytes, count)) && (!('sha256' in value) || nullable(value.sha256, hash))
    && (!('truncated' in value) || boolean(value.truncated)) && (!('complete' in value) || nullable(value.complete, boolean))
    && (!('utf8_valid' in value) || nullable(value.utf8_valid, boolean))
}

function displayOutput(value: Output | null) {
  if (typeof value?.excerpt !== 'string') return { output: value, escaped: false }
  const excerpt = value.excerpt.replace(/\p{Cc}/gu, character => ['\n', '\r', '\t'].includes(character)
    ? character : `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`)
  return { output: { ...value, excerpt }, escaped: excerpt !== value.excerpt }
}

const receiptKeys = ['version', 'source_seq', 'source_hash', 'started_seq', 'started_hash', 'role', 'tool', 'request', 'request_complete',
  'stdin', 'argv', 'authorized_root', 'cwd', 'environment', 'status', 'outcome', 'exit_code', 'complete', 'scope_current',
  'artifacts_unchanged', 'artifact_check_valid', 'selected_versions_current', 'stdout', 'stderr', 'checked_artifacts', 'delivery_artifact_seqs']

function receipt(value: unknown, deliverySeq: number): value is Receipt {
  if (!record(value) || !only(value, receiptKeys) || !receiptKeys.every(key => Object.hasOwn(value, key))
    || value.version !== 1 || value.tool !== 'workspace_run' || !seq(value.source_seq) || value.source_seq >= deliverySeq
    || !seq(value.started_seq) || value.started_seq >= value.source_seq
    || !hash(value.source_hash) || !hash(value.started_hash) || !text(value.role, 128)
    || !['ok', 'error'].includes(value.status as string) || !boolean(value.request_complete)
    || !nullable(value.request, request) || !nullable(value.outcome, item => typeof item === 'string' && Object.hasOwn(outcomes, item))
    || !nullable(value.exit_code, item => typeof item === 'number' && Number.isSafeInteger(item) && item >= -(2 ** 31) && item < 2 ** 32)
    || !['complete', 'scope_current', 'artifacts_unchanged', 'artifact_check_valid', 'selected_versions_current'].every(key => nullable(value[key], boolean))
    || !['authorized_root', 'cwd'].every(key => nullable(value[key], item => text(item, 1024, true)))
    || !nullable(value.environment, item => item === 'temporary_artifact_copy_not_sandbox')
    || !nullable(value.stdin, item => record(item) && only(item, ['bytes', 'sha256']) && count(item.bytes) && item.bytes <= 65536 && hash(item.sha256))
    || !nullable(value.argv, item => Array.isArray(item) && item.length <= 1024 && item.every(arg => text(arg, 1024, true)) && jsonSize(item) <= 1024)
    || !nullable(value.stdout, output) || !nullable(value.stderr, output)
    || !sequenceList(value.delivery_artifact_seqs, 8)) return false
  if (!nullable(value.checked_artifacts, items => Array.isArray(items) && items.length <= 8 && items.every(item => record(item)
    && only(item, ['seq', 'filename', 'content_sha256', 'directory']) && seq(item.seq) && item.seq < Number(value.source_seq)
    && text(item.filename, 512) && hash(item.content_sha256) && (!('directory' in item) || text(item.directory, 256)))
    && new Set(items.map(item => item.seq)).size === items.length)) return false
  return jsonSize(value) <= 24 * 1024
}

function matchesDelivery(artifact: Artifact, value: unknown) {
  if (!Array.isArray(value) || value.length > 128) return false
  const references = value.filter((item): item is Data => record(item) && item.seq === artifact.seq)
  return references.length === 1 && (['seq', 'filename', 'content_sha256'] as const)
    .every(key => Object.hasOwn(references[0], key) && references[0][key] === artifact[key])
}

const bodyClass = 'mt-1 max-h-48 min-w-0 max-w-full overflow-auto whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere]'
const foldClass = 'min-w-0 max-w-full'

function Request({ entry }: { entry: Receipt }) {
  const [state, setState] = useState<'idle' | 'copying' | 'copied' | 'failed'>('idle')
  const complete = entry.request_complete && entry.request !== null && text(entry.request.command, 1024) && entry.stdin === null
  const copy = async () => {
    setState('copying')
    try { await navigator.clipboard.writeText(JSON.stringify(entry.request, null, 2)); setState('copied') }
    catch { setState('failed') }
  }
  return <details className={foldClass}>
    <summary className="cursor-pointer font-semibold">实际工具请求</summary>
    <p>这是 Cater 工具请求数据；不等同于终端命令，也不保证能在其他机器直接运行。</p>
    {entry.request === null ? <p>请求正文未记录或已省略，不能完整复现。</p> : <>
      <div className="flex min-w-0 items-start gap-2">
        <pre tabIndex={0} aria-label="实际工具请求正文" className={`${bodyClass} flex-1`}>{JSON.stringify(entry.request, null, 2)}</pre>
        {complete && <button type="button" title="复制完整工具请求" aria-label="复制完整工具请求" disabled={state === 'copying'}
          className="mt-1 flex size-8 shrink-0 items-center justify-center rounded border border-current focus-visible:outline-2"
          onClick={() => void copy()}><LucideCopy size={15} aria-hidden="true" /></button>}
      </div>
      {!complete && <p>请求包含省略项或完整性未知，不能完整复现；省略项可能包括参数或输入正文。</p>}
    </>}
    {entry.stdin && <><p>标准输入正文已省略，仅保留配置输入摘要，不证明程序已读取；不能完整复现。</p>
      <p>配置输入：{entry.stdin.bytes} 字节</p><p>配置输入 SHA-256：{entry.stdin.sha256}</p></>}
    {state !== 'idle' && <p role="status">{state === 'copying' ? '正在复制' : state === 'copied' ? '已复制完整工具请求' : '复制失败，可手动选择请求正文。'}</p>}
  </details>
}

function ReceiptDetails({ entry, historical, name, deliveryArtifacts }: { entry: Receipt; historical: boolean; name?: (id: string) => string; deliveryArtifacts: unknown }) {
  const selected = entry.request && ('artifact_seqs' in entry.request || 'artifact_versions' in entry.request)
  const copy = entry.environment === 'temporary_artifact_copy_not_sandbox'
  const versioned = Boolean(entry.checked_artifacts?.length) || selected
  const command = typeof entry.request?.command === 'string' ? entry.request.command : '命令参数未完整记录'
  const preview = Array.from(command).slice(0, 64).join('').replace(/\s+/gu, ' ')
  const observation = entry.outcome === null ? '执行结果未知' : outcomes[entry.outcome]
  const valid = copy && entry.checked_artifacts?.length && entry.scope_current !== null && entry.artifacts_unchanged !== null
    && entry.artifact_check_valid === (entry.scope_current && entry.artifacts_unchanged) ? entry.artifact_check_valid : null
  const stdout = displayOutput(entry.stdout), stderr = displayOutput(entry.stderr)
  return <details className={foldClass}>
    <summary className="cursor-pointer whitespace-pre-wrap [overflow-wrap:anywhere]">{preview}{Array.from(command).length > 64 ? '（命令名节选）' : ''}
      {' · '}{observation}{entry.exit_code === null ? ' · 退出码未知' : ` · 退出码 ${entry.exit_code}`}</summary>
    <div className="mt-2 min-w-0 space-y-2 [overflow-wrap:anywhere]">
      <p>来源：宿主保存的命令事实 · 完成事件 #{entry.source_seq} · 伙伴：{name?.(entry.role) ?? '协作成员'}</p>
      <p>工具返回：{entry.status === 'ok' ? '正常返回' : '返回错误'} · {entry.complete === null ? '采集是否完成未知' : entry.complete ? '回执采集完整' : '回执采集未完成'}</p>
      <p>执行结果：{observation}。退出状态不证明业务通过、覆盖完整或独立批准。</p>
      {historical && <p>这是历史交付中的冻结记录，不代表当前目标或磁盘状态。</p>}
      <p>{entry.scope_current === null ? '范围有效性未知。' : entry.scope_current ? '回执记录时范围有效，不证明业务覆盖或批准。' : '回执范围已失效，只能作为历史记录。'}</p>
      <details className={foldClass}>
        <summary className="cursor-pointer font-semibold">执行环境与目录</summary>
        <p>工程授权/锁根：{entry.authorized_root ?? '未知'}。这不是外部客户端的工作目录。</p>
        {copy ? <><p>成果副本模式：执行根为已清理的临时成果副本，不是工程授权根，也不是操作系统沙箱。</p>
          <p>实际记录的工作目录（相对临时副本根）：{entry.cwd === null ? '未知' : entry.cwd === '' ? '根目录（空相对路径）' : entry.cwd}</p></>
          : <><p>{versioned ? '记录包含成果版本选择，执行环境未知，不能确认实际副本根。' : '普通工程命令：未绑定交付成果字节，不能据此声称已检查当前交付包。'}</p>
            <p>实际记录的相对工作目录：{entry.cwd === null ? '未知' : entry.cwd === '' ? '根目录（空相对路径）' : entry.cwd}</p>
            <p>执行根未单独记录，不能仅凭授权根拼出客户端执行目录。</p></>}
        {entry.environment === null ? <p>环境字段未记录。</p> : entry.environment !== 'temporary_artifact_copy_not_sandbox' && <p>环境记录值：{entry.environment}</p>}
      </details>
      <Request entry={entry} />
      <details className={foldClass}>
        <summary className="cursor-pointer font-semibold">原环境实际程序参数</summary>
        <p>以下参数来自原环境，不保证在下载机器可直接运行。</p>
        {entry.argv === null ? <p>程序参数未记录或因预算省略，不能补成完整命令。</p>
          : <pre tabIndex={0} aria-label="原环境实际程序参数正文" className={bodyClass}>{JSON.stringify(entry.argv, null, 2)}</pre>}
        {entry.argv?.length === 0 && <p>程序参数记录为空数组，启动状态未由此确认。</p>}
      </details>
      <details className={foldClass}>
        <summary className="cursor-pointer font-semibold">已采集输出</summary>
        <p>输出省略与请求省略、历史范围分别记录，输出片段不补成全文。</p>
        <CommandOutputs stdout={stdout.output} stderr={stderr.output} />
        {([['标准输出', entry.stdout, stdout.escaped], ['错误输出', entry.stderr, stderr.escaped]] as const).map(([label, stream, escaped]) => <div key={label} className="mt-1">
          {escaped && <p>{label}：部分控制字符以 Unicode 转义显示；字节数、SHA-256 和 UTF-8 有效性仍对应原始采集输出。</p>}
          <p>{label}：{stream?.bytes == null ? '字节数未知' : `${stream.bytes} 字节`} · {stream?.truncated === true ? '预览已截断' : stream?.truncated === false ? '预览未标记截断' : '预览截断状态未知'}</p>
          {stream?.complete == null && <p>{label}采集是否完成未知。</p>}
          {stream?.utf8_valid === undefined && <p>{label}的编码有效性未知。</p>}
          <p>{label} SHA-256：{stream?.sha256 ?? '未知'}</p>
        </div>)}
      </details>
      <details className={foldClass}>
        <summary className="cursor-pointer font-semibold">版本与事件来源</summary>
        <div tabIndex={0} aria-label="命令版本与事件来源" className="mt-1 max-h-64 min-w-0 space-y-1 overflow-auto whitespace-pre-wrap [overflow-wrap:anywhere]">
          <p>完成事件 #{entry.source_seq} · 指纹：{entry.source_hash}</p>
          <p>开始事件 #{entry.started_seq} · 指纹：{entry.started_hash}</p>
          <p>执行副本：{entry.artifacts_unchanged === null ? '是否改变未知' : entry.artifacts_unchanged ? '记录为未改变' : '记录为已改变'}</p>
          <p>确切版本检查：{valid === null ? '有效性未知或字段不一致' : valid ? '回执记录有效' : '回执记录无效'}</p>
          <p>所选版本：{!entry.checked_artifacts?.length || entry.selected_versions_current === null ? '是否最新未知' : entry.selected_versions_current ? '记录为检查时最新版' : '包含检查时的历史版本'}</p>
          <p>版本最新、范围有效与命令退出状态各自独立，不代表验收通过。</p>
          {!entry.checked_artifacts?.length ? <p>未记录完整的成果版本引用，未确认与本次交付的关联。</p> : <ul className="space-y-2">
            {entry.checked_artifacts.map(artifact => <li key={artifact.seq}>
              <p>成果版本 #{artifact.seq} · {artifact.filename}</p><p>成果 SHA-256：{artifact.content_sha256}</p>
              {artifact.directory && <p>副本挂载目录：{artifact.directory}</p>}
              <p>{entry.delivery_artifact_seqs.includes(artifact.seq) && matchesDelivery(artifact, deliveryArtifacts)
                ? '宿主记录与本次交付的序号、名称和字节指纹相符。' : '未确认与本次交付的关联。'}</p>
            </li>)}
          </ul>}
        </div>
      </details>
    </div>
  </details>
}

export function NativeCommandReceipts({ event, historical = false, name }: { event: TeamEvent; historical?: boolean; name?: (id: string) => string }) {
  if (!record(event.actor) || !['id', 'kind', 'role'].every(key => Object.hasOwn(event.actor, key)) || !isTeamHost(event)
    || event.type !== 'team.objective_completed' || !seq(event.seq) || !record(event.payload)
    || !record(event.payload.handoff) || !Object.hasOwn(event.payload.handoff, 'command_receipts')) return null
  const raw = event.payload.handoff.command_receipts
  if (Array.isArray(raw) && raw.length === 0) return null
  const entries = Array.isArray(raw) && raw.length <= 4 ? raw.map(value => receipt(value, event.seq) ? value : null) : null
  const distinct = entries && new Set(entries.flatMap(entry => entry ? [entry.source_seq] : [])).size === entries.filter(Boolean).length
    && jsonSize(entries) <= 24 * 1024
  return <details key={`${event.seq}:${event.hash ?? ''}`} className="min-w-0 max-w-full">
    <summary className="cursor-pointer font-semibold">实际命令记录</summary>
    <div className="mt-2 min-w-0 space-y-2 [overflow-wrap:anywhere]">
      <p>宿主冻结的来源事实；查看和复制不会执行命令。成员建议另列。</p>
      {!entries || !distinct ? <p>命令记录格式无法识别，未作为执行依据。</p> : entries.map((entry, index) => entry
        ? <ReceiptDetails key={`${event.seq}:${entry.source_seq}:${entry.source_hash}`} entry={entry} historical={historical} name={name} deliveryArtifacts={event.payload.artifacts} />
        : <p key={index}>第 {index + 1} 条命令记录不完整或格式无法识别，未作为执行依据。</p>)}
    </div>
  </details>
}
