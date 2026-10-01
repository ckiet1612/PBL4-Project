// Form draft → strict JobSpec for the three v1 templates (docs/contracts/openapi.yaml JobSpec).
import type { JobSpec, Template, TemplateId } from "../../api/types";
import { checkBounds, parseDecimal, parseParameter, type Parsed, type ParameterValue } from "./params";

export interface SubmitDraft {
  inputArtifactId: string | null;
  modelArtifactId: string | null;
  params: Record<string, string>;
  cores: string;
  memoryGib: string;
  priority: string;
  runtimeLimit: string;
  checkpointInterval: string;
}

/** Keys: template, input, model, param.<name>, cpu, memory, priority, runtime, checkpoint. */
export type FieldErrors = Record<string, string>;

const GIB = 1024 ** 3;
const MIN_MEMORY_BYTES = 64 * 1024 ** 2;
const MIN_CPU_MILLIS = 100;
const MAX_CPU_MILLIS = 100_000_000;

export const SUPPORTED_TEMPLATES: readonly TemplateId[] = ["cpu-iterative", "pytorch-cifar10-cnn", "batch-inference"];

export function isSupportedTemplate(templateId: string): templateId is TemplateId {
  return (SUPPORTED_TEMPLATES as readonly string[]).includes(templateId);
}

export function coresToMillis(raw: string): Parsed<number> {
  const cores = parseDecimal(raw);
  if (cores === null) return { error: "Phải là số" };
  const millis = cores * 1000;
  if (Math.abs(millis - Math.round(millis)) > 1e-9) return { error: "Tối đa 3 chữ số thập phân" };
  const value = Math.round(millis);
  if (value < MIN_CPU_MILLIS) return { error: "Tối thiểu 0,1 core" };
  if (value > MAX_CPU_MILLIS) return { error: "Tối đa 100.000 core" };
  return { value };
}

export function gibToBytes(raw: string): Parsed<number> {
  const gib = parseDecimal(raw);
  if (gib === null) return { error: "Phải là số" };
  const value = Math.round(gib * GIB);
  if (value < MIN_MEMORY_BYTES) return { error: "Tối thiểu 0,0625 GiB (64 MiB)" };
  if (!Number.isSafeInteger(value)) return { error: "Số quá lớn" };
  return { value };
}

function integerField(raw: string, minimum: number, maximum: number): Parsed<number> {
  const text = raw.trim();
  if (text === "") return { error: "Bắt buộc nhập" };
  if (!/^-?\d+$/.test(text)) return { error: "Phải là số nguyên" };
  const value = Number(text);
  const bound = checkBounds(value, minimum, maximum);
  return bound ? { error: bound } : { value };
}

export interface BuiltSpec {
  spec?: JobSpec;
  errors: FieldErrors;
}

/** Returns a complete spec only when every field is valid; never a partial spec. */
export function buildSpec(template: Template, draft: SubmitDraft): BuiltSpec {
  const errors: FieldErrors = {};
  if (!isSupportedTemplate(template.template_id)) {
    return { errors: { template: "Web UI chưa hỗ trợ template này; dùng CLI" } };
  }
  const templateId = template.template_id;
  if (!draft.inputArtifactId) errors.input = "Chọn dữ liệu đầu vào";
  if (templateId === "batch-inference" && !draft.modelArtifactId) errors.model = "Chọn model";

  const parameters: Record<string, ParameterValue> = {};
  for (const def of template.parameter_schema) {
    const parsed = parseParameter(def, draft.params[def.name] ?? "");
    if (parsed.error !== undefined) errors[`param.${def.name}`] = parsed.error;
    else if (parsed.value !== undefined) parameters[def.name] = parsed.value;
  }

  const fields = {
    cpu: coresToMillis(draft.cores),
    memory: gibToBytes(draft.memoryGib),
    priority: integerField(draft.priority, 0, 2),
    runtime: integerField(draft.runtimeLimit, 1, 300),
    checkpoint: integerField(draft.checkpointInterval, 5, 60),
  };
  for (const [key, parsed] of Object.entries(fields)) {
    if (parsed.error !== undefined) errors[key] = parsed.error;
  }
  if (Object.keys(errors).length > 0) return { errors };

  const base = {
    template_version: template.version,
    input_artifact_id: draft.inputArtifactId as string,
    resources: { cpu_millis: fields.cpu.value as number, memory_bytes: fields.memory.value as number, gpu_count: 0 },
    priority: fields.priority.value as number,
    runtime_limit_seconds: fields.runtime.value as number,
    checkpoint_interval_seconds: fields.checkpoint.value as number,
  };
  // Parameter names and types were checked against the template schema above; the server
  // re-validates the exact shape per template.
  let spec: JobSpec;
  if (templateId === "cpu-iterative") {
    spec = { ...base, template_id: templateId, parameters: parameters as never };
  } else if (templateId === "pytorch-cifar10-cnn") {
    spec = { ...base, template_id: templateId, parameters: parameters as never };
  } else {
    spec = {
      ...base,
      template_id: templateId,
      model_artifact_id: draft.modelArtifactId as string,
      parameters: parameters as never,
    };
  }
  return { spec, errors };
}
