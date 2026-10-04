# Nexa product CLI

Trạng thái của B12 là **Đã triển khai, chờ Task Review độc lập**. `nexa` là
REST client cho user và `SYSTEM_ADMIN`; API vẫn là nơi kiểm tra token scope,
tenant membership, ownership, role, state và precondition. CLI không truy cập
ORM, PostgreSQL, Docker socket, worker hoặc coordinator. `nexa-maintenance` là
CLI bảo trì riêng và không dùng thay cho `nexa`.

## Cài đặt và gọi lệnh

Trong môi trường đã cài package theo lockfile:

```bash
uv sync --frozen --all-groups --no-editable
uv run nexa --help
python -m nexa.cli.app --help
```

Entry point product là `nexa = nexa.cli.app:app`; entry point bảo trì
`nexa-maintenance = nexa.cli.main:app` vẫn giữ nguyên. Không đặt token trong
command line, URL, file spec, body log hoặc shell history. Token chỉ được trả
ở response thành công đầu tiên của `token create`; `config show` luôn che token.
Global options như `--output`, `--endpoint`, `--profile`, `--tenant` và
`--token-stdin` đứng trước command group, ví dụ `nexa --output json config show`.

## Cấu hình và profile

CLI lưu non-secret settings trong `config.toml` và token riêng trong
`credentials.json` dưới `XDG_CONFIG_HOME/nexa` (mặc định là thư mục cấu hình
của user). Thư mục có mode `0700`, file có mode `0600`, ghi bằng file tạm,
`fsync` và atomic replace.

Các biến môi trường:

| Giá trị | Cờ | Biến môi trường | Profile | Thứ tự chọn |
|---|---|---|---|---|
| endpoint | `--endpoint` | `NEXA_ENDPOINT` | `profiles.<name>.endpoint` | cờ → env → profile |
| profile | `--profile` | `NEXA_PROFILE` | `active_profile` | cờ → env → active |
| tenant context | `--tenant`/`--tenant-id` | `NEXA_TENANT_ID` | `profiles.<name>.tenant_id` | cờ → env → profile |

Bearer token được lấy theo thứ tự `--token-stdin` (đọc một lần từ stdin),
`NEXA_TOKEN`, rồi credentials của profile. Không có tùy chọn `--token` để
tránh đưa secret vào shell history. Ví dụ đọc token từ pipe:

```bash
printf '%s\n' "$NEXA_TOKEN" | nexa --token-stdin token list --page-size 50
```

Các lệnh cấu hình là `config show`, `config set-endpoint`, `config set-tenant`
và `config use-profile`. Thiếu tenant cho operation tenant-scoped để server
từ chối; CLI không tự suy ra membership.

Admin user create/update không nhận `--password` để tránh lộ secret trong shell
history hoặc process list. `admin user create` dùng prompt ẩn có xác nhận mặc
định; có thể dùng `--password-stdin` cho một dòng password từ pipe. `admin user
update` dùng `--password-stdin` khi đổi password. Khi tạo user bằng flags,
`system_roles` mặc định là `[]`; chỉ thêm `--system-role SYSTEM_ADMIN` khi cần.

## Command tree

```text
nexa config show|set-endpoint|set-tenant|use-profile
nexa token list|create|revoke
nexa artifact list|get|upload|download
nexa job submit|list|get|session|events|checkpoints|attempts|progress|result|result-download
nexa job cancel|pause|resume|retry
nexa template list|show|get
nexa sweep submit|show
nexa admin tenant list|create|get|update
nexa admin user list|create|get|update
nexa admin membership list|upsert|delete
nexa admin policy get|update
nexa admin tenant-policy get|update
nexa admin worker list|get|drain|disable|enable
nexa admin job list|get
nexa admin allocations
nexa admin fairness query
nexa admin recovery-events
nexa admin audit list
```

Các lệnh job/session/event/result dùng `jobs:read`; submit dùng `jobs:write`;
artifact list/get/download dùng `artifacts:read`; upload dùng `artifacts:write`; token
list/create/revoke dùng `tokens:write`; admin read và mutation lần lượt dùng
`admin:read` và `admin:write`. Scope là exact, không phân cấp: write không cấp
read. API còn kiểm tra active user, tenant role/membership, ownership và
`SYSTEM_ADMIN` tương ứng.

