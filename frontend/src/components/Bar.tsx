import styles from './Bar.module.css';

export interface BarProps {
  /** 0–1. Values outside that range are clamped, never silently rendered past the track. */
  value: number;
  label: string;
  /** Formatted value text shown beside the bar, e.g. "62%" — the caller computes it so this
   * component never invents precision the data doesn't have. */
  valueText: string;
  tone?: 'ok' | 'warn' | 'crit' | 'neutral';
}

/** A single labelled horizontal proportion bar — cache-reuse rate, parallelism, and similar
 * 0–1 metrics in the Scorecard panel. Not a chart library: one `<div>` fill, driven by
 * `tokens.css` custom properties only. */
export function Bar({ value, label, valueText, tone = 'neutral' }: BarProps): React.JSX.Element {
  const clamped = Math.min(1, Math.max(0, value));
  return (
    <div className={styles.row}>
      <span className={styles.label}>{label}</span>
      <div
        className={styles.track}
        role="meter"
        aria-valuenow={Math.round(clamped * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div className={`${styles.fill} ${styles[tone]}`} style={{ width: `${clamped * 100}%` }} />
      </div>
      <span className={`${styles.value} numeric`}>{valueText}</span>
    </div>
  );
}
