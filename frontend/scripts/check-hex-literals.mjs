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
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = path.join(root, 'src');

const HEX_RE = /#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b/g;

/** @type {string[]} */
const tsxFiles = [];
function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      walk(full);
    } else if (entry.endsWith('.tsx')) {
      tsxFiles.push(full);
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

if (violations.length > 0) {
  console.error(`check:hex-literals — found ${violations.length} literal hex colour(s) in .tsx files:\n`);
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line}  ${v.text}`);
  }
  console.error('\nAll colour must come from a tokens.css custom property (var(--...)).');
  process.exit(1);
}

console.log(`check:hex-literals — clean (${tsxFiles.length} .tsx files scanned, 0 literal hex colours).`);
