import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

function matchAny(id, patterns) {
  return patterns.some(pattern => id.includes(pattern))
}

function resolveElementPlusChunk(id) {
  return matchAny(id, ['element-plus', '@element-plus']) ? 'vendor-element-plus' : null
}

const elementPlusResolvers = ElementPlusResolver({
  importStyle: 'css',
  directives: true,
})

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      resolvers: [...elementPlusResolvers],
      imports: ['vue', 'vue-router', 'pinia'],
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [...elementPlusResolvers],
      dts: 'src/components.d.ts',
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return

          const elementPlusChunk = resolveElementPlusChunk(id)
          if (elementPlusChunk) return elementPlusChunk

          if (id.includes('echarts') || id.includes('zrender') || id.includes('vue-echarts')) {
            return 'vendor-echarts'
          }
          if (id.includes('vue') || id.includes('vue-router') || id.includes('pinia')) {
            return 'vendor-vue'
          }
          if (id.includes('/xlsx/')) {
            return 'vendor-xlsx'
          }
          if (id.includes('/axios/')) {
            return 'vendor-axios'
          }
          if (id.includes('/dayjs/')) {
            return 'vendor-dayjs'
          }
          return 'vendor-misc'
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
      },
    },
  },
})
