# B17 — Web UI cho user: evidence

Trạng thái: **vòng 2 — đã sửa B17-RV01/RV02, chờ Task Review vòng 2** (Task Code). Không phải kết luận
Task Review. Vòng 1 Task Review: Không duyệt (RV01, RV02 chặn; RV03–RV11 khuyến nghị); xem §13.

Baseline: `baa6e5083c52f951eb0b55acbd18ca7167215740` (`docs: record green GitHub CI
run after CI-python-fix`), working tree sạch khi bắt đầu. Không dùng Superpowers.

## 1. Kế hoạch

### 1.1 Context map

| Yêu cầu | Contract | Code hiện có | Test hiện có | Khoảng thiếu |
|---|---|---|---|---|
| UI user dùng REST `/v1` chung với CLI (PLAN §5, §13 B17) | `docs/contracts/openapi.yaml`, `state-machines.md`, `security.md` | `web/` chỉ là shell B02 (React 19.3.0, Vite 8.3.0, TS 7.0.2), chưa có router/API client | Không có test web; CI web chỉ typecheck + build | Toàn bộ UI, API client, test web |
| Đăng nhập trình duyệt, CSRF, Origin/Host | `BrowserSession`, `/v1/auth/*` | `src/nexa/api/routes_auth.py`, `nexa.identity` | `tests/api/test_http_contract.py`, `tests/integration/test_control_b15.py` | Không (dùng như có) |
| Tiến độ job | `getJobProgress` (openapi.yaml:553), `ProgressRecord` (:1742) | `attempts.progress_sequence/progress_snapshot` được renew cập nhật (`worker_service.py:1432`) | Renew progress trong `test_worker_authority_b10.py` | Route REST + CLI (B17-R02) |
| Log job | `readJobLogs` (openapi.yaml:521) | Không có route, không có pipeline LOG artifact | – | B17-R01 (ngoài phạm vi) |
| Phục vụ UI qua TLS | PLAN: Caddy/TLS | `deploy/b10/Caddyfile` chỉ proxy API | – | Caddyfile B17 + CSP (B17-R12/R13) |
| Test trình duyệt trên backend thật | PLAN §10/§13 (Playwright) | Pattern chạy API thật: `test_jobs_b08._running_api_process`, `tests/docker/b16_support.py`, `test_b11_vertical` | – | Harness `scripts/b17_e2e_stack.py`, Playwright W1/W2 |

### 1.2 Tóm tắt IA

IA đầy đủ ở [docs/web-ui.md](../web-ui.md) mục "Kiến trúc thông tin" (9 mục),
viết xong trước mọi code UI.

- Thời điểm hoàn tất IA (bản M1): `2026-09-30T16:52:11Z` (mtime tệp), kiểm
  bằng `date -u` lúc `2026-09-30T16:54:16Z`. Chưa có tệp nào trong `web/src`
  được sửa tại thời điểm này.
- Sitemap: `/login`, `/`, `/t/:tenantId/jobs`, `/t/:tenantId/jobs/new`,
  `/t/:tenantId/jobs/:jobId`, `/t/:tenantId/sweeps/:sweepId`, `/t/:tenantId/data`,
  `/account/tokens`, `*`.
- Global nav: `Jobs`, `Dữ liệu`; bộ chọn tenant khi ≥ 2 membership; menu tài khoản
  (`Token CLI`, `Đăng xuất`); slot B18 không render.
- Gộp: template vào Tạo job; upload vào form; sweep vào `Nâng cao`; session,
  attempt, checkpoint, progress vào tab `Tiến trình`; token vào menu tài khoản.
- Giả định UX: UX-A01…UX-A15 (docs/web-ui.md mục 9).

### 1.3 Quyết định kỹ thuật và lý do

| Quyết định | Lý do |
|---|---|
| Tenant nằm trên URL; không dùng localStorage/sessionStorage/IndexedDB | Link chia sẻ đúng tenant, nhiều tab không lẫn, không có gì nhạy cảm trong storage |
| CSRF chỉ trong memory, lấy lại bằng `GET /v1/auth/session` khi tải trang và một lần khi gặp `invalid_csrf` | Theo contract và 6.B |
| Một API client duy nhất `web/src/api/client.ts` dùng `fetch` (không thư viện data-fetching) | Theo 5.I; header tenant/CSRF/Idempotency-Key/If-Match tập trung một chỗ, test được |
| Type sinh bằng openapi-typescript vào `web/src/api/generated.ts` (commit), script `gen:api` và `check:api` (sinh vào tệp tạm rồi so sánh) | Drift giữa contract và type làm check fail |
| Router: `react-router` (pin chính xác) | Được phép; route lồng và `useSearchParams` cho filter/tab/cursor trên URL |
| Polling chi tiết 2 s ×1,5 tới trần 30 s; danh sách 10 s ×1,5 tới trần 60 s; dừng khi terminal/tab ẩn; Retry-After; một request mỗi lúc | 6.C; con số ghi ở docs/web-ui.md, kiểm bằng Vitest fake timers |
| Page size: jobs/artifacts/sweep 25, token 50, event/attempt/checkpoint 100 | Đủ mật độ, ≤ 100; attempt/checkpoint của một job hiếm khi vượt một trang (`web/src/api/limits.ts`) |
| Tải xuống: fetch → Blob → kiểm ETag với checksum → object URL; giới hạn 256 MiB, lớn hơn thì hướng dẫn CLI | Route download cần header tenant và không có Content-Disposition (B17-R07) |
| Upload: SHA-256 bằng `crypto.subtle.digest`, giới hạn 256 MiB | Server đòi `X-Artifact-Checksum` trước (B17-R08) |
| Tài nguyên mặc định 1 core, 1 GiB; priority 1; runtime 300 s; checkpoint 30 s | Vừa bounds của cả ba template v1; server là nơi quyết định (B17-R04) |
| Pause khi template không checkpointable: disabled kèm lý do | Rõ ràng hơn ẩn (UX-A07) |
| Retry mặc định "Chạy lại từ đầu" (`checkpoint_id: null`) | Luôn hợp lệ theo contract; checkpoint COMMITTED là lựa chọn thêm |
| Caddyfile B17 tại `deploy/web/Caddyfile` | Cấu hình phục vụ UI là hạ tầng deploy, không phải test fixture; B21/B25 có thể tái dùng; không đụng `deploy/b10/**` |
| Harness `scripts/b17_e2e_stack.py` (Python) | Tái dùng guard DB, `alembic upgrade head`, pattern chạy API/worker của B08/B11/B16 |
| Vitest chạy trong CI web; Playwright không vào CI | Theo phạm vi B17 (B17-R14) |

