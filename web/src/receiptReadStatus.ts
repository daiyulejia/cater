import { isTeamHost } from './team'
import type { TeamEvent } from './team'

type Data = Record<string, unknown>
export type ReceiptReadSource = { seq: number; kind: string; relation?: string }
export type ReceiptRead = {
  valid: boolean
  status: string
  section?: string
  sources?: ReceiptReadSource[]
  hasMore?: boolean
  pendingHasMore?: boolean
  errorNote?: string
}
const data = (value: unknown): value is Data => value !== null && typeof value === 'object' && !Array.isArray(value)
const object = (value: unknown): Data => data(value) ? value : {}
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const positive = (value: unknown): value is number => count(value) && value > 0
const id = (value: unknown): value is string => typeof value === 'string' && Boolean(value.trim()) && Array.from(value).length <= 128
const path = (value: unknown): value is string => typeof value === 'string' && Boolean(value)
  && value.length <= 8192 && !/^[\\/]|^[a-z]:/i.test(value)
  && value.split(/[\\/]/).every(part => Boolean(part) && part !== '.' && part !== '..'
    && !Array.from(part).some(char => char.charCodeAt(0) < 32))
// Filters retain their literal spelling; the backend owns path policy and matching.
const fileFilter = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && Array.from(value).length <= 2048

export function hasReceiptReadMetadata(event: TeamEvent): boolean {
  const result = object(event.payload.result)
  return (event.payload.name ?? event.payload.tool) === 'read_receipts'
    && ['record_read', 'query_state', 'scope_available'].some(key => Object.hasOwn(result, key))
}