B15 đăng ký control job `cancel`, `pause`, `resume`, `retry` (scope
`jobs:write`), `job attempts` (scope `jobs:read`) và admin
`worker`/`allocations`/`recovery-events` sau khi route và API integration
evidence tồn tại. Chỉ logs còn chưa được đăng ký vì FastAPI snapshot chưa có
route/service tương ứng; retention sweep là tác vụ coordinator, không có lệnh CLI.

B18 đăng ký `admin job list|get` (`adminListJobs`, `adminGetJob`) và `admin
fairness query` (`adminQueryFairness`), cùng scope `admin:read`; API không dùng tenant
header cho các operation này. `admin job list` nhận `--tenant-id`, `--user-id`, `--state`,
`--waiting-reason`, `--created-after`, `--cursor`, `--page-size` (1–100, mặc định 50);
cursor gắn với admin và bộ lọc đã tạo ra nó. Hai lệnh chỉ đọc: điều khiển job vẫn
cần membership của tenant qua `nexa job cancel|pause|resume|retry`. Fairness bắt
buộc `--from`, `--to` (RFC 3339 có múi giờ) và `--bucket-seconds` (1–86400); API trả
400 khi `to <= from`, khoảng dài hơn 31 ngày hoặc vượt 1000 bucket:

```sh
nexa admin job list --state QUEUED --waiting-reason waiting_for_quota --page-size 25
nexa admin fairness query --from 2026-09-30T00:00:00Z --to 2026-10-01T00:00:00Z \
  --bucket-seconds 3600 [--tenant-id TENANT_ID]
```

B17 đăng ký `job progress JOB_ID [--tenant T]` (`getJobProgress`, scope
`jobs:read`). Output là `ProgressRecord`: `{"available": false}` khi attempt mới
nhất của job chưa báo tiến độ (hoặc job chưa có attempt); khi có thì gồm
`attempt_id`, `progress_sequence`, `snapshot`, `restore_checkpoint_id` (checkpoint
mà attempt đó khôi phục, `null` nếu chạy từ đầu) và `reported_at` (thời điểm
attempt được cập nhật lần cuối, B17-R15). Attempt khôi phục bắt đầu lại từ
sequence 1; tiến độ của attempt cũ không được dùng lại.

B16 đăng ký `template list [--enabled true|false]` (`listTemplates`, mặc định
chỉ template enabled) và `template show TEMPLATE_ID` (`getTemplate`; `get` là
tên tương đương giữ lại từ B12), cả hai dùng `jobs:read` và tenant context. Không
có REST admin cho template: version được đăng ký bằng lệnh bảo trì local

```bash
nexa-maintenance register-template --file deploy/templates/pytorch-cifar10-cnn.v1.json \
  --image-digest sha256:<digest image đã build>
```

Lệnh idempotent: cùng nội dung trả `UNCHANGED`, nội dung khác cho cùng
`(template_id, version)` bị từ chối (exit 1), file sai schema exit 2; lần đăng ký
mới ghi `audit_records` trong cùng transaction và không sửa version đã có.

B16 cũng đăng ký hai lệnh sweep, đều cần tenant context:

- `sweep submit --file REQUEST.json [--idempotency-key KEY]` (`submitSweep`,
  scope `jobs:write`):
  - gửi `POST /v1/sweeps` và in `207` kèm `sweep_id`/`Location`;
  - request có tối đa 100 child, lỗi request-level trả 422 và không ghi gì;
  - lệnh tự sinh key khi thiếu nhưng không in key đó. Muốn tiếp tục sau khi mất
    response thì phải truyền `--idempotency-key` từ đầu và chạy lại cùng key:
    các child chưa có outcome được tiếp tục mà không tạo job trùng (B16-R27).
- `sweep show SWEEP_ID [--cursor C] [--page-size N]` (`getSweep`, scope
  `jobs:read`): đọc parent và một trang child theo `child_index`, gồm trạng thái
  ACCEPTED/REJECTED, `job_id` hoặc lỗi snapshot.

