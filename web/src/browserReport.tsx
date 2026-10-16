import { nativeBrowserReceiptStatus } from './conversationActivityState'
import { isTeamHost } from './team'
import type { TeamEvent } from './team'

type Field = { label: string; value: string }

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const text = (value: unknown, max = 1200): string => typeof value === 'string'
  ? value.length > max ? `${value.slice(0, max)}…（显示已截断）` : value : ''
const label = (value: unknown, labels: Record<string, string>, fallback: string): string => typeof value === 'string' && Object.hasOwn(labels, value) ? labels[value] : fallback

// Select runner fields explicitly; report data is text, never executable markup or file links.
function browserReportFields(value: unknown, conclusion?: string): Field[] {
  const report = record(value)
  const fields: Field[] = []
  const add = (name: string, content: string) => { if (content) fields.push({ label: name, value: content }) }
  add('浏览器回执', conclusion ?? label(report.outcome, { passed: '检查通过', failed: '检查未通过', unavailable: '检查工具不可用', timeout: '检查超时', launch_error: '检查启动失败' }, '结果待核对'))
  add('执行错误', text(report.error))
  add('回执说明', text(report.reason))
  if (report.truncated === true) add('报告完整性', '执行器报告已截断，以下内容不是完整输出。')
  if (Array.isArray(report.steps)) {
    report.steps.slice(0, 20).forEach((value, offset) => {
      const step = record(value)
      const index = count(step.index) && step.index > 0 ? step.index : offset + 1
      const parts = [label(step.status, { passed: '通过', failed: '失败', not_run: '未执行', running: '执行中' }, '状态待核对'), text(step.label, 200)]
      parts.push(`操作：${label(step.action, { click: '点击', click_at: '点击元素内坐标', press: '按键', fill: '填写', wait: '等待', assert_visible: '检查可见性', assert_text: '检查文本', evaluate: '页面内操作', assert_js: '检查页面条件' }, '操作待核对')}`)
      if (typeof step.actual === 'boolean') parts.push(`实际判断：${step.actual ? '满足' : '不满足'}`)
      else if (typeof step.actual === 'number' && Number.isFinite(step.actual)) parts.push(`实际值：${step.actual}`)
      else if (typeof step.actual === 'string') parts.push(`实际值：${text(step.actual) || '（空文本）'}`)
      if (typeof step.observed === 'string') parts.push(`实际观察：${text(step.observed) || '（空文本）'}`)
      if (typeof step.error === 'string' && step.error) parts.push(`错误：${text(step.error)}`)
      add(`执行步骤 ${index}`, parts.filter(Boolean).join('\n'))
    })
    if (report.steps.length > 20) add('步骤显示范围', `仅显示前 20 项，共 ${report.steps.length} 项；显示已截断。`)
  }
  const counts = record(report.counts)
  for (const [key, name] of [['page_errors', '页面错误'], ['blocked_requests', '已拦截请求'], ['console_messages', '控制台消息']] as const) {
    const entries = Array.isArray(report[key]) ? report[key] as unknown[] : []
    const shown = entries.slice(0, 8)
    if (count(counts[key])) add(`${name}总数`, String(counts[key]))
    shown.forEach((value, index) => {
      const entry = record(value)
      const content = key === 'page_errors' ? text(value)
        : key === 'blocked_requests' ? [label(entry.kind, { request: '网络请求', navigation: '页面跳转', popup: '新窗口', websocket: '实时连接', download: '下载' }, '请求'), text(entry.url, 500)].filter(Boolean).join('：')
          : [label(entry.type, { warning: '警告', error: '错误' }, '消息'), text(entry.text)].filter(Boolean).join('：')
      add(`${name} ${index + 1}`, content || '记录格式待核对。')
    })
    if (entries.length > shown.length || (count(counts[key]) && counts[key] > shown.length)) {
      add(`${name}完整性`, `已显示 ${shown.length} 条；其余内容已省略或截断。`)
    }
  }
  const screenshot = record(report.screenshot)
  if (report.screenshot !== undefined && report.screenshot !== null) {
    add('截图摘要', [typeof screenshot.sha256 === 'string' && /^[a-f0-9]{64}$/i.test(screenshot.sha256) ? `SHA-256：${screenshot.sha256}` : '摘要未记录或格式待核对。',
      count(screenshot.size) ? `大小：${screenshot.size} 字节` : '',
      '仅保留截图元数据；临时文件会清理，不提供打开链接。'].filter(Boolean).join('\n'))
  }
  add('截图错误', text(report.screenshot_error))
  return fields
}

export function BrowserReport({ report, conclusion }: { report: unknown; conclusion?: string }) {
  return <ul className="min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{browserReportFields(report, conclusion).map((field, index) => <li key={index}>{field.label}：{field.value}</li>)}</ul>
}

export function NativeBrowserReceipt({ event }: { event: TeamEvent }) {
  if (!isTeamHost(event) || event.type !== 'team.tool_finished') return null
  const result = record(event.payload.result), plan = record(result.browser_plan)
  const artifacts = Array.isArray(result.checked_artifacts) ? result.checked_artifacts : []
  return <div className="min-w-0 space-y-1 whitespace-pre-wrap [overflow-wrap:anywhere]">
    <p>检查对象为固定已发布文件副本，不代表工作区现状；回执不等于整体验收通过。</p>
    <p>{result.scope_current === false ? '历史版本：目标、控制或成果版本已变化。' : result.scope_current === true ? '回执记录时范围有效。' : '当前范围未确认。'}</p>
    <ul aria-label="被检查的文件版本">{artifacts.map((value, index) => {
      const artifact = record(value)
      return <li key={index}>{text(artifact.filename) || '文件名待核对'} · {count(artifact.seq) && artifact.seq > 0 ? `成果事件 #${artifact.seq}` : '成果事件待核对'}
        <p className="font-mono">SHA-256：{typeof artifact.content_sha256 === 'string' && /^[a-f0-9]{64}$/i.test(artifact.content_sha256) ? artifact.content_sha256 : '摘要缺失或格式待核对'}</p>
      </li>
    })}</ul>
    {!artifacts.length && <p>被检查的文件版本未完整记录。</p>}
    {text(plan.entrypoint) && <p>浏览器入口：{text(plan.entrypoint)}</p>}
    {Array.isArray(plan.steps) && <p>计划步骤：{plan.steps.length} 项；实际执行以回执步骤为准。</p>}
    <BrowserReport report={result.browser_report} conclusion={nativeBrowserReceiptStatus(event)} />
    {typeof result.exit_code === 'number' && Number.isFinite(result.exit_code) && <p>退出码：{result.exit_code}</p>}
    {count(result.duration_ms) && <p>耗时：{result.duration_ms} 毫秒</p>}
    {text(result.note) && <p>{text(result.note)}</p>}
    {text(result.message) && <p>{text(result.message)}</p>}
  </div>
}
