import { defineConfig } from 'vite';
import { resolve } from 'node:path';

export default defineConfig({
  base: './',
  build: {
    outDir: resolve(import.meta.dirname, '../src/rigm/viewer3d_static'),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: 'avatar-[hash].js',
        chunkFileNames: 'chunk-[name]-[hash].js',
        assetFileNames: '[name]-[hash][extname]',
      },
    },
  },
});
