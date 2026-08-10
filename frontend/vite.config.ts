import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The Control Tower is served by FastAPI in production from
// src/agentdx/api/static/ (PRD §39.4, §39.5), so the build output is plain
// static assets and the dev server proxies /api and /ws to the local API on
// 8420 — one origin, no CORS configuration to explain.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8420', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8420', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
