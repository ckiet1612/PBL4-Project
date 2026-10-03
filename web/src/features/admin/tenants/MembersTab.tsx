// Tenant ?tab=members. If-Match for add/change/remove is the MembershipSet ETag of the latest
// memberships read. Users (first 100) are read only when the add dialog opens (UX-A25).
import { useEffect, useId, useRef, useState } from "react";
import { isApiError } from "../../../api/errors";
import { PAGE_SIZE } from "../../../api/limits";
import type { Membership, MembershipPage, Role, User } from "../../../api/types";
import { ROLE_LABELS } from "../../../app/labels";
import { useApi } from "../../../auth/session";
import { EmptyState, Notice, Time } from "../../../components/bits";
import { ConfirmDialog } from "../../../components/Dialog";
import { EMPTY_TRAIL, back, forward, type PageTrail } from "../../../components/pageTrail";
import { Pager } from "../../../components/Pager";
import { conflictMessage, versionFromEtag } from "../errors";
import { INTENT_BUSY, IntentSlots, type IntentSlot } from "../intent";
import { AdminErrorPanel, RefreshBar, UserName, useAdminLoad, type AdminLoad } from "../shared";
import type { TenantView } from "./TenantDetailPage";

const ROLES = Object.keys(ROLE_LABELS) as Role[];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const CONFLICT_SAME_VERSION = "Đối tượng vừa được thay đổi. Kiểm tra rồi gửi lại.";
const REVOKES =
  "Mọi phiên đăng nhập và token CLI của user này bị thu hồi; user cần đăng nhập lại.";

interface MembersView {
  page: MembershipPage;
  etag: string;
}

interface MembershipDraft {
  userId: string;
  pasted: string;
  role: Role;
}

type Pending =
  | { kind: "add" }
  | { kind: "role"; membership: Membership }
  | { kind: "remove"; membership: Membership };

