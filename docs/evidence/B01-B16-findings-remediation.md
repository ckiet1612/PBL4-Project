# B01–B16 findings remediation evidence

Trạng thái: **đã triển khai, chờ Task Review** (vòng 2, sau Task Review vòng 1 "Không duyệt" với RV01–RV07;
xem "Vòng 2: phản hồi Task Review"). Baseline `a6c38bf` (B16), working tree sạch
lúc bắt đầu (2026-09-28). Không dùng Superpowers theo quyết định của user cho task này. Không commit,
push, branch; không sửa PLAN.md. P = Docker Desktop Linux VM trên Mac (arm64); L = VPS1 (AWS EC2
c7i.2xlarge, Ubuntu 24.04), Linux thật, không GPU — không phải G/R.

P = Docker Desktop VM; L = VPS1, Linux thật, không GPU (không phải G/R); không nâng gate ACC nào lên pass
nếu chưa có đủ evidence của gate đó.

## Kế hoạch

### Context map

| Vùng | Finding | Code chính | Contract/docs |
|---|---|---|---|
| Hàm SQL Decimal/trigger (B05/B13) | B13-R12 | `migrations/versions/20260919_0001_b05_initial.py`, `schema_v1.py` CHECK/index | database.md |
| Worker claim → journal → create → reconcile | B11-H01, B15-R39, B15-OBS-01 | `worker/agent.py`, `worker/journal.py`, `worker/execution.py`, `application/execution_cleanup.py` | openapi NoContainerProof/ContainerStoppedProof, worker-agent.md |
| Lớp lỗi worker ↔ API | B14-R08, B16-R21 (phân loại) | `worker/client.py`, `worker/checkpoint_flow.py` | openapi ErrorResponse |
| Runner/worker phân loại dừng | B15-R11, B15-R14, B15-R10, B14-K5, B16-DOC-01 | `worker/execution.py`, `worker/runner_control.py`, `workloads/trusted_runner.py` | workloads-checkpoints.md :178–185, trusted-runner.md |
| Chunk recognition sau fallback | B16-R21 | `application/chunk_recognition.py`, restore context, runner inference | workloads-checkpoints.md :106–130, openapi ExecutionContext |
| Checkpoint restore/storage health | B14-OBS-01, B14-R04, B15-R05 | `application/checkpoint_restore.py`, `infrastructure/artifacts/store.py` | workloads-checkpoints.md :173 |
| Maintenance mode | B15-OBS-02 | `application/execution_service.py`, `application/worker_service.py` | state-machines.md :110 |
| Khóa users/queue_submitters | B15-R32 | trigger 0010, service lock order | coordinator.md |
| API wire validation | B16-R08, B16-R10 | `api/app.py`, `application/sweep_expansion.py` | contracts.md:71, openapi |
| Coordinator chi phí | B13-OBS-01, B15-R18, B14-K1 | `coordinator/eligibility.py`, `retention.py`, `retry.py` | coordinator.md, contracts.md:92 |
| Harness/môi trường | B16-R29, ENV-01 | `tests/docker/test_real_runner.py`, `compose.yaml` | deploy docs |
| Docs | AUD-01, B16-DOC-01, B15-R02, B15-R34, B16-R04, B16-R05, B15-R33 | AGENTS.md:9, acceptance.md:7, B04/B05 evidence headers | — |
| Owner | B15-R06 (OD-1), B13-OBS-01 (OD-2), AUD-02 (OD-3) | — | 5.B để trống ⇒ NEEDS OWNER DECISION |

### Nhóm theo root cause và thứ tự mốc

- M0 baseline. M1 B13-R12. M2 B11-H01 + B15-R39 + B15-OBS-01 + B14-R08. M3 B16-R29 + ENV-01 (chẩn
  đoán sớm, soak nền). M4 B15-OBS-02; B15-R11 + B15-R14 + B15-R10; B16-R21 + B14-K5 + B16-DOC-01.
  M5 B13-OBS-01 (đo). M6 B15-R32, B14-OBS-01, B15-R05, B14-R04, B15-R33, B16-R08. M7 B15-R06, AUD-01.
  M8 B15-R18, B14-K1, B15-R02, B15-R34, B16-R04, B16-R05, B16-R10, AUD-02. M9 image + Docker P/L.
  M10 full suite, docs, closure matrix.

### Hướng sửa, test viết trước, lệnh và evidence đóng (từng finding)

| ID | Hướng sửa | Test viết trước (đỏ) | Lệnh kiểm chứng | Evidence đóng |
|---|---|---|---|---|
| B13-R12 | Migration 0021: `CREATE OR REPLACE` 6 hàm Decimal với lời gọi nội bộ qualify theo schema thật của hàm (lấy từ `pg_proc`, không hardcode), giữ inline cho hàm SQL; ghim `search_path = pg_catalog, <schema>, pg_temp` cho mọi hàm trigger/helper `nexa_*` khác; schema_v18 khai báo tập hàm | ANALYZE `fairness_ledgers` ở head 0020 lỗi; pg_dump→pg_restore không workaround lỗi; so sánh hàm cũ/mới bằng Hypothesis | PG integration + container `nexa_rem_pg` log sạch | log PG, dump/restore row count + checksum, chi phí trước/sau, upgrade/downgrade/upgrade |
| B11-H01 | Journal ghi intent bền (nonce/sequence, identity) trước Docker create; tombstone executor chặn create muộn; reconcile CLAIMED/REVOKED không container → scan theo identity + tombstone → NoContainerProof; container thật → stop → ContainerStoppedProof | 4 biến thể cửa sổ chết (worker unit + PG) | pytest worker + PG; Docker kill P/L | allocation release 1 lần, worker READY |
| B15-R39 | Mọi writer journal/resolution của attempt đi qua một khóa; lock order ghi docs | interleaving tất định bằng barrier | pytest worker; Docker P/L đếm /cleanup | không còn /cleanup kép |
| B15-OBS-01 | Xác định writer/reader gây JournalCorruption bằng dữ liệu; ghi atomic + khóa | tái hiện đọc nửa chừng | pytest + Docker P/L | không cảnh báo |
| B14-R08 | Map body 2xx không phải object ở reserve/publish thành CheckpointProtocolError | 2 test đỏ | pytest worker | — |
| B16-R29 | Oracle so UID số (`-eo pid,uid,args`) | lần fail trên L | Docker P/L | pass P/L |
| ENV-01 | Tái hiện compose `nexa_rem_*`, đo pids.current/ps; sửa theo cơ chế | chuỗi PID tăng | P + L soak ≥8 h | chuỗi PID |
| B15-OBS-02 | Cho poll/claim offer đã commit trong ADMISSION_OFF; không offer/dispatch mới | PG test đỏ | PG | — |
| B15-R11/R14/R10 | Quan sát Docker (exit/identity) để phân biệt container đã exit; frame STOPPED phân biệt startup/runtime limit; watchdog bound | unit + Docker kill | pytest + Docker P/L | reason đúng bảng |
| B16-R21 | ExecutionContext mang RecognizedChunk có thẩm quyền; runner carry-forward; WorkerApiError mang code/reason an toàn; conflict thật → INTERNAL ổn định | PG + runner test | pytest + torch/Docker L | job SUCCEEDED sau fallback |
| B14-K5 | Reason code riêng cho lỗi disk/I/O khi copy checkpoint | fault injection | pytest + Docker P | — |
| B16-DOC-01 | Sửa câu exit 65 | — (docs) | test dẫn chứng | — |
| B13-OBS-01 | Đo stall 100k (OD-2 để trống) | — | benchmark harness | NEEDS OWNER DECISION |
| B15-R32 | Bỏ vòng khóa users FOR UPDATE ↔ KEY SHARE (mode/thứ tự khóa) | 2 connection PG tất định | PG | lock order docs |
| B14-OBS-01 | Marker/identity của store để phân biệt store không khỏe với blob thiếu | 3 case filesystem | PG | — |
| B15-R05 | Server kiểm provenance checkpoint kế thừa; worker kiểm checksum | test âm | PG + worker | — |
| B14-R04 | Làm rõ :173; test hai trường hợp | PG | PG | — |
| B15-R33 | Structured log an toàn cho callback stale + kết quả reconcile | test log | pytest | — |
| B16-R08 | 400 cho lỗi wire path/query | API tests | pytest | — |
| B15-R06 | OD-1 để trống | — | — | NEEDS OWNER DECISION |
| AUD-01 | Sửa câu trạng thái | — (docs) | — | — |
| B15-R18 | Retention sweep có giới hạn | PG + EXPLAIN | PG | số đo |
| B14-K1 | Probe không khóa khi không có việc | PG race | PG | số đo |
| B15-R02, B15-R34, B16-R04, B16-R05 | Làm rõ contract; bổ sung test nếu thiếu | PG | PG | — |
| B16-R10 | 422 khi dimension có giá trị/tên trùng | unit/API | pytest | — |
| AUD-02 | OD-3 để trống | — | — | NEEDS OWNER DECISION |

## M0 — Baseline

- `git status --short --untracked-files=all`: sạch; `git log -1`: `a6c38bf`.
- Default (Mac): `PYTHONPATH=src:. uv run --no-sync pytest -q` → **1588 passed, 523 skipped**.
- Môi trường lúc bắt đầu (2026-09-28): `nexa_b10_smoke3-caddy-1` vẫn Up (unhealthy) với 31.320 PID;
  `docker exec` vào `nexa_b13_pg` thất bại `procReady not received` (VM cạn PID) → PG/Docker trên P bị
  chặn cho tới khi user dừng container đó. `ssh nexa-vps-1` timeout cổng 22 (khả năng SG "My IP").
  Cả hai đã báo user.
- PG (L, cây `git archive HEAD` = `a6c38bf`, venv `uv sync --frozen`, PostgreSQL 17.11 container riêng
  `nexa_rem_pg` bind 127.0.0.1): `pytest -q --run-postgres` → **2089 passed, 22 skipped, 3 warnings,
  704.91 s, EXIT=0** (không lỗi baseline nào trên HEAD).
- P (Docker Desktop) vẫn cạn PID suốt task (container B10 của task trước, không thuộc quyền task này
  đụng vào) ⇒ mọi Docker/PG trên P là **ENVIRONMENT BLOCKED**; PG và Docker chạy trên L.
- Môi trường L: VPS1 bị khởi động lại từ bên ngoài vài lần ngày 2026-09-28 (lần cuối 17:04 giờ VPS),
  không do task này; mỗi lần đều kiểm tra lại `nexa_rem_pg` rồi chạy tiếp. Firewall host của VPS1 chặn
  lưu lượng container → host qua `docker0`; task không được sửa ufw, và một lần chẩn đoán bằng HTTP
  server bind 0.0.0.0 đã bị classifier từ chối (Expose Local Services) nên không thử lại. Harness
  Docker B11/B14 vì vậy có chế độ loopback opt-in (xem "Test changes").

## M1 — B13-R12: hàm Decimal phụ thuộc `search_path` (L4)

**Root cause.** PostgreSQL 17 chạy biểu thức index và CHECK dưới `search_path` hạn chế khi ANALYZE,
autoanalyze, CREATE INDEX/REINDEX, và `pg_restore` nạp dữ liệu với `search_path` rỗng. Các helper
Decimal của B05 gọi nhau không qualify (`nexa_decimal_is_valid(...)`), nên `ANALYZE fairness_ledgers`
lỗi trên `ix_fairness_ledgers_score` và một `pg_dump`/`pg_restore` thuần lỗi ở CHECK Decimal. Lỗi có từ
B05, bộc lộ ở B13 khi bảng ledger có index biểu thức.

**Sửa.** Migration mới `20260928_0021_rem_decimal_search_path.py` + `schema_v18.py` (SCHEMA_GENERATION
18, không đổi bảng):

- 6 helper Decimal gọi helper khác (`QUALIFIED_DECIMAL_FUNCTIONS`) được `CREATE OR REPLACE` với thân
  B05 chỉ khác ở lời gọi nội bộ qualify theo schema thật của hàm (đọc từ `pg_proc`, không giả định
  `public`); không thêm `SET`, nên hàm SQL vẫn inline được và kết quả không đổi ⇒ index biểu thức và
  CHECK đã validate không cần REINDEX/validate lại.
- 39 hàm trigger/helper khác (`SEARCH_PATH_PINNED_FUNCTIONS`) ghim `SET search_path = pg_catalog,
  <schema>, pg_temp`; `nexa_decimal_is_valid` và `nexa_b13_eligibility_state` chỉ dùng `pg_catalog`
  (`CATALOG_ONLY_FUNCTIONS`).
- Downgrade dựng lại thân B05 byte-for-byte và reset `search_path` đã ghim; upgrade→downgrade→upgrade
  chạy được.

**Test (đỏ trước, xanh sau).**

| Test | Vai trò |
|---|---|
| `tests/integration/test_rem_b13_r12_search_path.py::test_before_0021_analyze_and_empty_search_path_checks_fail` | tái hiện ở revision 0020: ANALYZE và UPDATE dưới `search_path` rỗng lỗi `nexa_decimal_is_valid` |
| `…::test_before_0021_plain_restore_fails` | tái hiện ở 0020: `pg_dump \| pg_restore` thuần exit ≠ 0 |
| `…::test_analyze_reindex_and_empty_search_path_checks_succeed` | head: ANALYZE, REINDEX, CHECK dưới `search_path` rỗng đều qua |
| `…::test_every_nexa_function_matches_the_schema_v18_declaration` | mọi hàm `nexa_*` có đúng một lớp (qualified / pinned / catalog-only) |
| `…::test_sql_decimal_helpers_keep_their_planner_shape` | plan của helper SQL giống 0020 (inline giữ nguyên) |
| `…::test_upgrade_downgrade_upgrade_restores_b05_functions_exactly` | downgrade trả thân B05 chính xác |
| `…::test_0021_decimal_helpers_match_b05_bodies` (Hypothesis) | kết quả hàm 0021 = 0020 trên cặp Decimal sinh ngẫu nhiên |
| `…::test_plain_dump_restore_preserves_every_object_and_row` | restore không workaround: stderr rỗng, catalog tương đương (chuẩn hóa cast do PG deparse lại), row count + checksum khớp, dump lần 2 là điểm bất động |
| `…::test_restored_constraints_still_reject_invalid_values` | CHECK sau restore vẫn chặn giá trị sai |
| `tests/persistence/test_rem_b13_r12_decimal_bodies.py` (4) | render không qualifier = thân B05 byte-for-byte; bản qualify không còn lời gọi qua `search_path`; mọi helper B05 được phân loại; v18 giữ bảng v17 |

Trong lúc làm, lần chạy L đầu (`REM-B13-R12`, 3 failed/9 passed) lộ ba giả định test sai của chính
task (so tên hàm có khoảng trắng, premise "inline" sai, so text deparse); đã thiết kế lại test theo
catalog và fixed point, không nới điều kiện sản phẩm. Kết quả cuối trên L: `REM-B13-R12-green` →
**13 passed, 33.07 s, EXIT=0**; M0 PG baseline trên HEAD vẫn xanh (không có test cũ nào đổi).

**Chi phí** (`docs/evidence/raw/REM-B13-R12-cost.json`, L, 200.000 dòng ledger, 5 lần): median
2,099 s (0020) → 2,103 s (0021), tỉ lệ 1,002; `ANALYZE fairness_ledgers` 1,7 ms → 2,0 ms; upgrade
0020→0021 0,114 s, downgrade 0,107 s.

**Trạng thái: CLOSED** (tài liệu `docs/database.md` §Decimal cập nhật).

## M2 — Worker claim/journal/callback

### B11-H01: worker chết giữa claim commit và bind journal không bao giờ READY lại (L4)

**Root cause.** `_dispatch_offer` commit claim trước khi executor ghi journal. Nếu process chết trong
cửa sổ đó, server có Attempt `CLAIMED` không container identity; sau khi reaper fence lease, trang
reconciliation trả `REVOKED`/`CLAIMED`/container null. Worker incarnation mới không có record journal
và agent coi mọi claim cũ không container là "không chứng minh được" ⇒ attempt `unresolved` mãi,
`reconcile_complete` không bao giờ true, worker không READY, allocation quarantine không release.
Server đã chấp nhận `NoContainerProof` cho attempt CLAIMED chưa start (không container row,
`started_at` null, tombstone ≥ exec sequence ≥ 1) — thiếu là phía worker.

**Sửa** (`worker/agent.py`, `worker/executor.py`, `worker/journal.py`, `worker/state.py`):

- `WorkerAgent`: nhánh riêng cho dòng `REVOKED` + `CLAIMED` + không container + incarnation cũ →
  `_resolve_fenced_claim`; claim còn live thì vẫn unresolved (chặn READY).
- `DockerExecutor.resolve_fenced_claim` dưới khóa attempt: record local (nếu có) phải khớp Authority,
  nonce, resources; container đúng startup identity (nhãn managed/attempt/allocation/nonce/installation)
  bị gỡ chỉ khi bound (qua `cleanup`, stopped proof) hoặc Docker báo `created` chưa từng start; tombstone
  record (`PREPARED` → `tombstone`, `CREATE_IN_FLIGHT` không bound → `tombstone_abandoned_create`,
  không record → reconciliation tombstone `claim_state=CLAIMED`) **trước** lần scan cuối, rồi mới phát
  `NoContainerProof`. Mismatch, container đã start mà không bound, Docker không quan sát được → giữ
  unresolved (fail closed).
- Callback cleanup dùng lại pending callback bền nếu payload khớp (server dedup); sau verified cleanup,
  pending `claim`/`renew`/`failure` của attempt đó bị bỏ (`_discard_released_authority` mở rộng sang
  `claim`).
- Orphan scan: container do create trễ của process chết xuất hiện sau tombstone (record `TOMBSTONED`
  không container) → `remove_unstarted_orphan` (chỉ khi `created`, chưa start).
- Không gọi Docker trong transaction DB (toàn bộ ở worker, ngoài API).

**Test.**

| Mức | Test | Đỏ trên HEAD | Xanh |
|---|---|---|---|
| Unit (worker) | `tests/worker/test_rem_b11_h01_fenced_claim.py` (30 case: 8 cửa sổ chết — không journal, PREPARED, CREATE_IN_FLIGHT, create đã landed, CREATED, STARTED, cleanup trước/sau rm — × claim đã/chưa ack; Docker unavailable; container lạ; claim còn live; create trễ; create trễ đã start; create rơi vào giữa hai lần scan; mất response cleanup; journal identity khác; claim_state bị giới hạn; chỉ create unbound mới abandon được) | có (agent để unresolved) | 30 passed |
| PG integration | `tests/integration/test_rem_b11_h01_fenced_claim_pg.py` (API thật + reaper thật, process chết bằng `BaseException` ngay sau claim) | L `REM-B11-H01-pg-red` (cây HEAD + file test): **1 failed**, `ReconciliationResult(complete=False, … unresolved_attempts=(…))` | L `REM-B11-H01-pg`: **1 passed** |
| Docker thật | `tests/docker/test_rem_b11_h01_claim_window.py` (stack B15: API production, coordinator, container worker, image CPU; giữ khóa hàng Job để claim treo, SIGKILL worker, nhả khóa ⇒ claim commit cho process đã chết) | L, worker image HEAD `5defa941…`: **1 failed, 177,37 s**, `worker READY with a new incarnation: last=False` | L, worker image sửa `nexa/rem-worker:m2-amd64` `sha256:ad7cd16e…`: **1 passed, 186,97 s** |

Recovery timeline (L, run `docker-b11h01-m2-20260928T175749Z`, raw
`docs/evidence/raw/REM-B11-H01-docker.json`):

| Thời điểm (UTC) | Sự kiện |
|---|---|
| 17:58:57.692 | JOB_DISPATCHING; lease attempt 1 cấp, hết hạn 17:59:42.692 |
| (cửa sổ) | claim bị giữ trên khóa hàng Job; worker SIGKILL; claim commit: attempt `CLAIMED`, `started_at` null, không journal, không container, store chỉ có `claim` chưa ack |
| — | worker incarnation mới khởi động; lease chưa revoke ⇒ **không READY** (đúng) |
| 17:59:43.164 | ATTEMPT_LOST / LEASE_EXPIRED (reaper, DB time), job fence +1, allocation quarantine |
| 17:59:46.994 | ALLOCATION_RELEASED / VERIFIED_CLEANUP (NoContainerProof của incarnation mới) |
| 17:59:49.162 | RETRY_READY / BACKOFF_ELAPSED; worker READY 52,1 s sau khi kill |
| 17:59:57.239 → 18:00:50.419 | attempt 2 (incarnation khác), 5 checkpoint, RESULT_RECOGNIZED; checksum = baseline `sha256:e4f102e1…` |
| 18:00:55.989 | ALLOCATION_RELEASED / VERIFIED_CLEANUP attempt 2; counters khớp |

**Tài liệu:** `docs/worker-agent.md` (đoạn reconciliation claim cũ), `docs/worker-executor.md`
(§Stop, tombstone and cleanup), `docs/contracts/internal-interfaces.md` bước 6 reconciliation (xem
"Contract edits").

Lượt L với image final r2: xem "Toàn suite Docker L" (M10).

**Điều kiện đóng (thẻ finding).** Bốn biến thể cửa sổ (worker + PG): pass. Proof đúng loại, allocation release
đúng một lần, worker READY: pass. Docker kill trong đúng cửa sổ trên L: pass; **trên P: không chạy được**
(ENV-01). `worker-agent.md`: pass. Không suy "không có container" từ lease expiry hay một lần scan rỗng: pass.

**Trạng thái: ENVIRONMENT BLOCKED** (vòng 2; vòng 1 ghi CLOSED kèm ghi chú P). Thẻ đòi Docker kill "trên P và
L"; phần P không chạy được nên chưa đủ điều kiện CLOSED (§11). Hành động tối thiểu: owner giải phóng VM của P
(`nexa_b10_smoke3-caddy-1`), build image worker/CPU arm64 từ cây hiện tại, chạy
`tests/docker/test_rem_b11_h01_claim_window.py` trên P.

### B15-R39: replay vòng reconciliation không giữ khóa journal (L3)

**Root cause.** `_send_resolution` đọc pending operation, POST, ack, `finish` mà không khóa; vòng
reconciliation replay cùng callback song song với result thread ⇒ POST `/cleanup` hai lần hoặc
`KeyError` khi một bên `finish` trước; journal không ghi failure đã ack/cleanup đã verify nên scan sau
còn gửi lại.

**Sửa.** `_send_resolution` lấy khóa journal của attempt, đọc lại operation dưới khóa (đã xong ⇒ coi
là resolved, không POST); `_journal_resolution` ghi `failure_resolution.acknowledged` / marker
`cleanup_verified` trước khi drop operation; `_stop_orphan` và `_execution_failed` quyết định và gửi
dưới cùng khóa. Thứ tự khóa: khóa journal attempt → khóa nội bộ `PendingOperationStore`; không giữ khóa
hai attempt (ghi ở `docs/worker-agent.md`).

**Test.** `tests/worker/test_rem_b15_r39_resolution_lock.py` (4, interleaving tất định bằng
Event/Condition, không sleep): trên HEAD **3/4 đỏ** (POST lặp, `KeyError`), sau sửa 4 passed.
Docker L: đếm `/cleanup` theo attempt trong log worker của lượt L cuối, xem "Toàn suite Docker L" (M10).

**Điều kiện đóng (thẻ finding).** Test race đỏ → xanh, interleaving tất định: pass. Mọi writer
journal/resolution của một attempt qua cùng một khóa, lock order ghi ở `worker-agent.md`: pass. Docker không còn
`/cleanup` kép: L xem M10; **P không chạy được** (ENV-01).

**Trạng thái: ENVIRONMENT BLOCKED** (vòng 2; vòng 1 ghi CLOSED). Thẻ đòi "Docker P/L"; phần P không chạy được.
Hành động tối thiểu: chạy `tests/docker/test_b15_control_recovery.py` trên P với image arm64 cuối và
`NEXA_B15_WORKER_LOG_OUT`, đếm `/cleanup` theo attempt.

### B15-OBS-01: cảnh báo `JournalCorruption` (L3)

**Dữ liệu có được.** Worker log Docker P của B15 (2026-09-27, image worker trước remediation) còn trên Mac,
ngoài repo. Raw đã lọc bỏ ID/cổng: `docs/evidence/raw/REM-B15-OBS-01-p-logs.json`.
- Có **7 cảnh báo trong 4 lượt chạy** (`run7` 2, `run12` 1, `run16` 2, `run17` 2), đều là
  `worker_loop_failed operation=_result_once detail=JournalCorruption`.
- Log HEAD chỉ in tên class (`type(exc).__name__`): không có stack, message, độ dài file hay writer.
- 2 giây trước mỗi cảnh báo là các request khác nhau (poll, upload artifact, `adopt`, `start`, trang
  reconciliation…), nên không suy ra được một writer cố định.
- Mọi job bị ảnh hưởng vẫn có đúng một result được công nhận (B15 evidence :878).
- Trên L, không lượt Docker nào của task này có dòng `JournalCorruption` trong output, và lượt worker log
  đầy đủ ở M10 cũng không có (xem "Toàn suite Docker L").

**Giả thuyết cơ chế (chưa chứng minh bằng dữ liệu của sự cố).**
- `load`/`exists` đọc không khóa trong khi writer `os.replace`.
- State root của worker là bind mount của host. Trên Docker Desktop (virtiofs), replace có thể không nguyên
  tử với reader khác, nên reader thấy file vắng hoặc cũ ⇒ `JournalCorruption`.
- Trên filesystem POSIX gốc (L), reader luôn thấy inode cũ hoặc mới, khớp với việc L không có cảnh báo.
- Chưa kiểm chứng trực tiếp được vì P bị chặn (ENV-01).

**Sửa (phòng thủ, giữ lại).**
- `ExecutionJournal.exists`/`load` giữ khóa attempt như writer, nên cửa sổ trên bị đóng trên mọi filesystem.
- `JournalCorruption` mang `diagnostic` chỉ gồm `cause`, `length`, `sha256_16`, không có nội dung.
- Cảnh báo vòng lặp `worker_loop_failed` in các trường đó. Nhờ vậy lần xuất hiện sau phân biệt được record
  vắng, record rách hay record cũ.
- Corruption thật vẫn fail closed như trước.

**Test.** `tests/worker/test_rem_b15_obs01_journal_read.py` (5 test) tái hiện đúng cửa sổ bằng một replace
không nguyên tử:
- trên HEAD **5/5 đỏ**, sau sửa **5 passed**;
- có một test chứng minh marker bí mật không xuất hiện trong message, repr hay log.

**Điều kiện đóng (thẻ finding).**
1. Nguyên nhân xác định bằng dữ liệu (stack, cấu trúc file đã redact, writer, thời điểm): **không đạt**.
   Dữ liệu P chỉ có tên class và thời điểm; không lấy thêm được vì P bị chặn.
2. Nhánh (a), sửa + test tái hiện đỏ → xanh + chạy lại P và L không còn cảnh báo: phần sửa và test **pass**;
   L không có cảnh báo; **P không chạy được**.
3. Nhánh (b), chứng minh vô hại + corruption thật fail closed có chẩn đoán: phần fail closed có chẩn đoán
   **pass**; "vô hại" chỉ dựa vào kết quả B15, không phải một cơ chế đã chứng minh.

Theo thẻ finding: không xác định được cơ chế, và cảnh báo chỉ từng xảy ra trên Docker Desktop.

**Trạng thái: ENVIRONMENT BLOCKED.**

Hành động tối thiểu:
1. Owner giải phóng VM của P (container `nexa_b10_smoke3-caddy-1` của task trước; ENV-01 đã sửa trong repo).
2. Chạy `tests/docker/test_b15_control_recovery.py` trên P với image worker cuối và
   `NEXA_B15_WORKER_LOG_OUT`.
3. Đếm `JournalCorruption`. 0 cảnh báo ⇒ đủ nhánh (a). Nếu còn cảnh báo, các trường `cause`/`length`/`sha256_16`
   chỉ ra cơ chế.

### B14-R08: body 2xx không phải object ở reserve/publish (L1)

**Sửa.** `worker/checkpoint_flow.py`: `ValueError` từ body không phải object → `CheckpointProtocolError`
(chỉ fail attempt đó). **Test.** `tests/worker/test_checkpoint_flow_b14.py::
test_invalid_reserve_or_publish_answer_fails_only_that_attempt` (4 case mới) đỏ trên HEAD, xanh sau
sửa. **Trạng thái: CLOSED.**

## M3 — Harness Docker và môi trường

### B16-R29: kiểm tra cô lập UID trong `test_real_runner` phụ thuộc tên tài khoản host (L2)

**Root cause.** `docker top <c> -eo pid,user,args` phân giải UID bằng bảng tài khoản của **host**; trên
VPS1 (AWS EC2 c7i.2xlarge, Ubuntu 24.04) UID 1000/1001 trùng tài khoản host nên cột `user` không còn
là tên trong image và điều kiện sẵn sàng "runner/workload UID isolation" không bao giờ đúng. Sản phẩm
không sai; test đọc sai nguồn danh tính.

**Sửa.** `tests/docker/test_real_runner.py`: helper `_processes_by_uid` đọc `docker top -eo pid,uid,args`
(UID số, không phân giải tên); readiness đòi `{1000, 1001} ⊆ uids` và không có UID 0; test isolation
assert `trusted_runner` chỉ UID 1000, `workload_supervisor` chỉ UID 1001, không tiến trình UID 0.
Assertion không bị nới: vẫn so khớp đúng hai UID và loại UID 0.

| Lần chạy (L, loopback) | Kết quả |
|---|---|
| `r29-red` (test cũ, repo HEAD) | 1 failed, 2 passed, 72.39 s — "runner/workload UID isolation was not ready" |
| `r29-green` (`tests/docker/test_real_runner.py`) | 9 passed, 99.02 s, EXIT=0 |

P không chạy được (ENV-01, Docker Desktop cạn PID). Lượt L với image final r2: xem "Toàn suite Docker L" (M10).

**Trạng thái: ENVIRONMENT BLOCKED** (vòng 2; vòng 1 ghi CLOSED (L)). UID số và assertion chặt: pass; pass trên L:
pass; thẻ đòi "pass trên L và P", phần P không chạy được. Hành động tối thiểu: chạy
`tests/docker/test_real_runner.py` trên P với image arm64 cuối.

### ENV-01: caddy `nexa_b10_smoke3` giữ ~31.000 PID

**Tái hiện (L, project riêng).** `nexa_rem_env01_before` dựng từ `compose.yaml` của HEAD (API thay bằng
stub HTTP tĩnh không cần DB/secret, subnet riêng, không publish cổng). Lấy mẫu mỗi 30 s cgroup
`pids.current`, `cgroup.procs`/`cgroup.threads`, `ps` của host cho con của PID 1 container:

| Mốc (UTC, VPS1) | pids.current | task sống | zombie con của PID 1 | lệnh zombie |
|---|---|---|---|---|
| chẩn đoán, vài phút sau khi lên (66 → 78) | 78 | 14 | 64 | `ssl_client` |
| 18:29:10Z | 124 | 14 | 110 | `ssl_client` |
| 18:31:30Z | 152 | 14 | 138 | `ssl_client` |

**Cơ chế (đã chứng minh bằng dữ liệu).** Healthcheck `wget --spider https://…` của BusyBox fork helper
`ssl_client` cho TLS rồi thoát mà không `wait`; helper mồ côi được gán cho PID 1 của container là `caddy`
(Go), vốn không reap con không phải của mình ⇒ **mỗi lần healthcheck (5 s) rò 1 zombie**, kể cả khi
healthy (không cần upstream down). Zombie tính vào `pids.current` nhưng không hiện trong
`docker top`/`cgroup.procs`/`cgroup.threads` — đúng dấu hiệu "task không hiện thành process". 31.000 PID
≈ 43 giờ healthcheck. Không phải lỗi riêng Docker Desktop: tái hiện trên Linux (L). Nguyên nhân do cấu hình
repo (thiếu init).

**Sửa root cause.** `compose.yaml` service caddy: `init: true` (docker-init là PID 1 và reap mọi con mồ
côi). Lưới an toàn bổ sung: `pids_limit: 512`. Không đổi healthcheck, cổng hay mạng.
`docs/worker-agent.md` (Local Compose contract) ghi lý do.

**Test.** `tests/worker/test_compose_b10.py::test_caddy_reaps_its_healthcheck_helpers_and_bounds_pids`:
compose chưa sửa → 1 failed, 2 passed; sau sửa → 3 passed.

**Sau sửa (L, `nexa_rem_env01_after` dùng compose của repo):** PID 1 = `docker-init`, pids.current 15,
0 zombie ngay từ đầu. Soak ≥ 8 giờ: xem mục "ENV-01 soak" bên dưới (chuỗi PID trong
`docs/evidence/raw/REM-ENV-01-pids.csv`).

#### ENV-01 soak (L, 8 giờ 30 phút)

Soak chạy trong tmux riêng trên VPS1, ở hai project riêng không publish cổng:
- `nexa_rem_env01_before`: compose của HEAD, đối chứng;
- `nexa_rem_env01_after`: compose của repo.

