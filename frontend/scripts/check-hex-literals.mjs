#!/usr/bin/env node
/**
 * CI gate (AGENTS.md §4 / this prompt's design constraints): "Zero literal hex values in any
 * `.tsx` component — CI-graded, checked via a grep gate." All colour must come from
 * `tokens.css` custom properties (`var(--...)`).
 *
 * This is a second, independent enforcement of the same rule the `no-restricted-syntax` ESLint
 * rule in eslint.config.js already checks — kept as a separate plain-text scan, per the DoD's
 * explicit "the hex-literal grep gate passes" wording, so the check still runs even in a
 * context that only runs `npm run check:hex-literals` and not the full lint suite.
 *
 * **Also scans `.css`/`.module.css` files** (op2-audit-p15.md finding #6): the rule itself
 * ("no literal hex in a component") and `tokens.css`'s own header ("a literal hex value in a
 * component is a defect") never carved out CSS Modules — a component's colours can just as
 * easily be hardcoded in its stylesheet as in its JSX, and this gate previously only ever
 * walked `.tsx` files, a blind spot rather than a deliberate scope decision. `tokens.css`
 * itself is excluded — it is the one file allowed to define the hex values everything else
 * references by name. CSS block comments are stripped before scanning (several `.module.css`
 * files legitimately *cite* a hex value in a comment documenting a WCAG contrast fix — see
 * `Badge.module.css`/`EvidenceLink.module.css`/`SeverityDot.module.css`'s D-58 comments — citing
 * one in prose is not the defect this gate exists to catch; using one as a live property value
 * is).
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = path.join(root, 'src');
const tokensFile = path.join(srcDir, 'tokens.css');

const HEX_RE = /#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b/g;

/** Strips CSS block comments while preserving line numbers (newlines are kept, everything else
 * inside a comment is blanked out) so a hex value cited only in prose never counts as a
 * violation, but line numbers in any reported violation still point at the real source line. */
function stripCssComments(content) {
  return content.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '));
}

/** @type {string[]} */
const tsxFiles = [];
/** @type {string[]} */
const cssFiles = [];
function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      walk(full);
    } else if (entry.endsWith('.tsx')) {
      tsxFiles.push(full);
    } else if (entry.endsWith('.css') && full !== tokensFile) {
      cssFiles.push(full);
    }
  }
}
walk(srcDir);

/** @type {{ file: string; line: number; text: string }[]} */
const violations = [];
for (const file of tsxFiles) {
  const lines = readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    // A `var(--token-name)` reference never itself contains a literal hex value; tokens.css is
    // the one file allowed to define them, and it's excluded (.css, not .tsx) by the walk above.
    const matches = line.match(HEX_RE);
    if (matches) {
      violations.push({ file: path.relative(root, file), line: i + 1, text: line.trim() });
    }
  });
}
for (const file of cssFiles) {
  const raw = readFileSync(file, 'utf8');
  const lines = stripCssComments(raw).split('\n');
  lines.forEach((line, i) => {
    const matches = line.match(HEX_RE);
    if (matches) {
      violations.push({ file: path.relative(root, file), line: i + 1, text: line.trim() });
    }
  });
}

if (violations.length > 0) {
  console.error(
    `check:hex-literals — found ${violations.length} literal hex colour(s) in .tsx/.css files:\n`,
  );
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line}  ${v.text}`);
  }
  console.error('\nAll colour must come from a tokens.css custom property (var(--...)).');
  process.exit(1);
}

console.log(
  `check:hex-literals — clean (${tsxFiles.length} .tsx files + ${cssFiles.length} .css files scanned, 0 literal hex colours).`,
);
