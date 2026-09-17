# Contract index

Phiên bản contract: **`1.0.0-b01`**. Dẫn xuất từ [PLAN](../PLAN.md) §2–§10 và §13–§14; PLAN được ưu tiên nếu có mâu thuẫn. Bộ tài liệu này khóa contract cho B02–B25 nhưng chưa phải implementation, migration hay runtime evidence.

## Phạm vi và nguồn chuẩn

| Tài liệu | Nội dung chuẩn |
|---|---|
| [OpenAPI 3.1](contracts/openapi.yaml) | REST `/v1`, wire schema HTTP, authentication, status/header và operation ID |
| [Domain model](contracts/domain-model.md) | Entity, ownership, quan hệ, authority, constraint và index group |
| [State machines](contracts/state-machines.md) | State/desired state, guard, actor, atomic effect và race outcome |
| [Internal interfaces](contracts/internal-interfaces.md) | Năm interface, scheduler snapshot/decision, worker và trusted-runner protocol |
| [Workloads/checkpoints](contracts/workloads-checkpoints.md) | Bốn template, progress/result, compatibility và restore policy |
| [Concurrency/recovery](contracts/concurrency-recovery.md) | Linearization, lock order, fencing, cleanup, authorization, failure matrix và security limits |
| [Workload manifest schema](contracts/schemas/workload-manifests.schema.json) | JSON Schema chuẩn duy nhất cho checkpoint, result và chunk-output manifest |
| [Traceability](requirements-traceability.md) | PLAN → contract → INV → ACC → backlog → phép kiểm chứng |
| [Environment inventory](environment-inventory.md) | Môi trường đã quan sát, do user cung cấp và chưa xác nhận |
| [B01 review evidence](evidence/B01-contract-review.md) | Validator, walkthrough, scope review và kết luận ACC-01 |

OpenAPI sở hữu schema HTTP. JSON Schema bên ngoài chỉ sở hữu manifest phi HTTP; OpenAPI tham chiếu schema đó thay vì sao chép. Markdown sở hữu semantics transaction, ordering, authorization và failure mà schema không biểu diễn được. B05 mới chuyển các constraint logic thành DDL/Alembic.

## Quy ước wire chung

| Loại | Contract `1.0.0-b01` | Nguồn |
|---|---|---|
| ID | UUIDv7 canonical chữ thường, dạng `8-4-4-4-12`; server sinh. ID bootstrap cố định cũng phải là UUID hợp lệ | B01 cụ thể hóa để có thứ tự thời gian mà không lộ path/hostname |
| Timestamp | Chuỗi RFC 3339 UTC, đúng ba chữ số mili-giây và hậu tố `Z`; DB time là authority cho lease | PLAN §9; B01 khóa serialization |
| Duration | Số nguyên không âm, hậu tố field `_seconds` hoặc `_milliseconds`; không dùng duration string mơ hồ | B01 cụ thể hóa |
| Byte | Số nguyên không âm, hậu tố `_bytes`; không dùng MiB trong wire | PLAN §4/§9; B01 cụ thể hóa |
| CPU | Số nguyên `cpu_millis`; `1000` là một logical CPU; request tối thiểu `100` | B01 cụ thể hóa quanh PLAN §3–§5 |
| GPU | GPU cấp nguyên chiếc; request dùng `gpu_count` 0 hoặc 1, allocation/inventory dùng UUID thiết bị | PLAN §2–§5 |
| Version | Số nguyên 64-bit dương, bắt đầu 1, tăng đúng 1 khi representation có concurrency significance thay đổi | B01 cụ thể hóa |
| ETag | Strong ETag dạng `"v<version>"`; response versioned luôn trả `ETag` | B01 cụ thể hóa |
| Checksum | `sha256:` + đúng 64 ký tự hex chữ thường; byte đã hash là byte blob thực tế | PLAN §3/§7/§9 |
| Digest image | OCI digest `sha256:` + 64 hex; không nhận tag mutable trong job spec | PLAN §3/§7/§9 |
| Enum | Giá trị chữ hoa `UPPER_SNAKE_CASE`; giá trị không biết bị từ chối, không fallback | B01 cụ thể hóa |
| JSON | UTF-8, media type `application/json`; duplicate member hoặc field không khai báo bị từ chối; tối đa `api_json_max_bytes` | B01 cụ thể hóa và INV-16 |
| Số | JSON integer cho counter/resource/version; không dùng float cho resource, sequence hoặc tiền định scheduler | B01 cụ thể hóa |

