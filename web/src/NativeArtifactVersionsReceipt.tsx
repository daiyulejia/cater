import { archiveEntries, portableArchivePart } from './archiveManifest'
import { isTeamHost } from './team'
import type { TeamEvent } from './team'

type Artifact = { seq: number; filename: string; content_sha256: string; directory: string }
type File = { artifact_seq: number; source_path: string; path: string; bytes: number; sha256: string; executable: boolean }
type Version = { artifact: Artifact | null; latest?: number; current?: boolean }

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
  ? value as Record<string, unknown> : {}
const own = (value: Record<string, unknown>, key: string): unknown => Object.hasOwn(value, key) ? value[key] : undefined
const seq = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value)
const part = (value: unknown): value is string => typeof value === 'string'
  && new TextEncoder().encode(value).length <= 4096 && portableArchivePart(value)
const alias = (value: string) => value.normalize('NFKC').toLowerCase().toUpperCase().toLowerCase()
const only = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).every(key => keys.includes(key))
const artifactKeys = ['seq', 'filename', 'content_sha256', 'directory', 'latest_seq_at_check', 'is_latest_at_check']
const fileKeys = ['artifact_seq', 'source_path', 'path', 'bytes', 'sha256', 'executable']
const protectedNames = new Set(['.git', '.ssh', '.aws', '.azure', '.gnupg', '.kube', '.docker', '.config', '.codex', '.pki',
  '.terraform', '.pgpass', '.my.cnf', '.boto', '.s3cfg', '.vault-token', 'auth.json', 'terraform.tfstate', '.netrc', '_netrc',
  '.npmrc', '.pypirc', '.gitconfig', '.git-credentials', '.bash_history', '.zsh_history', '.python_history', 'credentials',
  'credentials.json', 'secrets', 'secrets.json', 'secrets.yaml', 'secrets.yml', 'gcloud', 'application_default_credentials.json',
  'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'])

function executionPath(value: string): boolean {
  const parts = value.split('/')
  return Array.from(value).length <= 2048 && new TextEncoder().encode(value).length <= 4096 && parts.length <= 32 && parts.every(item => {
    const name = alias(item)
    return part(item) && new TextEncoder().encode(item).length <= 240 && !/~[0-9]/.test(name) && !protectedNames.has(name)
      && !/^\.env|^\.workspace-tmp-|\.env\.|\.(env|pem|key|p12|pfx|keystore)$/.test(name)
  })
}

function version(value: unknown, eventSeq: number): Version {
  const item = record(value)
  const number = own(item, 'seq'), filename = own(item, 'filename'), sha = own(item, 'content_sha256'), directory = own(item, 'directory')
  if (!only(item, artifactKeys) || !seq(number) || number >= eventSeq || !part(filename)
    || new TextEncoder().encode(filename).length > 180 || !hash(sha) || !part(directory)
    || Array.from(directory).length > 64 || !executionPath(`${directory}/__mount__.txt`)) return { artifact: null }
  const artifact = { seq: number, filename, content_sha256: sha, directory }
  const latest = own(item, 'latest_seq_at_check'), current = own(item, 'is_latest_at_check')
  return seq(latest) && latest >= number && latest < eventSeq && typeof current === 'boolean' && current === (latest === number)
    ? { artifact, latest, current } : { artifact }
}

function checkedFiles(value: unknown, artifacts: Artifact[]): File[] | null {
  if (!Array.isArray(value) || !value.length || value.length > 128) return null
  const files: File[] = []
  for (const raw of value) {
    const item = record(raw), source = artifacts.find(artifact => artifact.seq === own(item, 'artifact_seq'))
    const sourcePath = own(item, 'source_path'), path = own(item, 'path'), bytes = own(item, 'bytes')
    const sha = own(item, 'sha256'), executable = own(item, 'executable')
    if (!only(item, fileKeys) || !source || typeof sourcePath !== 'string' || !sourcePath
      || typeof path !== 'string' || path !== `${source.directory}/${sourcePath}` || !executionPath(path)
      || typeof bytes !== 'number' || !Number.isSafeInteger(bytes) || bytes < 0 || bytes > 16 * 1024 * 1024
      || !hash(sha) || typeof executable !== 'boolean') return null
    files.push({ artifact_seq: source.seq, source_path: sourcePath, path, bytes, sha256: sha, executable })
  }
  if (!archiveEntries({ version: 1, total_bytes: files.reduce((total, file) => total + file.bytes, 0),
    files: files.map(({ path, bytes, sha256, executable }) => ({ path, bytes, sha256, executable })) })
    || files.reduce((total, file) => total + file.bytes, 0) > 32 * 1024 * 1024) return null
  const spellings = new Map<string, string>()
  for (const file of files) {
    const parts = file.path.split('/')
    for (let index = 1; index <= parts.length; index++) {
      const spelling = parts.slice(0, index).join('/'), key = alias(spelling)
      if (spellings.has(key) && spellings.get(key) !== spelling) return null
      spellings.set(key, spelling)
    }
  }
  for (const artifact of artifacts) {
    const members = files.filter(file => file.artifact_seq === artifact.seq)
    if (!members.length || (!/\.zip$/i.test(artifact.filename) && (members.length !== 1
      || members[0].source_path !== artifact.filename || members[0].sha256.toLowerCase() !== artifact.content_sha256.toLowerCase()))) return null
  }
  return files
}

