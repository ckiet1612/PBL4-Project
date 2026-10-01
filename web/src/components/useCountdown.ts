// Retry-After countdown (login and submit after 429). The wall clock only gates a button.
import { useCallback, useEffect, useState } from "react";

export function useCountdown(): { remaining: number; start(seconds: number | null): void } {
  const [until, setUntil] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (until === null) return;
    const timer = setInterval(() => {
      const current = Date.now();
      setNow(current);
      if (current >= until) setUntil(null);
    }, 250);
    return () => clearInterval(timer);
  }, [until]);

  const start = useCallback((seconds: number | null) => {
    const current = Date.now();
    setNow(current);
    setUntil(current + Math.max(1, seconds ?? 1) * 1000);
  }, []);

  const remaining = until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000));
  return { remaining, start };
}
