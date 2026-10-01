// /account/tokens — CLI tokens of the signed-in user. The raw token is shown once, from memory
// only, and is gone when this page unmounts.
import { useId, useRef, useState, type FormEvent } from "react";
import { isApiError } from "../../api/errors";
import { IntentTracker, newIdempotencyKey, withInProgressRetry } from "../../api/idempotency";
import type { TokenCreated, TokenMetadata, TokenScope } from "../../api/types";
import { TOKEN_SCOPE_LABELS } from "../../app/labels";
import { useCurrentTenant } from "../../app/AppLayout";
import { isSystemAdmin, useApi, useSession } from "../../auth/session";
import { CopyButton, EmptyState, Notice, Time } from "../../components/bits";
import { ConfirmDialog } from "../../components/Dialog";
import { ErrorPanel } from "../../components/ErrorPanel";
import { back, EMPTY_TRAIL, forward, type PageTrail } from "../../components/pageTrail";
import { Pager } from "../../components/Pager";
import { usePolled } from "../../components/usePolled";

const USER_SCOPES: TokenScope[] = ["jobs:read", "jobs:write", "artifacts:read", "artifacts:write", "tokens:write"];
const ADMIN_SCOPES: TokenScope[] = ["admin:read", "admin:write"];
const DEFAULT_SCOPES: TokenScope[] = ["jobs:read", "jobs:write", "artifacts:read", "artifacts:write"];
const DAY = 86_400;
const EXPIRY_DAYS = [1, 7, 30] as const;
const NAME_MAX = 64;

function tokenStatus(token: TokenMetadata): string {
  if (token.revoked_at !== null) return "Đã thu hồi";
  // Display only: the server decides validity.
  return Date.parse(token.expires_at) <= Date.now() ? "Hết hạn" : "Còn hiệu lực";
}

