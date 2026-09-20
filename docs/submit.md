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
được giữ khi Job active; B15 chịu trách nhiệm terminal transition, gia hạn theo thời điểm
terminal và sweep không xóa record còn active.

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
B15 tiếp nối terminal lifecycle, control và retention. Sweep và workload execution vẫn là
B16; B08 không chứng minh job đã chạy hay có kết quả.
