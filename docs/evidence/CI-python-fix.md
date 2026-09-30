# CI-python-fix — làm xanh job "Python quality"

Ngày 2026-09-30, base `b4271f2`, máy P (macOS + Docker Desktop). Không commit/push, không chạy workflow trên GitHub.

## Nguyên nhân

Run xanh cuối cùng là B11 `35964010775`. Từ B12 tới remediation, bước "Test Python" luôn đỏ.
Run mới nhất `36696888507` (b4271f2): pytest 879,31 s, 5 failed / 2341 passed / 25 skipped, job bị hủy lúc 15:16.

| ID | Nguyên nhân | Trạng thái |
|---|---|---|
| A | `timeout-minutes: 15`; thời gian pytest tăng từ 193 s (B12) lên 879 s (REM) | đã sửa |
| B | Typer 0.27.2 ép rich terminal khi có `GITHUB_ACTIONS`, nên `'--from'`/`'--to'` trong stderr bị chèn mã ANSI | đã sửa (chỉ trong test) |
| C | Runner ubuntu-24.04 có sẵn `/usr/bin/pg_dump` 16, còn service là `postgres:17` | **đã xác nhận** bằng pg_dump 16.15 thật (C1) |

## Thay đổi

- `.github/workflows/ci.yml`: job `python` có `timeout-minutes: 30`. Bước "Test Python" chạy
  `uv run --no-sync pytest -q --run-postgres -rs --durations=25` với `env` **của step**
  `NEXA_TEST_PG_CLIENT_PREFIX: docker exec -i ${{ job.services.postgres.id }}`.
  Job `web` giữ 15 phút, không đổi. `permissions: contents: read` giữ nguyên, không thêm secret.
- `tests/cli/test_pagination_and_headers.py`: assert `missing in click.unstyle(result.stderr)`.
  Giữ nguyên `exit_code == 2` và monkeypatch `_client`. Không patch Typer, không đổi env toàn cục.
- `tests/integration/test_rem_b13_r12_search_path.py` (`_dump_and_restore`): pg_dump dùng `check=False`.
  Returncode ≠ 0 thì gọi `pytest.fail("pg_dump exited with <code>: <≤2000 ký tự cuối stderr>", pytrace=False)`.
  Logic skip và mọi assert giữ nguyên.
- `docs/environment-inventory.md`: dòng "GitHub-hosted execution access" và câu dưới bảng ghi lại sự thật
  hosted run; credential/branch protection vẫn `unverified`.
- `docs/evidence/raw/CI-B.out`, `CI-C.out`, `CI-B3.out`: output rút gọn, đã lọc.

`git grep -n "pytest -q --run-postgres\|timeout-minutes" -- docs ':!docs/evidence'` có hit ở
`docs/database.md:218` (lệnh chạy local) và `docs/superpowers/plans/2026-09-19-b05-postgresql.md` (plan B05 lịch sử).
Không nơi nào mô tả command/timeout CI hiện tại, nên không đồng bộ.

## Lệnh và kết quả

Chung: `UV=/tmp/nexa-b12-uv-bootstrap/bin/uv`, `PYTHONPATH=src:.`.

| Bước | Lệnh (rút gọn) | Kết quả |
|---|---|---|
| B1 trước fix | `GITHUB_ACTIONS=true … pytest -q tests/cli/test_pagination_and_headers.py` | `2 failed, 7 passed in 0.53s` |
| B2 sau fix | cùng lệnh, `GITHUB_ACTIONS=true` | `9 passed in 0.45s` |
| B2 sau fix | cùng lệnh, `env -u GITHUB_ACTIONS` | `9 passed in 0.36s` |
| B3 | `GITHUB_ACTIONS=true … pytest -q -rs` (một lần) | `1772 passed, 599 skipped, 2 warnings in 47.39s`, 0 failed |
| C1 | prefix `docker run --rm -i --network container:nexa_b13_pg -e PGHOST=127.0.0.1 -e PGPASSWORD postgres:16`, 3 case pg_dump | `3 failed` với `pg_dump exited with 1: pg_dump: error: aborting because of server version mismatch` / `server version: 17.11 …; pg_dump version: 16.15 …`; mật khẩu xuất hiện 0 lần trong output |
| C2 | `NEXA_TEST_PG_CLIENT_PREFIX="docker exec -i nexa_b13_pg" GITHUB_ACTIONS=true … pytest -q --run-postgres -rs <file r12>` | `9 passed in 20.09s`, 0 skipped |
| A1 | actionlint 1.7.12 darwin arm64, SHA256 `aba9ced2…e6953f` khớp `checksums.txt`; `actionlint .github/workflows/ci.yml` | exit 0, không có finding |

Skip của B3 đều là opt-in (`--run-postgres` 123 dòng, Docker thật 11, relay B11 1, torch 1); không có skip mới.
C0/C3: DB `nexa_b05_test_cifix` tạo trên `nexa_b13_pg` (127.0.0.1:15439), sau đó
`DROP DATABASE … WITH (FORCE)`; còn 0 DB `nexa_b05_test_restore_*`. Mật khẩu lấy bằng `printenv`, không in ra.
Đã `unset PGPASSWORD`.

## Vấn đề phát sinh

