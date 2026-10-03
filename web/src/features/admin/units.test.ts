import { describe, expect, it } from "vitest";
import {
  bytesToGibText,
  millisToCoresText,
  numberText,
  parseCores,
  parseCount,
  parseDecimal,
  parseGib,
} from "./units";

describe("parseDecimal (UX-A21)", () => {
  it("accepts a comma or a dot as the decimal separator", () => {
    expect(parseDecimal("0,5")).toBe(0.5);
    expect(parseDecimal("0.5")).toBe(0.5);
    expect(parseDecimal(" 12 ")).toBe(12);
    expect(parseDecimal("1,25")).toBe(1.25);
  });

  it.each(["", " ", "1.2.3", "1,2,3", "-1", "+1", "1e3", "abc", ",5", "5,", "1 000", "Infinity", "NaN"])(
    "rejects %j",
    (text) => {
      expect(parseDecimal(text)).toBeNull();
    },
  );
});

describe("unit conversion", () => {
  it("cores → millicores with at most three decimals, exactly", () => {
    expect(parseCores("1,5")).toBe(1500);
    expect(parseCores("0.001")).toBe(1);
    expect(parseCores("2")).toBe(2000);
    expect(parseCores("0,3")).toBe(300);
    expect(parseCores("4.105")).toBe(4105);
    expect(parseCores("0,0001")).toBeNull();
    expect(parseCores("x")).toBeNull();
  });

  it("GiB → bytes, rounded to a byte", () => {
    expect(parseGib("1")).toBe(2 ** 30);
    expect(parseGib("0,5")).toBe(2 ** 29);
    expect(parseGib("1.25")).toBe(1.25 * 2 ** 30);
    expect(parseGib("0,1")).toBe(Math.round(0.1 * 2 ** 30));
    expect(parseGib("1e3")).toBeNull();
  });

  it("counts are non-negative integers", () => {
    expect(parseCount("0")).toBe(0);
    expect(parseCount("12")).toBe(12);
    expect(parseCount("1,5")).toBeNull();
    expect(parseCount("-1")).toBeNull();
    expect(parseCount("")).toBeNull();
  });

  it("formats server values for editing with a comma and no grouping", () => {
    expect(millisToCoresText(1500)).toBe("1,5");
    expect(millisToCoresText(100_000_000)).toBe("100000");
    expect(bytesToGibText(2 ** 30)).toBe("1");
    expect(bytesToGibText(3 * 2 ** 29)).toBe("1,5");
    expect(numberText(0.25)).toBe("0,25");
    expect(numberText(1000)).toBe("1000");
    expect(numberText(1e-7)).toBe("0,0000001");
  });

  it("round-trips what it displays", () => {
    for (const millis of [0, 1, 999, 1500, 64_000]) expect(parseCores(millisToCoresText(millis))).toBe(millis);
    for (const bytes of [0, 1, 1000, 2 ** 30, 3 * 2 ** 29, 512 * 2 ** 20, 64 * 2 ** 40 + 7]) expect(parseGib(bytesToGibText(bytes))).toBe(bytes);
  });
});
