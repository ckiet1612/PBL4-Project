# Contract baseline: API, domain và state

Dẫn xuất từ [PLAN.md](../PLAN.md) §3–§5, §7–§10, §13–§14; **PLAN được ưu tiên nếu có mâu thuẫn**. Đây là contract baseline bằng văn bản cho B01, không phải OpenAPI, DDL hay contract mới đã duyệt độc lập. B01 cụ thể hóa wire schemas/signature và B05 triển khai constraints/migrations; setup không tự chọn field/error/endpoint chưa được PLAN khóa.

## API `/v1`

| Nhóm | Contract đã khóa |
|---|---|
| Auth/catalog | Login/logout/session; template catalog và parameter schema theo quyền. PLAN chưa khóa URL chi tiết cho nhóm này; B01 xác định trong contract |
| Artifact | `POST /artifacts`: stream, checksum/size, idempotency; `GET /artifacts/{id}`: ownership. Nhận artifact ID, không nhận đường dẫn filesystem từ client |
| Job/session | `POST /jobs`; list/filter/keyset cursor; `GET /jobs/{id}`, `GET /sessions/{id}`; attempts/checkpoints/logs/events sau sequence |
| Control | `POST /jobs/{id}/{cancel,pause,resume,retry}` với idempotency và `If-Match`; pause/resume phụ thuộc capability |
| Admin | Tenant/user/membership, quota/weight có version; capacity/allocation của worker local, drain/disable; audit/fairness/recovery queries |
| Worker | Bootstrap local identity, heartbeat/poll; claim/start/renew/checkpoint/complete/cleanup có epoch/fence và dedup callback. Không có enrollment/phê duyệt nhiều máy |

Submit chỉ trả `202` sau khi input committed/template/compatibility/capacity/quota hợp lệ và transaction job/session/event/idempotency/counter đã commit. Worker offline nhưng pool đã cấu hình hợp lệ có thể nhận trong giới hạn và hiển thị `waiting_for_worker`; không có GPU cấu hình thì từ chối GPU request. Không giả định `waiting_for_worker` là một state mới ngoài state machine.

| Tình huống | Kết quả bắt buộc |
|---|---|
| Cùng idempotency key và payload | Trả lại cùng kết quả; replay trước kiểm `If-Match`, không tiêu thêm counter/token bucket |
| Cùng key, khác payload | `409` |
| Version không khớp | `412` cho request mới; replay hợp lệ vẫn trả kết quả gốc |
| Rate/quota số job vượt giới hạn | `429`, `Retry-After` |
| Queue đầy hoặc dependency không khỏe | `503 queue_full/dependency_unavailable`, `Retry-After` |
| Request không thể vừa hoặc sai capability | `422 infeasible_request` |
| Mọi lỗi | `code`, thông báo an toàn, `request_id`; không lộ secret hoặc dữ liệu tenant khác |

Idempotency scope: **tenant + principal + operation + key**; giữ khi job active và ≥30 ngày sau terminal. B01 định nghĩa schema lỗi/auth cụ thể còn lại theo yêu cầu ownership, không suy ra mã HTTP chưa được PLAN chỉ định. Một trang job/event tối đa 100; log theo byte/cursor giới hạn; dashboard aggregate giới hạn thời gian, không full queue download/offset pagination bảng lớn.

## Domain/data model logic

| Nhóm dữ liệu | Quan hệ, ownership và consistency cần giữ |
|---|---|
| Tenant/user/membership/role, session/token | Tenant là miền cách ly; membership/role/principal scope kiểm ở backend. CLI token lưu hash, có scope/expiry; browser session opaque server-side; worker credential riêng local identity |
| Template/input/spec | Admin allowlist image digest/adapter/version/schema; input committed và checksum; job spec/input/digest bất biến |
| Job/session/attempt | Một job có một session logic, nhiều attempt theo thời gian; tối đa một attempt có quyền/job; session state suy từ job |
| Retry lineage | Recovery/resume giữ job/session; manual retry từ failed job tạo job/session mới, `retry_of_job_id`; checkpoint hợp lệ kế thừa bằng reference |
| Allocation/GPU inventory | Resource vector CPU millicore/RAM byte/GPU nguyên chiếc theo UUID; quota/capacity tính tất cả allocation chưa release, gồm quarantine |
| Lease/epoch/fence | Coordinator leadership riêng; worker incarnation riêng; job fence tăng đơn điệu; không dùng coordinator epoch thay worker epoch/job fence |
| Quota/policy/admission/ledger | Policy version; global/tenant/user counters, durable rate buckets; dominant resource-time ledger và timeline không reset/double-charge |
| Idempotency/event/audit | Unique operation scope, sequence có thứ tự; state/event/counter/idempotency commit nguyên tử; callback dedup |
| Artifact/checkpoint/result/log | Tenant ownership; checkpoint/result/log gắn đúng attempt, input committed được tham chiếu qua ID; checksum/size/provenance/schema metadata trong DB; immutable committed blob trên filesystem; unique final result/job |