### 1.4 Findings và interpretation (B17-Rxx)

Mỗi mục được xác minh lại với code tại baseline; cột "Đóng khi" là điều kiện đóng.
Chi tiết root cause/test tái hiện/evidence đóng ở mục Findings cuối tài liệu.

| ID | Loại | Tóm tắt | Xác minh | Đóng khi |
|---|---|---|---|---|
| B17-R01 | Finding (ngoài phạm vi) | Không có `readJobLogs`, không có pipeline tạo LOG artifact → tab Log hiện "chưa hỗ trợ"; phần log của ACC-27 chưa đạt | Đúng: không có route trong `routes_jobs.py`; không writer nào tạo kind LOG | Task log riêng hoặc B19 |
| B17-R02 | Interpretation | `getJobProgress` chỉ có trong spec → B17 thêm route đúng contract | Đúng: operation matrix B06–B16 không có `getJobProgress` | Route + CLI + test PG (M2) |
| B17-R03 | Interpretation | User không có API quota/usage/chế độ vận hành → UI chỉ phản ánh qua lỗi và waiting_reason | Đúng | Giữ nguyên |
| B17-R04 | Interpretation | Template trên wire không có resource bounds/description → default an toàn + lỗi 422 từ server | Đúng: `Template` không có bounds; bounds nằm trong `deploy/templates/*.v1.json` | Giữ nguyên |
| B17-R05 | Finding (contract) | `BrowserSession` không có tên user/tenant → ID rút gọn + vai trò | Đúng | Contract task sau |
| B17-R06 | Interpretation | Không có yêu cầu artifact theo template trên wire → UI giữ bảng ánh xạ chỉ để lọc picker | Đúng | Giữ nguyên |
| B17-R07 | Interpretation | Download không có tên tệp, cần header tenant → fetch/Blob, tên từ manifest, giới hạn 256 MiB | Đúng: không có Content-Disposition (`artifact_service.py:821`) | Giữ nguyên |
| B17-R08 | Interpretation | Upload cần SHA-256 trước → WebCrypto, giới hạn 256 MiB | Đúng | Giữ nguyên |
| B17-R09 | Interpretation | Events chỉ phân trang bằng `after_sequence` | Đúng | Giữ nguyên |
| B17-R10 | Finding (contract) | Không có API liệt kê sweep; Job không có `sweep_id` → trang sweep không lên nav | Đúng | Contract task sau |
| B17-R11 | Interpretation | Danh sách job không lọc theo user, mỗi lần một state | Đúng | Giữ nguyên |
| B17-R12 | Interpretation | Caddy B10 không phục vụ UI → Caddyfile B17 cho dev/test | Đúng | Caddyfile B17 (M3) |
| B17-R13 | Interpretation | Chưa có CSP/security header → header tối thiểu trên đường UI; audit ở B20 | Đúng | Header + test CSP (M3/M10) |
| B17-R14 | Finding (CI) | Playwright chưa vào GitHub CI | Đúng | Task CI/release |
| B17-R15 | Interpretation (mới) | `ProgressPresent.reported_at`: DB không lưu thời điểm riêng của progress; dùng `attempts.updated_at` của attempt được chọn (renew cập nhật cùng lúc với progress, nhưng các chuyển trạng thái khác của attempt cũng cập nhật cột này) | Đọc `worker_service.py:1432–1451` | Ghi trong docs; cột riêng cần migration → task sau |
| B17-R16 | Finding (contract, mới) | Sweep child chỉ có `parameter_hash`, không có giá trị tham số → trang sweep không hiện tham số từng job con | `SweepChild` trong openapi.yaml | Contract task sau |
| B17-R17 | Finding (contract, mới) | Job chỉ lưu `retry_of_job_id` (mới → cũ); job nguồn bất biến và không có API "các lần chạy lại của job này" → liên kết cũ → mới chỉ biết cho retry tạo trong tab hiện tại (memory, `web/src/features/jobs/retryLinks.ts`) | `Job` trong openapi.yaml; W2 kịch bản 18 | Contract/API task sau |
| B17-R18 | Finding (backend, mới) | `ADMISSION_OFF → NORMAL` qua `requestOperationalMode` luôn 409 `state_conflict` "Readiness and worker reconciliation are required" vì API dùng proof provider mặc định `FailClosedRecoveryProofProvider` (`src/nexa/application/policy_service.py:42,68`) | W1 run 1 kịch bản 6e; `web/tests/e2e/admission/admission.spec.ts` khẳng định 409 | B18/B19 (recovery proof thật); kịch bản 6e chạy trên stack riêng |

### 1.5 Mốc và task

Mỗi mốc: test trước (Vitest/pytest đỏ, Playwright viết cùng trang), rồi code, rồi
lệnh kiểm.

