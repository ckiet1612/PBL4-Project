import { describe, expect, it } from "vitest";

import { safeNextPath } from "./next";

describe("safeNextPath", () => {
  it("keeps internal paths with query and hash", () => {
    expect(safeNextPath("/t/abc/jobs?state=QUEUED")).toBe("/t/abc/jobs?state=QUEUED");
    expect(safeNextPath("/account/tokens#x")).toBe("/account/tokens#x");
    expect(safeNextPath("/")).toBe("/");
  });

  it("rejects external, protocol-relative and odd forms", () => {
    for (const value of [
      null,
      "",
      "https://evil.example/",
      "//evil.example/x",
      "/\\evil.example",
      "\\\\evil.example",
      "javascript:alert(1)",
      "t/abc/jobs",
      "/%0a//evil.example",
      "/\n/evil.example",
      "/\t/evil.example",
      " /t/abc",
    ]) {
      expect(safeNextPath(value)).toBeNull();
    }
  });

  it("rejects dot-segment forms that normalise to a protocol-relative path (B17-RV01)", () => {
    for (const value of [
      "/.//evil.example",
      "/%2e%2e//evil.example",
      "/%2E%2E//evil.example",
      "/a/..//evil.example",
      "/a/b/../..//evil.example",
      "/././/evil.example?x=1",
    ]) {
      expect(safeNextPath(value)).toBeNull();
    }
  });

  it("never returns a value starting with // for any probed input", () => {
    const segments = ["", ".", "..", "%2e", "%2e%2e", "a", "//", "/", "evil.example"];
    for (const a of segments) {
      for (const b of segments) {
        for (const c of segments) {
          const result = safeNextPath(`/${a}/${b}/${c}`);
          if (result !== null) {
            expect(result.startsWith("/")).toBe(true);
            expect(result.startsWith("//")).toBe(false);
          }
        }
      }
    }
  });

  it("never loops back to the login page", () => {
    expect(safeNextPath("/login")).toBeNull();
    expect(safeNextPath("/login?next=/x")).toBeNull();
    expect(safeNextPath("/LOGIN")).toBeNull();
    expect(safeNextPath("/Login/x")).toBeNull();
  });
});
