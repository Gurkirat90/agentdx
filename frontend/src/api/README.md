# api/

`schema.ts` is GENERATED from `docs/openapi.json` via `npm run generate:api` (openapi-typescript)
— never hand-edit it, regenerate it when the backend's OpenAPI schema changes. `client.ts` is a
thin, schema-driven `openapi-fetch` client (no per-endpoint hand-written methods). `scorecard.ts`
narrows the intentionally-untyped `ScorecardResponse` into a documented `ScorecardPayload` shape
— see that file's header comment for why and its sourcing. Built at P15.