| Mốc | Tệp chính | Test trước | Lệnh kiểm |
|---|---|---|---|
| M1 IA | `docs/web-ui.md`, tệp này | – | đọc lại |
| M2 Backend progress | `src/nexa/api/schemas.py`, `routes_jobs.py`, `application/job_service.py`, `cli/commands/job.py`, `docs/cli.md` | `tests/api/test_openapi_b17.py`, `tests/integration/test_job_progress_b17.py`, `tests/cli/test_job_commands.py`, cập nhật `test_operation_matrix.py`, `test_api_route_contract.py` | `pytest -q`, `pytest --run-postgres -q` các tệp liên quan, ruff |
| M3 Tooling + harness | `web/package.json`, lockfile, `web/src/api/generated.ts`, `web/vitest.config.ts`, `web/playwright.config.ts`, `scripts/b17_e2e_stack.py`, `deploy/web/Caddyfile` | smoke Vitest | `pnpm install --frozen-lockfile`, `typecheck`, `build`, `test`, `check:api` |
| M4 Khung app | `web/src/api/*`, `auth/*`, `app/*`, `components/*`, `styles/*` | client, errors, next guard, idempotency | Vitest + W1 đăng nhập |
| M5 Jobs | `features/jobs/*` | polling, cursor stack, actions matrix, labels | Vitest + W1 phân trang/tenant |
| M6 Tạo job + sweep | `features/submit/*` | form schema, spec builder, units, sweep | Vitest + W1 tạo job/sweep |
| M7 Control | `features/jobs/controls*` | matrix, If-Match/412 | W1 hủy/412/vai trò; W2 pause/resume/cancel/retry |
| M8 Kết quả + Dữ liệu | `features/data/*`, `features/jobs/ResultTab*` | filename sanitize, checksum | W1 upload/download; W2 result |
| M9 Token | `features/account/*` | – | W1 token |
| M10 Responsive + Playwright | CSS, `web/tests/e2e/*` | – | W1 desktop+mobile, W2 |
| M11 Tài liệu + evidence | docs mục 9, `.github/workflows/ci.yml`, README/ROADMAP | – | toàn bộ gate, `git diff --check` |

## 2. Thay đổi chính theo từng file

### 2.1 Backend (getJobProgress, B17-R02)

| Tệp | Thay đổi |
|---|---|
| `src/nexa/api/schemas.py` | `ProgressPresent`, `ProgressAbsent`, `ProgressRecord` (union đóng theo `available`) đúng `openapi.yaml` |
| `src/nexa/api/routes_jobs.py` | `GET /v1/jobs/{job_id}/progress` (`getJobProgress`), đọc qua `resolve_principal(mutation=False)`, lỗi 400/401/403/404/500/503 |
| `src/nexa/application/job_service.py` | `get_progress`: kiểm tenant/membership như các route đọc job khác; job khác tenant → 404; chỉ xét attempt mới nhất (`attempt_number` cao nhất, vì attempt phục hồi bắt đầu lại từ sequence 1), chưa có progress → `available: false`; `reported_at` = `attempts.updated_at` (B17-R15); `restore_checkpoint_id` lấy từ `execution_context` của attempt đó |
| `src/nexa/cli/commands/job.py` | `nexa job progress <job_id> [--tenant]` |
| `tests/api/test_openapi_b17.py` (mới) | 2 test: shape operation, union đóng |
| `tests/integration/test_job_progress_b17.py` (mới, PG) | 4 test: absent, present mới nhất, sau restore, tenant/quyền đọc |
| `tests/cli/test_job_commands.py`, `tests/cli/test_api_route_contract.py`, `tests/api/test_operation_matrix.py` | lệnh CLI; route contract; operation matrix có `getJobProgress` |

Không migration mới; head vẫn `20260929_0022`, `schema_v19` không đổi.

### 2.2 Web

- `web/package.json`, `web/pnpm-lock.yaml`, `web/pnpm-workspace.yaml`: dependency mới pin chính xác:
  `react-router` 8.4.0; dev `@playwright/test` 1.63.0, `vitest` 5.0.2, `@types/node` 24.19.0.
  `openapi-typescript` 7.13.0 (cùng `typescript` 5.9.3 nó cần) nằm trong workspace riêng
  `web/tools/openapi-gen` để không kéo TypeScript thứ hai vào app. Script: `test`, `gen:api`,
  `check:api` (typecheck chạy `generate.mjs --check` trước `tsc -b`).
- `web/src/api/*`: client `fetch` duy nhất (tenant/CSRF/Idempotency-Key/If-Match, timeout, retry
  `invalid_csrf` một lần), ánh xạ lỗi, polling, idempotency, `generated.ts` (sinh từ contract),
  `limits.ts`.
- `web/src/auth/*`: session trong memory, guard `next` chống open redirect, trang đăng nhập.
- `web/src/app/*`: layout, nav, tenant switcher, nhãn trạng thái; `web/src/App.tsx` khai báo route.
- `web/src/components/*`: Dialog, Tabs, Pager, ErrorPanel, DownloadButton, CliHint, `usePolled`.
- `web/src/features/{jobs,submit,data,account}/*`: danh sách/chi tiết/điều khiển job, form tạo
  job + sweep, dữ liệu (upload/download), token CLI.
- `web/src/styles/{tokens,components,layout}.css` thay `web/src/styles.css` (xóa).
- `web/index.html`, `web/src/main.tsx`, `web/tsconfig.*.json`: entry, `types` cho test.
- `web/vitest.config.ts`, `web/playwright.config.ts` (project `w1-desktop`, `w1-mobile`,
  `w1-admission`, `w2`; retries 0), `web/tests/e2e/**` (fixture, global setup, W1/W2/mobile/admission).

### 2.3 Caddy, harness, CI, tài liệu

- `deploy/web/Caddyfile` (mới): một origin TLS (`tls internal`) cho `web/dist` + `/v1/*`; CSP trên
  tài liệu UI; `/docs`, `/redoc`, `/openapi.json`, `/v1/internal/*` trả 404; asset băm cache dài.
- `scripts/b17_e2e_stack.py` (mới): `run --tier w1|w2 -- <lệnh>` và `cleanup`; guard DB
  `nexa_b05_test_*`, `alembic upgrade head`, seed qua API thật, container `nexa_b17_*` chỉ bind
  127.0.0.1, API process không nhận `NEXA_TEST_*`/`NEXA_ENDPOINT`/`NEXA_TOKEN`/`NEXA_TENANT_ID`/`NEXA_PROFILE`.
- `.github/workflows/ci.yml`: thêm đúng một bước `pnpm --dir web run test` vào job Web quality.
- Tài liệu: `docs/web-ui.md` (mới), `docs/project-structure.md`, `docs/authentication.md`,
  `docs/cli.md`, `docs/environment-inventory.md`, `README.md`, `ROADMAP.md`.

## 3. Verification

### 3.1 Môi trường và phiên bản

