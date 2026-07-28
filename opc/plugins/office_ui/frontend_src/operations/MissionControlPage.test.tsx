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
assert.match(component, /Production ready/, 'Mission Control must render the long-window readiness state')
assert.match(component, /Evidence pending/, 'Mission Control must distinguish incomplete promotion evidence')
assert.match(component, /Recommended next/, 'Mission Control must render deterministic recommendations')
assert.match(component, /Review governed action/, 'Mission Control must expose allowlisted action review')
assert.match(component, /Confirm exact plan/, 'Mission Control must confirm the exact digest-bound plan')
assert.match(app, /activePage === 'operations'/, 'App must expose the Mission Control page')
assert.match(app, /30_000/, 'Mission Control must refresh periodically while visible')
assert.match(socket, /'mission_control'/, 'Mission Control transport must be project scoped')
assert.match(socket, /'mission_action'/, 'Mission Control actions must be project scoped')

console.log('MissionControlPage.test.tsx: OK')