Cả hai dùng cùng stub upstream. Mỗi 60 s, `sample.py` ghi cho container caddy của từng project:
- cgroup `pids.current`;
- số task sống (`cgroup.threads`);
- zombie có cha là PID 1 hoặc là process của cgroup (theo `ps` trên host), kèm tên lệnh;
- `Health.Status`/`FailingStreak`;
- `comm` của PID 1.

Không ghi payload hay tên host. Raw: [`raw/REM-ENV-01-pids.csv`](raw/REM-ENV-01-pids.csv) (570 mẫu),
mốc pha: [`raw/REM-ENV-01-phases.log`](raw/REM-ENV-01-phases.log).

| Pha (UTC) | Project | Mẫu | pids.current đầu → cuối (min–max) | Zombie đầu → cuối | Health | PID 1 |
|---|---|---|---|---|---|---|
| A 18:29–18:59, upstream sống | before | 30 | 128 → 471 (128–471) | 114 → 457 `ssl_client` | healthy 30/30 | `caddy` |
| A | after | 30 | 15 → 15 (15–15) | 0 → 0 | healthy 30/30 | `docker-init` |
| B 18:59–19:29, upstream dừng | before | 30 | 487 → 830 (487–830) | 473 → 816 `ssl_client` | unhealthy, streak 2 → 345 | `caddy` |
| B | after | 30 | 15 → 15 (15–17) | 0 → 0 | unhealthy, streak 0 → 343 | `docker-init` |
| C 19:30–23:00, upstream sống | after | 210 | 15 → 15 (15–17) | 0 → 0 | healthy 209/210 | `docker-init` |
| D 23:00–03:00, upstream dừng | after | 240 | 15 → 15 (15–21) | 0 → 0 | unhealthy, streak 0 → 2.823 | `docker-init` |

Sau pha B, project đối chứng bị hạ (`down -v`) để không để lại tải rò.

**Đối chứng (compose của HEAD).** Rò khoảng 343 zombie mỗi 30 phút, tức 1 zombie mỗi healthcheck 5 s, dù
upstream sống hay chết. Tốc độ này khớp cơ chế nêu trên và với snapshot `nexa_b10_smoke3`.

**Bản sửa.** `pids.current` bằng 15 ở đầu và cuối mọi pha, 0 zombie trong 510/510 mẫu, qua cả hai pha
healthy (4 giờ) và hai pha upstream dừng (4,5 giờ, 2.823 lần healthcheck fail liên tiếp ở pha D). Đỉnh
17–21 là process healthcheck đang chạy lúc lấy mẫu (task sống cùng lúc 15–21), không tích lũy.
`pids_limit: 512` không bao giờ bị chạm tới.

**P.** Không chạy được: Docker Desktop của máy phát triển vẫn cạn PID do chính container
`nexa_b10_smoke3-caddy-1` trước sửa (31.322 PID lúc 2026-09-29T03:11:42Z theo `docker stats`; `docker run` lỗi runc). Task không
được đụng container đó. Prompt chỉ yêu cầu thêm P "nếu Mac cho phép".

**Owner.** Sau khi dừng `nexa_b10_smoke3`, stack dựng lại từ compose hiện tại có `init: true` sẽ không rò.

**Trạng thái: CLOSED (L).**
- Cơ chế đã chứng minh và nằm ở cấu hình repo.
- Root cause đã sửa bằng `init: true`; `pids_limit` chỉ là lưới an toàn.
- Có test compose.
- Soak ≥ 8 giờ trên L với PID phẳng.
- P: ENVIRONMENT BLOCKED, do chính container trước sửa mà task không được đụng.

## M4 — Admission/runner/restore

### B15-OBS-02: offer đã commit trước `ADMISSION_OFF` bị mất và tiêu retry (L3)

**Root cause (2 lỗi cùng họ).**
1. `poll` (`ExecutionService.poll` và `WorkerService.poll`) từ chối mọi mode khác `NORMAL` ⇒ offer
   commit trước `ADMISSION_OFF` không bao giờ được claim, reaper chuyển attempt sang LOST và tiêu 1 retry.
2. Coordinator chỉ chặn `WRITE_FROZEN`: trong `ADMISSION_OFF` tick vẫn tạo offer/allocation mới
   (vi phạm SM:110 "reject … new dispatch"); cộng với lỗi 1, mọi job QUEUED (vd resume) bị dispatch rồi
   bị mất.

**Sửa.** `poll` chỉ từ chối `WRITE_FROZEN` (ở đó không còn allocation nào nên không thể có offer);
`ADMISSION_OFF` giống tiền lệ DRAINING (B15-R22). `CoordinatorService.tick`: snapshot và commit không ra
quyết định nào khi mode ≠ `NORMAL` (không offer, không tạo/hủy reservation); reaper, retry promotion,
retention sweep và ledger heartbeat vẫn chạy. Claim không cần đổi (không kiểm mode). Guard freeze giữ
nguyên (`allocations.state != 'RELEASED'` đếm cả HELD). Docs: `state-machines.md:110`, `coordinator.md`.

| Test (`tests/integration/test_rem_b15_obs02_admission_off.py`) | HEAD (`OBS02-red`) | Sau sửa |
|---|---|---|
| `test_admission_off_keeps_a_committed_offer_claimable_until_it_succeeds` — tick NORMAL tạo offer → `NORMAL→ADMISSION_OFF` qua API → freeze 409 khi còn HELD → poll/claim/start/complete/cleanup → SUCCEEDED, `retry_count=0`, không `ATTEMPT_LOST` → poll `offer=null` → freeze hợp lệ → poll trong `WRITE_FROZEN` vẫn 409 | đỏ: poll 409 "Worker is not eligible to poll for dispatch" | xanh |
| `test_admission_off_creates_no_new_offer_until_normal` — job QUEUED, 3 tick trong `ADMISSION_OFF` → không attempt/allocation, fence/retry không đổi → `NORMAL` → 1 tick tạo offer (đối chứng dương) | đỏ: tick tạo attempt trong `ADMISSION_OFF` | xanh |

Chạy L (PG17, `--run-postgres`): HEAD `2 failed, 4.58 s`; sau sửa cùng các suite liên quan
(`test_admin_workers_b15`, `test_control_b15`, `test_coordinator_b11`, `test_worker_api_b10`,
`test_policy_service`, `test_policy_races`, `test_jobs_b08`, `test_retry_b14`) **140 passed, 201.77 s,
EXIT=0**. Ruff sạch trên file đổi.

**Trạng thái: CLOSED.**

### B15-R11: runner chết sau start ACK, trước khi nhận deadline, thành `STARTUP_TIMEOUT` (L3)

**Root cause.** Sau `/start` ACK, `_apply_deadline` gửi `SET_AUTHORITY_DEADLINE` và replay tới khi runner
ACK hoặc hết budget `first_claim_send + 30 s`. Container runner đã chết (SIGKILL, exit 137) làm mọi lần
gửi/đọc lỗi, nhưng worker không phân biệt "runner đã exit" với "runner chưa sẵn sàng": nó replay đến
hết budget rồi báo `TIMEOUT/STARTUP_TIMEOUT`. `TIMEOUT` không retry ⇒ job FAILED dù đây là lỗi hạ tầng
retry được (`INFRASTRUCTURE/RUNNER_UNAVAILABLE`, workloads-checkpoints :178–185).

**Sửa** (`worker/execution.py`, `worker/runner_control.py`):

- Khi runner không nhận deadline, worker inspect **đúng container đã bind** (`_observe_container_exit`,
  state/exit code/OOMKilled theo identity) trước khi replay. Container đã exit ⇒ `_fail_exited_startup`
  phân loại ngay: frame `FAILED`/`STOPPED` đã đọc trên kết nối deadline quyết định nguyên nhân, nếu không
  thì exit status (xem B15-R14). Container còn chạy hoặc không inspect được ⇒ giữ replay; hết budget vẫn
  là `STARTUP_TIMEOUT`. Thời gian trôi không bao giờ là bằng chứng exit (§5.C :581).
- Cùng quan sát trước khi ném "claim startup budget elapsed" (container chết đúng lúc budget hết vẫn là
  `RUNNER_UNAVAILABLE`).
- `RunnerControl.send_control`: `sendall` nằm trong `try`, nên relay mất container làm lỗi gửi thành
  `RunnerControlError` như lỗi đọc (trước đó thoát ra như lỗi relay).
- Báo lỗi đi qua `_execution_failed` (dưới khóa journal, B15-R39) rồi `_retire_fenced_startup`; báo lỗi
  chưa resolve thì ném `RunnerControlError` để vòng replay gửi lại, không đoán kết quả.

**Test.**

| Mức | Test | HEAD | Sau sửa |
|---|---|---|---|
| Unit (worker) | `tests/worker/test_rem_b15_r11_start_deadline.py`: kill trước deadline (lỗi ở send/recv), OOM trước deadline, frame `STARTUP_LIMIT` giữ được, exit status của stop không ACK (3 lý do), container chết sau budget, bảng phân loại exit (9 case), `FAILURE` sau checkpoint control bị từ chối; **âm tính**: runner còn chạy không bao giờ ACK vẫn `STARTUP_TIMEOUT` | L `REM-B15-R11-R10-R14-red` (cây HEAD + file test mới): trong 28 case của 2 file đỏ, 23 failed / 5 passed; 5 case pass trên HEAD là đối chứng (exit 0/124/137/OOM và runner còn chạy vẫn timeout) | xanh |
| Docker thật | `tests/docker/test_rem_b15_r11_start_window.py` (stack B15: API production, coordinator, container worker, image CPU). Chuỗi holder khóa hàng Job (claim, tải input, `/start` đều khóa hàng Job) đóng băng worker đúng giữa commit `/start` và lúc đọc ACK; SIGKILL đúng container runner, rồi mới thả worker. Âm tính: `docker pause` runner (vẫn chạy) | L, image HEAD (CPU `6d6d0635…`, worker `5defa941…`): **1 failed, 101,48 s**, `('TIMEOUT', 'STARTUP_TIMEOUT') != ('INFRASTRUCTURE', 'RUNNER_UNAVAILABLE')` | L, image M4: **1 passed, 176,40 s** |

Chạy L sau sửa: `tests/worker` + `tests/workloads` **909 passed, 1 skipped, 72,71 s, EXIT=0**
(`REM-B15-R11-R10-R14-green`); `tests/docker/test_real_runner.py` trên image M4 **9 passed, 94 s**; lượt
Docker B11-H01 trên image M4 **1 passed, 173,7 s**. Local (Mac, không Docker/PG) toàn suite trước REM-R01:
1664 passed, 536 skipped; toàn suite cuối ở "Số đếm cuối" (M10).

Image M4 (build trên L, linux/amd64, containerd image store nên digest = image ID; source listing
`6b47f261…`): CPU `nexa/rem-cpu-iterative@sha256:1d031a0517e2dd68b6a71e2b34e32459764256d45732f649c08ca176953d8085`,
worker `nexa/rem-worker:m4-amd64` `sha256:8e2ec579c9946363e4d91e8ce7dcd9b17494d5a52008ea51bc614592cf0ef8f4`.
Image cuối cùng (r2) ở bảng image của M10.

Recovery timeline dương tính (L, run `docker-r11-green-20260928T194434Z`, raw
`docs/evidence/raw/REM-B15-R11-docker.json`). Coordinator epoch 1; cả hai attempt cùng worker incarnation
`01a0e98c-43bb…` (worker chỉ bị pause, không restart); desired state `RUNNING` suốt.

| Thời điểm (UTC) | Sự kiện | Fence / lease / allocation |
|---|---|---|
| 19:45:36.075 | JOB_DISPATCHING attempt 1 | fence 1; lease cấp, hết hạn 19:46:31.106; allocation `…2af6` HELD |
| 19:45:46.036 | ATTEMPT_STARTED (`/start` commit, worker đang pause) | — |
| (cửa sổ) | `docker kill -s KILL` runner `48b434bfd3b3`: exit 137, OOMKilled false; thả worker | — |
| 19:45:46.486 | ATTEMPT_FAILED `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, **0,203 s** sau khi thả worker | lease revoke 19:45:46.486 (trước hạn DB); job fence tăng; allocation quarantine |
| 19:45:46.645 | ALLOCATION_RELEASED / VERIFIED_CLEANUP | container stopped + verified đúng identity |
| 19:45:48.258 | RETRY_READY / BACKOFF_ELAPSED | retry 1, lý do `INFRASTRUCTURE` |
| 19:45:48.381 → 19:45:56.208 | JOB_DISPATCHING attempt 2, CHECKPOINT_FALLBACK_TO_INPUT (chưa có checkpoint), ATTEMPT_STARTED | fence 3; lease mới hết hạn 19:47:26.008 |
| 19:46:06.385 → 19:46:40.070 | 5 × CHECKPOINT_COMMITTED | — |
| 19:46:42.103 | RESULT_RECOGNIZED; checksum `sha256:5c42a0f8…` = baseline R0 cùng lượt | 1 result |
| 19:46:47.612 | ALLOCATION_RELEASED / VERIFIED_CLEANUP attempt 2 | lease revoke; counters `[0, 0]`; job SUCCEEDED, `retry_count` 1, event sequence 16 |

Âm tính (cùng run): runner `bdee807eed0e` bị `docker pause` (Running=true, Paused=true). ATTEMPT_STARTED
19:46:56.893 → ATTEMPT_FAILED `TIMEOUT/STARTUP_TIMEOUT` 19:47:26.568 (29,425 s sau khi thả worker, đúng
budget 30 s), lease revoke cùng lúc, ALLOCATION_RELEASED / VERIFIED_CLEANUP 19:47:31.343 (allocation đã
quarantine; container stopped + verified). Job FAILED, fence 2, `retry_count` 0, không RETRY_READY,
counters `[0, 0]`.

Gate (skill verifying-recovery-fencing; `gate_id → applicability → status → evidence`):

| Gate | Áp dụng | Trạng thái | Evidence |
|---|---|---|---|
| ACC-22 (crash container → retry trong budget; timeout không retry mù; typed failure; revoke/fence/quarantine trước release) | phần kill runner lúc startup | pass (L, image M4) | timeline trên: `RUNNER_UNAVAILABLE` → quarantine → VERIFIED_CLEANUP → RETRY_READY → R0; âm tính `STARTUP_TIMEOUT` không retry |
| ACC-13 (runner chỉ nhận deadline sau ACK hợp lệ trước candidate) | phần start/deadline | pass (L) cho đúng cửa sổ này | kill trước khi runner nhận deadline; budget 30 s giữ nguyên ở âm tính |
| ACC-14 (≤1 attempt được quyền/job, fence đơn điệu, 1 final result) | có | pass (L) cho scenario này | fence 1 → 3, 1 result, counters khớp |
| ACC-16 (cleanup theo identity, không release trước verify) | có | pass (L) cho scenario này | 2 × VERIFIED_CLEANUP, container stopped + verified |
| P (Docker Desktop) | có | blocked | ENV-01: VM của P cạn PID (M0) |

Các gate này không được nâng trạng thái toàn cục; đây chỉ là evidence cho phần R11 chạm tới.

**Điều kiện đóng.** (1) test đỏ → xanh: có (unit và Docker). (2) Kill sau start ACK, trước deadline →
`RUNNER_UNAVAILABLE` rồi attempt mới: **pass trên L**; **P: ENVIRONMENT BLOCKED** (ENV-01). (3) Âm tính
`STARTUP_TIMEOUT` thật vẫn là `TIMEOUT`: pass (unit + Docker L). Docs: `docs/worker-agent.md` (quan sát
Docker lúc startup).

Lượt L với image final r2: xem "Toàn suite Docker L" (M10).

**Trạng thái: ENVIRONMENT BLOCKED** (vòng 2; vòng 1 ghi CLOSED kèm ghi chú P). Thẻ đòi Docker kill "trên P và L";
phần P không chạy được. Hành động tối thiểu: chạy `tests/docker/test_rem_b15_r11_start_window.py` trên P với
image arm64 cuối.

### B15-R14 (residual): stop không ai nhận frame kết thúc bằng exit 0; watchdog stop không xác nhận treo `STOPPING` (L2)

**Root cause (2 nhánh).**
1. Runner dừng (runtime limit, lease deadline, startup limit…) khi không worker nào giữ frame `STOPPED`:
   linger 3 s rồi PID 1 exit 0. Worker map "exit 0 mà không thấy frame terminal" thành
   `INTERNAL/RUNNER_PROTOCOL_ERROR`, nên lý do dừng bị mất.
2. Watchdog gọi `enforce_deadlines`; nếu `stop_workload` không xác nhận được (supervisor không báo
   `EXIT`), watchdog chỉ đặt `STOPPING` và return: runner vẫn phục vụ control socket, không có bound,
   cho tới khi reconciliation gỡ container.

**Sửa.**
- `protocol.STOP_EXIT_CODES` (`PAUSE` 90 … `STARTUP_LIMIT` 96, tránh 0/78/124/128+n);
  `trusted_runner.exit_status`: 0 chỉ khi worker đã ACK `STOPPED`; stop không ACK hoặc fail closed sau
  khi đã yêu cầu stop ⇒ status của lý do; fail closed không có lý do ⇒ 124 (giữ nguyên). `stop_workload`
  ghi `FAILURE` làm lý do mặc định để frame và exit status cùng một lý do.
- Worker: `runner_stop_failure` dùng chung cho frame `STOPPED` và exit status; `container_exit_failure`
  map 90–96 như frame (kể cả `FAILURE` sau checkpoint control bị từ chối → `CHECKPOINT_PROTOCOL_ERROR`).
  Exit 0 không frame vẫn `RUNNER_PROTOCOL_ERROR`; 124/137 vẫn `RUNNER_UNAVAILABLE`; OOMKilled vẫn
  `CONTAINER_OOM`.
- Watchdog: `except (OSError, RuntimeError): self.fail_closed()` ⇒ PID 1 kết thúc trong bound, không
  phát `STOPPED` giả (stop chưa được xác nhận).

**Test.** `tests/workloads/test_rem_b15_r10_r14_runner.py` (nhánh 1: stop không ACK → exit status của lý
do, stop có ACK → 0, fail closed không lý do → 124, 7 status phân biệt; nhánh 2: watchdog stop không xác
nhận kết thúc < 2 s, `fatal_error`, không frame `STOPPED`, exit 94) và các case exit status trong
`tests/worker/test_rem_b15_r11_start_deadline.py`. HEAD đỏ trong `REM-B15-R11-R10-R14-red`; 2 test
guard (`…exits_zero`, `…keeps_exit_124`) đỏ trên HEAD chỉ vì chưa có `exit_status`, hành vi được giữ.
Sau sửa xanh (L 909 passed).

**Liên hệ với D6 của B16-R21 (chứng minh bằng cơ chế).** B16 evidence :329–341: cùng scenario D6
(runtime limit 90 s) lần 231234Z ra `INTERNAL/RUNNER_PROTOCOL_ERROR` "container exit 0 mà worker không
thấy frame terminal", lần 234124Z ra `TIMEOUT/RUNTIME_LIMIT_REACHED`. Attempt dừng ≈94 s = 90 s runtime
limit + grace/linger, tức runner đã tự dừng `RUNTIME_LIMIT`. Hai kết quả khác nhau đúng theo nhánh 1:
nếu kết nối worker đọc được `STOPPED{RUNTIME_LIMIT}` thì ra `RUNTIME_LIMIT_REACHED`; nếu không (kết nối
đang bận/đóng trong chu kỳ checkpoint bị từ chối) thì runner exit 0 và worker ra `RUNNER_PROTOCOL_ERROR`.
Sau sửa, trường hợp thứ hai exit 94 và map `TIMEOUT/RUNTIME_LIMIT_REACHED`, nên phân loại của D6 không
còn phụ thuộc thời điểm. Phần xung đột chunk 409 sau fallback vẫn thuộc B16-R21. D6 chạy lại trên image
r2: xem "Toàn suite Docker L" (M10).

**Trạng thái: CLOSED** (cả hai nhánh có test, lớp lỗi đúng, stop có bound; liên hệ D6 chứng minh bằng
cơ chế). Lượt L cuối với image r2: D6 không còn đi qua runtime limit, attempt 2 bị từ chối trước container
với `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE` (B16-R21); toàn lượt Docker L không có attempt nào
`RUNNER_PROTOCOL_ERROR` (xem "Toàn suite Docker L", M10).

### B15-R10 (residual): stop ở startup limit báo `RUNTIME_LIMIT` (L1)

**Root cause.** Runner dừng ở giới hạn khởi động 30 s (watchdog, đăng ký supervisor muộn, không bao giờ
nhận authority) với lý do `RUNTIME_LIMIT`; worker báo `TIMEOUT/RUNTIME_LIMIT_REACHED` thay vì
`STARTUP_TIMEOUT` mà openapi (~1339) tách riêng.

**Sửa.** 3 chỗ trong `trusted_runner.py` dùng `STARTUP_LIMIT`; `protocol.STOP_REASONS` =
`CONTROL_STOP_REASONS` ∪ {`STARTUP_LIMIT`}; `REQUEST_STOP` không nhận `STARTUP_LIMIT` (chỉ runner tự dừng
vì lý do này). Worker map `STARTUP_LIMIT` → `TIMEOUT/STARTUP_TIMEOUT`. Frame IPC: thêm giá trị enum trong
`schema_version` 1; frame của runner cũ vẫn parse; worker cũ gặp lý do mới thì fail closed (protocol
error) nên worker image phải nâng không muộn hơn runner image (cùng source). Đã cập nhật
`docs/trusted-runner.md`, `docs/contracts/internal-interfaces.md` (hàng `STOPPED`/`REQUEST_STOP`),
`docs/worker-agent.md`. Image runner/worker M4 build lại trên L; image cuối (r2, gồm PyTorch/inference) ở bảng image của M10.

**Test.** `test_startup_limit_stop_is_distinct_from_a_runtime_limit_stop`,
`test_runner_that_never_receives_authority_stops_for_its_startup_limit`,
`test_startup_limit_is_a_runner_stop_reason_but_not_a_worker_stop_control` (runner), cùng
`test_kept_startup_limit_frame_names_a_startup_timeout` (worker). Đỏ trên HEAD, xanh sau sửa.
`tests/workloads/test_runner.py` (2 chỗ) và `tests/docker/test_real_runner.py` (2 chỗ) đổi kỳ vọng
`RUNTIME_LIMIT` → `STARTUP_LIMIT` cho đúng stop startup-limit (xem "Test changes"); `test_real_runner`
9 passed trên L với image M4.

**Trạng thái: CLOSED.**

### REM-R01: reset kết nối supervisor bị coi là stop đã xác nhận (mới, phát hiện khi kiểm chứng M4)

**Mô tả.** Trên L, `test_supervisor_disconnect_after_start_sets_fatal_without_false_stopped`
(`tests/workloads/test_runner.py`) flake trên cây HEAD: `REM-R01-flake-baseline` lặp test 100 lần
**pass=98 fail=2** (lần 25 và 100, cùng assertion `reader.readline() == b"START\n"`). Cây remediation
trước khi sửa REM-R01 cũng fail 1/25 trong lần kiểm chứng M4 (output không lưu), nên không do
remediation gây ra.

**Nguyên nhân.** Trên Linux, peer đóng socket AF_UNIX khi còn dữ liệu chưa đọc (lệnh `TERM` của runner)
làm `recv` báo `ECONNRESET` thay vì EOF (macOS báo EOF). Thread đọc status bắt `OSError` và set
`_supervisor_exited` như khi nhận `EXIT`; `stop_workload` thấy event đã set nên coi stop là đã xác nhận,
phát `STOPPED` với exit code chuẩn hóa từ `None` và chuyển `STOPPED`. Đây là `STOPPED` giả: không có
báo cáo `EXIT` nào chứng minh workload đã dừng.

**Sửa.** `stop_workload`: sau khi chờ, `_supervisor_exit_code is None` ⇒ `RuntimeError("workload supervisor
ended without an exit report")`; đường fail closed hiện có xử lý tiếp (không `STOPPED` giả, PID 1 kết
thúc fail closed). `docs/trusted-runner.md`: chỉ `EXIT` xác nhận stop.

**Test.** `test_supervisor_reset_after_term_is_not_a_confirmed_stop` (fake supervisor reset
`ECONNRESET` sau `TERM`, tất định, không sleep/retry): L `REM-R01-red` trên cây trước sửa
**1 failed** (`DID NOT RAISE RuntimeError`); sau sửa xanh, `REM-R01-repeat` lặp test gốc 40 lần
**pass=40 fail=0** và `REM-R01-flake-repo` lặp thêm 100 lần **pass=100 fail=0** (cùng máy, cùng lệnh với
baseline 98/2; dữ liệu thô `raw/REM-R01-flake.json`). Điều kiện đóng: test đỏ → xanh, test gốc không còn flake trên L.

**Trạng thái: CLOSED.**

### B16-R21: attempt sau fallback tính lại chunk đã recognized và luôn nhận 409 (L3)

**Root cause.** Recognition commit cùng checkpoint, nên tập `RecognizedChunk` là prefix tới cursor của
checkpoint mới nhất. Khi claim restore một checkpoint cũ hơn (checkpoint mới nhất hỏng/không đọc được) hoặc
fallback về input, attempt mới không biết các chunk đã recognized phía sau cursor: nó tính lại chúng và
publish dưới source của chính nó. `chunk_recognition._same` so khớp toàn bộ `_RECOGNIZED_FIELDS` (artifact,
source attempt/fence) nên publish luôn `409 state_conflict`. Worker không phân biệt được 409 này
(`WorkerApiError` không mang lý do, `_ready` chỉ map 422 thành `REJECTED`) nên replay publish tới khi hết
lease/limit; D6 trên VPS1 (B16 evidence :324–341, :937–998) ra `INTERNAL/RUNNER_PROTOCOL_ERROR` hoặc
`TIMEOUT` tùy thời điểm, 0 result, recognized chunk 16 → 16. Thêm một lỗi tiềm ẩn cùng đường đi: REM-R02
bên dưới.

**Sửa (theo §5.A; không nới `_same`).**
- Claim (`application/checkpoint_restore.py`): với template chunked, `ExecutionContext.recognized_chunks` =
  dãy `RecognizedChunk` liên tục từ cursor restore (0 khi fallback) theo thứ tự chunk, mỗi mục mang range,
  artifact, size, checksum và source attempt/fence gốc. Snapshot quét ngoài transaction, kiểm lại trong
  transaction claim; blob chunk phía sau cursor không đọc/không khớp checksum hoặc dãy không liên tục ⇒
  `null`. Template không chunked không có field.
- Worker: `recognized_chunks = null` ⇒ `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE` trước khi tạo container (không
  retry mù, không recognition trùng). Danh sách được `chunk_manifest.validate_recognized` kiểm dạng đóng và
  launch spec mang nó cho runner.
- Chunk đã recognized **không bao giờ được tính lại** (vòng 2, RV03; bản vòng 1 để workload tính lại rồi
  runner so từng byte, nên chưa đạt "chỉ tính chunk chưa recognized"):
  - Server: route execution-artifact (`application/execution_artifacts.py`) đưa các mục
    `recognized_chunks` của claim vào download graph của attempt (kind `RESULT_FILE`, media type thuộc tập
    chunk, size/checksum phải bằng đúng claim). Blob đổi sau claim ⇒ 503 từ kiểm tra toàn vẹn store.
  - Worker (`worker/execution.py::_download_recognized`, `worker/adapter_dispatch.py`): tải từng file vào
    `downloads/<attempt>/recognized/`, kiểm size/checksum; sai ⇒ `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`,
    không tạo container. File được mount read-only dưới `/input/recognized/`
    (`adapter_launch.recognized_paths`), `models.py` kiểm mount khớp đúng danh sách.
  - Runner (`trusted_runner._recognized_files_are_valid`): trước khi launch workload, mọi file mount phải
    đúng size/checksum của claim, nếu không FAILED `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`; lệnh workload nhận
    `--recognized-dir /input/recognized --recognized-count N`.
  - Workload (`workloads/batch_inference.py::carry_recognized`): đọc N file tại cursor, kiểm header
    (run/model/input), chỉ số item và `records_checksum`, cộng `prediction_counts` từ các file đó rồi tính
    tiếp từ chunk chưa recognized đầu tiên. File không khớp run ⇒ lỗi nội bộ, không tính lại.
  - Runner ở lúc stage checkpoint/result dùng lại mục gốc (`chunk_manifest.carried_entry`, source
    attempt/fence gốc), không upload lại; cursor tiến qua các chunk carried. Workload vẫn ghi output cho một
    chỉ số đã recognized (bất kể byte giống hay khác) ⇒ FAILED `INTERNAL/CHUNK_OUTPUT_CONFLICT` trước mọi
    upload.
- API: `ErrorResponse.reason` (optional, safe code) — publish/complete xung đột vẫn `409 state_conflict`,
  nay kèm `reason: CHUNK_OUTPUT_CONFLICT`; `WorkerApiError.reason` chỉ giữ code khớp pattern an toàn và worker
  phân loại tất định `INTERNAL/CHUNK_OUTPUT_CONFLICT` (409 không có reason vẫn giữ replay như cũ).
- Cả hai lý do thuộc `INTERNAL` (bảng contract: "conflicting chunk/result" → không retry mù), được thêm vào
  allowlist server (`execution_cleanup._FAILURE_REASONS`) và `_FORWARDED_FAILURES` của worker.

**Test.**

| Mức | Test | Đỏ | Xanh |
|---|---|---|---|
| Unit worker + runner (harness B14: worker loop production + trusted runner thật; `RecognizingApi` áp đúng luật `chunk_recognition.recognize`) | `tests/worker/test_rem_b16_r21_carry_forward.py` (restore cũ adopt và giữ source; fallback adopt từ 0; result adopt không cần checkpoint; chu kỳ 0 batch (REM-R02); chunk tính lại khác byte ở checkpoint/result → `CHUNK_OUTPUT_CONFLICT`; 409 có reason → phân loại tất định; 409 không reason giữ replay; `recognized_chunks` hỏng → `CHUNK_OUTPUT_UNAVAILABLE` trước container; restore mới nhất không mang gì; list tối đa vừa pending store; `WorkerApiError` chỉ giữ reason an toàn), `tests/workloads/test_rem_b16_r21_recognized.py` (dạng đóng, bound 2048, `carried_entry`, launch spec) | cây HEAD (`git archive HEAD` + 2 file test mới, Mac): **42 failed, 6 passed**; 6 case pass là guard (launch spec HEAD vốn từ chối key lạ; restore mới nhất; list tối đa vừa store). Lượt đỏ đầu (23 case lúc viết): 21 failed, 2 passed | local 48 passed (trong toàn suite local 1724 passed, 551 skipped) |
| PG (L, PG17) | `tests/integration/test_rem_b16_r21_recognized_carry_forward.py`: claim sau restore cũ/fallback nêu đúng các row sau cursor với source gốc; publish carry-forward được nhận; tính lại dưới source hiện tại → 409 `reason: CHUNK_OUTPUT_CONFLICT` ở publish và complete; blob chunk thiếu/sai checksum → `null`; restore mới nhất và attempt đầu → `[]`; template không chunked không có field | L baseline (`REM-B16-R21-red-integration`): **8 failed, 1 passed** (`KeyError: 'recognized_chunks'`; case pass là template không chunked) | L `REM-B16-R21-green` cùng `test_inference_chunks_b16`, `test_checkpoint_restore_b14`, `test_checkpoint_b14`: **51 passed, 85,67 s, EXIT=0** |

Toàn suite PG trên L sau R21 (`REM-M4-R21-pg-full`, `tests --ignore=tests/docker`): 1 failed, 2231
passed, 4 skipped; failure duy nhất là REM-R03 (cô lập logger test, không liên quan R21), đã sửa và chạy lại
toàn suite (xem REM-R03).

**Docs/contract.** `openapi.yaml` (`ErrorResponse.reason`, `ExecutionContext.recognized_chunks`,
`RecognizedChunk`, mô tả 409 ở publish/complete), `workloads-checkpoints.md` (đoạn sau :130; hàng `INTERNAL`
:187), `trusted-runner.md`, `worker-agent.md`, `worker-executor.md`.

**Test vòng 2 (RV03: chứng minh chunk recognized không bị tính lại).**

