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
  it.each<{ label: string; value?: WorkState | null }>([
    { label: 'missing', value: undefined },
    { label: 'null', value: null },
    { label: 'empty', value: { plan_seq: null, stage_accepted: false, items: [] } },
  ])('keeps the progress heading and an accurate empty state for $label work', ({ value }) => {
    render(<WorkItems work={value} names={{}} />)
    expect(screen.getByRole('heading', { name: '工作进展' })).toBeTruthy()
    expect(screen.getByText('此任务尚无工作项与验收记录')).toBeTruthy()
    expect(screen.queryByText(/项通过|阶段验收已通过|方向或店规已更新|要求与安排/)).toBeNull()
    expect(screen.queryByRole('list')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('replaces the empty state when work arrives and restores it when changing to a task without work', () => {
    const view = render(<WorkItems work={null} names={{}} />)
    view.rerender(<WorkItems work={work} names={{}} />)
    expect(screen.queryByText('此任务尚无工作项与验收记录')).toBeNull()
    expect(screen.getByText('0 / 2 项通过')).toBeTruthy()
    view.rerender(<WorkItems work={null} names={{}} />)
    expect(screen.getByText('此任务尚无工作项与验收记录')).toBeTruthy()
    expect(screen.queryByText('交付登录入口')).toBeNull()
    expect(screen.queryByText('0 / 2 项通过')).toBeNull()
  })

  it('keeps independent items owned by the same member and distinguishes submission from acceptance', () => {
    render(<WorkItems work={work} names={{ dev: '米寇', test: '三角' }} />)
    expect(screen.getByText('交付登录入口')).toBeTruthy()
    expect(screen.getByText('完成依赖登录的资料页')).toBeTruthy()
    expect(screen.getByText('0 / 2 项通过')).toBeTruthy()
    expect(screen.getByText('等待实际检查')).toBeTruthy()
    const note = screen.getByText(/成果已提交，尚缺 1 项实际检查回执/)
    expect(note.closest('summary')).toBeTruthy()
    expect(note.textContent).toContain('源码审查不能替代运行验证')
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

  it.each(['assessment', 'lifecycle', 'review_only'])('does not claim missing execution receipts for %s', (source) => {
    const value = structuredClone(work)
    if (source === 'assessment') value.items[0].checks = [{ id: 'test', status: 'unknown', reason: '等待复核', receipt_seq: 10 }]
    if (source === 'lifecycle') value.items[0].check_lifecycle = { test: { receipt_seqs: [10] } }
    if (source === 'review_only') value.items[0].criteria[0].kind = 'review'
    render(<WorkItems work={value} names={{}} />)
    expect(screen.queryByText('等待实际检查')).toBeNull()
    expect(screen.getByText('等待验收')).toBeTruthy()
    expect(screen.queryByText(/成果已提交，尚缺/)).toBeNull()
  })

  it.each([false, true])('distinguishes in-flight and uncertain checks from checks not yet run (%s)', (uncertain) => {
    const value = structuredClone(work)
    value.items[0].check_in_flight = true
    value.items[0].check_uncertain = uncertain
    render(<WorkItems work={value} names={{}} />)
    expect(screen.getByText(uncertain ? '检查结果待核对' : '正在实际检查')).toBeTruthy()
    expect(screen.queryByText('等待实际检查')).toBeNull()
  })

  it('review-only approval without an execution receipt cannot hide missing local checks', () => {
    const value = structuredClone(work)
    value.items[0].checks = [{ id: 'test', status: 'passed', reason: '只读源码审查' }]
    render(<WorkItems work={value} names={{}} />)
    expect(screen.getByText('等待实际检查')).toBeTruthy()
    expect(screen.getByText('0 / 2 项通过')).toBeTruthy()
  })

  it('shows requirement coverage and candidate provenance without inheriting approval', () => {
    const planned = structuredClone(work)
    planned.planning = { summary: '先实现，再验证', coverage: [{ source_id: '1:goal:0', item_ids: ['first'], superseded_by: null, reason: '验证登录入口', quote: '保留接口' }] }
    planned.items[0].submission = { seq: 10, artifact_seqs: [6], reused_from: 7 }
    render(<WorkItems work={planned} names={{}} />)
    expect(screen.getByText('要求与安排')).toBeTruthy()
    expect(screen.getByText('工作 1 · 验证登录入口')).toBeTruthy()
    expect(screen.getByText('沿用候选成果 #7，本轮验收独立记录。')).toBeTruthy()
    expect(screen.getByText('0 / 2 项通过')).toBeTruthy()
  })
})
