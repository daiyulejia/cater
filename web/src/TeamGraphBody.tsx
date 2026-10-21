import { useCallback, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Background, BackgroundVariant, Handle, MarkerType, Position, ReactFlow } from '@xyflow/react'
import type { Edge, Node, NodeProps } from '@xyflow/react'
import { Info, List, Network } from 'lucide-react'
import { projectTeamGraph, teamGraphKinds } from './teamGraph'
import type { TeamGraphEntry } from './teamGraph'
import type { TeamEvent, TeamState } from './team'
import { layoutTaskGraph } from './taskGraphLayout'
import { GraphControls } from './TaskGraphControls'
import { ariaLabels, fitOptions } from './taskGraphConfig'

type TeamNode = Node<{ entry: TeamGraphEntry; owner: string; current: boolean; avatar: ReactNode; choose: (id: string) => void }, 'team'>

function TeamMessageNode({ data }: NodeProps<TeamNode>) {
  const entry = data.entry
  return <>
    <Handle type="target" position={Position.Left} isConnectable={false} aria-hidden="true" />
    <button type="button" className="task-graph-node nodrag nopan" aria-pressed={data.current}
      aria-label={`${teamGraphKinds[entry.kind]} #${entry.seq}：${entry.text}，${data.owner}，${entry.status}${entry.historical ? '，旧目标记录' : ''}`}
      onClick={event => { event.stopPropagation(); data.choose(entry.id) }}>
      <span className="task-graph-node-top"><span className="task-graph-owner">{data.avatar}<strong>{data.owner}</strong></span><span>{teamGraphKinds[entry.kind]} #{entry.seq}</span></span>
      <span className="task-graph-objective">{entry.text || '内容未包含在当前记录中'}</span>
      <span className="task-graph-team-status">{entry.historical ? '旧目标记录 · ' : ''}{entry.status}</span>
    </button>
    <Handle type="source" position={Position.Right} isConnectable={false} aria-hidden="true" />
  </>
}
const nodeTypes = { team: TeamMessageNode }
const teamAriaLabels = { ...ariaLabels, 'node.a11yDescription.default': '协作记录', 'node.a11yDescription.keyboardDisabled': '协作记录',
  'edge.a11yDescription.default': '消息与复核关系', 'handle.ariaLabel': '协作连接点' }

