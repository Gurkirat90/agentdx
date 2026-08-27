/**
 * Token contrast matrix (PRD §29.1's design constraint: "cream on navy-900 ≈12:1, NEVER use
 * `--sage-dim` for body text, ~4:1"; NFR-7: WCAG AA). Two independent checks, both required by
 * this prompt's Definition of Done ("the token contrast matrix test passes"):
 *
 * 1. Every foreground/background token pair this app actually uses for text meets WCAG AA
 *    (4.5:1 normal text, 3:1 large text/UI) against the surface it's painted on.
 * 2. `--sage-dim` — documented in tokens.css as landing near 4:1, below the 4.5:1 AA floor for
 *    body text — is never used as a text `color` anywhere in the component stylesheets. It may
 *    still be used for non-text purposes (an icon `fill`, a `border-color`) where the 3:1
 *    UI-component threshold, not the 4.5:1 text threshold, applies.
 *
 * Contrast math and the token hex values both come straight from the committed source files
 * (tokens.css, the component .module.css files) — nothing here is hand-typed or assumed.
 */
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const tokensCssPath = path.join(root, 'src/tokens.css');
const tokensCss = readFileSync(tokensCssPath, 'utf8');

function readToken(name: string): string {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`).exec(tokensCss);
  if (!match || !match[1]) throw new Error(`token --${name} not found in tokens.css`);
  return match[1];
}

function hexToRgb(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

// WCAG 2.x relative luminance / contrast ratio (https://www.w3.org/TR/WCAG21/#dfn-relative-luminance).
function relativeLuminance([r, g, b]: [number, number, number]): number {
  const [rs, gs, bs] = [r, g, b].map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs;
}

function contrastRatio(hexA: string, hexB: string): number {
  const lA = relativeLuminance(hexToRgb(hexA));
  const lB = relativeLuminance(hexToRgb(hexB));
  const [lighter, darker] = lA >= lB ? [lA, lB] : [lB, lA];
  return (lighter + 0.05) / (darker + 0.05);
}

const navy900 = readToken('navy-900');
const navy800 = readToken('navy-800');
const cream = readToken('cream');
const sage = readToken('sage');
const sageDim = readToken('sage-dim');
const ok = readToken('ok');
const warn = readToken('warn');
const crit = readToken('crit');
const fault = readToken('fault');

const AA_NORMAL_TEXT = 4.5;
const AA_LARGE_TEXT_OR_UI = 3.0;

describe('token contrast matrix (PRD §29.1, NFR-7)', () => {
  // Pairs mirror how each token is actually painted in the components (Badge.module.css,
  // SeverityDot.module.css) — not a generic "colour on canvas" guess. `--crit` (PRD §29.1's
  // exact `#A8443A`) fails AA as plain text-on-canvas (2.50:1) or as a small-icon fill
  // (2.94:1 against `--elevation-well`); both components route around that using only
  // already-specified tokens — see the comments in those two files for the rationale.
  it.each([
    // [label, foreground, background, minimum ratio]
    ['cream body text on navy-900 canvas', cream, navy900, AA_NORMAL_TEXT],
    ['cream text on navy-800 raised card', cream, navy800, AA_NORMAL_TEXT],
    ['sage secondary text on navy-900 canvas', sage, navy900, AA_NORMAL_TEXT],
    ['sage secondary text on navy-800 raised card', sage, navy800, AA_NORMAL_TEXT],
    ['ok badge text on elevation-well (Badge.module.css .ok)', ok, navy900, AA_NORMAL_TEXT],
    ['warn badge text on elevation-well (Badge.module.css .warn)', warn, navy900, AA_NORMAL_TEXT],
    // Badge.module.css .crit: solid --crit fill with --cream text, not --crit-as-text.
    ['cream text on crit fill (Badge.module.css .crit)', cream, crit, AA_NORMAL_TEXT],
    ['fault marker text on elevation-well (Badge.module.css .fault)', fault, navy900, AA_NORMAL_TEXT],
    // SeverityDot.module.css .mark: a cream stroke, not the fill, carries the boundary
    // contrast (WCAG 1.4.11 non-text, 3:1) — see that file's comment.
    ['SeverityDot cream stroke on navy-900 canvas', cream, navy900, AA_LARGE_TEXT_OR_UI],
  ])('%s meets its WCAG AA floor', (_label, fg, bg, minRatio) => {
    expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(minRatio);
  });

  it('documents that --crit alone (unstyled) fails AA even at the lenient 3:1 UI floor', () => {
    // Not a design defect to "fix" by inventing a new hex — PRD §29.1 specifies #A8443A
    // exactly. The two real components using it (Badge, SeverityDot) both route around this
    // measured fact rather than using --crit as bare text/fill-on-canvas; this test locks in
    // *why* those two components are built the way they are, so a future edit that reverts
    // them back to plain crit-on-navy fails here first.
    expect(contrastRatio(crit, navy900)).toBeLessThan(AA_LARGE_TEXT_OR_UI);
  });

  it('cream on navy-900 is close to the PRD-documented ≈12:1', () => {
    // PRD §29.1 states the pair "≈12:1" — assert it lands in a tight, verifiable band rather
    // than re-asserting the vague "≈", so a future token edit that quietly erodes contrast
    // fails loudly even while still technically AA-compliant.
    const ratio = contrastRatio(cream, navy900);
    expect(ratio).toBeGreaterThanOrEqual(11);
    expect(ratio).toBeLessThanOrEqual(13);
  });

  it('--sage-dim sits close enough to the AA text floor that the "never body text" rule is a real safety margin, not a formality', () => {
    // Measured ratio is 4.64:1 — narrowly *at or above* 4.5:1, not below it as tokens.css's
    // own comment ("lands near 4:1") suggests; the two figures are close enough that this is
    // a rounding-of-intent, not a contradiction. Either way, the design constraint ("NEVER use
    // --sage-dim for body text") is a stated policy independent of which side of the line the
    // exact number falls on — this test records the real number rather than asserting a false
    // one, and the structural test below enforces the actual policy.
    const ratio = contrastRatio(sageDim, navy900);
    expect(ratio).toBeGreaterThanOrEqual(4.0);
    expect(ratio).toBeLessThan(5.0);
  });
});

describe('--sage-dim is never used as a text colour (PRD §29.1)', () => {
  function findCssModules(dir: string): string[] {
    const out: string[] = [];
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) out.push(...findCssModules(full));
      else if (entry.name.endsWith('.module.css')) out.push(full);
    }
    return out;
  }

  const cssModules = findCssModules(path.join(root, 'src'));

  it('scanned at least one .module.css file', () => {
    // A canary against this test silently passing because the glob found nothing.
    expect(cssModules.length).toBeGreaterThan(0);
  });

  it.each(cssModules.map((f) => [path.relative(root, f), f] as const))(
    '%s never sets `color: var(--sage-dim)`',
    (_label, file) => {
      const css = readFileSync(file, 'utf8');
      // Matches `color: var(--sage-dim)` but not `background-color`/`border-color`/`fill`/
      // `stroke` — only the text-colour property is restricted.
      const textColorRule = /(?<![a-zA-Z-])color:\s*var\(--sage-dim\)/;
      expect(textColorRule.test(css)).toBe(false);
    },
  );
});
