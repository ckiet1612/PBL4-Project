// Tenant policy form (docs/web-ui.md A7): every field in core/GiB/decimal, PATCH only what changed.
// Bounds mirror TenantPolicyUpdate in the contract; the server still decides (400/409 shown as-is).
import type { ResourceCapacityVector, TenantPolicy, TenantPolicyUpdate } from "../../api/types";
import { bytesToGibText, millisToCoresText, numberText, parseCores, parseCount, parseDecimal, parseGib } from "./units";

const POSITIVE_INTEGERS = [
  "outstanding_limit",
  "user_outstanding_limit",
  "concurrent_attempt_limit",
  "user_concurrent_attempt_limit",
  "submit_burst",
  "user_submit_burst",
] as const;
const RATES = ["submit_rate_per_second", "user_submit_rate_per_second"] as const;

export type PolicyField = "weight" | (typeof POSITIVE_INTEGERS)[number] | (typeof RATES)[number] | "cpu" | "memory" | "gpu";
export type PolicyDraft = Record<PolicyField, string>;

export const POLICY_FIELD_LABELS: Record<PolicyField, string> = {
  weight: "Trọng số",
  outstanding_limit: "Job tồn đọng tối đa (tenant)",
  user_outstanding_limit: "Job tồn đọng tối đa (mỗi user)",
  concurrent_attempt_limit: "Lần chạy đồng thời tối đa (tenant)",
  user_concurrent_attempt_limit: "Lần chạy đồng thời tối đa (mỗi user)",
  cpu: "CPU (core)",
  memory: "RAM (GiB)",
  gpu: "GPU",
  submit_rate_per_second: "Tốc độ gửi job (tenant, job/giây)",
  submit_burst: "Gửi dồn tối đa (tenant)",
  user_submit_rate_per_second: "Tốc độ gửi job (mỗi user, job/giây)",
  user_submit_burst: "Gửi dồn tối đa (mỗi user)",
};

const MAX_CPU_MILLIS = 100_000_000;
const MAX_GPU = 64;
const MAX_WEIGHT = 1000;

export function policyDraft(policy: TenantPolicy): PolicyDraft {
  return {
    weight: numberText(policy.weight),
    outstanding_limit: String(policy.outstanding_limit),
    user_outstanding_limit: String(policy.user_outstanding_limit),
    concurrent_attempt_limit: String(policy.concurrent_attempt_limit),
    user_concurrent_attempt_limit: String(policy.user_concurrent_attempt_limit),
    cpu: millisToCoresText(policy.resource_limit.cpu_millis),
    memory: bytesToGibText(policy.resource_limit.memory_bytes),
    gpu: String(policy.resource_limit.gpu_count),
    submit_rate_per_second: numberText(policy.submit_rate_per_second),
    submit_burst: String(policy.submit_burst),
    user_submit_rate_per_second: numberText(policy.user_submit_rate_per_second),
    user_submit_burst: String(policy.user_submit_burst),
  };
}

export interface PolicyDiff {
  /** Empty while any field is invalid. */
  update: TenantPolicyUpdate;
  errors: Partial<Record<PolicyField, string>>;
  /** Some field differs from the server value (or cannot be read). */
  changed: boolean;
}

export function policyUpdate(base: TenantPolicy, draft: PolicyDraft): PolicyDiff {
  const errors: Partial<Record<PolicyField, string>> = {};
  const update: TenantPolicyUpdate = {};

  const weight = parseDecimal(draft.weight);
  if (weight === null || weight <= 0 || weight > MAX_WEIGHT) errors.weight = "Số lớn hơn 0 và không quá 1000";
  else if (weight !== base.weight) update.weight = weight;

  for (const field of POSITIVE_INTEGERS) {
    const value = parseCount(draft[field]);
    if (value === null || value < 1) errors[field] = "Số nguyên từ 1 trở lên";
    else if (value !== base[field]) update[field] = value;
  }
  for (const field of RATES) {
    const value = parseDecimal(draft[field]);
    if (value === null || value <= 0) errors[field] = "Số lớn hơn 0";
    else if (value !== base[field]) update[field] = value;
  }

  const cpu = parseCores(draft.cpu);
  if (cpu === null || cpu > MAX_CPU_MILLIS) errors.cpu = "Số core, tối đa 3 chữ số thập phân";
  const memory = parseGib(draft.memory);
  if (memory === null) errors.memory = "Số GiB không âm";
  const gpu = parseCount(draft.gpu);
  if (gpu === null || gpu > MAX_GPU) errors.gpu = "Số nguyên từ 0 đến 64";
  if (cpu !== null && memory !== null && gpu !== null) {
    const limit = base.resource_limit;
    if (cpu !== limit.cpu_millis || memory !== limit.memory_bytes || gpu !== limit.gpu_count) {
      update.resource_limit = { cpu_millis: cpu, memory_bytes: memory, gpu_count: gpu };
    }
  }

  const invalid = Object.keys(errors).length > 0;
  return { update: invalid ? {} : update, errors, changed: invalid || Object.keys(update).length > 0 };
}

/** A tenant whose CPU or RAM limit is 0 can never get an allocation. */
export function zeroResourceLimit(limit: ResourceCapacityVector): boolean {
  return limit.cpu_millis === 0 || limit.memory_bytes === 0;
}
