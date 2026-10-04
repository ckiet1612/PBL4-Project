# B19 Observability, storage limits và GC

Trạng thái: **bản cuối, khớp code B19 sau vòng sửa 2 (2026-10-05, RV01–RV11)**. Bản đầu (thiết kế trước khi
viết code) ghi lúc 2026-10-03T17:09Z; khác biệt so với bản đầu ghi ở
[evidence B19](evidence/B19-observability-storage.md) §2. Tài liệu này là nguồn mô tả cho
metric, log, readiness, alert, storage watermark/GC và giới hạn tài nguyên của
B19; contract vẫn là [concurrency-recovery](contracts/concurrency-recovery.md),
[state-machines](contracts/state-machines.md) và [openapi](contracts/openapi.yaml).

Nguyên tắc chung:

- PostgreSQL là nguồn sự thật. Không quyết định nào đọc lại từ metric; listener,
  collector hay Prometheus lỗi không đổi kết quả REST, tick, heartbeat hay callback.
- Metric sự kiện chỉ tăng sau commit (hook `after_commit` của session) hoặc khi đã
  quyết định trả lỗi. Lỗi trong code metric bị nuốt và đếm ở
  `nexa_metrics_internal_errors_total`.
- Không label nào mang ID (tenant, user, job, attempt, artifact, worker,
  incarnation, request), path thô hay message lỗi.

## 1. Danh mục metric

Thư viện: `prometheus-client`, mỗi process một `CollectorRegistry` riêng (không
dùng registry toàn cục, không bật process/platform collector mặc định). Khai báo
ở một module cho mỗi process: `nexa.observability.metrics_api`,
`metrics_coordinator`, `metrics_worker`; tập giá trị label đóng khai báo cùng chỗ.

Counter có label đóng được khởi tạo sẵn ở 0 cho mọi tổ hợp giá trị ngay khi import
module (`preinitialize`), nên lần tăng đầu tiên đã là `0 → 1` và `increase()`/`rate()`
trong alert thấy được nó (B19-RV03). Test quét `alerts.yml` để mọi counter dùng trong
`increase`/`rate` đều thuộc danh sách khởi tạo sẵn. Counter có label mở theo route
(`nexa_http_requests_total`, `nexa_worker_callback_rejected_total`) không dùng trong alert.

Cột "Nguồn": **E** = sự kiện (tăng khi xảy ra), **C** = collector đọc DB/storage
lúc scrape (cache), **G** = vòng GC/đối soát định kỳ ghi giá trị, **P** = trạng
thái trong process.

### 1.1 API (`service=api`)

