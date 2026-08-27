import { useControlTowerStore } from '../store';
import styles from './EvidenceLink.module.css';

export interface EvidenceLinkProps {
  /** Concrete event `seq` values this number is computed from (I6). Never render a number
   * whose evidence array is empty — the caller must not call this component in that case
   * (an empty array is itself a schema failure per I6, not a softened "no evidence" state). */
  seqs: readonly number[];
  /** Optional span ids this evidence also names, e.g. for a race finding's two writers. */
  spanIds?: readonly string[];
}

/**
 * Renders the `seq` values that justify a number on screen, each clickable — clicking sets
 * the store's selection to that event so any panel reacting to `selectionSlice` can jump to
 * it (PRD §28.2's cross-panel linking; no panel that reacts to this exists yet in this
 * prompt's scope, but the wiring is real and forward-compatible with P16).
 */
export function EvidenceLink({ seqs, spanIds }: EvidenceLinkProps): React.JSX.Element {
  const select = useControlTowerStore((s) => s.select);
  if (seqs.length === 0) {
    // I6: an empty evidence array is a schema failure — render that fact plainly rather than
    // silently showing nothing (§29.8: state what happened).
    return <span className={styles.missing}>no evidence</span>;
  }
  return (
    <span className={styles.wrap}>
      <span className={styles.seqLabel}>seq</span>
      {seqs.map((seq, i) => (
        <button
          key={seq}
          type="button"
          className={`${styles.seq} numeric`}
          onClick={() => select({ kind: 'event', id: String(seq) })}
        >
          {seq}
          {i < seqs.length - 1 ? <span aria-hidden="true">,</span> : null}
        </button>
      ))}
      {spanIds?.map((spanId) => (
        <button
          key={spanId}
          type="button"
          className={`${styles.span} numeric`}
          onClick={() => select({ kind: 'span', id: spanId })}
        >
          {spanId}
        </button>
      ))}
    </span>
  );
}
