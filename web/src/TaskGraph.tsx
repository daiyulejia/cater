import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import {
  Background, BackgroundVariant, Handle, MarkerType, Panel, Position,
  ReactFlow, ReactFlowProvider, useReactFlow,
} from '@xyflow/react'
import type { AriaLabelConfig, Edge, Node, NodeProps } from '@xyflow/react'
import { CheckCircle2, Circle, CircleAlert, Clock3, List, Maximize, Network, X, ZoomIn, ZoomOut } from 'lucide-react'
import type { WorkState } from './WorkItems'
import { layoutTaskGraph } from './taskGraphLayout'
import '@xyflow/react/dist/style.css'
import './TaskGraph.css'

type WorkItem = WorkState['items'][number]
type GraphWorkProps = {
  work: WorkState
  actorText: (id: string) => string
  portrait?: (id: string, size: 'sm' | 'md') => ReactNode
}
export type TaskGraphProps = GraphWorkProps & { open: boolean; onOpenChange: (open: boolean) => void }

const labels: Record<string, string> = {
  planned: '已安排', ready: '可以开工', running: '正在执行', submitted: '等待验收',
  accepted: '阶段通过', blocked: '等待前置', stale: '需要重新核对', uncertain: '结果待核实', rework: '需要返工',
}
const deliveryLabels: Record<string, string> = {
  unprepared: '待阶段验收',
  pending: '等待整体核对', reviewing: '正在整体核对', checking: '正在整体检查', uncertain: '复核结果待确认',
  accepted: '整体验收通过', stale: '需要重新核对', needs_evidence: '还需补齐依据', rework: '需要返工', invalid: '复核记录待补正', blocked: '停在检查点',
}
const statusLabel = (status: string, staleScope = false) => staleScope && status === 'accepted' ? '旧阶段通过' : labels[status] ?? '状态待核对'
const checkLabel = (status?: string, staleScope = false) => status === 'passed' ? staleScope ? '旧记录通过，待重新核对' : '通过' : status === 'failed' ? '未通过' : '待核验'

type TopologyItem = Pick<WorkItem, 'id' | 'depends_on'>

function Status({ value, staleScope = false }: { value: string; staleScope?: boolean }) {
  const Icon = staleScope && value === 'accepted' ? CircleAlert : value === 'accepted' ? CheckCircle2 : value === 'blocked' || value === 'submitted' ? Clock3
    : ['rework', 'stale', 'uncertain'].includes(value) ? CircleAlert : Circle
  return <span className="task-graph-status" data-status={value} data-stale-scope={staleScope}><Icon size={14} aria-hidden="true" />{statusLabel(value, staleScope)}</span>
}

type TaskNode = Node<{
  item: WorkItem; ordinal: number; owner: string; avatar: ReactNode; current: boolean; staleScope: boolean
  choose: (id: string) => void
}, 'work'>

function WorkNode({ data }: NodeProps<TaskNode>) {
  return <>
    <Handle type="target" position={Position.Left} isConnectable={false} aria-hidden="true" />
    <button type="button" className="task-graph-node nodrag nopan" aria-pressed={data.current}
      aria-label={`工作 ${data.ordinal}：${data.item.objective}，${data.owner}，${statusLabel(data.item.status, data.staleScope)}${data.staleScope ? '，范围已更新' : ''}`}
      onClick={(event) => { event.stopPropagation(); data.choose(data.item.id) }}>
      <span className="task-graph-node-top"><span className="task-graph-owner">{data.avatar}<strong>{data.owner}</strong></span><span>工作 {data.ordinal}</span></span>
      <span className="task-graph-objective">{data.item.objective}</span>
      <span className="task-graph-node-bottom"><Status value={data.item.status} staleScope={data.staleScope} />{data.staleScope && <span className="task-graph-scope">范围已更新</span>}</span>
    </button>
    <Handle type="source" position={Position.Right} isConnectable={false} aria-hidden="true" />
  </>
}
const nodeTypes = { work: WorkNode }
const ariaLabels: Partial<AriaLabelConfig> = {
  'node.a11yDescription.default': '工作项', 'node.a11yDescription.keyboardDisabled': '工作项',
  'node.a11yDescription.ariaLiveMessage': () => '工作项位置', 'edge.a11yDescription.default': '前置依赖',
  'controls.ariaLabel': '图谱视图', 'controls.zoomIn.ariaLabel': '放大图谱', 'controls.zoomOut.ariaLabel': '缩小图谱',
  'controls.fitView.ariaLabel': '适应全部任务', 'controls.interactive.ariaLabel': '图谱只读',
  'minimap.ariaLabel': '任务缩略图', 'handle.ariaLabel': '依赖连接点',
}
const fitOptions = { padding: 0.16, minZoom: 0.15, maxZoom: 1, duration: 0 }

