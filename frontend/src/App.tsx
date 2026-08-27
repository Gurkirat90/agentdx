/**
 * Control Tower shell (PRD §28.1). Replaces the P01 scaffold now that the store, the
 * generated API client and the routes this prompt owns all exist.
 */
import { RunListRoute } from './routes/RunListRoute';
import { RunRoute } from './routes/RunRoute';
import { ScorecardRoute } from './routes/ScorecardRoute';
import { RouterProvider, useRoute } from './routes/router';

function Routes(): React.JSX.Element {
  const route = useRoute();
  switch (route.name) {
    case 'run-list':
      return <RunListRoute />;
    case 'run':
      return <RunRoute runId={route.runId} />;
    case 'run-scorecard':
      return <ScorecardRoute runId={route.runId} />;
    case 'not-found':
      return (
        <main style={{ padding: 'var(--space-8)' }}>
          <p>No route for &quot;{route.path}&quot;.</p>
        </main>
      );
  }
}

export function App(): React.JSX.Element {
  return (
    <RouterProvider>
      <Routes />
    </RouterProvider>
  );
}
