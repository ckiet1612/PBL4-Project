import { describe, expect, it } from "vitest";
import { forgetRetries, rememberRetry, retriesOf } from "./retryLinks";

describe("retry links (in-memory, this browser session only)", () => {
  it("returns the retries created from a source job, per tenant, without duplicates", () => {
    forgetRetries();
    rememberRetry("t1", "old", "new1");
    rememberRetry("t1", "old", "new1");
    rememberRetry("t1", "old", "new2");
    expect(retriesOf("t1", "old")).toEqual(["new1", "new2"]);
    expect(retriesOf("t2", "old")).toEqual([]);
    expect(retriesOf("t1", "new1")).toEqual([]);
  });

  it("forgets everything on sign-out so another user never sees them", () => {
    rememberRetry("t1", "old", "new1");
    forgetRetries();
    expect(retriesOf("t1", "old")).toEqual([]);
  });
});