| Mục | Giá trị |
|---|---|
| Máy | Mac (P), Docker Desktop engine 29.8.1, VM `aarch64`, 8 CPU, `MemTotal` 4106604544 B (~3,82 GiB) |
| Python | `uv` 0.9.27 tại `/tmp/nexa-b12-uv-bootstrap/bin/uv`, `PYTHONPATH=src:.` |
| Node / pnpm | v24.21.0 / 11.9.0 (trong `/tmp`, chỉ PATH của phiên) |
| Playwright / Chromium | 1.63.0 / 153.0.8010.12 (`chromium-1243`, `chromium_headless_shell-1243`), `PLAYWRIGHT_BROWSERS_PATH=/tmp/nexa-b17-pw`, không `install-deps` |
| Caddy | `caddy:2.10.2-alpine` `sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d` |
| W2 CPU image (arm64, local, không push) | `nexa/cpu-iterative@sha256:a14b604aa9b2c8ccbe2f04b8263568fd63247cb73c069dfc4f9ce5ae18dfe934` |
| W2 worker image (arm64, local) | `nexa/b17-worker:local` id `sha256:9121dc5849805e39d5f941d2c70e6460386d19a1268792a1ca75c1ea52662e37` (`deploy/b10/Dockerfile`) |
| Base image của cả hai | `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9` |
| PostgreSQL | `nexa_b13_pg` (17, 127.0.0.1:15439); DB `nexa_b05_test_b17` (pytest), `nexa_b05_test_b17e2e` (Playwright) |

Image W2 build lúc 01:42–01:43 (`raw/B17-build-cpu.out`, `raw/B17-build-worker.out`); sau đó
không tệp nào trong `src/nexa`, `deploy/b10` hay thư mục image bị sửa, nên image khớp source cuối.

Mật khẩu PostgreSQL chỉ đi qua biến `PGPW` rồi `unset`; mọi output đã che bằng
`sed -E 's#postgresql\+psycopg://[^@]*@#***@#g'`. Dưới đây `<URL>` là
`postgresql+psycopg://postgres:***@127.0.0.1:15439/<db>`.

### 3.2 Lệnh và kết quả

| Lớp | Lệnh | Kết quả | Output |
|---|---|---|---|
| Ruff | `$U run --no-sync ruff check .`; `$U run --no-sync ruff format --check .` | All checks passed; 506 files already formatted; EXIT=0 | `raw/B17-final-gates.out` |
| pytest mặc định | `PYTHONPATH=src:. $U run --no-sync pytest -q` | **1775 passed, 603 skipped**, 0 failed, 57,55 s. Baseline (CI-python-fix B3): 1772 passed, 599 skipped → +3 passed (test không cần DB), +4 skipped (test PG mới) | `raw/B17-pytest.out` |
| pytest PG | `NEXA_TEST_DATABASE_URL=<URL>/nexa_b05_test_b17 PYTHONPATH=src:. $U run --no-sync pytest --run-postgres -q` | **2350 passed, 28 skipped**, 0 failed, 974,94 s | `raw/B17-pytest-pg.out` |
| 3 skip thêm của PG | cùng lệnh + `-rs tests/integration/test_rem_b13_r12_search_path.py`, không và có `NEXA_TEST_PG_CLIENT_PREFIX="docker exec -i nexa_b13_pg"` | không prefix: 6 passed, 3 skipped ("pg_dump/pg_restore unavailable"); có prefix: **9 passed** | `raw/B17-pytest-pg-prefix.out` |
| Đối chiếu PG | Baseline CI-python-fix R3 (có prefix): 2346 passed, 25 skipped. Lượt B17 tương đương 2350 + 3 = 2353 passed, 25 skipped → **+7 = đúng 7 test mới** (2 openapi, 4 PG progress, 1 CLI) | – | – |
| Web (CI replay) | bản sao sạch `/tmp/b17-ci-web` (100 tệp: `web/` tracked + untracked không ignore, `docs/contracts/`; không `node_modules`/`dist`), Node 24.21.0, pnpm 11.9.0: `pnpm --dir web install --frozen-lockfile && pnpm --dir web run typecheck && pnpm --dir web run build && pnpm --dir web run test` | install 85 package từ lockfile; typecheck (gồm `check:api`) sạch; build 135 module; **vitest 13 files / 101 tests passed**; EXIT=0 | `raw/B17-ci-web.out` |
| Playwright W1 | xem §3.3 | vòng 1: **21 passed** (20 `w1-desktop` + 1 `w1-mobile`), 22,3 s; vòng 2: **23 passed** (22 + 1), 23,4 s, dist `cef5ac78f3a6` | `raw/B17-w1-run4.out`, `raw/B17-w1-run5.out` |
| Playwright W1 admission | xem §3.3 | **1 passed** (vòng 1 và vòng 2) | `raw/B17-w1-admission-run2.out`, `raw/B17-w1-admission-run3.out` |
| Playwright W2 | xem §3.3 | vòng 1: **4 passed**, 2,8 phút; vòng 2: **4 passed**, 1,5 phút | `raw/B17-w2-run2.out`, `raw/B17-w2-run3.out` |
| Lượt gate cuối (sau khi xong tài liệu) | ruff, `pytest -q`, `pnpm --dir web install --frozen-lockfile`, `typecheck`, `build`, `test` | ruff sạch; **1775 passed, 603 skipped**; typecheck sạch; build ra đúng `index-DFsuDEKX.js`/`index-DxNve0CI.css` như CI replay (bundle không đổi từ các lượt Playwright cuối); **vitest 13/101 passed** | `raw/B17-final-gates.out` |

### 3.3 Lệnh Playwright (password không in, URL đã che)