function execution(result: Record<string, unknown>, envelope: boolean): string {
  if (!envelope || own(result, 'complete') !== true) return '执行结果待核对'
  const outcome = own(result, 'outcome'), code = own(result, 'exit_code')
  const exit = typeof code === 'number' && Number.isSafeInteger(code)
  if (outcome === 'passed' && exit && code === 0 && own(result, 'status') === 'ok') return '已结束 · 退出码 0'
  if (outcome === 'failed' && exit) return `执行失败 · 退出码 ${code}`
  if ((code === null || exit) && outcome === 'timeout') return '执行超时'
  if (code === null && outcome === 'launch_error') return '未能启动'
  if (code === null && outcome === 'not_started') return '未开始执行'
  return '执行结果待核对'
}

export function NativeArtifactVersionsReceipt({ event, collapsed = false }: { event: TeamEvent; collapsed?: boolean }) {
  if (!isTeamHost(event) || !['id', 'kind', 'role'].every(key => Object.hasOwn(event.actor, key))
    || event.type !== 'team.tool_finished' || (own(event.payload, 'name') ?? own(event.payload, 'tool')) !== 'workspace_run') return null
  const result = record(own(event.payload, 'result')), args = record(own(event.payload, 'arguments'))
  const selection = own(result, 'artifact_selection'), raw = own(result, 'checked_artifacts')
  const hinted = Object.hasOwn(args, 'artifact_versions')
    || Array.isArray(raw) && raw.some(item => Object.hasOwn(record(item), 'directory'))
  if (!hinted && (selection === undefined || selection === 'explicit' || selection === 'current_objective')) return null

  const envelope = seq(event.seq) && selection === 'version_directories'
    && own(result, 'environment') === 'temporary_artifact_copy_not_sandbox' && ['ok', 'error'].includes(own(result, 'status') as string)
  const versions = Array.isArray(raw) ? raw.slice(0, 128).map(item => version(item, event.seq)) : []
  const artifacts = versions.flatMap(item => item.artifact ? [item.artifact] : [])
  const distinct = new Set(artifacts.map(item => item.seq)).size === artifacts.length
    && new Set(artifacts.map(item => alias(item.directory))).size === artifacts.length
  const identities = versions.length > 0 && Array.isArray(raw) && raw.length <= 128 && artifacts.length === versions.length && distinct
  const files = identities ? checkedFiles(own(result, 'checked_files'), artifacts) : null
  const manifest = envelope && identities && files !== null
  const scope = manifest && typeof own(result, 'scope_current') === 'boolean' ? own(result, 'scope_current') as boolean : undefined
  const unchanged = manifest && typeof own(result, 'artifacts_unchanged') === 'boolean' ? own(result, 'artifacts_unchanged') as boolean : undefined
  const recordedValid = own(result, 'artifact_check_valid')
  const valid = scope !== undefined && unchanged !== undefined && typeof recordedValid === 'boolean'
    && recordedValid === (scope && unchanged) ? recordedValid : undefined
  const freshness = versions.map(item => versions.every(other => !item.artifact || !other.artifact
    || item.artifact.filename !== other.artifact.filename || item.latest === undefined || other.latest === undefined
    || item.latest === other.latest) ? item.current : undefined)
  const observedCurrent = freshness.some(value => value === false) ? false
    : freshness.length > 0 && freshness.every(value => value === true) ? true : undefined
  const selected = own(result, 'selected_versions_current')
  const current = manifest && typeof selected === 'boolean' && selected === observedCurrent ? selected : undefined
  const fields = [
    ['命令执行', execution(result, envelope)],
    ['任务权限与确切字节', scope === true ? '回执记录时有效' : scope === false ? '已失效' : '待核对'],
    ['执行副本', unchanged === true ? '未改变' : unchanged === false ? '已改变' : '是否改变待核对'],
    ['确切版本检查', valid === true ? '记录有效' : valid === false ? '记录无效' : '有效性待核对'],
    ['所选版本', current === true ? '均为检查时最新版' : current === false ? '包含检查时的历史版本' : '是否最新待核对'],
  ]
  const body = <div aria-label="版本副本检查回执" className="min-w-0 max-w-full space-y-2 whitespace-pre-wrap [overflow-wrap:anywhere]">
    <p className="font-semibold">版本副本检查</p>
    <p>{envelope ? '检查对象为临时成果副本，非操作系统沙箱；不代表工作区现状。' : '版本选择或执行环境记录待核对。'}</p>
    <dl className="space-y-1">{fields.map(([name, value]) => <div key={name} className="min-w-0 sm:grid sm:grid-cols-[9rem_minmax(0,1fr)] sm:gap-3">
      <dt className="font-semibold">{name}</dt><dd className="min-w-0">{value}</dd>
    </div>)}</dl>
    <p>任务权限与确切字节有效不代表所选版本最新；副本未改变也不代表命令成功。</p>
    <p>检查有效性不等于业务通过、覆盖完整或独立批准；仅展示宿主记录，界面未重新核验文件字节。</p>
    {!manifest && <p>版本或副本清单不完整或不一致，检查范围待核对。</p>}
    {manifest && valid === undefined && <p>检查有效性字段缺失或相互矛盾，不能确认记录有效。</p>}
    {versions.length > 0 && <ul aria-label="版本与挂载目录" tabIndex={0} className="max-h-48 min-w-0 max-w-full overflow-x-hidden overflow-y-auto">
      {versions.map((item, index) => <li key={index} className="min-w-0 py-1">
        {envelope && identities && item.artifact ? <>
          <p className="font-semibold">成果版本 #{item.artifact.seq} · {item.artifact.filename}</p>
          <p>挂载目录：<code>{item.artifact.directory}/</code></p>
          <p>{freshness[index] === true ? `检查时为最新版 #${item.latest}`
            : freshness[index] === false ? `检查时为历史版本 · 当时最新版 #${item.latest}` : '该版本是否最新待核对'}</p>
        </> : <p>第 {index + 1} 项版本或挂载记录待核对。</p>}
      </li>)}
    </ul>}
    {Array.isArray(raw) && raw.length > 128 && <p>版本清单超过范围；仅显示前 128 项占位，未确认完整清单。</p>}
    {manifest && files && <details key={`files:${event.seq}`} className="min-w-0 max-w-full">
      <summary className="cursor-pointer font-semibold">原路径 → 执行路径 · {files.length} 项</summary>
      <p>执行路径为临时副本内实际挂载的相对路径。</p>
      <ul aria-label="原路径与实际执行路径" tabIndex={0} className="mt-1 max-h-64 min-w-0 max-w-full overflow-x-hidden overflow-y-auto">
        {files.map(file => <li key={file.path} className="min-w-0 space-y-1 py-1">
          <p>来源成果版本 #{file.artifact_seq}</p>
          <p>原路径：<code>{file.source_path}</code></p>
          <p>执行路径：<code>{file.path}</code></p>
          <p>{file.bytes} 字节 · {file.executable ? '有可执行标记' : '无可执行标记'}</p>
        </li>)}
      </ul>
      <p>执行标记为记录值，不代表本平台已核对权限位或已执行每个文件。</p>
    </details>}
    {manifest && files && <details key={`hashes:${event.seq}`} className="min-w-0 max-w-full">
      <summary className="cursor-pointer font-semibold">版本与副本指纹</summary>
      <div tabIndex={0} aria-label="版本与副本 SHA-256" className="mt-1 max-h-48 min-w-0 max-w-full space-y-2 overflow-x-hidden overflow-y-auto font-mono">
        {artifacts.map(item => <p key={item.seq}>版本 #{item.seq} · {item.filename}：{item.content_sha256}</p>)}
        {files.map(file => <p key={file.path}>副本 {file.path}：{file.sha256}</p>)}
      </div>
    </details>}
  </div>
  return collapsed ? <details className="min-w-0 max-w-full">
    <summary className="cursor-pointer font-semibold">版本副本检查详情</summary>
    {body}
  </details> : body
}
