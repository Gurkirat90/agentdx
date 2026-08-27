#!/usr/bin/env node
/**
 * Post-processes the openapi-typescript output at src/api/schema.ts to break a self-referential
 * indexed-access type TypeScript cannot check (TS2502). See CONTEXT.md ruling C-016.
 *
 * openapi-typescript 7.13.0 emits the recursive "any JSON value" union (from the real
 * OpenAPI schema's `JsonValue-Input`/`JsonValue-Output`, whose `anyOf` legitimately contains
 * itself via array items and additionalProperties) spelled as
 * `components["schemas"]["JsonValue-Input"]` inside `components`'s own definition.
 * TypeScript can check a type alias that recurses through its own name, but not one that
 * recurses through a bracketed index into an interface currently being defined — that's TS2502,
 * a checker limitation, not a shape problem. This script re-spells the identical union as two
 * named type aliases (same members, same recursion, just addressable by name) and repoints every
 * reference at them. It invents nothing: run it against a schema that no longer has this shape
 * and the marker/regex guards below fail loudly rather than silently doing nothing.
 *
 * Wired into `npm run generate:api` so schema.ts stays 100% generated — no human ever
 * hand-edits the file directly; this script is the generation pipeline's last step, and it
 * re-derives its patch from openapi-typescript's own output every time it runs.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const schemaPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src/api/schema.ts');
let src = readFileSync(schemaPath, 'utf8');

const marker = 'export interface components {';
if (!src.includes(marker)) {
  throw new Error(
    'patch-schema-jsonvalue: expected `export interface components {` not found — ' +
      'openapi-typescript output shape changed; update this script before trusting its output.',
  );
}

const inputPropRe =
  /"JsonValue-Input": string \| number \| boolean \| components\["schemas"\]\["JsonValue-Input"\]\[\] \| \{\s*\[key: string\]: components\["schemas"\]\["JsonValue-Input"\];\s*\} \| null;/;
const outputPropRe =
  /"JsonValue-Output": string \| number \| boolean \| components\["schemas"\]\["JsonValue-Output"\]\[\] \| \{\s*\[key: string\]: components\["schemas"\]\["JsonValue-Output"\];\s*\} \| null;/;

if (!inputPropRe.test(src) || !outputPropRe.test(src)) {
  throw new Error(
    'patch-schema-jsonvalue: JsonValue-Input/Output no longer match the expected recursive ' +
      'shape — the upstream schema or codegen changed. Re-check whether TS2502 still ' +
      'reproduces before re-applying this patch; do not apply it blind.',
  );
}

src = src.replace(inputPropRe, '"JsonValue-Input": JsonValueInput;');
src = src.replace(outputPropRe, '"JsonValue-Output": JsonValueOutput;');

src = src
  .replaceAll('components["schemas"]["JsonValue-Input"]', 'JsonValueInput')
  .replaceAll("components['schemas']['JsonValue-Input']", 'JsonValueInput')
  .replaceAll('components["schemas"]["JsonValue-Output"]', 'JsonValueOutput')
  .replaceAll("components['schemas']['JsonValue-Output']", 'JsonValueOutput');

const aliasBlock = `/**
 * Named aliases for the (self-referential) JsonValue-Input/Output schemas — patched in by
 * scripts/patch-schema-jsonvalue.mjs, see CONTEXT.md C-016. Same recursive union
 * openapi-typescript generated from the real OpenAPI schema, just addressable by name instead
 * of by bracketed index, which is the part TypeScript can check (TS2502 workaround).
 */
type JsonValueInput = string | number | boolean | JsonValueInput[] | { [key: string]: JsonValueInput } | null;
type JsonValueOutput = string | number | boolean | JsonValueOutput[] | { [key: string]: JsonValueOutput } | null;

`;

src = src.replace(marker, aliasBlock + marker);

writeFileSync(schemaPath, src);
console.log('patch-schema-jsonvalue: patched JsonValue-Input/Output recursive type (TS2502 workaround).');
