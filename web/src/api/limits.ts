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
  /** Admin tables (UX-A18). */
  admin: 25,
  /** First page of allocations per state, summed next to the worker capacity. */
  allocations: 100,
  /** Tenant/user lists loaded only to show names and to pick a member. */
  adminNames: 100,
  /** /admin overview: first workers and the latest recovery events. */
  overviewWorkers: 10,
  overviewRecovery: 10,
} as const;

/** Browser uploads hash the whole file first and downloads buffer it in memory. */
export const BROWSER_TRANSFER_LIMIT_BYTES = 256 * 1024 * 1024;

export const REQUEST_TIMEOUT_MS = 30_000;
export const TRANSFER_TIMEOUT_MS = 15 * 60_000;

/** Server bound for control/retry and worker action reasons (ControlRequest/AdminReasonRequest). */
export const REASON_MAX_LENGTH = 256;

export const SWEEP_MAX_CHILDREN = 100;

/** Global outstanding limit bounds (GlobalPolicyUpdate). */
export const GLOBAL_LIMIT_MIN = 1;
export const GLOBAL_LIMIT_MAX = 1_000_000;
