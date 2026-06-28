import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// In dev, proxy the API + media to the Django server (`abv web`, port 8000).
// In prod, Django serves the built `dist/` and everything is same-origin.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/frames': 'http://localhost:8000',
      '/portraits': 'http://localhost:8000',
    },
  },
})
