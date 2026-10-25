import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function externalUrl(value?: string): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined
  } catch { return undefined }
}

export function ArtifactContent({ content, format }: { content: string; format?: string }) {
  return format === 'markdown' ? <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={(url) => externalUrl(url) ?? ''} components={{
    a: ({ href, children }) => externalUrl(href) ? <a href={externalUrl(href)} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
    img: ({ alt }) => <span className="artifact-image-label">图片：{alt || '未命名图片'}</span>,
    table: ({ children }) => <div className="artifact-table-scroll"><table>{children}</table></div>,
  }}>{content}</Markdown> : format === 'code' ? <pre className="artifact-code"><code>{content}</code></pre> : <div className="whitespace-pre-wrap break-words text-sm leading-7">{content}</div>
}