```sh
source /tmp/nexa-b17-env.sh            # PATH Node/pnpm, PLAYWRIGHT_BROWSERS_PATH
pnpm --dir web run build
PGPW=$(docker exec nexa_b13_pg printenv POSTGRES_PASSWORD)
export NEXA_TEST_DATABASE_URL="postgresql+psycopg://postgres:${PGPW}@127.0.0.1:15439/nexa_b05_test_b17e2e"
unset PGPW
# W1 (không worker)
PYTHONPATH=src:. $U run --no-sync python scripts/b17_e2e_stack.py run --tier w1 -- \
  pnpm --dir web exec playwright test --project=w1-desktop --project=w1-mobile
# W1 admission: stack mới riêng (B17-R18)
PYTHONPATH=src:. $U run --no-sync python scripts/b17_e2e_stack.py run --tier w1 -- \
  pnpm --dir web exec playwright test --project=w1-admission
# W2 (coordinator + worker Docker + image CPU)
NEXA_B17_CPU_IMAGE_REF=nexa/cpu-iterative@sha256:a14b604aa9b2c8ccbe2f04b8263568fd63247cb73c069dfc4f9ce5ae18dfe934 \
NEXA_B17_WORKER_IMAGE=nexa/b17-worker:local \
PYTHONPATH=src:. $U run --no-sync python scripts/b17_e2e_stack.py run --tier w2 -- \
  pnpm --dir web exec playwright test --project=w2
# dọn dẹp
PYTHONPATH=src:. $U run --no-sync python scripts/b17_e2e_stack.py cleanup
```

Mỗi lượt `run` tạo lại schema của `nexa_b05_test_b17e2e`, seed lại tenant/user/artifact qua API
thật và dừng mọi process/container khi lệnh con kết thúc. Trước các lượt dài đã kiểm
`docker stats --no-stream`: PID của `nexa_b10_smoke3-caddy-1` tăng chậm (1488 → 1529 → 1573 trong
phiên), chưa chặn; không dừng container đó.

### 3.4 Lịch sử lượt chạy Playwright (không retry, không sleep)

Sau lượt cuối chỉ đổi tài liệu và docstring của `scripts/b17_e2e_stack.py` (thêm dòng dùng `w1-admission`); không đổi code chạy.


| Lượt | Kết quả | Nguyên nhân gốc và sửa |
|---|---|---|
| W1 run 1 | 7 passed, 13 failed, 1 did not run | (a) `getByLabel("Tệp")` khớp 2 phần tử trong dialog upload → selector theo role (test). (b) cursor quá ngắn bị server trả `validation_failed`, UI chỉ bắt `invalid_cursor` → `isBadCursorError` nhận cả lỗi query `cursor` (code UI, có unit test). (c) test giả định form chọn sẵn CPU; form chọn template đầu tiên theo thứ tự API → helper `chooseCpu` (test). (d) 6e: `ADMISSION_OFF → NORMAL` bị 409 (B17-R18) nên tenant ở lại ADMISSION_OFF và các test submit sau fail dây chuyền → 6e tách sang project/stack `w1-admission`. (e) token: context request mang cả cookie member A và bearer → 400 → `storageState` rỗng tường minh (test). |
| W1 run 2 | 19 passed, 1 failed | 6b thiếu `chooseCpu`, picker không có INPUT CPU → thêm `chooseCpu` (test) |
| W1 run 3 | 20 passed | chưa có `origin.spec.ts` (thêm sau lượt này) |
| W1 run 4 (cuối vòng 1) | **21 passed** | – |
| Admission run 1 / 2 | 1 passed / **1 passed** | run 2 là lượt cuối, trên stack mới riêng |
| W2 run 1 | 3 passed, 1 failed | 16 kỳ vọng attempt `[1,2]`; API trả mới nhất trước `[2,1]` → sửa kỳ vọng và kiểm hàng UI theo ô `#n` (test) |
| W2 run 2 (cuối vòng 1) | **4 passed** | – |
| W1 run 5 (vòng 2) | **23 passed** | thêm 1b (RV01) và 7c (RV02); dist `cef5ac78f3a6` |
| Admission run 3 (vòng 2) | **1 passed** | cùng dist `cef5ac78f3a6` |
| W2 run 3 (vòng 2) | **4 passed** | cùng dist `cef5ac78f3a6` |

## 4. Kịch bản Playwright → yêu cầu → kết quả

PLAN:203 là yêu cầu Web UI user; "Gate B17" là gate B17 trong PLAN §13. Kịch bản 14 (CSP) chạy
trong fixture chung (`web/tests/e2e/support.ts`): mọi test (W1, admission, W2) theo dõi sự kiện
`securitypolicyviolation`, console CSP và page error, và kiểm mọi request list có `page_size ≤ 100`;
test fail ở teardown nếu vi phạm.

