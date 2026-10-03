// /admin/users/:userId — rename, reset password, enable/disable, system role (A3). Each form is
// its own intent with If-Match = ETag of the latest user read. Password, enabled and role
// changes revoke the user's sessions and CLI tokens on the server.
import { useId, useRef, useState, type FormEvent } from "react";
import { useParams } from "react-router";
import { isApiError } from "../../../api/errors";
import { outcomeUnknown } from "../../../api/idempotency";
import type { User, UserUpdateRequest } from "../../../api/types";
import { useApi, useSession } from "../../../auth/session";
import { Breadcrumb, Notice, ShortId, Time } from "../../../components/bits";
import { ConfirmDialog } from "../../../components/Dialog";
import { conflictMessage } from "../errors";
import { checkDisplayName, checkPasswords } from "../forms";
import { AdminIntent, INTENT_BUSY } from "../intent";
import { AdminErrorPanel, EnabledBadge, RefreshBar, useAdminLoad, type AdminLoad } from "../shared";
import { PasswordFields } from "./PasswordFields";

interface UserView {
  user: User;
  etag: string;
}

const REVOKES = "Mọi phiên đăng nhập và token CLI hiện có của user bị thu hồi ngay; user cần đăng nhập lại.";
const SELF_NOTE = "Đây là tài khoản bạn đang dùng: phiên hiện tại của bạn cũng bị thu hồi.";

type Outcome = { ok: true } | { ok: false; unknown: boolean };

export function UserDetailPage() {
  const { userId = "" } = useParams();
  const api = useApi().admin;
  const view = useAdminLoad(async (signal): Promise<UserView> => {
    const response = await api.getUser(userId, signal);
    return { user: response.data, etag: response.etag ?? `"v${response.data.version}"` };
  }, [userId]);
  const notFound = isApiError(view.error, "resource_not_found");
  const user = view.data?.user ?? null;
  return (
    <section className="page">
      <Breadcrumb items={[{ label: "User", to: "/admin/users" }, { label: user?.username ?? userId.slice(-8) }]} />
      <div className="page-header">
        <h1>
          {user ? user.display_name : "User"} {user && <EnabledBadge enabled={user.enabled} />}
        </h1>
        <RefreshBar loads={[view]} />
      </div>
      {view.error !== null && (
        <AdminErrorPanel
          error={view.error}
          onRetry={notFound ? undefined : view.refresh}
          title={notFound ? "Không tìm thấy user" : undefined}
        />
      )}
      {view.loading && user === null && <p className="status-line">Đang tải…</p>}
      {view.data && <UserForms view={view} />}
    </section>
  );
}

