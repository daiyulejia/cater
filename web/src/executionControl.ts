export type ControlAction = 'pause' | 'resume' | 'cancel'
export type ExecutionControl = { state: 'running' | 'paused' | 'cancelled'; seq: number | null; reason: string }
export const controlLabels = { pause: '暂停执行', resume: '恢复执行', cancel: '取消执行' }
export const executionLabel = (state?: ExecutionControl['state']) => state === 'paused' ? '已暂停' : state === 'cancelled' ? '已取消' : ''