| Metric | Loại | Đơn vị | Label (giá trị đóng) | Nguồn | Nhóm PLAN §9 |
|---|---|---|---|---|---|
| `nexa_http_requests_total` | counter | request | `route` (route template FastAPI hoặc `unmatched`), `method` (GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS, OTHER), `status_class` (1xx–5xx) | E | HTTP |
| `nexa_http_request_duration_seconds` | histogram | s | `route`, `method` | E | HTTP |
| `nexa_admission_total` | counter | request | `kind` (job, sweep), `outcome` (accepted, replayed, rejected), `reason` (ErrorCode hoặc `none`) | E | admission/reject reason |
| `nexa_storage_rejections_total` | counter | request | `reason` (high_watermark, critical_watermark, quota, enospc, unavailable), `operation` (admission, user_upload, worker_upload) | E | storage |
| `nexa_checksum_errors_total` | counter | lần | `operation` (upload, restore, download) | E | checksum errors |
| `nexa_worker_callback_rejected_total` | counter | request | `route` (route template `/v1/internal/*`), `code` (stale_authority, lease_expired, callback_replayed, state_conflict, version_conflict) | E | stale rejection |
| `nexa_retry_scheduled_total` | counter | lần | `reason` (FailureClass) | E | retry |
| `nexa_restore_total` | counter | lần | `outcome` (selected, fallback, none, failed) | E | restore |
| `nexa_upload_sessions_expired_total` | counter | session | – | E (G1) | storage |
| `nexa_gc_deleted_total` | counter | file | `kind` (staging, orphan) | E (G1/G2) | storage |
| `nexa_gc_bytes_deleted_total` | counter | byte | `kind` (staging, orphan) | E | storage |
| `nexa_gc_errors_total` | counter | lần | `kind` (staging, orphan, expire, reconcile) | E | storage |
| `nexa_gc_last_run_timestamp_seconds` | gauge | unix s | – | G | storage |
| `nexa_storage_used_ratio` | gauge | 0–1 | – | C (statvfs) | storage |
| `nexa_storage_free_bytes` | gauge | byte | – | C | storage |
| `nexa_storage_watermark_ratio` | gauge | 0–1 | `level` (high, critical) | P (config) | storage |
| `nexa_storage_committed_bytes` | gauge | byte | – (tổng mọi tenant) | C | storage |
| `nexa_storage_reserved_bytes` | gauge | byte | – | C | storage |
| `nexa_storage_staging_files` / `_bytes` | gauge | file / byte | – | G | storage |
| `nexa_storage_orphan_files` / `_bytes` | gauge | file / byte | – (lần quét G2 gần nhất, trước khi xóa) | G | storage |
| `nexa_storage_retained_unpublished_artifacts` / `_bytes` | gauge | artifact / byte | – | G | storage (R05) |
| `nexa_storage_counter_drift_tenants` | gauge | tenant | – | G | storage (R14) |
| `nexa_storage_reconcile_last_timestamp_seconds` | gauge | unix s | – | G | storage |
| `nexa_jobs` | gauge | job | `state` (QUEUED, DISPATCHING, RUNNING, PAUSING, PAUSED, RECOVERING, RETRY_WAIT, CANCELLING) | C | queue |
| `nexa_jobs_waiting` | gauge | job | `waiting_reason` (WaitingReason, 6 giá trị) | C | queue |
| `nexa_queue_oldest_age_seconds` | gauge | s (DB time) | – | C | queue/oldest age |
| `nexa_queue_outstanding` / `nexa_queue_outstanding_limit` | gauge | job | – (GLOBAL counter và global limit) | C | queue |
| `nexa_allocations` | gauge | allocation | `state` (HELD, QUARANTINED) | C | allocation |
| `nexa_allocated_resource` | gauge | đơn vị tài nguyên | `resource` (cpu_millis, memory_bytes, gpu) | C | allocation/GPU |
| `nexa_reservations_active` | gauge | reservation | – | C | reservation |
| `nexa_quarantine_oldest_age_seconds` | gauge | s | – | C | quarantine |
| `nexa_fairness_jain_index` | gauge | 1/n–1 | `window` (1h) | C | Jain |
| `nexa_fairness_dominant_resource_seconds` | gauge | s | `window` (1h) | C | dominant resource-time |
| `nexa_checkpoint_age_seconds` | gauge | s | – (lớn nhất trong attempt RUNNING/CHECKPOINTING có checkpoint interval; tính từ mốc muộn hơn giữa checkpoint mới nhất của job và lúc attempt bắt đầu, nên attempt resume không mang theo thời gian gián đoạn; B19-RV11) | C | checkpoint age |
| `nexa_workers` | gauge | worker | `health` (STARTING, READY, SUSPECT, UNAVAILABLE), `admin_state` (ENABLED, DRAINING, DISABLED) | C | worker |
| `nexa_worker_heartbeat_age_seconds` | gauge | s | – (lớn nhất) | C | worker |
| `nexa_operational_mode` | gauge | 0/1 | `mode` (NORMAL, ADMISSION_OFF, WRITE_FROZEN) | C | mode |
| `nexa_collector_up` | gauge | 0/1 | `collector` (queue, allocation, fairness, checkpoint, worker, storage) | P | collector |
| `nexa_collector_errors_total` | counter | lần | `collector` | P | collector |
| `nexa_collector_duration_seconds` | gauge | s | `collector` (lần làm mới gần nhất) | P | collector |
| `nexa_ready` | gauge | 0/1 | `check` (database, schema, storage) | P (readyz) | readiness |
| `nexa_metrics_internal_errors_total` | counter | lần | – | P | self |

`nexa_fairness_jain_index` chỉ có khi ít nhất một tenant được tính phí trong cửa sổ
1 h (Jain không xác định khi tổng bằng 0); `nexa_fairness_dominant_resource_seconds`
luôn có. Collector có engine riêng; mỗi lần làm mới đặt `statement_timeout` và
`lock_timeout` bằng `NEXA_METRICS_STATEMENT_TIMEOUT_MS`.

