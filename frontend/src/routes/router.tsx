/**
 * A minimal, dependency-free client-side router. PRD §28.1 names four routes; the locked
 * stack (CONTEXT.md §3: "React 18 + Vite + TypeScript + React Flow + Zustand + Tailwind +
 * visx") names no routing library, and adding one is a new dependency requiring an ADR
 * (AGENTS.md §2) for what four `history`-API-driven patterns don't need. This file is that
 * judgment call, made explicit rather than silently reached for react-router.
 */
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

export type Route =
  | { name: 'run-list' }
  | { name: 'run'; runId: string }
  | { name: 'run-scorecard'; runId: string }
  | { name: 'not-found'; path: string };

function parsePath(path: string): Route {
  const segments = path.split('/').filter(Boolean);
  if (segments.length === 0) return { name: 'run-list' };
  if (segments[0] === 'runs' && segments.length === 2 && segments[1]) {
    return { name: 'run', runId: segments[1] };
  }
  if (segments[0] === 'runs' && segments.length === 3 && segments[1] && segments[2] === 'scorecard') {
    return { name: 'run-scorecard', runId: segments[1] };
  }
  return { name: 'not-found', path };
}

const RouteContext = createContext<Route>({ name: 'run-list' });

export function RouterProvider({ children }: { children: ReactNode }): React.JSX.Element {
  const [route, setRoute] = useState<Route>(() => parsePath(window.location.pathname));

  useEffect(() => {
    const onPopState = () => setRoute(parsePath(window.location.pathname));
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  return <RouteContext.Provider value={route}>{children}</RouteContext.Provider>;
}

export function useRoute(): Route {
  return useContext(RouteContext);
}

export function navigate(path: string): void {
  window.history.pushState(null, '', path);
  window.dispatchEvent(new PopStateEvent('popstate'));
}

export function Link({
  to,
  children,
  className,
}: {
  to: string;
  children: ReactNode;
  className?: string | undefined;
}): React.JSX.Element {
  return (
    <a
      href={to}
      className={className}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // let the browser handle it
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
