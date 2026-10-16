import { Panel, useReactFlow } from '@xyflow/react'
import { Maximize, ZoomIn, ZoomOut } from 'lucide-react'
import { fitOptions } from './taskGraphConfig'

export function GraphControls() {
  const flow = useReactFlow()
  return <Panel position="top-left" className="task-graph-controls" aria-label="图谱视图">
    <button type="button" title="放大图谱" aria-label="放大图谱" onClick={() => void flow.zoomIn({ duration: 0 })}><ZoomIn size={18} /></button>
    <button type="button" title="缩小图谱" aria-label="缩小图谱" onClick={() => void flow.zoomOut({ duration: 0 })}><ZoomOut size={18} /></button>
    <button type="button" title="适应全部任务" aria-label="适应全部任务" onClick={() => void flow.fitView(fitOptions)}><Maximize size={18} /></button>
  </Panel>
}
