// Server state for one page: load, optional polling (Poller), refresh. Nothing outlives the page.
import { useEffect, useRef, useState } from "react";
import { ApiError, type ClientErrorCode } from "../api/client";
import { Poller, type PollOutcome, type PollSchedule } from "../api/polling";

/** Errors that another identical read cannot fix: stop instead of backing off. */
const DEFINITIVE = new Set<ClientErrorCode>([
  "authentication_required",
  "permission_denied",
  "resource_not_found",
  "invalid_cursor",
  "validation_failed",
]);

/** Read-only schedule for resources loaded once; refresh() still re-reads them. */
const ONCE: PollSchedule = { initialMs: 2000, maxMs: 30_000, factor: 2 };

export interface PolledOptions<T> {
  load(signal: AbortSignal): Promise<T>;
  /** null: load once (errors that may be transient still back off and retry). */
  schedule: PollSchedule | null;
  /** Equal fingerprints = unchanged, so the poller slows down. */
  fingerprint?(data: T): string;
  /** Stop polling (e.g. terminal job). */
  done?(data: T): boolean;
}

export interface Polled<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  refresh(): void;
}

export function usePolled<T>(options: PolledOptions<T>, deps: readonly unknown[]): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const pollerRef = useRef<Poller | null>(null);

  useEffect(() => {
    let previous: string | null = null;
    setData(null);
    setError(null);
    setLoading(true);
    const polling = optionsRef.current.schedule !== null;
    const poller = new Poller({
      schedule: optionsRef.current.schedule ?? ONCE,
      async run(signal): Promise<PollOutcome> {
        const current = optionsRef.current;
        let value: T;
        try {
          value = await current.load(signal);
        } catch (caught) {
          if (caught instanceof ApiError && DEFINITIVE.has(caught.code)) {
            setError(caught);
            setLoading(false);
            return "done";
          }
          throw caught;
        }
        setData(value);
        setError(null);
        setLoading(false);
        if (!polling || current.done?.(value)) return "done";
        const next = current.fingerprint ? current.fingerprint(value) : null;
        const changed = next === null || next !== previous;
        previous = next;
        return changed ? "changed" : "unchanged";
      },
      onError(caught) {
        setError(caught);
        setLoading(false);
      },
    });
    pollerRef.current = poller;
    poller.start();
    return () => {
      poller.stop();
      pollerRef.current = null;
    };
    // The caller's deps say when the resource identity changes (tenant, id, filters, cursor).
  }, deps);

  return {
    data,
    error,
    loading,
    refresh: () => pollerRef.current?.kick(),
  };
}
