# nexa / PBL4

Nền tảng single-node, self-hosted và hardware-portable chạy batch AI cho nhiều tenant trên một Linux server, phân phối CPU/RAM/GPU công bằng và tiếp tục job từ application checkpoint.

**Trạng thái: B01 contract `1.0.0-b01` đã được review và ACC-01 là `pass`; B02 bootstrap đã hoàn tất.** Python/UI dependencies có lockfile, Python 3.12 và Node 24 LTS quality commands đều pass, CI read-only đã được parse và actionlint validate. Repository vẫn chưa có product behavior, migration, Compose runtime hay runtime acceptance evidence. Các thông số hiệu năng vẫn là mục tiêu nghiệm thu, chưa phải kết quả.

[PLAN.md](PLAN.md) bản duyệt ngày 16/09/2026 là nguồn sự thật về phạm vi, kiến trúc, thuật toán, backlog và nghiệm thu; PLAN được ưu tiên khi tài liệu dẫn xuất này mâu thuẫn. Yêu cầu trực tiếp mới nhất của user có ưu tiên cao nhất; không tự sửa PLAN để hợp thức hóa thay đổi thiết kế.

## Phạm vi đã khóa

- Một deployment quản lý một Linux server; API/CLI/Web UI user và admin đều bắt buộc. Một bản release sản phẩm `v1.0.0`.
- Weighted dominant resource-time scheduling + hard quota + aging + một reservation local. FIFO/RR/WRR/DRR/DRF chỉ phục vụ so sánh trong simulator.
- Bốn workload được quản lý: CPU deterministic, PyTorch training nhỏ, sweep hữu hạn, inference theo chunk. Admin khóa template/image digest/adapter; không chạy code, shell, image hoặc host mount tùy ý.
- Recovery theo application checkpoint; compute at-least-once trong retry budget, chỉ một final result/job được công nhận. Chịu process crash, gián đoạn mạng và reboot khi dữ liệu bền vững còn nguyên; backup cần thiết cho mất ổ đĩa/corruption. Không phục hồi heap/socket/terminal/CUDA context tùy ý.
- Cùng release cài độc lập trên hai cấu hình Linux; di chuyển deployment có dừng và chỉ resume khi tương thích. GPU cần gate NVIDIA thật trước khi công bố hỗ trợ.
- Multi-server, shared object storage, cross-node recovery, GPU pools, interactive inference, Kubernetes và ≥1 triệu job là Future Work ngoài backlog hiện tại (PLAN §15).

## Đọc tài liệu

| Tài liệu | Vai trò |
|---|---|
| [PLAN.md](PLAN.md) | Quyết định đã duyệt, requirements chi tiết, 6 giai đoạn, 25 task B01–B25, DoD |
| [AGENTS.md](AGENTS.md) | Quy tắc ngắn cho agent, invariant và điều kiện hoàn tất task |
| [Project structure](docs/project-structure.md) | Cây hiện tại, target placement, kiến trúc, ownership và chiều dependency |
| [Contracts](docs/contracts.md) | Điểm vào contract `1.0.0-b01`: OpenAPI `/v1`, domain/state, internal interfaces, workload/checkpoint và concurrency/recovery |
| [Invariants](docs/invariants.md) | Tenant/resource accounting, concurrency, fencing, checkpoint, recovery, security |
| [Acceptance](docs/acceptance.md) | Gate ID, điều kiện pass, evidence, môi trường; testing/benchmark objectives |
| [ADR](docs/adr.md) | Khi nào ghi quyết định và nội dung tối thiểu; không thay PLAN |
| [Traceability](docs/requirements-traceability.md) | PLAN → contract → INV → ACC → backlog → phép kiểm chứng |
| [Environment inventory](docs/environment-inventory.md) | Công cụ/phần cứng/CI/nhân lực đã quan sát hoặc chưa xác nhận |
| [Agent setup / Skill Creator handoff](docs/agent-setup.md) | Kiểm kê instructions/skills, candidate skills và giới hạn setup |

Stack và module boundaries được tổng hợp tại tài liệu cấu trúc; quy tắc sản phẩm không nằm trong skill. OpenAPI và JSON Schema trong `docs/` là specification, không phải runtime implementation; kết quả review B01 nằm tại [contract review evidence](docs/evidence/B01-contract-review.md).

## Project skills hiện có

- [benchmarking-scheduler-fairness](.agents/skills/benchmarking-scheduler-fairness/SKILL.md): dùng khi chạy hoặc review evidence về fairness, aging, reservation, queue scalability hoặc load benchmark; không dành cho scheduler implementation hoặc generic unit testing.
- [verifying-recovery-fencing](.agents/skills/verifying-recovery-fencing/SKILL.md): dùng khi kiểm thử hoặc tổng hợp evidence về lease expiry, fencing, stale callback, cancel/reaper race, quarantine, checkpoint recovery hoặc reconciliation; không dành cho generic debugging, implementation planning hoặc documentation review đơn thuần.

