# Authentication, bootstrap and administration

Tài liệu này mô tả implementation B06. Nguồn chuẩn vẫn là `PLAN.md` và contract trong
`docs/contracts/`; nếu có khác biệt, contract đã duyệt được ưu tiên. B06 chỉ cung cấp lát cắt
identity/admin/policy. Nó không chứng minh workload, worker protocol, scheduler, recovery,
Web UI, TLS deployment hoặc release.

## Khởi chạy và secret

1. Tạo hai file secret do deployment quản lý, không đặt trong repository/image. Server secret
   chứa 32–4096 byte, không có NUL. Bootstrap secret chứa 32–4096 byte visible ASCII
   (`0x21`–`0x7e`) để giữ nguyên qua HTTP header. Một newline cuối file được bỏ khi đọc.
2. Cấu hình toàn bộ biến bắt buộc và identity ổn định trong `.env.example`. Nexa chỉ đọc
   process environment, không tự load `.env`.
3. Chạy migration tường minh trước khi mở API:

   ```sh
   NEXA_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST/DATABASE' \
     uv run --no-sync alembic upgrade head
   ```

4. Chạy API. Import module không kết nối DB; lifespan kiểm schema generation 2 và durable
   installation/worker identity rồi mới nhận request:

   ```sh
   uv run --no-sync uvicorn nexa.api.main:app --host 127.0.0.1 --port 8000
   ```

Không truyền bootstrap secret qua command argument, URL, log hoặc request body. HTTP client
vận hành phải đọc file secret và gửi chính xác giá trị trong `X-Nexa-Bootstrap-Secret` qua
kết nối TLS. Bootstrap chỉ được chấp nhận từ `NEXA_MAINTENANCE_CIDRS`; `X-Forwarded-For`
chỉ được tin khi peer trực tiếp thuộc `NEXA_TRUSTED_PROXY_CIDRS`.

Admin bootstrap dùng `POST /v1/internal/admin-bootstrap` với `Idempotency-Key`, header secret
và body `username`, `display_name`, `password`. Cửa sổ mặc định là 15 phút. Thành công tạo
user, SYSTEM_ADMIN grant, audit, idempotency record và latch đóng vĩnh viễn trong cùng
transaction. Restart hoặc disable admin không mở lại latch.

Worker bootstrap dùng `POST /v1/internal/worker-bootstrap` với đúng `installation_id` và
`credential_public_fingerprint` đã cấu hình. Nó rotate credential cho đúng local worker,
không tạo incarnation/inventory và không đánh dấu worker READY. Khi cửa sổ hết hạn, operator
local mở lại một cửa sổ có audit bằng:

```sh
uv run --no-sync nexa-maintenance reopen-worker-bootstrap
```

## Password và browser session

Password dài 12–1024 ký tự và được hash bằng Argon2id. Mặc định/giới hạn thấp nhất là
19,456 KiB memory, time cost 2, parallelism 1; concurrency hash mặc định là 4. Các giá trị
này khớp mức tối thiểu OWASP Password Storage Cheat Sheet được kiểm tra ngày 20/09/2026:
<https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html>.
Config yếu hơn bị từ chối.

`POST /v1/auth/login` yêu cầu `Origin` và `Host` khớp `NEXA_PUBLIC_ORIGIN`. Login rate mặc
định 10 lần/phút theo source + normalized username, lưu bền vững trong PostgreSQL và vẫn
được charge khi password sai. Password verification chạy ngoài event loop và bị giới hạn
concurrency.

Session secret có 256 bit ngẫu nhiên; DB chỉ giữ hash. Cookie luôn là
`nexa_session; Secure; HttpOnly; SameSite=Lax; Path=/`. Absolute TTL mặc định 12 giờ, idle
TTL 2 giờ; idle refresh không kéo dài absolute expiry. Mỗi request recheck expiry, revoke và
user enabled. `GET /v1/auth/session` trả CSRF token được HMAC-derive lại từ cookie bằng
server secret, nên dùng được qua restart mà không lưu raw token.

Mọi browser-cookie mutation cần `Origin`, `Host` và `X-CSRF-Token` bind đúng session.
`POST /v1/auth/logout` revoke server-side rồi xóa cookie. Request có đồng thời cookie và
Bearer bị từ chối, không merge authority. Theo OpenAPI hiện hành, `/v1/auth/session` chỉ
nhận browser cookie; mô tả “CLI introspection bằng any valid token” còn mâu thuẫn và chưa
được tự ý mở rộng.

## CLI token

Các scope exact, không phân cấp:

| Scope | Authority B06 |
|---|---|
| `tokens:write` | List/create/revoke token của chính user |
| `admin:read` | Admin read, đồng thời cần SYSTEM_ADMIN active |
| `admin:write` | Admin mutation, đồng thời cần SYSTEM_ADMIN active |
| `jobs:read`, `jobs:write` | Handoff cho B07/B08/B12; chưa có job endpoint B06 |
| `artifacts:read`, `artifacts:write` | Handoff cho B07/B12; chưa có artifact endpoint B06 |

