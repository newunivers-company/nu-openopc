import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(here, 'MessageComposer.tsx'), 'utf8')

assert.match(
  src,
  /<option value="task">Task · single agent<\/option>/,
  'Task Mode must explain that it uses one execution agent',
)
assert.match(
  src,
  /<option value="company">Company · role team<\/option>/,
  'Company Mode must explain that it coordinates a role team',
)
assert.match(
  src,
  /composer-mode-inline-label">Team<\/span>[\s\S]*<option value="corporate">Default roles<\/option>/,
  'company architecture choices must use user-facing team language',
)

console.log('MessageComposer.test.tsx: OK (execution modes disclose their operating model)')
