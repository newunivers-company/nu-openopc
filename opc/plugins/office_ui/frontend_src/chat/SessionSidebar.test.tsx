import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('./SessionSidebar.tsx', import.meta.url), 'utf8')

assert.match(
  source,
  /enabled:\s*useVirtualRows/,
  'the virtualizer must not observe the DOM while normal rows are rendered',
)
assert.match(
  source,
  /useAnimationFrameWithResizeObserver:\s*true/,
  'virtualized row measurements must run outside ResizeObserver delivery',
)

console.log('SessionSidebar.test.tsx: OK (virtualizer resize scheduling contract)')