- **CI-R01** (có từ trước, ngoài phạm vi): khi một test PG fail, traceback dạng long của pytest in các đối số
  (`clean_postgres_database`, `source_url`, `target_url`), tức URL có mật khẩu. Lượt C1 đầu với `pytest.fail` thường
  cho ra 9 lần mật khẩu trong output (chỉ nằm ở `/tmp`, không in ra terminal/evidence). Lần sửa thứ 2 của C thêm
  `pytrace=False` nên nhánh pg_dump không còn lộ; các assert khác và mọi test PG khác vẫn còn lộ khi fail.
  Trong CI, mật khẩu là giá trị CI-only đã công khai trong `ci.yml`. Máy local thì cần user quyết định có xử lý hay không.
- **CI-R02**: prompt kỳ vọng C2 "13 passed", nhưng file r12 hiện chỉ collect 9 test
  (`9 tests collected`). Kết quả 9 passed/0 skipped vẫn đáp ứng ý định "cả file chạy thật, không skip".
- Môi trường: lúc bắt đầu `nexa_b13_pg` đang `Exited (137)`; đã `docker start` để chạy C rồi `docker stop` trả lại
  trạng thái cũ. Image `postgres:16` được pull vào cache local. `nexa_b10_*` không bị động tới
  (caddy 855 PID tại thời điểm kiểm).

## Giới hạn

- C2 dùng `-U postgres` trên container local. CI dùng `-U nexa_ci -d nexa_b05_test_ci` qua socket local của
  service; điều đó đúng khi `POSTGRES_USER` là superuser và `local` trust mặc định của image, nhưng chỉ run hosted mới xác nhận được.
- Không chạy full suite PostgreSQL/Docker/torch local, nên thời lượng dưới 30 phút là ước tính: 879 s + khoảng 20 s cho 3 case pg_dump mới chạy.
  Vòng 2 đã chạy full suite PostgreSQL local (R3).

## Vòng 2 — CI-R03

Commit `ce6bd98` (vòng 1) đã push. Run `36731320212`: pytest 443 s, 3 failed / 2343 passed / 25 skipped.
Không còn SKIPPED của file r12, test CLI pass, không còn timeout. Cả 3 test fail cùng lỗi
`error: maintenance configuration is invalid or unavailable`:
`test_maintenance_cli.py::test_maintenance_cli_reopens_worker_bootstrap_window`,
`test_maintenance_cli.py::test_maintenance_cli_maps_transaction_retry_exhaustion_to_safe_failure`,
`test_templates_b16.py::test_maintenance_command_registers_and_reports_conflicts`.

**CI-R03, nguyên nhân:** vòng 1 đặt `NEXA_TEST_PG_CLIENT_PREFIX` vào env của step. `load_settings` từ chối mọi biến
`NEXA_*` không được khai báo (fail closed, hành vi đúng), nên `nexa-maintenance` ra `ConfigError`. Ba test trên
chỉ `delenv("NEXA_TEST_DATABASE_URL")`, vốn là biến `NEXA_TEST_*` duy nhất có mặt trước vòng 1.

**Cách sửa (chỉ trong test):** ở cả 3 chỗ, `monkeypatch.delenv` mọi key `NEXA_TEST_*` trong `os.environ`, kèm comment
lý do; thêm `import os`. Không sửa `src/`, `ci.yml` hay tên biến.

| Bước | Lệnh (rút gọn) | Kết quả |
|---|---|---|
| R1 trước fix | 3 test trên, `--run-postgres`, `GITHUB_ACTIONS=true`, prefix `docker exec -i nexa_b13_pg` | `3 failed, 2 warnings in 4.20s`, đúng lỗi của CI |
| R2 sau fix | cùng lệnh | `3 passed, 2 warnings in 6.48s` |
| R3 (một lần) | `GITHUB_ACTIONS=true NEXA_TEST_PG_CLIENT_PREFIX="docker exec -i nexa_b13_pg" … pytest -q --run-postgres -rs --durations=25` | `2346 passed, 25 skipped, 2 warnings in 797.22s (0:13:17)`, 0 failed |

R3: không có SKIPPED nào của `test_rem_b13_r12_search_path.py`. 25 skip gồm 24 suite Docker opt-in và 1 torch,
khớp con số 25 skipped của run `36731320212`. Tổng 2346 = 2343 + 3 test đã sửa. Mật khẩu xuất hiện 0 lần trong output.
PID của `nexa_b10_smoke3-caddy-1` khoảng 1,7–1,8k, không lỗi fork. DB `nexa_b05_test_cifix` đã DROP, không còn DB
`nexa_b05_test_restore_*`, `nexa_b13_pg` đã trả về trạng thái dừng. Output rút gọn ở `docs/evidence/raw/CI-R3.out`.
H: ruff check/format sạch, `git diff --check` sạch, lockfile không đổi.

**Bài học:** mọi thay đổi CI có thêm biến môi trường phải chạy toàn bộ lệnh CI với đúng môi trường đó trước khi push.
Vòng 1 chỉ chạy default suite (B3) và riêng file r12 (C2), nên không bắt được CI-R03.

## GitHub-hosted run sau fix

PENDING: user push rồi điền run ID tại đây. Cần kiểm: `Python quality` success, không có dòng `SKIPPED`
nào của `test_rem_b13_r12_search_path.py`, tổng thời gian dưới 30 phút.
