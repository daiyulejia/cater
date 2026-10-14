import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { BookOpen, BookPlus, Check, FolderPlus, Pencil, Plus, RefreshCw, Save, X } from 'lucide-react'

type Project = { id: string; name: string }
type Rule = { id: string; revision: number; text: string; scope: string; status: string; rationale?: string; source: { kind: string; actor: string; event_seq?: number }; seq: number }
type RuleState = { project_id: string | null; rules: Rule[]; enforcement: string }
type Request = <T>(path: string, init?: RequestInit) => Promise<T>
const scopeName: Record<string, string> = { workspace: '猫咖通用', project: '当前项目', task: '本次任务' }
const stateName: Record<string, string> = { active: '已采用', candidate: '待采用', retired: '已停用' }

function messageFor(error: unknown): string {
  const value = error instanceof Error ? error.message : ''
  if (value.includes('version changed') || value.includes('concurrently')) return '店规已有新版本，请刷新核对后再保存。你的编辑仍保留。'
  if (value.includes('equivalent rule')) return '这个范围已经有相同的店规，无需重复记录。'
  if (value.includes('capacity')) return '这个范围的店规已较多，请先合并或停用旧规则。'
  if (value.includes('already belongs')) return '这项任务已有所属项目，请刷新查看。'
  if (value.includes('while collaboration is active')) return '当前协作结束后再关联项目，避免混用上下文。'
  return '这次没有保存成功，请稍后重试。'
}