| Mức | Test | Đỏ | Xanh |
|---|---|---|---|
| PG (L) | `tests/integration/test_rem_b16_r21_recognized_carry_forward.py`: `test_the_attempt_downloads_the_recognized_chunks_it_carries` (chunk sau cursor tải được 200, đúng byte; chunk đã restore 404), `test_a_recognized_chunk_blob_changed_after_claim_is_refused` (đổi byte cuối blob sau claim ⇒ 503) | `execution_artifacts.py` của HEAD: **2 failed** (`assert 404 == 200`, `assert 404 == 503`), raw `raw/REM-RV03-server-red.out` | **11 passed** (file R21) |
| Worker + runner thật (harness B14) | `tests/worker/test_rem_b16_r21_carry_forward.py` (28): restore cũ tải đúng 1 lần file recognized, runner kiểm file hợp lệ, lệnh workload kết thúc `--recognized-dir /input/recognized --recognized-count 1`, checkpoint kế tiếp chỉ có chunk mới; fallback; result; workload ghi output cho chỉ số recognized (byte giống/khác) ⇒ `CHUNK_OUTPUT_CONFLICT`; byte tải về khác ⇒ `CHUNK_OUTPUT_UNAVAILABLE` không container; file mount bị sửa/xóa ⇒ runner từ chối; mount khớp danh sách | `src/nexa` trước RV03 (lấy từ image `nexa/rem-worker:final-amd64`): **14 failed, 14 passed** (`AttributeError: … no attribute 'recognized_paths'`), raw `raw/REM-RV03-worker-red.out` | **28 passed** |
| Launch spec | `tests/workloads/test_rem_b16_r21_recognized.py::test_recognized_chunks_are_mounted_and_handed_to_the_workload` | cùng lượt đỏ ở trên | xanh |
| Workload (torch, L) | `tests/workloads/test_pytorch_workloads_b16.py`: restore cursor 2 + chunk 2..4 recognized (JSONL, Parquet): spy `chunk_bytes` chỉ thấy chunk 5..n, chỉ chunk 5..n được ghi và trùng byte bản chạy liền, summary (gồm `prediction_counts`) = bản chạy liền; fallback từ 0 tương tự; file thiếu/khác chunk/khác run/cụt ⇒ `InternalWorkloadError`, không tính chunk nào, `/output` rỗng; `main` nhận cờ và exit `EXIT_INTERNAL` khi thiếu file | image `nexa/rem-b16-torch-tests:final` + file test mới: **8 failed** (`unexpected keyword argument 'recognized_dir'`, `SystemExit 2`), raw `raw/REM-RV03-workload-red.out` | image r2: `tests/workloads` **162 passed** (`raw/REM-R2-torch.out`) |

**Điều kiện đóng.** (1) Server mang tập recognized có thẩm quyền, attempt tải và kiểm các file đó, workload
chỉ tính chunk chưa recognized, runner carry-forward với source gốc: pass (PG L, worker/runner unit, torch L;
test chứng minh không tính lại ở bảng vòng 2). (2) Không nới `_same`: pass (tính lại khác byte vẫn
409, có test). (3) Conflict thật → `INTERNAL` ổn định với safe reason cụ thể: pass. (4) Job restart-safe
hoàn tất sau fallback, không công nhận output trùng, chạy thật: pass trên L với image r2 (inference
`sha256:32e963d0…`, CPU `sha256:8ecfe240…`, worker `sha256:897a7219…`). Lượt toàn suite
`docker-r2-20260929T220051Z` fail D6b vì một lỗi của harness (REM-R07, không phải sản phẩm). Sau khi sửa
harness, `test_b16_chunked_inference_carry_forward_and_blob_loss` chạy lại riêng trên cùng image và cùng cây:
lượt `docker-r2b-20260929T231201Z`, **1 passed, 213,42 s, EXIT=0**, raw
[`raw/REM-R2b-docker-L-b16-inference.out`](raw/REM-R2b-docker-L-b16-inference.out),
[`raw/REM-R2b-B16-inference.json`](raw/REM-R2b-B16-inference.json).
- D4 (chạy liền): SUCCEEDED, 40 chunk của 1 attempt, 4 checkpoint.
- D5 (kill, resume): restore cursor 16, 16 + 24 chunk theo source attempt, summary bitwise bằng D4.
- D6 (D6a, mất blob chunk 8 phía sau cursor restore 8; checkpoint mới nhất cursor 16 hỏng):
  - job `FAILED`, 2 attempt, attempt 2 `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE` trước container (chỉ attempt 1 có
    container), `retry_count` 1;
  - 0 result, recognized 16 → 16, event `CHECKPOINT_CORRUPT` rồi `CHECKPOINT_RESTORE_SELECTED`;
  - không còn `RUNNER_PROTOCOL_ERROR`/`TIMEOUT` như B16 evidence.
- D6b (chỉ mất state checkpoint mới nhất; mọi chunk đã recognized đọc được):
  - job `SUCCEEDED`, `retry_count` 1, attempt 2 restore checkpoint cursor 8;
  - `carried_forward_after_fallback_cursor` 8: chunk 8..15 mang nguyên source attempt 1, không tính lại;
  - `chunks_per_source_attempt` = 16 (attempt 1) + 24 (attempt 2, chỉ chunk 16..39);
  - 40 chunk distinct, `coverage_exact`, `checksums_equal_baseline`, 1 result, `RESULT_RECOGNIZED`;
  - summary bitwise bằng D4 (`sha256:454b1ac6…`), không có recognition trùng.

Timeline tách trường theo skill `verifying-recovery-fencing` (r2b, ID rút gọn 12 ký tự cuối; coordinator epoch 1,
một worker incarnation `…10b83d3b6648` suốt hai scenario, mỗi scenario đúng 1 bản ghi idempotency submit):

| Scenario | Attempt | Job fence | Lease | Allocation | Container | Restore | Kết cục |
|---|---|---|---|---|---|---|---|
| D6 | 1 `…0ade1cb3d403` | 1 | `…0ade1cb3d405` revoked | `…d404` quarantine → `RELEASED/VERIFIED_CLEANUP` | `2c32ac994c73` stopped, verified | — | `FAILED INFRASTRUCTURE/RUNNER_UNAVAILABLE` (kill có chủ đích), retry 1 |
| D6 | 2 `…3e74ee62a3a6` | 3 | `…3e74ee62a3a8` revoked | `…a3a7` quarantine → `RELEASED/VERIFIED_CLEANUP` | không có | checkpoint seq 1 (seq 2 `CHECKPOINT_BLOB_MISSING`) | `FAILED INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`; job `FAILED`, fence 4, event 1–13, 0 result |
| D6b | 1 `…058be1840192` | 1 | `…0194` revoked | `…0193` quarantine → `RELEASED/VERIFIED_CLEANUP` | `0518ee08e743` stopped, verified | — | `FAILED INFRASTRUCTURE/RUNNER_UNAVAILABLE`, retry 1 |
| D6b | 2 `…a9fae7b70b30` | 3 | `…0b32` revoked | `…0b31` `RELEASED/VERIFIED_CLEANUP`, không quarantine | `d1c5fee6560d` stopped, verified | checkpoint seq 1 (seq 2 `CHECKPOINT_BLOB_MISSING`) | `SUCCEEDED`, checkpoint seq 3–4, 1 result `…eae85f3b1272`; job event 1–16, ledger 12 segment |

Event D6b: `CHECKPOINT_CORRUPT` (10) → `CHECKPOINT_RESTORE_SELECTED` (11) → `ATTEMPT_STARTED` (12) → 2 ×
`CHECKPOINT_COMMITTED` → `RESULT_RECOGNIZED` (15) → `ALLOCATION_RELEASED` (16): fallback từ checkpoint mới nhất
hỏng sang checkpoint cũ hơn có event, trước mọi fallback restart_safe; job/session giữ nguyên, attempt mới.

Case fallback về input (cursor 0) chỉ có test ở mức worker/runner và PG. Case mất state mới nhất (D6b) là
nhánh restore cũ; cả hai dùng cùng đường carry-forward. Phần P: ENVIRONMENT BLOCKED (ENV-01), thẻ ghi
"L/P" và L đã chạy.

**Trạng thái: CLOSED** (bốn điều kiện đóng pass, gồm chạy thật trên L; P không chạy vì ENV-01).

### REM-R02: chu kỳ checkpoint không có chunk mới bị coi là batch thiếu (mới, phát hiện khi làm B16-R21)

**Mô tả.** Một chu kỳ không có chunk mới không mở upload state ở worker: hai checkpoint liên tiếp không
tiến, restore rồi checkpoint ở cùng cursor, hoặc result không có chunk mới sau checkpoint cuối. Runner gửi 0
batch và đi thẳng tới `CHECKPOINT_FILES_READY`; `require_all_chunks` của worker thấy
`inference_upload.reserved_id != reserved_id` và ném "chunk batches are incomplete" ⇒
`INTERNAL/CHECKPOINT_PROTOCOL_ERROR`; `binding_set_checksum` cũng dùng `start` cũ. Lỗi có từ B16 (không do
remediation), nhưng carry-forward của R21 làm nó xảy ra thường xuyên (chunk được adopt không upload).

**Sửa.** `inference_flow.require_all_chunks`: reservation không nhận batch nào (upload chưa mở cho reserved
id đó) không bị coi là thiếu batch; số chunk restore/carry/bound so với cursor vẫn quyết định.
`binding_set_checksum` nhận reserved id: reservation không có batch không bind chunk file nào của nó. **Test**
`test_zero_batch_checkpoint_cycles_commit_without_a_restore` (không restore, hai checkpoint không tiến):
đỏ trước sửa (`1 failed, 2 passed, 20 deselected`, `assert [{'authority'…}] == []` tại
`test_inference_flow_b16.py:186` — worker báo failure), xanh sau sửa. **Trạng thái: CLOSED.**

### B16-DOC-01: câu exit 65 "before it wrote any output" sai với chunk k>0 quá cỡ (L2)

**Root cause.** `docs/trusted-runner.md` (HEAD :264–268) nói exit 65 nghĩa là workload từ chối input
"before it wrote any output". Kích thước chunk chỉ được kiểm khi chunk đó được sinh ra, nên với chunk k>0
quá upload bound, chunk 0..k-1 và inference state đã được ghi (và có thể đã bind/recognized bởi checkpoint).

**Sửa (docs).** Bullet exit 65 tách hai nhóm: tham số, Arrow metadata/safetensors header, chunk plan trên
`MAX_CHUNKS` bị từ chối trước mọi output; chunk quá bound chỉ phát hiện khi sinh ra, chunk 0..k-1 và state đã
được ghi. Hành vi không đổi (phân loại vẫn `INVALID_INPUT/INVALID_INPUT`).

**Test dẫn chứng.** `tests/workloads/test_pytorch_workloads_b16.py::test_oversized_later_chunk_is_invalid_input_after_the_earlier_chunks`
(12 item, chunk 4, chunk 2 bị độn quá `MAX_CHUNK_FILE_BYTES`): exit `EXIT_INVALID_INPUT`, `/output` có
đúng `chunk-00000000.jsonl`, `chunk-00000001.jsonl` và state với `next_chunk == 2`. Torch không có trên
Mac, nên chạy trên L trong image `nexa/b16-torch-tests:r2` (network none, read-only, mount source hiện tại
read-only và fixture dữ liệu B16 read-only): `REM-B16-DOC-01-torch` `tests/workloads` **154 passed, 15,98 s,
EXIT=0**. Test mô tả hành vi hiện có nên pass cả trước và sau (chỉ câu chữ sai).

**Trạng thái: CLOSED** (câu chữ khớp hành vi, dẫn test).

### B14-K5: hết chỗ/lỗi I/O khi runner copy checkpoint bị báo `RUNNER_UNAVAILABLE` và tốn retry (L2)

**Root cause.** Bản copy staging checkpoint dùng chung tmpfs `/output` có bound của attempt với workload.
`OSError` (ENOSPC/EDQUOT/EIO) khi runner đọc snapshot, copy file, ghi chunk-output manifest của checkpoint
đang mở hoặc ghi checkpoint manifest thoát khỏi `apply_control` (hoặc watchdog khi stage request đang chờ):
runner fail closed exit 124 ⇒ worker báo `INFRASTRUCTURE/RUNNER_UNAVAILABLE` retry được. Lỗi tất định
("disk đầy") tiêu hết retry budget (B14 evidence ~853–855).

**Sửa.**
- `trusted_runner`: `_checkpoint_storage()` bọc đúng các control stage checkpoint (`REQUEST_CHECKPOINT`,
  `FINALIZE_CHECKPOINT_MANIFEST`, `BIND` purpose `CHECKPOINT`, `BIND` purpose `CHUNK_OUTPUT` của checkpoint
  đang mở) và lần stage request đang chờ từ watchdog. ENOSPC/EDQUOT/EIO (trực tiếp hoặc là `__cause__`) ⇒
  `CheckpointStorageFailed`: frame FAILED `INTERNAL/CHECKPOINT_STORAGE_FAILED`, control bị từ chối
  (`INVALID`), stop `FAILURE`. File dở không bao giờ được announce (`CHECKPOINT_FILES_READY`/
  `CHECKPOINT_READY` không phát). Mất frame FAILED thì exit status `STOP_EXIT_CODES["FAILURE"]` vẫn ra lớp
  `INTERNAL`, không bao giờ `RUNNER_UNAVAILABLE`.
- `OSError` khác (vd EACCES) giữ đường fail closed exit 124 như cũ. Đường result không đổi (ngoài phạm vi,
  ghi residual).
- Worker forward lý do (`_FORWARDED_FAILURES`); server allowlist chỉ nhận dưới `INTERNAL`
  (`execution_cleanup._FAILURE_REASONS`), không retry theo bảng contract; callback failure kết thúc
  reservation checkpoint đang mở (`ABANDONED`).
- Mã mới `CHECKPOINT_STORAGE_FAILED` (additive): allowlist có sẵn không có mã nào đúng nghĩa
  (`CHECKPOINT_PROTOCOL_ERROR` là vi phạm giao thức, `RUNNER_UNAVAILABLE` retry được). `reason_code` trong
  openapi chỉ có pattern (openapi.yaml ~2218), không có enum ⇒ không sửa openapi.

**Test (fault injection).**

| Mức | Test | Đỏ | Xanh |
|---|---|---|---|
| Runner | `tests/workloads/test_rem_b14_k5_checkpoint_storage.py`: copy staging ENOSPC/EDQUOT/EIO (không file dở, FAILED còn sau reload), đọc state EIO, ghi manifest ENOSPC trước `CHECKPOINT_READY`, request đang chờ stage từ watchdog, checkpoint training hỏng ở file thứ hai không announce; **âm tính** EACCES vẫn là `PermissionError`, không FAILED | local cây trước sửa: 7 failed, 1 passed (case âm tính) | xanh |
| Worker (harness B14/B16) | `tests/worker/test_rem_b14_k5_checkpoint_storage.py`: checkpoint CPU hết chỗ → 1 reserve, failure `[INTERNAL/CHECKPOINT_STORAGE_FAILED]`, cleanup đúng container, không publish/upload; chunk manifest inference hết chỗ; frame được forward và exit status `FAILURE` → `INTERNAL` | local: 3 failed | xanh |
| PG (L) | `tests/integration/test_rem_b14_k5_storage_failure_pg.py`: job restart-safe có checkpoint committed + reservation chu kỳ hai; `INTERNAL/CHECKPOINT_STORAGE_FAILED` → reservation `ABANDONED`, job `FAILED`, `retry_count` 0, 0 retry schedule, counters `(0, 0)`; đối chứng `INFRASTRUCTURE/RUNNER_UNAVAILABLE` → `RETRY_WAIT`, 1 retry; lý do dưới `INFRASTRUCTURE`/`TIMEOUT`/`INVALID_INPUT` → 422 | L cây trước sửa `REM-B14-K5-red` (3 file): **12 failed, 4 passed** (422 "Unknown failure reason", `OSError` thoát ra); 4 case pass: đối chứng `RUNNER_UNAVAILABLE`, 2/3 case 422 và case âm tính EACCES | L `REM-B14-K5-green` (3 file + `test_control_b15`, `test_checkpoint_b14`, `test_retry_b14`, `test_rem_b15_obs01_journal_read`): **110 passed, 141,90 s, EXIT=0** |

Local toàn suite (Mac, không Docker/PG) sau K5: **1724 passed, 551 skipped**.

