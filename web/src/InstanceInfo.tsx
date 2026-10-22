import { useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Info, RefreshCw, X } from 'lucide-react'

type DatabaseKind = 'file' | 'memory' | 'custom' | 'unknown'
type Instance = {
  instance_id: string
  package_version: string
  ui_version: string | null
  startup_recovery: boolean
  listener: { host: string; port: number }
  database: { kind: DatabaseKind; locator_id: string | null }
}
type State = { status: 'idle' | 'loading' | 'unknown' } | { status: 'ready'; value: Instance; readAt: string }
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown, max: number): value is string => typeof value === 'string' && value.trim().length > 0
  && value.length <= max && !Array.from(value).some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
const hash = (value: unknown): value is string | null => value === null || (typeof value === 'string' && /^[a-f0-9]{64}$/u.test(value))

function parseInstance(value: unknown): Instance | null {
  if (!object(value) || value.schema_version !== 1 || !text(value.instance_id, 128)
    || !text(value.package_version, 128) || value.backend_build_id !== null || !hash(value.ui_version)
    || typeof value.startup_recovery !== 'boolean' || !object(value.listener) || !text(value.listener.host, 255)
    || typeof value.listener.port !== 'number' || !Number.isInteger(value.listener.port) || value.listener.port < 1 || value.listener.port > 65535
    || !object(value.database) || !['file', 'memory', 'custom', 'unknown'].includes(value.database.kind as string)
    || !hash(value.database.locator_id) || (value.database.kind !== 'file' && value.database.locator_id !== null)) return null
  return { instance_id: value.instance_id, package_version: value.package_version, ui_version: value.ui_version,
    startup_recovery: value.startup_recovery, listener: { host: value.listener.host, port: value.listener.port },
    database: { kind: value.database.kind as DatabaseKind, locator_id: value.database.locator_id } }
}

const databaseLabels: Record<DatabaseKind, string> = { file: '文件配置', memory: '内存配置', custom: '自定义来源', unknown: '未知' }

export function InstanceInfo({ loadInstance }: { loadInstance: () => Promise<unknown> }) {
  const requested = useRef(false)
  const inFlight = useRef(false)
  const [state, setState] = useState<State>({ status: 'idle' })
  const load = () => {
    if (inFlight.current) return
    inFlight.current = true
    requested.current = true
    setState({ status: 'loading' })
    void Promise.resolve().then(loadInstance).then(value => {
      const instance = parseInstance(value)
      setState(instance ? { status: 'ready', value: instance, readAt: new Date().toLocaleString('zh-CN', { hour12: false }) } : { status: 'unknown' })
    }).catch(() => setState({ status: 'unknown' })).finally(() => { inFlight.current = false })
  }
  const instance = state.status === 'ready' ? state.value : null
  return <Dialog.Root onOpenChange={open => { if (open && !requested.current) load() }}>
    <Dialog.Trigger asChild><button type="button" className="grid size-8 shrink-0 place-items-center rounded-md text-[#34786e] hover:bg-[#e7f0ec]" aria-label="实例信息" title="实例信息"><Info size={17} /></button></Dialog.Trigger>
    <Dialog.Portal>
      <Dialog.Overlay className="artifact-overlay" />
      <Dialog.Content className="fixed left-1/2 top-1/2 z-[61] max-h-[85dvh] w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-md bg-white p-4 text-[#253d38] shadow-xl sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <Dialog.Title className="text-base font-bold">当前连接服务</Dialog.Title>
          <div className="flex shrink-0 gap-1">
            <button type="button" onClick={load} disabled={state.status === 'loading'} className="grid size-8 place-items-center rounded-md hover:bg-[#e7f0ec] disabled:opacity-50" aria-label="刷新实例信息" title="刷新实例信息"><RefreshCw size={17} className={state.status === 'loading' ? 'motion-safe:animate-spin' : ''} /></button>
            <Dialog.Close asChild><button type="button" className="grid size-8 shrink-0 place-items-center rounded-md hover:bg-[#e7f0ec]" aria-label="关闭实例信息" title="关闭实例信息"><X size={18} /></button></Dialog.Close>
          </div>
        </div>
        <Dialog.Description className="mt-2 text-xs leading-5 text-[#52766d]">以下为本次读取的服务报告；当前加载的页面脚本版本未核验，页面与服务是否一致未确认。</Dialog.Description>
        {state.status === 'ready' && <p className="mt-2 text-[11px] text-[#52766d]">本页读取时间：{state.readAt}</p>}
        {state.status === 'loading' && <p role="status" className="mt-4 text-xs">正在读取实例信息…</p>}
        {state.status === 'unknown' && <div className="mt-4 text-xs leading-5">
          <p role="status">实例信息未确认，读取失败或服务暂不支持。</p>
          <button type="button" onClick={load} className="mt-2 inline-flex items-center gap-1 text-[#24766b]" aria-label="重试读取实例信息" title="重试读取实例信息"><RefreshCw size={15} />重试</button>
        </div>}
        {instance && <>
          <dl className="mt-4 grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)] gap-x-3 gap-y-3 text-xs leading-5 [&>dd]:min-w-0 [&>dd]:[overflow-wrap:anywhere] [&>dt]:text-[#52766d]">
            <dt>服务实例标识</dt><dd>{instance.instance_id}</dd>
            <dt>服务包版本</dt><dd>{instance.package_version}</dd>
            <dt>后端构建指纹</dt><dd>未确认（服务未登记）</dd>
            <dt>服务提供的 UI 指纹</dt><dd>{instance.ui_version ?? '未确认'}</dd>
            <dt>实际监听地址</dt><dd>{instance.listener.host}</dd>
            <dt>实际监听端口</dt><dd>{instance.listener.port}</dd>
            <dt>启动续接设置</dt><dd>{instance.startup_recovery ? '开启' : '关闭'}</dd>
            <dt>数据库来源类型</dt><dd>{databaseLabels[instance.database.kind]}</dd>
            <dt>配置路径摘要</dt><dd>{instance.database.locator_id ?? '未提供'}</dd>
          </dl>
          <p className="mt-4 text-xs leading-5 text-[#52766d]">配置路径摘要不证明数据库相同或内容一致；启动续接设置不证明有无待处理工作。这份报告不核验模型可用性、任务完成或数据库兼容性。</p>
        </>}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
