import { describe, expect, it } from "vitest";
import { formatBytes, formatCores, formatTime, localInputToUtc, shortChecksum, shortId } from "./format";

describe("format", () => {
  it("shows CPU in cores", () => {
    expect(formatCores(1000)).toBe("1 core");
    expect(formatCores(1500)).toBe("1,5 core");
    expect(formatCores(250)).toBe("0,25 core");
  });

  it("uses binary units for sizes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1023)).toBe("1023 B");
    expect(formatBytes(1024)).toBe("1 KiB");
    expect(formatBytes(1536)).toBe("1,5 KiB");
    expect(formatBytes(2 * 1024 ** 3)).toBe("2 GiB");
    expect(formatBytes(256 * 1024 ** 2)).toBe("256 MiB");
  });

  it("shortens UUIDv7 by their random tail and checksums by the hex head", () => {
    expect(shortId("0190a000-0000-7000-8000-00000000abcd")).toBe("…0000abcd");
    expect(shortChecksum(`sha256:${"0123456789ab".repeat(5)}abcd`)).toBe("sha256:0123456789ab…");
  });

  it("formats local time with a zone and converts filter input to UTC", () => {
    const text = formatTime("2026-10-01T03:04:05Z", "Asia/Ho_Chi_Minh");
    expect(text).toContain("10:04:05");
    expect(text).toMatch(/GMT\+7|ICT/);
    expect(localInputToUtc("")).toBeNull();
    expect(localInputToUtc("not a date")).toBeNull();
    expect(localInputToUtc("2026-10-01T10:00")).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  });
});
