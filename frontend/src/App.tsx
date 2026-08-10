/**
 * Control Tower shell.
 *
 * P01 placeholder: it exists so the TypeScript and eslint gates run against real
 * code from day one. The panel layout, the Zustand store and the generated API
 * client land at P14–P16 (CONTEXT.md §5, rows 14–16).
 */
export function App(): React.JSX.Element {
  return (
    <main style={{ padding: 'var(--space-8)' }}>
      <h1 style={{ fontFamily: 'var(--font-display)', margin: 0 }}>AgentDX Control Tower</h1>
      <p style={{ color: 'var(--sage)' }}>
        Scaffold only — no run data yet. See CONTEXT.md §5 for build state.
      </p>
      <p className="numeric" style={{ color: 'var(--sage-dim)' }}>
        Bounded search: absence of findings is not proof of absence.
      </p>
    </main>
  );
}
