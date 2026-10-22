import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Check, Copy, FilePenLine, FileSearch, Folder, FolderCog, LoaderCircle, RefreshCw, Save, Terminal, Unlink, X } from 'lucide-react'
import { sameWorkScope } from './participants'
import { CommandOutputs } from './CommandOutputPreview'

export type WorkspaceMode = 'read_only' | 'edit' | 'execute'
export type WorkspaceOption = { id: string; title: string; root: string; mode: WorkspaceMode; commands: string[]; managed?: boolean }
export type WorkspaceBinding = {
  revision: number | null; workspace_id: string | null; title?: string; root?: string; mode?: WorkspaceMode
  available: boolean; reason: string; managed?: boolean; pending: { seq: number; task_id: string; name: string }[]
}
export type WorkspaceChange = { workspace_id: string | null; expected_revision: number | null }
type Props = {
  draft?: false
  taskId: string; workspace?: WorkspaceBinding; active: boolean; disabled?: boolean
  loadOptions: () => Promise<WorkspaceOption[]>
  onBind: (change: WorkspaceChange) => Promise<WorkspaceBinding | undefined>
  onRefresh: () => Promise<WorkspaceBinding | undefined>
} | {
  draft: true; workspaceId?: string | null; disabled?: boolean
  loadOptions: () => Promise<WorkspaceOption[]>
  onSelect: (workspaceId: string | null | undefined) => void
}
const modeText = (mode?: WorkspaceMode) => mode ? ({ read_only: '只读', edit: '可读写', execute: '可读写并运行命令' }[mode] ?? '权限待核对') : '权限待核对'
const toolText = (name: unknown) => ({ workspace_list: '读取目录', workspace_read: '读取文件', workspace_search: '查找内容', workspace_write: '修改文件', workspace_run: '运行命令' }[String(name)] ?? '工具调用')
const errorText = (error: unknown) => error instanceof Error ? error.message : '暂时无法保存工作目录，请稍后重试。'
const buttonClass = 'inline-flex min-h-9 items-center justify-center gap-1.5 rounded-md px-2 text-xs font-semibold text-[#34786e] hover:bg-[#e7f0ec] disabled:opacity-45'

