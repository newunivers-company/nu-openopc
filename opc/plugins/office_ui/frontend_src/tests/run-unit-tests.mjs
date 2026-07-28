import { readdirSync } from 'node:fs'
import { dirname, extname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const testsDir = dirname(fileURLToPath(import.meta.url))
const root = resolve(testsDir, '..')
const vitestFiles = new Set(['lib/phaseHelpers.test.ts'])

function collect(directory) {
  const files = []
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === 'frontend_dist') continue
    const path = join(directory, entry.name)
    if (entry.isDirectory()) {
      files.push(...collect(path))
      continue
    }
    const extension = extname(entry.name)
    if (
      (extension === '.ts' || extension === '.tsx') &&
      entry.name.includes('.test.') &&
      !entry.name.includes('.render.test.')
    ) {
      files.push(path)
    }
  }
  return files.sort()
}

const unitFiles = collect(root).filter((path) => {
  const local = relative(root, path).replaceAll('\\', '/')
  return !vitestFiles.has(local)
})

for (const path of unitFiles) {
  const local = relative(root, path)
  process.stdout.write(`\n[frontend unit] ${local}\n`)
  const result = spawnSync(
    process.execPath,
    ['--import', 'tsx', path],
    { cwd: root, env: process.env, stdio: 'inherit' },
  )
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status ?? 1)
}

process.stdout.write(`\n${unitFiles.length} frontend unit scripts passed.\n`)
