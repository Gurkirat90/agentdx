/** @type {import('tailwindcss').Config} */
// Tailwind is a layout and spacing utility here, not a colour system: every
// colour maps straight to a CSS custom property from tokens.css (PRD §24.6,
// §29.1), so there is exactly one place a colour is defined.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        navy: {
          700: 'var(--navy-700)',
          800: 'var(--navy-800)',
          900: 'var(--navy-900)',
          950: 'var(--navy-950)',
        },
        cream: 'var(--cream)',
        sage: { DEFAULT: 'var(--sage)', dim: 'var(--sage-dim)' },
        clay: 'var(--clay)',
        ok: 'var(--ok)',
        warn: 'var(--warn)',
        crit: 'var(--crit)',
        fault: 'var(--fault)',
      },
      fontFamily: {
        display: 'var(--font-display)',
        body: 'var(--font-body)',
        mono: 'var(--font-mono)',
      },
      borderRadius: {
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
      },
    },
  },
  plugins: [],
};