export function TokensPage() {
  const api = useApi();
  const session = useSession();
  const tenantId = useCurrentTenant();
  const ids = { name: useId(), expiry: useId() };
  const [cursor, setCursor] = useState<string | null>(null);
  const [trail, setTrail] = useState<PageTrail>(EMPTY_TRAIL);
  const tokens = usePolled(
    { load: async (signal) => (await api.listTokens(cursor, signal)).data, schedule: null },
    [cursor],
  );

  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<Set<TokenScope>>(new Set(DEFAULT_SCOPES));
  const [days, setDays] = useState<(typeof EXPIRY_DAYS)[number]>(7);
  const [showErrors, setShowErrors] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<unknown>(null);
  const [created, setCreated] = useState<TokenCreated | null>(null);
  const tracker = useRef(new IntentTracker());
  const inFlight = useRef(false);

  const [revoking, setRevoking] = useState<TokenMetadata | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);
  const [revokeError, setRevokeError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const revokeKey = useRef<{ tokenId: string; key: string } | null>(null);

  const trimmed = name.trim();
  const nameError = trimmed === "" ? "Nhập tên token" : trimmed.length > NAME_MAX ? `Tối đa ${NAME_MAX} ký tự` : null;
  const scopeError = scopes.size === 0 ? "Chọn ít nhất một quyền" : null;
  const scopeChoices = isSystemAdmin(session) ? [...USER_SCOPES, ...ADMIN_SCOPES] : USER_SCOPES;

  const toggleScope = (scope: TokenScope, on: boolean) =>
    setScopes((current) => {
      const next = new Set(current);
      if (on) next.add(scope);
      else next.delete(scope);
      return next;
    });

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (inFlight.current) return;
    if (nameError || scopeError) {
      setShowErrors(true);
      return;
    }
    inFlight.current = true;
    setCreating(true);
    setCreateError(null);
    const body = {
      name: trimmed,
      scopes: scopeChoices.filter((scope) => scopes.has(scope)),
      expires_in_seconds: days * DAY,
    };
    const key = tracker.current.keyFor(JSON.stringify(body));
    try {
      const response = await withInProgressRetry(() => api.createToken(body, { idempotencyKey: key }));
      tracker.current.settle();
      setCreated(response.data);
      setName("");
      setShowErrors(false);
      tokens.refresh();
    } catch (caught) {
      tracker.current.settle(caught);
      setCreateError(caught);
      // one_time_secret_unavailable: the token exists but its value is gone; it shows in the list.
      if (isApiError(caught, "one_time_secret_unavailable")) tokens.refresh();
    } finally {
      inFlight.current = false;
      setCreating(false);
    }
  };

  const revoke = async () => {
    if (revoking === null || revokeBusy) return;
    if (revokeKey.current?.tokenId !== revoking.token_id) {
      revokeKey.current = { tokenId: revoking.token_id, key: newIdempotencyKey() };
    }
    setRevokeBusy(true);
    setRevokeError(null);
    try {
      await withInProgressRetry(() => api.revokeToken(revoking.token_id, { idempotencyKey: revokeKey.current!.key }));
      revokeKey.current = null;
      setNotice(`Đã thu hồi token "${revoking.name}".`);
      if (created?.token_id === revoking.token_id) setCreated(null);
      setRevoking(null);
      tokens.refresh();
    } catch (caught) {
      setRevokeError(caught);
    } finally {
      setRevokeBusy(false);
    }
  };

  const page = tokens.data;
  const endpoint = window.location.origin;
  const tenantArg = tenantId ?? "<tenant-id>";

  return (
    <section className="page">
      <h1>Token CLI</h1>
      <p className="muted">Token dùng cho CLI và script, thay cho mật khẩu. Mỗi token chỉ hiện một lần khi tạo.</p>

      {created && (
        <div className="secret-box" role="status">
          <p className="error-title">Sao chép token ngay: token chỉ hiện một lần và mất khi rời hoặc tải lại trang.</p>
          <p>
            Token <strong>{created.name}</strong>, hết hạn <Time iso={created.expires_at} />.
          </p>
          <pre className="secret">
            <code>{created.token}</code>
          </pre>
          <div className="actions">
            <CopyButton value={created.token} label="Sao chép token" />
            <button type="button" onClick={() => setCreated(null)}>
              Tôi đã lưu token
            </button>
          </div>
        </div>
      )}
      {notice && <Notice onDismiss={() => setNotice(null)}>{notice}</Notice>}

      <form className="form-group token-form" onSubmit={create} noValidate aria-labelledby="create-token-title">
        <h2 id="create-token-title">Tạo token</h2>
        <div className="field">
          <label htmlFor={ids.name}>Tên token</label>
          <input
            id={ids.name}
            value={name}
            maxLength={NAME_MAX + 1}
            autoComplete="off"
            aria-invalid={showErrors && nameError !== null}
            aria-describedby={`${ids.name}-help`}
            onChange={(event) => setName(event.target.value)}
          />
          <p id={`${ids.name}-help`} className={showErrors && nameError ? "field-error" : "help"}>
            {(showErrors && nameError) || `Ví dụ: laptop-cli. Tối đa ${NAME_MAX} ký tự.`}
          </p>
        </div>
        <fieldset className="choice-list">
          <legend>Quyền</legend>
          {scopeChoices.map((scope) => (
            <label key={scope} className="checkbox">
              <input
                type="checkbox"
                checked={scopes.has(scope)}
                onChange={(event) => toggleScope(scope, event.target.checked)}
              />
              {TOKEN_SCOPE_LABELS[scope]} <code>{scope}</code>
            </label>
          ))}
          {showErrors && scopeError && <p className="field-error">{scopeError}</p>}
        </fieldset>
        <div className="field">
          <label htmlFor={ids.expiry}>Hết hạn sau</label>
          <select
            id={ids.expiry}
            value={days}
            onChange={(event) => setDays(Number(event.target.value) as (typeof EXPIRY_DAYS)[number])}
          >
            {EXPIRY_DAYS.map((value) => (
              <option key={value} value={value}>
                {value} ngày
              </option>
            ))}
          </select>
        </div>
        {createError !== null && <ErrorPanel error={createError} />}
        <div className="actions">
          <button type="submit" className="primary" disabled={creating}>
            {creating ? "Đang tạo…" : "Tạo token"}
          </button>
        </div>
      </form>

      <h2>Token của bạn</h2>
      {tokens.error !== null && <ErrorPanel error={tokens.error} onRetry={tokens.refresh} />}
      {page && page.items.length === 0 && cursor === null && <EmptyState title="Chưa có token" />}
      {page && page.items.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Tên</th>
                <th scope="col" className="col-optional">
                  Quyền
                </th>
                <th scope="col" className="col-optional">
                  Tạo lúc
                </th>
                <th scope="col">Hết hạn</th>
                <th scope="col">Trạng thái</th>
                <th scope="col">
                  <span className="visually-hidden">Thao tác</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((token) => (
                <tr key={token.token_id}>
                  <td>{token.name}</td>
                  <td className="col-optional">
                    {token.scopes.map((scope) => (
                      <code key={scope} className="tag">
                        {scope}
                      </code>
                    ))}
                  </td>
                  <td className="col-optional">
                    <Time iso={token.created_at} />
                  </td>
                  <td>
                    <Time iso={token.expires_at} />
                  </td>
                  <td>{tokenStatus(token)}</td>
                  <td>
                    {token.revoked_at === null && (
                      <button
                        type="button"
                        className="danger"
                        onClick={() => {
                          setRevokeError(null);
                          setRevoking(token);
                        }}
                        aria-label={`Thu hồi token ${token.name}`}
                      >
                        Thu hồi
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pager
        hasPrevious={cursor !== null}
        nextCursor={page?.page.next_cursor ?? null}
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

      <section aria-labelledby="cli-usage-title" className="cli-usage">
        <h2 id="cli-usage-title">Dùng token với CLI</h2>
        <p>
          Đặt endpoint một lần, nhập token vào biến môi trường mà không lưu vào lịch sử shell, rồi chạy lệnh. CLI không có
          tùy chọn <code>--token</code>.
        </p>
        <pre>
          <code>
            {`nexa config set-endpoint ${endpoint}\nread -rs NEXA_TOKEN && export NEXA_TOKEN   # dán token rồi Enter\nnexa --tenant ${tenantArg} job list`}
          </code>
        </pre>
      </section>

      <ConfirmDialog
        open={revoking !== null}
        title="Thu hồi token?"
        confirmLabel="Thu hồi"
        danger
        busy={revokeBusy}
        onConfirm={revoke}
        onCancel={() => {
          if (!revokeBusy) setRevoking(null);
        }}
      >
        <p>
          Token <strong>{revoking?.name}</strong> sẽ ngừng hoạt động ngay; CLI hoặc script đang dùng nó sẽ bị từ chối. Không
          hoàn tác được.
        </p>
        {revokeError !== null && <ErrorPanel error={revokeError} />}
      </ConfirmDialog>
    </section>
  );
}
