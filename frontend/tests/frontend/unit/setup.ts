import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

import '@testing-library/jest-dom/vitest';

// OP-3 repair (2026-08-24): @testing-library/react does not auto-register DOM cleanup for
// Vitest the way it does for Jest (this project does not set `test.globals: true`, so the
// library never sees a global afterEach to hook). Without this, a render() in one test leaks
// its DOM into the next test in the same file — found while adding
// panelErrorBoundary.test.tsx, whose "multiple elements with role alert" failures were this
// leak, not a real assertion failure.
afterEach(cleanup);
