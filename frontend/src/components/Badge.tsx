import type { ReactNode } from 'react';

import styles from './Badge.module.css';

export type BadgeTone = 'ok' | 'warn' | 'crit' | 'fault' | 'neutral';

export interface BadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  /** Extra accessible text beyond the visible label, for a state colour alone can't carry
   * (§29.9: colour independence). */
  srPrefix?: string;
}

/** A small status label. All colour comes from `tokens.css` custom properties (AGENTS.md §4)
 * — no literal hex ever appears here. */
export function Badge({ tone = 'neutral', children, srPrefix }: BadgeProps): React.JSX.Element {
  return (
    <span className={`${styles.badge} ${styles[tone]} numeric`}>
      {srPrefix ? <span className={styles.srOnly}>{srPrefix}: </span> : null}
      {children}
    </span>
  );
}
