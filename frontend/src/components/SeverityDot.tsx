import { cx } from '../lib/cx';
import styles from './SeverityDot.module.css';

export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info';

const SHAPE: Record<Severity, 'dot' | 'triangle'> = {
  critical: 'dot',
  high: 'dot',
  medium: 'triangle',
  low: 'triangle',
  info: 'dot',
};

const TONE_CLASS: Record<Severity, string | undefined> = {
  critical: styles.critical,
  high: styles.high,
  medium: styles.medium,
  low: styles.low,
  info: styles.info,
};

export interface SeverityDotProps {
  severity: Severity;
  /** Renders the severity word beside the shape too — never colour/shape alone (§29.9). */
  showLabel?: boolean;
}

/** §29.6's severity marker: colour AND shape both carry the meaning, so it survives
 * greyscale, colour-blindness and `prefers-reduced-motion` alike. */
export function SeverityDot({ severity, showLabel = false }: SeverityDotProps): React.JSX.Element {
  const shape = SHAPE[severity];
  return (
    <span className={styles.wrap}>
      <svg
        className={cx(styles.mark, TONE_CLASS[severity])}
        width="10"
        height="10"
        viewBox="0 0 10 10"
        role="img"
        aria-label={`${severity} severity`}
      >
        {shape === 'dot' ? (
          <circle cx="5" cy="5" r="4.5" />
        ) : (
          <polygon points="5,0.5 9.5,9 0.5,9" />
        )}
      </svg>
      {showLabel ? <span className={styles.label}>{severity}</span> : null}
    </span>
  );
}
