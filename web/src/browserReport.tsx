type Field = { label: string; value: string }

const record = (value: unknown): Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const count = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const text = (value: unknown, max = 1200): string => typeof value === 'string'
  ? value.length > max ? `${value.slice(0, max)}…（显示已截断）` : value : ''
const label = (value: unknown, labels: Record<string, string>, fallback: string): string => typeof value === 'string' && Object.hasOwn(labels, value) ? labels[value] : fallback

// Select runner fields explicitly; report data is text, never executable markup or file links.
function browserReportFields(value: unknown): Field[] {
  const report = record(value)
  const fields: Field[] = []
  const add = (name: string, content: string) => { if (content) fields.push({ label: name, value: content }) }
  add('浏览器回执', label(report.outcome, { passed: '检查通过', failed: '检查未通过', unavailable: '检查工具不可用' }, '结果待核对'))
  add('执行错误', text(report.error))
  add('回执说明', text(report.reason))
  if (report.truncated === true) add('报告完整性', '执行器报告已截断，以下内容不是完整输出。')
  if (Array.isArray(report.steps)) {
    report.steps.slice(0, 20).forEach((value, offset) => {
      const step = record(value)
      const index = count(step.index) && step.index > 0 ? step.index : offset + 1
      const parts = [label(step.status, { passed: '通过', failed: '失败', not_run: '未执行', running: '执行中' }, '状态待核对'), text(step.label, 200)]
      parts.push(`操作：${label(step.action, { click: '点击', press: '按键', fill: '填写', wait: '等待', assert_visible: '检查可见性', assert_text: '检查文本', evaluate: '页面内操作', assert_js: '检查页面条件' }, '操作待核对')}`)
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

export function BrowserReport({ report }: { report: unknown }) {
  return <ul className="whitespace-pre-wrap break-words">{browserReportFields(report).map((field, index) => <li key={index}>{field.label}：{field.value}</li>)}</ul>
}
