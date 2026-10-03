// /admin/tenants/:tenantId?tab=info|members|policy. The tenant is read once per open (breadcrumb,
// state); each tab adds its own read (members: memberships, policy: tenant policy).
import { useId, useRef, useState, type FormEvent } from "react";
import { useParams, useSearchParams } from "react-router";
import { isApiError } from "../../../api/errors";
import type { Tenant } from "../../../api/types";
import { useApi } from "../../../auth/session";
import { Breadcrumb, Notice, ShortId, Time } from "../../../components/bits";
import { ConfirmDialog } from "../../../components/Dialog";
import { Tabs } from "../../../components/Tabs";
import { conflictMessage } from "../errors";
import { checkDisplayName } from "../forms";
import { AdminIntent, INTENT_BUSY } from "../intent";
import { TenantPolicyTab } from "../policy/TenantPolicyTab";
import { AdminErrorPanel, EnabledBadge, RefreshBar, useAdminLoad, type AdminLoad } from "../shared";
import { MembersTab } from "./MembersTab";

type TenantTab = "info" | "members" | "policy";
const TABS: { id: TenantTab; label: string }[] = [
  { id: "info", label: "Thông tin" },
  { id: "members", label: "Thành viên" },
  { id: "policy", label: "Chính sách" },
];

export interface TenantView {
  tenant: Tenant;
  etag: string;
}

export function TenantDetailPage() {
  const { tenantId = "" } = useParams();
  const api = useApi().admin;
  const [params, setParams] = useSearchParams();
  const rawTab = params.get("tab");
  const tab: TenantTab = TABS.some((item) => item.id === rawTab) ? (rawTab as TenantTab) : "info";
  const view = useAdminLoad(async (signal): Promise<TenantView> => {
    const response = await api.getTenant(tenantId, signal);
    return { tenant: response.data, etag: response.etag ?? `"v${response.data.version}"` };
  }, [tenantId]);
  const tenant = view.data?.tenant ?? null;
  const notFound = isApiError(view.error, "resource_not_found");

  return (
    <section className="page">
      <Breadcrumb items={[{ label: "Tenant", to: "/admin/tenants" }, { label: tenant?.slug ?? tenantId.slice(-8) }]} />
      <div className="page-header">
        <h1>
          {tenant ? tenant.display_name : "Tenant"} {tenant && <EnabledBadge enabled={tenant.enabled} />}
        </h1>
      </div>
      {view.error !== null && (
        <AdminErrorPanel
          error={view.error}
          onRetry={notFound ? undefined : view.refresh}
          title={notFound ? "Không tìm thấy tenant" : undefined}
        />
      )}
      {view.loading && tenant === null && <p className="status-line">Đang tải…</p>}
      {view.data && (
        <Tabs
          label="Chi tiết tenant"
          tabs={TABS}
          active={tab}
          onSelect={(next) => setParams(next === "info" ? {} : { tab: next }, { replace: true })}
        >
          {tab === "info" && <InfoTab view={view as AdminLoad<TenantView>} />}
          {tab === "members" && <MembersTab tenantId={tenantId} tenantLoad={view} />}
          {tab === "policy" && <TenantPolicyTab tenantId={tenantId} tenantLoad={view} />}
        </Tabs>
      )}
    </section>
  );
}