**Docs/contract.** `workloads-checkpoints.md:187` (hàng `INTERNAL`), `trusted-runner.md` (đoạn "Checkpoint
storage failure (B14-K5)"), `worker-agent.md` (danh sách forward).

**Điều kiện đóng.** Safe reason riêng: pass. Lớp retry đúng bảng contract (`INTERNAL`, không retry mù; lý do
không nhận dưới lớp khác): pass (PG L). Fault injection đỏ → xanh: pass. Docker với `/output` nhỏ bị đầy:
pass trên L với image r2 (kịch bản K5 của lượt Docker L cuối, `raw/REM-R2-k5.json`): `/output` 16 MiB đầy
(`free_blocks` 0, 16 773 120 byte), attempt `FAILED` `INTERNAL/CHECKPOINT_STORAGE_FAILED`, `retry_count` 0,
không checkpoint/result, allocation release `VERIFIED_CLEANUP`, 61,6 s từ lúc đầy tới terminal. Thẻ ghi
Docker P "nếu khả thi": P ENVIRONMENT BLOCKED (ENV-01) nên không chạy, không phải điều kiện bắt buộc.

**Trạng thái: CLOSED.**

### REM-R03: test cảnh báo OBS-01 phụ thuộc thứ tự vì Alembic tắt logger (mới, phát hiện khi chạy toàn suite PG)

**Mô tả.** `REM-M4-R21-pg-full` (L, `tests --ignore=tests/docker`): **1 failed, 2231 passed, 4 skipped**,
EXIT=1; failure là `tests/worker/test_rem_b15_obs01_journal_read.py::test_the_loop_warning_names_the_cause_without_record_content`
(caplog rỗng). Chạy riêng thì pass.

**Nguyên nhân.** `migrations/env.py` gọi `fileConfig(alembic.ini)` (`disable_existing_loggers=True`) khi một
test migration PG chạy trước, tắt logger `nexa.worker.agent` đã tồn tại. Test REM mới không bật lại logger
như pattern có sẵn của repo (`tests/worker/test_agent_b10.py:97`). Chỉ là cô lập test; không đổi sản phẩm.

**Sửa.** `monkeypatch.setattr(logging.getLogger("nexa.worker.agent"), "disabled", False)` trong test.
**Tái hiện tất định** (plugin pytest tạm ngoài repo, gọi `fileConfig` sau collection): test cũ **1 failed,
4 passed**; sau sửa **5 passed**. **Trạng thái: CLOSED.**

### M4 tổng hợp: toàn suite PG sau M4

`REM-M4-pg-full-2` (L, PG17, cây làm việc sau toàn bộ sửa M4 gồm B16-R21, REM-R02, B16-DOC-01, B14-K5,
REM-R03; `tests --ignore=tests/docker`): **2248 passed, 4 skipped, 825,08 s, EXIT=0**. So với
`REM-M4-R21-pg-full` (1 failed, 2231 passed, 4 skipped): failure REM-R03 đã hết; tổng số test chạy tăng 16
(test thêm ở M4 sau lượt đó); số skip không đổi.

## M5 — B13-OBS-01: dispatch đứng sau thao tác admin-rate ở 100k (L3, đo; tiêu chí theo OD-2)

**Sự thật đã kiểm.** Chọn event theo `last_processed_at NULLS FIRST, event_id`
(`coordinator/eligibility.py:63-67`), mỗi tick replay một trang 64 Job. Tenant còn event pending bị loại
khỏi dispatch. Vì vậy các tenant replay xen kẽ, trang một lượt, và tenant nào cũng xong gần cuối toàn bộ lượt
replay. Với 100 tenant × 1.000 Job, mỗi tenant cần ⌈1000/64⌉ = 16 trang, tổng 1.600 tick (finding ước 1.563
theo 100.000/64). Tenant đầu tiên xong sau khoảng 15 × 100 + 1 tick.

**Harness (thêm vào harness có sẵn, không đổi sản phẩm).** `benchmarks/b13/queue_microbench.py` có thêm:
- chế độ `--measure-eligibility-stall capability|tenant-toggle` và `--stall-max-ticks`;
- `--evidence-layer P|L`;
- provenance `source_tree_sha256` khi chạy trên bản sao cây không có `.git`.

Trước khi đổi, harness rút cạn event cũ. Sau đó nó chạy tick đối chứng tới khi có Dispatch, áp một thay đổi
qua trigger PostgreSQL thật, rồi chạy tick liên tiếp với heartbeat, account và release như `_measure`.
Ước lượng production tính mỗi tick bằng `max(250 ms, tick đo)`, vì vòng production khởi tick nhiều nhất mỗi
250 ms. Test harness PG: `tests/benchmarks/test_b13_queue_microbench.py`, 2 test mới, fixture 4 × 200.
`REM-B13-OBS-01-harness` (L): **9 passed, EXIT=0**.

**Đo 100k trên L** (VPS1, PostgreSQL 17 `server_version_num` 170011, container riêng tại 127.0.0.1, Linux 7.0.0-1013-aws, glibc 2.39).
- Raw: `docs/evidence/raw/REM-B13-OBS-01-stall.json`, gồm lệnh, JSON kết quả và output đã lọc.
- Cây đo: `source_tree_sha256` `959ed2cb…3060`, migration head 0021. Các sửa sau đó (0022, store identity,
  `api/app.py`, `worker/agent.py`) không chạm file coordinator/scheduler nào.

| Đo | capability (thêm adapter, mọi Job vẫn possible) | tenant-toggle (tắt rồi bật 1 tenant) |
|---|---|---|
| Lệnh | `run_bench.sh remobs01 REM-B13-OBS-01-capability --seed --tenants 100 --jobs-per-tenant 1000 --measure-eligibility-stall capability --evidence-layer L …` | cùng DB, không `--seed`, `--measure-eligibility-stall tenant-toggle` |
| Job chờ / queue head | 100.000 / 100 | 99.949 / 100 |
| Event tạo / event của phần bị ảnh hưởng | 100 / 100 (mọi tenant) | 2 / 2 (một tenant) |
| Tick tới Dispatch đầu tiên (bất kỳ tenant) | **1.502** | 2 |
| Tick tới Dispatch đầu của phần bị ảnh hưởng | 1.502 | 198 (xem ghi chú) |
| Tick tới replay xong | 1.600 | **32** |
| Dispatch trước khi replay xong | 50 | 16 |
| Quyết định | `NoDecision:no_eligible_candidate` 1.500, `CreateReservation:eligible_wait_threshold` 50, `Dispatch` 50 | `CreateReservation:eligible_wait_threshold` 99, `Dispatch` 99 |
| tick ms median / p95 / max | 109,9 / 574,0 / 1.349,5 | 1.517,9 / 1.595,4 / 1.651,0 |
| Wall harness | 311,8 s | 307,1 s |
| Ước lượng production tới Dispatch của phần bị ảnh hưởng | **≈ 375,8 s (6,3 phút)** | ≈ 294,0 s (xem ghi chú) |
| Ước lượng production tới replay xong | ≈ 457,1 s (7,6 phút) | **≈ 42,9 s** |
| EXIT | 0 | 0 |

**Ghi chú tenant-toggle.** Các tenant khác vẫn dispatch từ tick 2. Tenant bị tắt/bật hết pending ở tick 32.
Dispatch đầu của nó là lượt thứ 99 trong 99 Dispatch, mỗi lượt một tenant khác nhau: đó là thứ tự weighted
fair-share, không phải stall. Harness không tách được hai phần này, nên con số stall có thể gán cho eligibility
là **32 tick ≈ 42,9 s**. Tick ≈ 1,5 s ở đây vì tick nào cũng đánh giá candidate trên 100k Job. Chi phí tick ở
50k/100k không phải gate (ngoài phạm vi); ghi lại để đối chiếu.

**Gate** (`benchmarking-scheduler-fairness`: gate_id → applicability → status → evidence):

| gate_id | applicability | status | evidence |
|---|---|---|---|
| OBS-01-measure-100k-L: đo stall 100k bằng harness có sẵn | áp dụng (bắt buộc bất kể OD-2) | pass | `REM-B13-OBS-01-capability` và `-tenant-toggle`, EXIT=0; raw JSON ở trên |
| OBS-01-measure-100k-P (Mac) | tùy chọn (prompt: "PG; thêm L nếu khả thi"); L đã đo trên PostgreSQL | not-run | — |
| OD-2A: ghi số đo và giới hạn vào tài liệu | chỉ áp dụng nếu owner chọn A | specified | đã chuẩn bị: `docs/coordinator.md` đoạn "Measured limit (B13-OBS-01 …)" |
| OD-2B: tenant bị ảnh hưởng dispatch lại được trong ≤ 60 s ở 100k trên L (Ask đề xuất) | chưa áp dụng: OD-2 để trống | specified | Đối chiếu tham khảo: capability ≈ 375,8 s tới Dispatch đầu (≈ 6,3 × bound), toggle replay ≈ 42,9 s |
| Simulator/property (D) | không áp dụng (không có policy claim mới) | — | — |

**Chuẩn bị cho OD-2B (chỉ phân tích, chưa sửa).** Có hai hướng giữ fairness/quota/aging/reservation, cần
owner chọn bound trước khi thiết kế:
1. Replay xong từng tenant trước khi sang tenant kế. Tenant đầu dispatch sau 16 tick, nhưng tenant cuối vẫn
   chờ 1.600 tick.
2. Replay theo tập hợp, đánh giá "possible" theo nhóm (template, adapter, kích thước) thay vì từng Job,
   một câu lệnh cho mỗi tenant. Cần benchmark trước/sau.

Chỉ hướng 2 có khả năng đạt ≤ 60 s cho mọi tenant ở 100k.

**Trạng thái: NEEDS OWNER DECISION (OD-2).** Đã đo và ghi evidence. Tiêu chí đóng phụ thuộc owner chọn A
(chấp nhận limit; tài liệu đã sẵn) hay B với bound; với bound 60 s, capability hiện gấp khoảng 6 lần.

## M6 — Khóa, lưu trữ, wire status, provenance

Mọi red chạy trên cây baseline (= HEAD `a6c38bf`) ở L (VPS1), chép test mới vào rồi xóa sau khi chạy. Green
chạy trên cây repo. Toàn suite sau B14-OBS-01, B15-R33, B16-R08, B15-R32 (trước B15-R05):
`REM-M6-pg-full-a` (L, `tests --ignore=tests/docker`): **2285 passed, 4 skipped, EXIT=0** (M4: 2248 passed).

### B15-R32: khóa principal `FOR UPDATE` chặn KEY SHARE của FK `queue_submitters` (L2)

**Root cause.** Mọi request transaction revalidate principal bằng `FOR UPDATE` trên hàng credential và
`users`, rồi mới khóa policy, counter, job. Coordinator giữ policy, counter, job khi promote retry. UPDATE job
thành queued kích trigger B13 `queue_submitters`, và FK của INSERT đó lấy `KEY SHARE` trên hàng `users` của
submitter. `KEY SHARE` xung đột với `FOR UPDATE`, nên có vòng `users` → job → `users`: deadlock, hoặc
`lock_timeout` 1 s của coordinator.

**Sửa (mode khóa trong code, không đổi schema, theo 5.C).** Mọi khóa hàng credential và `users` trong
`identity_service.py` thành `FOR NO KEY UPDATE` (`with_for_update(key_share=True)`, 7 chỗ). Không request hay
đường admin nào đổi `users.user_id`. `FOR NO KEY UPDATE` vẫn loại trừ request khác của cùng principal và mọi
UPDATE user, nhưng không chặn `KEY SHARE` của FK. Lock order ghi ở `docs/coordinator.md`
("Lock order against requests (B15-R32)").

**Test** `tests/integration/test_rem_b15_r32_submitter_lock.py` (PG, hai connection, tất định, không sleep):
- `test_retry_promotion_does_not_wait_for_a_request_holding_its_submitter[BROWSER|CLI]`: connection 1 giữ khóa
  principal như request (`revalidate_principal` thật); connection 2 chạy `promote_retries` với `lock_timeout`
  1 s của coordinator. Kiểm Job QUEUED, `retry_count` 1, `job_fence` 2, đúng một hàng `queue_submitters`,
  counter `outstanding`/`active_attempts` đúng, request commit được sau đó.
- `test_principal_lock_still_excludes_user_updates_and_other_requests[BROWSER|CLI]`: khóa mới vẫn chặn UPDATE
  user và request thứ hai của cùng principal (`55P03` với `lock_timeout` 200 ms), rồi request đó chạy được.
  Test không dùng sleep: chờ bằng khóa PostgreSQL.

| Chạy | Kết quả |
|---|---|
| `REM-B15-R32-red` (baseline) | 2 failed, 2 passed, EXIT=1. `LockNotAvailable: canceling statement due to lock timeout`, `while locking tuple … in relation "users"` trong `nexa_b13_refresh_queue_submitter` |
| `REM-B15-R32-green` (repo; test mới + `test_callback_lock_order_b15`, `test_identity_service`, `test_admin_identity`, `test_admin_races`, `test_policy_races`) | 40 passed, EXIT=0. Lần chạy đầu treo vì cây VPS1 chưa sync bản sửa; sync lại rồi chạy lại |
| `REM-M6-pg-full-a` | 2285 passed, 4 skipped, EXIT=0 |

Test cũ đổi (không nới): `test_admin_races.py` ×2 nhận diện câu SQL khóa `users` bằng `for no key update`;
`test_identity_service.py::_lock_and_expire` nhận cả hai mode; `test_callback_lock_order_b15.py` sửa docstring
và comment (helper vẫn giữ `FOR UPDATE`, mạnh hơn request).

**Trạng thái: CLOSED.**

### B14-OBS-01: blob thiếu trong store sai hoặc tái tạo bị đánh CORRUPT vĩnh viễn (L2)

**Kiểm chứng.** Đúng như finding. Mất cả `committed/` thì `lstat` lỗi → 503. Nhưng root có `committed/`
(volume sai, hoặc API khởi động trên mount rỗng và store tự tạo lại cây) thì mọi blob là `not_found` →
`CHECKPOINT_BLOB_MISSING` → mọi checkpoint CORRUPT một chiều.

**Sửa (tín hiệu tất định, 5.C).**
- Root store có file `store-identity` (mode 0600, `nexa-artifact-store v1 <uuid>`), tạo một lần qua temp +
  `link`, không bao giờ thay.
- PostgreSQL ghi cùng UUID trong singleton insert-only `artifact_store_identity`, migration `20260929_0022`
  (additive, downgrade chạy được).
- Khởi động: API chỉ bind store khi identity ở root bằng hàng DB. Lần đầu (kể cả nâng lên 0022) API ghi identity
  đang có ở root hoặc tạo mới. API từ chối khi DB đã tham chiếu blob committed mà `committed/` rỗng.
- `open`/`inspect` chỉ trả `not_found` khi store đã bind, identity ở root còn khớp, `committed/` là thư mục
  thật và blob vắng. Mọi trường hợp khác là `storage_unavailable` (503 tạm thời, không đánh dấu).
- Readiness probe cho worker READY cũng đòi identity đã xác minh.
- Code: `application/storage_identity.py`, `infrastructure/artifacts/store.py`, `api/app.py`,
  `schema_v19.py`. Docs: `docs/artifacts.md` "Store identity (B14-OBS-01)", `docs/database.md`,
  `workloads-checkpoints.md` bước 3 restore.

**Test (filesystem thật, không mock).**
- `tests/artifacts/test_rem_b14_obs01_store_identity.py` (5 test, 13 case): blob thiếu đơn lẻ trong store đã
  bind là `not_found`; store chưa bind; root thiếu; `committed/` thiếu; cây mới không identity; identity store
  khác; identity symlink; identity rác; identity tạo một lần; báo nội dung committed cho adoption.
- `tests/integration/test_rem_b14_obs01_store_identity_pg.py` (PG, 5 test):
  - claim trên store không xác minh được (`_root_missing`, `_committed_missing`, `_fresh_tree`,
    `_foreign_store`) → 503, không checkpoint nào bị đánh dấu, restore được sau khi trả volume;
  - blob thiếu trong store đã xác minh → CORRUPT + fallback;
  - API khởi động lại trên volume rỗng không bind, không đánh dấu;
  - lần khởi động đầu sau nâng cấp chỉ adopt store có nội dung;
  - hàng identity insert-only.

| Chạy | Kết quả |
|---|---|
| `REM-M6-red-obs01-r33` (baseline, PG) | 6 failed, 3 passed, EXIT=1. 5 OBS-01 failed: `_fresh_tree`, `_foreign_store` (`assert 200 == 503`: claim chấp nhận và đánh dấu), khởi động lại trên volume rỗng, adoption sau nâng cấp, bảng identity insert-only (chưa có). 3 pass trên HEAD vì hành vi đó đã đúng: `_root_missing`, `_committed_missing`, blob thiếu trong store khỏe → CORRUPT |
| `REM-M6-red-nonpg` (baseline) | 10 case artifacts failed: `FilesystemArtifactStore` chưa có `create_identity`/`read_identity`/`has_committed_blobs` (chưa có cơ chế) |
| `REM-M6-pg-full-a` (repo) | 2285 passed, 4 skipped, EXIT=0 |

Test cũ đổi: `tests/artifacts/test_store.py` ×2 bind identity trước khi mong `not_found`/readiness (hợp đồng
mới: chỉ store đã bind mới chứng minh blob thiếu); `tests/persistence/test_rem_b13_r12_decimal_bodies.py`
danh sách bảng sau v18 thêm `artifact_store_identity`.

**Còn lại (ghi, không chặn đóng):** upload vào store chưa bind bị từ chối 503 như mọi I/O; không có đường
tự tái khởi tạo khi storage mất thật (ngoài failure scope, `docs/artifacts.md`); metric là B19.
**Trạng thái: CLOSED.**

### B15-R33: callback stale bị từ chối và kết quả reconcile không quan sát được (L2, 5.A)

**Sửa (5.A: không thêm event type).** `409 stale_authority` và kết quả reconcile không commit gì, nên
`RECOVERY_EVENT_TYPES` giữ nguyên danh sách đóng. Thêm một dòng JSON có giới hạn mỗi kết quả:
- API (`api/app.py` `_log_rejected_callback`), WARNING, `{"event":"worker_callback_rejected", …}`: method,
  route template, status, code, message cố định, request ID, `worker_id`/`attempt_id` của path. Không
  credential, body hay authority.
- Worker (`worker/agent.py`), INFO, `{"event":"worker_reconcile","complete","items_seen","adopted","unresolved"}`:
  scan không đầy đủ, scan có adopt, và scan đầy đủ đầu tiên sau khởi động hoặc sau scan không đầy đủ. Scan
  khỏe đều đặn im lặng. Chỉ có số đếm.
- Docs: `docs/worker-agent.md` (nơi quan sát). Chưa có hạ tầng metrics (không có Prometheus trong `src`),
  nên counter thuộc B19 (PLAN §B19).

**Test.** `tests/integration/test_rem_b15_r33_rejection_log.py` (PG): đúng một dòng, đủ trường, không chứa
token/body. `tests/worker/test_rem_b15_r33_reconcile_log.py`: các nhánh log và im lặng.

| Chạy | Kết quả |
|---|---|
| `REM-M6-red-obs01-r33` (baseline) | R33 PG: `assert [] == [{'event': 'worker_callback_rejected', …}]` |
| `REM-M6-red-nonpg` (baseline) | 2 test worker failed: `assert [] == [{'event': 'worker_reconcile', …}]` |
| `REM-M6-pg-full-a` (repo) | 2285 passed, 4 skipped, EXIT=0 |

Quyết định: mức WARNING cho callback bị từ chối (tín hiệu vận hành, không phải lỗi server).
**Trạng thái: CLOSED** (phần metric ghi thuộc B19, đúng điều kiện 5.A).

### B16-R08: path/query sai định dạng trả 422 thay vì 400 (L2, 5.A, BREAKING)

**Sửa.** Handler `RequestValidationError` toàn app trả `400 validation_failed` khi lỗi nằm ở path hoặc query
(kiểu/pattern/format), hoặc là JSON hỏng/unknown field. Giá trị đúng định dạng nhưng vi phạm validation vẫn
422. Lỗi header ngoài phạm vi 5.A (chỉ nêu path và query), giữ 422 như các kiểm tra header của app.
Body parser đã trả 400 đúng từ trước.
- Docs: `contracts.md:71` liệt kê path/query; OpenAPI `BadRequest` thêm "path or query parameter of the wrong
  type, pattern or format". Kiểm bằng Ruby YAML: 69 operation có input path/query/body, 0 thiếu 400.
- CLI ánh xạ theo `code` (`validation_failed` → exit 9), không theo status; web chưa có code xử lý 422
  (`grep` trong `web/src` rỗng). `docs/cli.md` không đổi.

**Test.** `tests/api/test_rem_b16_r08_wire_status.py`: 8 case (template id sai pattern, job id không phải
UUID, UUID v4, `page_size` 0/101/"ten", cursor hỏng, state lạ) → 400 `validation_failed`, `X-Request-Id`
bằng `request_id` của body. Case header hỏng một mình vẫn 422.

| Chạy | Kết quả |
|---|---|
| `REM-M6-red-nonpg` (baseline) | 8 failed `assert 422 == 400`; case header pass |
| `REM-M6-pg-full-a` (repo) | 2285 passed, 4 skipped, EXIT=0 |

Test cũ đổi: 7 assertion PG thành 400 cho path/query sai định dạng. Ba chỗ `== 422` (`test_templates_b16`,
`test_checkpoint_b14`, `test_sweep_b16`) và bốn chỗ `in (400, 422)` được siết thành `== 400`
(`test_control_b15`, `test_admin_workers_b15` ×3). `tests/cli/test_control_commands_b15.py` thêm case 400 → exit 9.
**Breaking:** client dựa vào 422 cho path/query sai định dạng nhận 400 (cùng `code`).
**Trạng thái: CLOSED.**

### B15-R05: server không chứng minh nguồn của checkpoint kế thừa khi manual retry (L2, 5.A)

**Root cause.** Retry với `checkpoint_id` chỉ kiểm cùng tenant, cùng `spec_checksum`, COMMITTED và scan
hợp lệ; không kiểm chủ checkpoint thuộc chuỗi `retry_of_job_id`. Claim tin mọi hàng `checkpoint_references`
của Job đích, bất kể `reason` và chủ. Worker không tự dẫn ra session của Job nguồn nên bỏ qua kiểm session.
Hệ quả, đo trên HEAD:
- Checkpoint cùng spec của một Job không liên quan được tham chiếu và restore.
- Một reference sai (spec hoặc image khác) làm claim đánh CORRUPT một chiều checkpoint của Job khác.

**Sửa (server là bên chứng minh).**
- `job_recovery.retry_lineage`: CTE đệ quy theo `retry_of_job_id` trong tenant.
- `job_control._lock_retry_checkpoint`: chủ checkpoint phải thuộc lineage của Job nguồn, ngoài các điều kiện
  cũ. Sai → `422 infeasible_request` như trước, không ghi gì.
- `checkpoint_restore._proven_inherited`: ở mỗi claim, checkpoint kế thừa chỉ được chứng minh khi có đủ:
  - cùng tenant;
  - reference `MANUAL_RETRY` tới đúng Job;
  - chủ là tổ tiên trên chuỗi retry;
  - spec checksum, input, model, template version, adapter và image digest của chủ bằng của Job đích.
- Ứng viên không chứng minh được là `INCOMPATIBLE`/`CHECKPOINT_PROVENANCE_MISMATCH`. Không đọc blob, không
  đánh CORRUPT (không phải checkpoint của Job này); scan đi tiếp tới fallback restart_safe hoặc
  `RESTORE_UNAVAILABLE`.
- Worker giữ nguyên: kiểm record, manifest, checksum file và byte như checkpoint của chính Job. Chỉ session
  nguồn lấy từ chứng minh của server.
- Docs (loại c, §5.A): `workloads-checkpoints.md` đoạn chứng minh nguồn và dòng Tenant/job/session của bảng
  compatibility; `openapi.yaml` mô tả `RetryRequest.checkpoint_id`; `database.md` CheckpointReference;
  guard manual retry trong `state-machines.md`.

**Test.**
- `tests/integration/test_rem_b15_r05_inherited_provenance.py` (PG):
  - retry tham chiếu checkpoint cùng spec của Job ngoài chuỗi → 422, không Job/reference mới, counter
    nguyên; retry thường vẫn 202;
  - retry của retry kế thừa và restore checkpoint tổ tiên (dương tính);
  - claim với reference sai, 4 case: Job không liên quan, spec khác, template/image khác, reason khác
    `MANUAL_RETRY`. Kết quả: không restore, event đúng `[INCOMPATIBLE/PROVENANCE_MISMATCH, FALLBACK_TO_INPUT]`,
    không đánh dấu, checkpoint vẫn thuộc Job nguồn;
  - byte của checkpoint kế thừa hợp lệ bị đổi → CORRUPT `CHECKSUM_MISMATCH` + fallback;
  - reference khác tenant không lưu được (FK 23503).
- `tests/worker/test_rem_b15_r05_inherited_checksum.py`: manifest checksum sai, file checksum sai → từ chối;
  byte sai → `INCOMPATIBLE`/`CHECKPOINT_RESTORE_UNAVAILABLE`, `NO_CONTAINER`, 0 container tạo.
- `tests/application/test_checkpoint_validation_b16.py`: fixture ứng viên thêm `"unproven": False` (hình dạng
  ứng viên mới; `verify_candidate` fail closed khi thiếu khóa); thêm test ứng viên unproven không đọc store.

| Chạy | Kết quả |
|---|---|
| `REM-B15-R05-red` (baseline) | 5 failed, 7 passed, EXIT=1. Retry ngoài chuỗi: `assert 202 == 422`. `_unrelated`/`_other_reason`: restore checkpoint. `_other_spec`/`_other_image`: `('CHECKPOINT_CORRUPT', 'CHECKPOINT_PROVENANCE_MISMATCH')`. 7 pass: retry của retry, byte đổi, FK khác tenant, 4 test worker (hành vi đã đúng trên HEAD, test bảo vệ) |
| `REM-B15-R05-green` (repo; test mới + `test_control_b15`, `test_checkpoint_restore_b14`, `test_checkpoint_b14`, `test_checkpoint_corruption_b14`, `test_inference_chunks_b16`, `tests/worker`) | 605 passed, EXIT=0 |
| Local Mac `pytest -q tests` | 1751 passed, 574 skipped (PG) |

Lần red đầu fail sai lý do (helper test UPDATE `job_specs`, bảng có trigger bất biến); helper được sửa để
INSERT spec biến thể một lần, rồi chạy red lại. Không có migration. Không đổi API công khai: retry sai vẫn
`422 infeasible_request` như cũ, chỉ thêm trường hợp.
**Trạng thái: CLOSED.**

### B14-R04: "incompatible" gộp hai trường hợp khác nhau trong contract restore (L2, 5.A)

**Root cause (câu chữ, không phải code).** `workloads-checkpoints.md` bước 6 viết "Job fails or remains blocked
during relocation with `waiting_for_compatibility`". Đoạn "Relocation…" (HEAD :173) viết "Incompatible jobs
remain blocked…, not auto-failed". Cả hai dùng chữ "incompatible" cho hai tình huống khác nhau:
- destination không chạy được Job. Đây là evaluator dispatch/retry dùng (`JobService.template_runs_on` + tài
  nguyên), và nó giữ Job chờ;
- destination chạy được Job nhưng không checkpoint committed nào tương thích. Đây là quyết định restore lúc
  claim: `restart_safe` fallback về input (PLAN:236), nếu không thì `CHECKPOINT_RESTORE_UNAVAILABLE`.

Code trên HEAD đã tách đúng hai trường hợp:
- `coordinator/retry._blocked_reason` giữ `RETRY_WAIT` + `waiting_for_compatibility`;
- `checkpoint_restore._restore_decision` chọn FALLBACK hoặc RESTORE_UNAVAILABLE;
- start bị từ chối cho Attempt không restart_safe thiếu restore.

Nên finding là mơ hồ contract; §5.A quyết định giữ hành vi và làm rõ câu chữ. (":96–98" trong §5.A thuộc
B16-R04, sweep partial acceptance, không thuộc finding này.)

**Sửa (docs, loại c theo §5.A).** `docs/contracts/workloads-checkpoints.md`:
- Bước 6 chỉ còn trường hợp không restart_safe: event `CHECKPOINT_RESTORE_UNAVAILABLE`, start bị từ chối,
  Attempt `INCOMPATIBLE`/`CHECKPOINT_RESTORE_UNAVAILABLE` trước mọi container, Job fail. Bước 6 cũng ghi rằng
  bước 1–6 chỉ chạy cho Attempt trên destination chạy được Job.
- Đoạn Relocation tách hai gạch đầu dòng:
  - destination không chạy được Job: không dispatch, không restore selection; Job đang recover giữ
    `RETRY_WAIT` + `waiting_for_compatibility` (event `RETRY_BLOCKED` khi lý do đổi), giữ lịch retry,
    checkpoint không bị xét/đánh dấu; không auto-fail trừ khi admin cancel hoặc policy khai báo fail vĩnh viễn;
  - destination chạy được nhưng mọi checkpoint không tương thích: `CHECKPOINT_INCOMPATIBLE` với reason chính
    xác, rồi bước 5 (restart_safe) hoặc bước 6.
- `concurrency-recovery.md:107` đã nói đúng như vậy ("Dispatch recheck … blocks with
  `waiting_for_compatibility`; restore emits the typed incompatibility reason"), nên không sửa.

**Test.** `tests/integration/test_rem_b14_r04_incompatible_split.py` (PG). Hai test đều khẳng định evaluator
production (`coordinator.eligibility._compatible`) nói destination chạy được/không chạy được Job, để hai
trường hợp không lẫn nhau:
- `test_node_incompatible_retry_waits_visibly_and_keeps_its_checkpoints`. Template restart_safe chỉ amd64, 2
  checkpoint committed, fail INFRASTRUCTURE + cleanup thật → `RETRY_WAIT`; inventory arm64 → evaluator false.
  `promote_retries` = 0 và `tick` không Dispatch. Job vẫn `RETRY_WAIT`, `waiting_for_compatibility`,
  `retry_count` 1, `terminal_at` null; event cuối `RETRY_BLOCKED/waiting_for_compatibility/COORDINATOR`;
  không event restore, không corruption, checkpoint giữ nguyên. Inventory amd64 → evaluator true → promote 1 →
  `QUEUED`, checkpoint vẫn nguyên.
- `test_runnable_job_whose_checkpoints_are_all_incompatible[restart-safe|not-restart-safe]`. Template
  amd64+arm64, checkpoint viết trên amd64, inventory arm64 (evaluator true), Attempt 2 claim → restore `None`,
  không corruption.
  - restart-safe: `[INCOMPATIBLE/COMPATIBILITY_MISMATCH, FALLBACK_TO_INPUT]`, start 200.
  - not-restart-safe: `[INCOMPATIBLE/COMPATIBILITY_MISMATCH, RESTORE_UNAVAILABLE]`, start 409, fail
    `INCOMPATIBLE`/`CHECKPOINT_RESTORE_UNAVAILABLE` với `NO_CONTAINER` + cleanup tombstone → Job `FAILED`,
    `retry_count` 0, `started_at` null.

| Chạy | Kết quả |
|---|---|
| `REM-B14-R04-baseline` (HEAD a6c38bf) | 3 passed, EXIT=0: hành vi đã đúng trên HEAD, test là coverage khóa việc tách hai trường hợp, không phải red→green |
| `REM-B14-R04-green` (repo) | 3 passed, EXIT=0 |

Trong lúc viết test, hai lần chạy đầu fail do input test, không do sản phẩm; đã sửa test, không sửa code:
- `_inventory()` quảng bá image `sha256:c…` trong khi template của CheckpointFixture khóa `sha256:b…`, nên
  evaluator không bao giờ coi fixture chạy được. Heartbeat test giờ dùng digest của template.
- Tenant của fixture không có `tenant_policies` hiện hành, nên sau khi tương thích lại, retry chuyển đúng sang
  `waiting_for_quota`. Test seed policy như `seed_dispatchable`.
- Slug tenant có chữ hoa (`True`/`False`) vi phạm `ck_tenants_slug_format`.

Chẩn đoán bằng bản sao test chỉ chạy trên VPS1, đã xóa sau khi chạy. Không migration, không đổi API công khai.
**Trạng thái: CLOSED** (điều kiện §5.A: giữ fallback restart_safe có event, làm rõ câu chữ tách hai trường hợp,
test cả hai).

## M7 — Quyết định và tài liệu trạng thái

### B15-R06: manual retry lần hai với idempotency key mới (L2, OD-1)

**Hiện trạng (đo trên L, không đổi code).** `retry_failed_job` (`application/job_control.py:604`) chạy theo thứ tự:
idempotency theo tenant + principal + `retryFailedJob` + key, replay trả response đã lưu trước `If-Match`;
`_admit` như submit (khóa counter, quota `429 quota_exceeded`, global `503 queue_full`); đọc Job nguồn
`FOR SHARE`; kiểm `If-Match` và state `FAILED`; tạo Job/LogicalSession mới với `retry_of_job_id`. Job nguồn
terminal bất biến (SM:41): version, event và counter của nó không đổi. Vì vậy `If-Match` của request sau vẫn
khớp, không chặn được nhánh thứ hai. `jobs.retry_of_job_id` chỉ có FK cùng tenant
`fk_jobs_same_tenant_retry_source` (`schema_v1.py:331–336`), không có unique và không có index.

Kết quả: mỗi key mới tạo thêm một Job retry cùng `retry_of_job_id`, tính admission và bị quota outstanding
giới hạn như submit. Replay cùng key trả đúng Job của request đó, không tạo Job thứ ba.
`concurrency-recovery.md:77` ("Same request returns same new Job; no second lineage") đúng cho replay cùng
request; câu chữ không nói gì về request mới với key mới.

**Test tái hiện (chuẩn bị, không chọn phương án).** `tests/integration/test_rem_b15_r06_second_retry.py` (PG):
source `FAILED`, retry key `…0001` → 202, key `…0002` → 202; ba Job khác nhau, cùng `retry_of_job_id`; 2 retry;
counter GLOBAL (2, 0); version nguồn không đổi. Replay mỗi key với `If-Match: "v999"` → 202 và body gốc của
nó, vẫn 2 retry. Hạ `user_outstanding_limit` = 2 → key `…0003` → `429 quota_exceeded`, vẫn 2 retry, counter
không đổi.

| Chạy | Kết quả |
|---|---|
| `REM-B15-R06-baseline` (HEAD a6c38bf) | 1 passed, EXIT=0 |
| `REM-B15-R06-repo` | 1 passed, EXIT=0 |

Test ghi lại hành vi hiện tại để owner quyết định. Docstring của file nói rõ nó chưa chọn phương án; phương án
được chọn sẽ biến nó thành regression test.

**Phân tích theo phương án.**
- **A (Ask đề xuất).** Không đổi code, schema hay API. Chỉ làm rõ `concurrency-recovery.md:77`: "no second
  lineage" nói về replay cùng một request; request mới với key mới là một retry mới, có quota. Test hiện tại
  giữ nguyên làm regression test.
- **B.** Cần đủ các phần sau:
  - Migration `0023` thêm unique partial index `(tenant_id, retry_of_job_id) WHERE retry_of_job_id IS NOT NULL`.
    Khóa nguồn là `FOR SHARE`, nên hai request đồng thời cùng qua được bước kiểm tra trước; chỉ unique mới chặn
    được. Trước khi tạo index phải kiểm tra dữ liệu có sẵn: dữ liệu hiện tại hợp lệ có thể đã có nhiều retry
    cho một nguồn, và owner phải chọn fail migration hay giữ bản ghi cũ. Downgrade xóa index.
  - Code: kiểm tra retry đã có sau khi khóa nguồn, trả `409 state_conflict` kèm `job_id` của retry đó. Bắt
    unique violation, map về cùng `409` và rollback admission.
  - OpenAPI/state-machines: mô tả `409` mới của `retryFailedJob` (breaking với client đang tạo nhiều retry).
  - Test: đổi test trên thành `409`, cộng test hai request đồng thời khác key, chỉ một Job được tạo và counter
    chỉ tăng một.

**Hành động tối thiểu của owner:** điền OD-1 bằng A hoặc B. Với A: một câu làm rõ ở
`concurrency-recovery.md:77`. Với B: làm các phần trên.

**Trạng thái: NEEDS OWNER DECISION (OD-1).** Không migration, không đổi code hay contract ở vòng này.

### AUD-01: câu trạng thái lỗi thời trong AGENTS.md, acceptance.md và header B04/B05 (L2, docs)

**Root cause.** Câu trạng thái được viết ở B01/B02 và trong lúc chờ re-review, rồi không được cập nhật khi
B03–B16 được triển khai và duyệt; ROADMAP là nơi duy nhất ghi đúng.

**Sửa (chỉ câu trạng thái, không đổi quy tắc, không nâng gate):**
- `AGENTS.md:9`: thay "Chưa có product implementation, runtime, migration hoặc runtime acceptance evidence"
  bằng câu nêu B03–B16 đã có implementation và evidence theo task, còn B17–B25 chưa triển khai và
  ACC-02–ACC-39 chưa gate nào đạt. Các câu khác của dòng, gồm "Không coi bootstrap/CI xanh … là product
  acceptance đã đạt", giữ nguyên.
- `docs/acceptance.md:7`: ACC-02–ACC-39 vẫn `specified`. Chỉ đổi lý do: evidence theo task của B02–B16 không
  phải nghiệm thu gate, chưa gate nào được chạy đủ tiêu chí và environment, phần B17–B25 chưa triển khai.
  ACC-01 `pass` và bảng gate không đổi.
- `docs/evidence/B04-fairness.md:4, :175`: `approved`, dẫn [ROADMAP](../../ROADMAP.md). Lịch sử `Không duyệt`
  → remediation B04-R01/R02 được giữ. Câu tường thuật `:126` được giữ, thêm ghi chú rằng nó viết trước
  re-review.
- `docs/evidence/B05-postgresql.md:4`: `approved` (user xác nhận), dẫn ROADMAP. Câu `:179` được giữ và thêm
  ghi chú tương tự.
- Không thêm ngày hay ID review: header chỉ trỏ tới ROADMAP.

**Kiểm chứng.** `git diff` của bốn file chỉ chứa các câu trên. `git diff --check` EXIT=0. `grep` các cụm
"Chưa có product implementation", "chưa có implementation hoặc runtime", "awaiting independent" và
"await re-review" trong `AGENTS.md`, `README.md`, `ROADMAP.md` và `docs` không còn khớp trong phạm vi AUD-01.

Còn khớp ngoài phạm vi, không sửa (ghi nhận): `B07-artifact-store.md:51` và
`B10-worker-heartbeat-reconcile.md:95` là tường thuật trong thân evidence, viết lúc chờ review, không phải
header trạng thái; ROADMAP ghi hai task đã duyệt. File trong `docs/superpowers/plans/` là kế hoạch lịch sử.

**Trạng thái: CLOSED** (docs; không test code vì không có hành vi thay đổi).

## M8 — Retention, retry probe, làm rõ contract và sweep

Evidence của M8 ở lớp L: VPS1 (AWS EC2 c7i.2xlarge, Ubuntu 24.04), PostgreSQL 17 (`version_num` 170011).
Mỗi lần chạy dùng một DB `nexa_b05_test_*` mới. Baseline là HEAD a6c38bf, repo là working tree remediation.

### B15-R18 (residual): retention sweep đọc lại mọi record hết hạn chưa đến lượt xóa ở mỗi tick (L1)

**Root cause.** Sau B15, walk đi theo `ix_idempotency_records_b15_sweep` theo thứ tự `expires_at` và lọc
job terminal trong `WHERE`. Record hết hạn của job còn sống (hoặc không có job) không bao giờ bị xóa, nhưng
vẫn nằm trong khoảng index. Vì thế mỗi tick đọc lại tất cả chúng, và chi phí tăng theo số record như vậy.

**Sửa (`coordinator/retention.py`, `coordinator/service.py`).** `sweep_batch` khóa tối đa 100 record hết hạn
(`FOR UPDATE OF idempotency_records SKIP LOCKED`), đọc state job bằng scalar subquery theo khóa chính, rồi
chia hai nhóm:
- Job terminal: xóa, như trước.
- Job còn sống hoặc không có job: hoãn, tức nâng `expires_at` lên `now() + RECHECK` (1 ngày). Record rời
  khỏi walk và không tick nào đọc lại trước thời điểm đó.

Record `submitSweep` được xét khi batch job-request còn chỗ, theo B16-R05:
- sweep chưa xong: hoãn 1 ngày;
- sweep đã xong, không còn record con: xóa;
- sweep đã xong, còn record con: hoãn tới expiry lớn nhất của record con.

Hoãn là một lần ghi, nên cần leader và mode khác `WRITE_FROZEN`, như lệnh xóa.

Quy tắc retention (`contracts.md:92`) không đổi:
- Hoãn chỉ nâng `expires_at`, không phải quy tắc xóa.
- Nhánh terminal vẫn đặt `greatest(expires_at, terminal_at + 30 ngày)`.
- 1 ngày < 30 ngày tối thiểu, nên retention khi job kết thúc vẫn chính xác.

Không migration.

**Test.** `tests/integration/test_rem_b15_r18_bounded_sweep.py` (PG, 5 test):
- record hết hạn của job sống rời walk và không bị ghi lại ở tick sau;
- record đã hoãn nhận đúng `terminal_at + 30 ngày` khi job bị cancel;
- mỗi tick chỉ xét một batch có giới hạn;
- leader cũ và `WRITE_FROZEN` không ghi;
- parent sweep chờ record con cuối cùng mà nằm ngoài walk.

`tests/integration/test_sweep_b16.py`: helper `due()` đổi sang `sweep_batch(session).due`, assertion giữ nguyên.

| Chạy | Kết quả |
|---|---|
| `REM-B15-R18-red-baseline` | 5 failed, đúng lý do: record của job sống và record không job vẫn nằm trong walk sau tick; tick đầu xóa luôn record terminal nằm sau 150 record live (`assert 1 == 0`), tức walk đọc vượt batch; leader cũ không bị `LeadershipLost`; record parent sweep vẫn nằm trong walk |
| `REM-B15-R18-green` | 5 passed, EXIT=0 |
| `REM-M8-control-b15` (repo: `test_control_b15`, `test_retention_b15`, R18, R05, `test_sweep_b16`) | 79 passed, EXIT=0 |
| `REM-M8-R02-R04-R10-repo` (gồm R18, R05, `test_sweep_b16`, `test_control_b15`) | 114 passed, EXIT=0 |

**Số đo trước/sau (L, 100.000 record).** Harness `benchmarks/rem/maintenance_probes.py`, cùng lệnh và cấu
hình trên hai tree. Fixture: 100 tenant × 1.000 job, mỗi job một record `submitJob` đã hết hạn. Cứ 100 job có
1 job `CANCELLED` với `terminal_at` cách đây 40 ngày, tức 1.000 record đến hạn và 99.000 record của job sống;
ANALYZE sau seed. Drain chạy đến tick đầu tiên không ghi gì và không còn record đến hạn. Sau đó là 30 tick
steady. EXPLAIN chạy với `(ANALYZE, BUFFERS, FORMAT JSON)`, `jit = off`, trong transaction rollback. Raw:
[`raw/REM-M8-maintenance-probes-baseline.json`](raw/REM-M8-maintenance-probes-baseline.json),
[`raw/REM-M8-maintenance-probes-repo.json`](raw/REM-M8-maintenance-probes-repo.json).

| Chỉ số | Baseline | Repo |
|---|---|---|
| Record xóa khi drain | 1.000 | 1.000 |
| Record hết hạn còn trong walk sau drain | 99.000 | 0 |
| Tick steady (median / p95 / max) | 258,1 / 265,9 / 267,7 ms | 5,8 / 6,4 / 8,4 ms |
| EXPLAIN walk steady | 267,9 ms; `ix_idempotency_records_b15_sweep`; 99.000 dòng bị filter loại; 99.000 lần đọc `pk_jobs`; 533.503 shared hit | 0,11 ms; cùng index; 0 dòng; 113 shared hit |
| Drain | 11 tick (10 có ghi), median 167,0 ms | 1.001 tick (1.000 có ghi), median 23,4 ms, max 31,7 ms |

Diễn giải:
- **Drain một lần.** Repo mất khoảng 990 tick có giới hạn để hoãn 99.000 record có sẵn, khoảng 17 phút ở 1 tick/giây.
- **Tiếp theo.** Mỗi record của job sống quay lại walk tối đa một lần mỗi ngày.
- **Thời gian DB mỗi ngày, ở 1 Hz với tập record này.**
  - Baseline: khoảng 258 ms × 86.400 ≈ 6,2 giờ.
  - Repo: khoảng 5,8 ms × 86.400 ≈ 8,4 phút, cộng khoảng 1.000 tick hoãn × 23 ms ≈ 23 giây.
- **EXPLAIN câu walk của tick đầu** (chạy lại ngay sau tick đó) ở repo dùng `ix_idempotency_records_expiry`: 0,82 ms, 100 dòng, không dòng nào bị filter loại, 701 hit + 42 read. Baseline: 56,1 ms, 19.648 dòng bị filter loại, 106.883 hit. Planner của repo chọn index expiry vì lúc đó mọi record đều đã hết hạn. Plan steady quay về `ix_idempotency_records_b15_sweep`.
- **Walk của parent sweep** là `ix_idempotency_records_b16_sweep_parent`, 0,02 ms.

**Trạng thái: CLOSED.**
- Walk có giới hạn, và record chưa đến hạn không bị đọc lại mỗi tick.
- Có EXPLAIN ANALYZE và số đo trước/sau trên dữ liệu lớn.
- Quy tắc retention giữ nguyên và có test: `test_a_deferred_record_still_gets_exactly_the_terminal_retention`, B16-R05 và `test_retention_b15`.

### B14-K1: probe retry lấy khóa quyết định mỗi giây kể cả khi không có việc (L1)

**Root cause.** Ở baseline, `promote_retries` khóa lần lượt:
- leadership;
- `policy_versions FOR UPDATE`;
- counter GLOBAL;
- job và schedule của batch đến hạn.

Việc này xảy ra ngay cả khi mọi retry đến hạn vẫn bị chặn với đúng lý do đã công bố. Một retry bị chặn quota
nhiều giờ vì thế giữ khóa policy mỗi giây. Request submit/admin lấy cùng khóa đó phải chờ.

**Sửa (`coordinator/retry.py`, `coordinator/service.py`).**
- **Probe `retry_actionable`.** Đọc không khóa đúng batch `_candidates(now, limit)` và áp dụng cùng
  `_blocked_reason` với nhánh khóa. Trả `True` khi có ít nhất một job được promote, hoặc có lý do chặn khác
  `waiting_reason` đang hiển thị. Nếu `False`, `promote_retries` trả 0 mà không lấy khóa nào.
- **Nhánh có việc.** Giữ nguyên: đọc lại và kiểm tra lại mọi dòng dưới khóa, CAS state/version. Lần đọc
  không khóa không quyết định gì.
- **Thay đổi commit sau lần đọc.** Nhánh khóa hoặc probe kế tiếp sẽ thấy.
- Ngữ nghĩa retry không đổi.

**Test.** `tests/integration/test_rem_b14_k1_retry_probe_locks.py` (PG, 4 test):
- retry bị chặn với lý do không đổi được probe 3 lần, 0 câu khóa/ghi; version, event và schedule không đổi;
- khi một connection khác giữ khóa policy, probe rỗi trả trong < 0,5 s. Sau khi quota mở, retry được promote
  đúng một lần (`RETRY_READY` ×1, schedule đóng) dưới khóa;
- **race cancel:** cancel commit sau probe thì cancel thắng, không `RETRY_READY`/`RETRY_BLOCKED`;
- **race đổi leader:** leader đổi sau probe thì leader cũ nhận `LeadershipLost` và không ghi gì; leader mới
  promote đúng một lần.

| Chạy | Kết quả |
|---|---|
| `REM-B14-K1-red-baseline` | 4 failed: 24 câu khóa khi probe rỗi; `LockNotAvailable` trên `policy_versions`; hai test race `AttributeError`, vì baseline không có probe để chèn race |
| `REM-B14-K1-green` (K1 + `test_retry_b14` + `test_callback_lock_order_b15` + `test_coordinator_b11`) | 32 passed, EXIT=0 |
| `REM-M8-R02-R04-R10-repo` (gồm K1) | 114 passed, EXIT=0 |

Hai test race không có "đỏ" nghĩa về hành vi ở baseline. Chúng là regression test cho đường mới: race với
cancel, đổi leader và promote vẫn cho đúng kết quả của nhánh khóa.

**Số đo trước/sau khóa (L).** Cùng harness và DB 100k ở trên. Có 16 retry đến hạn bị chặn quota
(`cpu_limit_millis = 0`); lý do đã được công bố một lần trước đó. Harness đếm câu `UPDATE`/`INSERT`/`DELETE`/
`FOR UPDATE`/`FOR SHARE` qua listener `before_cursor_execute`. Contention: một connection giữ dòng counter
GLOBAL `FOR UPDATE` trong 300 ms; probe chạy trong thread, còn "victim" lấy `policy_versions FOR UPDATE` với
`lock_timeout` 2 s.

| Chỉ số | Baseline | Repo |
|---|---|---|
| Probe công bố lý do mới (có việc) | 86 câu khóa/ghi, 284 ms | 86 câu khóa/ghi, 275 ms |
| Câu khóa/ghi mỗi probe rỗi | 38 | 0 |
| Câu khóa/ghi mỗi giờ ở 1 probe/giây | 136.800 | 0 |
| Probe rỗi (median / p95 / max, 60 lần) | 75,3 / 111,3 / 119,1 ms | 20,8 / 60,7 / 64,4 ms |
| Victim chờ khóa policy (5 mẫu) | 306,6–354,0 ms | 0,8–1,4 ms |
| `waiting_reason` sau khi công bố | `waiting_for_quota` | `waiting_for_quota` |

**Trạng thái: CLOSED.** Khóa chỉ được lấy khi có việc thật. Test race cancel, đổi leader và promote xanh.
Có số đo khóa trước/sau.

### B15-R02: FAILED khi lỗi non-retryable xảy ra lúc desired PAUSED (L1, 5.A)

**Hiện trạng.** Code đã đúng 5.A (`application/execution_cleanup.py`): cả hai nhánh pause, tức `PAUSED` có
checkpoint và `CHECKPOINT_FOR_PAUSE`, đều cần failure class `INFRASTRUCTURE`. Mọi class khác vào nhánh
`FAILED`. Thiếu hai thứ:
- câu chữ trong contract;
- test cho nhánh restart-safe không có checkpoint. `test_pausing_failure_with_committed_checkpoint` đã phủ nhánh có checkpoint.

**Làm rõ `state-machines.md`.** Hai hàng RECOVERING có pause thêm điều kiện "committed failure class
`INFRASTRUCTURE` (the reaper commits it for lease/process loss)". Hàng non-retryable thành "desired not
CANCELLED, i.e. RUNNING or PAUSED". Class non-retryable thắng pause đang yêu cầu, có hay không có checkpoint,
và không tiêu retry.

**Test.** `tests/integration/test_rem_b15_r02_non_retryable_pause.py` (PG), tham số hóa `TIMEOUT`, `OOM`,
`INTERNAL`:
- template restart-safe, còn budget, pause 202;
- container fail với class trên, rồi cleanup;
- kỳ vọng: job `FAILED`, `retry_count` 0, `recovery_intent` NULL, `terminal_at` có giá trị; attempt
  `FAILED`; allocation `RELEASED`; 0 `retry_schedules`; counter GLOBAL (0, 0).

| Chạy | Kết quả |
|---|---|
| `REM-M8-R02-R04-R10-baseline` | 3 passed (hành vi đã đúng; test ghim hành vi) |
| `REM-M8-R02-R04-R10-repo` | 3 passed trong 114 passed, EXIT=0 |

**Trạng thái: CLOSED.** Contract đã làm rõ và có test assert.

### B15-R34: attempt đã LOST khi job bị cancel (L1, 5.A)

**Hiện trạng.** Attempt `LOST` giữ terminal, còn job thành `CANCELLED`. Cleanup
(`application/execution_cleanup.py`) chỉ đặt `ended_at`/`updated_at` cho attempt `LOST` và không đổi state.

**Làm rõ `state-machines.md`.**
- Hiệu ứng cleanup của CANCELLING: "Attempt CANCELLED, except an Attempt the reaper already made `LOST`, which
  stays `LOST` with `ended_at` set".
- Hàng Attempt `active | lease expired` thêm: `LOST` là terminal. Cleanup đã kiểm chứng giải phóng
  allocation và chuyển Job (retry, pause, failure hoặc cancel yêu cầu sau reap), nhưng không đổi Attempt.
- Guard `active/STOPPING | cancel/pause cleanup` ghi rõ Attempt `LOST` không còn active.

**Test.** Giữ nguyên `tests/integration/test_control_b15.py::test_cancel_of_a_reaped_job_keeps_the_lost_attempt_terminal`.
Nó xanh trong `REM-M8-control-b15` (79 passed) và `REM-M8-R02-R04-R10-repo` (114 passed).

**Trạng thái: CLOSED.** Contract đã làm rõ; test hiện có assert đúng hành vi.

### B16-R05: retention của parent/child mapping sweep (L1, 5.A)

**Kiểm chứng.** `contracts.md:92` đã quy định "giữ cùng thời gian dài hơn".
- **Baseline:** record `submitSweep` chỉ bị xóa khi mọi child đã có outcome và không còn child accepted nào
  giữ record `submitJob`.
- **Repo:** quy tắc giữ nguyên. B15-R18 chỉ thêm phần hoãn ngoài walk.

**Test.** `tests/integration/test_rem_b16_r05_sweep_retention.py` (PG):
- parent đã hết hạn còn được giữ khi child record còn;
- parent chưa xong được giữ;
- parent bị xóa khi record con cuối cùng đã bị xóa.

| Chạy | Kết quả |
|---|---|
| `REM-B16-R05-baseline` | 1 passed (implementation đã khớp) |
| `REM-B16-R05-repo` | 1 passed; cũng xanh trong `REM-M8-R02-R04-R10-repo` |

**Docs.** Mục sweep của `workloads-checkpoints.md` thêm đoạn retention trỏ về `contracts.md` Idempotency 8 và
mô tả việc hoãn ngoài walk. `docs/coordinator.md` mô tả cùng quy tắc.

**Trạng thái: CLOSED.**

### B16-R04: mất xác thực/membership giữa các child của sweep (L1, 5.A)

**Hiện trạng.** Code đã đúng 5.A:
- `_admit_child` gọi `_authorize` trong mỗi transaction child. Lỗi `authentication_required`/
  `permission_denied` làm request abort, không thành outcome `REJECTED`.
- Child `ACCEPTED` được giữ.
- Replay cùng key bởi principal có quyền tiếp tục index chưa xong (bước 4).

**Làm rõ `workloads-checkpoints.md` bước 3.**
- Auth và membership là precondition của request, không phải outcome của child.
- Nếu mất giữa các child thì abort fail closed với `401`/`403` và không ghi outcome nào, kể cả `REJECTED`,
  để outcome bất biến không đóng băng trạng thái credential tạm thời.
- Child `ACCEPTED` được giữ; replay bởi principal có quyền thì tiếp tục.
- `503 dependency_unavailable`, child record đang chạy và `WRITE_FROZEN` cũng abort như vậy.

**Test.** `tests/integration/test_rem_b16_r04_sweep_authorization_loss.py` (PG), tham số hóa: thu hồi session
(`401 authentication_required`) và xóa membership (`403 permission_denied`) giữa child thứ 2 và thứ 3.

Kỳ vọng:
- **Sau abort:** parent (6, 2, 0), child 0–1 có job, 2 job.
- **Replay khi còn bị thu hồi:** cùng lỗi; mapping (parent row và children) không đổi.
- **Khôi phục** (đăng nhập lại hoặc thêm lại membership), rồi replay: 207, cùng `sweep_id`, 6 child
  `ACCEPTED`, job id của child 0–1 giữ nguyên, 6 job.

| Chạy | Kết quả |
|---|---|
| `REM-M8-R02-R04-R10-baseline` | 2 passed (hành vi đã đúng) |
| `REM-M8-R02-R04-R10-repo` | 2 passed trong 114 passed, EXIT=0 |

**Trạng thái: CLOSED.**

### B16-R10: giá trị trùng trong một dimension bị bỏ trùng im lặng (L1, 5.A, BREAKING)

**Root cause.** OpenAPI `SweepDimension.values` có `uniqueItems: true`. B16 hiện thực bằng dedup RFC 8785
trong `expand`, nên request có giá trị trùng được nhận (207) thay vì bị từ chối như `contracts.md:71` (422
validation).

**Sửa.**
- `application/sweep_expansion.py`: `_check_shape` từ chối khi `canonical_values(values)` ngắn hơn
  `values` ("Sweep dimension values must be unique (RFC 8785)"). Service ánh xạ lỗi này thành
  `422 validation_failed` và không ghi gì.
- Tên dimension trùng đã bị từ chối từ trước; nay có test riêng cho message.
- `canonical_values` được giữ làm no-op phòng thủ trên request đã được nhận.
- Comment `SweepDimension` (`api/schemas.py`) và docstring được cập nhật.

**Quyết định replay.** Sweep đã lưu trước quy tắc replay nguyên trạng. `open_parent` trả mapping đã lưu và
tiếp tục các index chưa xong từ expansion đã lưu, trước khi validate request. Chỉ request mới bị validate.
Không migration dữ liệu.

**Test.**
- **Unit** (`tests/application/test_sweep_expansion_b16.py`):
  - `test_equal_json_values_in_one_dimension_reject_the_request` (8 case: `1`/`1.0`, `2.50`/`2.5`,
    `true`/`true`, `"1"`/`"1"`, decoded `1.0,1,1e0`, `0.10,0.1`, `0.1,0.01,1e-2,0.001`) thay
    `test_canonical_dedup_keeps_the_first_occurrence_of_equal_json_values`;
  - `test_values_equal_only_in_python_are_distinct_and_canonical_values_is_a_no_op`
    (`1`, `true`, `"1"` là ba giá trị);
  - property test tách đôi: tích Descartes trên giá trị unique, và
    `test_one_repeated_value_in_any_dimension_rejects_the_whole_request` (chèn một bản lặp, kể cả dạng
    `Decimal` tương đương, vào vị trí bất kỳ);
  - case tên trùng khớp "names must be unique".
- **PG** (`tests/integration/test_rem_b16_r10_sweep_unique_values.py`):
  - request có giá trị trùng (`[0.1, 0.01, 0.01, 0.001]`, `[1, 2, 1]`, raw `0.01` với `1e-2`, `0.010`,
    `1.0E-2`) hoặc tên trùng → 422 `validation_failed`, không có parent/job/record/counter, và key không bị
    tiêu;
  - sweep lưu theo hành vi B16 (quy tắc tắt tạm bằng monkeypatch, crash sau 2 child) replay sau khi bật lại
    quy tắc: 207, cùng `sweep_id`, 6 child theo expansion đã dedup, job id child 0–1 giữ nguyên; replay lần
    nữa trả body y hệt; key mới với cùng body → 422.
- **Fixture golden:**
  - `hyperparameter-sweep-v1/request.json` bỏ `1e-2`, vì trùng `0.01` (sha256 `2aa967a8…` → `6e196e7d…`).
    Docker D7 đọc fixture qua `json.loads` rồi post, nên fixture cũ sẽ gửi `[0.1, 0.01, 0.01, 0.001]` và nhận 422.
  - `expansion.json` chỉ sửa câu `note` (`81cdbe67…` → `6e93af5e…`). `children` không đổi, nên các giá trị
    băm ghi ở [evidence B16](B16-pytorch-sweep-inference.md) `:170–171` là của bản trước remediation.

| Chạy | Kết quả |
|---|---|
| Unit local (Mac, trước sửa) | 9 failed `DID NOT RAISE SweepExpansionError`, 13 passed |
| Unit local (sau sửa) | 22 passed |
| `REM-M8-R10-baseline` | 2 failed `assert 207 == 422` |
| `REM-M8-R02-R04-R10-repo` (R10, `test_sweep_b16`, unit, CLI sweep) | 114 passed, EXIT=0 |

**Tác động.**
- CLI ánh xạ theo `code` (`validation_failed` → exit 9), không cần đổi.
- `web/src` chưa có sweep UI.
- `docs/submit.md` và `workloads-checkpoints.md` đã cập nhật.
- OpenAPI đã có `uniqueItems: true`, không đổi.

**Breaking.** Request mới có giá trị trùng (RFC 8785) nhận 422 thay vì 207.

**Trạng thái: CLOSED.**

### AUD-02: file 93 MB đã track (L1, OD-3)

**Sự thật (đo trong repo, không đổi gì).**
- `benchmarks/results/b04-fairness.json`: 92.953.374 byte, track từ `744e907` (2026-09-19), commit duy nhất
  chạm file.
- Blob nén zlib khoảng 4,2 MB; `.git` hiện khoảng 43 MB (loose objects).
- File được dẫn trong `README.md:134–137`, [evidence B04](B04-fairness.md) `:24` (kèm checksum) và
  `docs/project-structure.md:54`.
- Không có `.gitattributes`/LFS. CI duy nhất là `.github/workflows/ci.yml`.
- File track lớn tiếp theo đều < 50 MB: bốn file `benchmarks/results/b13/*accepted.jsonl` khoảng 25 MB,
  `b03-baselines.json` 4,3 MB.
- GitHub cảnh báo file > 50 MB và chặn push file > 100 MB. File này nằm giữa hai ngưỡng.

**Chuẩn bị theo phương án (không chọn).**
- **A (Ask đề xuất).**
  - Guard read-only chạy trong CI hoặc pytest: liệt kê `git ls-files -z`, đo kích thước mỗi file; mọi file
    > 50 MB ngoài allowlist `{benchmarks/results/b04-fairness.json: sha256 1231f797…}` làm fail.
  - Allowlist khớp theo cả path lẫn checksum, để file khác cùng tên hoặc bản sửa to hơn vẫn bị chặn.
  - Thêm một dòng vào kế hoạch B25: phát hành bundle này như release asset kèm checksum.
  - Không đổi lịch sử.
- **B.** `git rm --cached` file, thêm vào `.gitignore`, giữ bản ngoài repo cùng checksum đã ghi ở evidence
  B04, và sửa `README.md:134–137` để lệnh tái tạo ghi ra đường dẫn ngoài repo. Lịch sử không đổi, nên
  clone vẫn tải blob cũ.
- **C.** Rewrite history, không thuộc task này.

**Hành động tối thiểu của owner:** điền OD-3. Với A: thêm guard, allowlist và dòng B25. Với B: làm các bước
trên. Không cần thao tác nào trên lịch sử Git.

**Trạng thái: NEEDS OWNER DECISION (OD-3).** Không thay đổi file nào cho AUD-02 ở vòng này.

## M9 — Image cuối cùng và chạy lại Docker trên L

P = Docker Desktop Linux VM trên Mac (arm64): **ENVIRONMENT BLOCKED** suốt task. VM của P cạn PID vì
container `nexa_b10_smoke3-caddy-1` của task trước (ENV-01, M0), và task không được đụng container
`nexa_b10_*` hay đổi cấu hình Docker Desktop. Vì vậy M9 không build được image arm64 và không chạy được
Docker trên P. L = VPS1 (AWS EC2 c7i.2xlarge, Ubuntu 24.04), Linux thật, không GPU (không phải G/R).
Không gate ACC nào được nâng lên pass chỉ nhờ các lượt chạy dưới đây.

### REM-R04: `test_b11_vertical` không chạy được trên amd64 (mới, phát hiện ở M9)

**Mô tả.** Trong lượt đầu toàn suite Docker trên L (`docker-m9-full-20260929T033005Z`), hai case
`tests/docker/test_b11_vertical.py` fail. Worker READY/ENABLED nhưng cả hai job đứng `QUEUED`
(`waiting_for_worker`) và không có attempt nào. Đây là lỗi tính di động của test, có từ trước và cùng lớp với
B16-R29; sản phẩm không sai.

**Nguyên nhân (3 lớp, cùng đường đi).**
1. `_template` ghi cứng `capability_requirements.architectures: ["linux/arm64"]`. Test chỉ từng chạy trên
   P (arm64), nên trên L không worker amd64 nào đủ điều kiện nhận job.
2. Khi test fail, `_runner_diagnostics` đọc `agent-state.json` từ host. Container worker chạy bằng root
   (`deploy/b10/Dockerfile` không có `USER`) và ghi file mode 0600, nên việc đọc ném `PermissionError`, che
   mất failure thật. Harness B14/B15 import cùng helper này nên cũng bị.
3. Case `[True]` đọc journal từ host bằng `ExecutionJournal.load`. Từ B15-OBS-01, lệnh đọc này lấy khóa
   attempt trong `journal/locks` mà worker sở hữu, nên cũng ném `PermissionError`.

**Sửa (chỉ test).**
- Kiến trúc lấy từ `docker image inspect --format {{.Os}}/{{.Architecture}}` của image đang test, giống
  `_template` của B14/B15/K5/H01.
- `_read_worker_json` trả `None` khi file không có, và trả `{"unreadable": "PermissionError: errno 13"}`
  khi không đọc được. Lý do được đưa vào diagnostics, không bị nuốt.
- `[True]` đọc `result_flow` bằng `docker exec <worker> python -c …` với `ExecutionJournal`, tức là đọc
  dưới UID của worker.
- Không đổi assertion nào.

**Quyết định tự chọn.** Giữ nguyên ngữ nghĩa khóa journal của sản phẩm (B15-OBS-01). Journal là dữ liệu
riêng của worker; một reader khác UID nằm ngoài process worker không phải kịch bản sản phẩm, nên test đọc
dưới UID chủ sở hữu.

**Test.**
- Sau sửa `_template`: `[False]` pass trên L (`docker-b11b15-20260929T152043Z`), còn `[True]` lộ lớp 3.
- Sau sửa lớp 3: `docker-b11v-20260929T154621Z` cho **2 passed, 85,88 s, EXIT=0** (image m9, trước
  REM-R05).
- Lượt cuối với image final r2: xem "Toàn suite Docker L" (M10).

**Điều kiện đóng.** Test chạy đúng trên amd64 mà không nới assertion: pass (L). P: ENVIRONMENT BLOCKED.

**Trạng thái: CLOSED** (phần P ghi ENVIRONMENT BLOCKED trong closure matrix).

### REM-R05: renewal gửi sau cleanup đã verify chặn READY vĩnh viễn (mới, phát hiện ở M9)

**Mô tả.** Scenario B15 C1 fail trong lượt toàn suite `docker-m9-full-20260929T033005Z`. Scenario C8 fail
trong `docker-b11b15-20260929T152043Z` và fail lại trong lượt chẩn đoán `docker-b15-20260929T153805Z`, chạy
trên bản copy harness có dump trạng thái worker, không phải repo. Triệu chứng giống nhau: job kế tiếp đứng
`QUEUED` với `attempts: []`, worker không bao giờ READY lại. Dump của lượt chẩn đoán cho thấy:
- store chỉ còn một operation `renew` cho attempt `01a0edd4-4fd0…`, attempt đã pause;
- journal của attempt này là `TOMBSTONED`, có `cleanup_verified` và `pause_stop`;
- log API có `worker_callback_rejected` `/renew` `409 stale_authority` (log B15-R33).

**Nguyên nhân.**
- `WorkerAgent.renew_once` liệt kê các attempt trong `_adopted`, rồi lấy khóa journal của từng attempt để
  renew.
- Cleanup đã verify của result thread (`_send_resolution`) giữ cùng khóa đó, từ B15-R39. Dưới khóa, nó ghi
  `cleanup_verified`, bỏ pending `claim`/`renew`/`failure` (B15-R09), và chỉ bỏ attempt khỏi `_adopted`
  sau khi nhả khóa.
- Một lượt renew đã liệt kê attempt và đang chờ khóa sẽ gửi renewal cho lease đã kết thúc. Server trả
  `409 stale_authority`, và operation `renew` ở lại trong store.
- Chỉ cleanup đã verify mới bỏ được renewal, mà cleanup đó đã xong. Vì thế `_blocking_pending_attempts`
  giữ worker ngoài READY mãi.
- Cửa sổ (từ lúc bỏ operation tới lúc pop `_adopted`) có từ trước. Khóa của B15-R39 kéo nó ra bằng cả thời
  gian POST cleanup, nên trên L nó xảy ra thường xuyên. Regression này do remediation làm lộ ra và được
  sửa trong task.

**Sửa.** Trong `worker/agent.py::renew_once`, dưới khóa journal, bỏ qua attempt mà journal bền đã có
`cleanup_verified`. Marker này được ghi dưới cùng khóa trước bước bỏ operation, nên lượt renew chờ khóa
luôn thấy nó. `renew_once` không pop `_adopted`; việc đó thuộc đường result/cleanup. Server không đổi:
renew sau khi release vẫn là `409 stale_authority`.

**Quyết định tự chọn.** Dựa vào marker journal bền thay vì `_adopted` trong bộ nhớ. Marker được ghi nguyên
tử cùng bước bỏ operation dưới khóa và còn nguyên sau restart.

**Test.** `tests/worker/test_rem_r05_renew_after_verified_cleanup.py` dùng lại helper của
`test_rem_b15_r39_resolution_lock.py` và ép interleaving bằng Event, không sleep:
- result thread bị giữ ở POST cleanup;
- lượt renew được xác nhận đang chờ khóa (hook `journal.on_lock`);
- sau đó mới thả cleanup.

Kết quả:
- Trước sửa: **1 failed**, `assert ['cleanup', 'renew'] == ['cleanup']`.
- Sau sửa: **1 passed**. `tests/worker`: **495 passed**.

Docs: `docs/worker-agent.md`, câu REM-R05 ở đoạn khóa journal.

**Điều kiện đóng.**
1. Renewal không bao giờ được gửi sau cleanup đã verify: pass (unit, tất định).
2. B15 C1/C8 pass trên L với image final: pass với image r2 (lượt `docker-r2-20260929T220051Z`,
   `C1_pause_resume` và `C8_timeout` pass, `failed_scenario` null; xem "Toàn suite Docker L", M10).

**Trạng thái: CLOSED.**

### REM-R06: đường khác vẫn hành động trên attempt đã verify cleanup (mới, phát hiện ở vòng 2 / RV01)

**Mô tả.** Với image final (có REM-R05), lượt toàn suite `docker-final-20260929T155913Z` vẫn fail B15 C1
(và D6, xem B16-R21). Job kế tiếp đứng `QUEUED`: attempt 2 `LOST/LEASE_EXPIRED`, allocation quarantine.
Worker log có chuỗi `worker_loop_failed operation=reconcile_once detail=409/stale_authority`. Lượt chẩn
đoán `docker-c1diag-20260929T180038Z` (chỉ C1, cùng image, có log worker, log ngoài repo) tái hiện:
**1 failed, 445,88 s**.

**Nguyên nhân.** REM-R05 chỉ chặn đường renew. Các đường còn lại vẫn hành động trên attempt đã được
verify cleanup:
- Vòng reconciliation lấy trang khi attempt đang pause còn `LIVE`, inspect container, rồi chờ khóa journal
  trong lúc result thread gỡ container và verify cleanup. Khi có khóa, scan adopt lại attempt đã release và
  lỗi `adopted container changed during reconciliation`.
- Vòng IPC và result lặp lại trên snapshot `_adopted` cũ, nên liên tục lỗi vì container đã bị gỡ
  (relay Docker "No such container").
- `_execution_failed` vẫn gửi failure cho lease đã kết thúc (`409 stale_authority`). Operation failure ở lại
  trong store, nên `_blocking_pending_attempts` giữ worker ngoài READY.
- Offer replay của attempt đã release vẫn được claim.
Hệ quả: worker không READY lại đến khi lease của offer kế tiếp hết hạn. Cùng lớp với REM-R05; do remediation
(khóa B15-R39) làm cửa sổ rộng ra.

**Sửa (`worker/agent.py`, `worker/execution.py`).** Mọi đường có thể tạo claim/failure/renew/adopt cho một
attempt đều đọc marker bền `cleanup_verified` dưới khóa journal:
- scan (`agent.py:403–412`): dòng `LIVE` mà journal đã có `cleanup_verified` ⇒ không adopt, pop `_adopted`,
  coi là resolved;
- vòng IPC (`agent.py:1411`) và result (`execution.py:680`): pop attempt khỏi `_adopted` và các map pause;
- `_execution_failed` (`execution.py:911`): không gửi failure, trả resolved;
- `_dispatch_offer` (`execution.py:163`): không claim offer của attempt đã release.
Server không đổi: callback sau release vẫn `409 stale_authority`.

**Quyết định tự chọn.** Giống REM-R05: dựa vào marker journal bền (ghi dưới khóa, cùng bước bỏ operation,
còn sau restart), không dựa vào `_adopted` trong bộ nhớ.

**Test.** `tests/worker/test_rem_r06_verified_cleanup_guards.py` (4 test, interleaving bằng Event, không
sleep; dùng lại helper của B15-R39/B11-H01):
- `test_a_scan_waiting_on_a_verified_cleanup_does_not_readopt_the_attempt`: result thread bị giữ ở
  `docker stop`; scan được xác nhận đang chờ khóa (hook `journal.on_lock`) rồi mới thả stop;
- `test_no_failure_is_sent_for_a_verified_attempt`;
- `test_the_ipc_and_result_loops_drop_a_verified_attempt`;
- `test_an_offer_for_a_verified_attempt_is_not_claimed`.

Trước sửa: **4 failed** (`RuntimeError('adopted container changed during reconciliation')`;
`['cleanup', 'failure'] == ['cleanup']`; relay "No such container"; "a released attempt must not be
claimed"). Sau sửa: **4 passed**; `tests/worker` **499 passed**.

Docs: `docs/worker-agent.md`, câu REM-R06 sau câu REM-R05.

**Điều kiện đóng.**
1. Không đường nào (scan, IPC, result, failure, offer) hành động trên attempt đã verify cleanup: pass (unit,
   tất định, 4 test).
2. B15 C1/C8 pass trên L với image cuối: pass với image r2 (worker `sha256:897a7219…`; lượt
   `docker-r2-20260929T220051Z`: B15 C0–C10 pass, gồm `C1_pause_resume` và `C8_timeout`); log worker của harness B15 lượt đó (chỉ B15
   ghi log worker, `NEXA_B15_WORKER_LOG_OUT`, ngoài repo): 29 attempt, mỗi attempt đúng 1 `/cleanup` (200), 0 `JournalCorruption` (xem "Toàn suite Docker L", M10).
3. Không callback claim/failure/renew/adopt/cleanup nào gửi sau cleanup đã verify của cùng attempt: pass (đếm
   theo từng attempt trên log worker B15 lượt r2: sau `/cleanup` 200 không có request `claim`, `adopt`, `fail`, `renew`,
   `start` hay `/cleanup` lần hai).

**Quan sát còn lại (không chặn, ghi rõ).** Log worker B15 lượt r2 có 1 request sau cleanup đã verify: `POST
/v1/attempts/<id>/checkpoint-reservations` → `409` cách `/cleanup` 200 của cùng attempt 56 ms. Result thread
kiểm `cleanup_verified` (`execution.py:680`) không giữ khóa journal. Cleanup do thread khác verify
(`_stop_orphan` sau khi lease bị thu hồi) nên có thể xen giữa lúc kiểm và lúc reserve. Không đổi thiết kế ở vòng
này vì:
- server fence từ chối (`409`), không có reservation, không có state;
- reserve không phải operation bền (`_blocking_pending_attempts` chỉ tính
  claim/start/adopt/renew/failure/cleanup/runner_deadline), nên không chặn READY;
- nhánh 409 của `CheckpointFlow._resume` bỏ cycle, và vòng result kế tiếp pop attempt (REM-R06).

Muốn đóng hẳn cửa sổ thì phải giữ khóa journal quanh mọi callback của result thread. Làm vậy chặn renewal
trong lúc upload blob, nên rủi ro hết lease còn lớn hơn cửa sổ này. Các `409/stale_authority` khác trong log
(12 `_result_once` ở upload artifact, 2 `renew_once`) đều gửi **trước** cleanup của attempt đó, lúc lease vừa bị
server thu hồi (cancel/pause trong scenario). Đó là hành vi đúng.

**Trạng thái: CLOSED.**

### REM-R07: fault injection D6 làm hỏng input của D6b qua blob dùng chung (mới, test harness, phát hiện ở vòng 2)

**Mô tả.** Lượt toàn suite `docker-r2-20260929T220051Z` (image r2): 23 passed, 1 failed, lỗi
`AssertionError: job 01a0ef4b-6511-… ended FAILED`, raw [`raw/REM-R2-docker-L.out`](raw/REM-R2-docker-L.out).
- D6 trong cùng lượt đúng như mong đợi: `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`, recognized 16 → 16, raw
  [`raw/REM-R2-B16-inference.json`](raw/REM-R2-B16-inference.json).
- Job của D6b: attempt 2 `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE` ngay sau claim, không tải file nào.

**Nguyên nhân (2 vòng điều tra).**
1. Vòng 1, test PG mới: loại trừ lỗi sản phẩm ở đường "chỉ mất state checkpoint mới nhất".
   `test_lost_newest_state_blob_hands_the_worker_a_valid_recognized_run`:
   - claim restore checkpoint cũ và mang đúng chunk sau cursor;
   - `adapter_recognized` của worker nhận danh sách đó.
   Kết quả trên L: **12 passed** (cả file), raw [`raw/REM-R2-d6b-server.out`](raw/REM-R2-d6b-server.out).
2. Vòng 2, đối chiếu evidence: blob D6 xóa (artifact `01a0ef49-8e42-…`, chunk 8) được tạo ở **D4**, không phải
   ở job D6.
   - Artifact COMMITTED được dedup theo tenant, checksum, kind và media type
     (`application/artifact_service.py`, khoảng :545–558).
   - Chunk inference là tất định, nên chunk 8 của D4, D5, D6 và D6b là **cùng một blob**.
   - D6 xóa blob đó rồi không trả lại. Claim của D6b quét chunk sau cursor 8, thấy chunk 8 không đọc được và
     trả `recognized_chunks = null` đúng thiết kế (B16-R21). Worker fail closed, đúng hành vi.

Sản phẩm đúng. Lỗi nằm ở harness: fault injection của một scenario lọt sang scenario sau.

**Sửa (chỉ test, không đổi `src`).**
- `tests/docker/b16_support.py`: thêm `take_blob` (xóa blob, giữ byte và mode), `put_back_blob` (ghi lại bằng
  `O_EXCL` và `fsync`) và `blob_intact` (kiểm checksum).
- `tests/docker/test_b16_workloads.py`:
  - D6 dùng `take_blob` và trả blob lại **sau khi** đã assert xong kết cục D6;
  - D6b assert trước khi xóa state rằng mọi chunk sẽ carry forward còn nguyên (`blob_intact`), để cùng lỗi này
    không thể làm scenario fail sai chỗ lần nữa.
- Assertion của D6 và D6b không nới: D6 vẫn đòi FAILED `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`, 0 result, không
  container attempt 2; D6b vẫn đòi SUCCEEDED, carry-forward, không recognition trùng.

**Đỏ/xanh.**
- Đỏ: lượt r2 ở trên (D6b FAILED).
- Xanh: `docker-r2b-20260929T231201Z`, cùng image và cùng cây `src`: **1 passed, 213,42 s, EXIT=0** (B16-R21).

**Giới hạn ghi lại.** Mất một blob đã dedup ảnh hưởng mọi job của tenant cùng tham chiếu blob đó. Đây là hệ quả
vốn có của dedup theo nội dung. Mất ổ đĩa hay blob nằm ngoài failure scope đã khóa (AGENTS: "không bao gồm mất ổ
đĩa/corruption"), nên đây không phải finding sản phẩm; sản phẩm fail closed đúng.

**Trạng thái: CLOSED.**

### REM-R08: precondition "first workload snapshot" của test Docker K5 fail một lần trong lượt r3 (mới, test harness, phát hiện ở vòng 2)

**Mô tả.** Lượt toàn suite `docker-r3-20260929T232319Z` (image r2, cây `src` cuối): 23 passed, 1 failed. Test fail
là `test_rem_b14_k5_full_output_fails_the_checkpoint_as_internal_without_a_retry`, lỗi
`AssertionError: first workload snapshot: last=False` (`tests/docker/test_rem_b14_k5_output_full.py:148`), raw
[`raw/REM-R3-docker-L.out`](raw/REM-R3-docker-L.out).
- Test dừng ở precondition: trong 30 s sau khi attempt 1 `RUNNING`, lệnh `docker exec` (UID workload) không thấy
  `/output/state.json`. Bước SIGSTOP, lấp `/output` và mọi assertion B14-K5 phía sau chưa chạy.
- Cùng test, cùng image, pass ở lượt r2 ([`raw/REM-R2-k5.json`](raw/REM-R2-k5.json)). Workload CPU ghi
  `state.json` khoảng mỗi 1 s (`state_interval_seconds` 1,0).
- Helper `_wait` cũ chỉ giữ giá trị bool, nên lượt r3 không để lại exit code/stderr của `docker exec`, trạng thái
  container hay danh sách process. Harness B14 không giữ log worker.

**Điều tra (2 vòng, theo §12).**
1. Vòng 1: thêm chẩn đoán vào test, rồi chạy riêng test K5 trên L. `_wait_for_first_snapshot` giữ nguyên
   predicate, bound 30 s và nhịp 0,2 s; khi hết hạn nó báo exit code và stderr của lần `docker exec` cuối,
   `docker inspect` (status, exit code) và `docker top` (pid, uid, args). Kết quả: **1 passed, 73,79 s**.
2. Vòng 2: chạy lại theo đúng thứ tự suite (`test_rem_b11_h01_claim_window.py` ngay trước K5): **2 passed,
   237,73 s**. Tiếp theo là 5 lượt K5 liên tiếp: **5/5 passed** (73,37–76,66 s).

Raw: [`raw/REM-R3-k5-reruns.out`](raw/REM-R3-k5-reruns.out). Không tái hiện trong 7 lượt, nên nguyên nhân chưa
xác định bằng dữ liệu. Các giả thuyết còn mở (workload khởi động chậm dưới tải, `docker exec` lỗi tạm thời, attempt
kết thúc sớm) chưa phân biệt được vì lượt r3 không có dữ liệu.

**Sửa (chỉ chẩn đoán, không đổi `src`, không nới assertion).** Như vòng 1: nếu lỗi lặp lại, message tự nêu
cơ chế. Test không thêm retry hay kéo dài thời gian chờ.

**Lượt cuối.** Toàn suite r4 (tests có chẩn đoán): K5 passed ([`raw/REM-R4-k5.json`](raw/REM-R4-k5.json)); toàn
suite r5 (cây cuối): K5 passed ([`raw/REM-R5-k5.json`](raw/REM-R5-k5.json)). Precondition không fail lại nên chẩn đoán chưa có dữ liệu.

**Điều kiện đóng.** (1) Nguyên nhân xác định bằng dữ liệu. (2) Nếu là lỗi harness thì sửa harness; nếu là lỗi sản
phẩm thì mở finding riêng. Điều kiện (1) chưa đạt: các lượt sau chẩn đoán đều pass.

**Trạng thái: BLOCKED.**
- Thiếu: dữ liệu của một lần tái hiện.
- Hành động tối thiểu: khi test fail lại ở "first workload snapshot", đọc `exec_rc`, `exec_stderr`, `container`
  và `processes` trong message rồi sửa theo cơ chế đó.
- Không ảnh hưởng trạng thái B14-K5: điều kiện đóng của nó (Docker L, `/output` đầy ⇒
  `INTERNAL/CHECKPOINT_STORAGE_FAILED`, retry 0) đã pass ở r2 và ở mọi lượt chạy lại trên.

### REM-R09: fault injection B14 làm hỏng checkpoint của scenario sau qua blob dùng chung (mới, test harness, phát hiện ở vòng 2)

**Mô tả.** Lượt toàn suite `docker-r4-20260930T005422Z` (image r2, cây `src` cuối): 23 passed, 1 failed, raw
[`raw/REM-R4-docker-L.out`](raw/REM-R4-docker-L.out).
- Test fail: `test_b14_cpu_checkpoint_crash_resume_corruption_and_adoption`, scenario 5b (kill ngay sau khi publish
  checkpoint commit).
- Lỗi: `assert None == {'checkpoint_id': '01a0efd5-0ff0-…', 'sequence': 1}`
  (`tests/docker/test_b14_checkpoint_restore.py:1161`).
- Attempt 2 không restore gì, dù checkpoint seq 1 của attempt 1 đã commit. Job vẫn `SUCCEEDED` với checksum đúng.
- Cùng scenario pass ở r2 và r3 ([`raw/REM-R3-b14.json`](raw/REM-R3-b14.json)).

**Nguyên nhân (1 vòng điều tra, có dữ liệu).**
- Harness không giữ timeline của scenario fail, và DB test đã bị các test sau reset. Nhưng thư mục tạm pytest trên
  L vẫn còn storage root của test B14.
- Đọc các manifest checkpoint trong đó (chỉ ID, step, checksum) cho thấy:
  - state file của checkpoint 5b (`01a0efd5-0ff0-…`, step 44 564 480) có checksum `sha256:125aee55…`;
  - checkpoint seq 1 của scenario 4 `fallback_input` (`01a0efd2-ed5e-…`) cũng ở step đó, cùng checksum;
  - blob trên đĩa bị lật byte 0.
  Raw: [`raw/REM-R4-b14-dedup-analysis.json`](raw/REM-R4-b14-dedup-analysis.json). Cặp baseline/`01a0efd5-43f2-…`
  (cùng checksum, blob nguyên) cho thấy trùng blob giữa các job là chuyện thường.
- Cơ chế:
  - artifact COMMITTED được dedup theo tenant, checksum, kind và media type (B07,
    `application/artifact_service.py`, khoảng :545–558);
  - workload CPU chụp state ở biên stride (65 536 bước), nên checkpoint của hai job có thể có state giống hệt nhau;
  - `corrupt_state_blob` (scenario 3 và 4) lật byte blob tại chỗ và không bao giờ trả lại. Checkpoint 5b trỏ vào
    blob đã hỏng đó.
- Claim của attempt 2 đọc blob, thấy lệch checksum, đánh dấu `CHECKPOINT_CHECKSUM_MISMATCH` rồi fall back về input
  (template restart-safe). Sản phẩm fail closed đúng. Lỗi nằm ở harness: fault injection lọt sang scenario sau.
  Cùng họ với REM-R07.
- Tần suất phụ thuộc thời điểm checkpoint rơi vào đúng step của một checkpoint đã bị lật, nên chỉ fail thỉnh
  thoảng.

**Sửa (chỉ test, không đổi `src`).**
- `tests/docker/test_b14_checkpoint_restore.py`:
  - `corrupt_state_blob` trả byte gốc và mode; `repair_state_blob` ghi lại (`O_TRUNC`, `fsync`, mode cũ);
    `state_blob_intact` so checksum blob với checksum của artifact;
  - scenario 3 và 4 assert blob còn nguyên trước khi lật, rồi sửa lại và assert đã nguyên **sau khi** assert xong
    kết cục của scenario (dấu corrupt trong DB là một chiều nên không đổi);
  - 5a và 5b assert blob state của checkpoint mới nhất còn nguyên trước khi assert restore, nên cùng lỗi harness này
    không thể làm scenario fail sai chỗ lần nữa.
- `tests/docker/b16_support.py`, `tests/docker/test_b16_workloads.py` (cùng nguyên nhân, chưa gây lỗi):
  - `corrupt_blob` của D3 trả byte và mode; `repair_blob` ghi lại và kiểm lại byte;
  - D3 sửa blob `model.safetensors` sau khi assert xong.
- Không assertion nào bị nới: scenario 3/4/5a/5b và D3 giữ nguyên mọi assertion cũ, chỉ thêm precondition.

**Đỏ/xanh.**
- Đỏ: lượt r4 ở trên (5b `None`).
- Xanh: lượt toàn suite `docker-r5-20260930T015931Z` trên cây cuối (tests `b05af4a7…`): **24 passed, 2448,70 s,
  EXIT=0** ([`raw/REM-R5-docker-L.out`](raw/REM-R5-docker-L.out)). B14 7 scenario `SUCCEEDED` cùng checksum, 5a/5b
  qua guard blob còn nguyên ([`raw/REM-R5-b14.json`](raw/REM-R5-b14.json)); D3 bitwise bằng baseline
  ([`raw/REM-R5-B16-training.json`](raw/REM-R5-B16-training.json)).

**Giới hạn ghi lại.** Giống Giới hạn 12: một blob hỏng trên đĩa ảnh hưởng mọi artifact cùng tenant dùng chung nó.
Upload mới trùng nội dung không tự thay blob hỏng. Corruption nằm ngoài failure scope đã khóa; claim phát hiện và
fall back công khai, đúng hành vi.

**Điều kiện đóng.** (1) Nguyên nhân xác định bằng dữ liệu ✓. (2) Lỗi harness được sửa tại gốc (fault injection
có repair, guard trước assertion), không nới assertion ✓. (3) Toàn suite Docker L trên cây cuối xanh ✓.

**Trạng thái: CLOSED.**

### REM-R10: test runner mới để lại thư mục socket tạm trong `/tmp` (mới, test hygiene, phát hiện khi dọn VPS1)

**Mô tả.** Khi kiểm kê VPS1 trước khi dọn, `/tmp` còn 12 thư mục `nexa-r14-*` và 2 thư mục `nexa-rem-r14-*`, tạo
trong các lượt PG r2 và r3 của task. Mac có 274 và 31 thư mục như vậy.
- `nexa-rem-r14-*` tạo bởi `test_unconfirmed_watchdog_stop_fails_closed_within_a_bound` trong file mới của task
  `tests/workloads/test_rem_b15_r10_r14_runner.py`.
- `nexa-r14-*` tạo bởi helper `_serving` của `tests/workloads/test_runner.py`, có từ trước task (HEAD `a6c38bf`).
  File mới của task gọi helper này 3 lần nữa.
- Hai chỗ đều `tempfile.mkdtemp(dir="/tmp")` (đường dẫn AF_UNIX ngắn cho macOS) và không xóa.

**Nguyên nhân.** Thư mục tạm chỉ được tạo, không có teardown. Không ảnh hưởng kết quả test, nhưng mỗi lượt để lại
rác ngoài thư mục tạm của pytest, và task đã làm số rác tăng lên.

**Sửa (chỉ test).**
- `tests/workloads/test_runner.py`: `_control_socket_path(prefix)` tạo thư mục và ghi lại;
  `_remove_control_socket_directories()` xóa; fixture autouse `_control_socket_cleanup` gọi nó sau mỗi test.
  `_serving` dùng `_control_socket_path()`.
- `tests/workloads/test_rem_b15_r10_r14_runner.py`: dùng cùng helper (prefix `nexa-rem-r14-`) và fixture autouse
  của module.
- Không assertion nào đổi; đường dẫn socket vẫn ngắn như cũ.

**Đỏ/xanh.**
- Đỏ: trên Mac, một lần chạy `tests/workloads/test_rem_b15_r10_r14_runner.py` (10 passed) để lại 1 thư mục
  `nexa-rem-r14-*` và 4 thư mục `nexa-r14-*`.
- Xanh: chạy lại hai file (49 passed), rồi toàn suite default (1772 passed, 599 skipped), không thêm thư mục nào.
- Trên L, lượt PG r6 đếm số thư mục trước và sau: 14 → 14 (14 thư mục còn lại từ r2/r3, không thêm thư mục nào; raw [`raw/REM-R6-socket-dirs.txt`](raw/REM-R6-socket-dirs.txt)).

**Điều kiện đóng.** (1) Test không để lại thư mục socket ✓ (Mac); L r6 không thêm thư mục ✓. (2) Suite liên quan xanh
trên cây cuối: default ✓, PG L r6 2343 passed, 4 skipped ✓. Torch image chỉ chứa 5 file `test_*_b16.py`; `tests/docker`
không import hai file này, nên lượt torch r2 và Docker r5 không đổi.

**Trạng thái: CLOSED.** Thư mục `nexa-r14-*` trên Mac và VPS1 do các lượt của task tạo ra đã được xóa. Trên Mac còn
48 thư mục `nexa-r14-*` có từ trước task (lượt test của task trước), không xóa.

## Vòng 2: phản hồi Task Review

Task Review vòng 1: **Không duyệt** (RV01–RV04 chặn, RV05–RV07 không chặn). Vòng 2 chỉ sửa đúng các điểm được nêu.
RV03 theo lựa chọn của owner: "Làm đúng cơ chế".

| Review | Nội dung | Xử lý ở vòng 2 | Ở đâu |
|---|---|---|---|
| RV01 (chặn) | REM-R05 chưa đóng; B15 C1 fail với image final | Tìm thêm các đường còn hành động sau cleanup đã verify (scan, IPC, result, failure, offer) ⇒ REM-R06; sửa và test tất định; C1/C8 chạy lại với image r2 | REM-R06; "Toàn suite Docker L" (M10) |
| RV02 (chặn) | D6/D6b chưa chạy lại với image final | Chạy lại D4/D5/D6/D6b trên L với image r2. Lượt toàn suite lộ lỗi harness (D6 làm mất blob dùng chung với D6b) ⇒ REM-R07, sửa test rồi chạy lại riêng test inference: 1 passed. Thay đổi test D6/D6b ở "Test changes" | B16-R21; REM-R07; "Toàn suite Docker L"; "Test changes" |
| RV03 (chặn) | Chunk đã recognized vẫn bị tính lại | Làm đúng cơ chế: server download graph, worker tải và mount read-only, runner kiểm, workload `carry_recognized`; test chứng minh không tính lại | B16-R21 "Test vòng 2" |
| RV04 (chặn) | Evidence/M10 thiếu | Closure matrix (31 + REM-R01..R10 + phần P), 5 bảng, tự review, hash `src/nexa`, digest, số đếm cuối, raw L cuối, dòng README/ROADMAP, sửa tham chiếu M9 lỗi thời | M10 |
| RV05 | B11-H01: container `created` chưa start được coi là `NO_CONTAINER` | Ghi ở "Quyết định tự chọn" kèm lý do | "Quyết định tự chọn" |
| RV06 | Số hàm ghi sai | Sửa "6 hàm" | M1 |
| RV07 | Giới hạn chưa ghi | Ghi 4 giới hạn (recognized list ≈684 KB; mất frame K5; bind OBS-01 một lần; `CREATE_IN_FLIGHT` cùng incarnation) | "Giới hạn" (M10) |
| Thiếu test: B11-H01 | image final L và P | Chạy lại trên L với image r2; P không chạy được ⇒ finding chuyển **ENVIRONMENT BLOCKED** (xem dưới) | B11-H01; closure matrix |
| Thiếu test: B15-R39 | đếm `/cleanup` | Đếm `/cleanup` theo attempt trong log worker của lượt L cuối | B15-R39; "Toàn suite Docker L" |
| Thiếu test: B14-K5 | Docker `/output` nhỏ | Kịch bản K5 trong lượt L cuối | B14-K5 |
| Thiếu test: B15-R14 | D6 chạy lại | D6 trong lượt L cuối | B15-R14 |
| Thiếu test: B15-R11 | P | P không chạy được ⇒ **ENVIRONMENT BLOCKED** | B15-R11; closure matrix |
| Thiếu test: REM-R04 | P | Điều kiện đóng của REM-R04 là tính di động amd64 (đã pass L). P chạy lại thuộc phần P chung của M9, ENVIRONMENT BLOCKED | REM-R04; closure matrix |
| Thiếu test: PG, torch | số đếm cuối | Toàn suite PG và torch trên cây cuối | "Số đếm cuối" (M10) |

**Đổi trạng thái so với vòng 1 (không phải mở lại vì evidence mới, mà áp đúng quy tắc §11).** Vòng 1 ghi B11-H01,
B15-R39, B15-R11, B16-R29 là CLOSED kèm ghi chú "P ENVIRONMENT BLOCKED". Thẻ của bốn finding này ghi điều kiện đóng
chạy "trên P và L" (hoặc "Docker P/L", "pass trên L và P"). §11 đòi mọi điều kiện đóng pass để CLOSED, nên vòng 2
chuyển cả bốn sang **ENVIRONMENT BLOCKED**. Mọi phần khác của điều kiện đóng đã pass trên L; hành động tối thiểu ở
closure matrix.

## M10 — Toàn suite, image cuối, bảng, closure matrix

### Cây và image cuối

- Listing hash `src/nexa` (`find src -type f -name "*.py" -print0 | sort -z | xargs -0 sha256sum | sha256sum`;
  Mac dùng `shasum -a 256`): **`27857313e4d6ddc7a94535c8a24076fd91e120a7d90de788a107da8fa091c965`**, trùng trên Mac
  và cây rsync trên VPS1. Mọi lượt dưới đây chạy trên cây này.
- Listing hash `tests` (cùng lệnh, thư mục `tests`) của cây cuối: **`258a3a4d9b5b80c6fcea6472c28db2baf443956df40f976e0713b560ae1eba1b`**,
  trùng trên Mac và VPS1. Hash này có sau ba lần sửa test cuối: chẩn đoán K5 (REM-R08), harness REM-R09 và thư mục
  socket REM-R10.
  - Lượt PG L cuối r6 và lượt default cuối chạy trên cây này.
  - Lượt Docker L cuối r5 chạy trên cây `b05af4a7…` (trước REM-R10). REM-R10 chỉ đổi
    `tests/workloads/test_runner.py` và `tests/workloads/test_rem_b15_r10_r14_runner.py`; `tests/docker` không
    import hai file này.
  - Lượt torch r2 chạy trước cả ba lần sửa. Image torch chỉ chứa 5 file `tests/workloads/test_*_b16.py`, không
    file nào đổi.
- Lockfile không đổi: `git diff --stat -- uv.lock pyproject.toml web/pnpm-lock.yaml web/package.json` rỗng. Không
  thêm dependency.
- Image cuối (r2) build trên L (VPS1), `linux/amd64`, base `python:3.12-slim`
  `sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`, containerd image store nên digest =
  image ID. Không push image.

| Vai trò | Ref | Kiến trúc | Digest / image ID |
|---|---|---|---|
| CPU iterative (runner + workload CPU) | `nexa/rem-cpu-iterative` | amd64 | `sha256:8ecfe240000348e492fbda2774acd78f214416a228bf7399632db686e179645e` |
| Worker | `nexa/rem-worker:r2-amd64` | amd64 | `sha256:897a7219b0610c9fab5e4f954791e7979f23030411fdd10277efc64ae236ac8f` |
| PyTorch CIFAR-10 | `nexa/rem-pytorch-cifar10` | amd64 | `sha256:fb7331027009ab223d0e361e1ba50c563ce6e033e7eacdee72960f010901f2ec` |
| Batch inference | `nexa/rem-batch-inference` | amd64 | `sha256:32e963d043bab5b07952cc90034985208eb601e9236b06cf9bd04b6efa0e3449` |
| Test torch (chỉ để chạy `tests/workloads`) | `nexa/rem-b16-torch-tests:r2` | amd64 | `sha256:5e35aa9f4b4505a551e58c90a1f260bc675a2eaf5ce1bd83eb86e23acb1367bf` |
| arm64 (P) | — | arm64 | **không build**: P ENVIRONMENT BLOCKED (ENV-01) |

Raw: [`raw/REM-R2-images.jsonl`](raw/REM-R2-images.jsonl).

### Lệnh chạy cuối trên L

Tuần tự trong tmux trên VPS1, không chạy song song, PostgreSQL 17 container riêng bind 127.0.0.1, DB tiền tố
`nexa_b05_test_`, harness loopback (`NEXA_DOCKER_WORKER_NETWORK=host`):

1. Torch: `docker run --rm --network none --memory 4g --cpus 2 --read-only --tmpfs /tmp:rw,size=512m -e
   NEXA_B16_DATA_DIR=/data -v <fixture B16>:/data:ro nexa/rem-b16-torch-tests:r2 tests/workloads -rs`.
2. Docker (lượt cuối r5, chỉ Docker; r3 chạy Docker rồi PG): `NEXA_RUN_DOCKER=1 NEXA_B11_RUNTIME_EVIDENCE=1 NEXA_B10_RUNTIME_EVIDENCE=1 NEXA_B09_IMAGE_REF=<CPU>
   NEXA_B11_WORKER_IMAGE=<worker> NEXA_B16_PYTORCH_IMAGE_REF=<PyTorch> NEXA_B16_INFERENCE_IMAGE_REF=<inference>
   NEXA_B16_DATA_DIR=<fixture B16> NEXA_B15_WORKER_LOG_OUT=<log worker, ngoài repo> NEXA_B15_EVIDENCE_OUT=…
   NEXA_B14_EVIDENCE_OUT=… NEXA_B16_EVIDENCE_OUT=… NEXA_REM_K5_EVIDENCE_OUT=… NEXA_TEST_DATABASE_URL=<guarded>
   PYTHONPATH=src:. uv run --no-sync pytest --run-postgres -q -p no:cacheprovider tests/docker -rs`.
3. PG (lượt cuối r6, sau REM-R10): `NEXA_TEST_DATABASE_URL=<guarded> PYTHONPATH=src:. uv run --no-sync pytest --run-postgres -q
   -p no:cacheprovider tests --ignore=tests/docker -rs`.

Local (Mac): `PYTHONPATH=src:. uv run --no-sync pytest -q -p no:cacheprovider`; `uv run --no-sync ruff check .`;
`uv run --no-sync ruff format --check .`; `git diff --check`.

### Toàn suite Docker L

Năm lượt `tests/docker` trên L với image r2, theo thứ tự:

| Lượt | Cây | Kết quả | Ghi chú |
|---|---|---|---|
| `docker-r2-20260929T220051Z` | `src` cuối, test trước REM-R07 | **1 failed, 23 passed, 2393,58 s, EXIT=1** | failure duy nhất là D6b ⇒ REM-R07 (lỗi harness); raw [`raw/REM-R2-docker-L.out`](raw/REM-R2-docker-L.out) |
| `docker-r2b-20260929T231201Z` | `src` cuối, test sau REM-R07 | **1 passed, 213,42 s, EXIT=0** (chỉ `test_b16_inference_chunks_resume_and_the_output_directory_is_bounded`) | raw [`raw/REM-R2b-docker-L-b16-inference.out`](raw/REM-R2b-docker-L-b16-inference.out) |
| `docker-r3-20260929T232319Z` | `src` cuối, test sau REM-R07, trước REM-R08/R09 | **1 failed, 23 passed, 2398,61 s, EXIT=1** | failure duy nhất: precondition K5 ⇒ REM-R08; raw [`raw/REM-R3-docker-L.out`](raw/REM-R3-docker-L.out) |
| `docker-r4-20260930T005422Z` | `src` cuối, test sau REM-R08, trước REM-R09 (tests `5565b433…`) | **1 failed, 23 passed, 2359,07 s, EXIT=1** | failure duy nhất: B14 5b ⇒ REM-R09 (lỗi harness); K5 passed; raw [`raw/REM-R4-docker-L.out`](raw/REM-R4-docker-L.out) |
| `docker-r5-20260930T015931Z` | **cây cuối** (`src` + tests; hash ở trên) | **24 passed, 2448,70 s, EXIT=0** | lượt cuối; raw [`raw/REM-R5-docker-L.out`](raw/REM-R5-docker-L.out) |

Kết quả theo scenario của lượt cuối r5 (evidence JSON do harness ghi, đã lọc path). Checksum khác r3 vì input
sinh mới mỗi lượt; điều cần đối chiếu là các scenario trong cùng lượt có cùng checksum:

| Nhóm | Raw | Kết quả |
|---|---|---|
| B15 control/recovery (C0–C10) | [`raw/REM-R5-evidence.json`](raw/REM-R5-evidence.json), [`raw/REM-R5-evidence-not-restart-safe.json`](raw/REM-R5-evidence-not-restart-safe.json) | `failed_scenario` = null; `not_run` chỉ `C8_log_flood`, `C8_oom` (như B15). C0/C1/C3/C4/C6/C9 `SUCCEEDED` cùng checksum `sha256:e81a6176…`; C2 và C5a `CANCELLED`; C7a `SUCCEEDED`; C8 `FAILED` (`TIMEOUT/RUNTIME_LIMIT_REACHED`, runtime limit 38 s); C3 và C9 attempt 1 `LOST/LEASE_EXPIRED`, attempt 2 `SUCCEEDED`; C9 `same_worker_incarnation` = true, runner cũ dừng trước lease hết 4,987 s; C10 epoch 1 → 2, 2 job `SUCCEEDED`, `retry_counts` [0, 0]; C5b `FAILED` (`RUNNER_UNAVAILABLE`), C7b `SUCCEEDED` |
| B14 checkpoint/restore | [`raw/REM-R5-b14.json`](raw/REM-R5-b14.json) | 7 scenario (`baseline`, `corrupt_newest`, `crash_resume`, `fallback_input`, `kill_after_publish`, `kill_before_first_and_between`, `worker_restart_adoption`) đều `SUCCEEDED` cùng checksum `sha256:2be85cab…`; reservation mở khi adopt kết thúc `COMMITTED`; 5a/5b qua guard `state_blob_intact` (REM-R09) |
| B16 training | [`raw/REM-R5-B16-training.json`](raw/REM-R5-B16-training.json) | D2, D3: `metrics.json` và `model.safetensors` bitwise bằng baseline; D7 sweep `replay_identical` = true |
| B16 inference | [`raw/REM-R5-B16-inference.json`](raw/REM-R5-B16-inference.json) | D5 restore sau khi recognized 16, summary bitwise bằng; D6 (blob chunk bị xóa) `FAILED`, attempt 2 `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`, recognized 16 → 16; D6b fallback checkpoint trước, carry-forward 8 chunk, summary bitwise bằng |
| B16 OOM | [`raw/REM-R5-B16-oom.json`](raw/REM-R5-B16-oom.json) | training và inference: 1 attempt `OOM/CONTAINER_OOM`, retry 0 |
| B11-H01 | [`raw/REM-R5-rem-b11-h01.json`](raw/REM-R5-rem-b11-h01.json) | cửa sổ claim: attempt `CLAIMED`, không container (`started` = false), không journal record |
| B15-R11 | [`raw/REM-R5-rem-b15-r11.json`](raw/REM-R5-rem-b15-r11.json) | `killed_runner`: attempt 1 `INFRASTRUCTURE/RUNNER_UNAVAILABLE`, attempt 2 `SUCCEEDED`; `paused_runner`: attempt 1 `TIMEOUT/STARTUP_TIMEOUT`, job `FAILED` |
| B14-K5 | [`raw/REM-R5-k5.json`](raw/REM-R5-k5.json) | r5: `/output` đầy (0 block trống), attempt 1 `INTERNAL/CHECKPOINT_STORAGE_FAILED`, job `FAILED`, không retry. r3 fail ở precondition (REM-R08), r4 passed ([`raw/REM-R4-k5.json`](raw/REM-R4-k5.json)) |

Lượt r3 (cùng `src`, trước REM-R08/R09) có cùng kết quả ở mọi nhóm trừ K5, raw `raw/REM-R3-*.json`.

Log worker của harness B15 (`NEXA_B15_WORKER_LOG_OUT`, ngoài repo; chỉ B15 ghi log worker), đếm theo attempt:

| Lượt | Attempt | `/cleanup` 200 mỗi attempt | Request `claim`/`adopt`/`fail`/`renew`/`start`/`/cleanup` sau cleanup đã verify | `JournalCorruption` | `RUNNER_PROTOCOL_ERROR` (toàn suite) |
|---|---|---|---|---|---|
| r2 | 29 | đúng 1 | 0 (1 reserve checkpoint `409`, REM-R06 "Quan sát còn lại") | 0 | 0 |
| r3 | 29 | đúng 1 | 0 (2 reserve checkpoint `409`, 54 ms và 56 ms, Giới hạn 13) | 0 | 0 |
| r5 | 29 | đúng 1 | 0 (1 reserve checkpoint `409`, 59 ms, Giới hạn 13) | 0 | 0 |

Log này gồm worker của `test_b15_control_recovery.py` (2 test) và `test_rem_b15_r11_start_window.py` (cùng harness
B15). Cảnh báo `worker_loop_failed` trong log (r2: 94 dòng; r3: 93 dòng; r5: 94 dòng) đã được phân loại, đều là hành vi chờ
đợi của scenario, không dòng nào là `JournalCorruption`:
- `WorkerTransportError` (heartbeat/poll/reconcile/result/renew): r2 có 35 dòng, 34 dòng nằm trong phút C9
  (relay `sever`, worker mất control plane có chủ đích), 1 dòng `reconcile_once` trong test B15-R11; r3 có 48 dòng
  (29 `_result_once`, 9 `reconcile_once`, 9 heartbeat, 1 `renew_once`), cả 48 dòng nằm trong 23:39:42–23:40:26,
  tức cửa sổ `sever` của C9 (lease cũ hết 23:40:25); r5 có 34 dòng (15 `_result_once`, 9 `reconcile_once`,
  9 heartbeat, 1 `renew_once`), cả 34 dòng nằm trong 02:16:03–02:16:47, cửa sổ `sever` của C9 (lease cũ hết 02:16:46);
- `_poll_once 409/state_conflict` (r2 20 dòng, r3 19 dòng, r5 23 dòng, rải đều): server từ chối poll khi worker chưa `READY` hoặc
  `DISABLED` (`worker_service.py` :949–962), ví dụ C6 hay lúc attempt còn unresolved chờ reconcile;
- `_poll_once RunnerControlError` (một chuỗi ~1/s, 30 s; r3 24 dòng, r5 24 dòng): case âm tính của B15-R11: container bị `docker pause`,
  không chứng minh được exit ⇒ worker giữ replay đến hết budget 30 s rồi `STARTUP_TIMEOUT` (`/fail` rồi một
  `/cleanup`);
- `409/stale_authority` (`_result_once`, `renew_once`; r3 2 dòng `renew_once`; r5 11 dòng `_result_once`, 2 dòng `renew_once`): gửi trước cleanup, lúc server vừa thu hồi lease
  (REM-R06 "Quan sát còn lại").

### Số đếm cuối

Mọi lượt trên cây cuối (hash `src/nexa` ở trên; hash `tests` của từng lượt ở "Cây và image cuối"), không có test nào fail.

| Suite | Nơi | Lệnh | Kết quả | Baseline (`a6c38bf`) | Giải thích chênh lệch skip |
|---|---|---|---|---|---|
| Default | Mac | `PYTHONPATH=src:. uv run --no-sync pytest -q -p no:cacheprovider` | **1772 passed, 599 skipped, 46,70 s, EXIT=0** (cây cuối) | 1588 passed, 523 skipped | +76 skip: 74 case trong file `test_rem_*` mới cần `--run-postgres` hoặc Docker opt-in, +2 test PG mới trong `tests/benchmarks/test_b13_queue_microbench.py`. Lý do skip không đổi: 123 dòng "requires --run-postgres", 11 "opt-in real Docker suite", 1 "opt-in B11 Linux Docker relay", 1 module torch (tổng 599 case) |
| PG | L | `pytest --run-postgres -q -p no:cacheprovider tests --ignore=tests/docker -rs` | **2343 passed, 4 skipped, 910,74 s, EXIT=0** (r6) | 2089 passed, 22 skipped (cả `tests/docker`) | Baseline chạy cả `tests/docker` (21 test lúc HEAD, skip vì Docker opt-in) + 1 module torch = 22. Lượt cuối tách `tests/docker` ra lượt Docker L; 4 skip = 1 module torch (`test_pytorch_workloads_b16.py:17`) + 3 case `pg_dump`/`pg_restore` (`test_rem_b13_r12_search_path.py:446`, chạy riêng có `NEXA_TEST_PG_CLIENT_PREFIX` ở M1: 13 passed) |
| Docker | L | `tests/docker -rs` (lệnh ở trên) | **24 passed, 2448,70 s, EXIT=0** (r5, [`raw/REM-R5-docker-L.out`](raw/REM-R5-docker-L.out)) | Mac Docker 17 passed (P, trước ENV-01) | 24 test (21 lúc HEAD + 3 file `test_rem_*` Docker); P không chạy được (ENV-01) |
| Torch | L (image `nexa/rem-b16-torch-tests:r2`) | `tests/workloads -rs` | **162 passed, 13,83 s, EXIT=0** ([`raw/REM-R2-torch.out`](raw/REM-R2-torch.out)) | 153 passed | +9 test mới (DOC-01, RV03), 0 skip; `tests/workloads` không đổi sau lượt này |
| Ruff | Mac | `uv run --no-sync ruff check .` / `ruff format --check .` | All checks passed / 500 files already formatted | — | — |
| Hygiene | Mac | `git diff --check` | sạch | — | — |

Raw PG cuối: [`raw/REM-R6-pg-full.out`](raw/REM-R6-pg-full.out). Lượt PG r3 (trước REM-R10): **2343 passed, 4 skipped,
900,08 s, EXIT=0** ([`raw/REM-R3-pg-full.out`](raw/REM-R3-pg-full.out)). Lượt PG r2 trước đó (trước test REM-R07 mới):
**2342 passed, 4 skipped, 898,75 s, EXIT=0** ([`raw/REM-R2-pg-full.out`](raw/REM-R2-pg-full.out)).

### Quyết định tự chọn

| Finding | Quyết định | Lý do / căn cứ |
|---|---|---|
| Toàn task | P (Docker Desktop) ENVIRONMENT BLOCKED suốt task; mọi PG/Docker chạy trên L. Finding có điều kiện đóng bắt buộc "trên P và L" (B11-H01, B15-R39, B15-R11, B16-R29) có trạng thái cuối ENVIRONMENT BLOCKED dù phần L đã pass; thẻ ghi P "nếu khả thi"/"nếu Mac cho phép" (B14-K5, ENV-01) không coi P là điều kiện bắt buộc | VM của P cạn PID vì `nexa_b10_smoke3-caddy-1` của task trước; task không được đụng `nexa_b10_*` hay cấu hình Docker Desktop (§10.A) |
| Harness Docker | Chế độ loopback opt-in (`NEXA_DOCKER_WORKER_NETWORK=host`): API bind 127.0.0.1, worker dùng host network; B15 C9 dùng relay TCP 127.0.0.1 có `sever`/`restore` (RST) thay cho `docker network disconnect` | Firewall host của VPS1 chặn container → host qua `docker0`; không được sửa ufw; bind 0.0.0.0 bị từ chối. Mặc định (bridge) không đổi cho P |
| B13-R12 | Qualify lời gọi nội bộ của 6 helper Decimal theo schema thật (`pg_proc`), không `SET` (giữ inline); ghim `search_path` cho 39 hàm trigger/helper khác | Không REINDEX/validate lại, kết quả hàm không đổi (Hypothesis), restore thuần chạy được |
| B11-H01 (RV05) | Container đúng startup identity mà Docker báo `created` (chưa từng start) và không bound: gỡ rồi gửi `NoContainerProof` (`NO_CONTAINER`), không dùng `ContainerStoppedProof` | Thẻ finding: "NO_CONTAINER, hoặc ContainerStoppedProof khi container thật tồn tại". Server chỉ nhận `ContainerStoppedProof` cho container đã có container row (bound, `execution_cleanup.py`); container chưa bound chưa từng có row và chưa từng chạy workload, nên sau khi gỡ và tombstone thì "không có container" là sự thật được chứng minh (scan theo identity + tombstone trước scan cuối), không suy từ lease expiry hay một lần scan rỗng. Container đã bound vẫn đi qua `cleanup` với stopped proof; container đã start mà không bound ⇒ unresolved (fail closed) |
| B15-R39 | Thứ tự khóa: khóa journal attempt → khóa nội bộ `PendingOperationStore`; không giữ khóa hai attempt | Tránh deadlock, ghi ở `docs/worker-agent.md` |
| B15-OBS-01 | Giữ sửa phòng thủ (reader lấy khóa attempt) và chẩn đoán `cause/length/sha256_16`; trạng thái ENVIRONMENT BLOCKED | Thẻ: không xác định được cơ chế và cảnh báo chỉ xảy ra trên Docker Desktop |
| ENV-01 | `init: true` là sửa root cause; `pids_limit: 512` chỉ là lưới an toàn | Thẻ ENV-01 |
| B15-OBS-02 | Trong `ADMISSION_OFF`, scheduler không ra quyết định nào (không offer, không tạo/hủy reservation); reaper/retry promotion/retention/heartbeat vẫn chạy. `poll` chỉ từ chối `WRITE_FROZEN` | SM:110 "reject … new dispatch"; reservation chỉ phục vụ dispatch; tiền lệ DRAINING (B15-R22) |
| B15-R11 | Chỉ exit đã được Docker chứng minh (inspect state/exit của đúng container bound) kết thúc startup sớm; container còn chạy/không inspect được ⇒ giữ replay; kiểm cả khi budget 30 s hết | §5.C :581: thời gian trôi không phải bằng chứng exit |
| B15-R11 (Docker test) | Chuỗi holder khóa hàng Job (claim, tải input, `/start`) để đóng băng worker đúng giữa commit `/start` và đọc ACK; âm tính bằng `docker pause`; chờ dispatch 30 s có diagnostics | Tất định, không sleep; lượt đầu 10 s timeout một lần trước dispatch (nhịp tick/poll, không phải lỗi sản phẩm) |
| B15-R14 | Exit status 90–96 theo lý do dừng khi không worker nào ACK `STOPPED` (tránh 0/78/124/128+n); worker map exit status giống frame (`runner_stop_failure`); watchdog stop không xác nhận ⇒ `fail_closed` | Lý do dừng không mất khi frame mất; PID 1 kết thúc trong bound |
| B15-R10 | `STARTUP_LIMIT` thêm vào `STOPPED.reason` trong `schema_version` 1 (additive); `REQUEST_STOP` không nhận; worker image nâng không muộn hơn runner (cùng source) | openapi :1339 tách `STARTUP_TIMEOUT` |
| REM-R01 | Chỉ báo cáo `EXIT` của supervisor xác nhận stop; reset/đứt stream ⇒ fail closed | Không phát `STOPPED` giả |
| B16-R21 | `ExecutionContext.recognized_chunks` (additive, chỉ template chunked): dãy liên tục từ cursor restore (0 khi fallback), `null` khi blob sau cursor không đọc được ⇒ `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE` trước container | §5.A; không nới `_same` |
| B16-R21 (RV03, user chọn "Làm đúng cơ chế") | Attempt tải file recognized qua route execution-artifact, kiểm size/checksum, mount read-only `/input/recognized/`; runner kiểm trước launch; workload lấy chunk đó từ file (không tính lại) và chỉ tính chunk sau; workload ghi output cho chỉ số recognized ⇒ `CHUNK_OUTPUT_CONFLICT` | Quyết định của user ở Task Review vòng 1 (RV03) |
| B16-R21 | Cả hai lý do thuộc `INTERNAL`; `ErrorResponse.reason` optional (safe code) để worker phân loại 409 tất định; 409 không reason giữ replay | Bảng contract: "conflicting chunk/result" → `INTERNAL`, không retry mù; §5.C |
| B16-R21 | Giữ bound pending store 1 MiB: list recognized tối đa (2048 mục gọn) vừa một claim; có test | Không mất im lặng: vượt bound thì fail closed (xem Giới hạn) |
| REM-R02 | Reservation không nhận batch nào không bị coi là thiếu batch | Chu kỳ checkpoint 0 chunk mới là hợp lệ |
| B14-K5 | Mã additive `INTERNAL/CHECKPOINT_STORAGE_FAILED` chỉ cho ENOSPC/EDQUOT/EIO ở stage checkpoint; `OSError` khác giữ exit 124; đường result không đổi; không sửa openapi (`reason_code` chỉ có pattern) | Không mã sẵn có nào đúng nghĩa; PLAN :248–249 (không retry mù, không công nhận file dở) |
| B14-K5 (Docker test) | Tất định: SIGSTOP chỉ process workload CPU (UID 1001, `docker exec`), lấp `/output` về 0 block trước lần staging đầu (interval 60 s = max template); assert chưa reserve gì lúc lấp | Staging sau unlink bản trước nên lấp muộn hơn sẽ racy |
| B16-DOC-01 | Chạy test dẫn chứng torch trong image `nexa/b16-torch-tests:r2` có sẵn trên VPS1 (read-only, network none, mount source read-only) | Torch không có trên Mac |
| B13-OBS-01 | Chỉ đo trên L (PG17); stall do eligibility = replay ticks; `coordinator.md` có đoạn "Measured limit" đánh dấu chờ OD-2 | OD-2 để trống; không tự chọn tiêu chí |
| B15-R32 | `FOR NO KEY UPDATE` trên hàng credential + users (7 chỗ), không đổi schema | §5.C ưu tiên sửa mode/thứ tự khóa; không đường nào đổi `users.user_id` |
| B14-OBS-01 | File identity ở root store + singleton DB insert-only (0022); API chỉ bind khi bằng nhau; lần đầu adopt/tạo; từ chối khi DB tham chiếu blob committed mà `committed/` rỗng; readiness đòi identity đã verify; khôi phục volume đúng cần restart API; không tự re-init | AGENTS: storage không khỏe phải fail closed |
| B15-R33 | WARNING cho callback stale bị từ chối, INFO cho reconcile (chỉ incomplete/adopted/lần complete đầu); counter để B19 | Không có hạ tầng metrics trong src |
| B16-R08 | Lỗi validate header giữ 422; path/query → 400; các assertion `in (400,422)` siết thành 400 | §5.A chỉ nêu path và query |
| B15-R05 | Candidate kế thừa không chứng minh được ⇒ `INCOMPATIBLE/CHECKPOINT_PROVENANCE_MISMATCH`, không đọc/không đánh dấu; chứng minh = cùng tenant + tham chiếu `MANUAL_RETRY` + chuỗi `retry_of_job_id` + checksum spec/input/model/template/adapter/image bằng nhau; `verify_candidate` đọc `candidate["unproven"]` không default | §5.A; fail closed |
| B14-R04 | Tham chiếu ":96–98" của §5.A hiểu là của B16-R04; câu chữ B14-R04 giới hạn ở bước restore 6 + đoạn Relocation, chỉ đường RETRY_WAIT đã có | Không mở rộng phạm vi |
| B15-R06 | Giữ test tái hiện (ghi hành vi hiện tại, docstring nói chưa chọn phương án); không đổi code/contract | OD-1 để trống |
| AUD-01 | Header B04/B05 trỏ ROADMAP, không bịa ngày/ID; giữ câu lịch sử B04:126/B05:179 kèm ghi chú; B07:51, B10:95 không đụng | Thẻ AUD-01 |
| B15-R18 | Record chưa đến hạn được hoãn bằng `expires_at = now() + 1 ngày` (RECHECK), không migration; parent sweep đã xong hoãn tới expiry lớn nhất của con, chưa xong hoãn 1 ngày | Quy tắc xóa không đổi (1 ngày < 30 ngày tối thiểu); giữ quy tắc B16-R05 |
| B14-K1 | Probe đọc không khóa dùng đúng `_candidates`/`_blocked_reason` của nhánh khóa; nhánh khóa không đổi | Mọi ghi vẫn kiểm lại dưới khóa |
| B16-R10 | Bỏ `1e-2` khỏi fixture request; expansion `children` không đổi (chỉ sửa `note`); sweep đã lưu replay nguyên trạng | §5.A "chỉ request mới bị validate" |
| REM-R04 | Giữ ngữ nghĩa khóa journal (B15-OBS-01); test đọc journal dưới UID worker qua `docker exec` | Journal là dữ liệu riêng của worker |
| REM-R05 / REM-R06 | Mọi đường renew/adopt/failure/claim/IPC/result đọc marker bền `cleanup_verified` dưới khóa journal, không dựa `_adopted` trong bộ nhớ | Marker ghi nguyên tử cùng bước bỏ operation, còn sau restart; server giữ `409 stale_authority` |
| REM-R07 | D6 lấy blob chunk ra rồi trả lại (cùng byte, mode) sau khi assert, thay vì dùng tenant riêng cho D6b; D6b kiểm trước rằng chunk carry forward còn nguyên | Giữ nguyên chuỗi scenario và tenant của B16; lỗi thuộc harness, không nới assertion |
| REM-R06 (cửa sổ còn lại) | Không giữ khóa journal quanh callback của result thread; reserve checkpoint gửi trong cửa sổ sau cleanup verify ở thread khác bị server fence từ chối | Giữ khóa khi upload chặn renewal ⇒ rủi ro hết lease lớn hơn; reserve không phải operation bền, không chặn READY |
| Fixture image | Mỗi fixture PyTorch/inference thêm đúng một mục `image.history` cho image cuối (r2), không đổi trường khác, không sửa template version đã đăng ký | Quy ước vòng 2 B16 (`_images` đòi ref = `history[-1]`) |

### Contract edits

Căn cứ theo mục 1 của prompt: (a) lỗi tài liệu rõ ràng; (b) evidence trong repo cho thấy ý định không đổi;
(c) quyết định owner / §5.A / quyết định của user (RV03).

| File:dòng (sau sửa) | Trước → sau | Căn cứ |
|---|---|---|
| `docs/contracts.md:71` | `ErrorResponse` 422 cho mọi lỗi validate → path/query sai kiểu/pattern/format là 400 `validation_failed` | (c) §5.A B16-R08 |
| `docs/contracts/openapi.yaml:1229–1231` (publish checkpoint), `:1302–1304` (complete) | thêm: chunk của Authority hiện tại khác row recognized ⇒ `409 state_conflict` + `reason: CHUNK_OUTPUT_CONFLICT`, không commit gì | (c) §5.A B16-R21 |
| `docs/contracts/openapi.yaml:1314` (execution-artifacts) | route trả byte cho input/model/dataset/restore file → thêm "hoặc recognized chunk file (`recognized_chunks`)" | (c) RV03 |
| `docs/contracts/openapi.yaml:1405` (`BadRequest`) | "Malformed body, duplicate JSON member, unknown field, filter, or cursor" → thêm "path or query parameter of the wrong type, pattern or format" | (c) §5.A B16-R08 |
| `docs/contracts/openapi.yaml:1433` (`ErrorResponse.reason`) | mới: optional, pattern `^[A-Z][A-Z0-9_]{0,63}$`, chỉ có ở operation nêu tên | (c) §5.A/§5.C B16-R21 |
| `docs/contracts/openapi.yaml:1755` (`RetryRequest.checkpoint_id`) | thêm mô tả: checkpoint kế thừa phải chứng minh được nguồn, nếu không 422 `infeasible_request` | (c) §5.A B15-R05 |
| `docs/contracts/openapi.yaml:2036–2068` (`ExecutionContext.recognized_chunks`, `RecognizedChunk`) | mới: dãy recognized từ cursor restore, `null` ⇒ `CHUNK_OUTPUT_UNAVAILABLE`; vòng 2: tải qua route execution-artifact, kiểm, mount `/input/recognized/`, workload chỉ tính chunk sau, output cho chunk recognized ⇒ `CHUNK_OUTPUT_CONFLICT` | (c) §5.A + RV03 |
| `docs/contracts/state-machines.md:30–41` | hàng RECOVERING: điều kiện `INFRASTRUCTURE` cho hai nhánh pause; non-retryable khi desired `RUNNING` hoặc `PAUSED` → `FAILED` | (c) §5.A B15-R02 |
| `docs/contracts/state-machines.md:77–78` | CANCELLING / Attempt `LOST` giữ terminal | (c) §5.A B15-R34 |
| `docs/contracts/state-machines.md:110` | `ADMISSION_OFF`: "reject … new dispatch" → offer đã commit vẫn claim/chạy được, không offer mới | (b) SM:110 "running continues", tiền lệ B15-R22 |
| `docs/contracts/state-machines.md` (guard manual retry) | thêm điều kiện chứng minh nguồn checkpoint kế thừa | (c) §5.A B15-R05 |
| `docs/contracts/internal-interfaces.md:260`, `:272` | `STOPPED.reason` thêm `STARTUP_LIMIT`; `REQUEST_STOP` không nhận | (b) openapi :1339 đã tách `STARTUP_TIMEOUT` |
| `docs/contracts/internal-interfaces.md:295` (reconciliation bước 6) | thêm claim `CLAIMED`/`REVOKED` không container của incarnation cũ → tombstone + scan theo identity → `NoContainerProof` | (b) server đã nhận `NoContainerProof` cho trường hợp này (`execution_cleanup.py`) |
| `docs/contracts/workloads-checkpoints.md:92–93`, `:98`, `:104–105` | sweep: tên/giá trị dimension unique theo RFC 8785 ⇒ 422; dedup là no-op phòng thủ; replay sweep đã lưu; bước 3 mất auth/membership là abort fail closed | (c) §5.A B16-R10, B16-R04 |
| `docs/contracts/workloads-checkpoints.md:136–137` | đoạn carry-forward: vòng 1 "adopt chỉ khi trùng byte" → vòng 2 "không bao giờ tính lại: tải, kiểm, mount read-only, workload chỉ tính chunk sau; workload ghi chunk recognized ⇒ `CHUNK_OUTPUT_CONFLICT`" | (c) §5.A + RV03 |
| `docs/contracts/workloads-checkpoints.md:156–163` | bằng chứng nguồn checkpoint kế thừa; hàng compatibility `CHECKPOINT_PROVENANCE_MISMATCH` | (c) §5.A B15-R05 |
| `docs/contracts/workloads-checkpoints.md:172` | restore bước 3: store không verify được identity ⇒ unavailable, không đánh CORRUPT | (b) AGENTS: storage không khỏe fail closed (B14-OBS-01) |
| `docs/contracts/workloads-checkpoints.md:181–184`, `:196` | bước 6 + đoạn Relocation tách "incompatible" thành hai trường hợp | (c) §5.A B14-R04 |
| `docs/contracts/workloads-checkpoints.md` hàng `INTERNAL` | thêm `CHUNK_OUTPUT_CONFLICT`, `CHUNK_OUTPUT_UNAVAILABLE`, `CHECKPOINT_STORAGE_FAILED` | (c) §5.A B16-R21; thẻ K5 cho phép mã additive |
| `docs/contracts/workloads-checkpoints.md` retention sweep | trỏ `contracts.md` Idempotency 8 | (c) §5.A B16-R05 |

Tài liệu mô tả (không phải contract) cập nhật cùng code: `docs/worker-agent.md` (B11-H01, B15-R39, B15-R33,
B15-R11/R14/R10, B16-R21, B14-K5, ENV-01 compose, REM-R05, REM-R06), `docs/worker-executor.md` (tombstone,
mount `/input/recognized/`), `docs/trusted-runner.md` (B16-DOC-01, B15-R10/R14 exit status, REM-R01, B14-K5,
B16-R21), `docs/database.md` (0021, 0022, `ArtifactStoreIdentity`, `CheckpointReference`),
`docs/artifacts.md` (store identity), `docs/coordinator.md` (B13-OBS-01 measured limit, B15-R32 lock order,
B14-K1, B15-R18, B16-R05, B15-OBS-02), `docs/submit.md` (B16-R10), `docs/project-structure.md`,
`docs/acceptance.md:7` và `AGENTS.md:9` (AUD-01), header B04/B05 (AUD-01).

### Test changes

Test đã có bị sửa (không test hợp lệ nào bị xóa; không assertion nào bị nới):

| File | Thay đổi | Lý do / căn cứ |
|---|---|---|
| `tests/docker/test_real_runner.py` | `docker top -eo pid,uid,args` + `_processes_by_uid`: readiness `{1000,1001} ⊆ uids`, không UID 0 (B16-R29); 2 chỗ `RUNTIME_LIMIT` → `STARTUP_LIMIT` cho stop startup-limit (B15-R10); case controller-disconnect thêm `exit_code == STOP_EXIT_CODES["RUNTIME_LIMIT"]` (94) và field evidence (B15-R14) | oracle theo UID số chặt hơn; openapi :1339; trusted-runner.md exit status |
| `tests/workloads/test_runner.py:266`, `:292` | `RUNTIME_LIMIT` → `STARTUP_LIMIT` cho stop startup-limit | B15-R10, độ mạnh assertion không đổi |
| `tests/workloads/test_runner.py` (helper) | `_serving` lấy đường dẫn socket từ `_control_socket_path`; thư mục tạm được ghi lại và xóa bởi fixture autouse `_control_socket_cleanup` | REM-R10 (rác `/tmp`, có từ trước task); không assertion nào đổi |
| `tests/workloads/test_rem_b15_r10_r14_runner.py` (file mới của task) | dùng `_control_socket_path(prefix="nexa-rem-r14-")` thay `tempfile.mkdtemp`; fixture autouse xóa thư mục sau test | REM-R10; không assertion nào đổi |
| `tests/docker/test_b11_vertical.py` | `_template(engine, image, architecture)` lấy từ `docker image inspect`; `_read_worker_json` báo file không đọc được thay vì ném; `[True]` đọc journal qua `docker exec` dưới UID worker; chế độ loopback opt-in | REM-R04; firewall L |
| `tests/docker/test_b14_checkpoint_restore.py` | chế độ loopback opt-in + `_LoopbackRelay` (127.0.0.1, RST khi `sever`), `cut_worker_network`/`restore_worker_network`; message của assertion S5a (event pairs, checkpoints, reservations) | firewall L; chỉ thêm chẩn đoán |
| `tests/docker/test_b14_checkpoint_restore.py` (sau lượt r4) | `corrupt_state_blob` trả byte gốc và mode; thêm `repair_state_blob` (`O_TRUNC` + `fsync`, mode cũ) và `state_blob_intact` (checksum blob = checksum artifact); scenario 3/4 assert blob nguyên trước khi lật, rồi sửa lại blob sau khi assert xong kết cục; 5a/5b assert blob state của checkpoint mới nhất còn nguyên trước khi assert restore | REM-R09 (blob dùng chung qua dedup); không assertion nào bị nới, chỉ thêm precondition |
| `tests/docker/test_b15_control_recovery.py` | C9 dùng `harness.cut_worker_network()`/`restore_worker_network()` (bridge: disconnect như cũ; loopback: relay) | C9 chạy được trên L; kịch bản không đổi (worker mất control plane) |
| `tests/docker/test_b16_workloads.py` D6 | trước: `ended["state"] != "SUCCEEDED"`, `after[:len(before)] == before`, runtime limit 90 s, chờ 600 s, kết quả "ghi nhận, không assert" → sau: `FAILED`, attempt 2 `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`, `retry_count` 1, chuỗi event + (`ATTEMPT_FAILED`, `CHUNK_OUTPUT_UNAVAILABLE`), không container row cho attempt 2, `after == before`; spec thường, chờ 300 s; `_peaks(..., without_container=...)` cho attempt không có container; blob chunk bị xóa lấy bằng `take_blob` và trả lại bằng `put_back_blob` sau khi assert xong D6 | R21 làm kết quả tất định; oracle chặt hơn (RV02); REM-R07 (blob dùng chung với D6b qua dedup) |
| `tests/docker/test_b16_workloads.py` D6b (mới trong file) | xóa blob state của checkpoint mới nhất ⇒ fallback checkpoint trước, `SUCCEEDED`, carry-forward dưới source gốc, `chunk_report` chính xác, `prediction_counts` = baseline, không recognition trùng; trước khi xóa state assert mọi chunk carry forward `blob_intact` | B16-R21 điều kiện 4; REM-R07 |
| `tests/docker/test_rem_b14_k5_output_full.py` (file mới của task, sửa sau lượt r3) | precondition "first workload snapshot" chuyển từ `_wait(lambda: _exec(...).returncode == 0, …, timeout=30)` sang `_wait_for_first_snapshot`: cùng predicate, cùng bound 30 s và nhịp 0,2 s; khi hết hạn báo exit code/stderr của `docker exec` cuối, `docker inspect` và `docker top` | REM-R08, chỉ chẩn đoán; assertion B14-K5 không đổi |
| `tests/docker/b16_support.py` | thêm `take_blob`, `put_back_blob` (`O_EXCL` + `fsync`), `blob_intact` (checksum); `delete_blob` không đổi; `corrupt_blob` trả byte gốc và mode, thêm `repair_blob` (`O_TRUNC` + `fsync`, kiểm lại byte) | REM-R07; REM-R09 |
| `tests/docker/test_b16_workloads.py` D3 | sửa lại blob `model.safetensors` đã lật bằng `repair_blob` sau khi assert xong provenance | REM-R09 (cùng nguyên nhân, chưa gây lỗi); assertion D3 không đổi |
| `tests/integration/test_rem_b16_r21_recognized_carry_forward.py` (file mới của task) | +`test_lost_newest_state_blob_hands_the_worker_a_valid_recognized_run` (claim restore cũ mang đúng chunk sau cursor; `adapter_recognized` của worker nhận) | REM-R07 vòng 1: loại trừ lỗi sản phẩm; xanh ngay (đối chứng), 12 passed L |
| `tests/fixtures/workloads/pytorch-cifar10-v1/fixture.json` | thêm một mục `image.history` = `nexa/rem-pytorch-cifar10:r2-amd64` `sha256:fb733102…`; sha256 fixture `09bf767e…` → `3664ba09…` | ảnh cuối r2; không sửa template version |
| `tests/fixtures/workloads/batch-inference-v1/fixture.json` | thêm một mục `image.history` = `nexa/rem-batch-inference:r2-amd64` `sha256:32e963d0…`; sha256 `00c1e7c9…` → `dd199a12…` | như trên |
| `tests/fixtures/workloads/hyperparameter-sweep-v1/request.json`, `expansion.json` | request bỏ `1e-2` (sha256 `2aa967a8…` → `6e196e7d…`); expansion chỉ sửa `note` (`81cdbe67…` → `6e93af5e…`), `children` không đổi | B16-R10 |
| `tests/application/test_sweep_expansion_b16.py` | thay test dedup bằng test từ chối giá trị trùng (8 case) + giá trị chỉ bằng nhau trong Python là khác; property test tách thành tích Descartes trên giá trị unique + một giá trị lặp ⇒ từ chối | B16-R10 (hành vi cũ bị §5.A thay) |
| `tests/integration/test_sweep_b16.py` | helper `due()` dùng `sweep_batch(session).due` (B15-R18, assertion không đổi); một chỗ `== 422` → `== 400` cho path/query sai định dạng (B16-R08) | API nội bộ đổi; §5.A B16-R08 |
| `tests/integration/test_checkpoint_b14.py`, `test_templates_b16.py` (`== 422`), `test_control_b15.py`, `test_admin_workers_b15.py` ×3 (`in (400, 422)`) | → `== 400` | B16-R08 (BREAKING có chủ đích); assertion bị siết |
| `tests/cli/test_control_commands_b15.py` | thêm case 400 `validation_failed` → exit 9 | B16-R08 |
| `tests/integration/test_admin_races.py` ×2, `test_identity_service.py`, `test_callback_lock_order_b15.py` | SQL khớp `for no key update`; `_lock_and_expire` nhận cả hai mode; docstring/comment | B15-R32 |
| `tests/artifacts/test_store.py` ×2 | bind identity trước khi mong `not_found`/readiness | B14-OBS-01 (contract mới) |
| `tests/application/test_checkpoint_validation_b16.py` | fixture `"unproven": False`; +1 test candidate chưa chứng minh không bị đọc | B15-R05 |
| `tests/worker/test_checkpoint_flow_b14.py` | +1 test (4 case) body 2xx không phải object ở reserve/publish | B14-R08 |
| `tests/worker/test_compose_b10.py` | +1 test caddy `init: true` + `pids_limit` | ENV-01 |
| `tests/workloads/test_pytorch_workloads_b16.py` | +test DOC-01 (chunk quá cỡ sau các chunk trước); +RV03 (carry-forward không tính lại JSONL/Parquet, fallback từ 0, file hỏng 4 case, `main` nhận cờ) | B16-DOC-01, B16-R21 |
| `tests/worker/test_rem_b15_obs01_journal_read.py` | bật lại logger `nexa.worker.agent` | REM-R03 (cô lập test) |
| `tests/benchmarks/test_b13_queue_microbench.py`, `benchmarks/b13/queue_microbench.py` | +2 test PG; harness thêm `--measure-eligibility-stall`, `--stall-max-ticks`, `--evidence-layer`, provenance `source_tree_sha256`; nhãn layer lấy từ cờ (trước ghi cứng "P") | B13-OBS-01 |

Test mới (mỗi file đỏ trên HEAD hoặc là đối chứng/tái hiện, ghi ở mục finding):
`tests/api/test_rem_b16_r08_wire_status.py`, `tests/artifacts/test_rem_b14_obs01_store_identity.py`,
`tests/docker/test_rem_b11_h01_claim_window.py`, `tests/docker/test_rem_b14_k5_output_full.py`,
`tests/docker/test_rem_b15_r11_start_window.py`, `tests/integration/test_rem_b11_h01_fenced_claim_pg.py`,
`test_rem_b13_r12_search_path.py`, `test_rem_b14_k1_retry_probe_locks.py`,
`test_rem_b14_k5_storage_failure_pg.py`, `test_rem_b14_obs01_store_identity_pg.py`,
`test_rem_b14_r04_incompatible_split.py` (phủ, xanh cả baseline), `test_rem_b15_obs02_admission_off.py`,
`test_rem_b15_r02_non_retryable_pause.py`, `test_rem_b15_r05_inherited_provenance.py`,
`test_rem_b15_r06_second_retry.py` (tái hiện, xanh cả baseline), `test_rem_b15_r18_bounded_sweep.py`,
`test_rem_b15_r32_submitter_lock.py`, `test_rem_b15_r33_rejection_log.py`,
`test_rem_b16_r04_sweep_authorization_loss.py`, `test_rem_b16_r05_sweep_retention.py`,
`test_rem_b16_r10_sweep_unique_values.py`, `test_rem_b16_r21_recognized_carry_forward.py` (dưới
`tests/integration/`); `tests/persistence/test_rem_b13_r12_decimal_bodies.py`;
`tests/worker/test_rem_b11_h01_fenced_claim.py`, `test_rem_b14_k5_checkpoint_storage.py`,
`test_rem_b15_obs01_journal_read.py`, `test_rem_b15_r05_inherited_checksum.py`,
`test_rem_b15_r11_start_deadline.py`, `test_rem_b15_r33_reconcile_log.py`,
`test_rem_b15_r39_resolution_lock.py`, `test_rem_b16_r21_carry_forward.py`,
`test_rem_r05_renew_after_verified_cleanup.py`, `test_rem_r06_verified_cleanup_guards.py` (dưới
`tests/worker/`); `tests/workloads/test_rem_b14_k5_checkpoint_storage.py`,
`test_rem_b15_r10_r14_runner.py`, `test_rem_b16_r21_recognized.py`. Harness đo ngoài suite:
`benchmarks/rem/maintenance_probes.py` (B15-R18/B14-K1).

### Migration

| Migration | Nội dung | Upgrade / downgrade | Dữ liệu |
|---|---|---|---|
| `20260928_0021_rem_decimal_search_path` (schema_v18) | 6 helper Decimal qualify lời gọi nội bộ; 39 hàm ghim `search_path`; không đổi bảng | upgrade→downgrade→upgrade pass (test); downgrade dựng lại thân B05 byte-for-byte; 0,114 s / 0,107 s trên 200.000 dòng ledger | không đổi dữ liệu; index/CHECK không cần REINDEX/validate lại |
| `20260929_0022_rem_artifact_store_identity` (schema_v19) | bảng `artifact_store_identity` (singleton insert-only) + trigger chặn UPDATE/DELETE | downgrade drop bảng và trigger; chạy trong suite PG | bảng mới rỗng; API ghi một lần khi bind store |

Không sửa migration đã phát hành (0001–0020). Head: 0022.

### Breaking/public API changes

| Thay đổi | Trước → sau | Ảnh hưởng | Căn cứ |
|---|---|---|---|
| B16-R08 | path/query sai kiểu/pattern/format: 422 → 400 (cùng code `validation_failed`) | client dựa vào 422 cho lỗi này; CLI map 400 → exit 9 như 422 | §5.A (BREAKING có chủ đích) |
| B16-R10 | `POST /v1/sweeps` giá trị trùng trong một dimension: 207 (dedup im lặng) → 422 `validation_failed`, không ghi gì; sweep đã lưu replay nguyên trạng | request cũ có giá trị trùng bị từ chối | §5.A (BREAKING có chủ đích) |
| B16-R21 | additive: `ErrorResponse.reason`, `ExecutionContext.recognized_chunks`, `RecognizedChunk`, route execution-artifact phục vụ thêm file recognized của claim; mã `INTERNAL` mới | worker cũ bỏ qua `reason`; runner cũ từ chối launch spec có `recognized_chunks`; workload cũ không có cờ `--recognized-*` ⇒ runner, worker, image workload phải cùng source (image r2) | §5.A + RV03 |
| B15-R10 / B15-R14 | IPC: `STOPPED.reason` thêm `STARTUP_LIMIT`; exit status 90–96 | worker cũ fail closed với lý do mới ⇒ worker nâng không muộn hơn runner | additive trong `schema_version` 1 |
| B14-K5 | mã `INTERNAL/CHECKPOINT_STORAGE_FAILED` | `reason_code` chỉ có pattern, không đổi openapi | additive |
| B15-R05 | không đổi wire (cùng 422 `infeasible_request`); chỉ chuỗi retry vốn không hợp lệ bị từ chối | — | §5.A |
| B14-OBS-01 | API từ chối bind store không verify được (503 cho artifact I/O, worker không READY) | vận hành, không đổi wire | AGENTS fail closed |
| B15-OBS-02 | `ADMISSION_OFF`: poll/claim offer đã commit được; tick không tạo offer mới | hành vi vận hành đúng SM:110 | (b) |

Không thay đổi dependency (lockfile `uv.lock`, `web/pnpm-lock.yaml` không đổi), không đổi web, không đổi kiến
trúc module. Schema: 0021 (hàm), 0022 (bảng mới). Security: B14-OBS-01 fail closed với store sai; B15-R05
chặn đọc checkpoint không chứng minh được nguồn; route execution-artifact chỉ mở thêm đúng các file
recognized mà claim đã ghim (kiểm kind/media/size/checksum).

### Giới hạn

1. **Kích thước danh sách recognized (RV07).** `recognized_chunks` tối đa 2048 mục; ở dạng gọn, list lớn
   nhất ≈684 KB, vừa bound 1 MiB của pending store worker (có test
   `test_rem_b16_r21_carry_forward.py`, list tối đa). Mục dài hơn dạng đã test (ví dụ media type/ID dài hơn
   quy ước hiện tại) không có đo riêng; hành vi khi list thực sự vượt bound 1 MiB chưa được test trong task
   này.
2. **Budget khởi động của carry-forward (RV03).** File recognized được tải trước `/start`, trong budget claim
   30 s. Trường hợp xấu nhất là 2048 file × tối đa 1.032.192 byte (≈2 GiB). Task chỉ đo với vài chunk
   (D6b, test worker), không đo quy mô 2048 chunk. Tải quá budget thì attempt kết thúc
   theo đường "claim startup budget elapsed" (không có container, không recognition trùng), không tiến được
   cho job đó đến khi có sửa riêng (vd tải trước khi claim, hoặc budget theo byte).
3. **Mất frame FAILED của B14-K5 (RV07).** Khi frame `FAILED{CHECKPOINT_STORAGE_FAILED}` mất, worker chỉ còn
   exit status `FAILURE` (không mang lý do) và báo `INTERNAL/CHECKPOINT_PROTOCOL_ERROR`. Lớp `INTERNAL`
   (không retry mù) vẫn đúng; lý do cụ thể mất.
4. **Bind store identity của B14-OBS-01 (RV07).** API bind identity một lần khi khởi động, không retry. Store
   không verify được ⇒ artifact I/O trả 503 và readiness fail cho đến khi API restart với volume đúng; không
   tự re-init.
5. **`CREATE_IN_FLIGHT` cùng incarnation (RV07, `agent.py:420`).** Record `CREATE_IN_FLIGHT` không container
   luôn unresolved. Với incarnation cũ, B11-H01 tombstone/abandon nó. Trong cùng incarnation, nếu thread
   tạo container chết giữa chừng mà process còn sống, attempt giữ unresolved (worker không READY) đến khi
   worker restart; fail closed, không oversubscription.
6. **Đường result của B14-K5.** ENOSPC/EIO khi stage result vẫn đi đường cũ (exit 124 ⇒
   `RUNNER_UNAVAILABLE`, retry được); chỉ stage checkpoint có mã riêng.
7. **B15-OBS-01.** Cơ chế chưa được chứng minh bằng dữ liệu sự cố; sửa phòng thủ và chẩn đoán đã có, P chưa
   chạy lại được.
8. **P (Docker Desktop).** Không lượt Docker/PG nào chạy trên P trong task; mọi phần P của điều kiện đóng là
   ENVIRONMENT BLOCKED. Image arm64 cho ref cuối chưa build.
9. **L không phải G/R.** VPS1 là EC2 VM không GPU; không gate ACC nào được nâng lên pass nhờ các lượt này.
10. **B13-OBS-01.** Đo stall có trên L; tiêu chí chấp nhận chưa có (OD-2).
11. **Không có Web UI/metrics.** Counter cho B15-R33 và các log mới chỉ là log JSON; metric thuộc B19.
12. **Blob dedup theo tenant (REM-R07, REM-R09).** Artifact COMMITTED cùng tenant/checksum/kind/media type dùng chung
    một blob, nên mất một blob chunk ảnh hưởng mọi job của tenant cùng tham chiếu nó. Claim fail closed
    `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE` cho từng job đó. Blob state checkpoint bị hỏng cũng vậy: mọi checkpoint
    cùng tham chiếu bị đánh dấu corrupt khi restore và fall back có event (REM-R09). Mất/hỏng blob nằm ngoài
    failure scope đã khóa; không có cơ chế tự tái tạo.
13. **Cửa sổ reserve checkpoint sau cleanup (REM-R06).** Result thread kiểm `cleanup_verified` không giữ khóa
    journal. Một reserve checkpoint có thể được gửi vài chục ms sau cleanup đã verify ở thread khác; lượt r2 có
    1 lần (56 ms), lượt r3 có 2 lần (54 ms, 56 ms), lượt r5 có 1 lần (59 ms). Server fence trả 409, không có reservation. Reserve không phải operation bền nên không chặn READY.

### Tự review

Đã đọc lại toàn bộ `git diff` và các file untracked của task (src, migrations, tests, docs) theo checklist
AGENTS.md:
- **Authorization/tenant.** Route execution-artifact mới chỉ mở các mục `recognized_chunks` của đúng attempt
  có authority, row lọc theo tenant của attempt, `COMMITTED`, size/checksum bằng claim, kind `RESULT_FILE`,
  media type thuộc tập chunk; không nhận path từ client. Các đường B15-R05 và B16-R04 kiểm tenant,
  membership và chuỗi `retry_of_job_id` trong transaction.
- **Fencing/lease.** Không đường claim/adopt/failure/renew nào gửi sau cleanup đã verify (REM-R05/R06, B15-R39;
  đếm trên log B15 r2). Còn một cửa sổ reserve checkpoint do server fence từ chối (Giới hạn 13).
  Publish carry-forward vẫn qua kiểm fence/lease/desired state; `_same` không nới. Lease expiry vẫn dùng DB
  time. Hết lease không dẫn tới release trước cleanup đúng identity (B11-H01 tombstone trước scan cuối).
- **Transaction.** Không gọi Docker trong transaction. Snapshot recognized quét ngoài transaction và kiểm lại
  trong transaction claim. State/event/counter commit cùng transaction.
- **Blob.** Không đường nào announce file checkpoint dở (K5). Identity store fail closed (OBS-01).
- **Security.** Không log credential/input/checkpoint. `JournalCorruption.diagnostic` chỉ có cause/length/hash
  16 ký tự. `ErrorResponse.reason` và `WorkerApiError.reason` chỉ giữ code khớp pattern an toàn. Mount
  recognized read-only; container vẫn non-root, network none.
- **Migration.** Chỉ thêm 0021/0022; downgrade chạy được (test); không sửa migration đã phát hành.
- **Tests.** Không test hợp lệ nào bị xóa. Assertion chỉ bị siết, không bị nới (bảng "Test changes"). Không
  thêm sleep/retry để che race; interleaving dùng Event/Condition/khóa hàng DB.
- **Docs.** Contract chỉ sửa theo các căn cứ (a)/(b)/(c) ở bảng "Contract edits". PLAN không đổi. AGENTS.md chỉ
  đổi câu trạng thái (AUD-01).
- **Hygiene.** `git diff --check` sạch. Raw đã lọc path home, tên tài khoản, URL DB; không có IP/secret.

Điểm review tự phát hiện trong task: REM-R01..REM-R10 (mỗi mục có section riêng). REM-R01..R07, REM-R09 và REM-R10
đã sửa; REM-R08 BLOCKED (nguyên nhân chưa xác định, đã thêm chẩn đoán). Khi viết M10, tự
review phát hiện câu "không `worker_loop_failed … 409`" ở REM-R06 là sai (log có 12 `_result_once` và 2
`renew_once` `409/stale_authority`, đều trước cleanup, do server thu hồi lease trong scenario). Câu đó đã thay
bằng số đếm theo attempt, và cửa sổ reserve checkpoint được ghi thành Giới hạn 13.

### Closure matrix

Sáu điều kiện CLOSED (§11): (1) root cause đã sửa; (2) có regression test; (3) test liên quan pass; (4) điều kiện
đóng của thẻ pass; (5) docs/evidence đã cập nhật; (6) không regression đáng kể. Ở cột "Điều kiện đóng", ✓ là pass
có evidence, ✗ là chưa đạt. P = Docker Desktop VM trên Mac; L = VPS1 (AWS EC2 c7i.2xlarge, Ubuntu 24.04), Linux
thật, không GPU. "Final" = image r2 (bảng image ở trên).

Quy tắc áp cho P: thẻ nào ghi điều kiện đóng "trên P và L" / "Docker P/L" / "pass trên L và P" (B11-H01, B15-R39,
B15-R11, B16-R29) thì thiếu P là thiếu một điều kiện đóng ⇒ **ENVIRONMENT BLOCKED**, dù mọi phần L đã pass. Thẻ ghi P
"nếu khả thi" / "nếu Mac cho phép" (B14-K5, ENV-01) thì P không là điều kiện bắt buộc.

| ID | Risk | Audit | Cuối | Root cause | Thay đổi chính | File chính | Test (đỏ → xanh) | Evidence P / L | Docs | Điều kiện đóng | Còn lại / owner |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B13-R12 | L4 | NEEDS DECISION | **CLOSED** | Helper Decimal B05 gọi nhau không qualify; ANALYZE/REINDEX/restore chạy dưới `search_path` hạn chế | Migration 0021: 6 helper qualify, 39 hàm ghim `search_path`, 2 catalog-only; downgrade byte-for-byte | `migrations/versions/20260928_0021_*`, `persistence/schema_v18.py` | `test_rem_b13_r12_search_path.py` (0020 đỏ: ANALYZE, restore) → 13 passed L; `test_rem_b13_r12_decimal_bodies.py` 4 | P: không yêu cầu (PG); L: 13 passed, cost ratio 1,002 | `database.md` §Decimal | ANALYZE/REINDEX/CHECK `search_path` rỗng ✓; dump/restore thuần ✓; downgrade ✓; kết quả hàm = 0020 (Hypothesis) ✓; (1)–(6) ✓ | — |
| B11-H01 | L4 | OPEN | **ENVIRONMENT BLOCKED** | Claim commit trước bind journal; incarnation mới coi claim cũ không container là không chứng minh được | `resolve_fenced_claim` dưới khóa attempt; tombstone trước scan cuối rồi `NoContainerProof`; orphan create trễ; fail closed khi mismatch | `worker/agent.py`, `worker/executor.py`, `worker/journal.py`, `worker/state.py` | unit 30 case (HEAD đỏ) → xanh; PG 1 failed → 1 passed; Docker L 1 failed (HEAD) → 1 passed (m2), r2 passed | P: **không chạy** (ENV-01); L: m2 + r2 + r5 (`raw/REM-R5-rem-b11-h01.json`, amd64), timeline M2 | `worker-agent.md`, `worker-executor.md`, `internal-interfaces.md` | 4 biến thể cửa sổ ✓ (unit+PG); proof đúng loại, release 1 lần, READY ✓; Docker kill L ✓, **P ✗**; `worker-agent.md` ✓; không suy từ lease/scan rỗng ✓ | Owner giải phóng VM P (`nexa_b10_smoke3-caddy-1`), build worker/CPU arm64 từ cây hiện tại, chạy `tests/docker/test_rem_b11_h01_claim_window.py` trên P |
| B15-R39 | L3 | OPEN | **ENVIRONMENT BLOCKED** | `_send_resolution` replay không khóa journal ⇒ `/cleanup` kép, `KeyError` | Gửi resolution dưới khóa journal attempt; ghi `acknowledged`/`cleanup_verified` trước drop; lock order | `worker/agent.py`, `worker/execution.py` | `test_rem_b15_r39_resolution_lock.py` HEAD 3/4 đỏ → 4 passed | P: **không chạy**; L r2, r3, r5: 29 attempt B15, mỗi attempt đúng 1 `/cleanup` 200 | `worker-agent.md` (lock order) | race đỏ → xanh tất định ✓; mọi writer qua cùng khóa + lock order ✓; không `/cleanup` kép Docker L ✓, **P ✗** | Như B11-H01: chạy `tests/docker/test_b15_control_recovery.py` trên P với `NEXA_B15_WORKER_LOG_OUT`, đếm `/cleanup` theo attempt |
| B15-OBS-01 | L3 | NEEDS VERIFICATION | **ENVIRONMENT BLOCKED** | Chưa xác định bằng dữ liệu (log P chỉ có tên class); giả thuyết đọc không khóa + virtiofs | `load`/`exists` giữ khóa attempt; `JournalCorruption.diagnostic` (cause/length/sha256_16) | `worker/journal.py`, `worker/agent.py` | `test_rem_b15_obs01_journal_read.py` HEAD 5/5 đỏ → 5 passed | P: **không chạy**; L r2, r3, r5: 0 `JournalCorruption` (log worker B15) | `worker-agent.md` | nguyên nhân bằng dữ liệu ✗; (a) sửa + test ✓, L không cảnh báo ✓, P ✗; (b) fail closed có chẩn đoán ✓, "vô hại" chưa chứng minh | Chạy lại suite B15 trên P với image cuối, đếm `JournalCorruption`; nếu còn, trường `cause`/`length`/`sha256_16` chỉ ra cơ chế |
| ENV-01 | L3 | NEEDS VERIFICATION | **CLOSED** | Caddy (Go) là PID 1, không reap `ssl_client` mồ côi của healthcheck BusyBox: 1 zombie / 5 s | `compose.yaml` caddy `init: true` (+ `pids_limit: 512` lưới an toàn) | `compose.yaml` | `test_compose_b10.py::test_caddy_reaps_…` 1 failed → 3 passed | P: không chạy (thẻ: "nếu Mac cho phép"); L: soak 8 h 30 phút, 0 zombie 510/510 mẫu, pids 15 | `worker-agent.md` (Local Compose) | cơ chế chứng minh ✓; root cause sửa ✓; soak ≥ 8 h L phẳng ✓; (1)–(6) ✓ | Owner dừng `nexa_b10_smoke3` và dựng lại từ compose hiện tại |
| B13-OBS-01 | L3 | OPEN | **NEEDS OWNER DECISION** | Replay eligibility theo trang 64 Job xen kẽ tenant: capability đổi ⇒ ≈1.502 tick tới Dispatch đầu ở 100k | Chỉ harness đo (`--measure-eligibility-stall`), không đổi sản phẩm | `benchmarks/b13/queue_microbench.py` | `tests/benchmarks/test_b13_queue_microbench.py` 2 mới, 9 passed L | L: capability ≈375,8 s, toggle ≈42,9 s; P: not-run (tùy chọn) | `coordinator.md` "Measured limit" (chuẩn bị cho OD-2A) | đo 100k L ✓; tiêu chí đóng chưa có (OD-2 trống) | Điền OD-2: A (chấp nhận limit, docs đã sẵn) hoặc B với bound (hướng replay theo nhóm, cần thiết kế + benchmark) |
| B15-R11 | L3 | OPEN | **ENVIRONMENT BLOCKED** | Runner chết sau `/start` ACK: worker replay deadline tới hết budget ⇒ `STARTUP_TIMEOUT` không retry | Inspect đúng container đã bind trước khi replay; exit ⇒ phân loại theo frame/exit status; `sendall` trong `try` | `worker/execution.py`, `worker/runner_control.py` | unit 23 failed / 5 guard → xanh; Docker L HEAD 1 failed → M4 1 passed, r2 passed (`raw/REM-R2-rem-b15-r11.json`) | P: **không chạy**; L: timeline M4, r5 (`raw/REM-R5-rem-b15-r11.json`: `RUNNER_UNAVAILABLE` → retry → `SUCCEEDED`; âm tính `STARTUP_TIMEOUT`) | `worker-agent.md` | đỏ → xanh ✓; kill L ✓, **P ✗**; âm tính ✓ | Chạy `tests/docker/test_rem_b15_r11_start_window.py` trên P với image arm64 cuối |
| B15-OBS-02 | L3 | NEEDS DECISION | **CLOSED** | `poll` từ chối mọi mode ≠ NORMAL; tick vẫn tạo offer trong `ADMISSION_OFF` | `poll` chỉ từ chối `WRITE_FROZEN`; tick không ra quyết định khi mode ≠ NORMAL | `application/execution_service.py`, `application/worker_service.py`, `coordinator/service.py` | `test_rem_b15_obs02_admission_off.py` 2 failed → xanh; liên quan 140 passed L | P: không yêu cầu (PG); L PG | `state-machines.md:110`, `coordinator.md` | offer đã commit claim được, không tiêu retry ✓; không offer mới trong `ADMISSION_OFF` ✓; freeze guard giữ ✓; (1)–(6) ✓ | — |
| B16-R21 | L3 | NEEDS DECISION | **CLOSED** | Attempt sau fallback/restore cũ không biết chunk đã recognized ⇒ tính lại, 409 mãi | Claim mang `recognized_chunks`; server download graph; worker tải + mount RO; runner kiểm; workload `carry_recognized`; `ErrorResponse.reason` | `application/checkpoint_restore.py`, `application/execution_artifacts.py`, `worker/execution.py`, `worker/adapter_dispatch.py`, `workloads/trusted_runner.py`, `workloads/batch_inference.py` | HEAD 42 failed; PG 8 failed → xanh; RV03 server 2, worker 14, workload 8 đỏ → xanh | P: không chạy (thẻ không đòi P); L r2b: D4/D5/D6/D6b 1 passed (`raw/REM-R2b-*`); r5 toàn suite (`raw/REM-R5-B16-inference.json`) | `openapi.yaml`, `workloads-checkpoints.md`, `trusted-runner.md`, `worker-agent.md`, `worker-executor.md` | (1) mang tập recognized, không tính lại ✓; (2) không nới `_same` ✓; (3) conflict → `INTERNAL` safe reason ✓; (4) D6b SUCCEEDED restore cũ, 8 chunk carry-forward, 16+24 theo source, không trùng ✓; D6 fail closed `CHUNK_OUTPUT_UNAVAILABLE` ✓ | Giới hạn 1, 2, 12 |
| B15-R32 | L2 | OPEN | **CLOSED** | Khóa principal `FOR UPDATE` chặn `KEY SHARE` của FK `queue_submitters` ⇒ vòng khóa | `FOR NO KEY UPDATE` (7 chỗ) | `application/identity_service.py` | `test_rem_b15_r32_submitter_lock.py` 2 failed → 40 passed (cùng suite liên quan) | L PG | `coordinator.md` lock order | promote không chờ request ✓; khóa vẫn loại trừ update user/request khác ✓; (1)–(6) ✓ | — |
| B14-OBS-01 | L2 | NEEDS VERIFICATION | **CLOSED** | Store sai/tái tạo có `committed/` ⇒ blob `not_found` ⇒ CORRUPT một chiều | `store-identity` + bảng `artifact_store_identity` (0022); `not_found` chỉ khi store đã bind đúng | `application/storage_identity.py`, `infrastructure/artifacts/store.py`, `api/app.py`, `persistence/schema_v19.py` | artifacts 10 case + PG 5 OBS-01 đỏ → xanh | L PG; filesystem thật | `artifacts.md`, `database.md`, `workloads-checkpoints.md` | store không xác minh ⇒ 503, không đánh dấu ✓; blob thiếu store đúng ⇒ CORRUPT ✓; adoption sau nâng cấp ✓; (1)–(6) ✓ | Không tự re-init (giới hạn 4) |
| B15-R05 | L2 | NEEDS DECISION | **CLOSED** | Retry `checkpoint_id` không kiểm lineage; claim tin mọi reference | `retry_lineage`; `_lock_retry_checkpoint` kiểm lineage; `_proven_inherited` ở claim | `application/job_recovery.py`, `application/job_control.py`, `application/checkpoint_restore.py` | `test_rem_b15_r05_inherited_provenance.py` 5 failed → 605 passed (cùng suite) | L PG | `workloads-checkpoints.md`, `openapi.yaml`, `database.md`, `state-machines.md` | retry ngoài chuỗi 422 ✓; reference sai không restore/không đánh CORRUPT ✓; tổ tiên restore được ✓; (1)–(6) ✓ | — |
| B16-R08 | L2 | NEEDS DECISION | **CLOSED** | Handler validation trả 422 cho path/query sai định dạng | `RequestValidationError` path/query/JSON hỏng ⇒ 400 `validation_failed` | `api/app.py` | `test_rem_b16_r08_wire_status.py` 8 failed → xanh | Mac + L PG | `contracts.md:71`, `openapi.yaml` `BadRequest` | 400 cho path/query sai định dạng ✓; 69 operation có 400 ✓; CLI theo `code` ✓; (1)–(6) ✓ | BREAKING (bảng Breaking) |
| B15-R06 | L2 | NEEDS DECISION | **NEEDS OWNER DECISION** | Không có unique theo `retry_of_job_id`; key mới tạo retry mới | Không đổi (chỉ test tái hiện) | — | `test_rem_b15_r06_second_retry.py` 1 passed (ghi hành vi) | L PG | — | chưa có tiêu chí (OD-1 trống) | Điền OD-1: A (một câu `concurrency-recovery.md:77`) hoặc B (migration 0023 unique partial + 409) |
| B14-R04 | L2 | NEEDS DECISION | **CLOSED** | Contract dùng "incompatible" cho hai trường hợp; code đã đúng | Làm rõ bước 6 và đoạn Relocation | `docs/contracts/workloads-checkpoints.md` | `test_rem_b14_r04_incompatible_split.py` 3 passed baseline và repo (coverage khóa hành vi, không red) | L PG | `workloads-checkpoints.md` | giữ fallback restart_safe có event ✓; câu chữ tách 2 trường hợp ✓; test cả hai ✓; (1)–(6) ✓ | — |
| B15-R33 | L2 | NEEDS DECISION | **CLOSED** | Callback stale bị từ chối và kết quả reconcile không quan sát được | Log JSON `worker_callback_rejected` (API) và `worker_reconcile` (worker), không event mới | `api/app.py`, `worker/agent.py` | PG 1 + worker 2 đỏ → xanh | L PG + Mac | `worker-agent.md` | quan sát được, có giới hạn, không credential ✓; không thêm event type ✓; metric → B19 (theo 5.A) ✓; (1)–(6) ✓ | Counter Prometheus thuộc B19 |
| B15-R14 | L2 | PARTIALLY FIXED | **CLOSED** | Stop không ai nhận frame ⇒ exit 0 ⇒ `RUNNER_PROTOCOL_ERROR`; watchdog stop không xác nhận treo `STOPPING` | `STOP_EXIT_CODES` 90–96; worker map exit status; watchdog `fail_closed` | `workloads/trusted_runner.py`, `worker/protocol.py`, `worker/execution.py` | `test_rem_b15_r10_r14_runner.py` + exit case R11 đỏ → 909 passed L | L r2/r2b/r5: D6 `INTERNAL/CHUNK_OUTPUT_UNAVAILABLE`, 0 `RUNNER_PROTOCOL_ERROR` toàn suite | `trusted-runner.md`, `internal-interfaces.md` | nhánh 1 lý do giữ qua exit status ✓; nhánh 2 bound < 2 s, exit 94 ✓; D6 phân loại tất định ✓; (1)–(6) ✓ | — |
| B14-K5 | L2 | OPEN | **CLOSED** | `OSError` khi stage checkpoint ⇒ exit 124 ⇒ `RUNNER_UNAVAILABLE` retry được | `CheckpointStorageFailed` ⇒ `INTERNAL/CHECKPOINT_STORAGE_FAILED`, không announce file dở | `workloads/trusted_runner.py`, `worker/execution.py`, `application/execution_cleanup.py` | runner 7, worker 3, PG 12 failed → 110 passed L | P: không chạy (thẻ: "nếu khả thi"); L r2: `/output` 16 MiB đầy ⇒ `INTERNAL/CHECKPOINT_STORAGE_FAILED`, retry 0 (`raw/REM-R2-k5.json`); r4, r5 như vậy (`raw/REM-R5-k5.json`) | `workloads-checkpoints.md:187`, `trusted-runner.md`, `worker-agent.md` | safe reason ✓; lớp retry đúng ✓; fault injection đỏ → xanh ✓; Docker L ✓; (1)–(6) ✓ | Đường result giữ nguyên (giới hạn 6) |
| B16-R29 | L2 | OPEN | **ENVIRONMENT BLOCKED** | `docker top -eo user` phân giải tên theo host | So UID số `-eo pid,uid,args`, assertion chặt | `tests/docker/test_real_runner.py` | L 1 failed → 9 passed; r2 9 passed | P: **không chạy**; L ✓ | — | UID số ✓; assertion chặt ✓; pass L ✓, **P ✗** | Chạy `tests/docker/test_real_runner.py` trên P |
| AUD-01 | L2 | OPEN | **CLOSED** | Câu trạng thái viết ở B01/B02 không cập nhật | Sửa câu trạng thái (không đổi quy tắc) | `AGENTS.md`, `docs/acceptance.md`, header B04/B05 | docs, không có hành vi | — | các file trên | câu trạng thái khớp repo ✓; không đổi quy tắc ✓ | — |
| B16-DOC-01 | L2 | OPEN | **CLOSED** | Câu exit 65 sai với chunk k>0 quá cỡ | Tách hai nhóm trong bullet exit 65 | `docs/trusted-runner.md` | `test_oversized_later_chunk_is_invalid_input_after_the_earlier_chunks` (hành vi có sẵn) torch L | L torch 154 → final 162 passed | `trusted-runner.md` | câu chữ khớp hành vi, có test dẫn ✓ | — |
| B14-R08 | L1 | PARTIALLY FIXED | **CLOSED** | `ValueError` body 2xx không phải object thoát khỏi flow | → `CheckpointProtocolError` (chỉ fail attempt đó) | `worker/checkpoint_flow.py` | 4 case đỏ → xanh | Mac | — | chỉ attempt đó fail ✓; (1)–(6) ✓ | — |
| B15-R10 | L1 | PARTIALLY FIXED | **CLOSED** | Stop startup limit dùng lý do `RUNTIME_LIMIT` | Lý do `STARTUP_LIMIT` ⇒ `TIMEOUT/STARTUP_TIMEOUT` | `workloads/trusted_runner.py`, `worker/protocol.py` | 4 test đỏ → xanh; `test_real_runner` 9 passed L | L | `trusted-runner.md`, `internal-interfaces.md`, `worker-agent.md` | lý do riêng ✓; worker map đúng ✓; (1)–(6) ✓ | — |
| B15-R18 | L1 | PARTIALLY FIXED | **CLOSED** | Walk retention đọc lại mọi record hết hạn chưa đến lượt | `sweep_batch` có giới hạn + hoãn ngoài walk | `coordinator/retention.py`, `coordinator/service.py` | `test_rem_b15_r18_bounded_sweep.py` 5 failed → xanh; EXPLAIN walk 267,9 ms → 0,11 ms (100.000 record) | L PG | `coordinator.md` | walk có giới hạn ✓; quy tắc retention giữ ✓; (1)–(6) ✓ | — |
| B14-K1 | L1 | DEFERRED | **CLOSED** | Probe retry lấy khóa policy mỗi giây kể cả khi không có việc | Chỉ khóa khi có việc thật | `coordinator/retry.py`, `coordinator/service.py` | `test_rem_b14_k1_retry_probe_locks.py` 4 failed → 32 passed (cùng suite liên quan) | L PG | `coordinator.md` | không khóa khi không có việc ✓; số đo khóa ✓; (1)–(6) ✓ | — |
| B15-R02 | L1 | NEEDS DECISION | **CLOSED** | Contract thiếu câu chữ; code đúng 5.A | Làm rõ `state-machines.md` | `docs/contracts/state-machines.md` | `test_rem_b15_r02_non_retryable_pause.py` (`TIMEOUT`/`OOM`/`INTERNAL`) | L PG | `state-machines.md` | theo 5.A: contract rõ + test assert ✓ | — |
| B15-R34 | L1 | NEEDS DECISION | **CLOSED** | Contract không nói attempt `LOST` khi cancel | Làm rõ `state-machines.md` | `docs/contracts/state-machines.md` | `test_control_b15.py::test_cancel_of_a_reaped_job_keeps_the_lost_attempt_terminal` (giữ nguyên) | L PG | `state-machines.md` | theo 5.A ✓ | — |
| B16-R04 | L1 | NEEDS DECISION | **CLOSED** | Contract bước 3 sweep chưa nói mất quyền giữa child | Làm rõ bước 3 | `docs/contracts/workloads-checkpoints.md` | `test_rem_b16_r04_sweep_authorization_loss.py` | L PG | `workloads-checkpoints.md` bước 3 | theo 5.A ✓ | — |
| B16-R05 | L1 | NEEDS DECISION | **CLOSED** | Kiểm chứng: `contracts.md:92` đã quy định | Không đổi quy tắc; phần hoãn ngoài walk thuộc B15-R18 | — | `test_rem_b16_r05_sweep_retention.py` | L PG | `workloads-checkpoints.md` (đoạn retention sweep), `coordinator.md` | theo 5.A ✓ | — |
| B16-R10 | L1 | NEEDS DECISION | **CLOSED** | Dedup RFC 8785 trong `expand` trái `uniqueItems` | `_check_shape` từ chối trùng ⇒ 422 | `application/sweep_expansion.py`, `api/schemas.py` (comment) | `test_sweep_expansion_b16.py` (unit) + `test_rem_b16_r10_sweep_unique_values.py` (PG) đỏ → 114 passed | Mac + L PG | `workloads-checkpoints.md`, `submit.md` | trùng ⇒ 422, không ghi gì ✓; (1)–(6) ✓ | BREAKING (bảng Breaking) |
| AUD-02 | L1 | OPEN | **NEEDS OWNER DECISION** | File 93 MB đã track | Không đổi | — | — | — | — | chưa có tiêu chí (OD-3 trống) | Điền OD-3: A (guard + allowlist + dòng B25) hoặc B (`git rm --cached`, tái tạo ngoài repo) |

**REM-Rxx (phát hiện trong task).**

| ID | Cuối | Nguyên nhân | Sửa | Test (đỏ → xanh) | Evidence | Điều kiện đóng |
|---|---|---|---|---|---|---|
| REM-R01 | **CLOSED** | `ECONNRESET` của supervisor bị coi như `EXIT` ⇒ `STOPPED` giả | `stop_workload` đòi exit report | 1 failed → xanh; test gốc 100/100 L (baseline 98/2) | L | đỏ → xanh ✓; hết flake L ✓ |
| REM-R02 | **CLOSED** | Chu kỳ 0 batch bị coi là thiếu batch | `require_all_chunks`, `binding_set_checksum` | 1 failed → xanh | Mac + L | đỏ → xanh ✓ |
| REM-R03 | **CLOSED** | Alembic `fileConfig` tắt logger ⇒ test phụ thuộc thứ tự | Bật lại logger trong test | tái hiện 1 failed → 5 passed | L PG full | đỏ → xanh ✓; suite PG xanh ✓ |
| REM-R04 | **CLOSED** | `test_b11_vertical` ghi cứng arm64; đọc state/journal của worker bằng UID host | Kiến trúc từ image; `_read_worker_json`; đọc journal qua `docker exec` | L `[False]`/`[True]` fail → 2 passed; r2 2 passed | L | chạy đúng trên amd64, không nới assertion ✓ (điều kiện tự đặt; P chạy lại thuộc M9 P, ENVIRONMENT BLOCKED) |
| REM-R05 | **CLOSED** | Renewal gửi sau cleanup đã verify | `renew_once` bỏ attempt có `cleanup_verified` | 1 failed → 1 passed | L r2 C1/C8 pass | (1) không renewal sau verify ✓; (2) C1/C8 L image cuối ✓ |
| REM-R06 | **CLOSED** | Scan/IPC/result/failure/offer vẫn hành động trên attempt đã verify | Mọi đường đọc `cleanup_verified` dưới khóa | 4 failed → 4 passed | L r2 C1/C8 pass; log B15: 0 claim/adopt/fail/renew/cleanup sau verify | (1) không đường claim/adopt/failure/renew hành động sau verify ✓; (2) C1/C8 L image cuối ✓ (còn cửa sổ reserve checkpoint, xem REM-R06) |
| REM-R07 | **CLOSED** | Test harness: D6 xóa blob chunk dùng chung (dedup theo tenant) với D6b | D6 trả blob lại sau khi assert; D6b guard `blob_intact` | r2 D6b FAILED → r2b 1 passed | L | lỗi harness được sửa, không nới assertion ✓; sản phẩm fail closed đúng ✓ |
| REM-R08 | **BLOCKED** | Precondition "first workload snapshot" của test Docker K5 fail 1 lần ở r3; nguyên nhân chưa xác định (lượt r3 không có dữ liệu chẩn đoán) | `_wait_for_first_snapshot` (chẩn đoán, không nới) | không tái hiện: 7 lượt chạy lại passed ([`raw/REM-R3-k5-reruns.out`](raw/REM-R3-k5-reruns.out)); r4 và r5 passed | L | nguyên nhân bằng dữ liệu ✗ (hành động: khi tái hiện, đọc `exec_rc`/`exec_stderr`/`container`/`processes` trong message, sửa theo cơ chế); không ảnh hưởng B14-K5 |
| REM-R09 | **CLOSED** | Test harness: scenario 3/4 B14 lật byte blob state tại chỗ và không trả lại; checkpoint 5b dedup lên blob hỏng đó ⇒ fall back về input | Corrupt có repair, guard `state_blob_intact`; B16 D3 `repair_blob` | r4 5b `None` → r5 24 passed (B14 7 scenario `SUCCEEDED`) | L | lỗi harness được sửa bằng dữ liệu, không nới assertion ✓; sản phẩm fail closed đúng ✓ |
| REM-R10 | **CLOSED** | Test runner tạo thư mục socket tạm trong `/tmp` không xóa (`_serving` có từ trước, file REM mới thêm lời gọi) | `_control_socket_path` + fixture autouse xóa | Mac: 1 lần chạy file REM để lại 5 thư mục → 0; default toàn suite 0; L r6 14 → 14 | Mac + L | không rác `/tmp` ✓; không assertion nào đổi ✓ |

**Phần P (Docker Desktop VM) của mọi finding: ENVIRONMENT BLOCKED.** Không lượt Docker/PG nào chạy được trên P trong
task (ENV-01, M0). Với B11-H01, B15-R39, B15-R11, B16-R29, thiếu P là thiếu điều kiện đóng nên đó là trạng thái cuối
của finding. Với các finding khác, thẻ không đòi P (hoặc chỉ "nếu khả thi"). Hành động tối thiểu chung: owner dừng
`nexa_b10_smoke3-caddy-1` (hoặc restart VM Docker Desktop), build image arm64 từ cây hiện tại, chạy
`tests/docker` trên P theo lệnh §10.A.

**Tổng.** 31 finding: CLOSED 23, BLOCKED 0, NEEDS OWNER DECISION 3 (B13-OBS-01, B15-R06, AUD-02), ENVIRONMENT
BLOCKED 5 (B11-H01, B15-R39, B15-OBS-01, B15-R11, B16-R29). REM-R01..R10 (phát hiện trong task): CLOSED 9, BLOCKED 1 (REM-R08).

### Bàn giao VPS1 (2026-09-30, sau khi đã kéo evidence)

- Đã xóa:
  - tmux `nexa-rem`;
  - container `nexa_rem_pg` và volume của nó (mọi DB `nexa_b05_test_rem_*`);
  - mọi tag `nexa/rem-*` (22 tag);
  - `~/nexa-rem`: repo, baseline, venv, tools, secrets, evidence, log chẩn đoán;
  - thư mục `/tmp/nexa-r14-*`, `/tmp/nexa-rem-r14-*` và thư mục tạm pytest của task. File trong thư mục tạm
    pytest do container test ghi dưới UID root, nên được xóa bằng một container `alpine:3.22` tạm, chỉ mount thư
    mục đó, `--network none`, `--rm`.
- Còn lại, không thuộc task nên không đụng:
  - image `nexa/b16-*`, `nexa/pytorch-cifar10:*`, `nexa/batch-inference:*`, `nexa/cpu-iterative:*` của task B16;
  - image `postgres` (untagged), `python:3.12-slim`, `alpine:3.22`, `caddy:2.10.2-alpine`, `hello-world`;
  - `~/nexa-b16` (chỉ đọc);
  - build cache Docker 2,54 GB, dùng chung với các build trước. Không prune.
- Không còn container, volume hay tmux session nào. Không mở port mới: chỉ còn 22 và DNS loopback. Không thao tác
  AWS, Security Group, ufw, sshd hay sudoers; không reboot.