export function MembersTab({ tenantId, tenantLoad }: { tenantId: string; tenantLoad: AdminLoad<TenantView> }) {
  const api = useApi().admin;
  const [cursor, setCursor] = useState<string | null>(null);
  const [trail, setTrail] = useState<PageTrail>(EMPTY_TRAIL);
  const read = async (signal?: AbortSignal): Promise<MembersView> => {
    const response = await api.listMemberships(tenantId, cursor, signal);
    return { page: response.data, etag: response.etag ?? `"v${response.data.membership_set_version}"` };
  };
  const members = useAdminLoad(read, [tenantId, cursor]);
  const [users, setUsers] = useState<User[] | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Intents outlive the dialog: cancel and reopen after a timeout resends the same key (B18-RV10).
  const slots = useRef(new IntentSlots<MembershipDraft>());
  const data = members.data;
  const dialogKey = pending && (pending.kind === "add" ? "add" : `${pending.kind}:${pending.membership.user_id}`);

  /** After a write or a 412: re-read the current page; returns it with the version-jump explanation. */
  const reload = async (before: number | null): Promise<{ latest: MembersView | null; message: string | null }> => {
    try {
      const latest = await read();
      members.set(latest);
      const after = latest.page.membership_set_version;
      return { latest, message: before !== null && after !== before ? conflictMessage(before, after) : null };
    } catch {
      members.refresh();
      return { latest: null, message: null };
    }
  };

  return (
    <div className="stack">
      <div className="page-header">
        <h2>Thành viên</h2>
        <div className="actions">
          <RefreshBar loads={[tenantLoad, members]} />
          <button type="button" className="primary" onClick={() => setPending({ kind: "add" })} disabled={data === null}>
            Thêm thành viên
          </button>
        </div>
      </div>
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      {members.error !== null && <AdminErrorPanel error={members.error} onRetry={members.refresh} />}
      {data && data.page.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Vai trò</th>
                <th scope="col" className="col-optional">
                  Thêm lúc
                </th>
                <th scope="col">Thao tác</th>
              </tr>
            </thead>
            <tbody>
              {data.page.items.map((membership) => (
                <tr key={membership.user_id}>
                  <td>
                    <UserName id={membership.user_id} users={users} />
                  </td>
                  <td>{ROLE_LABELS[membership.role]}</td>
                  <td className="col-optional">
                    <Time iso={membership.created_at} />
                  </td>
                  <td>
                    <span className="inline-actions">
                      <button type="button" className="button-link" onClick={() => setPending({ kind: "role", membership })}>
                        Đổi vai trò
                      </button>
                      <button
                        type="button"
                        className="button-link"
                        onClick={() => setPending({ kind: "remove", membership })}
                      >
                        Xóa
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && data.page.items.length === 0 && cursor === null && (
        <EmptyState title="Tenant chưa có thành viên">
          <p>Bấm Thêm thành viên để cấp quyền dùng tenant cho một user.</p>
        </EmptyState>
      )}
      <Pager
        hasPrevious={cursor !== null}
        nextCursor={data?.page.page.next_cursor ?? null}
        onFirst={() => {
          setTrail(EMPTY_TRAIL);
          setCursor(null);
        }}
        onPrevious={() => {
          const step = back(trail);
          setTrail(step.trail);
          setCursor(step.target);
        }}
        onNext={(next) => {
          setTrail(forward(trail, cursor));
          setCursor(next);
        }}
      />
      {pending && data && (
        <MembershipDialog
          key={dialogKey!}
          slot={slots.current.slot(dialogKey!)}
          tenantId={tenantId}
          pending={pending}
          view={data}
          users={users}
          onUsers={setUsers}
          onDone={async (message) => {
            setPending(null);
            setNotice(message);
            await reload(null);
          }}
          // 412 keeps the dialog and its input (B18-RV05); the next confirm is a new intent
          // sent with the ETag of this re-read. A change/remove whose member is gone closes it.
          onConflict={async () => {
            const { latest, message } = await reload(data.page.membership_set_version);
            const target = pending.kind === "add" ? null : pending.membership.user_id;
            if (target !== null && latest !== null && !latest.page.items.some((item) => item.user_id === target)) {
              setPending(null);
              setNotice(message);
              return null;
            }
            return message ?? CONFLICT_SAME_VERSION;
          }}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
}

interface DialogProps {
  slot: IntentSlot<MembershipDraft>;
  tenantId: string;
  pending: Pending;
  view: MembersView;
  users: User[] | null;
  onUsers(users: User[]): void;
  onDone(message: string): void;
  /** Re-reads the memberships; resolves to the explanation shown in the dialog (null: closed). */
  onConflict(): Promise<string | null>;
  onCancel(): void;
}

function MembershipDialog({ slot, tenantId, pending, view, users, onUsers, onDone, onConflict, onCancel }: DialogProps) {
  const api = useApi().admin;
  const intent = slot.intent;
  const [reopened] = useState(() => slot.reopen);
  const ids = { user: useId(), paste: useId(), role: useId() };
  const current = pending.kind === "add" ? null : pending.membership;
  const [userId, setUserId] = useState(reopened?.userId ?? "");
  const [pasted, setPasted] = useState(reopened?.pasted ?? "");
  const [role, setRole] = useState<Role>(reopened?.role ?? (current?.role === "MEMBER" ? "TENANT_ADMIN" : "MEMBER"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [usersError, setUsersError] = useState<unknown>(null);
  const [usersPartial, setUsersPartial] = useState(false);

  // One users read per open of the add dialog (A3: +1 audit row).
  useEffect(() => {
    if (pending.kind !== "add") return;
    const controller = new AbortController();
    api.listUsers(null, controller.signal, PAGE_SIZE.adminNames).then(
      (response) => {
        onUsers(response.data.items);
        setUsersPartial(response.data.page.next_cursor !== null);
      },
      (caught: unknown) => {
        if (!controller.signal.aborted) setUsersError(caught);
      },
    );
    return () => controller.abort();
    // Only on open; the dialog is keyed per intent.
  }, []);

  const memberIds = new Set(view.page.items.map((item) => item.user_id));
  const target = current?.user_id ?? (pasted.trim() || userId);
  const pasteError = pasted.trim() !== "" && !UUID.test(pasted.trim()) ? "User ID là UUID dạng 0190…-…" : null;
  const invalid =
    pending.kind === "add" ? target === "" || pasteError !== null : pending.kind === "role" && role === current?.role;

  const confirm = async () => {
    if (invalid) return;
    setBusy(true);
    setError(null);
    setConflict(null);
    const ifMatch = view.etag;
    slot.sent = { userId, pasted, role };
    try {
      const result =
        pending.kind === "remove"
          ? await intent.send(JSON.stringify({ remove: target }), ifMatch, (options) =>
              api.deleteMembership(tenantId, target, { idempotencyKey: options.idempotencyKey, ifMatch: options.ifMatch ?? "" }),
            )
          : await intent.send(JSON.stringify({ user_id: target, role }), ifMatch, (options) =>
              api.upsertMembership(
                tenantId,
                { user_id: target, role },
                { idempotencyKey: options.idempotencyKey, ifMatch: options.ifMatch ?? "" },
              ),
            );
      if (result === INTENT_BUSY) return;
      onDone(
        pending.kind === "remove"
          ? "Đã xóa thành viên khỏi tenant."
          : pending.kind === "add"
            ? `Đã thêm thành viên với vai trò ${ROLE_LABELS[role]}.`
            : `Đã đổi vai trò thành ${ROLE_LABELS[role]}.`,
      );
    } catch (caught) {
      if (isApiError(caught, "version_conflict")) setConflict(await onConflict());
      else setError(caught);
    } finally {
      setBusy(false);
    }
  };

  const title =
    pending.kind === "add" ? "Thêm thành viên" : pending.kind === "role" ? "Đổi vai trò thành viên" : "Xóa thành viên?";
  const etagVersion = versionFromEtag(view.etag);

  return (
    <ConfirmDialog
      open
      title={title}
      confirmLabel={pending.kind === "add" ? "Thêm" : pending.kind === "role" ? "Đổi vai trò" : "Xóa thành viên"}
      danger={pending.kind === "remove"}
      busy={busy}
      confirmDisabled={invalid}
      onConfirm={confirm}
      onCancel={onCancel}
    >
      {current && (
        <p>
          User: <UserName id={current.user_id} users={users} /> — hiện là {ROLE_LABELS[current.role]}.
        </p>
      )}
      {pending.kind === "add" && (
        <>
          <div className="field">
            <label htmlFor={ids.user}>Chọn user</label>
            <select id={ids.user} value={userId} onChange={(event) => setUserId(event.target.value)} disabled={pasted.trim() !== ""}>
              <option value="">{users === null && usersError === null ? "Đang tải…" : "— Chọn —"}</option>
              {users
                ?.filter((user) => !memberIds.has(user.user_id))
                .map((user) => (
                  <option key={user.user_id} value={user.user_id}>
                    {user.username} ({user.display_name}){user.enabled ? "" : " — đã tắt"}
                  </option>
                ))}
            </select>
            {usersPartial && <p className="help">Chỉ hiện {PAGE_SIZE.adminNames} user đầu tiên; user khác: dán user ID.</p>}
          </div>
          {usersError !== null && <AdminErrorPanel error={usersError} title="Không tải được danh sách user" />}
          <div className="field">
            <label htmlFor={ids.paste}>Hoặc dán user ID</label>
            <input
              id={ids.paste}
              value={pasted}
              onChange={(event) => setPasted(event.target.value)}
              aria-invalid={pasteError !== null}
              aria-describedby={pasteError ? `${ids.paste}-error` : undefined}
              spellCheck={false}
            />
            {pasteError && (
              <p id={`${ids.paste}-error`} className="field-error">
                {pasteError}
              </p>
            )}
          </div>
        </>
      )}
      {pending.kind !== "remove" && (
        <div className="field">
          <label htmlFor={ids.role}>Vai trò</label>
          <select id={ids.role} value={role} onChange={(event) => setRole(event.target.value as Role)}>
            {ROLES.map((value) => (
              <option key={value} value={value}>
                {ROLE_LABELS[value]}
              </option>
            ))}
          </select>
        </div>
      )}
      {pending.kind === "remove" && <p>User mất quyền vào tenant này.</p>}
      <p>{REVOKES}</p>
      {etagVersion !== null && <p className="muted">Danh sách thành viên phiên bản v{etagVersion}.</p>}
      {conflict !== null && <Notice onDismiss={() => setConflict(null)}>{conflict}</Notice>}
      {error !== null && <AdminErrorPanel error={error} />}
    </ConfirmDialog>
  );
}
