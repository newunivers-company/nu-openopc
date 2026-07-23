import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../frontend_dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          phaser: ['phaser'],
          graph: ['@dagrejs/dagre', '@xyflow/react'],
          markdown: ['react-markdown', 'remark-gfm'],
          dataView: ['@tanstack/react-table', '@tanstack/react-virtual'],
          dragDrop: ['@hello-pangea/dnd'],
        },
      },
    },
  },
})