Collector lỗi: `collector_up=0`, tăng `collector_errors_total`, **bỏ series** của
collector đó tới lần làm mới thành công (không giữ giá trị cũ, để alert không dựa
trên số liệu đã cũ). Gauge `G` giữ giá trị lần chạy gần nhất kèm timestamp.

### 1.2 Coordinator (`service=coordinator`)

| Metric | Loại | Label | Nguồn | Nhóm |
|---|---|---|---|---|
| `nexa_coordinator_leader` | gauge 0/1 | – | P | scheduling |
| `nexa_coordinator_tick_duration_seconds` | histogram | – | E | scheduling latency |
| `nexa_coordinator_decisions_total` | counter | `outcome` (offer, reservation, invalidate, none) | E (sau commit) | scheduling |
| `nexa_coordinator_dispatch_wait_seconds` | histogram | – (từ lúc job đủ điều kiện tới khi offer commit, DB time) | E | scheduling latency |
| `nexa_lease_expired_total` | counter | – | E (reaper, sau commit) | lease |
| `nexa_coordinator_maintenance_failures_total` | counter | `step` (reap, promote, sweep, quota) | E | lease/retry |
| `nexa_coordinator_quota_blocked_total` | counter | `transition` (blocked, unblocked) | E (sau commit) | storage quota |
| `nexa_ready` | gauge | `check` (database, schema) | P | readiness |
| `nexa_metrics_internal_errors_total` | counter | – | P | self |

### 1.3 Worker (`service=worker`)

| Metric | Loại | Label | Nguồn | Nhóm |
|---|---|---|---|---|
| `nexa_worker_executions_total` | counter | `outcome` (SUCCEEDED, PAUSED, INFRASTRUCTURE, TIMEOUT, OOM, INVALID_INPUT, INCOMPATIBLE, INTERNAL) | E (kết quả worker đã gửi và API đã xác nhận) | execution |
| `nexa_worker_container_oom_total` | counter | – | E | OOM |
| `nexa_worker_container_memory_ratio_max` | gauge | – (max usage/limit trên container đang chạy, mẫu gần nhất) | P (sampler) | RAM |
| `nexa_worker_container_cpu_ratio_max` | gauge | – (max CPU dùng / CPU được cấp; ~1 = chạm quota) | P (sampler) | CPU throttle (proxy, R18) |
| `nexa_worker_loop_failures_total` | counter | `operation` (heartbeat, renew, reconcile, poll, ipc, result, stats) | E | renewal/heartbeat thất bại |
| `nexa_worker_ready` | gauge 0/1 | – | P | worker |
| `nexa_ready` | gauge | `check` (reconciled, heartbeat, docker) | P | readiness |
| `nexa_metrics_internal_errors_total` | counter | – | P | self |

`outcome` là FailureClass của báo cáo thất bại, hoặc SUCCEEDED/PAUSED. Vượt log
dừng runner với lý do FAILURE nên được đếm là INTERNAL; cancel mà runner tuân theo
cũng báo cùng đường đó. Worker không có outcome riêng cho LOG_OVERFLOW hay CANCELLED
(B19-R19, đề xuất ở evidence).

Sampler RAM/CPU (R18): mỗi 30 s một lần `docker stats --no-stream` (timeout 5 s)
cho tối đa 8 container của attempt worker đang theo dõi, ngoài mọi transaction.
Docker CLI không có số liệu throttle (`nr_throttled`); B19 dùng tỉ lệ CPU/quota
làm proxy và ghi B19-R18.

GPU: chỉ allocation từ DB (`nexa_allocated_resource{resource="gpu"}`); utilization
thuộc B23.

## 2. Ngân sách cardinality

Series không phụ thuộc số tenant/job/attempt/worker; chỉ phụ thuộc số route
template (71 route `/v1` + `unmatched`) và tập enum.

| Process | Thành phần lớn nhất | Trần |
|---|---|---|
| API | HTTP histogram ≤ 72 route × 14 series; counter ≤ 72 × 5 status class; admission 2×3×25; còn lại < 150 | ≤ 2 500 |
| Coordinator | 2 histogram × 14, decisions 4, maintenance 4 | ≤ 100 |
| Worker | executions 8, loop 7, ready 3 | ≤ 60 |