function CurrentWorkspaceRoot({ root }: { root: string }) {
  const [copyState, setCopyState] = useState<'idle' | 'copying' | 'copied' | 'failed'>('idle')
  const mounted = useRef(true)
  const copying = useRef(false)
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const copy = async () => {
    if (copying.current) return
    copying.current = true
    setCopyState('copying')
    try {
      await navigator.clipboard.writeText(root)
      if (mounted.current) setCopyState('copied')
    } catch {
      if (mounted.current) setCopyState('failed')
    } finally { copying.current = false }
  }
  return <div className="mt-1 min-w-0">
    <div className="flex min-w-0 items-start gap-2">
      <p className="min-w-0 flex-1 select-text whitespace-pre-wrap font-mono [overflow-wrap:anywhere]">{root}</p>
      <button type="button" className={`${buttonClass} size-9 shrink-0`} aria-label="复制当前目录路径" title="复制当前目录路径" disabled={copyState === 'copying'} aria-busy={copyState === 'copying'} onClick={() => void copy()}>
        {copyState === 'copying' ? <LoaderCircle size={15} className="motion-safe:animate-spin" aria-hidden="true" /> : copyState === 'copied' ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
      </button>
    </div>
    <p className="text-[#58776e]">目录位于服务所在电脑，非手机本地目录。</p>
    {copyState === 'copying' && <p role="status">正在复制路径…</p>}
    {copyState === 'copied' && <p role="status" className="text-[#34786e]">已复制当前目录路径</p>}
    {copyState === 'failed' && <p role="alert" className="text-[#a54f3e]">复制失败，请手动选择路径复制。</p>}
  </div>
}

// Task identity owns the dialog, selection and pending UI, including across navigation.
export function WorkspacePanel(props: Props) {
  return <WorkspaceBindingPanel key={props.draft ? '$new' : props.taskId} {...props} />
}

function WorkspaceBindingPanel(props: Props) {
  const { disabled, loadOptions } = props
  const taskId = props.draft ? '$new' : props.taskId
  const workspace = props.draft ? undefined : props.workspace
  const active = props.draft ? false : props.active
  const [open, setOpen] = useState(false)
  const [options, setOptions] = useState<WorkspaceOption[]>([])
  const [loading, setLoading] = useState(Boolean(props.draft))
  const [optionsError, setOptionsError] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [reload, setReload] = useState(0)
  const [choice, setChoice] = useState<string>()
  const [revision, setRevision] = useState<number | null>(null)
  const mounted = useRef(true)
  const saving = useRef(false)
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const shouldLoad = Boolean(props.draft || open)
  useEffect(() => {
    if (!shouldLoad) return
    let cancelled = false
    void loadOptions().then((rows) => {
      if (!Array.isArray(rows)) throw new Error('工作目录列表格式无效，请重新读取。')
      if (!cancelled) setOptions(rows)
    }).catch((cause) => {
      if (!cancelled) { setOptions([]); setOptionsError(errorText(cause)) }
    }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [shouldLoad, reload, loadOptions])

  // Auto is a display default; it must not rewrite an explicit choice or a retry's identity.
  const managed = options.find((option) => option.id === '__managed__' && option.managed)
  const currentId = (props.draft ? props.workspaceId === undefined ? managed?.id : props.workspaceId : workspace?.workspace_id) ?? ''
  const currentManaged = Boolean(workspace?.managed && currentId)
  const choiceId = choice ?? currentId
  const selected = options.find((option) => option.id === choiceId)
  const draftDirectory = options.find((option) => option.id === currentId)
  const draftTitle = draftDirectory?.title || (currentId === '__managed__' ? '新建独立工作目录' : currentId)
    || (props.draft && props.workspaceId === null ? '不使用目录' : loading ? '正在读取目录配置' : optionsError ? '目录配置未核对' : '未选择工作目录')
  const pending = workspace?.pending ?? []
  const changedElsewhere = workspace !== undefined && workspace.revision !== revision
  const blocked = Boolean(disabled || busy || refreshing || (!props.draft && (active || !workspace || changedElsewhere || pending.some((item) => item.task_id === taskId))))
  const reset = (binding: WorkspaceBinding | undefined) => {
    setChoice(props.draft ? undefined : binding?.workspace_id ?? '')
    setRevision(binding?.revision ?? null)
  }
  const refresh = async () => {
    if (saving.current || refreshing) return
    setError('')
    setNotice('')
    setLoading(true)
    setOptionsError('')
    setReload((value) => value + 1)
    if (props.draft) return
    setRefreshing(true)
    try {
      const next = await props.onRefresh()
      if (mounted.current) reset(next)
    } catch (cause) {
      if (mounted.current) setError(errorText(cause))
    } finally { if (mounted.current) setRefreshing(false) }
  }
  const save = async (workspaceId: string | null) => {
    if (blocked || saving.current || (workspaceId !== null && (loading || optionsError || !options.some((option) => option.id === workspaceId)))) return
    if (props.draft) {
      props.onSelect(workspaceId)
      setOpen(false)
      return
    }
    saving.current = true
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const next = await props.onBind({ workspace_id: workspaceId, expected_revision: revision })
      if (!mounted.current) return
      if (!next) throw new Error('未取得最新绑定状态，请重新读取后核对保存结果。')
      reset(next)
      setNotice('工作目录已更新。请重新核对当前计划；任务未自动执行。')
    } catch (cause) {
      if (mounted.current) setError(errorText(cause))
    } finally {
      saving.current = false
      if (mounted.current) setBusy(false)
    }
  }

  return <Dialog.Root open={open} onOpenChange={(next) => {
    if (next) {
      if (!props.draft) { setLoading(true); setOptionsError('') }
      if (!saving.current) { reset(workspace); setError(''); setNotice('') }
    }
    setOpen(next)
  }}>
    <Dialog.Trigger asChild><button type="button" className={`origin-trigger ${buttonClass} w-9 sm:w-auto`} disabled={disabled} aria-label="工作目录" title={props.draft ? `新对话草稿目录：${draftTitle}${draftDirectory?.managed ? '（首次发送时创建）' : ''}` : '工作目录：查看或更换'}>
      {currentId ? <FolderCog size={16} aria-hidden="true" /> : <Folder size={16} aria-hidden="true" />}<span className="hidden sm:inline">{props.draft && draftDirectory?.managed ? '新建独立目录' : props.draft && props.workspaceId === null ? '不使用目录' : '工作目录'}</span>
    </button></Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-[60] bg-[#22352d]/30" />
      <Dialog.Content className="fixed left-1/2 top-1/2 z-[61] flex max-h-[calc(100dvh-32px)] w-[calc(100vw-32px)] max-w-[460px] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-lg border-t-[3px] border-[#c9745a] bg-[#fcfdfb] text-[#2d493e] shadow-xl [overflow-wrap:anywhere]">
        <header className="flex shrink-0 items-start gap-3 border-b border-[#dce7e0] bg-[#f2f7fa] p-4">
          <div className="min-w-0 flex-1"><Dialog.Title className="text-base font-bold">工作目录{props.draft && <span className="ml-2 text-xs font-normal text-[#58776e]">新对话草稿</span>}</Dialog.Title>
            <Dialog.Description className="mt-1 text-xs leading-5 text-[#58776e]">{props.draft ? '选择仅保存在当前草稿，首次发送时随新任务提交。' : '可选择宿主配置的目录或独立目录模板。保存绑定不会自动执行任务。'}</Dialog.Description></div>
          <Dialog.Close asChild><button type="button" className={`${buttonClass} size-9 shrink-0`} aria-label="关闭工作目录" title="回到聊天"><X size={18} /></button></Dialog.Close>
        </header>
        <div className="min-h-0 space-y-4 overflow-y-auto p-4 text-xs leading-5" aria-busy={busy || refreshing}>
          <section aria-label="当前工作目录">
            <h3 className="font-semibold">{props.draft ? '草稿目录' : '当前目录'}</h3>
            {props.draft ? <><p className="mt-1">{draftTitle}</p>
              {draftDirectory?.managed && <p className="mt-1">首次发送时由宿主创建本群独立目录；实际目录以创建回执为准。</p>}
              {props.workspaceId === null && <p className="mt-1">新任务不绑定工作目录，不提供文件和运行工具。</p>}
              {props.workspaceId === undefined && !managed && !loading && !optionsError && <p className="mt-1">宿主未配置默认独立目录；未选择目录时不提供文件和运行工具。</p>}
              {currentId && !loading && !optionsError && !draftDirectory && <p className="mt-1 text-[#895746]">原选择不在可选列表中；已有待确认请求仍使用原目录，需核对服务端配置。</p>}</>
              : <p className="mt-1">{workspace?.workspace_id ? workspace.title || workspace.workspace_id : '尚未绑定工作目录'}</p>}
            {workspace?.workspace_id && workspace.root && <CurrentWorkspaceRoot key={JSON.stringify([workspace.workspace_id, workspace.root])} root={workspace.root} />}
            {workspace?.workspace_id && <p className="mt-1">权限：{modeText(workspace.mode)} · {workspace.available ? '可用' : '不可用，需重新绑定'}</p>}
            {currentManaged && choiceId === currentId && !selected && workspace?.mode === 'execute' && <p className="mt-1 text-[#895746]">命令在服务所在电脑上运行，当前不是沙箱</p>}
            {workspace?.reason && <p className="mt-1 text-[#895746]">{workspace.reason}</p>}
            {!props.draft && !workspace && <p className="mt-1 text-[#895746]">暂未取得工作目录状态，请重新读取后再绑定。</p>}
          </section>
          <section className="border-t border-[#dce7e0] pt-3" aria-label="选择工作目录">
            <label htmlFor={`workspace-${taskId}`} className="font-semibold">可选目录</label>
            <select id={`workspace-${taskId}`} value={choiceId} disabled={blocked || loading || Boolean(optionsError) || !options.length} onChange={(event) => { setChoice(event.target.value); setNotice('') }} className="mt-2 block min-h-10 w-full min-w-0 max-w-full rounded-md border border-[#cddfe8] bg-white px-2 text-sm disabled:opacity-60">
              <option value="">选择预配置目录</option>
              {currentManaged && !options.some((option) => option.id === currentId) && <option value={currentId}>{workspace?.title || '独立工作目录'} · 当前绑定{!workspace?.available && '（不可用）'}</option>}
              {choiceId && !selected && !(currentManaged && choiceId === currentId) && <option value={choiceId}>{props.draft ? '原选择不在可选列表中' : '当前绑定不在可选列表中'}</option>}
              {options.map((option) => <option key={option.id} value={option.id}>{option.title} · {modeText(option.mode)}</option>)}
            </select>
            {loading ? <p role="status" className="mt-2">正在读取可选目录…</p>
              : optionsError ? <p role="alert" className="mt-2 text-[#a54f3e]">可选目录读取失败：{optionsError}</p>
                : !options.length && <p className="mt-2">尚未配置可选工作目录，请在服务端配置后重新读取。</p>}
            {selected && <div className="mt-2 space-y-1">
              {selected.id === '__managed__' && selected.managed && <>
                {!props.draft && <p>保存绑定时创建或绑定本群独立目录；不会自动执行任务。</p>}
                <p>创建位置（基目录），并非本群已创建的目录：</p>
              </>}
              <p className="font-mono">{selected.root}</p><p>权限：{modeText(selected.mode)}</p>
              {selected.mode === 'execute' && <><p className="text-[#895746]">命令在服务所在电脑上运行，当前不是沙箱</p><p>可用命令：{selected.commands.length ? selected.commands.join('、') : '未配置'}</p></>}
            </div>}
          </section>
          {!props.draft && <p className="border-t border-[#dce7e0] pt-3 text-[#58776e]">切换或解除绑定后，需重新核对当前计划与已有成果。</p>}
          {pending.length > 0 && <div role="alert" className="text-[#895746]">
            <p className="font-semibold">有 {pending.length} 项工具结果未明，请先核对实际影响，勿重复执行。</p>
            <ul>{pending.map((item) => <li key={`${item.task_id}:${item.seq}`}>{toolText(item.name)} · #{item.seq}{item.task_id !== taskId ? ` · 任务 ${item.task_id}` : ''}</li>)}</ul>
          </div>}
          {active && <p role="status">任务仍有活动，请等待结束后再更换或解除绑定。</p>}
          {changedElsewhere && <p role="status">当前绑定已变化，请重新读取后再选择。</p>}
          {error && <p role="alert" className="text-[#a54f3e]">{error}</p>}
          {notice && <p role="status">{notice}</p>}
        </div>
        <footer className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-[#dce7e0] px-3 py-2">
          <button type="button" className={buttonClass} onClick={() => void refresh()} disabled={busy || refreshing} title="重新读取目录与绑定"><RefreshCw size={15} className={refreshing ? 'motion-safe:animate-spin' : ''} />重新读取</button>
          <div className="flex flex-wrap gap-1">
            <button type="button" className={buttonClass} disabled={blocked || (props.draft ? props.workspaceId === null : !currentId)} onClick={() => void save(null)}><Unlink size={15} />{props.draft ? '不使用目录' : '解除绑定'}</button>
            <button type="button" className={`${buttonClass} bg-[#e7f0ec]`} disabled={blocked || loading || Boolean(optionsError) || !selected || (choiceId === currentId && (props.draft ? props.workspaceId !== undefined : workspace?.available))} onClick={() => void save(choiceId)} aria-busy={busy}>
              {busy ? <LoaderCircle size={15} className="motion-safe:animate-spin" /> : <Save size={15} />}{busy ? '保存中…' : props.draft ? '用于新对话' : '保存绑定'}
            </button>
          </div>
        </footer>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}

type ToolEvent = { seq: number; type: string; actor: { id: string; kind?: string }; payload: Record<string, unknown> }
const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const excerpt = (value: unknown) => typeof value === 'string' ? value.slice(0, 1600) + (value.length > 1600 ? '\n（仅显示部分内容）' : '') : ''
const hostReceipt = (event: ToolEvent) => event.actor.id === 'workspace_gateway' && event.actor.kind === 'system'

export function WorkspaceToolProgress({ events, claim, name }: { events: ToolEvent[]; claim: number; name: string }) {
  const claimed = events.find((event) => event.seq === claim && event.type === 'work.claimed')
  if (!claimed) return null
  const plan = events.find((event) => event.type === 'work.plan_created' && event.seq === claimed.payload.plan_seq && event.seq < claim)
  const items = plan?.payload.items
  const owner = Array.isArray(items) ? record(items.find((item) => record(item).id === claimed.payload.work_item_id)).owner : undefined
  if (typeof owner !== 'string') return null
  const matchesAttempt = (event: ToolEvent) => ['work_item_id', 'attempt_id', 'plan_seq'].every((key) => claimed.payload[key] !== undefined && event.payload[key] === claimed.payload[key])
    && sameWorkScope(claimed.payload.scope, event.payload.scope)
  const starts = events.filter((event) => event.type === 'workspace.tool_started' && hostReceipt(event) && event.seq > claim
    && event.payload.role === owner && matchesAttempt(event))
  if (!starts.length) return null
  return <details className="mt-3 border-t border-[#dfeae5] pt-2 text-xs text-[#52766d]">
    <summary className="cursor-pointer font-semibold">目录操作 · {starts.length} 项</summary>
    <ol aria-label={`${name}的目录操作`} className="mt-2 max-h-64 space-y-3 overflow-y-auto pr-1 [overflow-wrap:anywhere]">
      {starts.map((start) => {
        const p = start.payload
        const finished = events.find((event) => event.type === 'workspace.tool_finished' && hostReceipt(event) && event.seq > start.seq
          && event.payload.started_seq === start.seq && event.payload.call_id === p.call_id && event.payload.name === p.name
          && event.payload.role === p.role && event.payload.workspace_revision === p.workspace_revision && matchesAttempt(event))
        const reconciled = events.find((event) => event.type === 'workspace.tool_reconciled' && event.actor.id === 'human' && event.actor.kind === 'human'
          && event.seq > (finished?.seq ?? start.seq) && event.payload.started_seq === start.seq && event.payload.call_id === p.call_id)
        const result = record(finished?.payload.result)
        const stdout = record(result.stdout), stderr = record(result.stderr)
        const uncertain = Boolean(finished && (finished.payload.result === null || typeof finished.payload.result !== 'object' || Array.isArray(finished.payload.result)
          || result.complete === false || stdout.complete === false || stderr.complete === false || result.outcome === 'timeout' || record(result.error).code === 'process_failed'))
        const args = record(p.arguments)
        const Icon = p.name === 'workspace_run' ? Terminal : p.name === 'workspace_write' ? FilePenLine : p.name === 'workspace_search' ? FileSearch : Folder
        const outcome = reconciled ? '已人工核对实际影响，未重放'
          : uncertain ? '结果待核对'
            : !finished ? '已发起，结果待确认'
          : p.name === 'workspace_run' ? result.outcome === 'launch_error' ? '启动失败'
            : result.outcome === 'passed' && result.status === 'ok' && typeof result.exit_code === 'number' ? `运行成功 · 退出码 ${result.exit_code}`
              : result.outcome === 'failed' ? `运行失败${typeof result.exit_code === 'number' ? ` · 退出码 ${result.exit_code}` : ''}`
                : result.status === 'error' ? '未完成' : '运行结果待核对'
            : result.status !== 'ok' ? '未完成' : '已完成'
        const output = p.name === 'workspace_read' ? excerpt(result.content) : ''
        return <li key={start.seq} className="leading-5">
          <p className="flex items-start gap-1.5"><Icon size={14} className="mt-0.5 shrink-0" aria-hidden="true" /><strong>{toolText(p.name)}</strong><span>· {outcome}</span></p>
          {uncertain && !reconciled && <p>{result.outcome === 'timeout' ? '运行超时' : result.complete === false || stdout.complete === false || stderr.complete === false ? '输出不完整' : '进程影响未确认'}，请先人工核对实际影响，勿重复执行。</p>}
          {reconciled && <p>{excerpt(reconciled.payload.reason)}</p>}
          {uncertain && typeof result.exit_code === 'number' && <p>已记录退出码 {result.exit_code}；实际影响仍以人工核对为准。</p>}
          <p className="mt-1 whitespace-pre-wrap font-mono">{excerpt(args.path ?? args.command)}</p>
          {typeof args.query === 'string' && <p>查找：{excerpt(args.query)}</p>}
          {Array.isArray(result.entries) && <p>已读取 {result.entries.length} 项目录内容</p>}
          {Array.isArray(result.matches) && <p>找到 {result.matches.length} 处匹配</p>}
          {Array.isArray(result.entries) && <ul>{result.entries.slice(0, 6).map((entry, index) => <li key={index} className="font-mono">{excerpt(record(entry).path)}</li>)}</ul>}
          {Array.isArray(result.matches) && <ul>{result.matches.slice(0, 6).map((entry, index) => {
            const match = record(entry)
            return <li key={index}><span className="font-mono">{excerpt(match.path)}{typeof match.line === 'number' ? `:${match.line}` : ''}</span><p className="whitespace-pre-wrap">{excerpt(match.output)}</p></li>
          })}</ul>}
          {[result.entries, result.matches].some((items) => Array.isArray(items) && items.length > 6) && <p>仅展示前 6 项。</p>}
          {p.name === 'workspace_write' && result.status === 'ok' && !uncertain && <p>{result.created ? '已创建文件' : '已写入文件'}{typeof result.bytes === 'number' ? ` · ${result.bytes} 字节` : ''}</p>}
          {result.status === 'error' && <p>{excerpt(record(result.error).message) || '工具未能完成，请核对后重试。'}</p>}
          {output && <pre className="mt-1 whitespace-pre-wrap font-mono text-[11px]">{output}</pre>}
          {p.name === 'workspace_run' && <CommandOutputs stdout={result.stdout} stderr={result.stderr} limit={1600} />}
          {(result.truncated === true || stdout.truncated === true || stderr.truncated === true) && <p>结果仅返回部分内容。</p>}
          {p.name === 'workspace_run' && finished && <p className="mt-1">命令回执不代表任务验收通过。</p>}
        </li>
      })}
    </ol>
  </details>
}
