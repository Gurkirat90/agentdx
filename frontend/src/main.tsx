import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import './tokens.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('E-UI-001: #root is missing from index.html');
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