Mọi reference xuyên bảng có tenant ownership nhất quán, kể cả checkpoint kế thừa, parent/child sweep và download. B05 cần constraints/unique/composite reference, row lock/CAS và index phù hợp; không coi kiểm tra UI hoặc query trước transaction là đủ chống race.

## State machine

| Luồng | Chuyển trạng thái và guard |
|---|---|
| Execution | `QUEUED → DISPATCHING → RUNNING → SUCCEEDED/FAILED` |
| Automatic recovery | `DISPATCHING/RUNNING/PAUSING → RECOVERING → RETRY_WAIT → QUEUED`; vượt retry budget hoặc không restore an toàn thì `FAILED` |
| Pause/resume | `RUNNING → PAUSING → PAUSED → QUEUED`; chỉ PAUSED khi checkpoint committed **và** container dừng. Pause lỗi có thể về RUNNING nếu attempt còn hợp lệ, hoặc recovery giữ desired state paused |
| Cancel | Job chưa chạy → `CANCELLED`; attempt active → `CANCELLING → CANCELLED` sau cleanup. Cancel commit chặn completion đến sau |
| Terminal | `SUCCEEDED/FAILED/CANCELLED` bất biến; retry không hồi sinh terminal job |
| Session | Không có state machine thứ hai do client sửa |

Resume tạo attempt mới, không tiêu retry hạ tầng. Automatic recovery tối đa 2 retry sau attempt đầu; timeout/invalid input/OOM không auto retry cùng spec. Pause/resume không gia hạn runtime attempt đang chạy. Transition cụ thể phải đi cùng guard, desired state, job version và effect counter/allocation trong contract B01/B05; bảng này không cho phép bỏ bước cleanup/quarantine khi terminal.

## Internal interfaces

Các mô tả dưới đây là responsibility/input/output logic theo PLAN; chữ ký hàm và serialization thuộc B01.

| Interface | Đầu vào / trách nhiệm / đầu ra | Boundary |
|---|---|---|
| `SchedulerPolicy` | Snapshot capacity/quota/ledger/candidates và thời gian → quyết định deterministic, ordering/reservation/reason | Policy thuần; transaction commit do coordinator recheck, không import PyTorch |
| `ResourceProvider` | Host inventory CPU/RAM/GPU UUID/architecture/runtime/adapter → capacity/capability sau reserve | Discovery thực tế local; không bịa capacity từ queue |
| `Executor` | Authorized attempt/resource/deadline/identity → lifecycle container và bằng chứng cleanup | Worker-only Docker access; không tự công nhận final result hoặc release DB allocation |
| `WorkloadAdapter` | Validated spec/input và checkpoint hợp lệ → thực thi/checkpoint/result/provenance | Công bố `checkpointable`, `restart_safe`, device/architecture/restore compatibility; scheduler không chứa ML logic |
| `ArtifactStore` | Bounded staging/stream, checksum/size → durable immutable blob; read/reference/GC | Filesystem operations không thay authorization hoặc fenced metadata transaction |

## Workload và compatibility

CPU adapter lưu step/accumulator và phải cho kết quả exact so với run không crash. PyTorch CNN nhỏ dùng CIFAR-10 subset cố định có checksum/seed; checkpoint gồm model, optimizer, step/epoch, RNG Python/NumPy/PyTorch/CUDA khi dùng, data cursor/sampler, config, image digest, input/file checksum và schema version. Dùng tensor format an toàn + JSON, không pickle tùy ý. Tolerance ML phải ghi trước đo, không hứa bitwise identical giữa mọi device.

Sweep tối đa 100 child/request, mỗi child qua submit/idempotency/quota; parent không giữ execution slot và phải trả trạng thái nhận/từ chối từng child để replay an toàn. Chunked inference lưu cursor/output manifest, chunk ID deterministic để không công nhận trùng. Dataset chuẩn bị trước, read-only, workload không phụ thuộc Internet.

Contract/migration/image/adapter thay đổi phải được đối chiếu PLAN, [ADR](adr.md), [invariants](invariants.md), [acceptance](acceptance.md) và task phụ thuộc. Maintenance upgrade có backup DB/blob nhất quán và smoke trước mở ghi. Chỉ rollback migration theo điều kiện trước nhận ghi đã kiểm thử; snapshot cũ không bảo đảm rollback không mất dữ liệu sau khi nhận dữ liệu mới. Resume sau restore/relocation cần schema/input/checksum/adapter/image/architecture/framework/GPU-driver tương thích; thiếu capability phải chặn có reason, không fallback âm thầm.
