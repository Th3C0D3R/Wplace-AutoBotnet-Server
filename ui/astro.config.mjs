import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import tailwind from '@astrojs/tailwind';

// https://astro.build/config
export default defineConfig({
  integrations: [react(), tailwind()],
  server: {
    port: 3000,
    host: true
  },
  output: 'static',
  vite: {
    build: {
      rollupOptions: {
        onwarn(warning, warn) {
          // Suppress TypeScript warnings during build
          if (warning.code === 'PLUGIN_WARNING') return;
          warn(warning);
        }
      }
    }
  }
});