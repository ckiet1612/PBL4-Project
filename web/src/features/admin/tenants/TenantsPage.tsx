// /admin/tenants — list (1 read) and create. Create is one intent: a double click or a resend of
// the same slug/name after a network error or bare 5xx reuses its Idempotency-Key.
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { useApi } from "../../../auth/session";
import { EmptyState, Notice, Time } from "../../../components/bits";
import { checkDisplayName, checkSlug, SLUG_HINT } from "../forms";
import { serverFieldMessage, validationField } from "../errors";
import { INTENT_BUSY, IntentSlot } from "../intent";
import { useCursorPaging } from "../paging";
import { AdminErrorPanel, EnabledBadge, RefreshBar, useAdminLoad } from "../shared";

export function TenantsPage() {
  const api = useApi().admin;
  const paging = useCursorPaging([]);
  const { cursor, resetIfBadCursor } = paging;
  const tenants = useAdminLoad(async (signal) => (await api.listTenants(cursor, signal)).data, [cursor]);
  useEffect(() => resetIfBadCursor(tenants.error), [tenants.error, resetIfBadCursor]);
  const [creating, setCreating] = useState(false);
  // The create intent outlives the form: cancel and reopen after a timeout resends the same key (B18-RV10).
  const createSlot = useRef(new IntentSlot<TenantDraft>());
  const error = paging.shownError(tenants.error);
  const page = tenants.data;

  return (
    <section className="page">
      <div className="page-header">
        <h1>Tenant</h1>
        <div className="actions">
          <RefreshBar loads={[tenants]} />
          <button type="button" className="primary" onClick={() => setCreating(true)} disabled={creating}>
            Tạo tenant
          </button>
        </div>
      </div>
      {paging.notice && <Notice onDismiss={paging.clearNotice}>{paging.notice}</Notice>}
      {creating && <CreateTenantForm slot={createSlot.current} onCancel={() => setCreating(false)} />}
      {error !== null && <AdminErrorPanel error={error} onRetry={tenants.refresh} />}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Slug</th>
              <th scope="col">Tên hiển thị</th>
              <th scope="col">Trạng thái</th>
              <th scope="col" className="col-optional">
                Tạo lúc
              </th>
            </tr>
          </thead>
          <tbody>
            {tenants.loading && page === null && (
              <tr>
                <td colSpan={4}>Đang tải…</td>
              </tr>
            )}
            {page?.items.map((tenant) => (
              <tr key={tenant.tenant_id}>
                <td>
                  <Link to={`/admin/tenants/${tenant.tenant_id}`}>{tenant.slug}</Link>
                </td>
                <td>{tenant.display_name}</td>
                <td>
                  <EnabledBadge enabled={tenant.enabled} />
                </td>
                <td className="col-optional">
                  <Time iso={tenant.created_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {page && page.items.length === 0 && cursor === null && (
        <EmptyState title="Chưa có tenant nào. Tạo tenant đầu tiên">
          <p>Tenant là đơn vị hạn mức và chia sẻ dữ liệu; thêm thành viên sau khi tạo.</p>
        </EmptyState>
      )}
      {paging.pager(page?.page.next_cursor ?? null)}
    </section>
  );
}

interface TenantDraft {
  slug: string;
  name: string;
}

function CreateTenantForm({ slot, onCancel }: { slot: IntentSlot<TenantDraft>; onCancel(): void }) {
  const api = useApi().admin;
  const navigate = useNavigate();
  const [reopened] = useState(() => slot.reopen);
  const ids = { slug: useId(), name: useId() };
  const [slug, setSlug] = useState(reopened?.slug ?? "");
  const [name, setName] = useState(reopened?.name ?? "");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const errors = { slug: checkSlug(slug), display_name: checkDisplayName(name) };
  const serverField = validationField(error, ["slug", "display_name"] as const);
  const shown = (field: "slug" | "display_name") =>
    (touched ? errors[field] : null) ?? (serverField === field ? serverFieldMessage(error) : null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (errors.slug || errors.display_name) return;
    setBusy(true);
    setError(null);
    const body = { slug, display_name: name.trim() };
    slot.sent = { slug, name };
    try {
      const result = await slot.intent.send(JSON.stringify(body), null, (options) =>
        api.createTenant(body, { idempotencyKey: options.idempotencyKey }),
      );
      if (result === INTENT_BUSY) return;
      navigate(`/admin/tenants/${result.data.tenant_id}`);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form form-group" onSubmit={submit} aria-label="Tạo tenant" noValidate>
      <h2>Tạo tenant</h2>
      <div className="field">
        <label htmlFor={ids.slug}>Slug</label>
        <input
          id={ids.slug}
          value={slug}
          onChange={(event) => setSlug(event.target.value)}
          aria-invalid={shown("slug") !== null}
          aria-describedby={`${ids.slug}-help`}
          autoComplete="off"
          spellCheck={false}
        />
        <p id={`${ids.slug}-help`} className={shown("slug") ? "field-error" : "help"}>
          {shown("slug") ?? `${SLUG_HINT}. Không đổi được sau khi tạo.`}
        </p>
      </div>
      <div className="field">
        <label htmlFor={ids.name}>Tên hiển thị</label>
        <input
          id={ids.name}
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-invalid={shown("display_name") !== null}
          aria-describedby={shown("display_name") ? `${ids.name}-error` : undefined}
        />
        {shown("display_name") && (
          <p id={`${ids.name}-error`} className="field-error">
            {shown("display_name")}
          </p>
        )}
      </div>
      {error !== null && serverField === null && <AdminErrorPanel error={error} />}
      <div className="form-actions">
        <button type="button" onClick={onCancel} disabled={busy}>
          Bỏ qua
        </button>
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Đang tạo…" : "Tạo tenant"}
        </button>
      </div>
    </form>
  );
}