Skill hợp lệ về cấu trúc/hướng dẫn không chứng minh runtime acceptance. Status và evidence sản phẩm vẫn theo [acceptance](docs/acceptance.md); PLAN tiếp tục là nguồn sự thật chính.

## B02 bootstrap workspace

Prerequisite đã chọn cho bootstrap là Python 3.12, `uv` 0.12.15, Node.js 24 LTS và `pnpm` 11.9.0. Evidence B02 dùng Python 3.12.13, isolated `uv` 0.12.15 và isolated Node 24.21.0; không cài tool global hoặc sửa cấu hình máy. Node 26.4.0 vẫn là system runtime quan sát được, không phải support target.

Python workspace dùng các command reproducible sau:

```sh
uv sync --frozen --all-groups --no-editable
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest -q
```

`uv.lock` được tạo bằng `uv` 0.12.15; lock check, frozen non-editable sync, Ruff và pytest đều pass trên Python 3.12.13. `--no-sync` giữ các quality command trên đúng môi trường vừa cài, không để `uv run` tự đổi lại project sang editable install.

UI workspace đã được kiểm tra bằng lockfile hiện có:

```sh
pnpm --dir web install --frozen-lockfile
pnpm --dir web run typecheck
pnpm --dir web run build
```

Các command UI trên pass bằng selected target Node 24.21.0 LTS với checksum lockfile không đổi. Shell web chỉ hiển thị trạng thái bootstrap; nó không gọi API, không mô phỏng authorization và không triển khai user/admin flow. [CI workflow](.github/workflows/ci.yml) dùng quyền `contents: read` và gọi đúng các command Python/UI này.

### Cấu hình bootstrap

Runtime lấy cấu hình trực tiếp từ process environment. [`.env.example`](.env.example) chỉ là mẫu an toàn cho local/deployment tooling và không được Nexa tự động load. Không có `.env`, credential hoặc path máy phát triển mặc định; bootstrap CI/test dùng fixture an toàn và không cần biến `NEXA_*` thật.

| Biến | Kiểu và trạng thái | Ví dụ an toàn / mặc định |
|---|---|---|
| `NEXA_DATABASE_URL` | PostgreSQL URL, bắt buộc | `postgresql+psycopg://nexa@localhost/nexa`; credential thật phải được secret injection cung cấp |
| `NEXA_ARTIFACT_ROOT` | Absolute path, bắt buộc | `/srv/nexa/artifacts`; không có default |
| `NEXA_ENVIRONMENT` | `development\|test\|production`, tùy chọn | `development` |
| `NEXA_API_JSON_MAX_BYTES` | Integer 65,536–16,777,216, tùy chọn | `1048576` theo contract |
| `NEXA_LOG_LEVEL` | `DEBUG\|INFO\|WARNING\|ERROR\|CRITICAL`, tùy chọn | `INFO` |

Biến `NEXA_*` chưa khai báo làm config validation fail; biến process không thuộc namespace này được bỏ qua. Lỗi chỉ nêu tên biến và rule an toàn, không phản chiếu giá trị. Log/error không được chứa token, password, credential, input hay checkpoint content.

## Thứ tự triển khai

Theo PLAN §11/§13: **contract + simulator → vertical slice → fairness → recovery → Web UI → nghiệm thu/release**. Contract `1.0.0-b01` đã đóng R-03, R-05 và R-09 qua focused rereview cùng verification mới; ACC-01 là `pass`. B02 đã hoàn tất nên B03, B05 và B09 đủ dependency trực tiếp để bắt đầu. B23 GPU có điều kiện; thiếu GPU không chặn lõi CPU nhưng chặn claim GPU verified.

[Environment inventory](docs/environment-inventory.md) ghi nhận Git, Python 3.12, Docker/Compose, Node.js và `pnpm` trên máy macOS hiện tại; system PATH vẫn thiếu `uv` và `psql`, nhưng B02 đã dùng isolated `uv`/Node 24 có checksum để hoàn tất local evidence. GitHub-hosted run chưa được quan sát. macOS hỗ trợ tài liệu/development/bootstrap/simulator, không thay evidence Linux/cgroups. Các command trên chỉ kiểm tra bootstrap workspace, không phải product runtime.

## Kiểm tra repository hiện tại

```sh
git status --short --untracked-files=all
git diff
git diff --check
git ls-files --others --exclude-standard
git check-ignore -v --no-index .env
```

Đọc riêng nội dung file untracked vì `git diff` chưa hiển thị chúng. Với `git check-ignore`, exit 1 cho đường dẫn không bị ignore là kết quả mong đợi khi bảo vệ file nguồn/evidence. Không commit/push tự động.
