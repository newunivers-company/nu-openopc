import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const component = readFileSync(join(here, 'MissionControlPage.tsx'), 'utf8')
const app = readFileSync(join(here, '..', 'App.tsx'), 'utf8')
const socket = readFileSync(join(here, '..', 'lib', 'wsClient.ts'), 'utf8')

assert.match(component, /aria-busy=/, 'Mission Control must expose its loading state')
assert.match(component, /role="alert"/, 'Mission Control must expose its unavailable state')
assert.match(component, /No active alerts/, 'Mission Control must include a healthy empty state')
assert.match(component, /Provider capacity/, 'Mission Control must render provider SLO and quota evidence')
assert.match(component, /Recommended next/, 'Mission Control must render deterministic recommendations')
assert.match(app, /activePage === 'operations'/, 'App must expose the Mission Control page')
assert.match(app, /30_000/, 'Mission Control must refresh periodically while visible')
assert.match(socket, /'mission_control'/, 'Mission Control transport must be project scoped')

console.log('MissionControlPage.test.tsx: OK')