function GraphControls() {
  const flow = useReactFlow()
  return <Panel position="top-left" className="task-graph-controls" aria-label="图谱视图">
    <button type="button" title="放大图谱" aria-label="放大图谱" onClick={() => void flow.zoomIn({ duration: 0 })}><ZoomIn size={18} /></button>
    <button type="button" title="缩小图谱" aria-label="缩小图谱" onClick={() => void flow.zoomOut({ duration: 0 })}><ZoomOut size={18} /></button>
    <button type="button" title="适应全部任务" aria-label="适应全部任务" onClick={() => void flow.fitView(fitOptions)}><Maximize size={18} /></button>
  </Panel>
}

function ItemDetails({ item, work, actorText, portrait, choose, headingRef }: GraphWorkProps & {
  item: WorkItem; choose: (id: string) => void; headingRef: { current: HTMLHeadingElement | null }
}) {
  const ordinal = work.items.findIndex(entry => entry.id === item.id) + 1
  const files = work.delivery?.manifest?.files.filter(file => item.submission?.artifact_seqs.includes(file.artifact_seq)) ?? []
  const successors = work.items.filter(entry => entry.depends_on.includes(item.id))
  const related = (id: string) => {
    const index = work.items.findIndex(entry => entry.id === id)
    return index < 0 ? <span className="task-graph-missing">{id} · 当前计划未包含</span>
      : <button type="button" onClick={() => choose(id)}>工作 {index + 1} · {work.items[index].objective}</button>
  }
  return <aside className="task-graph-details" aria-label="工作项详情">
    <h3 ref={headingRef} tabIndex={-1}>工作 {ordinal}</h3>
    <p className="task-graph-detail-objective">{item.objective}</p>
    <div className="task-graph-detail-status"><Status value={item.status} staleScope={work.scope_current === false} />{work.scope_current === false && <span className="task-graph-scope">范围已更新</span>}</div>
    <dl className="task-graph-people">
      <div><dt>负责人</dt><dd>{portrait?.(item.owner, 'sm')}{actorText(item.owner)}</dd></div>
      <div><dt>复核人</dt><dd>{item.reviewer ? <>{portrait?.(item.reviewer, 'sm')}{actorText(item.reviewer)}</> : '尚未安排独立复核人'}</dd></div>
    </dl>
    <section><h4>最新状态原因</h4><p>{item.reason || '暂无状态原因记录'}</p></section>
    <section><h4>前置工作</h4>{item.depends_on.length ? <ul className="task-graph-related">{[...new Set(item.depends_on)].map(id => <li key={id}>{related(id)}</li>)}</ul> : <p>无前置工作</p>}</section>
    {successors.length > 0 && <section><h4>后续工作</h4><ul className="task-graph-related">{successors.map(entry => <li key={entry.id}>{related(entry.id)}</li>)}</ul></section>}
    <section><h4>验收条件</h4>{item.criteria.length ? <ul className="task-graph-criteria">{item.criteria.map(criterion => {
      const checks = item.checks.filter(check => check.id === criterion.id)
      return <li key={criterion.id}><p>{criterion.text}</p><p className="task-graph-muted">{criterion.kind === 'local_check' ? '实际检查' : '独立审查'} · {checks.length ? '' : '待核验'}</p>
        {checks.map((check, index) => <div className="task-graph-check" key={index}>
          <p><strong>{checkLabel(check.status, work.scope_current === false)}</strong>{check.reason && ` · ${check.reason}`}</p>
          {check.quote && <blockquote>“{check.quote}”</blockquote>}
          {check.artifact_seq != null && <p>依据成果 #{check.artifact_seq}</p>}
          {check.receipt_seq != null && <p>检查回执 #{check.receipt_seq}</p>}
        </div>)}
      </li>
    })}</ul> : <p>暂无验收条件记录</p>}</section>
    {item.checks.filter(check => !item.criteria.some(criterion => criterion.id === check.id)).map((check, index) => <section key={index}><h4>补充检查 · {check.id}</h4>
      <p>{checkLabel(check.status, work.scope_current === false)} · {check.reason}</p>{check.quote && <blockquote>“{check.quote}”</blockquote>}
      {check.artifact_seq != null && <p>依据成果 #{check.artifact_seq}</p>}{check.receipt_seq != null && <p>检查回执 #{check.receipt_seq}</p>}
    </section>)}
    <section><h4>成果与证据</h4>{item.submission ? <>
      <p>提交记录 #{item.submission.seq}</p>
      <p>{item.submission.artifact_seqs.length ? `成果记录：${item.submission.artifact_seqs.map(seq => `#${seq}`).join('、')}` : '暂无成果编号'}</p>
      {item.submission.reused_from != null && <p>沿用候选成果 #{item.submission.reused_from}，本轮验收独立记录。</p>}
      {files.length > 0 && <ul>{files.map((file, index) => <li key={index}>{file.path} · 成果 #{file.artifact_seq}</li>)}</ul>}
    </> : <p>尚未提交成果</p>}</section>
  </aside>
}

function GraphBody(props: GraphWorkProps) {
  const { work, actorText, portrait } = props
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const headingRef = useRef<HTMLHeadingElement>(null)
  const canvasRef = useRef<HTMLDivElement>(null)
  const selected = work.items.find(item => item.id === selectedId) ?? work.items[0]
  const topology = JSON.stringify(work.items.map(item => ({ id: item.id, depends_on: [...new Set(item.depends_on)].sort() }))
    .sort((a, b) => a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  const graph = useMemo(() => layoutTaskGraph(JSON.parse(topology) as TopologyItem[]), [topology])
  const choose = useCallback((id: string) => {
    setSelectedId(id)
    // Only explicit selection moves focus; realtime updates leave reading position alone.
    requestAnimationFrame(() => {
      headingRef.current?.focus({ preventScroll: true })
      if (window.matchMedia('(max-width: 760px)').matches) headingRef.current?.scrollIntoView({ block: 'start', behavior: 'instant' })
    })
  }, [])
  useEffect(() => {
    // React Flow's attribution label is currently hardcoded in the library.
    canvasRef.current?.querySelector('.react-flow__attribution a')?.setAttribute('aria-label', 'React Flow 图谱组件署名')
  }, [])
  if (selectedId && !work.items.some(item => item.id === selectedId)) setSelectedId(null)
  const nodes: TaskNode[] = graph.nodes.map(node => {
    const item = work.items.find(entry => entry.id === node.id)!
    const ordinal = work.items.indexOf(item) + 1
    return { ...node, type: 'work', selected: item.id === selected?.id, draggable: false, connectable: false, deletable: false, selectable: false, focusable: false,
      domAttributes: { 'aria-roledescription': '工作项' },
      data: { item, ordinal, owner: actorText(item.owner), avatar: portrait?.(item.owner, 'sm'), current: item.id === selected?.id, staleScope: work.scope_current === false, choose },
    }
  })
  const edges: Edge[] = graph.edges.map(edge => ({ ...edge, type: 'smoothstep', animated: false, selectable: false, deletable: false, reconnectable: false, focusable: false,
    ariaLabel: `${work.items.find(item => item.id === edge.source)?.objective} → ${work.items.find(item => item.id === edge.target)?.objective}，前置依赖`,
    domAttributes: { 'aria-roledescription': '前置依赖' }, markerEnd: { type: MarkerType.ArrowClosed, color: '#628b80' },
    className: edge.source === selected?.id || edge.target === selected?.id ? 'task-graph-edge-current' : undefined,
  }))
  return <>
    <div className="task-graph-summary" role="status">
      <span>{work.items.filter(item => item.status === 'accepted').length} / {work.items.length} 项{work.scope_current === false ? '旧阶段通过' : '阶段通过'}</span>
      {work.plan_seq != null && <span>计划 #{work.plan_seq}</span>}
      {work.stage_accepted && <span>{work.scope_current === false ? '旧阶段验收已通过' : '阶段验收已通过'}</span>}
      <span>{work.delivery ? work.scope_current === false && work.delivery.status === 'accepted' ? '旧整体验收通过，待重新核对' : deliveryLabels[work.delivery.status] ?? '整体交付状态待核对' : '尚未整体交付'}</span>
      {work.scope_current === false && <strong>方向或店规已更新，旧结果暂不放行。</strong>}
    </div>
    <div className="task-graph-layout">
      <div className="task-graph-main">
        <div className="task-graph-view-switch" role="group" aria-label="任务展示方式">
          <button type="button" aria-label="依赖图" title="依赖图" aria-pressed={view === 'graph'} onClick={() => setView('graph')}><Network size={16} />依赖图</button>
          <button type="button" aria-label="工作列表" title="工作列表" aria-pressed={view === 'list'} onClick={() => setView('list')}><List size={16} />工作列表</button>
        </div>
        <div ref={canvasRef} className="task-graph-canvas" hidden={view !== 'graph'} role="region" aria-label="当前任务依赖图">
          <ReactFlow<TaskNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={fitOptions}
            onNodeClick={(_, node) => choose(node.id)}
            minZoom={0.1} maxZoom={2} nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false}
            elementsSelectable={false} nodesFocusable={false} edgesFocusable={false} deleteKeyCode={null}
            selectionKeyCode={null} multiSelectionKeyCode={null} selectNodesOnDrag={false} selectionOnDrag={false}
            autoPanOnNodeFocus={false} disableKeyboardA11y ariaLabelConfig={ariaLabels} zoomOnDoubleClick={false}
            preventScrolling={false} aria-label="当前任务依赖图">
            <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#c9dcd4" />
            <GraphControls />
          </ReactFlow>
        </div>
        <div className={view === 'graph' ? 'task-graph-list task-graph-sr-only' : 'task-graph-list'}>
          <ol aria-label="当前任务工作列表">{work.items.map((item, index) => <li key={item.id}>
            <button type="button" aria-pressed={selected?.id === item.id} onClick={() => choose(item.id)}>
              <span className="task-graph-list-top"><strong>工作 {index + 1} · {actorText(item.owner)}</strong><Status value={item.status} staleScope={work.scope_current === false} /></span>
              <span>{item.objective}</span>
              <span className="task-graph-muted">{item.depends_on.length ? `前置：${[...new Set(item.depends_on)].map(id => {
                const dependencyIndex = work.items.findIndex(entry => entry.id === id)
                return dependencyIndex < 0 ? `${id}（当前计划未包含）` : `工作 ${dependencyIndex + 1}`
              }).join('、')}` : '无前置工作'}{work.scope_current === false ? ' · 范围已更新' : ''}</span>
            </button>
          </li>)}</ol>
        </div>
      </div>
      {selected && <ItemDetails {...props} item={selected} choose={choose} headingRef={headingRef} />}
    </div>
  </>
}

export function TaskGraph({ open, onOpenChange, ...props }: TaskGraphProps) {
  const openingFocus = useRef<HTMLElement | null>(null)
  useEffect(() => {
    if (open && !props.work.items.length) onOpenChange(false)
  }, [open, props.work.items.length, onOpenChange])
  if (!props.work.items.length) return null
  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal><Dialog.Overlay className="task-graph-overlay" /><Dialog.Content className="task-graph-dialog"
      onOpenAutoFocus={() => { openingFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null }}
      onCloseAutoFocus={event => { event.preventDefault(); if (openingFocus.current?.isConnected) openingFocus.current.focus() }}>
      <header className="task-graph-header"><div><Dialog.Title>任务脉络</Dialog.Title><Dialog.Description className="task-graph-sr-only">当前任务 · {props.work.items.length} 个工作项</Dialog.Description></div>
        <Dialog.Close asChild><button type="button" className="task-graph-close" aria-label="关闭任务脉络" title="关闭任务脉络"><X size={20} /></button></Dialog.Close>
      </header>
      <ReactFlowProvider><GraphBody {...props} /></ReactFlowProvider>
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>
}

export default TaskGraph