Test: số series sau kịch bản 50 tenant × 500 job bằng số series sau 1 tenant × 1
job với cùng tập state/reason; allowlist label kiểm trên registry thật.

## 3. Log JSON

Module dùng chung `nexa.observability.logging` (stdlib, không thêm dependency),
gọi ở API (khi tạo app), coordinator và worker main. Mỗi dòng một object:

| Field | Ý nghĩa |
|---|---|
| `ts` | UTC RFC3339, mili giây |
| `level`, `service` (api, coordinator, worker), `logger`, `event` | event snake_case ổn định |
| `message` | tùy chọn, ngắn, đã qua redaction |
| `request_id`, `job_id`, `attempt_id`, `worker_id`, `tenant_id` | qua contextvars, chỉ khi có |
| `reason`, `code` | khi có |
| `exception` | khi có exception: kiểu, message và traceback trong một chuỗi, đã qua redaction |

Message là chuỗi JSON object (log có từ B15-R33) được gộp vào dòng; field khác
truyền qua `extra`. Định dạng `text` cho dev qua `NEXA_LOG_FORMAT=text`.

Event chính: `http_request` (method, route, status, duration_ms, request_id; không
query/header/body), `unhandled_exception`, `worker_callback_rejected`,
`mode_reopen_refused` (check fail), `artifact_store_unavailable` (mã nội bộ của store
khi trả 503 dependency_unavailable, §6.3), `readiness_storage_failed`, `gc_pass`,
`gc_pass_failed`, `gc_step_failed`, `storage_counter_drift`, `coordinator_*`,
`worker_loop_failed`, `worker_reconcile`, `worker_unavailable`,
`worker_local_cleanup_failed`, `ops_listener_started`, `ops_listener_bind_failed`,
`ops_request_failed`, `metrics_collector_failed`.

`method` trong `http_request` là nhãn đóng giống metric (`GET`, `HEAD`, `POST`, `PUT`,
`PATCH`, `DELETE`, `OPTIONS`, còn lại `OTHER`); method thô của client không bao giờ được
ghi (B19-RV01).

Redaction (filter trên mọi handler): bỏ key chứa `password`, `token`, `secret`,
`cookie`, `csrf`, `authorization`, `credential`, `database_url`, `dsn`; che mẫu
`Bearer …`, tiền tố token Nexa và `scheme://user:pass@` (password có thể chứa `/`). Trong
văn bản `key=value`/`"key": "value"`, giá trị trong nháy được che tới nháy đóng (kể cả
khoảng trắng, ký tự escape); giá trị `[...]`/`{...}` che tới ngoặc đóng (lồng 2 mức),
không có ngoặc đóng thì tới hết dòng; giá trị không nháy che tới khoảng trắng, `,`, `}`,
`]`, giữ tên scheme HTTP auth (`Basic`, `Digest`, `Negotiate`) rồi che phần sau (B19-RV10).
Mọi mẫu neo bằng look-behind và dùng quantifier possessive nên thời gian tuyến tính theo
độ dài văn bản (64 KiB < 50 ms trong test; B19-RV01).
Không log body, input, checkpoint, chunk, model.

Logger hạ mức: `httpx`, `httpcore` → WARNING; `uvicorn.access` tắt (bỏ handler,
`propagate=False`) vì middleware ghi `http_request`; `uvicorn.error` đi qua root
JSON. `NEXA_LOG_LEVEL` áp dụng cho cả 3 process, kể cả logger `uvicorn` và
`uvicorn.error` (uvicorn đặt chúng ở INFO trước khi app được import; B19-RV07).

## 4. Cổng vận hành, /livez và /readyz

`NEXA_OPS_BIND=host:port` bật một listener HTTP riêng (stdlib
`ThreadingHTTPServer` trong thread daemon; tối đa 4 request đồng thời, timeout
socket 5 s cho mỗi lần đọc/ghi, và hạn tổng 5 s để nhận xong dòng request + header kể từ
khi kết nối, quá hạn thì đóng socket để client gửi nhỏ giọt không giữ chỗ; B19-RV06). Không đặt = tắt (mặc định). Sai cú pháp → process từ chối khởi động.
Bind thất bại → process dừng với event `ops_listener_bind_failed` (fail closed:
operator đã yêu cầu observability, chạy tiếp âm thầm sẽ che lỗi). Chỉ `GET /livez`,
`/readyz`, `/metrics`; đường khác 404; không đọc body. Không nằm trong `/v1`,
OpenAPI hay Caddy.

