// Login `next` parameter: only same-origin paths, never another login page (open redirect guard).
const PROBE_ORIGIN = "https://nexa.invalid";

export function safeNextPath(raw: string | null | undefined): string | null {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return null;
  // Backslashes, control characters and encoded control characters can be reinterpreted.
  if (/[\\\u0000-\u001f\u007f]/.test(raw) || /%(0[0-9a-f]|1[0-9a-f]|7f|5c)/i.test(raw)) return null;
  let url: URL;
  try {
    url = new URL(raw, PROBE_ORIGIN);
  } catch {
    return null;
  }
  if (url.origin !== PROBE_ORIGIN) return null;
  // Dot segments normalise away: "/.//x", "/%2e%2e//x" and "/a/..//x" all become "//x".
  if (url.pathname.startsWith("//")) return null;
  const path = url.pathname.toLowerCase();
  if (path === "/login" || path.startsWith("/login/")) return null;
  return `${url.pathname}${url.search}${url.hash}`;
}