// Validate the query envelope, not the conclusions or bodies of the original operations.
export function receiptReadProvenance(event: TeamEvent): ReceiptRead | undefined {
  if (!hasReceiptReadMetadata(event)) return
  const uncertain: ReceiptRead = { valid: false, status: '已存回执读取待核对' }
  const p = event.payload, result = object(p.result), read = object(result.record_read), args = object(p.arguments)
  if (!isTeamHost(event) || event.type !== 'team.tool_finished' || !positive(event.seq)
    || ['name', 'tool'].some(key => Object.hasOwn(p, key) && p[key] !== 'read_receipts')
    || !data(p.arguments) || !id(p.run_id) || !id(p.role) || !positive(p.message_seq)
    || !positive(p.started_seq) || p.message_seq >= p.started_seq || p.started_seq >= event.seq
    || result.complete !== undefined && result.complete !== true || result.truncated !== undefined && result.truncated !== false
    || result.outcome !== undefined || read.kind !== 'persisted_events' || read.reexecutes_operations !== false) return uncertain
  const readerKeys = ['reader_run_id', 'reader_message_seq', 'reader_started_seq']
  const readerPresent = readerKeys.some(key => Object.hasOwn(read, key))
  if (readerPresent && (!id(read.reader_run_id) || read.reader_run_id !== p.run_id
    || read.reader_message_seq !== p.message_seq || read.reader_started_seq !== p.started_seq)) return uncertain
  const limit = Object.hasOwn(args, 'limit') ? args.limit : 20, section = Object.hasOwn(args, 'section') ? args.section : 'receipts'
  if (!positive(limit) || limit > 50 || typeof section !== 'string' || !['receipts', 'file_reads'].includes(section)
    || Object.keys(args).some(key => !['limit', 'section', 'include_pending', 'run_id', 'role', 'name', 'message_seq', 'before_seq', 'receipt_seq', 'file_path'].includes(key))
    || ['run_id', 'role', 'name'].some(key => Object.hasOwn(args, key) && !id(args[key]))
    || ['message_seq', 'before_seq', 'receipt_seq'].some(key => Object.hasOwn(args, key) && !positive(args[key]))
    || Object.hasOwn(args, 'include_pending') && typeof args.include_pending !== 'boolean') return uncertain
  if (section === 'file_reads' && (['receipt_seq', 'include_pending'].some(key => Object.hasOwn(args, key))
    || Object.hasOwn(args, 'file_path') && !fileFilter(args.file_path))) return uncertain
  if (result.status === 'error') {
    if (section !== 'file_reads' || ![undefined, 'error', 'failed'].includes(p.status as string | undefined)
      || ['receipts', 'observations', 'query_state', 'scope_available', 'workspace_binding', 'has_more', 'next_before_seq',
        'pending_operations', 'pending_has_more', 'pending_next_before_seq'].some(key => Object.hasOwn(result, key))) return uncertain
    if (result.code === 'file_read_scope_changed') return { valid: false, status: '文件读取来源范围已变化，需重新查询',
      errorNote: '目录绑定已变化，请重新查询来源。' }
    if (result.code === 'file_read_metadata_unavailable') return { valid: false, status: '文件读取元数据暂不可用',
      errorNote: '文件读取元数据暂不可用，请稍后重新查询来源；未回退加载整段历史正文。' }
    return uncertain
  }
  if (result.status !== 'ok' || ![undefined, 'ok', 'completed', 'succeeded'].includes(p.status as string | undefined)) return uncertain

  const sources: ReceiptReadSource[] = []
  const rows = (value: unknown, maximum: number, kind: string): value is Data[] => {
    if (!Array.isArray(value) || value.length > maximum) return false
    let previous = 0
    return value.every(item => {
      if (!data(item) || !positive(item.seq) || item.seq <= previous || item.seq >= event.seq
        || positive(args.before_seq) && item.seq >= args.before_seq
        || ['run_id', 'role', 'message_seq', 'name'].some(key => Object.hasOwn(args, key)
          && (section === 'file_reads' && key === 'name' ? args.name !== 'workspace_read' : item[key] !== args[key]))) return false
      previous = item.seq
      // Legacy rows may lack original identities. Never invent a run relation for them.
      const relation = readerPresent && id(item.run_id) && id(item.role) && positive(item.message_seq)
        && positive(item.started_seq) && item.message_seq < item.started_seq
        && (kind === '未结算操作' ? item.started_seq === item.seq : item.started_seq < item.seq)
        ? item.run_id === read.reader_run_id ? '同轮原观察（非重新核验）' : '其他轮次原观察' : undefined
      sources.push({ seq: item.seq, kind, ...(relation ? { relation } : {}) })
      return true
    })
  }
  const page = (values: Data[], more: unknown, next: unknown, maximum: number): boolean => typeof more === 'boolean'
    && (more ? values.length === maximum && values.length > 0 && next === values[0].seq : next === null)

  if (section === 'file_reads') {
    if (['receipts', 'pending_operations', 'pending_has_more', 'pending_next_before_seq'].some(key => Object.hasOwn(result, key))
      || !rows(result.observations, limit, '文件读取')
      || !page(result.observations, result.has_more, result.next_before_seq, limit)) return uncertain
    const binding = object(result.workspace_binding)
    const validScope = result.scope_available === true && positive(binding.binding_seq) && binding.binding_seq < p.started_seq
      && id(binding.workspace_id) && typeof binding.fingerprint === 'string' && Boolean(binding.fingerprint.trim()) && binding.fingerprint.length <= 256
    if (result.query_state === 'scope_unavailable') {
      return result.scope_available === false && result.workspace_binding === null && result.observations.length === 0
        && result.has_more === false && result.next_before_seq === null
        ? { valid: true, status: '文件读取来源范围不可查询', section, sources, hasMore: false } : uncertain
    }
    if (!validScope || !['matched_records', 'no_matching_records'].includes(String(result.query_state))
      || (result.query_state === 'matched_records') !== (result.observations.length > 0)) return uncertain
    if (!result.observations.every(row => {
      const complete = row.offset === 0 && row.returned_bytes === row.bytes && row.truncated === false
      return positive(row.started_seq) && row.started_seq > (binding.binding_seq as number) && row.started_seq < (row.seq as number)
        && id(row.run_id) && id(row.role) && positive(row.message_seq) && row.message_seq < row.started_seq
        && path(row.path) && (!Object.hasOwn(args, 'file_path') || args.file_path === row.path)
        && typeof row.sha256 === 'string' && /^[a-f0-9]{64}$/.test(row.sha256)
        && count(row.bytes) && row.bytes <= 1024 * 1024 && count(row.offset) && row.offset <= 1024 * 1024
        && count(row.returned_bytes) && row.returned_bytes <= 65536 && row.returned_bytes <= Math.max(0, row.bytes - row.offset)
        && typeof row.truncated === 'boolean' && row.content_complete === complete && (row.truncated || complete)
        && row.read_tool === 'read_receipts' && data(row.read_args) && row.read_args.receipt_seq === row.seq
        && Object.keys(row.read_args).length === 1
    })) return uncertain
    return { valid: true, status: result.query_state === 'matched_records' ? '已存文件读取观察已读取' : '当前筛选无匹配文件读取记录',
      section, sources, hasMore: result.has_more as boolean }
  }
  if (Object.hasOwn(args, 'file_path') || ['observations', 'query_state', 'scope_available', 'workspace_binding'].some(key => Object.hasOwn(result, key))
    || Object.hasOwn(args, 'receipt_seq') && args.include_pending === true
    || !rows(result.receipts, Object.hasOwn(args, 'receipt_seq') ? 1 : limit, '回执')
    || !page(result.receipts, result.has_more, result.next_before_seq, limit)
    || Object.hasOwn(args, 'receipt_seq') && (result.has_more !== false || result.receipts.some(row => row.seq !== args.receipt_seq))) return uncertain
  if (args.include_pending === true) {
    if (!rows(result.pending_operations, limit, '未结算操作')
      || !page(result.pending_operations, result.pending_has_more, result.pending_next_before_seq, limit)) return uncertain
  } else if (['pending_operations', 'pending_has_more', 'pending_next_before_seq'].some(key => Object.hasOwn(result, key))) return uncertain
  return { valid: true, status: '已存回执已读取', section, sources, hasMore: result.has_more as boolean,
    pendingHasMore: result.pending_has_more === true }
}