| Process | /livez (200/503) | /readyz checks |
|---|---|---|
| API | vòng event loop ghi nhịp mỗi 1 s; quá 30 s → 503 | `database` (SELECT 1, statement_timeout 1 s), `schema` (đúng head), `storage` (`check_readiness`: fsync + dưới critical) |
| Coordinator | vòng chính (acquire/tick) ghi nhịp; quá 60 s → 503 | `database`, `schema`; JSON có `role` leader/standby, không làm fail |
| Worker | vòng heartbeat ghi nhịp mỗi lần lặp; quá 60 s → 503 | `reconciled`, `heartbeat` (heartbeat gần nhất được chấp nhận ≤ 30 s), `docker` (`docker version`, timeout 3 s) |

/readyz: `{"status": "ready"|"not_ready", "checks": {"<tên>": "ok"|"fail"|"skip"}}`,
cache ≤ 2 s; không message/path/DSN/secret. Chặn dispatch vẫn qua heartbeat/READY.

`nexa_ready{check}` không phụ thuộc việc ai gọi /readyz: mỗi lần phục vụ `/metrics`,
process làm mới readiness qua cùng cache (TTL 2 s, single-flight) rồi mới xuất số liệu,
nên Prometheus chỉ scrape `/metrics` vẫn thấy check fail là 0 và hồi phục là 1. Probe
ném exception → mọi `nexa_ready` của process về 0 (fail closed) và `/metrics` vẫn trả
số liệu (B19-RV02). `ok` và `skip` đều xuất 1.

## 5. Alert

File `deploy/prometheus/alerts.yml` (16 rule), test
`deploy/prometheus/tests/alerts_test.yml`. Ngưỡng bảo thủ cho single-node, không phải
kết quả benchmark. Kiểm tra bằng promtool trong image đã ghim
`prom/prometheus@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996`
(v3.5.0), không cần cài gì:

```
docker run --rm --network none --entrypoint /bin/promtool \
  -v "$PWD/deploy/prometheus:/rules:ro" -w /rules prom/prometheus@sha256:63805ebb… \
  check rules alerts.yml
... test rules tests/alerts_test.yml
... check config prometheus.yml
```

| Alert | Biểu thức (rút gọn) | for | Severity | Runbook |
|---|---|---|---|---|
| NexaQueueOldestAgeHigh | `nexa_queue_oldest_age_seconds > 1800` | 10m | warning | Xem `nexa_jobs_waiting` theo reason; worker READY? quota/capacity? |
| NexaQueueDepthHigh | `nexa_queue_outstanding / nexa_queue_outstanding_limit > 0.9` | 10m | warning | Sắp 429 queue_full; xem tenant bằng admin fairness/jobs. |
| NexaStorageHighWatermark | used ≥ watermark high | 5m | warning | Submit/upload user bị 503; dọn dữ liệu ngoài Nexa hoặc mở rộng ổ. |
| NexaStorageCriticalWatermark | used ≥ watermark critical | 1m | critical | Dispatch dừng, worker upload 503; mở rộng ổ ngay. |
| NexaWorkerNotReady | `sum by (job, instance) (nexa_workers{health="READY",admin_state="ENABLED"}) < 1` | 2m | critical | Xem log worker, /readyz worker, heartbeat. |
| NexaWorkerHeartbeatStale | `nexa_worker_heartbeat_age_seconds > 60` | 2m | warning | Worker không gửi heartbeat; kiểm process/network. |
| NexaCheckpointStale | `nexa_checkpoint_age_seconds > 7200` | 15m | warning | Attempt chạy lâu không checkpoint; kiểm adapter/upload. |
| NexaRestoreFailures | `increase(nexa_restore_total{outcome="failed"}[30m]) > 0` | 0m | warning | Xem event CHECKPOINT_RESTORE_UNAVAILABLE của job. |
| NexaAllocationQuarantinedTooLong | `nexa_quarantine_oldest_age_seconds > 900` | 5m | warning | Container chưa có cleanup proof; kiểm worker reconcile. |
| NexaNotReady | `min by (job, instance) (nexa_ready) == 0` | 2m | critical | /readyz chỉ check fail. |
| NexaTargetDown | `up{job=~"nexa-.*"} == 0` | 2m | critical | Process hoặc listener ngừng. |
| NexaMetricsCollectorDown | `nexa_collector_up == 0` | 5m | warning | Log `metrics_collector_failed`; DB chậm/khóa. |
| NexaStorageCounterDrift | `nexa_storage_counter_drift_tenants > 0` | 15m | warning | Chạy `nexa-maintenance storage-check`; không tự sửa. |
| NexaGcStalled | `nexa_gc_last_run_timestamp_seconds > 0 and time() - nexa_gc_last_run_timestamp_seconds > 7200` | 10m | warning | Vòng GC không xong quá 2 h; log `gc_pass`, `nexa_gc_errors_total`. |
| NexaGcNeverCompleted | `nexa_gc_last_run_timestamp_seconds == 0` | 2h | warning | API chạy 2 h mà chưa xong vòng GC nào; log `gc_pass`, `nexa_gc_errors_total`. |
| NexaOrphanBacklog | `nexa_storage_orphan_bytes > 1073741824` | 1h | warning | G2 không dọn kịp hoặc lỗi; `nexa_gc_errors_total`. |

