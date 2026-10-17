import { nativeProjectRestoreReceiptStatus, projectRestoreReceiptEvent } from './conversationActivityState'
import type { TeamEvent } from './team'

export function NativeProjectRestoreReceipt({ event }: { event: TeamEvent }) {
  const status = nativeProjectRestoreReceiptStatus(event)
  if (!projectRestoreReceiptEvent(event)) {
    if (status === '恢复影响待核对') return <p>文件可能已经写入；请先核对原操作是否停止、目标文件及对应回执，不要直接重试。</p>
    if (status === '未开始恢复历史文件') return <p>未写入这份历史文件。请核对来源引用、目录权限和目标已有内容；不同内容不会被覆盖。</p>
    return null
  }
  const result = event.payload.result as Record<string, unknown>
  const source = result.source as Record<string, string | number>
  const member = result.member as { path: string; bytes: number; sha256: string; executable: boolean }
  const restored = result.restored_file as { path: string; executable: boolean; executable_requested: boolean }
  return <div className="mt-1 min-w-0 space-y-2 [overflow-wrap:anywhere]">
    <p className="font-semibold">{status}</p>
    <p className="whitespace-pre-wrap">源相对路径：{member.path}</p>
    <p className="whitespace-pre-wrap">目标相对路径：{restored.path}</p>
    <p>源与目标一致：{member.bytes} 字节</p>
    <p className="font-mono">源与目标 SHA-256：{member.sha256}</p>
    <p>执行标记：来源{member.executable ? '有' : '无'} · 请求{restored.executable_requested ? '设置' : '不设置'} · 目标实际{restored.executable ? '有' : '无'}</p>
    <p>仅恢复文件；未运行，未验收，不继承历史执行权限或复核结论。</p>
    <details key={`restore-source:${event.seq}`}>
      <summary className="cursor-pointer">来源与范围</summary>
      <div className="mt-1 space-y-1 whitespace-pre-wrap">
        <p>同项目历史工程包：{source.filename}</p>
        <p>来源项目：{source.project_id}</p>
        <p>来源任务：{source.task_id} · 历史交付 #{source.delivery_seq}</p>
        <p>目录版本：事件 #{result.as_of_seq as number}</p>
        <p className="font-mono">版本事件 SHA-256：{result.as_of_hash as string}</p>
        <p className="font-mono">交付事件 SHA-256：{source.delivery_hash}</p>
        <p>历史文件事件 #{source.artifact_seq} · 当时发布成员：{source.author}</p>
        <p className="font-mono">工程包 SHA-256：{source.content_sha256}</p>
        <p className="font-mono">文件事件 SHA-256：{source.event_hash}</p>
        <p>以上为恢复时目标观察，不代表当前磁盘状态。</p>
      </div>
    </details>
  </div>
}
