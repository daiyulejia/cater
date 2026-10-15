import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function ChatMarkdown({ text }: { text: string }) {
  return <div className="chat-markdown text-[13px] leading-[1.75] sm:text-sm">
    <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
      a: ({ href, children }) => href && /^https?:\/\//i.test(href)
        ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
      img: ({ alt }) => <span>图片：{alt || '未命名图片'}</span>,
      table: ({ children }) => <div className="overflow-x-auto"><table>{children}</table></div>,
    }}>{text}</Markdown>
  </div>
}
