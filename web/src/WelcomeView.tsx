import type { ReactNode } from 'react'
import cafeWelcome from './assets/cafe-welcome-v2.jpg'

export function WelcomeView({ onChoose, portrait }: { onChoose: (value: string) => void; portrait: ReactNode }) {
  const examples = ['帮我梳理一页活动说明', '帮我复盘一个难定位的 Bug', '聊聊一个新产品想法']
  return <div className="welcome-view flex-1 overflow-y-auto">
    <div className="welcome-scene"><img src={cafeWelcome} alt="月亮、三角、米寇和罐头在温暖的猫咖里" /></div>
    <div className="welcome-content mx-auto max-w-2xl px-5 pb-6 pt-5 sm:px-8">
      <div className="flex items-start gap-3">{portrait}<div className="min-w-0"><span className="text-[11px] font-bold text-[#2a8075]">店长</span><h1 className="mt-1 text-xl font-bold leading-snug text-[#23433b] sm:text-2xl">欢迎来猫咖，今天想聊什么？</h1><p className="mt-2 text-[13px] leading-6 text-[#5e796e]">先说说你心里那件事。月亮、罐头、米寇和三角都在。</p></div></div>
      <div className="mt-5 flex gap-2 overflow-x-auto pb-2 sm:flex-wrap" aria-label="任务示例">{examples.map(example => <button key={example} onClick={() => { onChoose(example); document.getElementById('new-task')?.focus() }} className="example-prompt shrink-0 border border-[#c9ddd5] bg-white px-3 py-2 text-left text-xs font-medium text-[#356d61] shadow-sm">{example}</button>)}</div>
    </div>
  </div>
}