export function TeamGraphBody({ team, events, actorText, portrait }: {
  team: TeamState; events: TeamEvent[]; actorText: (id: string) => string; portrait?: (id: string, size: 'sm' | 'md') => ReactNode
}) {
  const model = useMemo(() => projectTeamGraph(team, events), [team, events])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const headingRef = useRef<HTMLHeadingElement>(null)
  const selected = model.nodes.find(entry => entry.id === selectedId) ?? model.nodes[0]
  const topology = JSON.stringify(model.nodes.map(entry => ({ id: entry.id,
    depends_on: model.edges.filter(edge => edge.target === entry.id).map(edge => edge.source) })))
  // Only topology affects positions; polling statuses leaves the viewport and selection alone.
  const graph = useMemo(() => layoutTaskGraph(JSON.parse(topology) as { id: string; depends_on: string[] }[]), [topology])
  const choose = useCallback((id: string) => {
    setSelectedId(id)
    requestAnimationFrame(() => {
      headingRef.current?.focus({ preventScroll: true })
      if (window.matchMedia('(max-width: 760px)').matches) headingRef.current?.scrollIntoView({ block: 'start', behavior: 'instant' })
    })
  }, [])
  const nodes: TeamNode[] = graph.nodes.map(node => {
    const entry = model.nodes.find(item => item.id === node.id)!
    return { ...node, type: 'team', draggable: false, connectable: false, deletable: false, selectable: false, focusable: false,
      data: { entry, owner: actorText(entry.recipient || entry.sender), avatar: portrait?.(entry.recipient || entry.sender, 'sm'), current: entry.id === selected?.id, choose } }
  })
  const edges: Edge[] = model.edges.map(edge => ({ ...edge, type: 'smoothstep', animated: false, selectable: false, deletable: false, reconnectable: false, focusable: false,
    ariaLabel: `记录 #${model.nodes.find(entry => entry.id === edge.source)?.seq} → 记录 #${model.nodes.find(entry => entry.id === edge.target)?.seq}，${edge.label}`,
    domAttributes: { 'aria-roledescription': edge.label }, markerEnd: { type: MarkerType.ArrowClosed, color: '#628b80' },
  }))
  const related = selected ? model.edges.filter(edge => edge.source === selected.id || edge.target === selected.id) : []
  return <>
    <div className="task-graph-summary"><span>{model.nodes.length} 条协作记录 · 只读</span><span className="inline-flex items-center gap-2">委托 · 回报 · 复核<span role="img" aria-label="协作关系说明" title="连线表示委托、回报与复核关系，不代表严格任务依赖。"><Info size={14} aria-hidden="true" /></span></span></div>
    <div className="task-graph-layout">
      <div className="task-graph-main">
        <div className="task-graph-view-switch" role="group" aria-label="协作展示方式">
          <button type="button" title="协作图" aria-pressed={view === 'graph'} onClick={() => setView('graph')}><Network size={16} />协作图</button>
          <button type="button" title="协作列表" aria-pressed={view === 'list'} onClick={() => setView('list')}><List size={16} />协作列表</button>
        </div>
        <div className="task-graph-canvas" hidden={view !== 'graph'} role="region" aria-label="当前团队协作图">
          <ReactFlow<TeamNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={fitOptions}
            onNodeClick={(_, node) => choose(node.id)}
            minZoom={0.1} maxZoom={2} nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false}
            elementsSelectable={false} nodesFocusable={false} edgesFocusable={false} deleteKeyCode={null}
            selectionKeyCode={null} multiSelectionKeyCode={null} selectNodesOnDrag={false} selectionOnDrag={false}
            autoPanOnNodeFocus={false} disableKeyboardA11y ariaLabelConfig={teamAriaLabels} zoomOnDoubleClick={false}
            preventScrolling={false} aria-label="当前团队协作图">
            <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#c9dcd4" /><GraphControls />
          </ReactFlow>
        </div>
        <div className={view === 'graph' ? 'task-graph-list task-graph-sr-only' : 'task-graph-list'}>
          <ol aria-label="当前团队协作列表">{model.nodes.map(entry => <li key={entry.id}>
            <button type="button" aria-pressed={entry.id === selected?.id} onClick={() => choose(entry.id)}>
              <span className="task-graph-list-top"><strong>{teamGraphKinds[entry.kind]} #{entry.seq} · {actorText(entry.sender)} → {entry.recipient ? actorText(entry.recipient) : '接收者未记录'}</strong><span>{entry.status}</span></span>
              <span>{entry.text || '内容未包含在当前记录中'}</span>{entry.historical && <span className="task-graph-scope">旧目标记录</span>}
            </button>
          </li>)}</ol>
        </div>
      </div>
      {selected ? <aside className="task-graph-details" aria-label="协作记录详情">
        <h3 ref={headingRef} tabIndex={-1}>{teamGraphKinds[selected.kind]} #{selected.seq}</h3>
        <p className="task-graph-detail-objective">{selected.status}</p>
        {selected.historical && <p className="task-graph-scope">旧目标记录，不作为当前目标完成依据</p>}
        <dl className="task-graph-people"><div><dt>发送者</dt><dd>{portrait?.(selected.sender, 'sm')}{actorText(selected.sender)}</dd></div>
          <div><dt>接收者</dt><dd>{selected.recipient ? <>{portrait?.(selected.recipient, 'sm')}{actorText(selected.recipient)}</> : '当前记录未包含'}</dd></div></dl>
        <section><h4>记录原文</h4><p className="task-graph-record-text">{selected.text || '内容未包含在当前记录中'}</p></section>
        {selected.withdrawal && <section><h4>工作撤回</h4>
          <p className="task-graph-record-text">撤回者：{actorText(selected.withdrawal.requester)}</p>
          <p className="task-graph-record-text">原因：{selected.withdrawal.reason}</p>
          <p>撤回事件 #{selected.withdrawal.withdrawal_seq}</p>
        </section>}
        {selected.processing && <section><h4>消息处理</h4><p>{selected.processing}</p>{selected.reason && <p className="task-graph-record-text">{selected.reason}</p>}</section>}
        {selected.artifacts && <section><h4>本次复核版本</h4><ul>{selected.artifacts.map(artifact => <li key={artifact.seq}>{artifact.filename} · 成果 #{artifact.seq}</li>)}</ul></section>}
        <section><h4>关联记录</h4>{related.length ? <ul className="task-graph-related">{related.map(edge => {
          const entry = model.nodes.find(item => item.id === (edge.source === selected.id ? edge.target : edge.source))!
          return <li key={edge.id}><button type="button" onClick={() => choose(entry.id)}>{edge.label} · {teamGraphKinds[entry.kind]} #{entry.seq}</button></li>
        })}</ul> : <p>当前记录没有可核对的关联来源。</p>}</section>
      </aside> : <aside className="task-graph-details"><p>尚无协作消息或复核记录。</p></aside>}
    </div>
  </>
}
