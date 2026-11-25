import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `process` is provided by Node at config-eval time; declared here to avoid a
// hard dependency on @types/node (only used by the dev-server proxy below).
declare const process: { env: Record<string, string | undefined> }
const BACKEND = process.env.VITE_BACKEND ?? 'http://localhost:8080'

export default defineConfig({
  plugins: [react()],
  resolve: {
    // Mirrors tsconfig's "@/*" → "src/*" so shadcn imports resolve at build time.
    alias: { '@': new URL('./src', import.meta.url).pathname },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: true,
        // SSE-friendly
        configure: (proxy) => {
          proxy.on('proxyReq', () => {})
        },
      },
    },
  },
})
