import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Separate from vite.config.ts (P01 scaffold, not one of this prompt's DELIVERABLES) so the
// app's dev/build config stays untouched. `npm run test:unit` (vitest) reads this file.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/frontend/unit/setup.ts'],
    include: ['tests/frontend/unit/**/*.test.{ts,tsx}'],
    css: false,
  },
});
