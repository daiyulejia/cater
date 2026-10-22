export function conversationTitle(goal: string): { label: string; excerpt: boolean } {
  // Without grapheme segmentation, keep the original text for CSS to constrain.
  if (typeof Intl === 'undefined' || typeof Intl.Segmenter !== 'function') {
    return { label: goal, excerpt: false }
  }
  const line = (goal.split(/\r\n|\r|\n/u).find(value => value.trim()) ?? '').trim().replace(/\s+/gu, ' ')
  const segments = Array.from(new Intl.Segmenter('zh', { granularity: 'grapheme' }).segment(line), part => part.segment)
  let length = Math.min(28, segments.length)
  if (segments.length > 28) {
    const boundary = segments.slice(0, 28).findIndex(part => /^[。！？；：]$/u.test(part))
    if (boundary > 0) length = boundary
  }
  const excerpt = length < segments.length || goal.trim().replace(/\s+/gu, ' ') !== line
  return { label: segments.slice(0, length).join('') + (excerpt ? '…' : ''), excerpt }
}
