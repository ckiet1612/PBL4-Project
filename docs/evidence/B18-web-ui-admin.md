# B18 — Web UI quản trị: evidence

Trạng thái: **đã triển khai và kiểm chứng, chờ Task Review độc lập** (Task Code, 2026-10-01).
Tài liệu này giữ kế hoạch, thay đổi, verification và acceptance của B18; `docs/acceptance.md`
giữ nguyên `specified`. IA chi tiết nằm ở
[docs/web-ui.md](../web-ui.md#khu-quản-trị-b18), ghi lúc **2026-10-01T08:03:58Z**,
trước mọi code UI quản trị.

## 1. Kế hoạch

### 1.1 Context map

| Yêu cầu | Contract | Code có sẵn | Test có sẵn | Thiếu |
|---|---|---|---|---|
| Admin xem/drain/disable/enable worker | `adminListWorkers`…`adminEnableWorker`, `adminListAllocations` (openapi.yaml) | `application/admin_workers.py`, `api/routes_admin.py` | `tests/api/test_admin_workers*.py`, operation matrix | UI |
| Tenant/user/membership | `adminListTenants`…`adminDeleteMembership` | `application/admin_service.py` | `tests/api/test_admin_*`, PG integration | UI |
| Quota/weight, global policy, mode | `adminGet/UpdateTenantPolicy`, `adminGet/UpdateGlobalPolicy` | `application/policy_service.py`, `domain/policy.py` | policy tests | UI; WRITE_FROZEN không đạt được qua API (B18-R18) |
| Hàng chờ toàn hệ thống | `adminListJobs`, `adminGetJob` | `JobService._job_query/_job_view`; CLI `job` Typer đã định nghĩa nhưng chưa đăng ký | `test_api_route_contract.py` liệt kê là EXCLUDED | Route, service, index toàn cục, CLI |
| Fairness | `adminQueryFairness`, `FairnessReport` | `allocation_ledger_segments` (B13), `accounting.py` | ledger tests B13 | Toàn bộ backend + CLI |
| Recovery/audit | `adminListRecoveryEvents`, `adminListAuditRecords` | có route | có | UI |
| Shell B17 | – | `GlobalNav` chỉ render khi có tenant; `refreshCsrf` không kiểm user; không có error boundary | Vitest B17 | B18-R13/R14/R15 |
| Harness | – | `scripts/b17_e2e_stack.py`, Playwright w1/w2 | B17 specs | admin thứ hai, project admin |

### 1.2 Tóm tắt IA

Khu `/admin/*` riêng trong cùng app, guard theo `SYSTEM_ADMIN`; secondary nav 4
nhóm (Vận hành, Tổ chức, Chính sách, Giám sát); global nav có `Quản trị` cả khi
admin không có tenant. Trang tải một lần + `Làm mới` vì GET admin ghi audit;
chỉ chi tiết worker tự cập nhật trong lúc chuyển trạng thái (5 s ×1,5 ≤ 60 s,
dừng sau 10 phút). Mọi mutation qua ConfirmDialog nêu hậu quả/hoàn tác. Chi tiết:
sitemap, nav, trang, component, workflow A-01..A-14, ma trận worker, ma trận
chế độ, chức năng gộp, UX-A16..A28 trong docs/web-ui.md.

### 1.3 Quyết định và lý do

| # | Quyết định | Lý do |
|---|---|---|
| D1 | Fairness tính bằng SQL aggregate trên `allocation_ledger_segments` (numeric), kèm module Python thuần làm mô hình tham chiếu và kiểm tham số | Không tải segment vào API; Python thuần cho test Hypothesis đối chiếu |
| D2 | Cast EXACT_DECIMAL_TEXT bằng built-in (`split_part`, `::numeric`) với tên schema đầy đủ `pg_catalog`, không phụ thuộc hàm của migration hay `search_path` | Tránh lặp lỗi search_path (B13-R12) |
| D3 | Admin jobs dùng lại `JobService._job_query/_job_view`, cursor ký bind actor + operation + mọi filter | Một view Job duy nhất; cursor không dùng lại chéo |
| D4 | Migration 0023 chỉ thêm index nếu EXPLAIN trên seed lớn chứng minh cần | Ràng buộc prompt; ACC-29 ghi |
| D5 | UI không polling danh sách admin | Mỗi GET admin = một dòng audit (B18-R02) |
| D6 | Kịch bản 13: khẳng định 409 thật của server cho ADMISSION_OFF → WRITE_FROZEN; trạng thái UI WRITE_FROZEN kiểm trên stack riêng do harness seed thẳng vào DB (chỉ test, ghi rõ) | WRITE_FROZEN không đạt được qua API (B18-R18) |
| D7 | Chi tiết job admin chỉ hiện object Job | progress/attempts/events là route tenant |
| D8 | Mutation không phải worker không có ô lý do | Contract không có trường `reason` cho chúng (UX-A22) |

### 1.4 Findings và interpretation (B18-Rxx)

Trạng thái: **CLOSED** = điều kiện đóng đạt, có evidence trong phiên này;
**ÁP DỤNG** = interpretation của prompt đã thực hiện, không phải lỗi;
**OPEN** = còn mở, ghi task nhận. Không finding nào được đánh dấu đóng chỉ vì UI che lỗi.

| ID | Mô tả | Nguyên nhân gốc | Test tái hiện | Điều kiện đóng | Evidence / trạng thái |
|---|---|---|---|---|---|
| B18-R01 | Thiếu 3 route `adminListJobs`, `adminGetJob`, `adminQueryFairness` | Contract có operation nhưng B15 chưa wire; `test_api_route_contract.py` liệt kê EXCLUDED | `test_openapi_b18.py`, `test_admin_jobs_b18.py`, `test_admin_fairness_b18.py`, `test_cli_admin_b18.py`, operation matrix, route contract | Route/CLI đúng contract, scope exact `admin:read`, PG xanh | **CLOSED**: §2.1, §3.2 |
| B18-R02 | Mỗi GET admin ghi một dòng audit | Thiết kế B15 (ACC-26): lượt đọc admin cũng audit | W1 10 (clock 120 s, 0 request); đo §5 | UI không polling danh sách; số dòng đo được | **ÁP DỤNG**: §5, W1 10 pass |
| B18-R03 | Không có API tổng sức chứa đang giữ | Contract chỉ có danh sách allocation phân trang | `actions.test.ts` (tổng + cờ "chưa đầy đủ"), W2 14–15 ("Đang giữ" 1 core) | UI cộng trang đầu HELD + QUARANTINED (≤ 100), báo "chưa đầy đủ" khi có trang sau | **ÁP DỤNG**; đề xuất contract aggregate → OPEN cho task contract |
| B18-R04 | Không có API đếm hàng chờ | Contract không có count | – | UI không hiện tổng số | **ÁP DỤNG**; đề xuất contract → OPEN |
| B18-R05 | Mở lại chế độ (ADMISSION_OFF → NORMAL, WRITE_FROZEN → ADMISSION_OFF) cần recovery proof (từ B17-R18) | `PolicyService` chỉ có `FailClosedRecoveryProofProvider` | `mode.spec.ts` 13, `frozen.spec.ts` 13, `modes.test.ts` | UI hiện 409 thật + cảnh báo "chưa mở lại được" | **OPEN** (backend recovery proof, B19/task recovery); phần UI pass |
| B18-R06 | Ngữ nghĩa fairness | Prompt 6.C | `test_fairness_report_b18.py` (Hypothesis 200 ví dụ, offset µs từ vòng 2), `test_admin_fairness_b18.py` | Bucket nửa mở, overlap theo **ms sàn** (B18-R26), segment mở đo tới giờ DB của statement, khớp ledger | **Vòng 1 đóng sai** (RV01: chỉ khớp vì test cắt thời gian segment về ms). Vòng 2: unit pass; PG `test_admin_fairness_b18.py` 19 passed × 3 trên segment thật, không cắt thời gian (§15.3) → **CLOSED** (vòng 2) |
| B18-R26 | **Mới (vòng 2, RV01).** Prompt mâu thuẫn: overlap theo µs (dòng 591) và "bằng ledger" (dòng 604); ledger tính `floor_ms(end) − floor_ms(start)` qua `epoch_ms`, nên tích phân µs trên segment thật lệch ~1e-3 tương đối | Hai câu của prompt không cùng đúng trên dữ liệu thật | `test_fairness_report_b18.py` (Hypothesis có offset µs); `test_admin_fairness_b18.py::test_normalized_service_equals_the_charged_ledger_amount` bỏ `_truncate_segment_times_to_milliseconds`, segment thật của coordinator, sai số 1e-9 | Interpretation (a): mọi mốc (segment start/end, cận bucket, giờ statement) lấy sàn về ms như `epoch_ms` trước khi tính overlap; SQL tính bigint ms | **ÁP DỤNG** (interpretation): `fairness_report.py`, `admin_queries.py`, docs/coordinator.md; điều kiện đóng PG đạt: 19 passed × 3, sai số 1e-9 (§15.3) |
| B18-R07 | Vượt 1000 dòng/bucket → 400, không cắt bớt | Prompt | `test_more_than_1000_tenant_buckets_is_rejected`, `fairness.test.ts` | `LIMIT 1001` → 400 | **CLOSED** |
| B18-R08 | Mật khẩu chỉ trong state của form | Prompt (ACC-24) | W1 3 (URL/storage/console/body), `PasswordFields.tsx` | Ô bị xóa sau intent; không xuất hiện ngoài form | **CLOSED** |
| B18-R09 | Tên tenant/user chỉ từ danh sách đã tải | Không có API tra tên hàng loạt; tra từng cái = audit | – | UX-A20 | **ÁP DỤNG** |
| B18-R10 | Không có API membership theo user | Contract chỉ có membership theo tenant | – | Quản lý ở tab Thành viên của tenant | **ÁP DỤNG**; đề xuất contract → OPEN |
| B18-R11 | Playwright không chạy trong CI | `ci.yml` chỉ có typecheck/build/vitest | – | – | **OPEN** (B25/CI); ghi giới hạn ACC-39 |
| B18-R12 | Tối đa một migration, chỉ index | Ràng buộc prompt | `test_migration_b18.py` (4) | 0023 chỉ thêm 2 index, có downgrade, parity | **CLOSED**: §4 |
| B18-R13 | Nav không có slot admin khi admin không có tenant | `GlobalNav` B17 chỉ render khi có tenant | `nav.test.ts`, W1 1 (admin không membership) | Link `Quản trị` hiện cả khi không tenant | **CLOSED** |
| B18-R14 | `refreshCsrf` không kiểm user | B17 lấy CSRF mới mà không so `user_id` | `client.test.ts` "does not resend the mutation and does not send the new user to login"; vòng 2: `csrfRefresh.test.ts` (5) chạy client thật, không stub `refreshCsrf` | Khác user → xóa state, không gửi lại mutation; **vòng 2 (RV03)**: gắn với user đã gửi request, refresh đồng thời dùng chung một lần đọc session, request gửi khi chưa biết user không gửi lại | Vòng 1 chỉ đúng khi một mutation (RV03: mutation thứ hai đọc user mới sau `await` và gửi lại dưới cookie B). Vòng 2: `auth/csrfRefresh.ts`; Vitest pass, đỏ trước `raw/B18-RV03-red.out`; W1 admin 20 + B17 W1 23 passed trên dist `de4c3dbb3979` (§15.3) → **CLOSED** (vòng 2) |
| B18-R15b | **Vòng 2 (RV10).** Error boundary không reset khi chuyển route | Một boundary ngoài router | `AppErrorBoundary.test.ts` (đỏ: `raw/B18-RV10-boundary-red.out`, 2 failed / 1 passed) | Boundary quanh trang trong layout, khóa `pathname` | **CLOSED** (vòng 2): Vitest pass; W1 admin 20 passed |
| B18-R15 | Không có error boundary | B17 không có | `AppErrorBoundary.test.ts` | Lỗi render → fallback, không giữ error | **CLOSED** |
| B18-R16 | Job admin chỉ xem | Contract không có control admin cho job | `test_admin_jobs_b18.py` (get read-only, cancel admin 403/404), W1 7 | Không control trên trang job admin | **CLOSED** |
| B18-R17 | Audit/recovery không có trần 31 ngày ở server | Contract | – | UI mặc định 24 h, không thêm trần riêng | **ÁP DỤNG** |
| B18-R18 | ADMISSION_OFF → WRITE_FROZEN luôn 409 "Freeze requires stopped containers and reconciled unreleased allocations" | `freeze_ready` của `FailClosedRecoveryProofProvider` luôn False; kịch bản 13 giả định chuyển được | `mode.spec.ts` 13 khẳng định 409 | Backend có proof thật | **OPEN** (ngoài B18); UI WRITE_FROZEN kiểm bằng seed DB chỉ cho test (D6) |
| B18-R19 | Slug: service `^[a-z][a-z0-9-]{1,61}[a-z0-9]$` (3–63) hẹp hơn contract `{1,62}` (3–64) | Regex service lệch contract | Phát hiện khi đọc code; không có test tự động tái hiện (B18 không sửa backend này) | Service theo contract | **OPEN**, không chặn |
| B18-R20 | `gpu_count` của `resource_limit` tenant bị service giới hạn ≤ 1, contract cho tới 64 | Service lệch contract | – | Service theo contract | **OPEN**, không chặn; UI không chặn thêm |
| B18-R21 | **Mới.** Khi WRITE_FROZEN, user không phải admin đăng nhập nhận 401 chung trước khi kiểm mật khẩu, nên thông báo frozen của B17 ("chỉ admin đăng nhập được") không bao giờ hiện | `identity_service.py` `_record_login_attempt` (~424/435) ném `_AUTHENTICATION_ERROR`; nhánh 403 "Only an enabled system administrator may login while frozen" (~585) không đạt được với non-admin | `frozen.spec.ts` (lượt red3 kỳ vọng thông báo B17 → fail; spec giờ khẳng định 401) | Backend trả lỗi phân biệt được, hoặc B17 bỏ thông báo | **OPEN** (backend B06/identity) |
| B18-R22a | **Mới.** Sau khi allocation đổi (job xong/RELEASED), worker có cửa sổ STARTING/kiểm READY lại; enable/drain trong cửa sổ đó nhận 409 "The worker is not READY"/"The latest worker heartbeat did not pass the READY checks" hoặc 412 vì version tăng | Version worker tăng khi health/`ready_at`/admin_state đổi; enable đòi heartbeat mới nhất đã qua READY (`admin_workers.py` 508–556) | W2 red1 (badge "Đang khởi động" thay "Sẵn sàng", enable 409); annotations §7 | UI hiện lý do server, đọc lại; admin bấm lại | **ÁP DỤNG** (đúng semantics fail-closed); spec ghi mọi lần từ chối (`actUntilAccepted`), không giấu |
| B18-R22b | **Mới, đã sửa.** ETag worker đổi ở mỗi lần rediscover inventory dù inventory không đổi → admin luôn 412 | Checksum inventory gồm `discovered_at` | `test_rediscovered_inventory_keeps_the_worker_etag` (đỏ: `raw/B18-R22-red.out`, version đổi) | Checksum bỏ `discovered_at`; test xanh; worker image build lại | **CLOSED**: `worker_service.py`, PG §3.2, W2 cuối |
| B18-R23 | **Mới, đã sửa.** Câu hoàn tất drain/disable có thể hiện sớm từ danh sách allocation đọc **trước** thao tác | `transitionStatus` không biết danh sách đọc trước hay sau POST | `actions.test.ts` "lists read before the action never prove drain or disable finished" (đỏ: `raw/B18-R23-red.out`, 1 failed / 11 passed) | Chỉ báo xong khi danh sách đọc sau thao tác | **CLOSED**: dist `9c9397bd5322`, W2 14–16 |
| B18-R24 | **Mới, quan sát.** Audit `artifact.upload.commit` do worker gửi ghi `actor_type` USER với id worker, trang Audit hiện "User …" | `artifact_service.py:619-630` (B07) | ảnh `08-desktop-audit.png` | Backend ghi đúng loại actor | **OPEN** (B07/B20); UI hiện đúng dữ liệu server, ghi trong docs/web-ui.md |
| B18-R25 | **Mới, quan sát, không chặn.** pytest PG có warning thứ ba `UnsupportedFieldAttributeWarning` (pydantic, alias `worker_id`/`Idempotency-Key`) | FastAPI/pydantic dựng TypeAdapter lười của tham số route khi nhiều request đầu tiên chạy đồng thời (nhánh `AttributeError: __pydantic_core_schema__` trong `type_adapter.py`), trên route B08/B15 B18 không sửa | `tests/integration/test_jobs_b08.py::test_concurrent_different_keys_respect_queue_and_rate_limits` chạy lại 3 lần: warning ở 1/3 lần, test luôn pass | Không đổi dependency (cấm); theo dõi khi nâng FastAPI/pydantic | **OPEN** (không thuộc B18) |

### 1.5 Mốc và task

Mỗi task: test viết trước (đỏ đúng lý do) → code → test + `ruff`/`typecheck`.

| Mốc | Task | File chính | Test viết trước | Lệnh kiểm |
|---|---|---|---|---|
| M1 | IA, kế hoạch | docs/web-ui.md, file này | – | review |
| M2 | Fairness: hàm thuần (kiểm tham số, bucket, overlap, gộp) | `src/nexa/application/fairness_report.py` | `tests/unit/test_fairness_report.py` (+ Hypothesis) | `pytest tests/unit/test_fairness_report.py` |
| M2 | Fairness: SQL aggregate + service + route | `fairness_report.py`, `admin_workers.py` hoặc service mới, `routes_admin.py` | `tests/integration/test_admin_fairness_pg.py` (đối chiếu mô hình, ledger consistency, read-only, không chặn `charge_locked`) | `pytest --run-postgres tests/integration/test_admin_fairness_pg.py` |
| M2 | Seed ≥ 200k segment / ≥ 60 ngày, EXPLAIN 1 h / 24 h / 31 d ± tenant | `scripts/b18_explain.py` | – | raw `docs/evidence/raw/B18-explain-*.out` |
| M3 | Admin jobs list/get + cursor + audit | `job_service.py`, `routes_admin.py` hoặc `routes_jobs.py` | `tests/api/test_admin_jobs.py`, `tests/integration/test_admin_jobs_pg.py` | pytest |
| M3 | Seed ≥ 100k job / ≥ 20 tenant, EXPLAIN 7 truy vấn; quyết định 0023 | `scripts/b18_explain.py`, `migrations/versions/0023_*`, `schema_v20.py` | `test_migrations.py`, `test_indexes.py` | pytest PG |
| M4 | CLI `admin job list|get`, `admin fairness query`; route contract; OpenAPI parity; operation matrix | `cli/commands/admin.py`, `tests/cli/*`, `tests/api/test_openapi_b18.py`, `test_operation_matrix.py`, docs/cli.md | các test đó | pytest |
| M5 | Shell: guard, nav, error boundary, refreshCsrf, endpoints/types admin | `web/src/app/*`, `auth/session.tsx`, `api/endpoints.ts`, `api/types.ts`, `features/admin/AdminLayout.tsx` | Vitest: nav visibility, refreshCsrf user khác | `pnpm --dir web run test`, `typecheck` |
| M6 | Worker: danh sách, chi tiết, sức chứa, drain/disable/enable, transition poll | `features/admin/workers/*`, `actions.ts`, `transitionPoll.ts`, `units.ts` | Vitest: ma trận, poller fake timers, tổng sức chứa + cờ chưa đầy đủ | vitest + typecheck |
| M7 | Tenant/user/membership | `features/admin/tenants/*`, `users/*` | Vitest: ánh xạ lỗi admin | vitest |
| M8 | Chính sách hệ thống/chế độ, chính sách tenant | `features/admin/policy/*`, `modes.ts` | Vitest: lựa chọn chế độ, parse số `,`/`.`, đổi đơn vị, diff field | vitest |
| M9 | Hàng chờ + chi tiết job | `features/admin/queue/*` | Vitest: nhãn | vitest |
| M10 | Fairness, khôi phục, audit | `features/admin/fairness/*`, `recovery/*`, `audit/*`, `fairness.ts`, `labels.ts` | Vitest: pre-check 31 ngày/1000 bucket, tổng theo tenant | vitest |
| M11 | Harness + Playwright w1-admin, w1-admin-mode, w1-admin-frozen, w2-admin, mobile; hồi quy B17 | `scripts/b17_e2e_stack.py`, `web/tests/e2e/admin*/**`, `playwright.config.ts` | spec đỏ trước | harness `run --tier w1|w2` |
| M12 | Docs, evidence, ảnh, tự review, chạy lại toàn bộ gate | docs/* | – | toàn bộ lệnh mục 3 |


Ghi chú: bảng mốc trên giữ tên tệp lúc lập kế hoạch (ví dụ `tests/unit/test_fairness_report.py`,
`features/admin/queue/*`). Tên thật khi triển khai nằm ở §2 (ví dụ
`tests/application/test_fairness_report_b18.py`, `features/admin/jobs/*`, `features/admin/monitor/*`).

## 2. Thay đổi chính theo từng file

Tracked: 38 tệp, +1287/−113 (`git diff --stat`); untracked: 77 tệp code/test/doc ngoài
`docs/evidence/raw/` (liệt kê dưới đây).

### 2.1 Backend, migration, CLI

| Tệp | Thay đổi |
|---|---|
| `src/nexa/api/routes_admin.py` | 3 route: `GET /v1/admin/jobs` (`adminListJobs`), `GET /v1/admin/jobs/{job_id}` (`adminGetJob`), `GET /v1/admin/fairness` (`adminQueryFairness`); scope `admin:read`, không tenant header, mỗi request một dòng audit (`job.list`, `job.get`, `fairness.query`) |
| `src/nexa/api/schemas.py` | `FairnessReport`/`FairnessBucket` đóng (`extra=forbid`), tối đa 1000 dòng |
| `src/nexa/application/admin_queries.py` (mới) | `AdminQueryMixin`: list/get job dùng lại `JobService._job_query/_job_view` (D3), cursor ký bind actor + operation + mọi filter; truy vấn fairness bằng SQL aggregate trên `allocation_ledger_segments` (D1, D2) |
| `src/nexa/application/fairness_report.py` (mới) | Kiểm tham số (khoảng ≤ 31 ngày, bucket 1..86400 s nguyên, ≤ 1000 bucket, timezone), mô hình tham chiếu Python thuần (bucket nửa mở, cắt overlap, segment mở tới giờ statement, fallback weight), serialize |
| `src/nexa/application/admin_workers.py` | `AdminWorkerService(AdminQueryMixin, AdminService)` |
| `src/nexa/application/worker_service.py` | B18-R22b: checksum inventory bỏ `discovered_at` |
| `src/nexa/infrastructure/persistence/schema.py`, `schema_v20.py` (mới) | Metadata khai báo 2 index của 0023 |
| `migrations/versions/20261001_0023_b18_admin_read_indexes.py` (mới) | Chỉ index: `ix_jobs_created_keyset (created_at DESC, job_id DESC)` và GiST `ix_allocation_ledger_segments_period` trên `tstzrange(started_at, ended_at, '[)')`; downgrade xóa đúng 2 index |
| `src/nexa/cli/commands/admin.py` | Đăng ký `admin job list|get` (đã định nghĩa từ trước nhưng chưa gắn) và `admin fairness query` |
| `scripts/b18_explain.py` (mới) | Seed lớn + EXPLAIN (ANALYZE, BUFFERS) + đo chi phí ghi, chỉ chạy với `NEXA_TEST_DATABASE_URL` guarded |

Test Python:

| Tệp | Nội dung |
|---|---|
| `tests/api/test_openapi_b18.py` (mới, 3) | Hình dạng operation admin job/fairness, không có 422 cho fairness, model báo cáo đóng và có trần |
| `tests/application/test_fairness_report_b18.py` (mới, 10) | Khoảng ≤ 31 ngày, giới hạn/đếm bucket, timezone, bucket nửa mở, overlap tách/cắt, segment mở, fallback weight, dòng thưa có thứ tự, serialize/từ chối > 1000; Hypothesis 200 ví dụ: tổng của khoảng đóng = charge ledger |
| `tests/integration/test_admin_fairness_b18.py` (mới, 6, PG) | SQL khớp mô hình tham chiếu; segment mở đo tới giờ statement; service chuẩn hóa = lượng ledger đã charge; validation/audit/authorization; > 1000 → 400; truy vấn là read thuần, không chặn accounting |
| `tests/integration/test_admin_jobs_b18.py` (mới, 3, PG) | Thứ tự/filter/trang; cursor bind admin + vai trò; get chỉ đọc, không control |
| `tests/integration/test_cli_admin_b18.py` (mới, 1, PG) | CLI end-to-end qua API thật |
| `tests/integration/test_migration_b18.py` (mới, 4, PG) | Upgrade chỉ thêm 2 index, downgrade xóa; SQL offline chỉ index; metadata khai báo; truy vấn đọc dùng được index |
| `tests/integration/_admin_b18.py` (mới) | Helper seed |
| `tests/cli/test_admin_commands.py` | +4 test CLI (forward filter không tenant header, get một job, fairness forward khoảng, input sai bị chặn trước transport) |
| `tests/api/test_operation_matrix.py`, `tests/cli/test_api_route_contract.py`, `test_control_commands_b15.py`, `test_pagination_and_headers.py` | 3 operation mới vào ma trận; bỏ khỏi EXCLUDED |
| `tests/integration/test_admin_workers_b15.py` | B18-R22b `test_rediscovered_inventory_keeps_the_worker_etag` |
| `tests/integration/test_indexes.py` | Danh sách index thêm 2 index 0023 |

### 2.2 Web

| Tệp | Thay đổi |
|---|---|
| `web/src/App.tsx` | Route `/admin/*` dưới guard `SYSTEM_ADMIN`, lazy theo trang; `AppErrorBoundary` |
| `web/src/app/nav.ts` (mới), `AppLayout.tsx`, `SimplePages.tsx` | B18-R13: slot `Quản trị` cả khi không có tenant; `Mở khu quản trị` ở `/` cho admin không membership |
| `web/src/app/AppErrorBoundary.tsx` (mới) | B18-R15 |
| `web/src/auth/session.tsx`, `web/src/api/client.ts` | B18-R14: `refreshCsrf` so user, khác thì xóa state và không gửi lại |
| `web/src/api/endpoints.ts`, `types.ts`, `limits.ts` | Endpoint admin (page size 25/100, không tenant header, worker action có reason + If-Match + Idempotency-Key, membership có ETag MembershipSet) |
| `web/src/api/idempotency.ts` | `outcomeUnknown`: intent được gửi lại cùng key khi lỗi mạng hoặc 5xx proxy rỗng |
| `web/src/components/ErrorPanel.tsx`, `styles/components.css`, `styles/layout.css` | Panel lỗi admin; layout 2 cột admin, bảng cuộn trong container, mobile |
| `web/src/features/admin/` (mới) | `AdminLayout`, `OverviewPage`; logic thuần có Vitest: `actions` (ma trận worker, sức chứa, transition status), `transitionPoll` (5 s ×1,5 ≤ 60 s, dừng sau 10 phút, tạm dừng khi tab ẩn, Retry-After), `intent`, `errors`, `fairness`, `forms`, `labels`, `modes`, `overview`, `policyForm`, `ranges`, `units`; component `paging`, `shared` |
| `features/admin/workers/*` | `WorkersPage`, `WorkerDetailPage` (sức chứa, allocation HELD/QUARANTINED, dialog lý do, polling chuyển trạng thái), `CapacityTable`, `badges` |
| `features/admin/jobs/*` | `AdminJobsPage` (filter tenant/state/waiting_reason/user/created_after, cursor), `AdminJobPage` (chỉ xem) |
| `features/admin/tenants/*`, `users/*` | Tenant (Thông tin, Thành viên, Chính sách), User (tạo với `PasswordFields`, bật/tắt) |
| `features/admin/policy/*` | `PolicyPage` (giới hạn toàn cục, chế độ), `TenantPolicyTab`, `ConflictCompare` (412: giá trị server cạnh giá trị đã nhập) |
| `features/admin/monitor/*` | `FairnessPage`, `RecoveryPage`, `AuditPage`, `format` |

Vitest mới: `adminEndpoints.test.ts`, `nav.test.ts`, `AppErrorBoundary.test.ts` và 12 tệp trong
`features/admin/`; sửa `client.test.ts` (R14), `idempotency.test.ts`.

### 2.3 Harness, Playwright, tài liệu

| Tệp | Thay đổi |
|---|---|
| `scripts/b17_e2e_stack.py` | User `admin2` (admin thứ hai cho 412); `--operational-mode WRITE_FROZEN`: seed thẳng policy WRITE_FROZEN vào DB test (D6, chỉ test) |
| `web/playwright.config.ts` | Project `w1-admin`, `w1-admin-mode`, `w1-admin-frozen`, `w2-admin`, `w2-admin-mobile` (retries 0, workers 1) |
| `web/tests/e2e/admin-support.ts` (mới), `fixture.ts`, `global-setup.ts` | Guard: fail test nếu UI gửi tenant header tới `/v1/admin` hoặc dùng storage (`expectNoBrowserStorage`: local/session/IndexedDB/`document.cookie` rỗng); guard CSP/page error/page_size của B17 giữ nguyên |
| `web/tests/e2e/admin/*`, `admin-mode/*`, `admin-frozen/*`, `admin-mobile/*`, `w2-admin/*` (mới) | Kịch bản 1–19 (§6) |
| `docs/web-ui.md` | Phần "Khu quản trị (B18)" A1–A9, ánh xạ lỗi admin, polling chuyển trạng thái, cây thư mục, cách chạy Playwright admin, cập nhật các câu "B18 chưa có" |
| `docs/cli.md`, `authentication.md`, `coordinator.md`, `database.md`, `project-structure.md`, `worker-agent.md` | Lệnh CLI mới; operation đã wire; ngữ nghĩa fairness; migration 0023; dòng B18; ETag worker (R22b) |

Không sửa: PLAN, `docs/contracts.md`, `docs/contracts/**`, `AGENTS.md`, `docs/acceptance.md`,
README, ROADMAP, `compose.yaml`, `deploy/b10/**`, `deploy/web/Caddyfile`, `ci.yml`, lockfile,
migration đã phát hành. Không thêm dependency.

## 3. Verification

### 3.1 Môi trường và phiên bản

| Mục | Giá trị |
|---|---|
| Máy | Mac (P), Docker Desktop engine 29.8.1, VM `aarch64`, 8 CPU, `MemTotal` 4106604544 B (~3,82 GiB) |
| Python | `uv` 0.9.27 tại `/tmp/nexa-b12-uv-bootstrap/bin/uv` (`$U`), `PYTHONPATH=src:.` |
| Node / pnpm | v24.21.0 / 11.9.0 (trong `/tmp`, chỉ PATH của phiên) |
| Playwright / Chromium | 1.63.0 / 153.0.8010.12 (`chromium-1243`), `PLAYWRIGHT_BROWSERS_PATH=/tmp/nexa-b17-pw`, không `install-deps` |
| Caddy | `caddy:2.10.2-alpine` `sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d` |
| W2 CPU image (arm64, local, không push) | `nexa/cpu-iterative@sha256:ec419c61c084a33ca957fd7b387a13a39f875b239c52df99a0859b09b2fc5cee` (tag `nexa/cpu-iterative:b18`, build 11:43:22Z, `raw/B18-build-cpu.out`). Build lại vì Dockerfile `COPY src/nexa` và B18 đổi `src/nexa`; image B17 `a14b604a…` vẫn còn |
| W2 worker image (arm64, local) | `nexa/b18-worker:local` id `sha256:a2b0aace4881c028a3593b2e0974406f14cce3f4e67584e34a847c2360e17d19` (build 11:26:36Z sau sửa R22b, `raw/B18-build-worker.out`) |
| Base image | `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9` |
| PostgreSQL | `nexa_b13_pg` (17.11, 127.0.0.1:15439); DB `nexa_b05_test_b18` (pytest), `nexa_b05_test_b18e2e` (Playwright), `nexa_b05_test_b18perf` (EXPLAIN) |
| Dist cuối | `index.html` sha256 `9c9397bd5322…`; `index-CYtK5Fb9.js` (463,12 kB), `index-B-fVmwfX.css`, 172 module |

Image khớp source cuối: `find src deploy/b10 deploy/cpu-iterative pyproject.toml uv.lock -newer
docs/evidence/raw/B18-build-worker.out` trả rỗng (không tệp nào đổi sau lần build worker; CPU
build sau đó). Mật khẩu PostgreSQL chỉ đọc lúc chạy bằng
`docker exec nexa_b13_pg printenv POSTGRES_PASSWORD` vào biến của subshell, không in; output che
bằng `sed -E 's#postgresql\+psycopg://[^@]*@#***@#g'`. `<URL>` dưới đây là
`postgresql+psycopg://postgres:***@127.0.0.1:15439/<db>`. Không đặt `NEXA_DATABASE_URL` trong shell.

### 3.2 Lệnh và kết quả

| Lớp | Lệnh | Kết quả | Output |
|---|---|---|---|
| Ruff | `$U run --no-sync ruff check .`; `$U run --no-sync ruff format --check .` | All checks passed; 519 files already formatted | `raw/B18-final-gates.out` |
| pytest mặc định | `env -u NEXA_TEST_DATABASE_URL PYTHONPATH=src:. $U run --no-sync pytest -q -p no:cacheprovider` | **1795 passed, 620 skipped**, 0 failed, 2 warning (Starlette/anyio deprecation, như baseline), 54,67 s. Baseline B17: 1775/603 → +20 passed (13 tệp mới + 7 ở tệp sửa), +17 skipped (16 test PG mới + 1 test R22b) | `raw/B18-pytest.out` |
| pytest PG | `NEXA_TEST_DATABASE_URL=<URL>/nexa_b05_test_b18 PYTHONPATH=src:. $U run --no-sync pytest --run-postgres -q -p no:cacheprovider` | **2387 passed, 28 skipped**, 0 failed, 3 warning, 747,78 s, EXIT=0. B17: 2350/28 → **+37 passed** = 20 test không cần DB + 17 test PG mới (16 + R22b). Warning thứ ba: B18-R25 (không do B18) | `raw/B18-pytest-pg.out` |
| Skip của PG | cùng URL, `-rs tests/integration/test_rem_b13_r12_search_path.py`, không và có `NEXA_TEST_PG_CLIENT_PREFIX="docker exec -i nexa_b13_pg"` | không prefix: 6 passed, 3 skipped ("pg_dump/pg_restore unavailable"); có prefix: **9 passed** (như B17). Tương đương toàn bộ có prefix: 2390 passed, 25 skipped | `raw/B18-pytest-pg-prefix.out` |
| Web gate | `pnpm --dir web install --frozen-lockfile`; `run typecheck`; `run build`; `run test` | install OK; typecheck (gồm `check:api`) sạch; build 172 module, dist `9c9397bd5322`; **vitest 28 files / 197 tests passed** (B17: 13/105) | `raw/B18-final-gates.out` |
| Web CI replay | bản sao sạch `/tmp/b18-ci-web` (164 tệp: `web/` tracked + untracked không ignore, `docs/contracts/`; không `node_modules`/`dist`), Node 24.21.0, pnpm 11.9.0, đúng các bước `ci.yml` | 85 package từ lockfile; typecheck sạch; build ra cùng `index-CYtK5Fb9.js`/`index-B-fVmwfX.css`; **vitest 28/197 passed**; EXIT=0 | `raw/B18-ci-web.out` |
| Playwright (chain3, dist `9c9397bd5322`) | §3.3 | `w1-admin` **16 passed**; `w1-admin-mode` **1 passed**; `w1-admin-frozen` **1 passed**; `w2-admin` + `w2-admin-mobile` **5 passed** (4 + 1); B17 `w1-desktop`+`w1-mobile` **23 passed**, `w1-admission` **1 passed**, `w2` **4 passed**. 0 failed, 0 flaky, retries 0 | `raw/B18-{w1-admin,w1-admin-mode,w1-admin-frozen,w2-admin,b17-w1,b17-admission,b17-w2}.out`, `raw/B18-chain3-driver.out` |

### 3.3 Lệnh Playwright (password không in, URL đã che)

```sh
source /tmp/nexa-b17-env.sh            # PATH Node/pnpm, PLAYWRIGHT_BROWSERS_PATH
pnpm --dir web run build
# NEXA_TEST_DATABASE_URL=<URL>/nexa_b05_test_b18e2e (đặt trong subshell, password đọc từ container)
S="env PYTHONPATH=src:. $U run --no-sync python scripts/b17_e2e_stack.py"
P="pnpm --dir web exec playwright test"
$S run --tier w1 -- $P --project=w1-desktop --project=w1-mobile          # hồi quy B17
$S run --tier w1 -- $P --project=w1-admission                            # hồi quy B17, stack riêng
$S run --tier w1 -- $P --project=w1-admin
$S run --tier w1 -- $P --project=w1-admin-mode                           # stack riêng
$S run --tier w1 --operational-mode WRITE_FROZEN -- $P --project=w1-admin-frozen
W2="NEXA_B17_CPU_IMAGE_REF=nexa/cpu-iterative@sha256:ec419c61c084a33ca957fd7b387a13a39f875b239c52df99a0859b09b2fc5cee NEXA_B17_WORKER_IMAGE=nexa/b18-worker:local"
env $W2 $S run --tier w2 -- $P --project=w2                              # hồi quy B17
env $W2 $S run --tier w2 -- $P --project=w2-admin --project=w2-admin-mobile
$S cleanup
```

Chain3 (12:13:34Z → 12:22:35Z) chạy tuần tự 8 lượt trên (thêm một lượt chụp ảnh/đo audit ngoài
repo); trước và sau mỗi lượt kiểm process/session DB/container còn sót: **0/0/0** mọi lần
(`raw/B18-chain3-driver.out`). PID của `nexa_b10_smoke3-caddy-1` (`docker stats --no-stream`):
9912 đầu phiên → 10875 lúc bắt đầu chain3 → 10979 lúc kết thúc → 11081 trước lượt pytest PG; tăng
chậm, không chặn lượt nào; không dừng/sửa container đó.

### 3.4 Lịch sử lượt chạy Playwright (không retry, không sleep/`waitForTimeout`)

| Lượt (run id, dist) | Kết quả | Nguyên nhân gốc và sửa |
|---|---|---|
| w1-admin red1 (`1dbf2c7e`, `4f9036eb5739`) | 14 passed, 2 failed | (7) spec chọn option tenant trước khi danh sách tenant tải xong → sửa test chờ option. (8) "Tenant created" không có trong Audit: điểm cuối khoảng mặc định làm tròn **xuống** phút hiện tại nên bỏ mất bản ghi mới nhất → Vitest đỏ trước (`ranges.test.ts` "ending at the next whole minute…"), sửa `ranges.ts` (UX-A19) |
| w1-admin red2 (`7c5d51b1`, cùng dist) | 15 passed, 1 failed | Cùng dist nên chỉ test đổi: (7) xanh, (8) vẫn đỏ → xác nhận nguyên nhân ở UI; dist kế tiếp có sửa |
| w1-admin early (`6bdeb410`, `26f823701cc6`) | 16 passed | – |
| w1-admin-mode early (`dcbdb277`) | 1 passed | Spec xanh ngay lần đầu; **không có lượt đỏ** cho spec này |
| w1-admin-frozen red1 (`9ab3c8ce`) | 1 failed | Spec tìm "Khóa ghi" nhưng nhãn mode của UI khác → sửa test theo nhãn thật |
| w1-admin-frozen red2 (`303d872c`) | 1 failed | Strict mode: `getByRole('cell', { name: 'b17-tenant-a' })` khớp 2 ô (slug và tên) → sửa selector (test) |
| w1-admin-frozen red3 (`98f4a512`) | 1 failed | Spec kỳ vọng thông báo frozen của B17 khi member đăng nhập; server trả 401 trước → **B18-R21**; spec giờ khẳng định 401 |
| w1-admin-frozen early (`e1616139`) | 1 passed | – |
| w2-admin red1 (`6a43d1e6`, CPU `6382e10a`, worker `f446acea`) | 1 passed, 4 failed (10,7 phút) | 14–15: badge "Đang khởi động" thay "Sẵn sàng"; 16: enable 409 thay 200 (→ **B18-R22a**); 17: hết 10 phút chờ nút; 18: tenant-b undefined (dây chuyền) |
| w2-admin red2 (`dd1c341f`) | 1 passed, 4 failed | 14–15: "Đang giữ" hiện 0 core (đọc trước khi allocation HELD); 16: chi tiết 412 thiếu; 17: STARTING thay READY sau 120 s → ETag churn (**B18-R22b**): test PG đỏ (`raw/B18-R22-red.out`), sửa `worker_service.py`, build lại worker (`a2b0aace`) |
| w2-admin red3 (`29631b0a`) | ngắt tay giữa chừng | Thời kỳ process mồ côi/đổi image; không dùng làm kết quả |
| w2-admin run4 / run5 (`5974e6b5` / `b3d0dae7`) | 5 passed / 5 passed | Còn CPU image cũ `6382e10a` (trước khi build lại) → không dùng làm kết quả cuối |
| b17-w2 invalid (`cf20631c`) | ngắt (test 17 hết giờ) | CPU image bị retag giữa lượt; `pkill` để lại API/coordinator mồ côi từ 11:42Z → chạy lại toàn bộ (chain2) có kiểm sót |
| B18-R23 | Vitest đỏ `raw/B18-R23-red.out` | Câu hoàn tất có thể hiện từ danh sách đọc trước thao tác → sửa `actions.ts`/`WorkerDetailPage.tsx`; dist mới `9c9397bd5322` |
| chain2 (dist `26f823701cc6`, CPU `ec419c61`) | b17 23 + 1 + 4, admin 16 + 1 + 1 + 5: tất cả passed | Bị thay bằng chain3 vì R23 đổi dist; giữ `raw/B18-*-chain2.out` |
| **chain3** (dist `9c9397bd5322`) | b17 **23 + 1 + 4**, admin **16 + 1 + 1 + 5**, tất cả passed | Kết quả cuối |

Một số lượt trung gian không có raw (ví dụ b17-w2 `c87fd44f`, 4 passed trên dist `26f823701cc6`)
vì đã bị chain2/chain3 thay thế.

## 4. EXPLAIN và migration 0023

Seed trong `nexa_b05_test_b18perf` (`scripts/b18_explain.py`): **100 200 job / 20 tenant**;
**202 010 segment ledger**, 40 segment mở, trải 61 ngày, segment mở cũ nhất 43 ngày. PostgreSQL
17.11 aarch64. Đo 15:37–15:47 (+07). Output: `raw/B18-explain-{jobs,fairness}-{before,after}.out`,
`raw/B18-explain-jobs-join-only.out`, `raw/B18-write-cost.out`.

`adminListJobs` (page 51 dòng), execution time trước 0023 → sau:

| Truy vấn | Trước | Sau |
|---|---|---|
| không filter | 307,270 ms | 0,234 ms |
| tenant | 20,2 ms | 0,861 ms |
| state | 20,5 ms | 5,5 ms |
| waiting_reason | 16,8 ms | 5,3 ms |
| user | 16,5 ms | 1,09 ms |
| created_after | 22,8 ms | 0,139 ms |
| trang 2 | 110,7 ms | 0,180 ms |
| trang 2 + tenant | 19,1 ms | 0,234 ms |

Biến thể chỉ join (không index mới, `raw/B18-explain-jobs-join-only.out`): 128,9 / 110,96 ms →
cần index keyset toàn cục `ix_jobs_created_keyset`.

`adminQueryFairness`, trước → sau. **Đây là SQL µs của vòng 1, đã được thay**; số đo của SQL ms đang ship (B18-R26) nằm ở bảng ngay dưới, vòng 4:

| Khoảng | Mọi tenant | Một tenant |
|---|---|---|
| 1 h | 15,1 → 4,8 ms | 1,1 → 0,64 ms |
| 24 h | 85,4 → 35,5 ms | 2,7 → 2,25 ms |
| 31 ngày | 906 → 672 ms | 49,8 → 35,4 ms |
| 31 ngày kết thúc 29 ngày trước | 960 → 660 ms | 57 → 30 ms |

Vòng 1, SQL µs: 31 ngày mọi tenant với `enable_seqscan=off` là 683 ms.

**`adminQueryFairness` với SQL ms đang ship (vòng 4, `raw/B18-r4-explain-fairness.out`).**

- Seed mới vào DB `nexa_b05_test_b18r4perf`, bằng cùng script (`raw/B18-r4-explain-seed.out`):
  - PostgreSQL 17.11 aarch64; 100 200 job / 20 tenant;
  - **202 010 segment**, 40 segment mở, trải 61 ngày, segment mở cũ nhất 43 ngày.
- Đo lúc 2026-10-03T15:01Z, sau migration 0023.
- Source (ngoài `docs/`) có fingerprint `be03d8db4c150aa7` trên HEAD `3d00c23`, không đổi kể từ
  lượt full PG vòng 2.

| Khoảng | Mọi tenant | Một tenant |
|---|---|---|
| 1 h (bucket 300 s) | 7,0 ms, GiST `ix_allocation_ledger_segments_period` | 0,63 ms, BitmapAnd GiST + `ix_allocation_ledger_segments_tenant_time` |
| 24 h (bucket 3600 s) | 44,1 ms, GiST | 2,7 ms, BitmapAnd |
| 31 ngày (bucket 86 400 s) | 773,1 ms, Seq Scan: giữ 101 465 / loại 100 545 dòng | 31,1 ms, `tenant_time` |
| 31 ngày kết thúc 29 ngày trước | 736,0 ms, Seq Scan: giữ 103 837 / loại 98 173 | 32,9 ms, `tenant_time` |

Các số trên là `Execution Time` của EXPLAIN (ANALYZE, BUFFERS).

- **Wall time** do script đo, gồm cả fetch kết quả: 31 ngày mọi tenant 931,5 ms (lượt đầu, cache
  lạnh), 31 ngày kết thúc 29 ngày trước 685,5 ms. Cả hai dưới mục tiêu 1 s.
- **31 ngày mọi tenant với `enable_seqscan=off`:** 788,6 ms. Planner chuyển sang `Index Scan
  Backward` trên PK, không phải GiST, và không nhanh hơn.
- **Vì sao 31 ngày mọi tenant dùng Seq Scan:** khoảng này khớp khoảng 50 % bảng (seed trải
  61 ngày). Đọc tuần tự rẻ hơn đọc theo index, nên Seq Scan là lựa chọn đúng theo selectivity,
  giống vòng 1.
- **Kết luận:**
  - Khoảng ngắn (1 h, 24 h) dùng GiST, nên chỉ đọc segment giao với khoảng. Truy vấn một tenant
    dùng index, nên chỉ đọc segment của tenant đó (khoảng 9 530 dòng); mốc thời gian lọc sau.
  - Segment mở cũ vẫn được tính: GiST đánh index `tstzrange(started_at, ended_at, '[)')`, và
    `ended_at` NULL là cận trên mở. `tenant_time` có `Index Cond` chỉ trên `tenant_id`.
  - Trường hợp xấu nhất (31 ngày × 20 tenant trên seed lớn) khoảng 0,7–0,8 s execution, dưới
    1 s. Đây là trang đọc theo yêu cầu; giới hạn đã ghi.

Chi phí ghi (median 5 lần, trước → sau): insert 5000 job 1001,4 → 960,3 ms; 200 tick charge
952,1 → 945,1 ms; 200 rebuild biên (close + insert segment) 2469,1 → 2781,6 ms (+12,7 %, trong
nhiễu giữa các lần: 2065–2989 ms sau, 2085–2646 ms trước). `test_migration_b18.py` kiểm upgrade/
downgrade, SQL offline chỉ `CREATE/DROP INDEX`, metadata parity.

## 5. Đo audit (lượt chain3 `3aa2a217`, `raw/B18-audit-measure.out`)

Spec đo một lần ngoài repo đếm request `/v1/admin` của UI và số dòng `audit_records` tăng thêm.

| Trang | Request `/v1/admin` | Dòng audit |
|---|---|---|
| Tổng quan | 5 (policy, workers, allocations HELD, allocations QUARANTINED, recovery-events) | 5 |
| Worker | 1 | 1 |
| Chi tiết worker | 3 (worker, allocations HELD, QUARANTINED) | 3 |
| Hàng chờ | 2 (tenants, jobs) | 2 |
| Chi tiết job | 1 | 1 |
| Tenant | 1 | 1 |
| Tenant – Thông tin / Thành viên / Chính sách | 1 / 2 / 2 | 1 / 2 / 2 |
| User / Chi tiết user | 1 / 1 | 1 / 1 |
| Chính sách hệ thống | 1 | 1 |
| Fairness | 2 (tenants, fairness) | 2 |
| Khôi phục / Audit | 1 / 1 | 1 / 1 |
| Chuyển trạng thái "Ngừng nhận job" | 4 (POST drain + một nhịp poll: worker, allocations HELD, QUARANTINED) | 4 |
| Chuyển trạng thái "Bật lại" | 1 (POST enable; worker đã READY nên không poll) | 1 |

Request = dòng audit ở mọi trang, khớp thiết kế docs/web-ui.md A3. Một chuyển trạng thái chờ lâu
hơn tốn 3 dòng mỗi nhịp poll (5 s ×1,5 … ≤ 60 s, dừng sau 10 phút ⇒ tối đa ~16 nhịp ≈ 48 dòng).
Toàn bộ lượt W2-admin chain3 (`raw/B18-w2-admin-timeline.out`): allocation.list 38, worker.list 31,
worker.get 18, membership.upsert 9, user.create 9, policy.tenant.get 4, …; 409/412 bị từ chối của
worker action không ghi audit (`test 16` kiểm "enable too early" không có dòng).

## 6. Kịch bản Playwright → yêu cầu → kết quả

PLAN:197 (chức năng admin), PLAN:199 (cookie/CSRF/credential), PLAN:203 (drain/disable, audit,
Playwright admin flows); "Gate B18" = PLAN:324 (quyền admin, version conflict, drain/disable,
aggregate có giới hạn). Mọi test admin chạy dưới guard chung: CSP, page error, `page_size ≤ 100`,
không tenant header tới `/v1/admin`, storage rỗng (kịch bản 12).

| # | Test (lượt chain3) | Yêu cầu | ACC | Kết quả |
|---|---|---|---|---|
| 1 | `admin/01-access` ×3: member_a, admin_a (TENANT_ADMIN) không link, trang guard không có request `/v1/admin`, API 403; sysadmin không membership vào được | PLAN:197, Gate B18 quyền | ACC-02, ACC-03 | pass |
| 2a | `02-tenants` double click → 1 tenant | PLAN:197 | ACC-07 | pass |
| 2b | 502 rỗng sau commit → gửi lại cùng key → 1 tenant | PLAN:197 | ACC-07 | pass |
| 2c | sửa tên với ETag; tắt/bật qua dialog | PLAN:197 | ACC-27 | pass |
| 3 | `03-users` tạo user + mật khẩu (không ở URL/storage/console/body, ô bị xóa); đăng nhập context mới; tắt user → phiên mở bị đưa về login; bật lại không hồi sinh phiên cũ (401), đăng nhập mới được | PLAN:197, PLAN:199 | ACC-24 | pass |
| 4 | membership MEMBER → TENANT_ADMIN → gỡ; admin thứ hai ETag cũ → 412, không đổi trùng | Gate B18 version conflict | ACC-05, ACC-07 | pass |
| 5a | `04-policy` sửa weight + resource_limit; 412 giữa hai admin, giá trị server cạnh nhau | Gate B18 version conflict | ACC-05 | pass |
| 5b | giảm outstanding_limit dưới QUEUED đã commit → 409 lý do server; policy không đổi | PLAN:197 quota | ACC-05 | pass |
| 6 | global outstanding limit; 412 giữa hai admin | Gate B18 | ACC-05 | pass |
| 7 | `05-queue` 2 tenant; filter tenant/state/waiting_reason; trang sau/trước/đầu; cursor hỏng → trang đầu + thông báo; `page_size ≤ 100`; chi tiết không control | PLAN:197 queue, Gate B18 aggregate | ACC-27, ACC-03 | pass |
| 8 | `06-monitor` audit lọc action/khoảng/trang; thấy thao tác 2–6 và lượt đọc admin | PLAN:203 audit | ACC-26 | pass |
| 9 | fairness/recovery empty; > 31 ngày chặn ở form; tham số sai → 400 | Gate B18 aggregate có giới hạn | ACC-27 | pass |
| 10 | clock 120 s ở Tổng quan, Worker, Hàng chờ → 0 request `/v1/admin` | Gate B18 không polling dày | ACC-27 | pass |
| 11 | POST `/v1/admin/tenants` thiếu `X-CSRF-Token` → 403 `invalid_csrf` | PLAN:199 | ACC-24 | pass |
| 12 | guard storage + CSP trong mọi test admin | PLAN:199 | ACC-24 | Vòng 1: **không chứng minh được** (RV02: spec đóng context trước teardown, `pages()` rỗng; chỉ `03-users` kiểm thật). Vòng 2: kiểm khi đóng từng context (`guardStorage`), fixture đòi số lần kiểm > 0, `01-access` 12 là negative control. Vòng 2 chạy: mọi test admin in `[storage-guard] … pages≥1 contexts≥2` (W1 admin 20/20, mode, frozen, W2 admin + mobile 5/5), negative control 12 pass (§15.3) → pass |
| 13 | `admin-mode/mode`: NORMAL → ADMISSION_OFF, member thấy thông điệp tạm ngừng của B17; ADMISSION_OFF → WRITE_FROZEN và → NORMAL bị 409, UI hiện lý do server (B18-R18, B18-R05) | PLAN:197 | ACC-21 | pass (chuyển mode bị server từ chối: finding) |
| 13f | `admin-frozen/frozen` (stack seed WRITE_FROZEN, D6): đọc admin chạy; tạo tenant 409 hiện; rời WRITE_FROZEN 409; non-admin login 401 (B18-R21) | PLAN:197 | ACC-21 | pass |
| 14–15 | `w2-admin/01-workers`: inventory/capacity, job RUNNING → HELD, tổng đang giữ; drain có lý do → DRAINING; job B QUEUED `waiting_for_worker`; câu hoàn tất chỉ sau khi hết HELD; enable → B dispatch → SUCCEEDED | PLAN:203, Gate B18 drain | ACC-16, ACC-27 | pass |
| 16 | disable → DISABLED, QUARANTINED, attempt bị fence; không câu "đã dừng/đã dọn"; enable khi QUARANTINED và worker đang pause → 409 `state_conflict` hiển thị (lý do là **check đầu tiên không đạt** theo thứ tự guard — RV07; vòng 1 thường là heartbeat, vòng 2 run3 là câu QUARANTINED, `raw/B18-r2-w2-admin-annotations.out`); cleanup → RELEASED → enable 200 → READY; attempt mới → SUCCEEDED; Khôi phục có ATTEMPT_FENCED WORKER_DISABLED; Audit có drain/disable/enable kèm lý do | PLAN:203, Gate B18 disable | ACC-16, ACC-26 | pass |
| 17 | 412 worker action: admin thứ hai trang cũ → 412 → đọc lại; thao tác còn áp dụng giữ dialog và lý do, không còn áp dụng → đóng, câu conflict trên trang (RV05). Vòng 2: điều kiện đầu là worker không còn allocation HELD/QUARANTINED (§15.3) | Gate B18 version conflict | ACC-05, ACC-07 | pass (vòng 2: run1 failed vì thiếu điều kiện đầu, run2/run3 pass) |
| 18 | `02-fairness`: tenant đã chạy có service > 0; filter tenant | PLAN:197 fairness | ACC-27 | pass |
| 19 | `admin-mobile/mobile` 390×844: nav, Tổng quan, chi tiết worker, form tenant policy, không cuộn ngang toàn trang (chạy trên stack W2) | PLAN:197 | ACC-27 | pass |
| 20 | Hồi quy B17 trên dist cuối: w1-desktop + w1-mobile 23, w1-admission 1, w2 4 | PLAN:203 | ACC-27, ACC-39 | pass |

## 7. Timeline W2 drain/disable/enable (chain3 `becccfec`)

API: `raw/B18-w2-admin-timeline.out` (job event, allocation, worker version/admin_state từ DB,
UTC). UI: assertion trong spec (nhãn badge/transition) và annotation `raw/B18-w2-admin-annotations.out`.

| Thời điểm (UTC) | API | UI quan sát |
|---|---|---|
| 12:18:41.025 | job A accepted | – |
| 12:18:41.224 | A allocation HELD | chi tiết worker: hàng A "Đang giữ", capacity "Đang giữ" 1 core |
| 12:18:43.291 | A ATTEMPT_STARTED | health "Sẵn sàng" (API READY) |
| 12:18:45.119 | worker v4→v5 DRAINING "b18 e2e drain" (không bị từ chối) | badge "Ngừng nhận job", "Đang chờ 1 job đang chạy kết thúc" |
| 12:18:45.200 | job B accepted, QUEUED `waiting_for_worker` | – |
| 12:19:14.864 | A RESULT_RECOGNIZED | – |
| 12:19:20.936 | A allocation RELEASED (VERIFIED_CLEANUP) | "Đã ngừng nhận job; không còn job đang chạy" (chỉ sau khi hết HELD) |
| (trước 12:19:37) | enable bị từ chối: 409 "The worker is not READY", rồi 412 (B18-R22a) | dialog hiện lý do server, trang đọc lại |
| 12:19:37.217 | enable v11→v12 | "Đang bật" |
| 12:19:37.355 → 12:19:42.936 | B dispatch/HELD → RESULT_RECOGNIZED | – |
| 12:19:44.322 → 12:19:51.420 | job C accepted → HELD → started | hàng C "Đang giữ" |
| 12:19:53.871 | disable v15→v16 (worker container đang `docker pause`) | badge "Đã tắt" |
| 12:19:53.876 | C ATTEMPT_FENCED WORKER_DISABLED, allocation QUARANTINED | "Chờ xác nhận dọn dẹp (QUARANTINED)", "Đang chờ worker xác nhận dọn dẹp (còn 1 phân bổ QUARANTINED)"; không câu "đã dừng/đã dọn" |
| (trong lúc pause) | enable → 409 "The latest worker heartbeat did not pass the READY checks" | dialog "Chi tiết từ máy chủ: …"; vẫn "Đã tắt" |
| 12:20:06.577 | C allocation RELEASED (VERIFIED_CLEANUP) sau unpause | "Worker đã xác nhận dọn dẹp xong" |
| 12:20:08.623 | C RETRY_READY BACKOFF_ELAPSED | – |
| 12:20:17.562 | enable v16→v17 (sau 2 lần 409 cùng lý do heartbeat) | "Đang bật" → "Worker sẵn sàng nhận job" |
| 12:20:20.340 → 12:20:21.663 | C HELD, CHECKPOINT_FALLBACK_TO_INPUT, attempt 2 started | – |
| 12:20:56.762 → 12:20:58.983 | CHECKPOINT_COMMITTED, RESULT_RECOGNIZED | attempt 1 `failure_class` INFRASTRUCTURE |
| 12:21:03.588 / 12:21:03.882 | kịch bản 17: drain bởi admin một v18→v19; admin hai thao tác trên trang cũ → 412; sau khi đọc lại, enable bởi admin hai v19→v20 | 412 hiện, trang đọc lại |

Cuối: 3 job SUCCEEDED; worker ENABLED/READY v20. Giới hạn: 409 khi QUARANTINED hiện lý do của
**kiểm tra đầu tiên thất bại** trong `_require_enable_ready` (heartbeat mới, READY, `ready_checked_at`,
incarnation reconciled, rồi mới QUARANTINED). Vì container worker bị pause, kiểm heartbeat thất bại
trước; câu "Quarantined allocations still await verified cleanup" không xuất hiện trong lượt nào
(chain2 là "current worker incarnation is not reconciled"). UI hiện đúng câu server trả; trạng thái
QUARANTINED tại thời điểm 409 được kiểm bằng API trong spec.

## 8. Ảnh chụp

`docs/evidence/raw/B18/` (chụp trên stack chain3 `3aa2a217`, dist `9c9397bd5322`):
`01-desktop-overview` (Tổng quan, worker "Đang khởi động" khi có 1 HELD — R22a),
`02-desktop-worker-held`, `03-desktop-worker-drain-dialog` (lý do minh họa, không gửi),
`04-desktop-queue`, `05-desktop-tenant-members` (ID, không tên), `06-desktop-policy`,
`07-desktop-fairness`, `08-desktop-audit` (có "Tải dữ liệu lên" bởi "User …" — R24),
`09-desktop-recovery` (empty), `10-mobile-worker-detail` (390 px, bảng cuộn trong container).
Đã xem từng ảnh: không password, cookie, CSRF hay token. **Ảnh chỉ minh họa bố cục, không chứng
minh authorization hay hành vi.**

## 9. Output thô

`docs/evidence/raw/B18-*.out`: Playwright cuối (§3.2), `*-chain2`, các lượt đỏ/sớm (§3.4),
`w2-admin-{annotations,timeline}` (+ `-chain2`, `run5-*`), `audit-measure`, `chain3-driver`,
`explain-*`, `write-cost`, `R22-red`, `R23-red`, `w1-admin-frozen-red2`, `build-{cpu,cpu-1,worker}`, `pytest`, `pytest-pg`,
`pytest-pg-prefix`, `ci-web`, `final-gates`. Đã bỏ ANSI và che DB URL;
`/usr/bin/grep -iE "(password=|passwd|set-cookie|x-csrf|bearer |postgresql(\+psycopg)?://[^*])"`
chỉ khớp tiêu đề test nhắc "X-CSRF-Token". Trace/HAR/video/storageState không nằm trong repo
(thư mục tạm của harness, bị `cleanup` xóa; `web/test-results` ignore và đã xóa).

## 10. Quan sát và sai lệch so với prompt

- **Kịch bản 13**: ADMISSION_OFF → WRITE_FROZEN không đạt được qua API (B18-R18); WRITE_FROZEN chỉ
  kiểm trên stack seed DB (D6). Hai chiều mở lại bị 409 (B18-R05).
- **Kịch bản 16** dùng `docker pause` trên container worker của chính harness (`nexa_b17_worker_*`)
  để giữ cửa sổ QUARANTINED đủ lâu; worker dọn trong ~1 vòng renew nếu không pause.
- **B18-R22a**: enable/drain ngay sau khi allocation đổi có thể 409/412; spec ghi lại mọi lần từ chối
  và bấm lại như admin thật (`actUntilAccepted`), không retry Playwright.
- **API bind**: harness B17 bind API 127.0.0.1 cả trên Docker Desktop (như B17 §8), chặt hơn prompt.
- **Trang Audit** trên desktop: bảng rộng cuộn ngang trong container (không cuộn toàn trang).
- **pytest PG có 3 warning** (B17: 2): warning thứ ba không ổn định, sinh từ FastAPI/pydantic khi
  request đầu tiên tới route B08 chạy đồng thời (B18-R25); không test nào fail.
- **VPS1 (L)** không làm; ACC-16 L = not-run.

## 11. Tự review

- Đọc lại `git diff` và từng tệp untracked. Không `dangerouslySetInnerHTML`; không ghi
  local/session storage/IndexedDB/cookie từ JS (guard trong mọi test admin); mật khẩu chỉ trong state
  form; client không log request/response.
- Authorization chỉ ở backend: guard UI chỉ để không gửi request vô ích; W1 1 gọi thẳng API → 403;
  route mới scope exact `admin:read` (operation matrix).
- Đối chiếu docs/web-ui.md A1–A9 với UI cuối: sitemap/nav/trang/ma trận worker và mode/polling
  khớp code; sửa R23 được ghi vào A6; R24 vào giới hạn.
- Không sửa PLAN, contracts, AGENTS, acceptance, README, ROADMAP, compose, `deploy/**`, CI, lockfile;
  một migration chỉ index. Không commit/push/branch/worktree, không push image, không dùng
  Superpowers, không AWS, không alias admin VPS.

## 12. Acceptance

### 12.1 AC của B18

| AC | Status | Evidence |
|---|---|---|
| AC-01 IA trước code, khớp UI | pass | IA ghi 2026-10-01T08:03:58Z (đầu tệp), trước mọi code UI admin; đối chiếu §11 |
| AC-02 quyền | pass | W1 1 (×3), W1 7 (chi tiết không control), `test_admin_jobs_b18.py` |
| AC-03 worker | pass (P) | W2 14–17, annotations/timeline §7; `actions.test.ts`, `adminEndpoints.test.ts`; R22a/R22b/R23 |
| AC-04 hàng chờ | pass | W1 7, `test_admin_jobs_b18.py`, EXPLAIN §4 |
| AC-05 tenant/user/membership | pass | W1 2a–2c, 3, 4 |
| AC-06 quota/weight | pass | W1 5a, 5b; `policyForm.test.ts` (cảnh báo resource_limit bằng 0) |
| AC-07 global policy và mode | pass (phần UI); chuyển mode mở lại bị backend chặn | W1 6, mode 13, frozen 13; `modes.test.ts`; B18-R05/R18 OPEN |
| AC-08 fairness | pass (vòng 4). Vòng 1 "khớp ledger" **không chính xác** (RV01); vòng 2 theo B18-R26: unit + Hypothesis (µs), PG 19 passed × 3 trên segment thật, W2 18. EXPLAIN của SQL ms đang ship đo lại ở vòng 4: 202 010 segment, 1 h/24 h/31 ngày × mọi/một tenant, 31 ngày ≤ 0,8 s execution (RV11) | B18-R26, §4 (bảng vòng 4), §15.3, §16; `raw/B18-r4-explain-fairness.out` |
| AC-09 recovery/audit | pass | W1 8, 9; W2 16 |
| AC-10 replay | pass | W1 2a, 2b, 4, 17; `intent.test.ts`, `idempotency.test.ts` |
| AC-11 polling/audit | pass | W1 10; `transitionPoll.test.ts`; §5 |
| AC-12 CLI | pass | `test_admin_commands.py`, `test_cli_admin_b18.py`, route contract, operation matrix, docs/cli.md |
| AC-13 mobile/CSP/storage/CSRF | pass (vòng 2). Vòng 1 phần storage không chứng minh được (RV02); vòng 2 guard kiểm khi đóng context, log từng test, negative control 12 | W2 19, W1 11, W1 12, §15.3 |
| AC-14 migration | pass | `test_migration_b18.py`, §4 |
| AC-15 hồi quy B17 | pass | Vòng 2 (§15.3): 23 + 1 + 4 trên dist `de4c3dbb3979`; vòng 1: chain3 trên `9c9397bd5322` |
| AC-16 mọi gate | pass | Vòng 2: §15.2, §15.3; vòng 1: §3.2 |

### 12.2 Gate (docs/acceptance.md giữ nguyên `specified`)

| Gate | Tiêu chí thuộc B18 | Test | Evidence | Môi trường | Applicability | Status (phần B18) | Giới hạn / task khác |
|---|---|---|---|---|---|---|---|
| ACC-27 | admin flow, drain/disable/quota/quyền qua REST chung, aggregate có giới hạn, không polling dày | W1-admin 1–12, mode/frozen 13, W2 14–19, Vitest | §3, §5, §6 | P + W | phần admin của gate | pass (phần admin); **gate toàn phần: specified** | log B17-R01; L not-run |
| ACC-02 | principal thiếu quyền admin bị chặn; scope exact route mới | W1 1; `test_admin_jobs_b18`, `test_admin_fairness_b18` (authorization); operation matrix | §6 | P + W | áp dụng | pass (phần B18) | race/security đầy đủ B20 |
| ACC-05 | policy version conflict, giảm dưới held/counter bị từ chối, thấy qua UI | W1 4, 5a, 5b, 6; W2 17 | §6 | W | phần UI/route | pass (phần B18) | – |
| ACC-16 | drain/disable/enable trên worker Docker thật | W2 14–17 | §7 | W2 trên P | phần drain/disable | pass (P); **L: not-run** | bootstrap/adoption không thuộc B18; Linux thật cần VPS1 |
| ACC-24 | CSRF/cookie cho mutation admin, storage rỗng, tắt user thu hồi phiên, mật khẩu không lộ | W1 3, 11, 12 | §6 | W | phần B18 | pass (phần B18) | Compose/release B21/B25; header audit B20 |
| ACC-03 | user tenant không thấy dữ liệu admin/cross-tenant qua UI/URL/API | W1 1 (member, tenant admin), guard tenant header | §6 | W | phần UI | pass (phần UI) | race/security B20 |
| ACC-07 | replay mutation admin qua UI | W1 2a, 2b, 4, 5a, 6; W2 17; Vitest intent | §6 | W | phần B18 | pass (phần B18) | – |
| ACC-21 | UI hiển thị 503/WRITE_FROZEN, không che | mode 13, frozen 13; `errors.test.ts` | §6 | W | phần B18 | pass (phần UI); WRITE_FROZEN qua seed DB (D6) | 503 thật khi DB hỏng B20/B22; recovery proof B18-R05 |
| ACC-26 | mọi thao tác và lượt đọc admin có audit; UI không log credential | W1 8; W2 16; đo §5; review §11 | §5 | W | phần B18 | pass (phần B18) | log pipeline B17-R01/B19; actor upload worker B18-R24 |
| ACC-39 | typecheck/build/vitest/Playwright chạy thật | §3.2 | §3 | P + W | phần B18 | pass (phần B18) | Playwright chưa vào CI (B18-R11) |
| L (VPS1) | kịch bản 15–16 trên Linux | – | – | L | tùy chọn | not-run | – |

## 13. Phần thuộc task khác và giới hạn

- Recovery proof thật cho mở lại mode và WRITE_FROZEN (B18-R05/R18, từ B17-R18): B19 hoặc task
  recovery. Log job (B17-R01): task riêng/B19. Metrics/storage watermark/GC: B19. Race/security,
  header audit, actor upload worker (R24): B20. Compose/UI image/portability: B21. GPU: B23.
  Release và Playwright trong CI (B18-R11): B25/CI.
- Đề xuất contract: aggregate sức chứa (R03), đếm hàng chờ (R04), membership theo user (R10),
  slug 64 (R19), GPU tenant limit (R20), lỗi login frozen (R21).
- Finding cũ không đổi: B17-R01, B17-R05, B17-R18 (→ B18-R05), B15-R11, B15-R39, B16-R29, B11-H01,
  B15-OBS-01, OD-1..3, REM-R08, CI-R01.

## 14. Trạng thái môi trường khi bàn giao

- `scripts/b17_e2e_stack.py cleanup`: 0 container, 0 state dir; `docker ps -a` không còn
  `nexa_b17_*`/`nexa_b18_*`; không còn process uvicorn/coordinator/harness/Playwright/vite.
- `web/test-results` đã xóa; `web/dist` (dist cuối `9c9397bd5322`), `web/node_modules` bị ignore,
  giữ lại để chạy lại.
- DB `nexa_b05_test_b18perf` **đã xóa** (`dropdb`) sau khi đo EXPLAIN; còn lại có chủ đích
  `nexa_b05_test_b18` (pytest) và `nexa_b05_test_b18e2e` (Playwright) trong `nexa_b13_pg`.
- Image local (không push): `nexa/cpu-iterative:b18` `ec419c61c084`, `nexa/b18-worker:local`
  `a2b0aace4881`, `caddy:2.10.2-alpine`; image các task trước giữ nguyên.
- `/tmp`: Node/pnpm/Chromium/uv của B17/B12; `/tmp/nexa-b18-pg.sh` (helper đọc password từ container
  lúc chạy, không chứa secret); `/tmp/nexa_b18_prompt.md`. Các tệp tạm B18 khác (script chụp ảnh/đo,
  chain, CI replay copy, probe) đã xóa.
- `nexa_b10_smoke3-caddy-1` PID **11275** lúc bàn giao (tăng chậm từ 9912 đầu phiên; không dừng/sửa).
- Container `nexa_b10_*` và DB evidence của task trước không bị đụng. VPS1 không dùng; không AWS.

## 15. Vòng 2 (sau Task Review vòng 1 "Không duyệt", 2026-10-03)

Các mục §3–§14 ở trên là kết quả **vòng 1** trên dist `9c9397bd5322`. Mục này ghi lại thay đổi và
kiểm chứng của vòng 2. Một kết quả chỉ được ghi pass khi lệnh đã chạy trong vòng 2.

### 15.1 Sửa theo finding

| RV | Sửa | Test (đỏ trước khi sửa) |
|---|---|---|
| RV01 (chặn) | B18-R26 interpretation (a): sàn ms như `epoch_ms`; `fairness_report.py`, SQL `admin_queries.py` theo bigint ms; docs/coordinator.md | Unit/Hypothesis có offset µs (`test_fairness_report_b18.py`, 11). PG `test_admin_fairness_b18.py` bỏ bước cắt thời gian segment, thêm assert segment có µs, sai số 1e-9: **19 passed × 3** (§15.3) |
| RV02 (chặn) | `admin-support.ts`: `guardStorage` kiểm storage của mọi page **khi đóng context**, đếm `storageGuardChecks`; fixture đòi > 0; `mode.spec.ts` guard cả `memberContext`; `01-access` 12 là negative control (storage có giá trị → guard phải báo) | Playwright vòng 2: log `[storage-guard]` ở mọi test admin, 12 pass (§15.3) |
| RV03 (chặn) | `auth/csrfRefresh.ts`: `refreshCsrf(sentAs)`; client ghi `sessionUser()` lúc gửi; refresh đồng thời dùng chung một lần đọc session; chỉ gửi lại khi phiên đọc được là đúng user đó; `sentAs` null → không gửi lại | `csrfRefresh.test.ts` 5: hai mutation đồng thời lúc đổi user → 2 request, 0 lần gửi lại, `switched` 1 lần; null user; cùng user gửi lại 1 lần với token mới (đỏ: `raw/B18-RV03-red.out`, 4 failed / 1 passed) |
| RV04 | InfoTab tenant: một slot in-flight cho rename/toggle, nút kia disabled | W1 2d (PATCH bị giữ; If-Match v, v+1): pass |
| RV05 | Dialog membership và thao tác worker giữ input khi 412, hiện câu conflict trong dialog; membership đích đã mất / thao tác worker không còn áp dụng → đóng, báo trên trang (câu conflict y nguyên) | W1 4b pass; W2 17 pass ở run2/run3 (§15.3) |
| RV06 | `numberText` hiển thị thập phân ngắn nhất đọc lại đúng (bỏ `toFixed(10)`) | `policyForm.test.ts` (đỏ: `raw/B18-RV06-red.out`, 8 failed / 9 passed) |
| RV07 | Tên kịch bản 16 và §6 nói đúng: 409 là check đầu tiên không đạt; spec khẳng định `state_conflict` + lý do thuộc danh sách guard | W2 16 pass (run1, run2, run3) |
| RV08 | `actUntilAccepted` chỉ chấp nhận đúng các lần từ chối khai báo (`STALE_ETAG`, `ENABLE_NOT_YET`); mode/frozen khẳng định status/code/message của từng PATCH/POST | mode 1, frozen 1, W2 5 pass |
| RV09 | Docstring `_freeze_writes` nói rõ không phải đường API: không rule/proof chuyển mode, không audit/event, không charge ledger | – |
| RV10 | (1) `useUrlRange`: Audit/Khôi phục/Fairness ghi khoảng mặc định lên URL; (2) error boundary trong layout reset theo `pathname`; (3) `IntentSlot`: intent + giá trị đã gửi sống ở trang, mở lại dialog sau kết quả chưa chắc → điền lại, gửi lại cùng key | (2) `AppErrorBoundary.test.ts` (đỏ: `raw/B18-RV10-boundary-red.out`, 2 failed / 1 passed); (3) `intent.test.ts` (đỏ: `raw/B18-RV10-intent-red.out`, 2 failed / 5 passed); (1) W1 8/9 và (3) W1 2e: pass |

### 15.2 Gate đã chạy trong vòng 2 (`raw/B18-round2-gates.out`)

| Lệnh | Kết quả |
|---|---|
| `uv run --no-sync ruff check .` / `ruff format --check .` | sạch / 519 files already formatted |
| `pytest -q` (mặc định, không PG) | **1796 passed, 620 skipped** (vòng 1: 1795/620; +1 test unit fairness) |
| `pnpm --dir web run typecheck` (gồm `check:api`, `tsc -b` cả `tests/e2e`) | sạch |
| `pnpm --dir web run build` | 173 module; `index.html` sha256 `de4c3dbb3979…`; `index-CgsJn8hd.js` (465,53 kB), `index-B-fVmwfX.css` |
| `pnpm --dir web run test` | **29 files / 217 tests passed** (vòng 1: 28/197) |
| Sau khi sửa spec W2 17: `typecheck` / `test` / hash dist (`raw/B18-round2-gates-final-web.out`) | sạch / **29 files, 217 tests** / `de4c3dbb3979` không đổi |
| `git diff --check` | sạch |

### 15.3 PG và Playwright vòng 2 (sau khi môi trường hết bị chặn)

Lần đầu thử, Docker bị chặn: `nexa_b10_smoke3-caddy-1` có **31286 PID**, và `docker exec` báo
"procReady not received" (`raw/B18-round2-gates.out`). Container này không bị đụng tới. Sau khi user
giải phóng PID, nó còn **14 PID** và mọi lượt dưới đây đã chạy trong vòng 2.

**PG** (DB `nexa_b05_test_b18`, `--run-postgres`, URL đã che)

| Lệnh | Kết quả | Raw |
|---|---|---|
| `pytest --run-postgres tests/integration/test_admin_fairness_b18.py tests/unit/test_fairness_report_b18.py` | Lượt đầu **1 failed / 18 passed**: `test_open_segment_is_measured_to_the_statement_time` (xem lỗi 1). Sau khi sửa: **19 passed × 3 lượt** | `raw/B18-round2-pg-fairness.out` |
| `pytest --run-postgres -q` (toàn suite, lượt 1) | **1 failed, 2387 passed, 28 skipped, 2 warnings**, 975 s, EXIT=1: `test_b18_admin_reads_can_use_the_new_indexes` (xem lỗi 2) | `raw/B18-round2-pytest-pg.out` |
| `pytest --run-postgres` cho các test bị ảnh hưởng sau khi sửa (`test_migration_b18.py`, `test_admin_fairness_b18.py`) | **23 passed** | `raw/B18-round2-pg-index-fix.out` |
| `pytest --run-postgres -q` (toàn suite, lượt 2, sau khi sửa) | **2388 passed, 28 skipped, 2 warnings**, 755,5 s, EXIT=0 (vòng 1: 2387/28; +1 test fairness PG). Hai warning là deprecation của dependency (`StarletteDeprecationWarning` httpx, `DeprecationWarning` anyio `BlockingPortal`), cùng số với lượt 1 của vòng 2 | `raw/B18-round2-pytest-pg-run2.out` |

Lỗi tìm thấy trong vòng 2 và cách sửa (đều ở test hoặc script, không ở sản phẩm):

1. **Lệch đồng hồ host và DB.** Test segment mở lấy cận trên và cận dưới bằng `datetime.now` của
   host, trong khi server đo tới giờ DB của statement. Docker VM chạy nhanh hơn host khoảng 10–33 ms,
   nên `30.179654 <= 30.146` sai. Sửa: lấy cả hai cận bằng `clock_timestamp()` của DB (`_db_now`),
   dung sai 1 ms.
2. **Regression do chính vòng 2 gây ra.** `test_migration_b18.py` và `scripts/b18_explain.py` vẫn
   bind `bucket_us`/`range_us` của SQL µs cũ. Sửa: thêm `fairness_parameters()` trong
   `admin_queries.py`; `fairness_rows`, test và script cùng dùng hàm này.

Trong vòng 2, `scripts/b18_explain.py` chưa được chạy lại; raw EXPLAIN vòng 1 là của SQL µs. Vòng 2 chỉ có
`test_b18_admin_reads_can_use_the_new_indexes` pass với SQL ms. Task Review vòng 3 ghi thiếu sót này là RV11; EXPLAIN đã đo lại ở vòng 4 (§4, §16).

**Playwright** trên dist `de4c3dbb3979` (`index-CgsJn8hd.js`). Cấu hình: retries=0, workers=1, không
sleep. DB riêng là `nexa_b05_test_b18r2e2e`; stack `up --tier w1` không thuộc task này (DB `b18e2e`,
Caddy `nexa_b17_caddy_ec2189ce`) không bị đụng, và không chạy harness `cleanup`. Sau mỗi lượt đều kiểm
leftover: procs=0, containers=0, db_sessions=0, smoke3 14 PID (`raw/B18-r2-chain4-driver.out`).

| Lượt | Kết quả | Raw |
|---|---|---|
| w1-admin (RV02/RV03/RV04/RV05/RV10) | **20 passed** (32.6s) | `raw/B18-r2-w1-admin.out` |
| B17 w1-desktop + w1-mobile (RV03, AC-15) | **23 passed** | `raw/B18-r2-b17-w1.out` |
| w1-admin-mode | **1 passed** | `raw/B18-r2-w1-admin-mode.out` |
| w1-admin-frozen | **1 passed** | `raw/B18-r2-w1-admin-frozen.out` |
| B17 w1-admission | **1 passed** | `raw/B18-r2-b17-admission.out` |
| w2-admin + w2-admin-mobile, run1 | **1 failed, 4 passed** (kịch bản 17) | `raw/B18-r2-w2-admin.out` |
| B17 w2 | **4 passed** | `raw/B18-r2-b17-w2.out` |
| w2-admin + w2-admin-mobile, run2 (sau khi sửa) | **5 passed** | `raw/B18-r2-w2-admin-run2.out` |
| w2-admin + w2-admin-mobile, run3 (reporter JSON) | **5 passed**; JSON: expected 5, unexpected 0, flaky 0 | `raw/B18-r2-w2-admin-run3.out`, `raw/B18-r2-w2-admin-annotations.out` |

**RV02.** Mỗi test admin in một dòng `[storage-guard] <test>: pages=N contexts=M`, với N ≥ 1 và
M ≥ 2. Ví dụ: w1-admin 20/20 dòng, mode `pages=2 contexts=3`, W2 5/5 dòng. Negative control
`01-access` 12 pass.

**W2 run1, kịch bản 17.** Lần enable cuối nhận 409 "Quarantined allocations still await verified
cleanup".

- Annotation của run3 ghi rằng khi kịch bản 17 bắt đầu, worker vẫn còn một allocation **HELD**: job
  của kịch bản 16 đã SUCCEEDED, nhưng allocation chỉ được giải phóng sau khi worker xác nhận đã dọn
  container.
- Lần disable mới của vòng 2 (RV05: admin hai disable rồi enable) đã fence allocation này thành
  QUARANTINED, nên enable bị guard chặn đúng.
- Đây là hành vi đúng của sản phẩm (HELD-until-cleanup, invariant release sau cleanup), không phải
  defect. Test thiếu điều kiện đầu.
- Sửa: kịch bản 17 chờ `HELD + QUARANTINED = 0` trên worker (`expect.poll`, khoảng 1 s) trước khi
  thao tác. `actUntilAccepted` nay ghi cả các lần từ chối trước đó khi gặp kết quả ngoài danh sách.
- Run2 và run3 pass. Annotation run3 của kịch bản 17: lúc bắt đầu `["HELD"]`; các lần enable bị từ
  chối trước 200 là incarnation chưa reconcile, chưa READY, rồi 412. Kịch bản 16 thấy câu QUARANTINED
  là check đầu tiên không đạt.

Điều kiện đóng:

- **RV01:** PG chạy trên segment thật, không cắt thời gian, sai số 1e-9. Đạt.
- **RV02:** storage được kiểm trước khi context đóng; đã chạy lại w1-admin, mode, frozen, w2-admin và
  mobile. Đạt.
- **RV03:** Vitest đồng thời và null-user, dist mới, W1 admin 20 và B17 W1 23. Đạt.
- RV04–RV10: xem §15.1.

**Môi trường sau vòng 2**

- DB `nexa_b05_test_b18r2e2e` giữ lại (Playwright vòng 2); `nexa_b05_test_b18` và
  `nexa_b05_test_b18e2e` giữ nguyên.
- `web/test-results` không còn trace.
- Tệp tạm `/tmp/nexa-b18-chain4*.sh` và `/tmp/nexa-b18-w2r3.json` đã xóa; `/tmp/nexa-b18-pg.sh` giữ
  lại (không chứa secret).
- Container `nexa_b10_*`, image và stack ngoại không đổi. Không dùng VPS1 hay AWS.

## 16. Vòng 4 (sau Task Review vòng 3 "Không duyệt", 2026-10-03)

Vòng 3 đóng RV01, RV02, RV03 và RV13 (lệch đồng hồ host/DB). Finding chặn còn lại duy nhất là
**RV11**: EXPLAIN chưa đo lại SQL ms đang ship, trong khi AC-08 ghi pass. Vòng 4 **không sửa source**,
chỉ chạy lại script và cập nhật tài liệu.

| Bước | Kết quả | Raw |
|---|---|---|
| `createdb nexa_b05_test_b18r4perf`, `scripts/b18_explain.py seed` | EXIT=0, khoảng 9 phút; PG 17.11; 100 200 job / 20 tenant; **202 010 segment**, 40 mở, trải 61 ngày | `raw/B18-r4-explain-seed.out` |
| `scripts/b18_explain.py explain-fairness` | EXIT=0; 1 h/24 h/31 ngày/31 ngày kết thúc 29 ngày trước × mọi tenant/một tenant, kèm `Execution Time`, cộng biến thể `enable_seqscan=off` | `raw/B18-r4-explain-fairness.out` |

Số liệu và lý do chọn plan ở §4 (bảng vòng 4). 31 ngày mọi tenant: 773,1 ms execution (Seq Scan,
selectivity khoảng 50 %); 31 ngày kết thúc 29 ngày trước: 736,0 ms. Cả hai dưới 1 s. AC-08 nay trích
raw này.

Điều kiện đóng RV11:

- Raw mới có header PG 17 và ≥200k segment.
- Có đủ 1 h/24 h/31 ngày × mọi/một tenant, kèm `Execution Time`.
- §4 và AC-08 trích đúng raw đó.
- **Không có source nào mới hơn raw.** Fingerprint của source ngoài `docs/` (`git diff` cộng nội dung
  file untracked) là `be03d8db4c150aa7` trên HEAD `3d00c23`, lấy trước và sau vòng 4. Ngoài `docs/`,
  chỉ `.ruff_cache` và `.hypothesis` (đều bị ignore) mới hơn lượt full PG vòng 2.
- Vì source không đổi, các gate của vòng 2 (§15.2, §15.3) vẫn áp dụng mà không cần chạy lại.

Non-blocking chưa sửa trong vòng 4 (Review giữ mức non-blocking; sửa sẽ đổi source và phải chạy lại
gate):

- **RV12:** sau 412, hộp membership vẫn giữ user đã bị lọc khỏi danh sách, và dòng "hiện là" còn cũ.
- **RV14:** `slot.sent.password` chỉ được xóa khi mở lại dialog.
- **RV15:** `response.json()` được gọi trước khi ghi kết quả.

Môi trường: DB `nexa_b05_test_b18r4perf` đã xóa (`dropdb`) sau khi lưu raw. Không đụng tới các DB `b18`, `b18e2e`, `b18r2e2e`,
stack `up --tier w1` không thuộc task này, container `nexa_b10_*` hay VPS1.
