# B19 — Metrics/log/audit, storage watermark/GC và resource bounds: evidence

Trạng thái: **vòng sửa 2 đã triển khai, chờ Task Review độc lập** (Task Code, 2026-10-05). Review
vòng 1 (2026-10-04) Không duyệt; RV01–RV11 và verification vòng 2 ở **§23**; §1–§22 là vòng 1,
số liệu ở đó thuộc cây vòng 1 (`16959afc…`). Baseline
`42502c8`, cây sạch lúc bắt đầu. Danh mục observability ở
[docs/observability.md](../observability.md): bản đầu ghi lúc **2026-10-03T17:09:11Z**, trước mọi
code B19; bản cuối khớp code (§2 ghi khác biệt). `docs/acceptance.md` giữ nguyên `specified`.
Mọi kết quả dưới đây chạy trên Mac (P, Docker Desktop arm64) trong phiên này; VPS1 không dùng (L =
not-run).

## 1. Kế hoạch

### 1.1 Context map

| Yêu cầu | Contract | Code có sẵn | Test có sẵn | Thiếu |
|---|---|---|---|---|
| Metrics PLAN §9 | PLAN :253–260; ACC-26 | không có metric sản phẩm; `benchmarks/simulator/metrics.py` chỉ là simulator | – | toàn bộ; dependency `prometheus-client` |
| Log JSON | PLAN :257; ACC-26/27 | `_log_rejected_callback` (app.py:52, JSON trong message), `_log_reconciliation` (agent.py:360); `basicConfig` text ở coordinator/worker main; `NEXA_LOG_LEVEL` không dùng; uvicorn access log có query | `test_rem_b15_r33_rejection_log.py`, `test_rem_b15_r33_reconcile_log.py` (parse `getMessage()` là JSON) | module dùng chung, redaction, `http_request`, `unhandled_exception`, canary |
| Readiness/livez | PLAN :274; SM :113 | `check_readiness` (store.py:571), `require_current_schema`, heartbeat storage check (worker_service.py:757) | heartbeat/readiness tests B10 | listener riêng, /readyz 3 process |
| Reopen ADMISSION_OFF→NORMAL | SM :108–117 | `FailClosedRecoveryProofProvider` (policy_service.py:42); `_require_enable_ready` (admin_workers.py:509) | `test_policy_service.py`, `test_policy_races.py`, `test_rem_b15_obs02_admission_off.py`, `test_policy.py` (provider cho phép tất cả) | provider thật; copy UI (modes.ts) |
| Watermark | concurrency-recovery :219–226; PLAN :239 | `_check_disk` chỉ high watermark, cả worker; statvfs trong transaction; `append` OSError → `storage_unavailable` | B07 artifact tests | ngữ nghĩa 6.B, cache 1 s, mid-stream, ENOSPC |
| Byte quota | ACC-23 | quota ở upload (artifact_service.py:330) | B07 | admission 429; dispatch `waiting_for_quota` |
| Mã lỗi wire | openapi :1424 | `storage_unavailable`, `size_mismatch` lọt ra wire (:170, :432, :502) | – | ánh xạ + test enum |
| Header/body | openapi :245, :1241, :1387 | user upload đã so Content-Length (service); worker route thiếu `le`; JSON 413 (dependencies.py:39) chưa có test | – | `le` 1 TiB, test 413/422/chunked |
| GC G1 | CR :48, :73–76 | `expire_uploads` không được gọi lúc runtime; `_active` handle của store | B07 expire test | vòng nền, audit SYSTEM, staging sweep |
| GC G2 | CR :48, :55, :75–76 | `delete_unreferenced` + `issue_gc_token` (token trong memory, chỉ cho blob trùng) | B07 | quét orphan + khóa advisory chung với commit |
| Đối soát/consistency | ACC-23/26 | `artifact_storage_counters` | – | `storage-check`, `consistency-check` |
| Giới hạn PID/scratch/log | CR :170–193 | cứng: pid 128 (docker_config.py:135), scratch min(64 MiB, mem//4), log 1 MiB (dispatch.py:219, adapter_dispatch.py:452) | `tests/docker/test_real_runner.py:943–1066, :1212–1260` | env worker, validate, Docker test với giá trị không mặc định |
| Audit actor | ACC-26 | commit upload luôn `USER` (B18-R24) | – | WORKER/SYSTEM |
| Thư mục local worker | – | `downloads/<attempt>` (execution.py:187), `materialized/<attempt>` (journal.py:91), không có rmtree | – | dọn sau cleanup đã xác nhận (R17) |

Sai lệch prompt/code đã xác minh: blob nằm ở `committed/<hex32>` (store.py), không có thư mục
`committed/blobs/`; G2 làm theo layout thật. Thời hạn upload session = `staging_ttl_seconds`
(artifact_service.py:359), nên G1 chỉ xóa staging cũ hơn `staging_ttl`, không thuộc handle đang
mở và không thuộc session ACTIVE.

### 1.2 Quyết định và lý do

| # | Quyết định | Lý do |
|---|---|---|
| D1 | `prometheus-client` 0.26.0 (dependency duy nhất được thêm), registry riêng mỗi process, không bật process/platform collector | Exposition chuẩn, counter/histogram thread-safe; tự viết sẽ phải tự lo escape, histogram và text format. Registry riêng để không có series ngoài ngân sách |
| D2 | Listener stdlib `ThreadingHTTPServer` trong thread daemon, semaphore 4 (vượt → 503), timeout socket 5 s, chỉ GET `/livez` `/readyz` `/metrics` | Không chặn event loop, không thêm dependency, dùng chung cho 3 process |
| D3 | Bind thất bại → process dừng (`ops_listener_bind_failed`) | Operator đã yêu cầu observability; chạy tiếp âm thầm che lỗi. Lỗi lúc chạy (scrape) không ảnh hưởng REST/tick/heartbeat |
| D4 | Metric sự kiện tăng qua hook `after_commit` của session (`session.info`), bỏ khi rollback/savepoint rollback; lỗi bị nuốt và đếm | "Chỉ sau commit" đúng cả khi `run_transaction` retry |
| D5 | Collector API: engine riêng `pool_size=1, max_overflow=0`; mỗi lần làm mới một transaction READ ONLY với `SET LOCAL statement_timeout` và `lock_timeout` = `NEXA_METRICS_STATEMENT_TIMEOUT_MS` (mặc định 2000); cache TTL `NEXA_METRICS_CACHE_SECONDS` (15, khớp scrape 15 s), single-flight; lỗi → bỏ series, `collector_up=0` | Không làm cạn pool REST; không giữ số cũ để alert không dựa trên dữ liệu cũ |
| D6 | Readiness proof (`ReadinessRecoveryProofProvider`): probe storage (fsync + dưới critical) **trước** transaction; trong transaction đổi mode đọc lại schema, worker `FOR SHARE` và DB time; dùng chung `worker_readiness_failure` với `_require_enable_ready` | Không TOCTOU giữa đọc worker và commit mode; không chép code; Docker/statvfs không nằm trong transaction |
| D7 | Mức storage ở admission/upload đọc trước transaction (cache 1 s/process), áp sau replay bên trong transaction; trong stream kiểm lại **mỗi 1 MiB** đã ghi | statvfs ngoài transaction; replay vẫn trả response cũ khi đĩa đầy; 1 MiB giới hạn lượng ghi vượt ngưỡng mà không gọi statvfs mỗi chunk (cache 1 s) |
| D8 | Byte quota ở dispatch: snapshot tính `artifact_quota_available` (bool) cho `TenantPolicySnapshot`; policy thuần trả lý do `artifact_quota_exhausted`; bước bảo trì coordinator `track_artifact_quota` đặt/gỡ `waiting_reason=waiting_for_quota` theo lô 256 (`SKIP LOCKED`, tăng `version`), **không ghi event**, bỏ qua khi WRITE_FROZEN, quét lại mỗi 30 s | Domain thuần; reason quan sát được; tập event type đã đóng nên không thêm event (sửa câu sai ở bản đầu, §2) |
| D9 | G2: `pg_advisory_xact_lock` độc quyền (class 1_919_001, hash blob key) → kiểm lại không có dòng `artifacts` → unlink → fsync thư mục → audit → commit; commit metadata upload giữ cùng khóa ở mức shared → stat blob → insert. Một vòng GC một lúc nhờ `pg_try_advisory_lock` (class 1_919_002) | Thực hiện CR :48/:75 cho orphan mà không cần bảng/migration mới |
| D10 | Chu kỳ GC: `NEXA_GC_INTERVAL_SECONDS` (60; 5–3600), mỗi vòng G1 → G2 → đối soát counter | Đủ nhanh để G1 nhả reservation hết hạn, rẻ (EXPLAIN §17) |
| D11 | Throttle/RAM: Docker CLI không có `nr_throttled`; sampler `docker stats --no-stream` mỗi 30 s, timeout 5 s, tối đa 8 container, ngoài transaction; tỉ lệ CPU dùng / CPU cấp làm proxy (R18) | Không mở đường truy cập socket Docker mới; có trần chi phí |
| D12 | Không tạo migration 0024 | EXPLAIN trên 100 k job / 200 k artifact: đường nóng dùng index sẵn có; seq scan chỉ ở lệnh vận hành toàn bảng ≤ 0,1 s (§17) |

### 1.3 Milestones

| M | Nội dung | File chính | Test | Trạng thái |
|---|---|---|---|---|
| M1 | Danh mục + kế hoạch | `docs/observability.md`, evidence này | – | xong (17:09Z) |
| M2 | Config key | `config.py`, `worker/main.py`, `.env.example` | `tests/test_config_b19.py`, `tests/worker/test_resource_limits_b19.py` | xong |
| M3 | Log JSON | `observability/logging.py`, `api/app.py`, `api/main.py`, coordinator/worker main | `tests/observability/test_logging_b19.py`, `test_api_logging_b19.py`, PG uvicorn thật | xong |
| M4 | Listener + readiness | `observability/{ops_server,probes}.py`, `api/ops.py`, `coordinator/ops.py`, `worker/ops.py` | `test_ops_server_b19.py`, `test_worker_ops_b19.py`, PG `test_ops_b19.py`, `test_coordinator_ops_b19.py` | xong |
| M5 | Metrics + collector | `observability/metrics*.py`, `fairness.py`, `api/collectors.py` | `test_metrics_b19.py`, PG collector | xong |
| M6 | Reopen NORMAL + UI copy | `application/readiness_proof.py`, `admin_workers.py`, `policy_service.py`, `modes.ts` | PG `test_readiness_proof_b19.py`, Vitest, W1-admin-mode, W2 `00-reopen` | xong |
| M7 | Mã lỗi, watermark, quota, ENOSPC, header/body | `storage_pressure.py`, `artifact_service.py`, `store.py`, `job_service.py`, `sweep_service.py`, `routes_worker.py`, `coordinator/{artifact_quota,service,snapshot}.py` | PG `test_storage_pressure_b19.py`, unit | xong |
| M8 | GC G1/G2, dọn thư mục worker | `application/storage_gc.py`, `api/app.py`, `worker/local_cleanup.py`, `agent.py` | PG `test_storage_gc_b19.py`, `test_gc_store_b19.py`, `test_local_cleanup_b19.py` | xong |
| M9 | storage-check, consistency-check | `application/storage_checks.py`, `cli/main.py` | PG `test_storage_checks_b19.py` | xong |
| M10 | Resource limits | `docker_config.py`, `dispatch.py`, `adapter_dispatch.py`, `executor.py`, `worker/main.py` | unit + Docker | xong |
| M11 | Alert, scrape, outage, Playwright | `deploy/prometheus/**`, `scripts/b17_e2e_stack.py`, `scripts/b19_*.py`, web e2e | promtool, scrape, outage, disk-full, W2 | xong |
| M12 | Docs, evidence, review | docs §9 | regression đầy đủ | xong |

## 2. Khác biệt giữa bản đầu và bản cuối của docs/observability.md

Bản đầu (17:09:11Z) được giữ ngoài repo để so; các thay đổi đều do test hoặc code buộc phải đổi:

| Mục | Bản đầu | Bản cuối | Lý do |
|---|---|---|---|
| 1.1 `nexa_checksum_errors_total{operation}` | upload, restore, download, chunk | upload, restore, download | Chunk upload không có checksum riêng trên wire; lỗi checksum chỉ phát hiện ở commit (upload) |
| 1.1 fairness/collector | – | ghi chú Jain chỉ có khi có tenant được tính phí; collector có engine riêng, đặt statement/lock timeout | Jain không xác định khi tổng 0 (test Hypothesis) |
| 1.3 `nexa_worker_executions_total{outcome}` | gồm CANCELLED, LOG_OVERFLOW; nguồn "worker báo" | bỏ CANCELLED/LOG_OVERFLOW, thêm PAUSED; nguồn "kết quả worker đã gửi và API đã xác nhận" | Worker không phân biệt được hai outcome đó (R19); chỉ đếm sau khi API chấp nhận để không đếm hai lần khi retry callback |
| 1.3 `nexa_worker_loop_failures_total{operation}` | 6 giá trị | thêm `result` (7) | Vòng gửi kết quả có retry riêng |
| 1.3 | – | đoạn R19 | như trên |
| 2 Cardinality | executions 9, loop 6 | 8, 7 | theo 1.3 |
| 5 Alert | 15 rule, chưa có lệnh promtool | 16 rule, lệnh promtool + digest | – |
| 5 NexaWorkerNotReady / NexaNotReady | không `by` | `sum/min by (job, instance)` | promtool test: tránh gộp series của nhiều target |
| 5 NexaGcStalled | `time() - last > 7200`, 30m | `last > 0 and time() - last > 7200`, 10m | Khi chưa có vòng nào (`last = 0`) rule cũ firing sai ngay khi khởi động |
| 5 NexaGcNeverCompleted | – | mới (`== 0`, 2h) | Phần "chưa bao giờ xong" tách khỏi NexaGcStalled |
| 5.1 Bật scrape | – | mới | Hướng dẫn và harness kiểm chứng |
| 6.2 Byte quota | "ghi event QUEUE_BLOCKED/QUEUE_UNBLOCKED" | lô 256, SKIP LOCKED, tăng version, **không event**, đếm ở `nexa_coordinator_quota_blocked_total` | Tập event type đóng (contract); reason chỉ là dẫn xuất. Câu bản đầu sai so với code, đã sửa |

## 3. Findings và interpretation (B19-Rxx)

| ID | Mô tả | Quyết định / nguyên nhân | Test / evidence | Status |
|---|---|---|---|---|
| R01 | Pipeline log của workload (= B17-R01) | Contract không có callback có fence để publish LogSegment, không header offset; runner bỏ output. Không làm trong B19 | Đề xuất §3.1; docs/web-ui.md A8, docs/database.md LogSegment | **hoãn**, chờ owner duyệt contract |
| R02 | `readiness_verified` thật cho ADMISSION_OFF → NORMAL (SM:113) | DB/schema head, storage (fsync, dưới critical, probe ngoài transaction), ≥1 worker, mọi worker ENABLED + READY + heartbeat ≤ 30 s + reconcile của incarnation hiện tại khớp snapshot + inventory hợp lệ. QUARANTINED không phải điều kiện. Message 409 giữ nguyên, không thêm `reason`; điều kiện fail ghi ở log `mode_reopen_refused`. `freeze_ready`/`restore_verified` vẫn False (B21) | PG `test_readiness_proof_b19.py` 7 test; W1-admin-mode; W2 `00-reopen.spec.ts` | **đóng** (P) |
| R03 | Cổng vận hành riêng | Mặc định tắt; không qua Caddy (Caddy `/metrics` trả SPA HTML 200, không phải exposition); compose không đổi | `test_b19_api_defaults_keep_ops_listener_off`, PG `test_api_ops_listener_ready_metrics_and_rest_isolation`, scrape `caddy_metrics.prometheus_text=false` | **đóng** |
| R04 | Label tập đóng | Giá trị lạ → `other`/`unmatched`; fairness chỉ Jain/DRT toàn hệ thống; số theo tenant vẫn ở adminQueryFairness | `test_closed_labels_map_unknown_values`, `test_registry_labels_are_allowlisted_and_never_ids`, scrape `id_labels=[]` | **đóng** |
| R05 | GC metadata/xóa bớt checkpoint | Trigger coi reference UPLOAD_SESSION là đang tham chiếu; B19 không xóa metadata/checkpoint, chỉ đo byte bị giữ (`nexa_storage_retained_unpublished_*`, `storage-check`) | Đề xuất §3.2; `test_storage_check_counts_bytes_retained_until_published` | **hoãn**, có số đo |
| R06 | Upload của worker nhận tới critical | Đổi hành vi so với B07 (trước đây worker bị chặn ở high) | `test_worker_upload_is_refused_only_at_critical_and_audited_as_the_worker` | **đóng** |
| R07 | Byte quota admission/dispatch | Admission: `committed + reserved ≥ quota` → 429 `quota_exceeded` (sau replay). Dispatch: D8. v1 không có cách tenant tự giải phóng dung lượng (không có API xóa artifact) | `test_submit_job_watermark_quota_and_replay`, `test_an_exhausted_tenant_gets_no_dispatch_and_a_derived_quota_reason`, `test_startup_clears_stale_reasons_and_write_frozen_writes_nothing`, scheduler unit 3 | **đóng**; giới hạn v1 ghi lại |
| R08 | `storage_unavailable`/`size_mismatch` lọt ra wire | Ánh xạ: dependency_unavailable 503, validation_failed 422, storage_pressure 503; mọi mã nội bộ khác cũng là dependency_unavailable 503 (vòng 1 ghi nhầm `internal_error`; vòng 2 sửa docs và thêm log `artifact_store_unavailable` có mã nội bộ, RV04) | `test_every_store_code_maps_to_a_published_error_code` (so với enum ErrorCode của OpenAPI) | **đóng** |
| R09 | Actor audit của upload do worker (= B18-R24) | Upload commit của worker ghi `WORKER`; của user ghi `USER`; GC/expiry ghi `SYSTEM` (`storage-gc`) | PG test R06 ở trên, `test_g1_expires_sessions_and_removes_only_stale_unowned_staging`; outage: `WORKER artifact.upload.commit` 16 | **đóng** |
| R10 | Config key mới ngoài contract | Ghi ở docs/observability.md §8 kèm đề xuất bổ sung contract; không sửa contract | – | **đề xuất**, chờ owner |
| R11 | compose/deploy không đổi | Coordinator trong compose không nhận `NEXA_TENANT_ARTIFACT_QUOTA_BYTES` nên dùng mặc định bằng mặc định của API; đổi quota ở API thì phải đặt cả ở coordinator (`.env.example`). Mạng quản trị/Prometheus trong Compose thuộc B21 | `test_coordinator_quota_matches_api_default_and_bounds` | **ghi nhận** → B21 |
| R12 | Playwright chưa vào CI (B17-R14, B18-R11) | Không đổi ci.yml | – | **mở** → B25 |
| R13 | Phạm vi outage | Prometheus dừng, scrape dồn dập, collector lỗi bằng khóa bảng ở kết nối khác. Không mô phỏng DB down (ACC-21 phần khác) | §15 | **đóng** (phạm vi này) |
| R14 | Đối soát counter chỉ đọc | `storage-check` và vòng GC chỉ báo drift (metric + exit 1), không sửa | `test_reconcile_reports_counter_drift_without_repairing_it`, `test_storage_check_reports_drift_without_repairing_it` | **đóng** |
| R15 | Vượt log attempt = fail | LOG_OVERFLOW → FAILURE, không truncate; PID/scratch/log cấu hình trên worker | Docker `test_configured_attempt_log_bound_fails_the_attempt` | **đóng** |
| R16 | Audit không có retention | Đo: ≈ 7,25 dòng audit/job CPU thành công (§16). Đề xuất §3.3 | outage `audit_added` | **đề xuất**, chờ owner |
| R17 | Dọn thư mục local của worker | `downloads/<attempt>`, `materialized/<attempt>` chỉ bị xóa sau cleanup đã xác nhận (allocation RELEASED) và một lần quét hoàn chỉnh; không theo symlink; tên lạ/adopted bỏ qua; mỗi vòng có trần | `tests/worker/test_local_cleanup_b19.py` 5 test | **đóng** |
| R18 | Đo throttle/RAM | D11; trên Docker Desktop chỉ là P | `test_sampler_reports_largest_ratios_relative_to_granted_cpu`, `test_stats_sampler_failure_is_counted_and_never_blocks_readiness` | **đóng** với proxy; throttle thật (cgroup) cần L |
| R19 | Worker không phân biệt LOG_OVERFLOW/CANCELLED | Runner dừng với reason FAILURE → worker báo INTERNAL/WORKLOAD_EXIT_NONZERO; metric đếm theo FailureClass đã được API chấp nhận | `test_failure_outcome_counts_once_after_the_acknowledgement`, `test_completion_counts_only_an_accepted_acknowledgement`; đề xuất §3.4 | **ghi nhận**, đề xuất |
| R20 | (mới) Snapshot reconciliation đổi sau khi nhả allocation | Có từ trước (B15-R07): sau khi allocation RELEASED, heartbeat kế tiếp đưa worker về STARTING tới khi reconcile lại; trong thời gian đó poll trả 409 `state_conflict` và proof mở lại NORMAL từ chối (đúng thiết kế, fail closed). Đây cũng là nguyên nhân flake của kịch bản 17 B18. Không đổi code sản phẩm; `00-reopen.spec.ts` chờ allocation RELEASED rồi một heartbeat READY sau `released_at` | W2 `00-reopen` 6/6 × 2; outage: đúng 1 lần 4xx là `POST /v1/workers/{worker_id}/poll` | **ghi nhận** (không phải lỗi B19) |
| R21 | (mới) Canary cấp stack | Test canary PG chỉ phủ API (password, bearer, cookie, query). Bổ sung quét canary trong harness sau mỗi run B19 trên log thật của API, coordinator và worker | §9 | **đóng** |
| R22 | (self-review) `has_blob` ném `ArtifactError` khi commit metadata | Volume lỗi giữa `commit_blob` và commit metadata → trước đây 500 `internal_error`, idempotency PENDING, reservation giữ tới G1. Sửa: không kiểm tra được = thiếu → 503 `dependency_unavailable`, nhả reservation, blob (nếu có) để G2 | PG `test_a_blob_that_cannot_be_inspected_at_metadata_commit_fails_closed_and_releases` (đỏ trước sửa, xanh sau) | **đóng** |
| R23 | (self-review) Redaction văn bản `key=value` | Giá trị trong nháy có khoảng trắng (`password="a b"`) chỉ bị che từ đầu tới khoảng trắng; `password=a;b` lộ phần sau `;`; password URL chứa `/` không bị che. Sửa regex: giá trị trong nháy che tới nháy đóng (có escape), giá trị trần che tới khoảng trắng/`,`/`}`/`]`, password URL cho phép `/`. Field có cấu trúc vốn đã che theo key | `test_redaction_masks_the_whole_secret_value` (7 mẫu, đỏ trước sửa), `test_redaction_keeps_the_text_after_a_quoted_secret` | **đóng** |
| R24 | (self-review) Rò trạng thái test | `test_api_logging_b19.py` đặt `nexa.api.app` ở INFO không trả lại → `test_rem_b15_r33_rejection_log.py` fail nếu chạy sau (thứ tự mặc định không lộ). Sửa: fixture autouse trả level/disabled | chạy hai thứ tự: 79 passed, 71 passed | **đóng** |
| R25 | (self-review, không chặn) | (a) `waiting_for_quota` ghi đè `waiting_for_worker` đặt lúc submit và khi gỡ đặt NULL thay vì khôi phục; job bị `SKIP LOCKED` bỏ qua ở lô gỡ cuối giữ reason cũ tới khi dispatch (dispatch đặt NULL) hoặc coordinator khởi động lại. Chỉ là hiển thị: policy dùng boolean snapshot. (b) G1 staging: nếu `remove_scanned` lỗi giữa vòng, file đã xóa trước đó không có audit tóm tắt và không vào metric (lỗi được đếm `nexa_gc_errors_total{kind="staging"}`, vòng sau tiếp tục) | – | **mở, không chặn** → task sau |

### 3.1 Đề xuất R01 — log của job (≤ 1 trang)

1. **Thu output**: runner chuyển stdout/stderr của workload vào một file trong scratch của
   attempt, giới hạn `NEXA_ATTEMPT_LOG_MAX_BYTES` (đã có, R15); vượt → LOG_OVERFLOW như hiện nay.
   Runner chỉ đọc file, không chạy shell.
2. **Publish có fence**: callback mới `POST /v1/internal/attempts/{attempt_id}/log-segments`
   cùng điều kiện với publish artifact (worker epoch, attempt, job fence, lease, desired state).
   Mỗi segment là một artifact kind `LOG` bất biến (qua đường upload có checksum/fsync hiện có)
   cộng một dòng `log_segments(job_id, attempt_id, seq, byte_offset, byte_length, artifact_id)`
   unique `(attempt_id, seq)`; replay idempotent theo `X-Callback-Id`.
3. **Offset và UTF-8**: segment cắt theo byte nhưng lùi về ranh giới UTF-8 hợp lệ;
   `byte_offset` liên tục theo attempt; API đọc trả `X-Log-Next-Offset` để client đọc tiếp.
4. **Nội dung nhạy cảm**: workload CPU hiện có thể in `invalid input: {exc}` (echo một phần input).
   Cần sửa thông báo của adapter thành mã lỗi, hoặc chấp nhận vì log thuộc tenant sở hữu input;
   không bao giờ ghi log job vào log JSON của process.
5. **Quyền và quota**: đọc theo quyền đọc job; byte LOG tính vào byte quota của tenant;
   retention theo job terminal + N ngày, GC theo reference như artifact; metric chỉ đếm tổng.
6. **Contract cần đổi**: openapi (route đọc/ghi segment, header offset), state-machines (segment
   sau terminal bị từ chối), database (bảng/unique). Làm ở một task riêng sau khi owner duyệt.

### 3.2 Đề xuất R05 — GC metadata và checkpoint (≤ 1 trang)

1. **Hiện trạng đo được**: artifact có reference `UPLOAD_SESSION` nhưng chưa bao giờ publish
   (upload user không dùng, upload worker của attempt bị fence) được trigger coi là đang tham
   chiếu nên giữ mãi; `storage-check` báo `retained_unpublished_artifacts/bytes` (0 trong
   outage/scrape, có test với dữ liệu thật).
2. **G3 đề xuất**: reference `UPLOAD_SESSION` hết hạn khi session COMMITTED quá
   `staging_ttl` mà artifact không có reference nào khác (INPUT/JOB/CHECKPOINT/RESULT). Xóa
   reference và dòng `artifacts` trong một transaction có khóa blob độc quyền như G2; blob để G2
   xóa ở vòng sau (thứ tự metadata → blob giữ nguyên ACC-17).
3. **Checkpoint**: giữ ≥ 2 checkpoint committed mới nhất mỗi job đang sống và mọi checkpoint của
   job terminal trong cửa sổ retention; chỉ xóa checkpoint cũ hơn hai bản mới nhất **sau** khi
   restore đã chọn bản mới hơn (event CHECKPOINT_SELECTED/FALLBACK) để không phá fallback.
4. **Contract**: concurrency-recovery (G3, retention), database (trigger cho phép xóa reference
   UPLOAD_SESSION), acceptance (ACC-17/18 thêm test). Cần migration đổi trigger nên không thuộc
   B19 (prompt cấm migration đổi trigger).

### 3.3 Đề xuất R16 — retention audit

≈ 7,25 dòng audit/job CPU một attempt (outage §16) cộng các lượt đọc admin được audit (B18). Đề
xuất: giữ audit ≥ 400 ngày (đủ cho kỳ báo cáo năm), xóa theo partition tháng bằng lệnh
`nexa-maintenance` có audit SYSTEM, không xóa audit của job chưa terminal; cần owner chốt thời
gian giữ và quy định pháp lý trước khi làm (B21/B25).

### 3.4 Đề xuất R19 — outcome LOG_OVERFLOW/CANCELLED

Thêm vào báo cáo stop của runner một `stop_reason` đóng (`LOG_OVERFLOW`, `CANCELLED`,
`RUNTIME_LIMIT`, `FAILURE`) để worker gắn FailureClass và outcome metric đúng; cần đổi IPC
runner–worker (contract runner) nên để task sau.

## 4. Thay đổi chính theo từng file

### 4.1 Observability (mới)

- `src/nexa/observability/logging.py`: formatter JSON (UTC ms, service, logger, event,
  contextvars `request_id/job_id/attempt_id/worker_id/tenant_id`), filter redaction (key nhạy cảm,
  Bearer, tiền tố token, DSN có password, exception/traceback), `configure_logging` idempotent,
  `uvicorn.access` tắt, httpx/httpcore WARNING.
- `metrics.py`: registry riêng, `exposition`, `after_commit`, `guarded`, `nexa_metrics_internal_errors_total`.
- `metrics_api.py`, `metrics_coordinator.py`, `metrics_worker.py`: khai báo metric + tập label đóng.
- `fairness.py`: Jain thuần. `ops_server.py`: listener D2, cache readiness ≤ 2 s single-flight.
  `probes.py`: probe DB/schema.

### 4.2 API và application

- `api/app.py`: middleware `http_request` (route template, không query), `unhandled_exception`
  có redaction, metric HTTP, `nexa_worker_callback_rejected_total`; lifespan chạy vòng GC và nhịp
  liveness; storage probe cho policy service. `api/main.py`: `configure_logging` trước `create_app`.
- `api/ops.py` (mới): /readyz `database`/`schema`/`storage`. `api/collectors.py` (mới): collector
  READ ONLY (queue, allocation, fairness, checkpoint, worker, storage).
- `api/routes_worker.py`: `X-Artifact-Size` có `le` (1 TiB) và kiểm trước mọi truy cập DB.
- `application/readiness_proof.py` (mới): R02. `admin_workers.py`: `worker_readiness_failure`
  dùng chung. `policy_service.py`: `storage_probe` ngoài transaction, chỉ đọc worker khi mở lại NORMAL.
- `application/storage_pressure.py` (mới): bảng watermark 6.B, cache 1 s, byte quota, ánh xạ mã lỗi,
  metric từ chối. `artifact_service.py`: kiểm watermark/quota sau replay, kiểm lại mỗi 1 MiB trong
  stream, ENOSPC → `storage_pressure` + nhả reservation, khóa blob shared khi commit metadata,
  actor WORKER/USER/SYSTEM, G1 `expire_uploads` actor SYSTEM.
- `job_service.py`, `sweep_service.py`: `storage_reading()` trước transaction, `enforce_storage()`
  sau replay (watermark + 429 quota); metric admission.
- `checkpoint_restore.py`: `nexa_restore_total`, checksum restore. `execution_artifacts.py`: actor
  upload của worker. `execution_cleanup.py`: checksum download, retry metric.
- `application/storage_gc.py` (mới): G1/G2/đối soát, khóa D9, audit SYSTEM, gauge G.
  `application/storage_checks.py` (mới): drift, retained unpublished, result consistency.
- `infrastructure/artifacts/store.py`: phân loại ENOSPC/EDQUOT, `disk_usage` có cache, `scan`,
  `remove_scanned` (lstat, không follow symlink, đúng inode), `active_staging_keys`, `has_blob`.
- `infrastructure/persistence/locking.py`: `lock_blob_key`, class 1_919_001/1_919_002.
  `schema_guard.py`: `current_schema_head`.
- `cli/main.py`: `nexa-maintenance storage-check`, `consistency-check --accepted-ids` (READ ONLY,
  exit 0/1/2, JSON chỉ có số đếm).
- `config.py`: `NEXA_OPS_BIND`, `NEXA_LOG_FORMAT`, GC/metrics cadence, `ResourceLimits`, quota của coordinator.

### 4.3 Coordinator

- `coordinator/artifact_quota.py` (mới), `service.py` (`track_artifact_quota`, metric quyết
  định/dispatch wait/lease reaped/maintenance failures sau commit), `snapshot.py`
  (`artifact_quota_available`), `domain/scheduling.py` + `scheduler/accounting.py`
  (`artifact_quota_exhausted`, domain vẫn thuần), `runtime.py` (`CoordinatorStatus` leader/liveness),
  `ops.py` (mới, /readyz database/schema + role), `main.py` (log JSON, ops listener engine riêng).

### 4.4 Worker

- `worker/main.py`: đọc log/ops/limits trước khi chạy, lỗi config → exit không echo giá trị.
  `docker_config.py`: PID mặc định 512 (32–32768), `attempt_bounds` (scratch = min(max, memory//4)),
  json-file 1 MiB. `dispatch.py`, `adapter_dispatch.py`, `executor.py`, `execution.py`: nhận
  `ResourceLimits`, runner `--log-limit` theo cấu hình. `docker_client.py`: `ping`, `stats` có timeout.
  `agent.py`: readiness/liveness, sampler stats, dọn thư mục local (R17), metric loop. `ops.py`
  (mới), `local_cleanup.py` (mới), `result_flow.py`: metric sau khi API xác nhận.

### 4.5 Web, harness, deploy, docs

- `web/src/features/admin/modes.ts` (+ `modes.test.ts`): copy điều kiện mở lại NORMAL;
  `web/tests/e2e/admin-mode/mode.spec.ts`: W1 (không worker) mở lại → 409 đúng message;
  `web/tests/e2e/w2-admin/00-reopen.spec.ts` (mới): mở lại thành công trên W2.
- `scripts/b17_e2e_stack.py`: `--ops`, `--prometheus` (image ghim digest, config sinh trong state),
  `--alert-for`, `--api-env` (allowlist key), `nexa-maintenance` sau lệnh con, quét canary (R21).
  `scripts/b19_{client,scrape,outage,disk_full,explain}.py` (mới).
- `deploy/prometheus/{prometheus.yml,alerts.yml,tests/alerts_test.yml}` (mới).
- `pyproject.toml`/`uv.lock`: `prometheus-client` 0.26.0. `.env.example`: key mới.
- Docs: `observability.md` (mới), `artifacts.md`, `database.md`, `worker-agent.md`, `coordinator.md`,
  `cli.md`, `web-ui.md`, `authentication.md`, `project-structure.md`, `environment-inventory.md`.
- Không có migration; compose.yaml, deploy/b10, Caddyfile, README, ROADMAP, PLAN, contract,
  AGENTS.md, acceptance không đổi.

### 4.6 Test

Mới: `tests/observability/` (5 tệp), `tests/test_config_b19.py`, `tests/artifacts/test_{gc_store,storage_pressure_units}_b19.py`,
`tests/scheduler/test_artifact_quota_b19.py`, `tests/worker/test_{local_cleanup,resource_limits}_b19.py`,
`tests/integration/test_{ops,coordinator_ops,readiness_proof,storage_pressure,storage_gc,storage_checks}_b19.py`.
Sửa: `tests/docker/test_real_runner.py` (+3 test giá trị không mặc định), `b16_support.py`,
`test_artifacts_b07.py`, `test_checkpoint_restore_b14.py`, `test_cleanup_b11.py`, `test_config.py`,
`test_agent_b10.py` (theo hành vi mới: worker upload tới critical, actor, PID mặc định 512).

## 5. Verification

### 5.1 Môi trường và phiên bản

| Mục | Giá trị |
|---|---|
| Máy | Mac (P), Docker Desktop engine 29.8.1, VM `aarch64` |
| Python | `uv` 0.9.27 (`$U=/tmp/nexa-b12-uv-bootstrap/bin/uv`), Python 3.12, `PYTHONPATH=src:.` |
| prometheus-client | 0.26.0 (`uv.lock`) |
| Prometheus / promtool | `prom/prometheus@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996` (v3.5.0), promtool trong cùng image |
| Node / pnpm / Playwright | v26.4.0 / 11.9.0 / 1.63.0 (Chromium `chromium-1243`, `PLAYWRIGHT_BROWSERS_PATH=/tmp/nexa-b17-pw`) |
| CPU image (local, không push) | `nexa/cpu-iterative@sha256:a35ca28733855ab40ff10207f69d94c028338073b74984ef9d0b9f3783b0d956` (tag `nexa/cpu-iterative:b19`, build 2026-10-03T19:41:40Z, `raw/B19-build-cpu.out`). Build lại vì build context `src/nexa` đổi (§10.B); 18 tệp runner import closure giống hệt image B18 `ec419c61…` (`raw/B19-cpu-image-runner-closure.sha256`) |
| Worker image (local) | r0: `sha256:d563c2c33257b5e2c6612eb2a06b89be7d337be3c6ee6105e49b8f866ba2f8cf` (build 2026-10-03T19:14:10Z, `raw/B19-build-worker.out`, giữ tag `nexa/b19-worker:r0`) — dùng cho scrape (§8), outage `af383797` (§15). **r2 (cuối)**: `nexa/b19-worker:local` id `sha256:5d35ccb53860063567a06082cd3c33c25fd329990c874bbc8b265fbeb5916192` (build 20:44:33Z sau sửa R22/R23, `raw/B19-build-worker-r2.out`, `deploy/b10/Dockerfile` với `FROM` ghim `python:3.12-slim@sha256:2f17fc04…06a9`) — dùng cho canary, Playwright cuối |
| Source trong image | Lệnh: `cd src && find nexa -name '*.py' \| LC_ALL=C sort \| xargs shasum -a 256 \| shasum -a 256` (174 tệp; trong image dùng `sha256sum`). Working tree cuối = worker r2 = `16959afcd3df34720cc4d5357508c3c3ecabef33a5cd1c39af21213fddb7587f`. CPU image và worker r0 = `2cbb22a94e3d2cb719eb5b4137fad6fe7bbda653c766616a20595b0773275cf9`: khác tree đúng 2 tệp `nexa/application/artifact_service.py` (R22 + một dòng trống của ruff format) và `nexa/observability/logging.py` (R23), cả hai không thuộc runner closure 18 tệp nên runner của CPU image không đổi, không build lại CPU image |
| Caddy | `caddy:2.10.2-alpine` `sha256:4c6e91c6ed0e…` (harness) |
| PostgreSQL | `nexa_b13_pg` 17.11 (127.0.0.1:15439); DB `nexa_b05_test_b19` (pytest), `nexa_b05_test_b19e2e` (stack), `nexa_b05_test_b19docker` (Docker), `nexa_b05_test_b19perf` (EXPLAIN, đã xóa, §22), `nexa_b05_test_b19ci` (CI replay, đã xóa, §22) |
| Dist cuối | `index.html` sha256 `b57852b8a94a…` |

Password PostgreSQL chỉ đọc lúc chạy bằng `docker exec nexa_b13_pg printenv POSTGRES_PASSWORD`
vào biến của subshell, không in; output che bằng `sed "s/${PGPW}/***/g"`. `<URL>` =
`postgresql+psycopg://postgres:***@127.0.0.1:15439/<db>`. Không đặt `NEXA_DATABASE_URL` trong shell.

### 5.2 Lệnh và kết quả

(Vòng 1. Kết quả trên cây vòng 2 ở §23.3.)

Tất cả chạy trong phiên này trên P. "Code cuối" = working tree sau sửa R22–R24 (source listing
`16959afc…`).

| Gate | Lệnh | Code | Kết quả | Raw |
|---|---|---|---|---|
| Ruff | `uv run --no-sync ruff check .` | cuối | All checks passed, exit 0 | `raw/B19-final-gates.out` |
| Ruff format | `uv run --no-sync ruff format --check .` | cuối | 563 files already formatted, exit 0 | `raw/B19-final-gates.out` |
| Whitespace | `git diff --check` + `git diff --no-index --check /dev/null <f>` cho 65 tệp untracked | cuối | exit 0; 0 tệp lỗi (đã cắt một dòng trống cuối `raw/B19-explain.out`) | `raw/B19-final-gates.out` |
| Pytest mặc định | `PYTHONPATH=src:. uv run --no-sync pytest -q` | cuối | **1942 passed, 677 skipped**, exit 0 (57 s) | `raw/B19-pytest.out` |
| Pytest PostgreSQL | `NEXA_TEST_DATABASE_URL=<URL>/nexa_b05_test_b19 PYTHONPATH=src:. uv run --no-sync pytest -q --run-postgres` | trước sửa R22–R24 | 2579 passed, 31 skipped (842,7 s) | `raw/B19-pytest-pg.out` |
| Test nhắm sau sửa | pressure + GC; observability + R33 + reconcile log + ops (cả hai thứ tự) | cuối | 27 passed; 79 passed; 71 passed | §3 R22–R24 |
| CI replay | bản sao sạch `/tmp/b19_ci` (git ls-files + untracked không bị ignore), các bước job `python` của `.github/workflows/ci.yml`: `uv sync --frozen --all-groups --no-editable`, ruff check, ruff format --check, `pytest -q --run-postgres -rs` trên DB `nexa_b05_test_b19ci` | cuối | `uv sync` exit 0; ruff All checks passed; format 737 files already formatted (= 563 + 174 tệp `build/lib` do `--no-editable` tạo trong bản sao không có `.git`); **pytest 2591 passed, 28 skipped**, exit 0 (810,9 s). +9 test so với lượt PG trước = 9 test mới của R22/R23; skip 28 thay vì 31: với `NEXA_TEST_PG_CLIENT_PREFIX` các test dump/restore B13-R12 chạy (có trong danh sách durations), lượt PG trước không đặt biến này | `raw/B19-ci-replay.out` |
| promtool | `promtool check rules`, `check config`, `test rules` (image prom/prometheus v3.5.0) | `deploy/prometheus/**` không đổi sau 02:25 | 16 rules SUCCESS; config SUCCESS; test rules SUCCESS | `raw/B19-promtool.out` |
| Docker | `NEXA_RUN_DOCKER=1 [NEXA_B11_RUNTIME_EVIDENCE=1] pytest … tests/docker/…` (CPU image `a35ca287…`) | worker r0 (19:49Z) và **cuối** (21:09Z, worker r2) | cả hai lần: runner bounds/pid/log/rootfs **6 passed**; runner + executor + B11 IPC + B11 vertical (PG `nexa_b05_test_b19docker`) **17 passed**, exit 0. Lần chạy lại đầu tiên sai tên biến image (fail ở setup, 0,26 s, không chạy code sản phẩm), chạy lại với `NEXA_B09_IMAGE_REF` | `raw/B19-docker.out` |
| Disk-full tmpfs | `docker run … --tmpfs /artifacts:size=16m … b19_disk_full.py` | worker r0 | mọi check true (§14) | `raw/B19-disk-full.out` |
| Dừng dispatch | `pytest -s --run-postgres test_storage_pressure_b19.py -k next_heartbeat` × 3 | cuối | 40,6 / 40,3 / 36,6 ms | `raw/B19-dispatch-stop.out` |
| Web | `pnpm --dir web install --frozen-lockfile`, `run typecheck`, `run build`, `exec vitest run` | cuối | exit 0 cả bốn; Vitest **29 files / 217 tests passed**; dist `b57852b8a94a` | `raw/B19-final-gates.out` |
| Playwright | §5.3 (worker r2, dist `b57852b8a94a`, retries 0) | cuối | **56 passed, 0 failed**: w1-admin-mode 1, w2-admin + w2-admin-mobile 6, w1-desktop + w1-mobile 23, w1-admission 1, w1-admin 20, w1-admin-frozen 1, w2 4 | `raw/B19-playwright.out` |
| Scrape + alert sống | §5.3 `b19_scrape.py` | worker r0 | §7, §8, AC-05 | `raw/B19-scrape.{log,json}` |
| Outage | §5.3 `b19_outage.py` | r0 (`af383797`) và cuối r2 (`af4a6590`) | `failures=[]` cả hai (§15) | `raw/B19-outage.{log,json}`, `raw/B19-canary.log` |
| EXPLAIN | `scripts/b19_explain.py` trên DB 100 k job | trước sửa (không đổi truy vấn) | §17 | `raw/B19-explain.out` |

Lưu ý: repo `.venv` giữ bản cài `--no-editable` cũ trước B19, nên chạy trong repo dùng
`PYTHONPATH=src:.`; CI replay `uv sync` lại trong bản sao sạch nên không phụ thuộc điều này.

### 5.3 Lệnh stack (W2) và Playwright

```sh
export PATH=/tmp/nexa-b17-node/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/tmp/nexa-b17-pw
pnpm --dir web run build
# NEXA_TEST_DATABASE_URL=<URL>/nexa_b05_test_b19e2e (subshell, password đọc từ container)
S="env PYTHONPATH=src:. $U run --no-sync python scripts/b17_e2e_stack.py"
P="pnpm --dir web exec playwright test"
W2="NEXA_B17_CPU_IMAGE_REF=nexa/cpu-iterative@sha256:a35ca28733855ab40ff10207f69d94c028338073b74984ef9d0b9f3783b0d956 NEXA_B17_WORKER_IMAGE=nexa/b19-worker:local"
env $W2 $S run --tier w2 --prometheus --alert-for 20s -- .venv/bin/python scripts/b19_scrape.py --out docs/evidence/raw/B19-scrape.json
env $W2 $S run --tier w2 --prometheus --api-env NEXA_METRICS_CACHE_SECONDS=1 --api-env NEXA_METRICS_STATEMENT_TIMEOUT_MS=200 -- .venv/bin/python scripts/b19_outage.py --out docs/evidence/raw/B19-outage.json
# canary cuối (worker r2): như trên với --out /tmp/b19_canary_outage.json → raw/B19-canary.log
$S run --tier w1 -- $P --project=w1-admin-mode
env $W2 $S run --tier w2 -- $P --project=w2-admin --project=w2-admin-mobile
$S run --tier w1 -- $P --project=w1-desktop --project=w1-mobile
$S run --tier w1 -- $P --project=w1-admission
$S run --tier w1 -- $P --project=w1-admin
$S run --tier w1 --operational-mode WRITE_FROZEN -- $P --project=w1-admin-frozen
env $W2 $S run --tier w2 -- $P --project=w2
```

Retries = 0, không `waitForTimeout`/sleep cố định trong spec mới.

## 6. PLAN §9 → metric → test → kết quả

| Nhóm PLAN §9 | Metric | Test | Kết quả |
|---|---|---|---|
| queue depth / oldest age | `nexa_jobs{state}`, `nexa_jobs_waiting{waiting_reason}`, `nexa_queue_oldest_age_seconds`, `nexa_queue_outstanding(_limit)` | PG `test_api_ops_listener_ready_metrics_and_rest_isolation`, scrape nhóm `jobs`/`queue` | pass |
| admission / reject reason | `nexa_admission_total{kind,outcome,reason}`, `nexa_storage_rejections_total` | PG `test_submit_job_watermark_quota_and_replay`, scrape nhóm `admission` | pass |
| scheduling latency | `nexa_coordinator_tick_duration_seconds`, `nexa_coordinator_dispatch_wait_seconds`, `nexa_coordinator_decisions_total` | PG `test_decisions_dispatch_wait_and_reaped_leases_count_after_commit`, scrape | pass |
| allocation / reservation / quarantine | `nexa_allocations{state}`, `nexa_allocated_resource{resource}`, `nexa_reservations_active`, `nexa_quarantine_oldest_age_seconds` | PG collector tests, scrape nhóm `allocation` | pass |
| fairness (Jain, dominant resource-time) | `nexa_fairness_jain_index{window}`, `nexa_fairness_dominant_resource_seconds{window}` | Hypothesis `test_jain_is_within_bounds`, `test_jain_is_one_when_equal`, `test_jain_ignores_idle_tenants_and_handles_none`; scrape nhóm `fairness` | pass |
| lease / renewal / heartbeat thất bại | `nexa_lease_expired_total`, `nexa_worker_loop_failures_total{operation}`, `nexa_worker_heartbeat_age_seconds` | PG coordinator test, `test_loop_failures_are_counted_by_closed_operation_name` | pass |
| stale rejection | `nexa_worker_callback_rejected_total{route,code}` | PG `test_api_ops_listener_ready_metrics_and_rest_isolation` (callback stale) | pass |
| retry / restore / checkpoint age | `nexa_retry_scheduled_total`, `nexa_restore_total{outcome}`, `nexa_checkpoint_age_seconds` | `test_checkpoint_restore_b14.py` (bổ sung), scrape nhóm `checkpoint` | pass |
| checksum errors | `nexa_checksum_errors_total{operation}` | `test_checkpoint_restore_b14.py`, `test_cleanup_b11.py` (bổ sung) | pass |
| storage / watermark / GC | `nexa_storage_*`, `nexa_gc_*`, `nexa_upload_sessions_expired_total` | PG GC/pressure tests, scrape nhóm `storage`/`watermark`/`gc` | pass |
| execution / OOM / RAM / CPU throttle | `nexa_worker_executions_total{outcome}`, `nexa_worker_container_oom_total`, `nexa_worker_container_{memory,cpu}_ratio_max` | `test_worker_ops_b19.py`; scrape nhóm `worker_executions` (job thật SUCCEEDED → 1) | pass; OOM thật không chạy trong B19 (không có test OOM worker-level sẵn), throttle là proxy (R18) |
| worker / mode / readiness | `nexa_workers{health,admin_state}`, `nexa_operational_mode{mode}`, `nexa_ready{check}`, `nexa_worker_ready` | scrape nhóm `workers`/`mode`/`readiness`/`worker_ready` | pass |
| GPU | `nexa_allocated_resource{resource="gpu"}` (từ DB) | collector test | pass phần allocation; utilization → B23 |
| HTTP | `nexa_http_requests_total`, `nexa_http_request_duration_seconds` | `test_access_line_uses_route_template_and_drops_query_and_headers`, scrape nhóm `http` | pass |

Scrape thật: `groups_missing=[]` cho 22 nhóm (`raw/B19-scrape.json`).

## 7. Cardinality

| Kịch bản | API | Coordinator | Worker | Ghi chú |
|---|---|---|---|---|
| Unit: 1 tenant × 1 job vs 50 tenant × 500 job (cùng tập state/reason) | bằng nhau | – | – | `test_series_count_does_not_grow_with_tenants_or_jobs` |
| Scrape thật sau 1 job (run `6b46bc35`, Prometheus) | 458 | 40 | 14 | 69 tên metric, `id_labels=[]` |
| Trần thiết kế (observability §2) | ≤ 2 500 | ≤ 100 | ≤ 60 | – |

Kịch bản lớn trên stack thật là outage 8 job (§15): số series không phụ thuộc số job theo
thiết kế và test unit; series HTTP chỉ tăng theo route template đã gọi (≤ 72).

## 8. Exposition và readiness

Mẫu rút gọn từ Prometheus (run `6b46bc35`, `raw/B19-scrape.json`; không có ID, không secret):

```text
# api (host.docker.internal:<ops>)
nexa_operational_mode{mode="NORMAL"} 1
nexa_jobs{state="QUEUED"} 0
nexa_allocations{state="QUARANTINED"} 0
nexa_queue_outstanding 0
nexa_storage_used_ratio 0.7536084868946297
nexa_storage_watermark_ratio{level="high"} 0.85
nexa_collector_up{collector="queue"} 1
nexa_ready{check="database"} 1
nexa_fairness_dominant_resource_seconds{window="1h"} 0
nexa_gc_last_run_timestamp_seconds 0
nexa_admission_total{kind="job",outcome="accepted",reason="none"} 1
# coordinator
nexa_coordinator_leader 1
nexa_coordinator_decisions_total{outcome="none"} 103
nexa_coordinator_tick_duration_seconds_count 104
# worker
nexa_worker_executions_total{outcome="SUCCEEDED"} 1
nexa_worker_ready 0   # sau khi worker container bị dừng
```

/readyz trên stack thật: API 200, coordinator 200, worker 200; 300 probe (50 × `/metrics` +
50 × `/readyz` mỗi process) đều 200, số dòng audit (78) và events (5) không đổi trước/sau
(collector và readiness không ghi DB). 503: `test_not_live_and_not_ready_return_503_with_extra_fields`
(body `{"status":"not_ready","checks":{...:"fail"}}`), PG
`test_readiness_fails_closed_for_schema_storage_and_database` (DB sai cổng, schema lệch, storage
critical → 503 với đúng check `fail`; body không có DSN/path), `test_readiness_probe_exception_is_not_ready`,
`test_concurrency_is_bounded_with_503`. Caddy `/metrics` trả 200 HTML của SPA (`prometheus_text=false`):
metric không đi qua Caddy.

## 9. Log và canary

- PG `test_real_uvicorn_logs_json_access_without_query_or_credentials`: chạy đúng lệnh compose
  (`uvicorn nexa.api.main:app`), gửi canary ngẫu nhiên trong query `password=`, body password,
  `Authorization: Bearer`, cookie phiên và `cursor=`; canary xuất hiện **0** lần trong
  stdout+stderr; mọi dòng là JSON; `http_request` không có `?`; không có dòng `uvicorn.access`.
- Unit: redaction cho từng mẫu secret, key nhạy cảm lồng nhau, exception/traceback
  (`test_logging_b19.py`), `unhandled_exception` (lỗi 500 giả lập) có traceback đã che
  (`test_unhandled_exception_logs_request_id_route_and_redacted_traceback`).
- Quét canary cấp stack (R21, `StackHarness.canary_scan`): sau lệnh con, so trong bộ nhớ các giá
  trị thật của run với log API (`api.log`), coordinator (`coordinator.log`) và `docker logs` của
  worker: server secret, bootstrap secret, password mọi user, cookie phiên và CSRF token của
  phiên admin harness, password DB, worker credential, byte input, 64 byte đầu của mọi blob đã
  commit (gồm checkpoint và result). Không in giá trị; hit chỉ in loại và tên log.

Kết quả (`raw/B19-canary.log`, run `af4a6590`, code cuối, worker r2, kịch bản outage 8 job + 2
login + Prometheus):

```text
[b17-stack] canary scan: 44 values x 3 logs (api 109044 B, coordinator 383 B, worker 4467 B): 0 hit(s)
```

Lần chạy trước (`866d7bb4`, worker r0, regex trước R23) cũng 0 hit. Log coordinator nhỏ vì khi khỏe
coordinator chỉ ghi `ops_listener_started` (INFO); các dòng còn lại là WARNING khi lỗi
(`coordinator_*_unavailable`, `coordinator_maintenance_failed`), phủ bởi unit test redaction.
Một lần chạy trước đó (`8f5da6e1`) bị chính tôi chạy thiếu `--api-env NEXA_METRICS_CACHE_SECONDS=1`
(cache 15 s nên collector không trúng cửa sổ khóa, script báo `failures=['collector']`); không phải
lỗi sản phẩm, đã chạy lại đúng lệnh.

## 10. Watermark và quota

| Ô (bảng observability §6.1/§6.2) | Test | Kết quả |
|---|---|---|
| submitJob/createSweep: < high nhận; high/critical 503 `storage_pressure` + Retry-After; sau replay | `test_submit_job_watermark_quota_and_replay`, `test_create_sweep_parent_is_storage_gated_and_replays_on_a_full_disk`, unit `test_watermark_table` | pass |
| replay khi đĩa đầy trả response cũ; statvfs lỗi → 503 `dependency_unavailable`, không cache | `test_a_replay_never_needs_disk_and_a_failed_statvfs_fails_closed`, `test_failed_statvfs_fails_closed_after_replay_and_is_never_cached` | pass |
| upload user: high/critical 503 | `test_user_upload_follows_the_watermark_table` | pass |
| upload worker: high nhận, critical 503 (R06) | `test_worker_upload_is_refused_only_at_critical_and_audited_as_the_worker` | pass |
| vượt ngưỡng giữa stream → 503, nhả reservation | `test_mid_stream_pressure_aborts_and_releases_the_reservation`, `test_expected_bytes_count_toward_the_watermark` | pass |
| ENOSPC ở write/fsync/rename → 503 `storage_pressure`, nhả reservation, không metadata | `test_enospc_while_staging_is_storage_pressure_and_releases`, `test_enospc_is_storage_pressure_and_counted`, `test_a_blob_missing_at_metadata_commit_is_retryable_and_releases` | pass |
| header/body: `X-Artifact-Size` > 1 TiB/âm → 422 trước DB; Content-Length lệch → 422; JSON > giới hạn → 413 kể cả chunked; vượt per-file → 413 | `test_worker_upload_headers_are_checked_before_any_db_work`, `test_oversized_json_body_is_413_also_when_chunked` | pass |
| dispatch dừng ở critical, chạy lại dưới critical | `test_critical_storage_stops_new_offers_at_the_next_heartbeat` | pass |
| đọc/download/restore/cancel/cleanup không bị chặn | `test_published_checkpoints_survive_gc_and_a_restore_reads_during_a_pass`, B07/B11/B14 regression | pass |
| byte quota admission 429; dispatch `waiting_for_quota`; tenant khác vẫn dispatch | `test_submit_job_watermark_quota_and_replay`, `test_an_exhausted_tenant_gets_no_dispatch_and_a_derived_quota_reason`, `test_other_tenants_still_dispatch_while_one_tenant_is_over_quota`, `test_an_aged_job_of_an_exhausted_tenant_gets_no_dispatch_and_no_reservation` | pass |
| reason cũ được gỡ khi khởi động; WRITE_FROZEN không ghi | `test_startup_clears_stale_reasons_and_write_frozen_writes_nothing` | pass |

Thời gian dừng dispatch ở critical: dispatch dừng qua heartbeat (worker → STARTING, coordinator
chỉ dispatch cho worker READY), coordinator không đọc storage. Giới hạn trên = một chu kỳ heartbeat
(5 s) + timeout thao tác (8 s) = **≤ 13 s**. Trong test PG, heartbeat được gửi ngay sau khi vượt
critical; cửa sổ đo từ lúc vượt tới khi tick không có quyết định: **40,6 / 40,3 / 36,6 ms** qua 3 lần
chạy (`raw/B19-dispatch-stop.out`; test yêu cầu < 5 s). Trên stack thật cửa sổ bị chặn trên bởi
chu kỳ heartbeat như trên.

## 11. GC

| Yêu cầu | Test | Kết quả |
|---|---|---|
| G1 có runtime (lifespan), dừng khi shutdown | `test_the_api_lifespan_runs_gc_and_stops_it_on_shutdown` | pass |
| G1 expire session nhả reservation, audit SYSTEM `artifact.upload.expire`; xóa staging cũ không thuộc handle/session ACTIVE; audit tóm tắt `artifact.gc.staging` | `test_g1_expires_sessions_and_removes_only_stale_unowned_staging`, `test_an_open_staging_handle_is_never_removed` | pass |
| G2 chỉ xóa blob cũ, không có dòng `artifacts`; không follow symlink; tên lạ/ngoài root bỏ qua | `test_g2_removes_only_old_unreferenced_blobs_and_never_follows_symlinks`, `test_scan_yields_only_regular_files_with_key_names`, `test_remove_scanned_unlinks_the_same_file_only_and_never_follows_symlinks`, `test_an_unbound_or_replaced_store_is_never_swept` | pass |
| G2 vs commit metadata, cả hai thứ tự (barrier bằng khóa, không sleep) | `test_g2_waits_for_a_metadata_commit_and_then_keeps_the_blob`, `test_a_metadata_commit_waits_for_g2_and_then_fails_closed` | pass |
| restore đọc checkpoint trong lúc GC; checksum mọi blob được tham chiếu trước = sau; ≥ 2 checkpoint còn nguyên | `test_published_checkpoints_survive_gc_and_a_restore_reads_during_a_pass` | pass |
| một vòng một lúc; lỗi đếm theo bước | `test_one_pass_at_a_time_and_failures_are_counted_per_step` | pass |

Audit mẫu: actor `SYSTEM`, actor id `storage-gc`, action `artifact.upload.expire` /
`artifact.gc.staging` / `artifact.gc.orphan`, chi tiết chỉ có số đếm/byte/blob key (không path
tuyệt đối). Metric: `nexa_gc_deleted_total{kind}`, `nexa_gc_bytes_deleted_total{kind}`,
`nexa_gc_errors_total{kind}`, `nexa_gc_last_run_timestamp_seconds`.

## 12. Đối soát và consistency-check

Test PG: `test_storage_check_reports_drift_without_repairing_it` (sửa tay một counter → exit 1,
`drift_tenants=1`, counter không bị sửa, gauge drift), `test_storage_check_counts_bytes_retained_until_published`,
`test_consistency_check_reports_missing_ids_and_result_rules` (thiếu ID → exit 1),
`test_checks_exit_2_on_bad_input_or_an_unavailable_database`.

Output trên stack thật sau outage (run `af383797`):

```text
nexa-maintenance consistency-check exit=0
{"accepted":8,"consistent":true,"missing_accepted":0,"multiple_results":0,"result_of_unsucceeded_job":0,"succeeded":8,"succeeded_without_result":0}
nexa-maintenance storage-check exit=0
{"consistent":true,"drift":[],"drift_tenants":0,"retained_unpublished_artifacts":0,"retained_unpublished_bytes":0}
```

## 13. Giới hạn tài nguyên trên Docker (P)

`raw/B19-docker.out` (2026-10-03T19:49Z):

- `NEXA_RUN_DOCKER=1 pytest -q -s tests/docker/test_real_runner.py -k 'bounds or pid or log_bound or rootfs'`: **6 passed**, 6 deselected, 78 s.
  - `test_configured_pid_and_scratch_limits_bind_the_container` (cấu hình worker PID 64, scratch
    96 MiB, memory 512 MiB): `docker inspect` thấy `PidsLimit=64`, tmpfs `/tmp` `size=100663296`,
    json-file `max-size=1048576`; fork bomb bị chặn (< 64 tiến trình); ghi scratch gặp ENOSPC sau
    ≥ 90 MiB và ≤ 96 MiB.
  - `test_default_pid_limit_is_512`: `PidsLimit=512`.
  - `test_configured_attempt_log_bound_fails_the_attempt` (log 2 MiB): vượt → runner dừng reason
    FAILURE trong ≤ 7 s (LOG_OVERFLOW, R15).
  - `test_production_container_enforces_cpu_pid_scratch_and_memory_bounds`,
    `..._denies_rootfs_input_network_and_runner_control`, `test_watchdog_stops_workload_when_runner_log_bound_is_exceeded`: hồi quy pass.
- `NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 pytest -q --run-postgres tests/docker/test_real_runner.py tests/docker/test_real_executor.py tests/docker/test_b11_worker_ipc.py tests/docker/test_b11_vertical.py` (DB `nexa_b05_test_b19docker`): **17 passed**, 0 failed, 248 s.
- Chạy lại cả hai lệnh trên code cuối (2026-10-03T21:09Z, worker r2, cùng raw): **6 passed** (79 s)
  và **17 passed** (258 s), exit 0.

## 14. Disk-full thật trên tmpfs

`raw/B19-disk-full.out`: worker image, artifact root trên tmpfs 16 MiB, `--network none --read-only`:

```text
first_blob 1 MiB committed  sha256:206b4b95…54a  readiness_before=ready
64 MiB upload → enospc {"code":"storage_full","received":15724544}; usage_when_full 100 %
readiness_when_full = "storage_unavailable: Artifact storage is at the critical watermark"
abort_staging → staging_after_abort=[]; committed_after=[f71e259b…] (chỉ blob đầu)
first_blob_after sha256:206b4b95…54a (không đổi); readiness_after=ready
checks: 6/6 true; exit=0
```

`storage_full` là mã nội bộ của store; service ánh xạ thành `storage_pressure` 503 (R08, test §10).

## 15. Outage test (run `af383797`, `raw/B19-outage.{log,json}`)

Cách làm collector lỗi: kết nối khác `LOCK TABLE checkpoints IN ACCESS EXCLUSIVE MODE` trong 3 s
(4 lần); collector checkpoint hết `lock_timeout` → series bị bỏ, `collector_up=0`, rồi lên lại. Không
có code test trong sản phẩm.

| Thời điểm (UTC 2026-10-03) | Sự kiện |
|---|---|
| 19:51:02.788 | submit 8 job cpu-iterative (30 M iteration, 256 MiB), 8 × 202, accepted ID lưu cho consistency-check |
| 19:51:04.117 | job đầu RUNNING |
| 19:51:04.314 | dừng hẳn Prometheus; bắt đầu bắn `/metrics` API 20 req/s × 30 s |
| 19:51:04 → 19:51:26.8 | 4 lần khóa bảng: collector down (series bỏ) / up ×4 |
| 19:51:34.321 | hết bắn: 503 request, 503 × 200, 0 lỗi, p50 7,9 ms, p99 242,1 ms, max 276,8 ms |
| 19:52:18.038 | 8/8 SUCCEEDED |
| sau lệnh | consistency-check exit 0, storage-check exit 0 (§12) |

Kết quả: mọi job terminal SUCCEEDED; REST qua metric API: 2xx 403, 4xx 1, **5xx 0** (trước = sau = 0);
REST của script 200 × 192, 202 × 8; `nexa_collector_errors_total` 9; khóa rc [0,0,0,0]. Lần 4xx duy nhất
là `POST /v1/workers/{worker_id}/poll` 409 khi worker tạm STARTING sau khi nhả allocation (R20).

Lặp lại trên code cuối (run `af4a6590`, worker r2, `raw/B19-canary.log`): `failures=[]`, collector
down/up × 4, hammer 509 request 0 lỗi (p99 240,7 ms), 8/8 SUCCEEDED, 5xx 0 → 0, 2xx 375 / 4xx 3
(cả 3 là poll 409, R20) / 5xx 0, `collector_errors_total` 10, audit +58, events +40, hai lệnh
kiểm tra exit 0.

## 16. Audit growth (R16)

Outage 8 job: **+58 dòng audit** (≈ 7,25/job), +40 events (5/job):

| Actor / action | Dòng |
|---|---|
| COORDINATOR JOB_DISPATCHING | 8 |
| USER auth.login | 2 |
| USER job.submit | 8 |
| WORKER allocation_released | 8 |
| WORKER artifact.upload.commit | 16 (checkpoint + result) |
| WORKER attempt_started | 8 |
| WORKER result_recognized | 8 |

300 probe readiness/metrics: +0 audit, +0 events.

## 17. EXPLAIN và migration 0024

`raw/B19-explain.out`, DB `nexa_b05_test_b19perf` (PG 17.11; 20 tenant, 100 000 job, 200 020
artifact, 219 911 reference, 79 946 result; `scripts/b19_explain.py`):

| Truy vấn | Plan | Execution |
|---|---|---|
| G2 orphan lookup (`blob_key IN` 500) | Index Only Scan `uq_artifacts_blob_key` | 4,7 ms |
| G1 staging lookup (`staged_key IN` 500, ACTIVE) | `uq_upload_sessions_staged_key` | 3,7 ms |
| G2 kiểm lại trong khóa (`blob_key =`) | index | 0,015 ms |
| G1 expire candidates (limit 1000) | `ix_upload_sessions_active_expiry` | 0,29 ms |
| storage-check counter drift | Parallel Seq Scan `artifacts` (aggregate toàn bảng) | 44 ms |
| storage-check retained unpublished | Parallel Seq Scan `artifacts`/`artifact_references` | 58 ms |
| consistency-check 1000 ID | Seq Scan `jobs` (aggregate) | 98,8 ms |

Đường nóng (GC theo lô) dùng index có sẵn; seq scan chỉ ở lệnh vận hành tổng hợp toàn bảng, chạy
mỗi vòng GC/khi operator gọi, ≤ 0,1 s ở 100 k job. Không cần index mới → **không tạo migration
0024** (AC-17).

## 18. Quan sát trong khi kiểm chứng

- R20 (snapshot flip): kịch bản `00-reopen` lần đầu fail vì kết thúc khi worker vừa STARTING; sửa
  spec chờ allocation RELEASED rồi heartbeat READY sau `released_at` (6/6 × 2 sau sửa). Không đổi
  sản phẩm.
- Prometheus `NexaGcNeverCompleted` pending/firing ngay trong scrape vì `--alert-for 20s` và vòng GC
  đầu chạy sau 60 s: đúng biểu thức (`last == 0`), không phải lỗi.
- Dist trong log scrape/outage là `de4c3dbb3979` (trước khi build lại sau sửa copy modes.ts);
  scrape/outage không dùng UI. Playwright chạy trên dist cuối `b57852b8a94a`.

## 19. Tự review

- Đọc lại toàn bộ `git diff` và các tệp untracked của B19 theo invariant AGENTS.md (tenant/scope,
  không gọi Docker/statvfs trong transaction, fail closed, audit cùng transaction, label không ID).
- Một lượt rà soát đọc-only độc lập (không chạy test/DB) theo 6 vùng rủi ro: GC G1/G2 và khóa
  advisory với commit metadata; proof mở lại NORMAL; thứ tự replay → watermark/quota và nhả
  reservation trên mọi đường lỗi stream; redaction log; theo dõi `waiting_for_quota` của coordinator;
  dọn thư mục local worker. Kết quả đã xác minh lại trên code: R22 (sửa, test đỏ → xanh), R23 (sửa,
  7 mẫu đỏ → xanh), R25 (ghi nhận, không chặn). Proof mở lại NORMAL, thứ tự replay/nhả reservation và
  `local_cleanup` không có lỗi được xác nhận.
- Trong khi chạy lại gate phát hiện R24 (rò level logger giữa test), sửa và chạy hai thứ tự.
- Quét secret: `grep` password PostgreSQL thật (đọc trong subshell, không in) trên
  `docs/evidence/**`, `docs/observability.md`, `deploy/prometheus/**`, `scripts/`, `src/`, `tests/`,
  `web/tests/`, `.env.example`: **0 tệp**. Không có cookie/CSRF/token/credential thô trong raw; URL DB luôn ở dạng `<URL>`/`***`.
  Canary log stack 0 hit (§9).
- Phạm vi file: không sửa PLAN.md, AGENTS.md, README.md, ROADMAP.md, `docs/contracts*`,
  `docs/acceptance.md`, `compose.yaml`, `deploy/b10/**`, `deploy/web/Caddyfile`, migration, CI,
  `web/package.json`/lockfile; dependency thêm duy nhất `prometheus-client`; web chỉ 3 tệp được phép +
  `00-reopen.spec.ts`; không tạo `docs/superpowers/` mới (thư mục có sẵn từ B02–B08, không đổi).
- Không commit/push/branch.

## 20. Acceptance

### 20.1 AC của B19

| AC | Status | Evidence |
|---|---|---|
| AC-01 docs/observability.md bản đầu trước code, bản cuối khớp code | pass | bản đầu 17:09:11Z; §2 khác biệt; đối chiếu §6 |
| AC-02 metrics đủ nhóm, allowlist, series không phụ thuộc tenant/job, không ID | pass | §6, §7; scrape `id_labels=[]` |
| AC-03 cổng vận hành 3 process, mặc định tắt, không qua Caddy/REST, readiness 503 không secret | pass | §8; R03 |
| AC-04 log JSON 3 process, NEXA_LOG_LEVEL, access log không query, canary 0 kể cả 500 | pass | §9 |
| AC-05 alert đủ nhóm, promtool check/test, scrape `up`=1, alert sống | pass | §5.2 promtool; scrape: NexaWorkerNotReady pending 3,7 s → firing 23,2 s, NexaTargetDown pending 8,8 s → firing 28,3 s sau khi dừng worker (`for` rút còn 20s, sinh từ file gốc) |
| AC-06 outage | pass | §15 |
| AC-07 mode | pass (P) | PG 7 test; W1-admin-mode; W2 `00-reopen`; WRITE_FROZEN vẫn 409 (`test_frozen_and_freeze_transitions_stay_fail_closed`); `modes.test.ts` |
| AC-08 watermark, replay, mid-stream, thời gian dừng dispatch | pass | §10 |
| AC-09 byte quota, domain thuần | pass | §10; scheduler unit không import storage |
| AC-10 header/body, ENOSPC | pass | §10, §14 |
| AC-11 GC | pass; G3 hoãn (R05) có số đo | §11, §12 |
| AC-12 storage-check/consistency-check | pass | §12 |
| AC-13 giới hạn tài nguyên | pass (P) | §13; config unit |
| AC-14 audit actor; collector/readiness không ghi DB | pass | §8, §16, R09 |
| AC-15 mã lỗi thuộc ErrorCode | pass | `test_every_store_code_maps_to_a_published_error_code` |
| AC-16 thư mục local worker | pass | R17 |
| AC-17 migration 0024 | không tạo, có lý do | §17 |
| AC-18 không hồi quy | pass (P) | §5.2: CI replay trên code cuối 2591 passed / 0 failed; pytest mặc định 1942 passed; Docker 6 + 17 passed; Playwright 56 passed (gồm B17/B18); Vitest 217; ruff/format/diff-check sạch |

### 20.2 Gate (docs/acceptance.md giữ nguyên `specified`)

| Gate | Tiêu chí thuộc B19 | Test | Evidence | Môi trường | Applicability | Status (phần B19) | Giới hạn / task khác |
|---|---|---|---|---|---|---|---|
| ACC-23 | watermark/quota trước admission/dispatch/upload, bound khi stream, disk-full không publish dở, GC không xóa referenced/in-use/active upload, audit quota/GC, checksum blob tham chiếu | §10, §11, §14 | §10–§14 | P | áp dụng | **P: pass**; **L: not-run** | VPS1 không dùng |
| ACC-26 | metric/log/audit đủ nhóm, cardinality, không secret, alert, outage, accepted-ID/result consistency | §6–§9, §15, §16 | §5–§16 | P | áp dụng | **P: pass**; **L: not-run** | log job R01 |
| ACC-21 | readiness DB/schema/storage fsync/watermark/capability/reconciliation; API 503; không dispatch khi không READY | PG ops/readiness/pressure | §8, §10 | P | phần B19 | pass (phần B19, P) | fault partition/heartbeat đầy đủ: B20/B22 |
| ACC-25 | PID/log/scratch có hiệu lực và cấu hình được | Docker §13 | §13 | P | phần B19; gate thuộc B20 (L) | pass (P); **L: not-run** | Docker Desktop không gán L |
| ACC-17, ACC-18 | GC không phá thứ tự blob → metadata; ≥ 2 checkpoint được giữ | §11 | §11 | P | phần B19 | pass (phần B19) | R05 |
| ACC-24 | listener metrics mặc định tắt, không qua Caddy | R03 | §8 | P | phần B19 | pass (phần B19) | topology mạng quản trị B21 |
| ACC-27 | log | §9 | §9 | P | phần log | phần process log: pass; **gate toàn phần: specified** | log của job R01 |
| ACC-39 | lệnh thật ruff/pytest/promtool/Docker/web | §5.2 | §5 | P | phần B19 | phần B19: pass (P, lệnh thật trong phiên); **gate toàn phần: specified** | Playwright/promtool chưa vào CI (R12) |
| L (VPS1, 10.D) | ACC-23/26 trên Linux | – | – | L | tùy chọn | not-run | – |

## 21. Giới hạn và phần thuộc task khác

- B20: race/security đầy đủ, ACC-25 trên Linux (L), header audit.
- B21: Prometheus/mạng quản trị trong Compose (R11), freeze_ready/restore_verified (B18-R05/R18),
  retention audit (R16, cần owner).
- B22: load/soak, số series dưới tải lớn, DB down (ACC-21 phần còn lại).
- B23: GPU utilization (chỉ có allocation từ DB).
- B25: Playwright/promtool trong CI (R12), release.
- Task riêng chờ owner duyệt contract: log của job (R01), G3/checkpoint pruning (R05), stop
  reason của runner (R19), bổ sung key config vào contract (R10).
- Finding cũ: B18-R24 đóng bởi R09; B17-R01 vẫn hoãn (= R01); B18-R05/R18 phần mở lại NORMAL đóng
  bởi R02, phần WRITE_FROZEN vẫn mở (B21); B15-R33 log JSON giờ đi qua formatter chung (test cũ
  vẫn pass); B15-R11, B15-R39, B16-R29, B11-H01, B15-OBS-01, OD-1..3, REM-R08 không đổi; CI-R01:
  output evidence đã che password.

## 22. Trạng thái môi trường khi bàn giao

| Thành phần | Trạng thái |
|---|---|
| Container của B19 | không còn: mọi stack harness (Caddy/API/coordinator/worker/Prometheus) dọn bởi `b17_e2e_stack.py` sau mỗi run (`cleaned up run …`); không còn container/network/volume tên `b19` |
| Container không thuộc B19 | không chạm: `nexa_b13_pg`, `nexa_b10_smoke3-caddy-1`, `nexa_b10_*`, stack lạ `nexa_b17_caddy_ec2189ce` và process `b17_e2e_stack.py up --tier w1` (pid 95133) của phiên khác |
| DB giữ lại (để review tái lập) | `nexa_b05_test_b19` (pytest PG), `nexa_b05_test_b19e2e` (stack), `nexa_b05_test_b19docker` (Docker) |
| DB đã xóa | `nexa_b05_test_b19perf` (EXPLAIN 100 k job, 438 MB), `nexa_b05_test_b19ci` (CI replay) |
| DB của task trước | không chạm (`nexa_b05_test_b18*` và các DB khác) |
| Image local (không push) | `nexa/cpu-iterative:b19` (`a35ca287…`), `nexa/b19-worker:local` (r2 `5d35ccb5…`), `nexa/b19-worker:r0` (`d563c2c3…`), `prom/prometheus:v3.5.0` (pull theo digest) |
| Tệp tạm | đã xóa toàn bộ `/tmp/b19_*` và bản sao `/tmp/b19_ci`; `web/test-results` (ignored, Playwright) đã xóa; không có trace/HAR/video/storageState trong repo |
| Docker Desktop | không đổi cấu hình |
| VPS1 | không dùng (L = not-run) |
| Git | không commit/push/branch; thay đổi nằm trong working tree (`git status`) |

## 23. Vòng sửa 2 (Task Review vòng 1: Không duyệt, RV01–RV11)

Phạm vi: sửa RV01–RV03 (chặn) và RV04–RV11 (không chặn) theo review vòng 1; các mục "minor, không
bắt buộc" không đổi. Không đổi payload REST, contract, migration (0024 vẫn không cần), compose,
`deploy/b10/**`, Caddyfile, web code hay spec B18. Source listing (174 tệp) sau vòng 2:
`c36d2e0a07ade2090a61be3d91e394b6afae927935fc8c884238b14ea6589163` = nội dung worker image **r3**
`nexa/b19-worker:local` id `sha256:5a6039116ffe1a58244ec2c7ccdb283f940ae4d5d7498b745e8f48d22c3a2528`
(build 2026-10-04T17:44Z, `raw/B19-build-worker-r3.out`; hash tính trong
`/opt/nexa/.venv/lib/python3.12/site-packages`). Image r2 giữ tag `nexa/b19-worker:r2`. CPU image
`a35ca287…` không build lại: 18 tệp runner closure không đổi ở vòng 2.

### 23.1 Từng finding

| RV | Sửa | Tệp chính | Test (mới/sửa) | Kiểm tra âm (trước sửa) | Trạng thái |
|---|---|---|---|---|---|
| RV01 (chặn) | `http_request` ghi `method_label()` (tập đóng `HTTP_METHODS`, còn lại `OTHER`), cùng hàm với metrics; mọi pattern redaction neo bằng look-behind và quantifier possessive, nên tuyến tính theo độ dài | `observability/metrics.py`, `observability/logging.py`, `api/app.py` | `test_access_line_logs_the_closed_method_label_not_the_raw_method`, `test_redact_text_is_linear_on_long_inputs` (64 KiB < 50 ms), `test_real_uvicorn_long_method_does_not_stall_other_requests` (uvicorn thật, method 16 KiB, request song song < 0,5 s, log không có method thô) | regex cũ 7,68 s trên 16 000 ký tự; test linear và uvicorn fail | **đóng** |
| RV02 (chặn) | Phục vụ `/metrics` gọi `refresh_readiness` qua `CachedReadiness` (TTL ≤ 2 s, single-flight) ở cả 3 process; probe ném lỗi → mọi `nexa_ready{check}` = 0 (`on_ready_failure`) | `observability/ops_server.py`, `api/ops.py`, `coordinator/ops.py`, `worker/ops.py` | 4 test "metrics scrape alone" (ops server, API, coordinator, worker): 0 khi check hỏng, 1 khi phục hồi, không gọi `/readyz` | 4 test fail không có sửa | **đóng**; sống: §23.3 |
| RV03 (chặn) | `preinitialize()` mọi tổ hợp label đóng của counter dùng trong `increase()`/`rate()` (13 lời gọi trong `metrics_api/coordinator/worker.py`, gồm `RESTORE_OUTCOMES`); rà toàn bộ 16 alert, ghi chú trong `alerts.yml`; thêm test promtool cho series chưa khởi tạo | `observability/metrics*.py`, `deploy/prometheus/alerts.yml`, `tests/alerts_test.yml` | `test_closed_counters_are_exposed_at_zero_from_import` (`nexa_restore_total{outcome="failed"} 0.0` ngay sau import), promtool test rules | test fail không có sửa | **đóng** |
| RV04 | Ánh xạ mã nội bộ của store → `503 dependency_unavailable` ghi đúng một dòng WARNING `artifact_store_unavailable` với `code`, `operation` (không ghi message có thể chứa path); docs §6.3 sửa (trước ghi nhầm `internal_error`) | `application/storage_pressure.py`, `docs/observability.md` | `test_mapping_to_dependency_unavailable_logs_the_internal_code_once` (5 tham số) | bỏ dòng log → 2 failed | **đóng** |
| RV05 | Probe fsync storage chỉ chạy cho admin OFF→NORMAL mới, sau authorize và replay idempotency (`_StorageProbeRequired`) | `application/policy_service.py` | `test_storage_is_probed_only_for_an_authorized_fresh_reopen` | fail không có sửa | **đóng** |
| RV06 | Ops listener: deadline tổng 5 s cho cả request head (Timer), slowloris bị cắt | `observability/ops_server.py` | `test_slow_request_head_is_dropped_at_the_deadline` | fail không có sửa | **đóng** |
| RV07 | `NEXA_LOG_LEVEL` áp cho `uvicorn` và `uvicorn.error` | `observability/logging.py` | `test_real_uvicorn_honours_nexa_log_level_for_uvicorn_loggers` | — | **đóng** |
| RV08 | Docs: json-file `max-size` = min(giá trị, 1 MiB), trường `exception`, runbook `metrics_collector_failed`, danh sách event đúng code (thêm 8, bỏ 2 không tồn tại) | `docs/observability.md`, `deploy/prometheus/alerts.yml`, `tests/alerts_test.yml` | promtool | — | **đóng** |
| RV09 | `local_cleanup` mở thư mục vùng bằng `O_DIRECTORY\|O_NOFOLLOW`, xóa tương đối descriptor (`rmtree(dir_fd=…)`), fsync thư mục; symlink → bỏ qua và log `worker_local_cleanup_failed` `unsafe_area_directory` | `worker/local_cleanup.py` | `test_a_symlinked_area_directory_is_never_followed[downloads\|materialized]` | code cũ xóa nội dung ngoài cây | **đóng** |
| RV10 | Redaction che `Authorization: Basic …` (giữ tên scheme) và giá trị `[...]`/`{...}` (2 mức) | `observability/logging.py` | `test_redaction_masks_auth_schemes_and_bracketed_values` (7 ca) | `redact_text` của vòng 1 (lấy từ image r2) rò 5/7 ca, ví dụ `Authorization: [REDACTED] dXNl…`, `secret: [REDACTED], dXNl…]` | **đóng** |
| RV11 | Tuổi checkpoint = now − GREATEST(checkpoint mới nhất, `started_at` của attempt) | `api/collectors.py`, `docs/observability.md` | `test_age_counts_from_the_later_of_the_last_checkpoint_and_the_attempt_start` (PG) | 1,41 s thay vì 0,016 s | **đóng** |

### 23.2 Finding mới trong vòng 2

| ID | Quan sát | Bằng chứng | Trạng thái |
|---|---|---|---|
| R26 | Kịch bản 17 của B18 (`w2-admin/01-workers.spec.ts:395`) fail 2/2 lượt đầu với r3: spec đọc `version` lúc mở trang (v20), nhưng sau khi allocation nhả, worker lật STARTING→READY (R20, có từ B15-R07) làm version tăng hai lần trong ~5 s; drain của admin một bị 412 rồi chờ chu kỳ refresh 5 s, trong lúc đó trang admin hai tự đọc lại v21, nên thông báo là "v21 → v22" thay vì "v20 → v22". Bisect: chỉ `w2-admin`, r2 pass 2/2, r3 pass 1/1; lệnh chính thức `w2-admin + w2-admin-mobile` trên r3 pass 2/2 liên tiếp. Thay đổi worker của vòng 2 (`local_cleanup`, `ops`, metrics) không chạm heartbeat/health. Đây là race thời điểm của spec B18, không phải lỗi B19; spec thuộc B18 nên không sửa | `raw/B19-playwright-r2.out` (gồm trích network từ trace) | **ghi nhận**; đề xuất cho owner B18: spec đọc `version` từ `If-Match` thực gửi thay vì từ trang lúc mở |

### 23.3 Verification vòng 2 (cây cuối `c36d2e0a…`, trong phiên này trên P)

| Gate | Kết quả | Raw |
|---|---|---|
| Ruff check / format | All checks passed; 564 files already formatted | `raw/B19-final-gates-r2.out` |
| `git diff --check` + `git diff --no-index --check /dev/null <f>` cho 76 tệp untracked | exit 0; 0 tệp lỗi | — |
| Pytest mặc định | **1973 passed, 683 skipped**, exit 0 | `raw/B19-final-gates-r2.out` |
| CI replay (full PG) | bản sao sạch `/tmp/b19_ci`, các bước job `python`: `uv sync --frozen --all-groups --no-editable` exit 0; ruff All checks passed; format 738 files (= 564 + 174 `build/lib`); `pytest -q --run-postgres -rs` trên DB mới `nexa_b05_test_b19ci` với `NEXA_TEST_PG_CLIENT_PREFIX`: **2628 passed, 28 skipped**, exit 0 (799,1 s); 0 FAILED/ERROR. +37 so với CI replay vòng 1 (2591) = test mới của vòng 2 | `raw/B19-ci-replay-r2.out` |
| promtool | check rules 16 SUCCESS; test rules SUCCESS (gồm series chưa khởi tạo); check config SUCCESS | `raw/B19-final-gates-r2.out` |
| Web | install/typecheck/build exit 0; Vitest 29 files / 217 tests; dist `b57852b8a94a` (không đổi) | `raw/B19-final-gates-r2.out` |
| Docker (r3, 17:44:56Z) | runner bounds **6 passed**; runner + executor + B11 IPC + vertical **17 passed** | `raw/B19-docker.out` (phần round 2) |
| Scrape + alert sống (run `d94b8962`, r3, alert-for 20s) | `failures=[]`; trước mọi `/readyz`, `nexa_ready` từ `/metrics` = 1 cho cả 3 job; **RV02 sống**: chmod staging 0500 → `nexa_ready{job="nexa-api",check="storage"}` = 0 sau 2,1 s, NexaNotReady pending 3,1 s, firing 22,6 s; khôi phục → 1 sau 26,8 s, resolved 27,9 s. NexaTargetDown, NexaWorkerNotReady firing; groups_missing `[]`, không label ID; consistency/storage-check OK; canary 30 giá trị × 3 log: 0 hit. NexaGcNeverCompleted firing như vòng 1 (GC pass đầu sau 60 s > alert-for 20 s, đúng thiết kế) | `raw/B19-scrape-r2.{json,log}` |
| Outage (run `53b76ff5`, r3) | `failures=[]`; hammer 503/503 trả 200 (p50 8,8 ms, p99 251,3 ms, max 303,9 ms); 5xx 0/0; collector errors 9, chuyển down/up × 4; lock [0,0,0,0]; 8/8 SUCCEEDED; audit +58, events +40; 4xx poll = 4 (409 khi worker tạm STARTING, R20); canary 44 × 3: 0 hit | `raw/B19-outage-r2.{json,log}` |
| Playwright (r3, retries 0) | w1-admin-mode 1, w1-desktop + w1-mobile 23, w1-admission 1, w1-admin 20, w1-admin-frozen 1, w2 4 passed; w2-admin + w2-admin-mobile: 5 passed / 1 failed × 2 lượt đầu (R26), sau đó **6 passed × 2** trên cùng lệnh | `raw/B19-playwright-r2.out` |

Mọi raw đã che password (`sed`); `grep` password trên `docs/evidence/raw/` = 0 dòng.

### 23.4 Trạng thái môi trường khi bàn giao vòng 2

Thay §22 ở các điểm sau; phần còn lại của §22 giữ nguyên.

| Thành phần | Trạng thái |
|---|---|
| Image local (không push) | `nexa/b19-worker:local` = **r3** `5a603911…` (cuối), `nexa/b19-worker:r2` `5d35ccb5…` (vòng 1, dùng cho bisect R26), `nexa/b19-worker:r0` `d563c2c3…`, `nexa/cpu-iterative:b19` `a35ca287…`, `prom/prometheus:v3.5.0` |
| DB | giữ `nexa_b05_test_b19`, `nexa_b05_test_b19e2e`, `nexa_b05_test_b19docker`; `nexa_b05_test_b19ci` tạo lại cho CI replay vòng 2 rồi đã xóa |
| Container/network/volume B19 | không còn (harness dọn sau mỗi run) |
| Tệp tạm | đã xóa `/tmp/b19_*`, bản sao `/tmp/b19_ci`, trace Playwright giải nén; không có `web/test-results`, trace/HAR/video/storageState trong repo |
| Không chạm | `nexa_b13_pg` (chỉ tạo/xóa DB của B19), `nexa_b10_*`, `nexa_b17_caddy_ec2189ce`, process `b17_e2e_stack.py up --tier w1` (pid 95133), DB `nexa_b05_test_b18*`, spec B18 `01-workers.spec.ts`, cấu hình Docker Desktop |
| VPS1 | không dùng (L = not-run) |
| Git | không commit/push/branch |
