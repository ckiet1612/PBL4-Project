// Fixed client limits (UX-A03, UX-A10). The server caps every page at 100 items.
export const MAX_PAGE_SIZE = 100;

export const PAGE_SIZE = {
  jobs: 25,
  artifacts: 25,
  sweepChildren: 25,
  tokens: 50,
  events: 100,
  attempts: 100,
  checkpoints: 100,
} as const;

/** Browser uploads hash the whole file first and downloads buffer it in memory. */
export const BROWSER_TRANSFER_LIMIT_BYTES = 256 * 1024 * 1024;

export const REQUEST_TIMEOUT_MS = 30_000;
export const TRANSFER_TIMEOUT_MS = 15 * 60_000;

/** Server bound for control/retry reasons (ControlRequest.reason). */
export const REASON_MAX_LENGTH = 256;

export const SWEEP_MAX_CHILDREN = 100;
