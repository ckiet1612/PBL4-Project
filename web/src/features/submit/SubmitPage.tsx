// /t/:tenantId/jobs/new — 1 Template → 2 Dữ liệu → 3 Tham số → 4 Tài nguyên → 5 Nâng cao (sweep).
// One confirmed submit = one Idempotency-Key; the form keeps its values on any error.
import { useEffect, useId, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { isApiError } from "../../api/errors";
import { IntentTracker, withInProgressRetry } from "../../api/idempotency";
import { SWEEP_MAX_CHILDREN } from "../../api/limits";
import type { ParameterDefinition, Template, TrainingJobSpec } from "../../api/types";
import { useTenant } from "../../auth/guards";
import { useApi } from "../../auth/session";
import { Breadcrumb, EmptyState } from "../../components/bits";
import { ErrorPanel } from "../../components/ErrorPanel";
import { useCountdown } from "../../components/useCountdown";
import { usePolled } from "../../components/usePolled";
import { TEMPLATE_ARTIFACTS } from "../data/files";
import { ArtifactPicker } from "./ArtifactPicker";
import { DEFAULT_DRAFT } from "./defaults";
import { initialParameterValue, parameterHelp, type ParameterValue } from "./params";
import { buildSpec, isSupportedTemplate, type FieldErrors, type SubmitDraft } from "./spec";
import { parseSweepValues, SWEEP_MAX_DIMENSIONS, sweepChildCount } from "./sweep";

const SWEEP_TEMPLATE = "pytorch-cifar10-cnn";

interface SweepRow {
  key: number;
  name: string;
  raw: string;
}

const ADVANCED_FIELDS = new Set(["priority", "runtime", "checkpoint", "sweep"]);

function fieldLabel(key: string): string {
  const fixed: Record<string, string> = {
    template: "Template",
    input: "Dữ liệu đầu vào",
    model: "Model",
    cpu: "CPU",
    memory: "RAM",
    priority: "Priority",
    runtime: "Giới hạn thời gian",
    checkpoint: "Chu kỳ checkpoint",
    sweep: "Sweep",
  };
  if (key.startsWith("param.")) return `Tham số ${key.slice(6)}`;
  if (key.startsWith("sweep.")) return "Sweep";
  return fixed[key] ?? key;
}

function templateKey(template: Template): string {
  return `${template.template_id}@${template.version}`;
}

function initialParams(template: Template | null): Record<string, string> {
  const params: Record<string, string> = {};
  for (const def of template?.parameter_schema ?? []) params[def.name] = initialParameterValue(def);
  return params;
}

/** Server errors go to the group they belong to when the code says so; otherwise to the form top. */
function errorGroup(error: unknown): "data" | "resources" | "top" {
  if (isApiError(error, "quota_exceeded", "infeasible_request")) return "resources";
  if (isApiError(error, "resource_not_found")) return "data";
  return "top";
}

export function SubmitPage() {
  const { tenantId } = useTenant();
  const api = useApi();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const presetInput = params.get("input");
  const presetTemplate = params.get("template");
  const templates = usePolled({ load: async (signal) => (await api.listTemplates(tenantId, signal)).data, schedule: null }, [
    tenantId,
  ]);

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<SubmitDraft>({
    ...DEFAULT_DRAFT,
    inputArtifactId: presetInput,
    modelArtifactId: null,
    params: {},
  });
  const [touched, setTouched] = useState<Set<string>>(new Set());
  const [showAll, setShowAll] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [sweepOn, setSweepOn] = useState(false);
  const [sweepRows, setSweepRows] = useState<SweepRow[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<unknown>(null);
  const tracker = useRef(new IntentTracker());
  const inFlight = useRef(false);
  const nextRowKey = useRef(1);
  const summaryRef = useRef<HTMLDivElement>(null);
  const countdown = useCountdown();
  const ids = {
    input: useId(),
    model: useId(),
    cpu: useId(),
    memory: useId(),
    priority: useId(),
    runtime: useId(),
    checkpoint: useId(),
    sweepAdd: useId(),
  };

  const list = templates.data;
  const selected = useMemo(() => list?.find((t) => templateKey(t) === selectedKey) ?? null, [list, selectedKey]);

  // First load: ?template= if supported, else the first supported template.
  useEffect(() => {
    if (!list || selectedKey !== null) return;
    const supported = list.filter((t) => t.enabled && isSupportedTemplate(t.template_id));
    const initial = supported.find((t) => t.template_id === presetTemplate) ?? supported[0];
    if (initial) {
      setSelectedKey(templateKey(initial));
      setDraft((current) => ({ ...current, params: initialParams(initial) }));
    }
  }, [list, selectedKey, presetTemplate]);

  const chooseTemplate = (template: Template) => {
    if (selected && templateKey(template) === templateKey(selected)) return;
    const previous = selected && isSupportedTemplate(selected.template_id) ? TEMPLATE_ARTIFACTS[selected.template_id] : null;
    const next = isSupportedTemplate(template.template_id) ? TEMPLATE_ARTIFACTS[template.template_id] : null;
    const sameInput =
      previous !== null &&
      next !== null &&
      previous.input.kind === next.input.kind &&
      previous.input.mediaType === next.input.mediaType;
    setSelectedKey(templateKey(template));
    setDraft((current) => ({
      ...current,
      inputArtifactId: sameInput ? current.inputArtifactId : null,
      modelArtifactId: next?.model ? current.modelArtifactId : null,
      params: initialParams(template),
    }));
    setSweepOn(false);
    setSweepRows([]);
    setTouched(new Set());
    setShowAll(false);
    setServerError(null);
  };

  const sweepAllowed = selected?.template_id === SWEEP_TEMPLATE;
  const sweepActive = sweepAllowed && sweepOn;
  const defsByName = new Map((selected?.parameter_schema ?? []).map((def) => [def.name, def]));
  const sweepParsed = sweepRows.map((row) => {
    const def = defsByName.get(row.name);
    return def ? parseSweepValues(def, row.raw) : { error: "Chọn tham số" };
  });
  const sweptNames = new Set(sweepActive ? sweepRows.map((row) => row.name) : []);
  const childCount = sweepActive
    ? sweepChildCount(sweepParsed.map((parsed) => (parsed.values ? parsed.values : [])))
    : 0;

  // Base value of a swept parameter = its first sweep value (the server replaces it per child).
  const effectiveParams = { ...draft.params };
  if (sweepActive) {
    sweepRows.forEach((row, index) => {
      const values = sweepParsed[index].values;
      if (values && values.length > 0) effectiveParams[row.name] = String(values[0]);
    });
  }
  const effectiveDraft: SubmitDraft = sweepActive ? { ...draft, params: effectiveParams } : draft;

  const built = selected ? buildSpec(selected, effectiveDraft) : { errors: { template: "Chọn template" } as FieldErrors };
  const errors: FieldErrors = { ...built.errors };
  if (sweepActive) {
    if (sweepRows.length === 0) errors.sweep = "Thêm ít nhất một tham số để sweep";
    sweepParsed.forEach((parsed, index) => {
      if (parsed.error !== undefined) errors[`sweep.${sweepRows[index].key}`] = parsed.error;
    });
    if (childCount > SWEEP_MAX_CHILDREN) errors.sweep = `Tối đa ${SWEEP_MAX_CHILDREN} job con; hiện có ${childCount}`;
  }
  const visibleError = (key: string) => (showAll || touched.has(key) ? errors[key] : undefined);
  const touch = (key: string) =>
    setTouched((current) => (current.has(key) ? current : new Set(current).add(key)));

  const setField = (field: keyof Omit<SubmitDraft, "params">, value: string | null) =>
    setDraft((current) => ({ ...current, [field]: value }));
  const setParam = (name: string, value: string) =>
    setDraft((current) => ({ ...current, params: { ...current.params, [name]: value } }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (inFlight.current || countdown.remaining > 0 || !selected) return;
    if (Object.keys(errors).length > 0 || !built.spec) {
      setShowAll(true);
      if (Object.keys(errors).some((key) => ADVANCED_FIELDS.has(key.split(".")[0]))) setAdvancedOpen(true);
      requestAnimationFrame(() => summaryRef.current?.focus());
      return;
    }
    inFlight.current = true;
    setSubmitting(true);
    setServerError(null);
    const spec = built.spec;
    const dimensions = sweepActive
      ? sweepRows.map((row, index) => ({ name: row.name, values: sweepParsed[index].values as ParameterValue[] }))
      : null;
    const key = tracker.current.keyFor(JSON.stringify({ spec, dimensions }));
    try {
      if (dimensions) {
        const response = await withInProgressRetry(() =>
          api.submitSweep(tenantId, { base_spec: spec as TrainingJobSpec, dimensions }, { idempotencyKey: key }),
        );
        tracker.current.settle();
        navigate(`/t/${tenantId}/sweeps/${response.data.sweep_id}`, {
          state: { flash: `Đã gửi sweep: ${response.data.accepted_count} job được nhận, ${response.data.rejected_count} bị từ chối.` },
        });
      } else {
        const response = await withInProgressRetry(() => api.submitJob(tenantId, spec, { idempotencyKey: key }));
        tracker.current.settle();
        navigate(`/t/${tenantId}/jobs/${response.data.job_id}`, { state: { flash: "Đã nhận job" } });
      }
    } catch (caught) {
      tracker.current.settle(caught);
      setServerError(caught);
      if (isApiError(caught, "rate_limited")) countdown.start(caught.retryAfterSeconds);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  };

  const crumbs = [{ label: "Jobs", to: `/t/${tenantId}/jobs` }, { label: "Tạo job" }];
  if (templates.data === null) {
    return (
      <section className="page page-narrow">
        <Breadcrumb items={crumbs} />
        <h1>Tạo job</h1>
        {templates.error !== null ? (
          <ErrorPanel
            error={templates.error}
            onRetry={templates.refresh}
            title={isApiError(templates.error, "permission_denied") ? "Không có quyền tạo job trong tenant này" : undefined}
          />
        ) : (
          <p>Đang tải template…</p>
        )}
      </section>
    );
  }
  const enabled = templates.data.filter((t) => t.enabled);
  if (enabled.length === 0) {
    return (
      <section className="page page-narrow">
        <Breadcrumb items={crumbs} />
        <h1>Tạo job</h1>
        <EmptyState title="Chưa có template nào được bật. Liên hệ quản trị viên." />
      </section>
    );
  }

  const artifacts = selected && isSupportedTemplate(selected.template_id) ? TEMPLATE_ARTIFACTS[selected.template_id] : null;
  const group = serverError !== null ? errorGroup(serverError) : null;
  const serverPanel = (
    <ErrorPanel
      error={serverError}
      title={
        isApiError(serverError, "permission_denied")
          ? "Không có quyền tạo job trong tenant này"
          : isApiError(serverError, "state_conflict")
            ? "Hệ thống đang tạm ngừng nhận job mới"
            : undefined
      }
    />
  );
  const errorList = showAll ? Object.entries(errors) : [];
  const submitLabel = submitting
    ? "Đang gửi…"
    : countdown.remaining > 0
      ? `Thử lại sau ${countdown.remaining} giây`
      : sweepActive
        ? `Gửi sweep (${childCount} job)`
        : "Gửi job";

  return (
    <section className="page page-narrow">
      <Breadcrumb items={crumbs} />
      <h1>Tạo job</h1>
      <form className="submit-form" onSubmit={submit} noValidate>
        {errorList.length > 0 && (
          <div className="error-summary" role="alert" tabIndex={-1} ref={summaryRef}>
            <p className="error-title">Còn {errorList.length} lỗi cần sửa trước khi gửi</p>
            <ul>
              {errorList.map(([key, message]) => (
                <li key={key}>
                  {fieldLabel(key)}: {message}
                </li>
              ))}
            </ul>
          </div>
        )}
        {group === "top" && serverPanel}

        <fieldset className="form-group">
          <legend>1. Template</legend>
          <div className="choice-list">
            {enabled.map((template) => {
              const supported = isSupportedTemplate(template.template_id);
              return (
                <label key={templateKey(template)} className={supported ? undefined : "disabled"}>
                  <input
                    type="radio"
                    name="template"
                    checked={selectedKey === templateKey(template)}
                    disabled={!supported || submitting}
                    onChange={() => chooseTemplate(template)}
                  />
                  <span>
                    <strong>{template.display_name}</strong> · v{template.version} ·{" "}
                    {template.allowed_devices.join("/")}
                    {template.checkpointable && " · Hỗ trợ tạm dừng/tiếp tục"}
                    {!supported && <span className="muted"> — Web UI chưa hỗ trợ template này; dùng CLI</span>}
                  </span>
                </label>
              );
            })}
          </div>
        </fieldset>

        {selected && artifacts && (
          <>
            <fieldset className="form-group">
              <legend>2. Dữ liệu đầu vào</legend>
              {group === "data" && serverPanel}
              <ArtifactPicker
                tenantId={tenantId}
                label="Dữ liệu đầu vào"
                fieldId={ids.input}
                requirement={artifacts.input}
                value={draft.inputArtifactId}
                onChange={(value) => {
                  setField("inputArtifactId", value);
                  touch("input");
                }}
                error={visibleError("input")}
              />
              {artifacts.model && (
                <ArtifactPicker
                  tenantId={tenantId}
                  label="Model"
                  fieldId={ids.model}
                  requirement={artifacts.model}
                  value={draft.modelArtifactId}
                  onChange={(value) => {
                    setField("modelArtifactId", value);
                    touch("model");
                  }}
                  error={visibleError("model")}
                />
              )}
            </fieldset>

            <fieldset className="form-group">
              <legend>3. Tham số</legend>
              {selected.parameter_schema.length === 0 && <p className="muted">Template này không có tham số.</p>}
              {selected.parameter_schema.map((def) => (
                <ParameterField
                  key={def.name}
                  def={def}
                  value={draft.params[def.name] ?? ""}
                  swept={sweptNames.has(def.name)}
                  error={visibleError(`param.${def.name}`)}
                  onChange={(value) => setParam(def.name, value)}
                  onBlur={() => touch(`param.${def.name}`)}
                />
              ))}
            </fieldset>

            <fieldset className="form-group">
              <legend>4. Tài nguyên</legend>
              {group === "resources" && serverPanel}
              <TextField
                id={ids.cpu}
                label="CPU (core)"
                value={draft.cores}
                help="Ví dụ 1 hoặc 0,5. Tối thiểu 0,1 core."
                error={visibleError("cpu")}
                inputMode="decimal"
                onChange={(value) => setField("cores", value)}
                onBlur={() => touch("cpu")}
              />
              <TextField
                id={ids.memory}
                label="RAM (GiB)"
                value={draft.memoryGib}
                help="Ví dụ 1 hoặc 0,5. Tối thiểu 0,0625 GiB (64 MiB)."
                error={visibleError("memory")}
                inputMode="decimal"
                onChange={(value) => setField("memoryGib", value)}
                onBlur={() => touch("memory")}
              />
            </fieldset>

            <details
              className="form-group advanced"
              open={advancedOpen}
              onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
            >
              <summary>5. Nâng cao</summary>
              <TextField
                id={ids.priority}
                label="Priority"
                value={draft.priority}
                help="0, 1 hoặc 2. Mặc định 1."
                error={visibleError("priority")}
                inputMode="numeric"
                onChange={(value) => setField("priority", value)}
                onBlur={() => touch("priority")}
              />
              <TextField
                id={ids.runtime}
                label="Giới hạn thời gian (giây)"
                value={draft.runtimeLimit}
                help="Từ 1 đến 300 giây."
                error={visibleError("runtime")}
                inputMode="numeric"
                onChange={(value) => setField("runtimeLimit", value)}
                onBlur={() => touch("runtime")}
              />
              <TextField
                id={ids.checkpoint}
                label="Chu kỳ checkpoint (giây)"
                value={draft.checkpointInterval}
                help="Từ 5 đến 60 giây."
                error={visibleError("checkpoint")}
                inputMode="numeric"
                onChange={(value) => setField("checkpointInterval", value)}
                onBlur={() => touch("checkpoint")}
              />
              {sweepAllowed && (
                <SweepEditor
                  on={sweepOn}
                  onToggle={(on) => {
                    setSweepOn(on);
                    if (on && sweepRows.length === 0) {
                      const first = selected.parameter_schema[0]?.name ?? "";
                      setSweepRows([{ key: nextRowKey.current++, name: first, raw: "" }]);
                    }
                  }}
                  defs={selected.parameter_schema}
                  rows={sweepRows}
                  errors={errors}
                  showAll={showAll}
                  addId={ids.sweepAdd}
                  childCount={childCount}
                  onRows={setSweepRows}
                  newKey={() => nextRowKey.current++}
                />
              )}
            </details>
          </>
        )}

        <div className="actions form-actions">
          <Link className="button" to={`/t/${tenantId}/jobs`}>
            Hủy
          </Link>
          <button
            type="submit"
            className="primary"
            disabled={submitting || countdown.remaining > 0 || (sweepActive && childCount > SWEEP_MAX_CHILDREN)}
          >
            {submitLabel}
          </button>
        </div>
      </form>
    </section>
  );
}

interface TextFieldProps {
  id: string;
  label: string;
  value: string;
  help: string;
  error?: string;
  inputMode?: "decimal" | "numeric" | "text";
  disabled?: boolean;
  onChange(value: string): void;
  onBlur(): void;
}

function TextField({ id, label, value, help, error, inputMode = "text", disabled, onChange, onBlur }: TextFieldProps) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value}
        inputMode={inputMode}
        disabled={disabled}
        aria-invalid={error !== undefined}
        aria-describedby={`${id}-help`}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
      />
      <p id={`${id}-help`} className={error ? "field-error" : "help"}>
        {error ?? help}
      </p>
    </div>
  );
}

function ParameterField({
  def,
  value,
  swept,
  error,
  onChange,
  onBlur,
}: {
  def: ParameterDefinition;
  value: string;
  swept: boolean;
  error?: string;
  onChange(value: string): void;
  onBlur(): void;
}) {
  const id = useId();
  const label = `${def.name}${def.required ? "" : " (không bắt buộc)"}`;
  if (swept) {
    return (
      <div className="field">
        <span className="label">{def.name}</span>
        <p className="help">Giá trị lấy từ sweep; giá trị đầu tiên dùng cho spec gốc.</p>
      </div>
    );
  }
  if (def.type === "ENUM" || def.type === "BOOLEAN") {
    const options = def.type === "ENUM" ? (def.enum_values ?? []).map((v) => [v, v]) : [["true", "Có"], ["false", "Không"]];
    return (
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <select
          id={id}
          value={value}
          aria-invalid={error !== undefined}
          aria-describedby={`${id}-help`}
          onChange={(event) => onChange(event.target.value)}
          onBlur={onBlur}
        >
          <option value="">Chọn</option>
          {options.map(([optionValue, text]) => (
            <option key={optionValue} value={optionValue}>
              {text}
            </option>
          ))}
        </select>
        <p id={`${id}-help`} className={error ? "field-error" : "help"}>
          {error ?? parameterHelp(def)}
        </p>
      </div>
    );
  }
  return (
    <TextField
      id={id}
      label={label}
      value={value}
      help={parameterHelp(def)}
      error={error}
      inputMode={def.type === "INTEGER" ? "numeric" : def.type === "NUMBER" ? "decimal" : "text"}
      onChange={onChange}
      onBlur={onBlur}
    />
  );
}

function SweepEditor({
  on,
  onToggle,
  defs,
  rows,
  errors,
  showAll,
  addId,
  childCount,
  onRows,
  newKey,
}: {
  on: boolean;
  onToggle(on: boolean): void;
  defs: ParameterDefinition[];
  rows: SweepRow[];
  errors: FieldErrors;
  showAll: boolean;
  addId: string;
  childCount: number;
  onRows(rows: SweepRow[]): void;
  newKey(): number;
}) {
  const used = new Set(rows.map((row) => row.name));
  const free = defs.filter((def) => !used.has(def.name));
  const update = (key: number, patch: Partial<SweepRow>) =>
    onRows(rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  return (
    <fieldset className="sweep-editor">
      <legend>Sweep tham số</legend>
      <label className="checkbox">
        <input type="checkbox" checked={on} onChange={(event) => onToggle(event.target.checked)} />
        Tạo nhiều job với các tổ hợp giá trị tham số
      </label>
      {on && (
        <>
          <p className="help">
            Nhập các giá trị cách nhau bởi dấu chấm phẩy (0,1; 0,01) hoặc dấu phẩy (1, 2, 3). Số job con = tích số giá
            trị của các tham số, tối đa {SWEEP_MAX_CHILDREN}.
          </p>
          {rows.map((row) => {
            const error = showAll || row.raw !== "" ? errors[`sweep.${row.key}`] : undefined;
            const selectId = `${addId}-${row.key}-name`;
            const valuesId = `${addId}-${row.key}-values`;
            return (
              <div key={row.key} className="sweep-row">
                <div className="field">
                  <label htmlFor={selectId}>Tham số</label>
                  <select id={selectId} value={row.name} onChange={(event) => update(row.key, { name: event.target.value })}>
                    {defs
                      .filter((def) => def.name === row.name || !used.has(def.name))
                      .map((def) => (
                        <option key={def.name} value={def.name}>
                          {def.name}
                        </option>
                      ))}
                  </select>
                </div>
                <div className="field grow">
                  <label htmlFor={valuesId}>Giá trị</label>
                  <input
                    id={valuesId}
                    value={row.raw}
                    aria-invalid={error !== undefined}
                    aria-describedby={`${valuesId}-help`}
                    onChange={(event) => update(row.key, { raw: event.target.value })}
                  />
                  <p id={`${valuesId}-help`} className={error ? "field-error" : "help"}>
                    {error ?? "Ví dụ: 1; 2; 3"}
                  </p>
                </div>
                <button
                  type="button"
                  className="button-link"
                  onClick={() => onRows(rows.filter((r) => r.key !== row.key))}
                  aria-label={`Bỏ tham số ${row.name} khỏi sweep`}
                >
                  Bỏ
                </button>
              </div>
            );
          })}
          {free.length > 0 && rows.length < SWEEP_MAX_DIMENSIONS && (
            <button type="button" onClick={() => onRows([...rows, { key: newKey(), name: free[0].name, raw: "" }])}>
              Thêm tham số
            </button>
          )}
          <p className={childCount > SWEEP_MAX_CHILDREN ? "field-error" : "muted"} aria-live="polite">
            {errors.sweep ?? `Sẽ tạo ${childCount} job con`}
          </p>
        </>
      )}
    </fieldset>
  );
}