| # | Test | Yêu cầu | ACC | Kết quả (lượt cuối) |
|---|---|---|---|---|
| 1 | `auth.spec.ts` 1 | đăng nhập, lỗi chung, cookie HttpOnly/Secure/SameSite=Lax, storage sạch, reload, logout → 401 | ACC-24, ACC-27 | pass |
| 1b | `auth.spec.ts` 1b | đăng nhập từ `/login?next=/.//evil.example` → trang Jobs nội bộ, reload vẫn vào, không page error (B17-RV01) | ACC-24 | pass (vòng 2) |
| 2 | `auth.spec.ts` 2 | mutation thiếu CSRF → 403 `invalid_csrf` | ACC-24 | pass |
| 3 | `auth.spec.ts` 3 | phiên bị thu hồi → login + thông báo, `next` về đúng trang | ACC-24 | pass |
| 4 | `tenancy.spec.ts` 4 | B không xem/tải/gọi tay object của A (404); multi đổi tenant | ACC-03 | pass |
| 5 | `tenancy.spec.ts` 5 | member A2 không có control, gọi ép 404; tenant admin A hủy được | ACC-03, ACC-27 | pass |
| 6a/b/c/f | `submit.spec.ts` | 3 template từ `parameter_schema`; lỗi inline; 422 `infeasible_request` trong nhóm Tài nguyên | ACC-27 | pass |
| 6d | `submit.spec.ts` | `quota_exceeded` rõ nghĩa, không đếm ngược | ACC-27 | pass |
| 6e | `admission.spec.ts` | ADMISSION_OFF → thông điệp tạm ngừng, không tạo job; trả NORMAL → 409 (B17-R18) | ACC-21, ACC-27 | pass (phần trả NORMAL: finding) |
| 7a/b | `submit.spec.ts` | double click → 1 job; cắt response đầu → gửi lại cùng Idempotency-Key → 1 job | ACC-07 | pass |
| 7c | `submit.spec.ts` | API commit, trình duyệt nhận 502 rỗng (như Caddy khi upstream chết) → gửi lại cùng Idempotency-Key → 1 job (B17-RV02) | ACC-07 | pass (vòng 2) |
| 8a/b | `data.spec.ts` | upload INPUT → danh sách → form; tải xuống SHA-256 khớp; tệp > 256 MiB không đọc, hiện lệnh CLI | ACC-27 | pass |
| 9 | `pagination.spec.ts` | ≥ 58 job của tenant B (> 2 trang); sau/trước/đầu; filter state/template; empty; cursor sửa → trang đầu; `page_size` 25 | ACC-27 | pass |
| 10 | `controls.spec.ts` 10 | ETag cũ → 412 → tải lại, giải thích, không gửi lặp | ACC-07, ACC-27 | pass |
| 11 | `controls.spec.ts` 11 | hủy QUEUED: bỏ qua không đổi gì; xác nhận → CANCELLED | ACC-27 | pass |
| 12 | `sweep.spec.ts` | sweep 2×2 → 4 job con đúng tổ hợp, link mở job | ACC-27 | pass |
| 13 | `tokens.spec.ts` | token hiện một lần, reload mất, thu hồi → 401; member không thấy `admin:*` | ACC-24 | pass |
| 14 | fixture + `origin.spec.ts` | không vi phạm CSP; header tài liệu; asset băm; không inline script; docs/internal đóng | ACC-24 | pass |
| M | `mobile.spec.ts` (390×844) | login, list, submit, detail, dialog, dữ liệu, token; không cuộn ngang | ACC-27 | pass |
| 15 | `lifecycle.spec.ts` 15 | submit UI → RUNNING + progressbar → SUCCEEDED → tệp/chỉ số → tải tệp khớp checksum manifest; không còn request đọc sau terminal | ACC-27 | pass |
| 16 | `lifecycle.spec.ts` 16 | pause → PAUSING/PAUSED + checkpoint COMMITTED → resume → SUCCEEDED; 2 attempt | ACC-27 | pass |
| 17 | `lifecycle.spec.ts` 17 | hủy RUNNING: CANCELLING rồi mới "Đã hủy" | ACC-27 | pass |
| 18 | `lifecycle.spec.ts` 18 | FAILED (runtime 5 s, TIMEOUT) → retry từ đầu → job mới có `retry_of_job_id`, link hai chiều, job cũ không đổi | ACC-27 | pass |
| 19 | – | (tùy chọn) recovery qua UI | ACC-27 | not-run (không làm) |

## 5. Timeline control W2 (lượt run 2)

Harness không ghi timestamp cho từng chuyển trạng thái; bảng dưới ghi thứ tự quan sát được (các
assertion web-first chạy tuần tự) và thời lượng test trong `raw/B17-w2-run2.out`.

| Kịch bản | Thời lượng | UI (nhãn `.status-block .badge`) | API |
|---|---|---|---|
| 15 | 31,4 s | "Đang chạy" (+ progressbar "Tiến độ") → "Hoàn tất"; tab Kết quả mở | result có manifest; SHA-256 manifest = `manifest_checksum`; tệp tải về = `checksum` trong manifest. Số request đọc job > 3 khi terminal và **không tăng** sau `clock.runFor(120 000 ms)` |
| 16 | 1,5 phút | "Đang chạy" → "Đang tạm dừng (chờ checkpoint)" hoặc đã sang "Đã tạm dừng" → "Đã tạm dừng"; hàng checkpoint "Đã ghi"; 1 hàng attempt → "Đang chạy" → "Hoàn tất" | khi PAUSED có checkpoint `COMMITTED`; cuối: attempts `[2, 1]`, attempt #2 `SUCCEEDED`; bảng UI `#2 Hoàn tất`, `#1` |
| 17 | 21,6 s | chuỗi nhãn ghi bằng MutationObserver: … "Đang chạy" … rồi đúng `["Đang hủy (chờ xác nhận dừng)", "Đã hủy"]` | job `CANCELLED`; mọi attempt terminal |
| 18 | 15,8 s | "Thất bại"; hàng attempt "Quá thời gian" → dialog retry ("Chạy lại từ đầu" mặc định) → job mới | attempts `[["FAILED","TIMEOUT"]]`; job mới `retry_of_job_id` = job cũ; job cũ vẫn `FAILED`, `version` không đổi |

Giới hạn của bằng chứng: assertion progressbar ở 15 không bắt buộc cùng khung hình với RUNNING;
cpu-iterative không có metrics nên phép so số dòng "Chỉ số" là 0 = 0; ở 16 nhãn PAUSING có thể
đã qua trước khi assertion nhìn thấy (regex nhận cả hai), PAUSED và checkpoint COMMITTED thì
được kiểm chắc chắn.

## 6. Ảnh chụp

`docs/evidence/raw/B17/`: `01-desktop-login`, `02-desktop-create-job`, `03-desktop-job-running`,
`04-desktop-job-result`, `05-desktop-jobs`, `06-desktop-data`, `07-desktop-tokens`,
`08-mobile-jobs`, `09-mobile-job-result`, `10-mobile-create-job` (`.png`). Chụp bằng script một lần
ngoài repo trên stack W2. Đã xem từng ảnh: ô đăng nhập trống, không có token, password hay CSRF;
trang token không có secret đang hiện. **Ảnh chỉ minh họa bố cục, không chứng minh authorization.**

## 7. Output thô

`docs/evidence/raw/B17-*.out`: `w1-run1..5`, `w1-admission-run1..3`, `w2-run1..3`, `pytest`,
`pytest-pg`, `pytest-pg-prefix`, `ci-web`, `final-gates`, `build-cpu`, `build-worker`. Đã bỏ mã màu ANSI và che
DB URL; `grep` các mẫu password/cookie/CSRF/bearer/`postgresql://` trả 0 dòng. Trace/HAR/video/
storageState không nằm trong repo: storageState và fixture (0600) nằm trong thư mục tạm của
harness (`tempfile.mkdtemp`), bị `cleanup` xóa; `web/test-results` bị ignore và đã xóa khi dọn.

## 8. Quan sát và sai lệch so với prompt

