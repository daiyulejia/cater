import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WorkItems } from '../src/WorkItems'
import type { WorkState } from '../src/WorkItems'

const work: WorkState = {
  plan_seq: 4, stage_accepted: false, scope_current: true,
  items: [
    { id: 'first', owner: 'dev', reviewer: 'test', objective: '交付登录入口', status: 'submitted', reason: '', depends_on: [],
      criteria: [{ id: 'test', kind: 'local_check', text: '检查错误密码分支' }], checks: [], submission: { seq: 7, artifact_seqs: [6] } },
    { id: 'second', owner: 'dev', reviewer: 'test', objective: '完成依赖登录的资料页', status: 'blocked', reason: '等待前置工作验收。',
      depends_on: ['first'], criteria: [{ id: 'scope', kind: 'review', text: '覆盖资料编辑' }], checks: [], submission: null },
  ],
}

describe('work item progress', () => {
  it('keeps independent items owned by the same member and distinguishes submission from acceptance', () => {
    render(<WorkItems work={work} names={{ dev: '米寇', test: '三角' }} />)
    expect(screen.getByText('交付登录入口')).toBeTruthy()
    expect(screen.getByText('完成依赖登录的资料页')).toBeTruthy()
    expect(screen.getByText('0 / 2 项通过')).toBeTruthy()
    expect(screen.getByText('等待验收')).toBeTruthy()
    expect(screen.getByText('等待前置')).toBeTruthy()
    expect(screen.getByText('前置：工作 1')).toBeTruthy()
  })

  it('renders stale scope and escaped evidence, without treating stage acceptance as final delivery', () => {
    const accepted = structuredClone(work)
    accepted.scope_current = false
    accepted.items[0].checks = [{ id: 'test', status: 'failed', reason: '用例失败', quote: '<script>bad()</script>', receipt_seq: 9 }]
    const { container } = render(<WorkItems work={accepted} names={{}} />)
    expect(screen.getByText('方向或店规已更新，旧结果暂不放行。')).toBeTruthy()
    expect(screen.getByText('检查回执 #9')).toBeTruthy()
    expect(container.querySelector('script')).toBeNull()
  })

  it('does not label stage acceptance as final delivery', () => {
    const accepted = structuredClone(work)
    accepted.stage_accepted = true
    accepted.items.forEach((item) => { item.status = 'accepted' })
    render(<WorkItems work={accepted} names={{}} />)
    expect(screen.getByText('阶段验收已通过，尚不代表整体交付完成。')).toBeTruthy()
  })
})
