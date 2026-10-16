import type { TaskDetail } from '../src/App'
import type { NativeHandoff } from '../src/nativeHandoff'

export const event = (seq: number, type: string, payload: Record<string, unknown> = {},
  actor = { id: 'team', kind: 'system', role: 'team' }): TaskDetail['events'][number] => ({ seq, type, payload, actor, category: 'collaboration' })
export const handoff = (why = '复用现有入口，保留每次交付的说明。'): NativeHandoff => ({
  version: 1, why, changed: ['提取消息详情，按需读取说明。'], impact: ['聊天入口继续显示原摘要。'],
  alternatives: ['暂不增加独立交付面板。'], reproduce: ['打开已保存的网页。', 'npm run test -- --run'], limits: ['尚未连接真实模型或生产环境。'],
  evidence: [
    { source_seq: 2, kind: 'artifact', quote: '  <main>交付正文</main>\n保留换行与空格。  ', source_hash: 'a'.repeat(64),
      filename: '说明文件.html', content_sha256: 'b'.repeat(64) },
    { source_seq: 3, kind: 'tool_receipt', quote: '定向测试：2 项通过，1 项未运行。', source_hash: 'c'.repeat(64),
      role: 'dev', tool: 'workspace_run', status: 'completed', exit_code: 0, complete: true },
    { source_seq: 4, kind: 'peer_review', quote: '移动端还需要复查。', source_hash: 'd'.repeat(64), role: 'test', status: 'changes_requested' },
  ],
})
export const completion = (seq = 10, saved: unknown = handoff()) => event(seq, 'team.objective_completed', {
  summary: `交付摘要 ${seq}`, verification: `原有验证范围 ${seq}`, artifacts: [2], run_id: `run-${seq}`, handoff: saved,
})
export const detail = (events = [completion()], id = 'native-ui'): TaskDetail => ({
  task: { id, goal: id === 'native-ui' ? '原生交付测试' : '另一件任务', acceptance: [], status: 'done',
    messages: [], assignments: [], deliverables: [], team: { enabled: true, objective: '原生交付测试', status: 'done',
      pending_count: 0, runs: [], assignments: [], checkpoint: '', revision: 1, ...(id !== 'native-ui' ? { objective: '另一件任务' } : {}) } },
  events, activity: { active: false, state: 'completed' }, review: {},
})
export const leader = { id: 'store_manager', name: '店长', role: 'leader' }
export const members = [{ id: 'dev', name: '米寇', role: 'dev' }, { id: 'test', name: '三角', role: 'test' }]
export const summaries = (tasks: TaskDetail[]) => ({ tasks: tasks.map(({ task, events }) => ({
  id: task.id, goal: task.goal, status: task.status, status_label: '已完成', conversation_count: events.length,
  latest_seq: events.at(-1)?.seq ?? 0, version: events.length,
})) })