- **API bind 127.0.0.1 cả trên Docker Desktop** (prompt gợi ý 0.0.0.0). Caddy và worker tới API qua
  `host-gateway` đã đủ; bind loopback chặt hơn. Vì Docker Desktop đưa request từ proxy tới API với
  nguồn 127.0.0.1, nằm trong CIDR bảo trì mặc định, Caddyfile đóng `/v1/internal/*` (404) và
  `origin.spec.ts` kiểm điều này.
- **Retry-After của `quota_exceeded`**: UI bỏ qua (không đếm ngược) vì quota không tự hết theo
  thời gian; `rate_limited` và đăng nhập vẫn đếm ngược theo Retry-After (`web/src/api/errors.ts`).
- **Caddy PID** của `nexa_b10_smoke3-caddy-1`: 1488 → 1529 → 1573 trong các lượt chạy, **2865** lúc bàn
  giao (`docker stats --no-stream`). Không chặn lượt nào của B17; không dừng/sửa container đó
  (thuộc B10). Người dùng nên theo dõi trước các lượt Docker dài tiếp theo.
- **Kịch bản 19** (tùy chọn) không làm; **L (VPS1)** không chạy cho B17.

## 9. Tự review

- Đọc lại toàn bộ `git diff` và từng tệp untracked. Không `dangerouslySetInnerHTML`; không ghi
  `localStorage`/`sessionStorage`/`IndexedDB` (W1 kịch bản 1 kiểm storage rỗng); CSRF chỉ trong
  memory; client không `console.log` request/response; tên tệp tải xuống được làm sạch
  (`files.ts`, unit test).
- Authorization chỉ ở backend: UI ẩn/disable control theo ma trận hành động nhưng W1 kịch bản 4/5
  gọi ép API và nhận 404.
- Đối chiếu `docs/web-ui.md` với code: đã sửa các chỗ lệch (tên tệp route, page size, hàm polling,
  hành vi CSRF lần hai, 503 không đếm ngược).
- Không sửa PLAN, `docs/contracts/**`, `AGENTS.md`, `docs/acceptance.md`, `compose.yaml`,
  `deploy/b10/**`, migration. Không commit/push/branch, không push image, không dùng Superpowers.

## 10. Acceptance

### 10.1 AC của B17

| AC | Status | Evidence |
|---|---|---|
| AC-01 IA trước code, khớp UI | pass | §1.2 thời điểm IA; `docs/web-ui.md` đã đối chiếu lại (§9) |
| AC-02 login/logout/reload/thu hồi, storage sạch | pass | W1 1, 1b, 3; Vitest `next.test.ts` |
| AC-03 cookie flags, CSRF qua trình duyệt | pass | W1 1, 2 |
| AC-04 hai tenant, đổi tenant | pass | W1 4 |
| AC-05 tạo job 3 template, lỗi, không nhân đôi | pass | W1 6a–6f, 7a/7b/7c, admission 6e; Vitest `idempotency.test.ts` |
| AC-06 filter, cursor ≥ 2 trang, cursor hỏng, ≤ 100 | pass | W1 9 + fixture page_size |
| AC-07 chi tiết từ API thật, polling backoff/dừng | pass | W2 15–18; Vitest `polling.test.ts` (backoff, trần, tab ẩn, Retry-After, một request) |
| AC-08 control, 412, vai trò, không "đã dừng" sớm | pass | W1 5, 10, 11; W2 16, 17, 18 |
| AC-09 tải/upload, checksum | pass | W1 8a/8b; W2 15 |
| AC-10 token một lần, thu hồi, ẩn admin | pass | W1 13 |
| AC-11 responsive | pass | W1 mobile |
| AC-12 không redesign, style tách lớp | pass | `web/src/styles/{tokens,components,layout}.css`; `docs/web-ui.md` Hướng dẫn redesign |
| AC-13 progress route + CLI + PG + operation matrix | pass | §2.1; pytest PG |
| AC-14 Caddy TLS, không vi phạm CSP | pass | `origin.spec.ts` + fixture CSP |
| AC-15 mọi quality gate | pass | §3.2, §11 |

### 10.2 Gate (docs/acceptance.md giữ nguyên `specified`)

| Gate | Tiêu chí thuộc B17 | Test | Môi trường | Status (phần B17) | Giới hạn / task khác |
|---|---|---|---|---|---|
| ACC-27 | user flow, control theo capability, hai tenant, replay/412, phân trang/tải qua REST chung, ≤ 100, polling backoff, attempt/checkpoint thật | W1 1–14 + mobile, W2 15–18, Vitest | P + W | phần user: pass; **gate toàn phần: specified** (chưa thể pass) | log B17-R01; admin flow B18; L not-run |
| ACC-03 | cross-tenant qua UI/URL/API trong trình duyệt | W1 4, 5 | W | pass (phần UI) | race/security đầy đủ B20 |
| ACC-24 | cookie flags, CSRF, storage sạch, TLS Caddy dev/test | W1 1, 2, 3, 13, origin | W | pass (phần B17) | Compose/release B21/B25; audit header B20 |
| ACC-39 | typecheck, build, unit web, Playwright chạy thật | CI replay, W1, W2 | P + W | pass (phần B17) | Playwright chưa vào CI (B17-R14) |
| ACC-07 | replay submit/control qua UI | W1 7a/7b, 10 | W | pass (phần UI) | – |
| ACC-21 | UI hiển thị fail closed/503, không che | W1 admission 6e; Vitest `errors.test.ts` (503) | W | pass (phần UI) | kiểm 503 thật khi DB hỏng thuộc B20/B22 |
| ACC-26 | UI không log nội dung/credential | review §9; client không log | W | pass (phần UI) | log pipeline B17-R01, B19 |
| ACC-36 | UI không trộn simulator/GPU | B17 không hiển thị dữ liệu loại này | W | specified | GPU B23 |
| L (VPS1) | – | – | L | not-run | – |

## 11. Phần thuộc task khác

- Log job (B17-R01): task riêng hoặc B19. Admin UI: B18. Metrics/GC/storage limits: B19.
  Race/security/header audit: B20. Compose, UI image, portability: B21. GPU: B23. Release và
  Playwright trong CI (B17-R14): B25/CI.