### 5.1 Bật scrape

1. Đặt `NEXA_OPS_BIND` cho từng process, chỉ trên loopback hoặc mạng quản trị
   (ví dụ API `127.0.0.1:9464`, coordinator `127.0.0.1:9465`, worker
   `127.0.0.1:9466`). Không đưa cổng này qua Caddy; Caddy không proxy `/metrics`.
2. Dùng `deploy/prometheus/prometheus.yml` (scrape 15 s, khớp cache collector 15 s),
   sửa target theo bind thật. Topology Compose/mạng quản trị thuộc B21.
3. Kiểm tra: `up{job=~"nexa-.*"} == 1` cho 3 job, `/readyz` của mỗi process trả 200.

Harness kiểm chứng (`scripts/b17_e2e_stack.py`, chỉ cho test): `--ops` bật 3 listener
trên loopback; `--prometheus` (W2, kéo theo `--ops`) thêm Prometheus đã ghim digest, sinh cấu hình
trong thư mục state với scrape/evaluation 5 s; `--alert-for 20s` sinh bản rule có mọi
`for:` đổi thành 20s (file trong repo giữ nguyên); `--api-env KEY=VALUE` chỉ nhận key
`NEXA_METRICS_*`, `NEXA_GC_*`, `NEXA_STORAGE_*`, `NEXA_LOG_*`. Sau lệnh con, nếu có ID
job đã nhận (`accepted-ids.txt`), harness chạy `nexa-maintenance consistency-check
--accepted-ids` rồi `storage-check` và trả exit code xấu nhất.

## 6. Storage

### 6.1 Watermark (u = tỉ lệ dùng đĩa của artifact root)

| Thao tác | u < high | high ≤ u < critical | u ≥ critical |
|---|---|---|---|
| submitJob/createSweep (sau auth và replay) | nhận | 503 storage_pressure + Retry-After | 503 storage_pressure |
| upload của user | nhận | 503 storage_pressure | 503 storage_pressure |
| upload của worker | nhận | nhận (R06) | 503 storage_pressure |
| dispatch | chạy | chạy | dừng (heartbeat → worker STARTING → coordinator không dispatch) |
| đọc/download/restore, cancel, cleanup, recovery | chạy | chạy | chạy |
| /readyz API | ready | ready | not_ready |

statvfs ở admission cache ≤ 1 s/process; lỗi statvfs → 503 dependency_unavailable.
Trong stream: kiểm lại mỗi 1 MiB đã ghi theo ngưỡng của thao tác (dùng cache 1 s).

### 6.2 Byte quota