function UserForms({ view }: { view: AdminLoad<UserView> }) {
  const api = useApi().admin;
  const session = useSession();
  const { user, etag } = view.data!;
  const self = user.user_id === session.user_id;
  const isAdmin = user.system_roles.includes("SYSTEM_ADMIN");
  const intents = useRef({
    rename: new AdminIntent(),
    password: new AdminIntent(),
    enabled: new AdminIntent(),
    role: new AdminIntent(),
  });
  const passwordRevision = useRef(0);
  const nameId = useId();
  const [name, setName] = useState(user.display_name);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [passwordTouched, setPasswordTouched] = useState(false);
  const [dialog, setDialog] = useState<"password" | "enabled" | "role" | null>(null);
  const [busy, setBusy] = useState<keyof typeof intents.current | null>(null);
  const [error, setError] = useState<{ form: string; error: unknown } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const nameError = checkDisplayName(name);
  const passwordErrors = checkPasswords(password, confirm);

  const send = async (
    form: keyof typeof intents.current,
    fingerprint: unknown,
    body: UserUpdateRequest,
    done: (user: User) => string,
  ): Promise<Outcome> => {
    setBusy(form);
    setError(null);
    setNotice(null);
    try {
      const result = await intents.current[form].send(JSON.stringify(fingerprint), etag, (options) =>
        api.updateUser(user.user_id, body, { idempotencyKey: options.idempotencyKey, ifMatch: options.ifMatch ?? "" }),
      );
      if (result === INTENT_BUSY) return { ok: false, unknown: true };
      view.set({ user: result.data, etag: result.etag ?? `"v${result.data.version}"` });
      setNotice(done(result.data));
      return { ok: true };
    } catch (caught) {
      if (isApiError(caught, "version_conflict")) {
        try {
          const latest = await api.getUser(user.user_id);
          view.set({ user: latest.data, etag: latest.etag ?? `"v${latest.data.version}"` });
          setNotice(conflictMessage(user.version, latest.data.version));
        } catch (reloadError) {
          setError({ form, error: reloadError });
        }
      } else {
        setError({ form, error: caught });
      }
      return { ok: false, unknown: outcomeUnknown(caught) };
    } finally {
      setBusy(null);
    }
  };

  const rename = async (event: FormEvent) => {
    event.preventDefault();
    if (nameError || name.trim() === user.display_name) return;
    const body = { display_name: name.trim() };
    await send("rename", body, body, () => "Đã đổi tên hiển thị.");
  };

  const openPassword = (event: FormEvent) => {
    event.preventDefault();
    setPasswordTouched(true);
    if (passwordErrors.password || passwordErrors.confirm) return;
    setDialog("password");
  };

  const resetPassword = async () => {
    const outcome = await send("password", { password: passwordRevision.current }, { password }, () =>
      "Đã đặt lại mật khẩu; phiên và token cũ của user đã bị thu hồi.",
    );
    setDialog(null);
    if (outcome.ok || !outcome.unknown) {
      setPassword("");
      setConfirm("");
      setPasswordTouched(false);
      passwordRevision.current += 1;
    }
  };

  const toggleEnabled = async () => {
    const body = { enabled: !user.enabled };
    await send("enabled", body, body, (next) =>
      next.enabled ? "User đã được bật lại." : "User đã bị tắt; phiên và token của user đã bị thu hồi.",
    );
    setDialog(null);
  };

  const toggleRole = async () => {
    const body: UserUpdateRequest = { system_roles: isAdmin ? [] : ["SYSTEM_ADMIN"] };
    await send("role", body, body, (next) =>
      next.system_roles.includes("SYSTEM_ADMIN") ? "Đã cấp quyền quản trị hệ thống." : "Đã thu hồi quyền quản trị hệ thống.",
    );
    setDialog(null);
  };

  const formError = (form: string) =>
    error?.form === form ? <AdminErrorPanel error={error.error} /> : null;

  return (
    <div className="stack">
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}
      <dl className="key-values">
        <dt>Tên đăng nhập</dt>
        <dd>{user.username}</dd>
        <dt>User ID</dt>
        <dd>
          <ShortId id={user.user_id} copyLabel="Sao chép user ID" />
        </dd>
        <dt>Quyền hệ thống</dt>
        <dd>{isAdmin ? "Quản trị hệ thống" : "Không có"}</dd>
        <dt>Phiên bản</dt>
        <dd>v{user.version}</dd>
        <dt>Tạo lúc</dt>
        <dd>
          <Time iso={user.created_at} />
        </dd>
        <dt>Cập nhật lúc</dt>
        <dd>
          <Time iso={user.updated_at} />
        </dd>
      </dl>
      <p className="muted">Thành viên tenant được quản lý ở tab Thành viên của từng tenant.</p>

      <form className="form form-group" onSubmit={rename} aria-label="Đổi tên hiển thị" noValidate>
        <h2>Đổi tên hiển thị</h2>
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
        {formError("rename")}
        <div className="form-actions">
          <button
            type="submit"
            className="primary"
            disabled={busy !== null || nameError !== null || name.trim() === user.display_name}
          >
            {busy === "rename" ? "Đang lưu…" : "Đổi tên"}
          </button>
        </div>
      </form>

      <form className="form form-group" onSubmit={openPassword} aria-label="Đặt lại mật khẩu" noValidate>
        <h2>Đặt lại mật khẩu</h2>
        <PasswordFields
          label="Mật khẩu mới"
          password={password}
          confirm={confirm}
          errors={passwordErrors}
          show={passwordTouched}
          onChange={(nextPassword, nextConfirm) => {
            if (nextPassword !== password) passwordRevision.current += 1;
            setPassword(nextPassword);
            setConfirm(nextConfirm);
          }}
        />
        {formError("password")}
        <div className="form-actions">
          <button type="submit" className="primary" disabled={busy !== null}>
            Đặt lại mật khẩu
          </button>
        </div>
      </form>

      <section className="danger-zone" aria-labelledby={`${nameId}-role`}>
        <h2 id={`${nameId}-role`}>Quyền quản trị hệ thống</h2>
        <p>{isAdmin ? "User đang có quyền quản trị hệ thống." : "User không có quyền quản trị hệ thống."}</p>
        {formError("role")}
        <button type="button" className={isAdmin ? "danger" : undefined} onClick={() => setDialog("role")} disabled={busy !== null}>
          {isAdmin ? "Thu hồi quyền quản trị" : "Cấp quyền quản trị"}
        </button>
      </section>

      <section className="danger-zone" aria-labelledby={`${nameId}-enabled`}>
        <h2 id={`${nameId}-enabled`}>{user.enabled ? "Tắt user" : "Bật lại user"}</h2>
        <p>
          {user.enabled
            ? "Thu hồi ngay mọi phiên đăng nhập và token CLI của user; bật lại không khôi phục chúng."
            : "User đăng nhập lại được; phiên và token đã thu hồi không được khôi phục."}
        </p>
        {formError("enabled")}
        <button
          type="button"
          className={user.enabled ? "danger" : "primary"}
          onClick={() => setDialog("enabled")}
          disabled={busy !== null}
        >
          {user.enabled ? "Tắt user" : "Bật lại user"}
        </button>
      </section>

      <ConfirmDialog
        open={dialog === "password"}
        title={`Đặt lại mật khẩu cho ${user.username}?`}
        confirmLabel="Đặt lại mật khẩu"
        busy={busy === "password"}
        onConfirm={resetPassword}
        onCancel={() => setDialog(null)}
      >
        <p>{REVOKES}</p>
        {self && <p className="warning">{SELF_NOTE}</p>}
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "enabled"}
        title={user.enabled ? `Tắt user ${user.username}?` : `Bật lại user ${user.username}?`}
        confirmLabel={user.enabled ? "Tắt user" : "Bật lại user"}
        danger={user.enabled}
        busy={busy === "enabled"}
        onConfirm={toggleEnabled}
        onCancel={() => setDialog(null)}
      >
        <p>
          {user.enabled
            ? "Thu hồi ngay mọi phiên đăng nhập và token CLI của user; bật lại không khôi phục chúng."
            : "User đăng nhập lại được bằng mật khẩu hiện có. Phiên và token đã thu hồi không được khôi phục."}
        </p>
        {self && user.enabled && <p className="warning">{SELF_NOTE}</p>}
      </ConfirmDialog>
      <ConfirmDialog
        open={dialog === "role"}
        title={isAdmin ? `Thu hồi quyền quản trị của ${user.username}?` : `Cấp quyền quản trị cho ${user.username}?`}
        confirmLabel={isAdmin ? "Thu hồi quyền" : "Cấp quyền"}
        danger={isAdmin}
        busy={busy === "role"}
        onConfirm={toggleRole}
        onCancel={() => setDialog(null)}
      >
        <p>
          {isAdmin
            ? "User không vào được khu quản trị nữa. Máy chủ từ chối nếu đây là quản trị viên cuối cùng."
            : "User có toàn quyền khu quản trị: worker, tenant, user, chính sách."}
        </p>
        <p>{REVOKES}</p>
        {self && <p className="warning">{SELF_NOTE}</p>}
      </ConfirmDialog>
    </div>
  );
}
