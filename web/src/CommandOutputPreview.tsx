import { outputPreview } from './outputPreview'

export function OutputPreview({ name, output, limit }: { name: string; output: unknown; limit?: number }) {
  const preview = outputPreview(output, limit)
  const body = (saved = false) => {
    const shown = saved ? outputPreview(output) : preview
    return <>
      {shown.parts.map((part, index) => <div key={index} className="min-w-0">
        {shown.headTail && index === 1 && <p>中间省略，非完整原文。</p>}
        {shown.headTail && <p className="font-semibold">{part.label}</p>}
        <div className={`min-w-0 max-h-48 overflow-auto${shown.headTail && index === 1 && !saved ? ' flex flex-col-reverse' : ''}`}>
          <pre tabIndex={0} aria-label={`${name}${shown.headTail ? ` · ${part.label}` : '输出文本'}`} className="mt-1 min-w-0 max-w-full shrink-0 whitespace-pre-wrap font-mono text-[11px] [overflow-wrap:anywhere]">{part.text || '未记录该片段正文。'}</pre>
        </div>
      </div>)}
      {!shown.parts.length && <p>{shown.emptyText}</p>}
    </>
  }
  return <div className="min-w-0 max-w-full space-y-1 [overflow-wrap:anywhere]">
    {body()}
    {preview.incomplete && <p>采集未完成。</p>}
    {preview.partial && !preview.headTail && <p>当前展示非完整原文。</p>}
    {preview.clipped && <details>
      <summary className="cursor-pointer">已保存片段</summary>
      <div className="min-w-0 max-h-48 overflow-auto">{body(true)}</div>
    </details>}
  </div>
}

export function CommandOutputs({ stdout, stderr, limit }: { stdout: unknown; stderr: unknown; limit?: number }) {
  return <div className="min-w-0 space-y-2">{([['标准输出', stdout], ['错误输出', stderr]] as const).map(([name, output]) => output !== undefined && <div key={name} role="group" aria-label={name} className="min-w-0">
    <p className="font-semibold">{name}</p><OutputPreview name={name} output={output} limit={limit} />
  </div>)}</div>
}
