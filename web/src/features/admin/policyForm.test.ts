import { describe, expect, it } from "vitest";
import type { TenantPolicy } from "../../api/types";
import { policyDraft, policyUpdate, zeroResourceLimit } from "./policyForm";

const GIB = 2 ** 30;

const BASE: TenantPolicy = {
  tenant_id: "01900000-0000-7000-8000-000000000001",
  version: 4,
  weight: 1,
  outstanding_limit: 100,
  user_outstanding_limit: 20,
  concurrent_attempt_limit: 4,
  user_concurrent_attempt_limit: 2,
  resource_limit: { cpu_millis: 2500, memory_bytes: 4 * GIB, gpu_count: 0 },
  submit_rate_per_second: 0.5,
  submit_burst: 5,
  user_submit_rate_per_second: 0.25,
  user_submit_burst: 3,
  updated_at: "2026-10-01T00:00:00.000Z",
};

describe("tenant policy form", () => {
  it("shows every field in admin units", () => {
    expect(policyDraft(BASE)).toEqual({
      weight: "1",
      outstanding_limit: "100",
      user_outstanding_limit: "20",
      concurrent_attempt_limit: "4",
      user_concurrent_attempt_limit: "2",
      cpu: "2,5",
      memory: "4",
      gpu: "0",
      submit_rate_per_second: "0,5",
      submit_burst: "5",
      user_submit_rate_per_second: "0,25",
      user_submit_burst: "3",
    });
  });

  it("sends nothing when nothing changed", () => {
    const result = policyUpdate(BASE, policyDraft(BASE));
    expect(result).toEqual({ update: {}, errors: {}, changed: false });
  });

  it("sends only the changed fields, accepting comma or dot decimals", () => {
    const draft = { ...policyDraft(BASE), weight: "0.5", user_submit_burst: "4" };
    expect(policyUpdate(BASE, draft)).toEqual({
      update: { weight: 0.5, user_submit_burst: 4 },
      errors: {},
      changed: true,
    });
    expect(policyUpdate(BASE, { ...policyDraft(BASE), submit_rate_per_second: "2,75" }).update).toEqual({
      submit_rate_per_second: 2.75,
    });
  });

  it("an equal value written differently is not a change", () => {
    expect(policyUpdate(BASE, { ...policyDraft(BASE), weight: "1,0", cpu: "2.500" }).changed).toBe(false);
  });

  it("sends the whole resource_limit vector in millicores and bytes when one part changed", () => {
    const draft = { ...policyDraft(BASE), memory: "0,5" };
    expect(policyUpdate(BASE, draft).update).toEqual({
      resource_limit: { cpu_millis: 2500, memory_bytes: GIB / 2, gpu_count: 0 },
    });
  });

  it("marks invalid fields and sends nothing while any field is invalid", () => {
    const draft = {
      ...policyDraft(BASE),
      weight: "0",
      outstanding_limit: "1.5",
      cpu: "1,2345",
      gpu: "-1",
      submit_rate_per_second: "abc",
      submit_burst: "0",
    };
    const result = policyUpdate(BASE, draft);
    expect(result.changed).toBe(true);
    expect(result.update).toEqual({});
    expect(Object.keys(result.errors).sort()).toEqual(
      ["cpu", "gpu", "outstanding_limit", "submit_burst", "submit_rate_per_second", "weight"].sort(),
    );
    expect(result.errors.weight).toBe("Số lớn hơn 0 và không quá 1000");
    expect(result.errors.outstanding_limit).toBe("Số nguyên từ 1 trở lên");
    expect(result.errors.cpu).toBe("Số core, tối đa 3 chữ số thập phân");
  });

  it("warns when the CPU or RAM part of the resource limit is zero", () => {
    expect(zeroResourceLimit({ cpu_millis: 0, memory_bytes: GIB, gpu_count: 0 })).toBe(true);
    expect(zeroResourceLimit({ cpu_millis: 1000, memory_bytes: 0, gpu_count: 0 })).toBe(true);
    expect(zeroResourceLimit({ cpu_millis: 1000, memory_bytes: GIB, gpu_count: 0 })).toBe(false);
  });
});

describe("server numbers round-trip through the form unchanged (B18-RV06)", () => {
  it.each([1 / 3, 2 / 3, 0.1 + 0.2, 1e-7, 1e-12, 123.456789012345, 1000, 5e-324, 1e21])(
    "an untouched %s is not a change and is not sent",
    (value) => {
      const base = { ...BASE, weight: Math.min(value, 1000), submit_rate_per_second: value, user_submit_rate_per_second: value };
      const result = policyUpdate(base, policyDraft(base));
      expect(result.errors).toEqual({});
      expect(result.changed).toBe(false);
      expect(result.update).toEqual({});
    },
  );

  it("editing one field sends only that field, never a rounded weight", () => {
    const base = { ...BASE, weight: 1 / 3 };
    const result = policyUpdate(base, { ...policyDraft(base), user_submit_burst: "9" });
    expect(result.update).toEqual({ user_submit_burst: 9 });
  });
});