`ResourceRequestVector` gồm `cpu_millis`, `memory_bytes`, `gpu_count`; request/allocation phải có CPU/RAM dương, GPU 0/1 và không có resource lạ. `ResourceCapacityVector` dùng cùng field nhưng cho phép 0 ở mọi dimension để biểu diễn host không còn allocatable capacity hoặc policy vô hiệu hóa một dimension; capacity cấp job là discovered host capacity trừ reserve và không thể âm. Allocation thêm `gpu_uuids` và phải khớp request dương đã authorize.

## Authentication và actor

| Actor | Credential | Quy tắc |
|---|---|---|
| Browser user/admin | Opaque server session cookie `nexa_session`; mutation yêu cầu header `X-CSRF-Token` khớp secret session | Cookie `HttpOnly`, `Secure`, `SameSite=Lax`, path `/`; session và CSRF không lưu localStorage |
| CLI user/admin | Opaque bearer token có scope, expiry, revocation; DB chỉ giữ hash | Token chỉ hiện một lần khi tạo; không được gửi qua query string |
| Worker | Opaque bearer credential gắn đúng `worker_id` và incarnation | Chỉ gọi worker operations cho identity local; không có quyền user/admin |
| Bootstrap operator | Secret ngoài repository/image, chỉ dùng hai operation bootstrap trong maintenance network | Tạo system admin đầu tiên đúng một lần và cấu hình/rotate đúng worker local; không phải public enrollment |

Mọi operation public tenant-scoped bắt buộc `X-Nexa-Tenant-Id`. Browser session hoặc CLI token có thể đại diện một user có nhiều membership, nhưng header này chọn đúng một tenant context; backend recheck active membership/role/scope trên tenant đó trước lookup, cursor hoặc idempotency. Token không tự mang một “active tenant” ngầm. Cursor, audit và idempotency đều bind tenant context này. Global system-admin là grant riêng trên user, không phải tenant membership; cross-tenant access chỉ qua operation `/admin` được audit.

Authentication, revocation, tenant context, membership, global role, ownership và principal scope được kiểm tra **trước** idempotency replay. Không phân biệt `not_found` với `forbidden` cho object tenant khác: API trả `404 resource_not_found` sau khi tenant context hợp lệ. Membership tenant thường không cho quyền admin global.

