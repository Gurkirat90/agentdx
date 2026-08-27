/**
 * `src/agentdx/analysis/baseline.py`'s `_BUCKET_LABELS`, transcribed verbatim (PRD §17.4's
 * printed labels) — the same six keys `ScorecardBucket.bucket` can carry.
 */
export const BUCKET_LABELS: Record<string, string> = {
  retry_recovery: 'retry recovery',
  redundant_work: 'redundant tool calls',
  orchestration: 'orchestration',
  handoff: 'handoff latency',
  blocking_wait: 'blocking wait',
  unattributed: 'unattributed',
};

export function bucketLabel(bucket: string): string {
  return BUCKET_LABELS[bucket] ?? bucket;
}
