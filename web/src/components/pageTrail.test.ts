import { describe, expect, it } from "vitest";
import { back, EMPTY_TRAIL, forward } from "./pageTrail";

describe("cursor trail", () => {
  it("walks forward and back through cursor pages", () => {
    let trail = forward(EMPTY_TRAIL, null);
    trail = forward(trail, "cursor-page-2-aaaa");
    expect(trail.previous).toEqual([null, "cursor-page-2-aaaa"]);
    const step = back(trail);
    expect(step.target).toBe("cursor-page-2-aaaa");
    const first = back(step.trail);
    expect(first.target).toBeNull();
    expect(first.trail).toEqual(EMPTY_TRAIL);
  });

  it("goes to the first page when nothing is remembered (reload on a later page)", () => {
    expect(back(EMPTY_TRAIL)).toEqual({ trail: EMPTY_TRAIL, target: null });
  });
});
