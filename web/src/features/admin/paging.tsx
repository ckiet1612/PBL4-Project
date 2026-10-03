// Admin lists: filters and cursor on the URL, previous-page trail in memory (as /t/:id/jobs).
// A broken cursor, or a filter change while on a later page, goes back to page one with a notice.
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { isBadCursorError } from "../../api/errors";
import { back, EMPTY_TRAIL, forward, type PageTrail } from "../../components/pageTrail";
import { Pager } from "../../components/Pager";
import { defaultRange, type TimeRange } from "./ranges";

export const BAD_CURSOR_NOTICE = "Vị trí trang không còn hợp lệ, đã quay về trang đầu";
export const FILTER_CHANGED_NOTICE = "Bộ lọc đã đổi, đã quay về trang đầu";

export function useCursorPaging(filterNames: readonly string[]) {
  const [params, setParams] = useSearchParams();
  const cursor = params.get("cursor");
  const filterKey = filterNames.map((name) => `${name}=${params.get(name) ?? ""}`).join("&");
  const [trailState, setTrailState] = useState<{ key: string; trail: PageTrail }>({ key: filterKey, trail: EMPTY_TRAIL });
  const trail = trailState.key === filterKey ? trailState.trail : EMPTY_TRAIL;
  const [notice, setNotice] = useState<string | null>(null);

  const goTo = (target: string | null, nextTrail: PageTrail) => {
    setTrailState({ key: filterKey, trail: nextTrail });
    const next = new URLSearchParams(params);
    if (target) next.set("cursor", target);
    else next.delete("cursor");
    setNotice(null);
    setParams(next);
  };

  /** Several filters at once (a submitted form); null/"" removes the parameter. */
  const setFilters = (values: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [name, value] of Object.entries(values)) {
      if (value) next.set(name, value);
      else next.delete(name);
    }
    next.delete("cursor");
    setNotice(cursor !== null ? FILTER_CHANGED_NOTICE : null);
    setParams(next);
  };

  const clearFilters = () => {
    setNotice(cursor !== null ? FILTER_CHANGED_NOTICE : null);
    setParams(new URLSearchParams());
  };

  /** Call from an effect with the list error: a refused URL cursor resets to page one. */
  const resetIfBadCursor = useCallback(
    (error: unknown) => {
      if (!isBadCursorError(error, cursor)) return;
      setNotice(BAD_CURSOR_NOTICE);
      setTrailState({ key: filterKey, trail: EMPTY_TRAIL });
      const next = new URLSearchParams(params);
      next.delete("cursor");
      setParams(next, { replace: true });
    },
    [cursor, filterKey, params, setParams],
  );

  const pager = (nextCursor: string | null, disabled = false) => (
    <Pager
      hasPrevious={cursor !== null}
      nextCursor={nextCursor}
      disabled={disabled}
      onFirst={() => goTo(null, EMPTY_TRAIL)}
      onPrevious={() => {
        const step = back(trail);
        goTo(step.target, step.trail);
      }}
      onNext={(next) => goTo(next, forward(trail, cursor))}
    />
  );

  return {
    params,
    cursor,
    notice,
    clearNotice: () => setNotice(null),
    setFilters,
    clearFilters,
    resetIfBadCursor,
    /** Errors other than a refused URL cursor (that one is handled by resetIfBadCursor). */
    shownError: (error: unknown) => (error !== null && !isBadCursorError(error, cursor) ? error : null),
    pager,
  };
}

/**
 * The page's time range from the URL. A missing bound is written to the URL as the default
 * (replace, no history entry), so a later cursor, a reload or a shared link keeps the range the
 * page was read with (B18-RV10). The first read already uses the same values.
 */
export function useUrlRange(): TimeRange {
  const [params, setParams] = useSearchParams();
  const [fallback] = useState(() => defaultRange());
  const from = params.get("from");
  const to = params.get("to");
  useEffect(() => {
    if (from !== null && to !== null) return;
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        if (!next.has("from")) next.set("from", fallback.from);
        if (!next.has("to")) next.set("to", fallback.to);
        return next;
      },
      { replace: true },
    );
  }, [from, to, fallback, setParams]);
  return { from: from ?? fallback.from, to: to ?? fallback.to };
}
