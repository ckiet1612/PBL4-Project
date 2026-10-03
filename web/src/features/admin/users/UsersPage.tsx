// /admin/users — list (1 read) and create. The password is never part of the intent fingerprint
// and is cleared once the outcome is definitive (UX-A27).
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { outcomeUnknown } from "../../../api/idempotency";
import { useApi } from "../../../auth/session";
import { EmptyState, Notice, Time } from "../../../components/bits";
import { serverFieldMessage, validationField } from "../errors";
import { checkDisplayName, checkPasswords, checkUsername } from "../forms";
import { INTENT_BUSY, IntentSlot } from "../intent";
import { useCursorPaging } from "../paging";
import { AdminErrorPanel, EnabledBadge, RefreshBar, useAdminLoad } from "../shared";
import { PasswordFields } from "./PasswordFields";

export function UsersPage() {
  const api = useApi().admin;
  const paging = useCursorPaging([]);
  const { cursor, resetIfBadCursor } = paging;
  const users = useAdminLoad(async (signal) => (await api.listUsers(cursor, signal)).data, [cursor]);
  useEffect(() => resetIfBadCursor(users.error), [users.error, resetIfBadCursor]);
  const [creating, setCreating] = useState(false);
  // The create intent outlives the form: cancel and reopen after a timeout resends the same key
  // (B18-RV10). The draft (with the password) stays in memory only and is dropped once settled.
  const createSlot = useRef(new IntentSlot<UserDraft>());
  const error = paging.shownError(users.error);
  const page = users.data;

  return (
    <section className="page">
      <div className="page-header">
        <h1>User</h1>
        <div className="actions">
          <RefreshBar loads={[users]} />
          <button type="button" className="primary" onClick={() => setCreating(true)} disabled={creating}>
            Tạo user
          </button>
        </div>
      </div>
      {paging.notice && <Notice onDismiss={paging.clearNotice}>{paging.notice}</Notice>}
      {creating && <CreateUserForm slot={createSlot.current} onCancel={() => setCreating(false)} />}
      {error !== null && <AdminErrorPanel error={error} onRetry={users.refresh} />}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">Tên đăng nhập</th>
              <th scope="col">Tên hiển thị</th>
              <th scope="col">Trạng thái</th>
              <th scope="col" className="col-optional">
                Quyền hệ thống
              </th>
              <th scope="col" className="col-optional">
                Tạo lúc
              </th>
            </tr>
          </thead>
          <tbody>
            {users.loading && page === null && (
              <tr>
                <td colSpan={5}>Đang tải…</td>
              </tr>
            )}
            {page?.items.map((user) => (
              <tr key={user.user_id}>
                <td>
                  <Link to={`/admin/users/${user.user_id}`}>{user.username}</Link>
                </td>
                <td>{user.display_name}</td>
                <td>
                  <EnabledBadge enabled={user.enabled} />
                </td>
                <td className="col-optional">{user.system_roles.includes("SYSTEM_ADMIN") ? "Quản trị hệ thống" : "—"}</td>
                <td className="col-optional">
                  <Time iso={user.created_at} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {page && page.items.length === 0 && cursor === null && <EmptyState title="Chưa có user nào" />}
      {paging.pager(page?.page.next_cursor ?? null)}
    </section>
  );
}

interface UserDraft {
  username: string;
  name: string;
  password: string;
  systemAdmin: boolean;
  passwordRevision: number;
}

function CreateUserForm({ slot, onCancel }: { slot: IntentSlot<UserDraft>; onCancel(): void }) {
  const api = useApi().admin;
  const navigate = useNavigate();
  const [reopened] = useState(() => slot.reopen);
  const passwordRevision = useRef(reopened?.passwordRevision ?? 0);
  const ids = { username: useId(), name: useId(), admin: useId() };
  const [username, setUsername] = useState(reopened?.username ?? "");
  const [name, setName] = useState(reopened?.name ?? "");
  const [password, setPassword] = useState(reopened?.password ?? "");
  const [confirm, setConfirm] = useState(reopened?.password ?? "");
  const [systemAdmin, setSystemAdmin] = useState(reopened?.systemAdmin ?? false);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const passwordErrors = checkPasswords(password, confirm);
  const errors = { username: checkUsername(username), display_name: checkDisplayName(name) };
  const serverField = validationField(error, ["username", "display_name", "password"] as const);
  const shown = (field: "username" | "display_name") =>
    (touched ? errors[field] : null) ?? (serverField === field ? serverFieldMessage(error) : null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (errors.username || errors.display_name || passwordErrors.password || passwordErrors.confirm) return;
    setBusy(true);
    setError(null);
    const roles: "SYSTEM_ADMIN"[] = systemAdmin ? ["SYSTEM_ADMIN"] : [];
    const fingerprint = JSON.stringify({ username, name: name.trim(), roles, password: passwordRevision.current });
    slot.sent = { username, name, password, systemAdmin, passwordRevision: passwordRevision.current };
    try {
      const result = await slot.intent.send(fingerprint, null, (options) =>
        api.createUser(
          { username, display_name: name.trim(), password, system_roles: roles },
          { idempotencyKey: options.idempotencyKey },
        ),
      );
      if (result === INTENT_BUSY) return;
      setPassword("");
      setConfirm("");
      navigate(`/admin/users/${result.data.user_id}`);
    } catch (caught) {
      if (!outcomeUnknown(caught)) {
        setPassword("");
        setConfirm("");
        passwordRevision.current += 1;
      }
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="form form-group" onSubmit={submit} aria-label="Tạo user" noValidate>
      <h2>Tạo user</h2>
      <div className="field">
        <label htmlFor={ids.username}>Tên đăng nhập</label>
        <input
          id={ids.username}
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={shown("username") !== null}
          aria-describedby={shown("username") ? `${ids.username}-error` : undefined}
        />
        {shown("username") && (
          <p id={`${ids.username}-error`} className="field-error">
            {shown("username")}
          </p>
        )}
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
      <PasswordFields
        password={password}
        confirm={confirm}
        errors={serverField === "password" ? { password: serverFieldMessage(error) ?? undefined } : passwordErrors}
        show={touched || serverField === "password"}
        onChange={(nextPassword, nextConfirm) => {
          if (nextPassword !== password) passwordRevision.current += 1;
          setPassword(nextPassword);
          setConfirm(nextConfirm);
        }}
      />
      <label className="checkbox" htmlFor={ids.admin}>
        <input id={ids.admin} type="checkbox" checked={systemAdmin} onChange={(event) => setSystemAdmin(event.target.checked)} />
        Quản trị hệ thống (toàn quyền khu quản trị)
      </label>
      {error !== null && serverField === null && <AdminErrorPanel error={error} />}
      <div className="form-actions">
        <button type="button" onClick={onCancel} disabled={busy}>
          Bỏ qua
        </button>
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Đang tạo…" : "Tạo user"}
        </button>
      </div>
    </form>
  );
}
