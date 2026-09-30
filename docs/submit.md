# B08 submit và durable queue

B08 nhận một `JobSubmitRequest` đã xác thực, kiểm tra artifact/template/spec và ghi
`Job`, `LogicalSession`, immutable `JobSpec`, các `JOB_SPEC` reference cho input/model,
event đầu tiên, audit, admission counters, durable rate buckets và completed idempotency
snapshot trong cùng một transaction PostgreSQL. API chỉ trả `202` sau commit thành công.
Không có `Attempt`, `Allocation` hay `Lease` ở bước này; job bắt đầu ở `QUEUED`,
`desired_state=RUNNING`, `version=1`, `job_fence=0`, `event_sequence=1`.

## Authorization và validation

`JobService` revalidate principal trong transaction, kiểm tra membership tenant và scope
chính xác (`jobs:write` cho submit, `jobs:read` cho query). Browser mutation đi qua
Origin/CSRF của B06; CLI bearer không được dùng để bypass membership. Artifact phải cùng
tenant và `COMMITTED`. Compatibility v1 fail closed theo template: CPU iterative nhận đúng
input media riêng, CIFAR-10 nhận dataset Arrow, và batch inference nhận dataset Arrow cùng
model `application/octet-stream`; version chưa có rule bị từ chối. Pydantic strict models
kiểm tra discriminator, UUIDv7, bounds và unknown fields; immutable template
`parameter_schema` cùng đầy đủ resource/runtime/checkpoint bounds được kiểm tra kiểu và giá
trị trước khi ghi. Metadata template null, sai shape, thiếu field hoặc sai kiểu trả
`422 infeasible_request`, không trở thành submit không giới hạn hoặc lỗi `500`.

Không có worker inventory vẫn cho phép CPU job vào queue với `waiting_for_worker`. GPU
request không có inventory/capability phù hợp bị từ chối `422 infeasible_request`; worker
đã cấu hình nhưng không ở trạng thái `READY` vẫn để job chờ. Resource admission không tạo
attempt concurrency hay allocation.

## Transaction và lock order

Transaction dùng `run_transaction` với tối đa ba lần retry cho serialization/deadlock.
Thứ tự là idempotency scope, current global policy, current tenant policy, global/tenant/
user counters, tenant/user rate buckets, rồi template/artifact/inventory reads và các
insert Job/Session/Spec/Event/Audit. Không gọi Docker, filesystem hay network trong body.
Các counter và bucket first-use được insert-on-conflict rồi khóa `FOR UPDATE`; token refill
dùng DB timestamp, Decimal exact text, chặn elapsed âm và không vượt burst. Một accepted
submit tăng mỗi rate bucket version một lần và counters một lần.

## Idempotency và durable query

Scope là `tenant_id + principal_id + operation_id + Idempotency-Key`; hash dùng JCS của
operation/path/tenant/body. Replay được authorize lại trước lookup, sau đó trả nguyên
status/body/Location/ETag đã lưu mà không chạy admission, refill, counter hay event lần nữa.
Cùng key khác payload trả `409 idempotency_conflict`; pending row có lock timeout hữu hạn và
trả `409 idempotency_in_progress` cùng `Retry-After: 1`.