Xem [submit](submit.md#parameter-sweep-b16-đã-triển-khai-chờ-task-review).

## Token và response loss

```bash
nexa token create --name cli --scope jobs:read --persist \
  --idempotency-key token-create-20260924-01
nexa token list --page-size 50
nexa token list --cursor OPAQUE_CURSOR --page-size 50
nexa token revoke TOKEN_ID --idempotency-key token-revoke-20260924-01
```

`token create` gửi `Idempotency-Key` ổn định cho một logical invocation. Chỉ
khi response đầu tiên nhận được và có `--persist` thì raw token mới được lưu
trong credentials file. Nếu transport mất response, retry mutation chỉ được
thực hiện với idempotency key; server có thể trả `409
one_time_secret_unavailable` và `Location`. Khi đó không tạo token thứ hai
bằng cùng key: ghi nhận locator, revoke token không biết (nếu cần), rồi tạo
lại bằng key mới theo quy trình vận hành đã được phê duyệt.

## Tenant, request headers và concurrency

Tenant-scoped calls gửi `X-Nexa-Tenant-Id`. Mutation gửi `Idempotency-Key` dài
16–128 ký tự ASCII; CLI không thay đổi key khi retry. Admin mutation yêu cầu
`--if-match` đúng ETag contract và truyền nguyên văn. Response rỗng `204` vẫn
giữ ETag mới trong output để dùng cho request kế tiếp. CLI không dùng wildcard,
không tự refetch để ghi đè race; `412`/`428` kết thúc với conflict exit code.

Các lệnh page nhận `--cursor` opaque và `--page-size` từ 1 đến 100; cursor
được truyền byte-for-byte và `next_cursor` được giữ trong JSON output. Filter
`--from`, `--to`, `--bucket-seconds` và các filter admin khác được truyền theo
contract, không được CLI diễn giải lại.

## Artifact và job

Upload đọc file theo chunk bounded, tính `sha256:<64 hex>` và size, gửi
`Content-Type: application/octet-stream` cùng `X-Artifact-Checksum`,
`X-Artifact-Size`, `X-Artifact-Kind`, `X-Artifact-Media-Type` và idempotency
key. Download không ghi đè file có sẵn nếu thiếu `--force`; nội dung đi qua
file tạm cùng thư mục, kiểm ETag/checksum/length, `fsync`, rồi atomic replace.
Transport chỉ chấp nhận response `2xx`; mọi redirect `3xx` đều là lỗi API.
Lỗi integrity trả exit code 10 và xóa file tạm.

`job submit` nhận `--spec-file` JSON strict hoặc các field đã công bố, truyền
object đã decode không sửa field/đơn vị. `job result-download` đọc metadata
result trước rồi tải manifest artifact qua operation artifact content. Event,
log và page luôn bounded; CLI không gom lịch sử vô hạn trong bộ nhớ.

B14 thêm `nexa job checkpoints JOB_ID [--cursor C] [--page-size N] [--tenant T]`
(scope `jobs:read`), gọi `GET /v1/jobs/{job_id}/checkpoints`. Kết quả là trang
metadata checkpoint `COMMITTED` hoặc `CORRUPT` theo `sequence` giảm dần: ID,
attempt, sequence, manifest artifact/checksum, trạng thái và thời điểm tạo. Lệnh
không trả reservation, staging hay nội dung checkpoint/cursor, và không có lệnh
restore thủ công. Automatic recovery tự chọn checkpoint; manual retry thuộc B15.

## Control job và recovery (B15)

```bash
nexa job cancel JOB_ID --reason TEXT --if-match '"v3"' [--idempotency-key K] [--tenant T]
nexa job pause JOB_ID --reason TEXT --if-match '"v3"' [--idempotency-key K] [--tenant T]
nexa job resume JOB_ID --reason TEXT --if-match '"v4"' [--idempotency-key K] [--tenant T]
nexa job retry JOB_ID --reason TEXT [--checkpoint-id CKPT] --if-match '"v9"'
nexa job attempts JOB_ID [--cursor C] [--page-size N] [--tenant T]
nexa admin worker list|get WORKER_ID
nexa admin worker drain|disable|enable WORKER_ID --reason TEXT --if-match '"v2"'
nexa admin allocations [--state HELD|QUARANTINED|RELEASED] [--cursor C] [--page-size N]
nexa admin recovery-events --from RFC3339 --to RFC3339 [--cursor C] [--page-size N]
```

Control gọi `POST /v1/jobs/{job_id}/{cancel|pause|resume|retry}` với body
`{"reason": ...}`; retry luôn gửi `checkpoint_id` (`null` khi không chọn) và
tạo job/session mới với `retry_of_job_id`, không sửa job nguồn. Trước khi gửi
request CLI kiểm tra cục bộ và trả exit 2 nếu: reason ngoài 1–256 ký tự, thiếu
`--if-match`, `--idempotency-key` không khớp 16–128 ký tự ASCII hiển thị, hoặc
page size ngoài 1–100 (`job attempts`, admin worker/allocations/recovery-events
từ chối, không kẹp). Admin worker action áp cùng quy tắc reason/If-Match.
ETag của worker chỉ đổi khi field hiển thị đổi (health, inventory version,
`ready_at`, admin state); heartbeat đều đặn (kể cả `discovered_at` mới của inventory, B18-R22) không làm ETag cũ (B15-R08), nên
`nexa admin worker get` rồi drain/disable/enable không bị `412` chỉ vì heartbeat.
`--from`/`--to` là bắt buộc theo contract và được truyền nguyên văn.

CLI không sinh lại key khi transport retry và không tự refetch ETag: `409`
(state không hợp lệ, `ADMISSION_OFF` cho retry, `WRITE_FROZEN` cho mọi control),
`412` hoặc `428` đều kết thúc với exit 6; người dùng đọc lại job rồi gửi
invocation mới với ETag mới. Reason là free text của người dùng: CLI không in
lại nó trong error output.

## Output và exit codes

Mặc định output human; dùng `--output json` cho JSON deterministic trên stdout.
Error đi stderr và chỉ chứa message, contract code, HTTP status, request ID,
Retry-After, Location hoặc ETag an toàn; không chứa bearer token, password,
artifact bytes hay server path.

| Exit | Ý nghĩa |
|---:|---|
| 0 | Thành công |
| 2 | Input/config local sai (bao gồm page size, JSON, ETag bắt buộc) |
| 3 | 401 chưa xác thực hoặc token hết hạn |
| 4 | 403 thiếu quyền/scope/membership |
| 5 | 404 resource không tồn tại hoặc bị tenant ẩn |
| 6 | Conflict, replay one-time secret, 412 hoặc 428 |
| 7 | Rate limited (429) |
| 8 | Server/dependency failure (5xx) |
| 9 | Lỗi API/client khác |
| 10 | Checksum/ETag/Content-Length integrity failure |

## Kiểm tra storage và kết quả (B19; chờ Task Review)

Hai lệnh bảo trì chỉ đọc, chạy local với `NEXA_DATABASE_URL` của deployment (không
qua REST, không ghi audit, chạy được cả khi `WRITE_FROZEN`; transaction `READ ONLY`):

```bash
nexa-maintenance storage-check
nexa-maintenance consistency-check --accepted-ids FILE   # mỗi dòng một job ID
```

- `storage-check` so `artifact_storage_counters` với tổng artifact `COMMITTED` và
  upload `ACTIVE` của từng tenant, in JSON một dòng (`consistent`, `drift_tenants`,
  `drift[]`, `retained_unpublished_artifacts`, `retained_unpublished_bytes`). Lệch chỉ
  được báo, không tự sửa (B19-R14).
- `consistency-check` kiểm mọi ID đã nhận có Job, Job `SUCCEEDED` có đúng một
  result và không có result cho Job chưa `SUCCEEDED`; in JSON gồm `accepted`,
  `missing_accepted`, `succeeded`, `succeeded_without_result`, `multiple_results`,
  `result_of_unsucceeded_job`, `consistent`.
- Exit code: 0 nhất quán, 1 không nhất quán, 2 cấu hình/schema/DB lỗi hoặc file ID
  sai định dạng. Output không có path, DSN hay secret. Xem
  [observability](observability.md) §6.4.

## Giới hạn phạm vi

CLI này không tự chứng minh semantics cancel/pause/resume/retry/recovery của
B15 (evidence nằm ở tài liệu evidence B15), scheduler fairness production, GPU, multi-server portability, load/soak/chaos,
checkpoint recovery ngoài lệnh xem danh sách checkpoint của B14, Web UI, bare-Linux acceptance hay release
`v1.0.0`. Những điều đó cần gate và evidence riêng theo PLAN.