CLI scope là exact và không phân cấp: `jobs:read` cho template/job/session/event/log/result read; `jobs:write` cho submit/sweep/control; `artifacts:read`/`artifacts:write` cho tenant artifact read/upload; `tokens:write` chỉ quản lý token của chính caller; `admin:read`/`admin:write` cho operation `/admin` tương ứng và vẫn cần active `SYSTEM_ADMIN`. Bảng operation group chuẩn nằm trong [concurrency/recovery](contracts/concurrency-recovery.md#cli-scope-to-operation-mapping) và được lặp trong mô tả `cliBearer` của OpenAPI.

## Error envelope

Mọi lỗi JSON dùng `ErrorResponse` trong OpenAPI:

```json
{
  "code": "version_conflict",
  "message": "The resource changed; refresh and retry.",
  "request_id": "018f0d60-7b6a-7a21-9d82-1aa39c4f30b7"
}
```

`message` an toàn, không chứa credential, path, input, checkpoint, SQL hay dữ liệu tenant khác. `request_id` được server sinh và mọi JSON error phản chiếu qua `X-Request-Id`. Mã ổn định gồm `authentication_required`, `invalid_csrf`, `permission_denied`, `resource_not_found`, `validation_failed`, `invalid_cursor`, `idempotency_conflict`, `idempotency_in_progress`, `one_time_secret_unavailable`, `precondition_required`, `version_conflict`, `state_conflict`, `infeasible_request`, `rate_limited`, `quota_exceeded`, `queue_full`, `dependency_unavailable`, `checksum_mismatch`, `payload_too_large`, `storage_pressure`, `stale_authority`, `lease_expired`, `callback_replayed` và `internal_error`.

Status chuẩn: `400` wire/cursor sai; `401` thiếu/sai credential; `403` authenticated nhưng thiếu scope; `404` không thấy hoặc không sở hữu; `409` idempotency/callback/state/stale-authority conflict; `412` stale `If-Match`; `413` quá giới hạn byte; `422` validation/infeasible/capability; `428` thiếu `If-Match`; `429` rate/outstanding quota; `500 internal_error` khi lỗi server bất ngờ đã rollback/fail closed; `503` queue/dependency/storage chưa sẵn sàng. `429` và `503` luôn có `Retry-After` số giây nguyên dương; `500` vẫn dùng error envelope và `X-Request-Id`, không lộ dữ liệu nhạy cảm.

## Idempotency

Các operation mutation được OpenAPI đánh dấu `Idempotency-Key` dùng `IdempotencyRecord` với scope `(context, principal_id, operation_id, key)`. Namespace và principal là đóng, không được tự suy diễn:

- Public tenant route: `context=<tenant UUID>` lấy từ `X-Nexa-Tenant-Id`; `principal_id` là user/service-account đã xác thực trong tenant context đó.
- User-global và mọi `/admin` route: `context=GLOBAL`; `principal_id` là user/service-account đã xác thực. Tenant ID trong admin path chỉ là normalized path parameter của request hash, không đổi namespace.
- Hai bootstrap route: `context=BOOTSTRAP`; `principal_id` là identity nội bộ dẫn xuất từ bootstrap credential đã xác thực, không phải raw secret.
- `workerCreateIncarnation`: `context=WORKER:<worker_id>` và `principal_id=<worker_id>`. Server recheck path worker, logical worker identity và current non-revoked credential trước lookup/replay.
- `workerUploadAttemptArtifact`: không nhận hay tin tenant header. Server resolve `context=<tenant UUID>` từ exact live Authority → Attempt → Job trước lookup/replay; `principal_id=<worker_id>` từ worker credential đã xác thực, rồi recheck current credential/incarnation/full Authority.

Các worker operation mang `Callback-Id` không dùng `IdempotencyRecord`; chúng dùng `CallbackReceipt` unique theo `(worker_id, operation_id, callback_id)` như quy tắc callback bên dưới. Key idempotency là ASCII 16–128 ký tự, không chứa whitespace. Canonical payload hash:

1. Xác thực actor và ownership/scope trước khi đọc record replay.
2. Với JSON, parse sau khi từ chối duplicate/unknown field, serialize theo RFC 8785 JCS rồi hash SHA-256 trên `operation_id`, normalized path parameters và canonical body. Header `If-Match` không nằm trong hash.
3. Với upload, HTTP transport luôn `application/octet-stream`; hash toàn bộ metadata có thể đổi behavior/authorization: original `X-Artifact-Media-Type`, declared size, declared checksum và `X-Artifact-Kind`. Worker upload còn hash normalized attempt path và full Authority header tuple `(worker_id, worker_incarnation_id, allocation_id, lease_id, job_fence)`; tenant context đã được server resolve từ exact live Authority vào idempotency scope, không đến từ client header. Completed same-Attempt upload sau successful adoption là ngoại lệ replay hẹp: caller vẫn phải có current Authority; stored Authority phải là predecessor trong immutable grant lineage; attempt/allocation/lease/fence và toàn bộ stored upload metadata phải khớp, rồi server mới trả nguyên resource/response cũ dù incarnation/hash khác. Stale Authority không đọc được replay và không receipt nào bị scan/rewrite. Thiếu hoặc đổi bất kỳ field nào khác dùng cùng key trả `409 idempotency_conflict`; media type phải qua allowlist của kind khi upload và được recheck theo template/manifest khi tham chiếu/publish; checksum thực của stream phải khớp trước commit.
4. Transaction đầu tiên insert row `PENDING` unique theo scope, khóa counter/rate cần thiết, thực hiện mutation, lưu status/body/các header replayable rồi đổi `COMPLETED` cùng commit.
5. Cùng key/hash đã `COMPLETED` trả nguyên status/body và `Location`/`ETag`; xử lý replay này trước `If-Match` và không tiêu token/counter/quota lần nữa, ngoại trừ secret dùng một lần ở quy tắc kế tiếp.
6. Cùng key khác hash trả `409 idempotency_conflict`. Cùng hash đang `PENDING` chờ tối đa `idempotency_pending_wait_milliseconds` (mặc định 5000); chưa xong trả `409 idempotency_in_progress`, `Retry-After: 1`.
7. Crash làm transaction rollback không để record có hiệu lực. Response mất sau commit được replay từ record hoàn chỉnh.
8. Retention kéo dài khi resource còn active và ít nhất 30 ngày sau terminal; sweep parent/child mapping được giữ cùng thời gian dài hơn.

`createCliToken` và `bootstrapLocalWorker` là ngoại lệ one-time-secret: raw token/worker credential được sinh trong memory, chỉ hash được commit và raw secret không nằm trong idempotency response snapshot. Lần đầu có thể trả secret đúng một lần. Replay cùng key/hash trả `409 one_time_secret_unavailable`, `Location` của credential metadata và không tạo credential thứ hai. Với CLI token, caller dùng `Location` để revoke token không nhận được rồi tạo token bằng key mới. Với worker bootstrap, request key mới atomically revoke/rotate credential current trước khi phát secret mới, nên response loss không để nhiều worker credential current. Không log, backup hay persist raw secret để đổi lấy replay.

Authentication, credential revocation, tenant membership/scope/ownership và, với worker upload, current credential/incarnation cùng full live Authority luôn được recheck trước lookup/replay. Vì vậy upload đã commit không thể được replay bởi authority đã fenced/revoked; record chỉ dedup khi caller vẫn có cùng live authority. Worker callback dùng `callback_id` UUIDv7 unique theo `(worker_id, operation_id, callback_id)` và payload hash. Callback replay hợp lệ trả acknowledgment gốc theo callback semantics; stale authority vẫn bị từ chối nếu chưa có acknowledgment đã commit. Riêng adoption replay recheck credential, current new incarnation và stored transfer outcome; nó không yêu cầu prior incarnation vẫn current sau chính commit adoption. Quy tắc này bao phủ claim/adopt/start/renew/reservation/checkpoint/complete, callback failure có phân loại và cleanup; failure commit luôn fence/quarantine trước khi cleanup có thể release.

## Version và conditional mutation

`If-Match` bắt buộc cho job control, tenant policy/quota, membership mutation, worker drain/disable và object admin versioned. Membership mutation luôn target `MembershipSet` của tenant: aggregate bắt đầu version 1, list trả ETag kể cả empty, và mỗi create/update/delete tăng đúng một lần. Thiếu header trả `428`; ETag không đúng version hiện tại trả `412`. Replay idempotency hợp lệ được trả trước hai kiểm tra này. Version tăng khi state, desired state, policy/quota, membership set, worker administrative state hoặc metadata người dùng nhìn thấy thay đổi; append-only event/log, heartbeat timestamp và metric tick không tự tăng job version. Mỗi transition state chỉ tăng một lần trong transaction thắng race.

## Cursor và giới hạn đọc

Cursor là opaque base64url do server ký, chứa contract version, stable sort key, direction và hash của filters/actor scope; client không được tự tạo. Đổi filter/sort với cursor trả `400 invalid_cursor`. Cursor hết hạn sau 24 giờ; pagination vẫn nhất quán theo keyset nhưng không hứa snapshot isolation giữa các page.

| Collection | Ordering ổn định | Page mặc định / tối đa |
|---|---|---|
| Jobs, attempts, checkpoints, artifacts, workers, audit | `created_at DESC, id DESC` | 50 / 100 |
| Events/recovery events | `sequence ASC, id ASC` | 50 / 100 |
| Sweep children | `child_index ASC, child_job_id ASC` | 50 / 100 |
| Logs | `(attempt_id, byte_offset)`; `after_offset` là byte kế tiếp chưa đọc | 64 KiB / 1 MiB mỗi response |

Job/event page không vượt 100 theo PLAN. Aggregate fairness/usage nhận range tối đa 31 ngày, tối đa 1000 bucket và không trả raw full queue. Filter phải được whitelist trong từng operation; cursor luôn ràng buộc actor và tenant scope.

## Compatibility và thay đổi contract

- `/v1` là major API; thêm optional field chỉ được phép khi client contract chấp nhận nhưng server vẫn từ chối **request** unknown field để phát hiện mismatch sớm.
- Thay required field, enum semantics, state/guard, checksum/provenance hoặc signature interface là breaking change: phải cập nhật PLAN nếu đổi quyết định đã khóa, ADR/contract/traceability và task phụ thuộc trước implementation.
- `schema_version` của workload manifest bắt đầu `1`; adapter phải hỗ trợ rõ range, không đoán forward compatibility.
- B01 khóa behavior bên ngoài và pre/postcondition. Chi tiết class/module, SQL shape, cache layout và Docker SDK call không ảnh hưởng contract thuộc task triển khai sở hữu, miễn giữ invariant.

## Quyết định và nguồn

| Quyết định | Phân loại | Lý do |
|---|---|---|
| Single-node modular monolith, API/coordinator riêng, worker-only Docker | Đã khóa trong PLAN §1–§3 | Boundary bắt buộc |
| PostgreSQL authority + immutable filesystem blob | Đã khóa trong PLAN §3/§9 | Correctness và durability |
| Weighted dominant resource-time + quota/aging/one reservation | Đã khóa trong PLAN §5 | Thuật toán sản phẩm duy nhất |
| UUIDv7, strict JSON, ETag format, cursor signature, pending replay behavior | B01 cụ thể hóa | Loại bỏ mơ hồ wire mà không đổi scope/guarantee |
| Configurable abuse limits có default | B01 cụ thể hóa | Fail closed, portable, không trình bày như benchmark result |
| Runtime/framework tolerances | Chưa khóa số đo tại B01 | B16 phải đóng băng fixture/tolerance trước phép đo; contract khóa phương pháp và nơi ghi |

Không có đề xuất nào trong bộ contract này thay PLAN hoặc đưa Future Work §15 vào release `v1.0.0`.