`NEXA_IDEMPOTENCY_TERMINAL_RETENTION_DAYS` có default và minimum 30 theo contract. Upper bound
được tính từ khoảng thời gian còn biểu diễn được đến `datetime.max`, nên cấu hình làm expiry
vượt miền `datetime` bị từ chối lúc load thay vì làm submit phát sinh `OverflowError`. Submit
dùng giá trị này cho expiry ban đầu thay vì constant nội bộ. Record liên kết resource vẫn phải
được giữ khi Job active. **B15 đã triển khai (chờ Task Review):** mọi terminal transition nâng
`expires_at` của các record `resource_id` thuộc Job lên `terminal_at + retention` trong cùng
transaction; coordinator leader mỗi giây xóa tối đa 100 record `COMPLETED` đã hết hạn của
`submitJob`, `cancelJob`, `pauseJob`, `resumeJob`, `retryFailedJob` chỉ khi Job đã terminal
(`FOR UPDATE SKIP LOCKED`, không ghi khi `WRITE_FROZEN`). Record `PENDING`, operation khác và
record của Job còn active không bị xóa. Xem [coordinator](coordinator.md#b15-reaper-retry-promotion-and-retention-sweep).

`GET /v1/jobs` dùng keyset `(created_at DESC, job_id DESC)` với cursor ký, TTL và binding
actor/tenant/filter. Cursor phải chứa timestamp có timezone và UUIDv7. Job/session/event
đều tenant-scoped; object tenant khác trả `404` sau authorization. Events đọc theo
`sequence ASC` và `after_sequence`.

PostgreSQL là accepted-ID ledger: một Job ID chỉ được coi là accepted khi đồng thời có
Job, Session, Spec, committed input/model `JOB_SPEC` references, event sequence 1, counter
effect và completed idempotency snapshot. Các reference này giữ artifact khỏi B07 GC trong
suốt thời gian JobSpec còn tham chiếu. Cache/API process không phải nguồn sự thật. Integration
test làm process thoát tại `http.response.start` sau commit nhưng trước khi client nhận response;
client thấy transport failure, còn API process mới dựng lại service/connection từ cùng DB và
replay snapshot durable khi retry cùng key.

## Handoff

B11 dùng Job/Session/Spec/reference/event/counter đã accepted để dispatch và vẫn cần B10
worker readiness. B12 dùng REST chung cho CLI; B13 nối queue với production fairness/ledger;
B15 tiếp nối terminal lifecycle, control (cancel/pause/resume/manual retry) và retention sweep
idempotency. B16 bổ sung parameter sweep và workload AI (hai mục dưới). B08 không chứng minh
job đã chạy hay có kết quả.

## Workload AI (B16; đã triển khai, chờ Task Review)

Template được đăng ký bằng `nexa-maintenance register-template` (xem [CLI](cli.md)). Người dùng
xem template qua `GET /v1/templates` và `nexa template list|show`. Có hai template AI, cả hai CPU
và `checkpointable`. Cả hai có `restart_safe = true`, CPU 1000–8000 millicore, RAM
1 GiB–8 GiB, `gpu_count = 0`, runtime tối đa 300 s và checkpoint interval 5–60 s.

**`pytorch-cifar10-cnn` v1** (adapter `pytorch.cifar10` 1.0.0)
- Input: `DATASET` `application/vnd.apache.arrow.file`. Đây là Arrow IPC file có metadata
  JSON trong schema (B16-R02).
- Tham số: `epochs` 1–100, `batch_size` 1–512, `learning_rate` ≤ 1, `seed`
  0–2147483647, `subset_size` 100–50000.
- Result: `model.safetensors` (`application/octet-stream`) và `metrics.json`
  (`application/json`).

**`batch-inference` v1** (adapter `batch.inference` 1.0.0)
- Input: `DATASET` Arrow như trên, cộng `MODEL` `application/octet-stream`
  (safetensors).
- Tham số: `chunk_size` 1–100000, `batch_size` 1–4096, `output_format`
  `JSONL|PARQUET`.
- Result: `summary.json` và các chunk `chunk-%08d.jsonl`
  (`application/x-ndjson`) hoặc `chunk-%08d.parquet`
  (`application/vnd.apache.parquet`) đã được nhận diện. Mỗi chunk chỉ được
  công nhận một lần cho mỗi job.
- Giới hạn: tối đa 2048 chunk và mỗi file chunk ≤ 1 MiB − 16 KiB (B16-R17).
  Tham số sinh vượt giới hạn làm attempt fail `INVALID_INPUT` (workload thoát
  65 trước khi ghi chunk nào, không retry). Workload vượt giới hạn memory của
  container fail `OOM/CONTAINER_OOM`, cũng không retry.

Submit vẫn đi qua `submitJob` như trên. Media type sai với template trả
`422 infeasible_request`. Job chờ worker có image, adapter và framework khớp
capability của template (xem [coordinator](coordinator.md)). Image PyTorch
được build từ `deploy/pytorch-cpu/` (`scripts/b16_build_image.sh`) và không
được push lên registry.

**Khuyến nghị RAM (số đo trên VPS1, amd64).** Đây là số đo để chọn `memory_bytes`, không
phải acceptance. Mỗi dòng chạy workload trực tiếp (không có runner) trong container hardened,
1 CPU và 1 thread, rồi đọc `memory.peak` của cgroup
(`docs/evidence/raw/B16-ram.json`).

| Cấu hình | RAM đỉnh | Thời gian |
|---|---|---|
| training `batch_size` 64, `subset_size` 5000, 1 epoch | 316 MiB | 4,9 s |
| training `batch_size` 512, `subset_size` 5000, 3 epoch | 409 MiB | 8,7 s |
| inference `chunk_size` 50, `batch_size` 50, N = 2000 | 242 MiB | 2,8 s |
| inference `chunk_size` 2000, `batch_size` 2000, N = 2000 | 506 MiB | 3,0 s |

Khi chạy qua worker (có runner và checkpoint), cấu hình fixture training (`batch_size` 64,
`subset_size` 5000, 30 epoch) có RAM đỉnh 320–384 MiB ở các container D1–D3/D7. Mức tối
thiểu của template (1 GiB) đủ cho mọi cấu hình đã đo và vẫn còn dư.
Chưa đo (not-run):
- `subset_size` 50000, vì fixture chỉ có 5000 mẫu training;
- `batch_size` inference 4096, vì dataset fixture chỉ có N = 2000.

Với cấu hình lớn hơn, hãy đo lại trước khi hạ `memory_bytes`.

## Parameter sweep (B16; đã triển khai, chờ Task Review)

`POST /v1/sweeps` (`nexa sweep submit`) nhận `base_spec` (một `JobSubmitRequest` không kèm
tham số sweep) và `dimensions` (tên tham số của template con, danh sách giá trị).

- **Request-level.** Tên dimension không được trùng. Giá trị trong một dimension
  cũng không được trùng theo dạng RFC 8785 (`uniqueItems`): `1e-2`, `0.010` và
  `0.01` là một giá trị, còn `1`, `true` và `"1"` là ba giá trị. Request có tên
  hoặc giá trị lặp bị từ chối `422 validation_failed` (remediation B16-R10, chờ
  Task Review; thay đổi breaking: B16 bỏ trùng một cách im lặng). Tích số giá trị
  phải ≤ 100 và được tính bằng phép nhân, không dựng danh sách. Mỗi giá trị phải
  hợp lệ với schema của template con, và template phải enabled. Lỗi ở mức này
  trả `422` và không ghi gì. Sweep đã lưu trước quy tắc này vẫn replay nguyên
  trạng với cùng key; chỉ request mới bị kiểm tra.
- **Parent.** Parent được ghi cùng idempotency record và response `207` có
  `sweep_id`/`Location`.
- **Children.** Mỗi child được admit trong một transaction riêng, qua đúng core
  `submitJob`: authorization, ownership input, resource bounds, admission, quota
  và rate. Child dùng key suy ra từ `(sweep_id, child_index, parameter_hash)`.
  Outcome ACCEPTED (có `job_id`) hoặc REJECTED (có snapshot lỗi, ví dụ
  `quota_exceeded`) là bất biến.
- **Mất response.** Replay cùng key trả nguyên response đã lưu và chỉ tiếp tục
  các child chưa có outcome. Không tạo job trùng và không đổi counter.

Parent không nhận allocation hay slot. `GET /v1/sweeps/{id}` (`nexa sweep show`)
trả số child, accepted/rejected và từng trang child. Các lỗi abort được liệt kê ở
B16-R04. Quy tắc retention của `submitSweep` được liệt kê ở B16-R05. Xem
[B16 evidence](evidence/B16-pytorch-sweep-inference.md).