- Contract: tên user/tenant trong session (B17-R05), liệt kê sweep (B17-R10), tham số sweep child
  (B17-R16), liên kết retry ngược (B17-R17). Backend: recovery proof cho `ADMISSION_OFF → NORMAL`
  (B17-R18). Cột thời điểm progress riêng (B17-R15, cần migration).
- Finding cũ không đổi: B11-H01, B15-R11, B15-R39, B15-OBS-01, B16-R29, OD-1..3, REM-R08, CI-R01.

## 12. Trạng thái môi trường khi bàn giao

- `scripts/b17_e2e_stack.py cleanup`: 0 container, 0 state dir còn lại; `docker ps -a --filter
  name=nexa_b17` rỗng; không còn process uvicorn/coordinator/harness/Playwright.
- `web/test-results` đã xóa; `web/dist`, `web/node_modules` bị ignore (giữ lại để chạy lại).
- Còn lại có chủ đích: DB test `nexa_b05_test_b17`, `nexa_b05_test_b17e2e` trong `nexa_b13_pg`;
  image local `nexa/cpu-iterative@sha256:a14b604a…fe934`, `nexa/b17-worker:local`,
  `caddy:2.10.2-alpine`; Node/pnpm/Chromium/uv trong `/tmp` (không chứa secret).
- VPS1: không dùng cho B17.

## 13. Vòng 2 — sửa theo Task Review vòng 1

Task Review vòng 1: **Không duyệt**. Chặn: B17-RV01, B17-RV02. Khuyến nghị (không chặn, chưa sửa ở
vòng này): RV03–RV11. Chỉ sửa hai finding chặn và một khuyến nghị một dòng của RV01 (so `/login`
không phân biệt hoa thường); không đổi backend, harness, Caddy hay tài liệu contract.

| Finding | Sửa | Kiểm đóng | Status |
|---|---|---|---|
| B17-RV01 `next` dot-segment → `//evil.example`, trang trắng | `web/src/auth/next.ts`: sau `new URL`, trả `null` nếu `url.pathname` bắt đầu bằng `//`; so `/login` theo chữ thường | Vitest `next.test.ts`: `/.//x`, `/%2e%2e//x`, `/%2E%2E//x`, `/a/..//x`, `/a/b/../..//x`, `/././/x?x=1` → `null`; vét 729 tổ hợp `/a/b/c` từ các đoạn `.`, `..`, `%2e`, `%2e%2e`, `//`… và assert mọi giá trị trả về bắt đầu bằng `/` nhưng không bằng `//`; `/LOGIN`, `/Login/x` → `null`. W1 1b: đăng nhập từ `/login?next=/.//evil.example` vào trang Jobs, reload vẫn vào; fixture `guard` không ghi nhận page error | closed (chờ Task Review xác nhận) |
| B17-RV02 proxy 5xx không envelope → bỏ key → gửi lại tạo trùng | `web/src/api/idempotency.ts`: `IntentTracker.settle` giữ key khi `code === "unexpected_response"` và `status >= 500`; 5xx có envelope (ví dụ 503 `storage_pressure`) vẫn là intent mới. Áp dụng cho mọi nơi dùng `IntentTracker` (tạo job/sweep, control, upload, tạo token); thu hồi token vốn giữ key theo token | Vitest `idempotency.test.ts`: 500/502/503/504 không envelope giữ key; 503 `storage_pressure` và 418 không envelope sinh key mới. W1 7c: `route.fetch()` nhận 202 thật, `route.fulfill({ status: 502, body: "" })`; bấm gửi lại → 2 POST cùng `Idempotency-Key`, API chỉ có 1 job mới | closed (chờ Task Review xác nhận) |

Đỏ trước xanh: tạm bỏ hai dòng sửa (`url.pathname.startsWith("//")`, `isProxyFailure`) rồi chạy
`vitest run src/auth/next.test.ts src/api/idempotency.test.ts` → **3 failed** (2 ca RV01, 1 ca RV02);
khôi phục → 15/15 passed.

Lệnh và kết quả vòng 2 (Mac, môi trường P; lệnh Playwright như §3.3):

| Gate | Kết quả | Raw |
|---|---|---|
| `pnpm --dir web run typecheck` (gồm `check:api`) | sạch | – |
| `pnpm --dir web run test` | **13 files / 105 tests passed** (vòng 1: 101; +4) | – |
| `pnpm --dir web run build` | 135 module; `index-DILNAH-c.js`, `index-DxNve0CI.css`; `index.html` sha256 `cef5ac78f3a6…` (vòng 1: `f5b69e53c73c`) | – |
| Playwright W1 (`w1-desktop` + `w1-mobile`) | **23 passed**, 23,4 s; header `dist index=cef5ac78f3a6` | `raw/B17-w1-run5.out` |
| Playwright W1 admission | **1 passed**; header `dist index=cef5ac78f3a6` | `raw/B17-w1-admission-run3.out` |
| Playwright W2 | **4 passed**, 1,5 phút; header `dist index=cef5ac78f3a6` | `raw/B17-w2-run3.out` |
| `git diff --check` | sạch | – |
| Quét raw vòng 2 (password/CSRF/bearer/cookie/DB URL) | chỉ khớp tên test "2. CSRF: …"; không có secret | – |

Không chạy lại ở vòng 2: ruff/pytest (không đổi Python), CI clean-copy replay và
`pnpm install --frozen-lockfile` (không đổi `package.json`/lockfile). Trước lượt chạy,
`nexa_b10_smoke3-caddy-1` có 5158 PID (vòng 1 bàn giao: 2865; lúc Task Review: 4519); không dừng
container đó theo ràng buộc, user nên xử lý trước các lượt Docker dài.

Khuyến nghị error boundary cấp app (RV01) chưa làm: sau sửa, `<Navigate>` không còn nhận giá trị
`//…`; để lại cho Task Review quyết định cùng RV03–RV11.

Trạng thái môi trường sau vòng 2: `cleanup` sau mỗi lượt; không còn container `nexa_b17_*`, state
dir `nexa-b17-run-*`, process harness/uvicorn/coordinator. VPS1 vẫn không dùng.
