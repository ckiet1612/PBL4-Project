# nexa / PBL4

Nền tảng single-node, self-hosted và hardware-portable chạy batch AI cho nhiều tenant trên một Linux server, phân phối CPU/RAM/GPU công bằng và tiếp tục job từ application checkpoint.

**Trạng thái: project/agent setup, chưa triển khai sản phẩm.** Repository chứa kế hoạch, tài liệu hướng dẫn và hai project skills dạng instruction-only; chưa có source, tests, manifests, lockfiles, migrations, Compose hay CI. Các thông số hiệu năng là mục tiêu nghiệm thu, chưa phải kết quả.

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
| [Contracts](docs/contracts.md) | API `/v1`, domain/data model, state machine, internal interfaces và compatibility |
| [Invariants](docs/invariants.md) | Tenant/resource accounting, concurrency, fencing, checkpoint, recovery, security |
| [Acceptance](docs/acceptance.md) | Gate ID, điều kiện pass, evidence, môi trường; testing/benchmark objectives |
| [ADR](docs/adr.md) | Khi nào ghi quyết định và nội dung tối thiểu; không thay PLAN |
| [Agent setup / Skill Creator handoff](docs/agent-setup.md) | Kiểm kê instructions/skills, candidate skills và giới hạn setup |

Stack và module boundaries được tổng hợp tại tài liệu cấu trúc; quy tắc sản phẩm không nằm trong skill. Tài liệu contract hiện là bản trích xuất PLAN, chưa phải OpenAPI/schema đã triển khai hoặc bằng chứng B01 hoàn tất.

## Project skills hiện có

- [benchmarking-scheduler-fairness](.agents/skills/benchmarking-scheduler-fairness/SKILL.md): dùng khi chạy hoặc review evidence về fairness, aging, reservation, queue scalability hoặc load benchmark; không dành cho scheduler implementation hoặc generic unit testing.
- [verifying-recovery-fencing](.agents/skills/verifying-recovery-fencing/SKILL.md): dùng khi kiểm thử hoặc tổng hợp evidence về lease expiry, fencing, stale callback, cancel/reaper race, quarantine, checkpoint recovery hoặc reconciliation; không dành cho generic debugging, implementation planning hoặc documentation review đơn thuần.

Skill hợp lệ về cấu trúc/hướng dẫn không chứng minh runtime acceptance. Status và evidence sản phẩm vẫn theo [acceptance](docs/acceptance.md); PLAN tiếp tục là nguồn sự thật chính.

## Thứ tự triển khai

Theo PLAN §11/§13: **contract + simulator → vertical slice → fairness → recovery → Web UI → nghiệm thu/release**. B01 mở đầu: chốt domain/API/adapter contract, đối chiếu acceptance và ghi inventory phần cứng/nhân lực; B02 mới bootstrap source, dependencies và CI. Các task sau chỉ bắt đầu khi đủ dependency, không coi setup tài liệu là đã hoàn thành B01/B02. B23 GPU có điều kiện; thiếu GPU không chặn lõi CPU.

Máy phát triển dự kiến cần Git, Python 3.12/`uv`, Docker/Compose, PostgreSQL client, Node.js LTS/`pnpm`. Đó là prerequisite trong PLAN §10, **chưa được kiểm tra/cài đặt bởi task setup**. macOS hỗ trợ phát triển/simulator, không thay evidence Linux/cgroups. Chưa có lệnh install/run/test sản phẩm; chỉ bổ sung khi command có trong repository.

## Kiểm tra tài liệu hiện tại

```sh
git status --short
git diff
git diff --check
git ls-files --others --exclude-standard
git check-ignore -v --no-index .env
```

Đọc riêng nội dung file untracked vì `git diff` chưa hiển thị chúng. Với `git check-ignore`, exit 1 cho đường dẫn không bị ignore là kết quả mong đợi khi bảo vệ file nguồn/evidence. Không commit/push tự động.
