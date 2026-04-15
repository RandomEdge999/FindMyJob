import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: resolve(__dirname, '../src/findmyjob/web/frontend_dist'),
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
  },
})