export function RulesPanel({ request, taskId, projectId, draft, busy, onProjectChange, onRecorded, onUpdated }: {
  request: Request; taskId?: string; projectId?: string | null; draft: string; busy: boolean
  onProjectChange: (project: Project | null) => Promise<void>; onRecorded: (text: string) => void; onUpdated: () => void
}) {
  const [open, setOpen] = useState(false)
  const [projects, setProjects] = useState<Project[]>([])
  const [loaded, setLoaded] = useState<{ key: string; state: RuleState } | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [newName, setNewName] = useState('')
  const [creatingProject, setCreatingProject] = useState(false)
  const [text, setText] = useState('')
  const [scope, setScope] = useState(taskId ? 'task' : projectId ? 'project' : 'workspace')
  const [editing, setEditing] = useState<Rule | null>(null)
  const [showRetired, setShowRetired] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const writing = useRef(false)
  const contextKey = `${taskId ?? ''}:${projectId ?? ''}`
  const contextRef = useRef(contextKey)
  useLayoutEffect(() => { contextRef.current = contextKey }, [contextKey])
  const state = loaded?.key === contextKey ? loaded.state : null
  const effectiveScope = scope === 'project' && !projectId || scope === 'task' && !taskId ? '' : scope
  const currentProject = projects.find((item) => item.id === projectId)
  const latestEdit = editing ? state?.rules.find((rule) => rule.id === editing.id && rule.revision !== editing.revision) : undefined
  const tooLong = text.trim().length > 2000

  const refresh = useCallback(async () => {
    controller.current?.abort()
    const pending = new AbortController()
    controller.current = pending
    const timer = window.setTimeout(() => pending.abort(), 8000)
    setLoading(true)
    try {
      const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
      const [nextProjects, nextState] = await Promise.all([
        request<Project[]>('/api/projects', { signal: pending.signal }),
        request<RuleState>(`/api/rules${query}`, { signal: pending.signal }),
      ])
      if (mounted.current && controller.current === pending && !pending.signal.aborted) {
        setProjects(nextProjects); setLoaded({ key: contextKey, state: nextState }); setError('')
      }
    } catch {
      if (mounted.current && controller.current === pending) setError('暂时无法读取店规，请重试。')
    } finally {
      window.clearTimeout(timer)
      if (mounted.current && controller.current === pending) setLoading(false)
    }
  }, [request, taskId, projectId, contextKey])
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; controller.current?.abort() }
  }, [])
  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => void refresh(), 0)
    return () => { window.clearTimeout(timer); controller.current?.abort() }
  }, [open, refresh])

  const mutate = async (action: () => Promise<void>) => {
    if (writing.current) return
    writing.current = true
    const origin = contextKey
    setSaving(true); setError(''); setNotice('')
    try { await action(); if (mounted.current) { onUpdated(); if (contextRef.current === origin) await refresh() } }
    catch (err) {
      if (mounted.current) {
        if (err instanceof Error && /version changed|concurrently/.test(err.message)) await refresh()
        if (mounted.current) setError(messageFor(err))
      }
    }
    finally { writing.current = false; if (mounted.current) setSaving(false) }
  }
  const record = () => void mutate(async () => {
    const submitted = text.trim()
    if (editing) {
      await request(`/api/rules/${editing.id}`, { method: 'POST', body: JSON.stringify({ revision: editing.revision, text: submitted, status: editing.status, task_id: taskId }) })
    } else {
      await request('/api/rules', { method: 'POST', body: JSON.stringify({ text: submitted, scope: effectiveScope, task_id: taskId, project_id: projectId }) })
      onRecorded(submitted)
    }
    if (mounted.current) { setText(''); setEditing(null); setNotice('已保存，后续调用将使用新的店规版本。') }
  })
  const changeStatus = (rule: Rule, status: string) => void mutate(async () => {
    await request(`/api/rules/${rule.id}`, { method: 'POST', body: JSON.stringify({ revision: rule.revision, text: rule.text, status, task_id: taskId }) })
    if (mounted.current) setNotice(status === 'retired' ? '已停用，历史记录仍然保留。' : '已采用，后续调用会带上这条要求。')
  })
  const pickProject = (project: Project | null) => void mutate(async () => { await onProjectChange(project) })
  const createProject = () => void mutate(async () => {
    const project = await request<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name: newName.trim() }) })
    if (mounted.current) { setProjects((items) => [...items, project]); setNewName(''); setCreatingProject(false) }
    await onProjectChange(project)
  })

  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <div className="flex min-w-0 shrink-0 items-center justify-between gap-2 border-t border-[#e0e6dc] bg-[#f5f9f4] px-4 py-1.5 sm:px-7">
      <Dialog.Trigger asChild><button type="button" className="inline-flex min-w-0 items-center gap-1.5 text-xs font-semibold text-[#427567]" title="项目与店规"><BookOpen size={15} className="shrink-0" /><span>店规</span><span className="max-w-40 truncate font-normal text-[#788b7e]">{currentProject?.name ?? (projectId ? '项目已关联' : '未关联项目')}</span></button></Dialog.Trigger>
      <button type="button" disabled={!draft.trim() || saving} onClick={() => { setEditing(null); setText(draft.trim()); setOpen(true) }} className="inline-flex shrink-0 items-center gap-1 text-xs text-[#a45c48] disabled:opacity-40" title="把当前输入记为店规"><BookPlus size={15} /> 记为店规</button>
    </div>
    <Dialog.Portal><Dialog.Overlay className="artifact-overlay" /><Dialog.Content className="rules-dialog flex flex-col">
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[#d3e3d9] px-5 py-4 sm:px-7">
        <div><p className="text-[11px] font-semibold text-[#ac6650]">Cater · 店长记事簿</p><Dialog.Title className="mt-1 text-xl font-bold text-[#305749]">项目与店规</Dialog.Title><Dialog.Description className="mt-2 text-xs leading-5 text-[#718574]">工作要求会进入后续上下文；目前不提供文件保护或自动验收保证。</Dialog.Description></div>
        <div className="flex shrink-0 gap-1"><button onClick={() => void refresh()} disabled={loading || saving} className="artifact-tool" aria-label="刷新店规" title="刷新店规"><RefreshCw size={17} className={loading ? 'motion-safe:animate-spin' : ''} /></button><Dialog.Close asChild><button className="artifact-tool" aria-label="关闭店规" title="回到聊天"><X size={19} /></button></Dialog.Close></div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5 sm:px-7">
        <section className="border-b border-[#dde6dc] pb-4">
          <div className="flex flex-wrap items-center gap-2"><label htmlFor="rule-project" className="text-xs font-semibold">所属项目</label>
            <select id="rule-project" value={projectId ?? ''} disabled={saving || busy || Boolean(editing) || Boolean(taskId && projectId)} onChange={(event) => { const project = projects.find((item) => item.id === event.target.value); if (project || !taskId) pickProject(project ?? null) }} className="min-w-0 flex-1 rounded-md border border-[#cfddd4] bg-white px-2 py-2 text-sm">
              <option value="" disabled={Boolean(taskId)}>未关联项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
            </select>
            {!(taskId && projectId) && <button onClick={() => setCreatingProject(!creatingProject)} disabled={saving || busy || Boolean(editing)} className="artifact-tool" title="新建项目" aria-label="新建项目"><FolderPlus size={18} /></button>}
          </div>
          {taskId && projectId && <p className="mt-2 text-[11px] text-[#798a7f]">此任务已关联项目，历史归属保持不变。</p>}
          {creatingProject && <div className="mt-3 flex gap-2"><input aria-label="项目名称" value={newName} disabled={saving || busy || Boolean(editing)} onChange={(event) => setNewName(event.target.value)} maxLength={120} placeholder="项目名称" className="min-w-0 flex-1 rounded-md border border-[#cfddd4] bg-white p-2 text-sm" /><button onClick={createProject} disabled={saving || busy || Boolean(editing) || !newName.trim()} className="artifact-tool" title="创建并关联项目" aria-label="创建并关联项目"><Check size={18} /></button></div>}
        </section>
        {error && <p role="alert" className="mt-4 text-xs text-rose-700">{error}</p>}
        {notice && <p role="status" className="mt-4 text-xs text-[#327665]">{notice}</p>}
        <section className="border-b border-[#dde6dc] py-4">
          <div className="mb-2 flex items-center justify-between gap-3"><h2 className="text-sm font-bold">{editing ? `修改店规 · v${editing.revision}` : '记下一条要求'}</h2>
            <select aria-label="店规适用范围" value={editing?.scope ?? effectiveScope} disabled={Boolean(editing) || saving} onChange={(event) => setScope(event.target.value)} className="rounded-md border border-[#cfddd4] bg-white px-2 py-1 text-xs"><option value="" disabled>请选择范围</option><option value="workspace">猫咖通用</option><option value="project" disabled={!projectId}>当前项目</option><option value="task" disabled={!taskId}>本次任务</option></select>
          </div>
          <textarea aria-label="店规内容" value={text} disabled={saving} aria-invalid={tooLong} onChange={(event) => setText(event.target.value)} maxLength={2000} rows={3} placeholder="写下这次需要始终遵守的要求" className="w-full resize-y rounded-md border border-[#cfddd4] bg-white p-3 text-sm leading-6" />
          {tooLong && <p role="alert" className="mt-1 text-xs text-rose-700">店规最多 2000 字，当前 {text.trim().length} 字。原文已保留，请精简后保存。</p>}
          {latestEdit && <div className="mt-3 border-l-2 border-[#c9745a] pl-3 text-xs leading-5"><p className="font-semibold">已有新版本 v{latestEdit.revision} · {stateName[latestEdit.status]}</p><p className="mt-1 whitespace-pre-wrap break-words">{latestEdit.text}</p><div className="mt-2 flex flex-wrap gap-3"><button disabled={saving} onClick={() => { setEditing(latestEdit); setText(latestEdit.text); setError('') }} className="font-semibold text-[#367562]">使用新版内容</button><button disabled={saving} onClick={() => { setEditing(latestEdit); setError('') }} className="font-semibold text-[#a45c48]">基于新版保留我的修改</button></div></div>}
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2"><span className="text-[11px] text-[#718574]">{(editing?.scope ?? effectiveScope) === 'workspace' ? '适用于所有项目与任务' : `${scopeName[editing?.scope ?? effectiveScope] ?? '请选择适用范围'} · 上下文要求`}</span><div className="flex gap-2">{editing && <button onClick={() => { setEditing(null); setText('') }} disabled={saving} className="artifact-tool" title="取消编辑" aria-label="取消编辑"><X size={17} /></button>}<button onClick={record} disabled={saving || loading || !state || !text.trim() || tooLong || !effectiveScope || Boolean(latestEdit)} className="inline-flex items-center gap-1.5 rounded-md bg-[#367562] px-3 py-2 text-xs font-semibold text-white disabled:opacity-40"><Save size={15} />{saving ? '保存中' : '保存店规'}</button></div></div>
        </section>
        <section className="pt-4">
          <div className="mb-3 flex items-center justify-between gap-2"><h2 className="text-sm font-bold">适用店规</h2><label className="flex items-center gap-1.5 text-xs text-[#718574]"><input type="checkbox" checked={showRetired} onChange={(event) => setShowRetired(event.target.checked)} />包含已停用</label></div>
          {loading && !state ? <p className="py-4 text-sm text-[#718574]">正在读取…</p> : state && !state.rules.filter((rule) => showRetired || rule.status !== 'retired').length ? <p className="py-4 text-sm text-[#718574]">这个范围还没有店规。</p> : null}
          {state?.rules.filter((rule) => showRetired || rule.status !== 'retired').map((rule) => <article key={rule.id} className="border-b border-[#e0e6dc] py-3 last:border-0">
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-[#738478]"><span className={rule.status === 'candidate' ? 'font-semibold text-[#b26a3e]' : 'font-semibold text-[#3d7460]'}>{stateName[rule.status]}</span><span>{scopeName[rule.scope]}</span><span>v{rule.revision}</span><span>{rule.source.kind === 'human' ? '由你制定' : `成员建议 · #${rule.source.event_seq}`}</span></div>
            <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6">{rule.text}</p>
            {rule.rationale && <details className="mt-2 text-xs leading-5 text-[#718574]"><summary className="cursor-pointer">提出依据（成员报告）</summary><p className="mt-1 whitespace-pre-wrap break-words">{rule.rationale}</p></details>}
            <div className="mt-2 flex justify-end gap-2"><button onClick={() => { setEditing(rule); setText(rule.text) }} disabled={saving} className="artifact-tool" title="修改店规" aria-label={`修改店规 ${rule.text}`}><Pencil size={15} /></button>{rule.status !== 'active' && <button onClick={() => changeStatus(rule, 'active')} disabled={saving} className="inline-flex items-center gap-1 text-xs font-semibold text-[#367562]"><Plus size={14} />采用</button>}{rule.status !== 'retired' && <button onClick={() => changeStatus(rule, 'retired')} disabled={saving} className="text-xs text-[#927665]">停用</button>}</div>
          </article>)}
        </section>
      </div>
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>
}