Admission: `committed_bytes + reserved_bytes ≥ NEXA_TENANT_ARTIFACT_QUOTA_BYTES` →
429 quota_exceeded (đọc không khóa, sau replay). Dispatch: snapshot tính
`artifact_quota_available` cho mỗi tenant; policy chỉ nhận boolean; job QUEUED của
tenant hết quota có `waiting_reason=waiting_for_quota`, đặt/gỡ theo lô 256 job
(`SKIP LOCKED`, tăng `version`), không ghi event vì tập event type đã đóng và reason chỉ
là dẫn xuất; chuyển trạng thái đếm ở `nexa_coordinator_quota_blocked_total`. Upload vẫn
là nơi giữ chỗ chính xác.

### 6.3 Lỗi và mã trên wire

payload_too_large → payload_too_large 413; checksum_mismatch → checksum_mismatch 422;
size_mismatch, validation_failed → validation_failed 422; ENOSPC/EDQUOT (storage_full)
→ storage_pressure 503 + Retry-After, đếm `nexa_storage_rejections_total{reason="enospc"}`.
Mọi mã nội bộ khác (storage_unavailable, not_found, OSError khác, …) →
dependency_unavailable 503 + Retry-After: 1; client không thấy mã nội bộ, còn log ghi đúng
một event `artifact_store_unavailable` (WARNING) với `code` nội bộ và `operation`,
không ghi message của store vì có thể chứa đường dẫn (B19-RV04).

### 6.4 GC

- **G1** (API, `NEXA_GC_INTERVAL_SECONDS`): `pg_try_advisory_lock`; `expire_uploads`
  (actor SYSTEM, `artifact.upload.expire`), staging cũ hơn TTL không thuộc handle
  đang mở, không thuộc session ACTIVE → xóa, audit tóm tắt `artifact.gc.staging`.
- **G2**: file trong `committed/<hex32>` (layout thật; không có thư mục `blobs/`) không
  có dòng `artifacts`, lstat regular file, trong root, mtime > orphan TTL. GC giữ
  `pg_advisory_xact_lock` độc quyền theo hash blob key → kiểm lại → unlink → fsync
  thư mục → audit `artifact.gc.orphan` → commit. Commit metadata upload giữ cùng
  khóa shared → stat blob → insert.
- Không làm (R05): không xóa metadata/checkpoint; đo `retained_unpublished_*`.
- Đối soát counter (R14): mỗi vòng GC và `nexa-maintenance storage-check`; chỉ đọc.

## 7. Giới hạn tài nguyên (worker)

| Key | Mặc định | Khoảng | Áp dụng |
|---|---|---|---|
| `NEXA_CONTAINER_PID_LIMIT` | 512 | 32–32768 | `--pids-limit` |
| `NEXA_SCRATCH_MAX_BYTES` | 2 GiB | ≥ 64 MiB | tmpfs `/tmp` size = min(scratch_max, memory//4), luôn < memory |
| `NEXA_ATTEMPT_LOG_MAX_BYTES` | 100 MiB | 1 MiB–10 GiB | runner `--log-limit`, supervisor `--log-limit`; json-file `max-size` = min(giá trị này, 1 MiB) vì file đó chỉ chứa thông điệp ngắn của runner |

Vượt log: fail (LOG_OVERFLOW → FAILURE), không truncate (R15).

## 8. Config key mới và đề xuất bổ sung contract

| Key | Process | Mặc định | Khoảng |
|---|---|---|---|
| `NEXA_OPS_BIND` | API, coordinator, worker | tắt | `host:port`, port 1–65535 |
| `NEXA_LOG_FORMAT` | cả 3 | json | json, text |
| `NEXA_LOG_LEVEL` | cả 3 (trước chỉ API) | INFO | như cũ |
| `NEXA_GC_INTERVAL_SECONDS` | API | 60 | 5–3600 |
| `NEXA_METRICS_CACHE_SECONDS` | API | 15 | 1–300 |
| `NEXA_METRICS_STATEMENT_TIMEOUT_MS` | API | 2000 | 1–10000 |
| `NEXA_TENANT_ARTIFACT_QUOTA_BYTES` | coordinator (đã có ở API) | như API | như API |

Contract key đã có nhưng thiếu trong code: `container_pid_limit`,
`scratch_max_bytes`, `attempt_log_max_bytes` (mục 7). Đề xuất bổ sung contract
(owner duyệt): thêm các key trên vào bảng giới hạn của concurrency-recovery.md.
