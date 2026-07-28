import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Kept separate from vite.config.ts so build-only settings (manualChunks,
// dist output) never leak into the test pipeline. Render tests use the
// `*.render.test.*` suffix; everything else stays on the node runner in
// tests/run-unit-tests.mjs, which excludes that suffix.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    css: false,
    setupFiles: ['./tests/vitest.setup.ts'],
    include: ['**/*.render.test.{ts,tsx}', 'lib/phaseHelpers.test.ts'],
    exclude: ['**/node_modules/**', '**/frontend_dist/**', 'game/**'],
  },
})
