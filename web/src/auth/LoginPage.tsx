// /login: one generic error for bad credentials, Retry-After countdown, WRITE_FROZEN notice.
import { useState, type FormEvent } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router";
import { isApiError } from "../api/errors";
import { ErrorPanel } from "../components/ErrorPanel";
import { useCountdown } from "../components/useCountdown";
import { safeNextPath } from "./next";
import { useSessionContext } from "./session";

type LoginError = { kind: "credentials" } | { kind: "frozen" } | { kind: "other"; error: unknown };

export function LoginPage() {
  const { state, login } = useSessionContext();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<LoginError | null>(null);
  const countdown = useCountdown();
  const target = safeNextPath(params.get("next")) ?? "/";
  const expired = params.get("expired") === "1" || (state.status === "anonymous" && state.reason === "expired");

  if (state.status === "authenticated" && !busy) return <Navigate to={target} replace />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || countdown.remaining > 0) return;
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      setPassword("");
      navigate(target, { replace: true });
    } catch (caught) {
      setPassword("");
      if (isApiError(caught, "rate_limited")) {
        countdown.start(caught.retryAfterSeconds);
        setError(null);
      } else if (isApiError(caught, "authentication_required", "validation_failed")) {
        setError({ kind: "credentials" });
      } else if (isApiError(caught, "permission_denied")) {
        setError({ kind: "frozen" });
      } else {
        setError({ kind: "other", error: caught });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card">
        <h1>Nexa</h1>
        <p>Nền tảng chạy batch AI trên container cho nhiều tenant</p>
        {expired && (
          <p className="notice" role="status">
            Phiên đăng nhập đã hết hạn. Đăng nhập lại để tiếp tục
          </p>
        )}
        <form onSubmit={submit} className="form" noValidate>
          <div className="field">
            <label htmlFor="login-username">Tên đăng nhập</label>
            <input
              id="login-username"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="login-password">Mật khẩu</label>
            <input
              id="login-password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </div>
          {error?.kind === "credentials" && (
            <p className="field-error" role="alert">
              Tên đăng nhập hoặc mật khẩu không đúng
            </p>
          )}
          {error?.kind === "frozen" && (
            <p className="field-error" role="alert">
              Hệ thống đang ở chế độ chỉ đọc; chỉ quản trị viên hệ thống được đăng nhập
            </p>
          )}
          {error?.kind === "other" && <ErrorPanel error={error.error} />}
          {countdown.remaining > 0 && (
            <p className="field-error" role="alert">
              Bạn đăng nhập quá nhanh. Thử lại sau {countdown.remaining} giây.
            </p>
          )}
          <button type="submit" className="primary" disabled={busy || countdown.remaining > 0}>
            {busy ? "Đang đăng nhập…" : "Đăng nhập"}
          </button>
        </form>
      </section>
    </main>
  );
}
