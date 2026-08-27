import { describe, expect, it } from 'vitest';

import { BUCKET_LEGEND, fillForBucket } from '../../../src/panels/waterfallBuckets';

describe('fillForBucket (PRD §28.3 four-fill ruling)', () => {
  it('maps null (the real backend\'s current always-null bucket) to the neutral work fill', () => {
    expect(fillForBucket(null)).toBe('work');
  });

  it('maps every real backend bucket name to its documented fill', () => {
    expect(fillForBucket('productive_work')).toBe('work');
    expect(fillForBucket('orchestration')).toBe('work');
    expect(fillForBucket('redundant_work')).toBe('work');
    expect(fillForBucket('blocking_wait')).toBe('blocking_wait');
    expect(fillForBucket('handoff')).toBe('handoff');
    expect(fillForBucket('retry_recovery')).toBe('retry');
  });

  it('falls back to work for an unrecognised bucket name rather than throwing', () => {
    expect(fillForBucket('some_future_bucket')).toBe('work');
  });

  it('legend covers exactly the four §28.3 fills, in the documented glyph order', () => {
    expect(BUCKET_LEGEND.map((l) => l.fill)).toEqual(['work', 'blocking_wait', 'handoff', 'retry']);
    expect(BUCKET_LEGEND.map((l) => l.glyph)).toEqual(['█', '░', '▓', '▒']);
  });
});
