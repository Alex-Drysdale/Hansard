import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api/hansard': {
        target: 'https://hansard-api.parliament.uk',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/hansard/, ''),
        secure: false,
      },
      '/api/members': {
        target: 'https://members-api.parliament.uk/api',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/members/, ''),
        secure: false,
      },
    },
  },
})