Browser user có thể yêu cầu token trong authority hiện hành: token/admin scope chỉ khi có
membership/SYSTEM_ADMIN tương ứng. Bearer caller phải có `tokens:write` và chỉ được cấp
tập con scope của chính token đó. TTL hợp lệ là 300–2,592,000 giây. DB chỉ giữ SHA-256 hash;
raw token chỉ trả ở response commit đầu tiên. Replay cùng idempotency key trả
`one_time_secret_unavailable`, không cấp secret thứ hai.

`GET /v1/tokens`, `POST /v1/tokens` và `DELETE /v1/tokens/{token_id}` luôn bind user hiện
hành. Token của user khác được che thành 404. Disable user revoke toàn bộ browser session
và CLI token của user trong cùng workflow transaction; re-enable không hồi sinh credential.

## Operation và authorization matrix

| Nhóm operation | Browser | CLI bearer | Worker | Bootstrap |
|---|---|---|---|---|
| `loginBrowserSession` | Anonymous, đúng Origin/Host | Không | Không | Không |
| `getBrowserSession`, `logoutBrowserSession` | Cookie-only; logout cần CSRF | Không | Không | Không |
| `listCliTokens`, `createCliToken`, `revokeCliToken` | User hiện hành; mutation cần CSRF | Exact `tokens:write`; requested scope không vượt caller | Không | Không |
| Tenant/user/membership/policy/audit reads | SYSTEM_ADMIN active | SYSTEM_ADMIN active + exact `admin:read` | Không | Không |
| Tenant/user/membership/policy mutations | SYSTEM_ADMIN active + CSRF | SYSTEM_ADMIN active + exact `admin:write` | Không | Không |
| `bootstrapInitialAdmin`, `bootstrapLocalWorker` | Không | Không | Không | Maintenance source + bootstrap secret + open window |

24 operation ID B06 đã wire:

- Auth/token: `loginBrowserSession`, `getBrowserSession`, `logoutBrowserSession`,
  `listCliTokens`, `createCliToken`, `revokeCliToken`.
- Identity: `adminListTenants`, `adminCreateTenant`, `adminGetTenant`,
  `adminUpdateTenant`, `adminListUsers`, `adminCreateUser`, `adminGetUser`,
  `adminUpdateUser`, `adminListMemberships`, `adminUpsertMembership`,
  `adminDeleteMembership`.
- Policy/audit: `adminGetGlobalPolicy`, `adminUpdateGlobalPolicy`,
  `adminGetTenantPolicy`, `adminUpdateTenantPolicy`, `adminListAuditRecords`.
- Bootstrap: `bootstrapInitialAdmin`, `bootstrapLocalWorker`.

TENANT_ADMIN không được gọi `/v1/admin` trong v1. SYSTEM_ADMIN không tự có membership vào
mọi tenant và không impersonate user. B07/B08 phải dùng principal + tenant context + live
membership guard cho resource route, thay vì suy quyền từ global role.

## Version, idempotency và operational mode

User, tenant, MembershipSet và policy mutation dùng strong ETag `"v<version>"`.
`If-Match` thiếu trả 428, stale trả 412. Thứ tự mutation là live auth/ownership,
idempotency lookup/replay, `If-Match`, rồi mutation + audit + response snapshot trong một
transaction. JSON body bị giới hạn byte, từ chối duplicate/unknown member; request hash dùng
RFC 8785 JCS sau khi parse Decimal trong miền wire đã chốt.

Global policy v1 được migration seed với outstanding 100000 và mode `NORMAL`. Tenant mới
có count/rate defaults theo contract nhưng resource limits bằng zero khi chưa có inventory
đã xác minh. Policy update khóa current version, counter liên quan và kiểm aggregate mọi
allocation chưa release, kể cả `QUARANTINED`. Admission transaction B08 phải lấy shared
policy lock trước khi đổi counter để giữ cùng lock order.

Mode chỉ đi `NORMAL -> ADMISSION_OFF -> WRITE_FROZEN` và mở lại theo chiều ngược. Boundary
proof mặc định fail closed vì B06 chưa có reconcile/restore/readiness runtime. Trong
`WRITE_FROZEN`, chỉ browser login/session logout, login-rate metadata, admin read audit,
stored idempotency replay và guarded recovery transition thuộc allowlist; token, grant,
membership, bootstrap và config mutation bị chặn.

Pagination mặc định 50, tối đa 100. Cursor ký bằng server secret, TTL mặc định 24 giờ và bind
actor/operation/filter. Error response dùng server UUIDv7 `X-Request-Id`, không echo ID client;
dependency DB trả 503 + `Retry-After` mà không lộ SQL/credential.

## Handoff

- B07 dùng `Principal`, exact scope, live tenant membership/ownership, error/idempotency
  helpers và transaction boundary; B06 chưa chứng minh job/artifact isolation surface.
- B08 dùng policy/counter lock order, JCS request hash, replay-before-ETag và atomic audit/
  idempotency; submit/admission/rate bucket vẫn chưa triển khai.
- B10 dùng local worker ID, bootstrap credential và `resolve_worker_credential`; vẫn cần B09
  executor và phải thêm incarnation/heartbeat/reconcile/fencing.
- B12/UI dùng cookie/CSRF hoặc exact-scope token, ETag, signed cursor và error contract. B12
  chưa được phép coi `/auth/session` là bearer introspection khi OpenAPI chưa đổi.
