import type { AriaLabelConfig } from '@xyflow/react'

export const ariaLabels: Partial<AriaLabelConfig> = {
  'node.a11yDescription.default': '工作项', 'node.a11yDescription.keyboardDisabled': '工作项',
  'node.a11yDescription.ariaLiveMessage': () => '记录位置', 'edge.a11yDescription.default': '前置依赖',
  'controls.ariaLabel': '图谱视图', 'controls.zoomIn.ariaLabel': '放大图谱', 'controls.zoomOut.ariaLabel': '缩小图谱',
  'controls.fitView.ariaLabel': '适应全部任务', 'controls.interactive.ariaLabel': '图谱只读',
  'minimap.ariaLabel': '任务缩略图', 'handle.ariaLabel': '依赖连接点',
}
export const fitOptions = { padding: 0.16, minZoom: 0.15, maxZoom: 1, duration: 0 }

export function initialGraphView(): 'graph' | 'list' {
  return typeof window !== 'undefined' && window.matchMedia?.('(max-width: 760px)').matches ? 'list' : 'graph'
}
