import { useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Copy, Download, ExternalLink, FileText, X } from 'lucide-react'

export type Artifact = {
  seq: number; title: string; summary: string; kind?: string; uri?: string; message_seq?: number
  actor?: string; source?: string; content?: string; format?: string; filename?: string; content_error?: string
}

function externalUrl(value?: string): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined
  } catch { return undefined }
}

function downloadName(item: Artifact): string {
  const name = item.filename?.trim()
  const unsafeCharacter = name ? [...name].some((char) => char.charCodeAt(0) < 32 || '\\/:*?"<>|'.includes(char)) : false
  if (name && new TextEncoder().encode(name).length <= 180 && !unsafeCharacter && !name.endsWith('.') && !['.', '..'].includes(name) && !/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(name.split('.')[0])) return name
  return `成果-${item.seq}.${item.format === 'markdown' ? 'md' : 'txt'}`
}

const sourceText = (source?: string) => source === 'local_demo' ? '演示样例' : source === 'external' ? '成员发布' : '来源未记录'
const bodyError = (reason?: string) => ({ too_large: '正文太大，需要拆分后重新发布。', invalid_format: '正文格式不支持，摘要已保留。', invalid_encoding: '正文编码异常，摘要已保留。', invalid_content: '没有收到可阅读的正文。' }[reason ?? ''] ?? '没有收到可阅读的正文。')

export function ArtifactPreview({ item, publisher, showSummary = true }: { item: Artifact; publisher?: string; showSummary?: boolean }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [downloadError, setDownloadError] = useState(false)
  const hasBody = typeof item.content === 'string' && Boolean(item.content.trim()) && !item.content_error && new TextEncoder().encode(item.content).length <= 65536
  const source = externalUrl(item.uri)
  const filename = downloadName(item)
  const copy = async () => {
    try { await navigator.clipboard.writeText(item.content ?? ''); setCopyState('copied') }
    catch { setCopyState('failed') }
  }
  const download = () => {
    try {
      const blob = new Blob([item.content ?? ''], { type: item.format === 'markdown' ? 'text/markdown;charset=utf-8' : 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = filename
      anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setDownloadError(false)
    } catch { setDownloadError(true) }
  }
  return <div>
    {showSummary && <><strong className="block text-xs text-[#294c40]">{item.title}</strong><p className="mt-1 whitespace-pre-wrap break-words text-xs leading-5 text-[#687e73]">{item.summary}</p></>}
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 text-[11px]">
      <span className={item.source === 'local_demo' ? 'text-[#a06b36]' : 'text-[#839087]'}>{sourceText(item.source)}</span>
      {hasBody && <Dialog.Root onOpenChange={() => { setCopyState('idle'); setDownloadError(false) }}>
        <Dialog.Trigger asChild><button className="inline-flex items-center gap-1.5 font-semibold text-[#2e786b] hover:text-[#a95945]"><FileText size={14} /> 查看正文</button></Dialog.Trigger>
        <Dialog.Portal>
          <Dialog.Overlay className="artifact-overlay" />
          <Dialog.Content className="artifact-reader flex flex-col">
            <header className="artifact-toolbar shrink-0 px-5 py-4 sm:px-8">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0"><p className="mb-1 text-[11px] font-semibold text-[#a7644d]">{publisher || '伙伴'} · {sourceText(item.source)}</p><Dialog.Title className="break-words text-lg font-bold leading-snug text-[#294a40]">{item.title}</Dialog.Title></div>
                <div className="flex shrink-0 gap-1">
                  <button onClick={() => void copy()} className="artifact-tool" title={copyState === 'copied' ? '已复制正文' : '复制正文'} aria-label="复制正文">{copyState === 'copied' ? <Check size={18} /> : <Copy size={18} />}</button>
                  <button onClick={download} className="artifact-tool" title={`下载 ${filename}`} aria-label="下载正文"><Download size={18} /></button>
                  <Dialog.Close asChild><button className="artifact-tool" title="回到对话" aria-label="关闭成果正文"><X size={19} /></button></Dialog.Close>
                </div>
              </div>
              <Dialog.Description className="mt-2 break-words text-xs leading-5 text-[#728679]">{item.summary}</Dialog.Description>
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[#9a8778]"><span className="break-all">{filename}</span><span>事件 #{item.seq}</span>{copyState !== 'idle' && <span role="status" className="text-[#377968]">{copyState === 'copied' ? '已复制' : '复制未完成'}</span>}{downloadError && <span role="alert" className="text-rose-700">下载未完成</span>}</div>
            </header>
            <article className="artifact-document min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-8 sm:py-8">
              {item.format === 'markdown' ? <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={(url) => externalUrl(url) ?? ''} components={{
                a: ({ href, children }) => externalUrl(href) ? <a href={externalUrl(href)} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
                img: ({ alt }) => <span className="artifact-image-label">图片：{alt || '未命名图片'}</span>,
                table: ({ children }) => <div className="artifact-table-scroll"><table>{children}</table></div>,
              }}>{item.content}</Markdown> : item.format === 'code' ? <pre className="artifact-code"><code>{item.content}</code></pre> : <div className="whitespace-pre-wrap break-words text-sm leading-7">{item.content}</div>}
            </article>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>}
      {source && <a href={source} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[#377d70]"><ExternalLink size={13} /> 打开来源</a>}
    </div>
    {item.uri && !source && <p className="mt-2 break-all text-[11px] leading-5 text-[#86968c]">资源引用：{item.uri}</p>}
    {item.content_error && <p className="mt-2 text-[11px] leading-5 text-[#a3704a]">{bodyError(item.content_error)}</p>}
  </div>
}
