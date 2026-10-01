// Old → new retry links. The API only stores new → old (retry_of_job_id) and the source job is
// immutable, so the reverse link is known only for retries made in this tab (B17-R17). Memory only;
// cleared on sign-out and session expiry.
const retries = new Map<string, string[]>();

const keyOf = (tenantId: string, jobId: string) => `${tenantId}/${jobId}`;

export function rememberRetry(tenantId: string, sourceJobId: string, retryJobId: string): void {
  const key = keyOf(tenantId, sourceJobId);
  const known = retries.get(key) ?? [];
  if (!known.includes(retryJobId)) retries.set(key, [...known, retryJobId]);
}

export function retriesOf(tenantId: string, sourceJobId: string): string[] {
  return retries.get(keyOf(tenantId, sourceJobId)) ?? [];
}

export function forgetRetries(): void {
  retries.clear();
}
