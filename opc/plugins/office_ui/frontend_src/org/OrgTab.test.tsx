/**
 * Structural regression test for OrgTab's 4-tab layout.
 *
 * Guards the sub-tab rename (flow → runtime, marketplace → architecture +
 * employees) and the three new marketplace panels. Reads OrgTab.tsx as
 * source text and asserts against it — the existing zero-framework test
 * convention (see runtimeOrg.test.ts, workItemSessions.test.ts) runs with
 * plain `tsx` and requires no vitest / jsdom / @testing-library install.
 *
 * Why source-scan instead of React render:
 *   OrgTab.tsx imports './org.css'; Node can't load CSS without a vite
 *   transform. A source-scan catches the primary regression concerns
 *   (tab label rename, legacy label removal, panel import presence,
 *   default active tab) without pulling in a test runtime.
 *
 * Run with:
 *   tsx opc/plugins/office_ui/frontend_src/org/OrgTab.test.tsx
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(here, 'OrgTab.tsx'), 'utf8')
const createModalSrc = readFileSync(join(here, 'OrgCreateModal.tsx'), 'utf8')
const visualTypesSrc = readFileSync(join(here, '..', 'types', 'visual.ts'), 'utf8')
const orgCssSrc = readFileSync(join(here, 'org.css'), 'utf8')
const roleInspectorSrc = readFileSync(join(here, 'RoleInspector.tsx'), 'utf8')

// ── 1. Four sub-tab labels declared ──
for (const label of ['Team', 'Runtime', 'Architecture', 'Employees']) {
  assert.match(
    src,
    new RegExp(`label:\\s*['"]${label}['"]`),
    `OrgTab.tsx must declare tab label "${label}" (sub-tab rename regression)`,
  )
}

// ── 2. Legacy labels removed ──
assert.doesNotMatch(
  src,
  /label:\s*['"]Marketplace['"]/,
  'OrgTab.tsx must NOT declare legacy "Marketplace" sub-tab label',
)
assert.doesNotMatch(
  src,
  /label:\s*['"]Flow['"]/,
  'OrgTab.tsx must NOT declare legacy "Flow" sub-tab label',
)

// ── 3. Active tabs typed against the new SubTab union ──
assert.match(
  src,
  /type\s+SubTab\s*=\s*['"]team['"]\s*\|\s*['"]runtime['"]\s*\|\s*['"]architecture['"]\s*\|\s*['"]employees['"]/,
  'OrgTab.tsx must define SubTab = "team" | "runtime" | "architecture" | "employees"',
)

// ── 4. The three marketplace panels are imported ──
for (const comp of [
  'ArchitectureMarketplace',
  'EmployeesMarketplace',
  'ConfigImportExportPanel',
]) {
  assert.match(
    src,
    new RegExp(`import\\s*\\{\\s*${comp}\\s*\\}\\s*from\\s*['"]\\./${comp}['"]`),
    `OrgTab.tsx must import ${comp} from './${comp}'`,
  )
}

// ── 5. Default active tab is 'team' ──
assert.match(
  src,
  /useState<SubTab>\(\s*['"]team['"]\s*\)/,
  'OrgTab.tsx must initialize activeTab state to "team"',
)

// ── 6. data-testid wired on each marketplace panel root ──
for (const [file, testId] of [
  ['EmployeesMarketplace.tsx', 'employees-marketplace'],
  ['ArchitectureMarketplace.tsx', 'architecture-marketplace'],
  ['ConfigImportExportPanel.tsx', 'config-import-export-panel'],
] as const) {
  const panelSrc = readFileSync(join(here, file), 'utf8')
  assert.match(
    panelSrc,
    new RegExp(`data-testid="${testId}"`),
    `${file} root must carry data-testid="${testId}"`,
  )
}

// ── 7. Create-org prompt is optional and carried in the member payload ──
assert.match(
  visualTypesSrc,
  /prompt\?:\s*string/,
  'OrgCreateMemberInput must allow an optional prompt field',
)
assert.match(
  createModalSrc,
  /<textarea[\s\S]+placeholder="Prompt optional"/,
  'OrgCreateModal must render an optional prompt textarea for each role',
)
assert.match(
  createModalSrc,
  /prompt:\s*member\.prompt\.trim\(\)/,
  'OrgCreateModal submit payload must trim and include each role prompt',
)
assert.doesNotMatch(
  createModalSrc,
  /membersValid[\s\S]{0,120}prompt/,
  'OrgCreateModal must not require prompt text for member validity',
)

// ── 8. Native select option popovers must have readable themed colors ──
assert.match(
  orgCssSrc,
  /\.org-switcher-select option\s*\{[\s\S]*background:\s*var\(--bg-elevated\);[\s\S]*color:\s*var\(--text\);/,
  'Organization select options must use explicit themed colors',
)
assert.match(
  orgCssSrc,
  /\.org-create-member-row select option\s*\{[\s\S]*background:\s*var\(--bg-elevated\);[\s\S]*color:\s*var\(--text\);/,
  'Create-org reports-to select options must use explicit themed colors',
)

// ── 9. Self-built role skills are editable and saved as explicit refs ──
assert.match(
  roleInspectorSrc,
  /skill_refs\?:\s*string\[\]/,
  'RoleUpdatePatch must carry explicit skill_refs',
)
assert.match(
  roleInspectorSrc,
  /handleSkillRefsChange/,
  'RoleInspector must persist edited role skill refs',
)
assert.match(
  roleInspectorSrc,
  /One installed skill name per line/,
  'RoleInspector must expose the installed-skill assignment editor',
)

console.log(
  'OrgTab.test.tsx: OK (tabs, marketplaces, skill refs, create-org prompt, select option colors)',
)
