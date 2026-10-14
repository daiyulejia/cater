import { CircleHelp, X } from 'lucide-react'

export function QuestionComposerBar({ onCancel }: { onCancel: () => void }) {
  return <div className="flex shrink-0 items-center gap-2 border-t border-[#dce8e1] bg-[#f2f8f5] px-4 py-2 text-xs text-[#34786e] sm:px-7" role="region" aria-label="提问草稿">
    <CircleHelp size={16} aria-hidden="true" />
    <strong className="min-w-0 flex-1 break-words">提问</strong>
    <button type="button" onClick={onCancel} className="origin-tool" aria-label="取消提问" title="取消提问，返回协作草稿"><X size={17} aria-hidden="true" /></button>
  </div>
}
