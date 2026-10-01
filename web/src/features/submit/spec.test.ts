import { describe, expect, it } from "vitest";
import type { ParameterDefinition, Template } from "../../api/types";
import { DEFAULT_DRAFT } from "./defaults";
import { parameterHelp, parseParameter } from "./params";
import { buildSpec, coresToMillis, gibToBytes, type SubmitDraft } from "./spec";
import { parseSweepValues, sweepChildCount } from "./sweep";

const def = (over: Partial<ParameterDefinition>): ParameterDefinition => ({
  name: "iterations",
  type: "INTEGER",
  required: true,
  minimum: 1,
  maximum: 10,
  default: null,
  ...over,
});

function template(template_id: string, parameter_schema: ParameterDefinition[]): Template {
  return {
    template_id,
    version: 1,
    display_name: template_id,
    adapter_id: "a",
    adapter_version: "1.0.0",
    image_digest: `sha256:${"0".repeat(64)}`,
    enabled: true,
    checkpointable: true,
    restart_safe: true,
    allowed_devices: ["CPU"],
    capability_requirement: {} as Template["capability_requirement"],
    parameter_schema,
  };
}

const CPU = template("cpu-iterative", [
  def({ name: "iterations", maximum: 1_000_000_000 }),
  def({ name: "seed", minimum: 0, maximum: 2147483647 }),
  def({ name: "modulus", minimum: 2, maximum: 2147483647 }),
]);
const INFERENCE = template("batch-inference", [
  def({ name: "chunk_size", maximum: 100000 }),
  def({ name: "batch_size", maximum: 4096 }),
  def({ name: "output_format", type: "ENUM", minimum: null, maximum: null, enum_values: ["JSONL", "PARQUET"] }),
]);
const INPUT = "0190a000-0000-7000-8000-0000000000c1";
const MODEL = "0190a000-0000-7000-8000-0000000000c2";

const draft = (over: Partial<SubmitDraft>): SubmitDraft => ({
  ...DEFAULT_DRAFT,
  inputArtifactId: INPUT,
  modelArtifactId: null,
  params: { iterations: "100", seed: "7", modulus: "97" },
  ...over,
});

describe("parameters", () => {
  it("validates type and bounds inline", () => {
    expect(parseParameter(def({}), "")).toEqual({ error: "Bắt buộc nhập" });
    expect(parseParameter(def({}), "1.5")).toEqual({ error: "Phải là số nguyên" });
    expect(parseParameter(def({}), "0")).toEqual({ error: "Tối thiểu 1" });
    expect(parseParameter(def({}), "11")).toEqual({ error: "Tối đa 10" });
    expect(parseParameter(def({}), " 5 ")).toEqual({ value: 5 });
    expect(parseParameter(def({ type: "NUMBER", minimum: null, maximum: 1 }), "0,01")).toEqual({ value: 0.01 });
    expect(parseParameter(def({ type: "NUMBER" }), "abc")).toEqual({ error: "Phải là số" });
    expect(parseParameter(def({ type: "ENUM", enum_values: ["A"] }), "B")).toEqual({ error: "Chọn một giá trị trong danh sách" });
    expect(parseParameter(def({ type: "BOOLEAN" }), "true")).toEqual({ value: true });
    expect(parseParameter(def({ required: false }), "")).toEqual({ value: undefined });
  });

  it("helper text states type, limits and unit", () => {
    expect(parameterHelp(def({ maximum: 1_000_000_000 }))).toBe("Số nguyên, từ 1 đến 1.000.000.000");
    expect(parameterHelp(def({ type: "NUMBER", minimum: null, maximum: 1 }))).toBe("Số thực, tối đa 1");
    expect(parameterHelp(def({ unit: "giây" }))).toBe("Số nguyên, từ 1 đến 10 (giây)");
  });
});

