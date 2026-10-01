// Keyset pagination only moves forward; "previous" replays remembered cursors (memory only).
export interface PageTrail {
  /** Cursors of the pages before the current one; null = first page. */
  previous: (string | null)[];
}

export const EMPTY_TRAIL: PageTrail = { previous: [] };

export function forward(trail: PageTrail, current: string | null): PageTrail {
  return { previous: [...trail.previous, current] };
}

export function back(trail: PageTrail): { trail: PageTrail; target: string | null } {
  if (trail.previous.length === 0) return { trail: EMPTY_TRAIL, target: null };
  return { trail: { previous: trail.previous.slice(0, -1) }, target: trail.previous[trail.previous.length - 1] };
}
