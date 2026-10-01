import { describe, expect, it } from "vitest";
import { downloadName, etagMatches, extensionFor, safeFileName, sha256Checksum, TEMPLATE_ARTIFACTS } from "./files";

describe("download file names", () => {
  it("strips path separators, control characters and reserved characters", () => {
    expect(safeFileName("result.json")).toBe("result.json");
    expect(safeFileName("../../etc/passwd")).toBe("passwd");
    expect(safeFileName("..\\..\\windows\\evil.bat")).toBe("evil.bat");
    expect(safeFileName("a\u0000b\u001fc\u007f.txt")).toBe("abc.txt");
    expect(safeFileName('we<i>rd:"na|me?*.json')).toBe("weirdname.json");
    expect(safeFileName("  .hidden.  ")).toBe("hidden");
    expect(safeFileName("..")).toBeNull();
    expect(safeFileName("/")).toBeNull();
    expect(safeFileName("x".repeat(300))).toHaveLength(128);
  });

  it("falls back to the artifact id plus an extension from the media type", () => {
    const id = "0190a000-0000-7000-8000-0000000000c1";
    expect(downloadName(null, id, "application/json")).toBe(`${id}.json`);
    expect(downloadName("../x", id, "application/json")).toBe("x");
    expect(downloadName("", id, "application/vnd.apache.arrow.file")).toBe(`${id}.arrow`);
    expect(extensionFor("application/x-ndjson")).toBe(".jsonl");
    expect(extensionFor("application/vnd.apache.parquet")).toBe(".parquet");
    expect(extensionFor("application/vnd.nexa.cpu-iterative-input+json")).toBe(".json");
    expect(extensionFor(null)).toBe(".bin");
  });
});

describe("checksums", () => {
  it("computes sha256 in the contract format", async () => {
    const checksum = await sha256Checksum(new Blob([new TextEncoder().encode("abc")]));
    expect(checksum).toBe("sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });

  it("compares the strong ETag with the expected checksum", () => {
    const checksum = `sha256:${"a".repeat(64)}`;
    expect(etagMatches(`"${checksum}"`, checksum)).toBe(true);
    expect(etagMatches(`W/"${checksum}"`, checksum)).toBe(false);
    expect(etagMatches(`"sha256:${"b".repeat(64)}"`, checksum)).toBe(false);
    expect(etagMatches(null, checksum)).toBe(false);
  });
});

describe("template artifact requirements (B17-R06)", () => {
  it("matches the adapter descriptors", () => {
    expect(TEMPLATE_ARTIFACTS["cpu-iterative"]).toEqual({
      input: { kind: "INPUT", mediaType: "application/vnd.nexa.cpu-iterative-input+json" },
      model: null,
    });
    expect(TEMPLATE_ARTIFACTS["batch-inference"].model).toEqual({ kind: "MODEL", mediaType: "application/octet-stream" });
  });
});