describe("units", () => {
  it("converts cores and GiB exactly", () => {
    expect(coresToMillis("1")).toEqual({ value: 1000 });
    expect(coresToMillis("1,5")).toEqual({ value: 1500 });
    expect(coresToMillis("0.25")).toEqual({ value: 250 });
    expect(coresToMillis("0.05")).toEqual({ error: "Tối thiểu 0,1 core" });
    expect(coresToMillis("1.0005")).toEqual({ error: "Tối đa 3 chữ số thập phân" });
    expect(gibToBytes("1")).toEqual({ value: 1073741824 });
    expect(gibToBytes("0,5")).toEqual({ value: 536870912 });
    expect(gibToBytes("0.01")).toEqual({ error: "Tối thiểu 0,0625 GiB (64 MiB)" });
    expect(gibToBytes("x")).toEqual({ error: "Phải là số" });
  });
});

describe("JobSpec builder", () => {
  it("builds a strict cpu-iterative spec with every required field", () => {
    const result = buildSpec(CPU, draft({}));
    expect(result.errors).toEqual({});
    expect(result.spec).toEqual({
      template_id: "cpu-iterative",
      template_version: 1,
      input_artifact_id: INPUT,
      resources: { cpu_millis: 1000, memory_bytes: 1073741824, gpu_count: 0 },
      priority: 1,
      runtime_limit_seconds: 300,
      checkpoint_interval_seconds: 30,
      parameters: { iterations: 100, seed: 7, modulus: 97 },
    });
  });

  it("requires the model for batch-inference and places it in the spec", () => {
    const params = { chunk_size: "10", batch_size: "4", output_format: "JSONL" };
    expect(buildSpec(INFERENCE, draft({ params })).errors).toEqual({ model: "Chọn model" });
    const spec = buildSpec(INFERENCE, draft({ params, modelArtifactId: MODEL })).spec;
    expect(spec).toMatchObject({ template_id: "batch-inference", model_artifact_id: MODEL, parameters: { output_format: "JSONL" } });
  });

  it("reports field errors and never returns a partial spec", () => {
    const result = buildSpec(
      CPU,
      draft({ inputArtifactId: null, params: { iterations: "", seed: "7", modulus: "1" }, priority: "3", runtimeLimit: "0" }),
    );
    expect(result.spec).toBeUndefined();
    expect(result.errors).toEqual({
      input: "Chọn dữ liệu đầu vào",
      "param.iterations": "Bắt buộc nhập",
      "param.modulus": "Tối thiểu 2",
      priority: "Tối đa 2",
      runtime: "Tối thiểu 1",
    });
  });

  it("refuses templates the UI has no spec shape for", () => {
    expect(buildSpec(template("new-template", []), draft({})).errors).toEqual({
      template: "Web UI chưa hỗ trợ template này; dùng CLI",
    });
  });
});

describe("sweep", () => {
  const lr = def({ name: "learning_rate", type: "NUMBER", minimum: null, maximum: 1 });
  it("parses comma separated values with the parameter rules", () => {
    expect(parseSweepValues(lr, "0.1; 0.01")).toEqual({ values: [0.1, 0.01] });
    expect(parseSweepValues(def({ name: "epochs" }), "1, 2, 3")).toEqual({ values: [1, 2, 3] });
    expect(parseSweepValues(def({}), "1, 1")).toEqual({ error: "Giá trị bị trùng: 1" });
    expect(parseSweepValues(def({}), "1, 20")).toEqual({ error: "20: Tối đa 10" });
    expect(parseSweepValues(def({}), " ")).toEqual({ error: "Nhập ít nhất một giá trị" });
  });

  it("counts children as the product and caps at 100", () => {
    expect(sweepChildCount([[1, 2], [1, 2, 3]])).toBe(6);
    expect(sweepChildCount([])).toBe(0);
    expect(sweepChildCount([Array.from({ length: 11 }, (_, i) => i), Array.from({ length: 10 }, (_, i) => i)])).toBe(110);
  });
});
