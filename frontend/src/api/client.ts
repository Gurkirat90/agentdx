/**
 * Typed API client — thin wrapper around `openapi-fetch`, driven entirely by the
 * GENERATED `./schema.ts` (openapi-typescript output from `docs/openapi.json`, PRD §26).
 *
 * `frontend/src/api/README.md` requires this directory be generated, never hand-written.
 * `openapi-typescript` generates the *types* (`schema.ts`, regenerate via
 * `npm run generate:api` whenever the backend's `docs/openapi.json` changes); the few lines
 * below are fixed, schema-driven boilerplate — one `createClient<paths>()` call and one path
 * constant — not a per-endpoint hand-written surface. No endpoint, field name or response
 * shape is typed by hand anywhere in this file.
 */
import createClient from 'openapi-fetch';

import type { paths } from './schema';

const BASE_URL = typeof window !== 'undefined' ? '' : 'http://127.0.0.1:8420';

export const api = createClient<paths>({ baseUrl: BASE_URL });

export type { components, operations } from './schema';
