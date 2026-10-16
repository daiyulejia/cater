import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    manifest: 'workroom-manifest.json',
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: 'command-output',
              test: /[\\/]src[\\/](?:CommandOutputPreview|outputPreview)\.tsx?$/,
              priority: 20,
            },
            {
              name: 'page-recovery',
              test: /[\\/]src[\\/](?:AsyncPanel|pageRecovery|pageReload|pageReloadContext)\.tsx?$/,
              priority: 20,
            },
            {
              name: 'chat-conversation',
              test: /[\\/]src[\\/]ChatConversation\.tsx$/,
              priority: 20,
            },
            {
              name: 'team-state',
              test: /[\\/]src[\\/]team\.ts$/,
              priority: 20,
            },
            {
              name: 'graph-vendor',
              test: /[\\/]node_modules[\\/](?:@xyflow[\\/]|@dagrejs[\\/]|d3-[^\\/]+[\\/]|zustand[\\/])/,
              priority: 30,
            },
            {
              name: 'react-vendor',
              test: /[\\/]node_modules[\\/](?:react|react-dom|scheduler)[\\/]/,
              priority: 40,
            },
            {
              name: 'vendor',
              test: /[\\/]node_modules[\\/]/,
              priority: 10,
            },
          ],
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8766',
      '/tasks': 'http://127.0.0.1:8766',
    },
  },
})