function InfoTab({ view }: { view: AdminLoad<TenantView> }) {
  const api = useApi().admin;
  const { tenant, etag } = view.data!;
  const renameIntent = useRef(new AdminIntent());
  const enableIntent = useRef(new AdminIntent());
  const nameId = useId();
  const [name, setName] = useState(tenant.display_name);
  // One mutation per object (B18-RV04): rename and enable/disable share one in-flight slot;
  // the ref closes the gap before the re-render disables the other control.
  const [pending, setPending] = useState<"rename" | "toggle" | null>(null);
  const pendingRef = useRef<"rename" | "toggle" | null>(null);
  const begin = (kind: "rename" | "toggle") => {
    if (pendingRef.current !== null) return false;
    pendingRef.current = kind;
    setPending(kind);
    return true;
  };
  const end = () => {
    pendingRef.current = null;
    setPending(null);
  };
  const renaming = pending === "rename";
  const toggling = pending === "toggle";
  const busy = pending !== null;
  const [confirmToggle, setConfirmToggle] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const nameError = checkDisplayName(name);
  const nameChanged = name.trim() !== tenant.display_name;

  const apply = (next: Tenant, nextEtag: string | null) => view.set({ tenant: next, etag: nextEtag ?? `"v${next.version}"` });

  /** 412: reload the tenant, explain, keep the form; the next send is a new intent. */
  const conflict = async () => {
    try {
      const latest = await api.getTenant(tenant.tenant_id);
      apply(latest.data, latest.etag);
      setNotice(conflictMessage(tenant.version, latest.data.version));
    } catch (caught) {
      setError(caught);
    }
  };

  const rename = async (event: FormEvent) => {
    event.preventDefault();
    if (nameError || !nameChanged || !begin("rename")) return;
    setError(null);
    setNotice(null);
    const body = { display_name: name.trim() };
    try {
      const result = await renameIntent.current.send(JSON.stringify(body), etag, (options) =>
        api.updateTenant(tenant.tenant_id, body, { idempotencyKey: options.idempotencyKey, ifMatch: options.ifMatch ?? "" }),
      );
      if (result === INTENT_BUSY) return;
      apply(result.data, result.etag);
      setName(result.data.display_name);
      setNotice("Đã đổi tên tenant.");
    } catch (caught) {
      if (isApiError(caught, "version_conflict")) await conflict();
      else setError(caught);
    } finally {
      end();
    }
  };

  const toggle = async () => {
    if (!begin("toggle")) return;
    setError(null);
    setNotice(null);
    const body = { enabled: !tenant.enabled };
    try {
      const result = await enableIntent.current.send(JSON.stringify(body), etag, (options) =>
        api.updateTenant(tenant.tenant_id, body, { idempotencyKey: options.idempotencyKey, ifMatch: options.ifMatch ?? "" }),
      );
      if (result === INTENT_BUSY) return;
      apply(result.data, result.etag);
      setConfirmToggle(false);
      setNotice(result.data.enabled ? "Tenant đã được bật." : "Tenant đã được tắt.");
    } catch (caught) {
      setConfirmToggle(false);
      if (isApiError(caught, "version_conflict")) await conflict();
      else setError(caught);
    } finally {
      end();
    }
  };

  return (
    <div className="stack">
      <RefreshBar loads={[view]} />
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {error !== null && <AdminErrorPanel error={error} />}
      <dl className="key-values">
        <dt>Slug</dt>
        <dd>{tenant.slug}</dd>
        <dt>Tenant ID</dt>
        <dd>
          <ShortId id={tenant.tenant_id} copyLabel="Sao chép tenant ID" />
        </dd>
        <dt>Trạng thái</dt>
        <dd>
          <EnabledBadge enabled={tenant.enabled} />
        </dd>
        <dt>Phiên bản</dt>
        <dd>v{tenant.version}</dd>
        <dt>Tạo lúc</dt>
        <dd>
          <Time iso={tenant.created_at} />
        </dd>
        <dt>Cập nhật lúc</dt>
        <dd>
          <Time iso={tenant.updated_at} />
        </dd>
      </dl>

      <form className="form form-group" onSubmit={rename} aria-label="Đổi tên tenant" noValidate>
        <h2>Đổi tên</h2>
        <div className="field">
          <label htmlFor={nameId}>Tên hiển thị</label>
          <input
            id={nameId}
            value={name}
            onChange={(event) => setName(event.target.value)}
            aria-invalid={nameError !== null}
            aria-describedby={nameError ? `${nameId}-error` : undefined}
          />
          {nameError && (
            <p id={`${nameId}-error`} className="field-error">
              {nameError}
            </p>
          )}
        </div>
        <div className="form-actions">
          <button type="submit" className="primary" disabled={busy || !nameChanged || nameError !== null}>
            {renaming ? "Đang lưu…" : "Đổi tên"}
          </button>
        </div>
      </form>

      <section className="danger-zone" aria-labelledby={`${nameId}-state`}>
        <h2 id={`${nameId}-state`}>{tenant.enabled ? "Tắt tenant" : "Bật tenant"}</h2>
        <p>{tenant.enabled ? DISABLE_CONSEQUENCES : ENABLE_CONSEQUENCES}</p>
        <button
          type="button"
          className={tenant.enabled ? "danger" : "primary"}
          onClick={() => setConfirmToggle(true)}
          disabled={busy}
        >
          {tenant.enabled ? "Tắt" : "Bật"}
        </button>
      </section>

      <ConfirmDialog
        open={confirmToggle}
        title={tenant.enabled ? `Tắt tenant ${tenant.slug}?` : `Bật tenant ${tenant.slug}?`}
        confirmLabel={tenant.enabled ? "Tắt tenant" : "Bật tenant"}
        danger={tenant.enabled}
        busy={toggling}
        onConfirm={toggle}
        onCancel={() => setConfirmToggle(false)}
      >
        <p>{tenant.enabled ? DISABLE_CONSEQUENCES : ENABLE_CONSEQUENCES}</p>
      </ConfirmDialog>
    </div>
  );
}

const DISABLE_CONSEQUENCES =
  "Thành viên mất quyền vào tenant. Job đang chờ không được cấp phát. Job đang chạy không bị dừng. Phiên đăng nhập của thành viên không bị thu hồi.";
const ENABLE_CONSEQUENCES = "Thành viên vào lại được tenant; job đang chờ được xét cấp phát trở lại.";
