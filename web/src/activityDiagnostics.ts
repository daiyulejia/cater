// These exact strings are fixed host diagnostics; arbitrary error text is not trusted.
const auditErrors = new Set([
  '事件日志已有签名记录，但当前没有签名密钥；未写入，请核对审计配置。',
  '事件日志签名与当前审计密钥配置不匹配或历史不完整；未写入，请核对审计配置。',
  '监督未启动：事件日志无法校验，请核对日志和审计配置。',
  '监督未启动：缺少事件日志所需的审计验证密钥，请核对审计配置。',
  '监督未启动：事件日志完整性校验未通过，请核对日志和审计配置。',
])

export function safeAuditBlocked(activity?: { state?: string; error?: string }): boolean {
  return activity?.state === 'blocked' && typeof activity.error === 'string' && auditErrors.has(activity.error)
}

export function isSafeAuditError(message: string): boolean {
  return auditErrors.has(message)
}
