import { defineConfig, devices } from '@playwright/test';

// `npm run dev` (vite) proxies /api to a live backend that isn't running in this test
// environment — every spec stubs /api/** itself via `page.route` (tests/frontend/e2e/
// stubApi.ts) with real backend-computed fixture JSON, so the proxy target is never reached.
export default defineConfig({
  testDir: 'tests/frontend/e2e',
  fullyParallel: false,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:5199',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    // --host 127.0.0.1 pins the dev server to IPv4 explicitly. Vite's default `localhost`
    // binding resolves per-OS/Node-version — on some setups it lands on the IPv6 loopback
    // (::1) instead, which leaves this config's IPv4 health check (and baseURL above) polling
    // a socket nothing is listening on until it times out, even though Vite itself started
    // fine. Pinning removes the ambiguity rather than guessing at DNS resolution order.
    command: 'npm run dev -- --port 5199 --strictPort --host 127.0.0.1',
    url: 'http://127.0.0.1:5199',
    reuseExistingServer: false,
    timeout: 60_000,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // PLAYWRIGHT_CHROMIUM_PATH is set only in this build's own cloud sandbox, where
        // @playwright/test's version can drift ahead of the browser revision pre-installed
        // at /opt/pw-browsers and needs pinning. Anywhere else (a contributor's machine, CI)
        // this is unset and Playwright falls back to its own normally-installed browser
        // (`npx playwright install chromium`) — never assume this build's sandbox path.
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
          : {},
      },
    },
  ],
});
