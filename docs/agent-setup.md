# Agent setup và bàn giao Skill Creator

Dẫn xuất từ [PLAN.md](../PLAN.md) §3–§14 và yêu cầu Task Setup/Repair; **PLAN được ưu tiên nếu có mâu thuẫn**. Tài liệu phân biệt kiểm kê lịch sử với trạng thái sau Skill Creator và reconciliation, không bổ sung runtime dependency hoặc backlog sản phẩm.

## Historical initial snapshot — kiểm kê trước Task Setup

**Đây là snapshot ban đầu ngày 17/09/2026, không phải trạng thái repository hiện tại.** Bảng này giữ bối cảnh trước khi tài liệu setup và hai project skills được tạo; trạng thái hiện tại nằm ở phần kết quả Skill Creator bên dưới.

| Hạng mục | Quan sát |
|---|---|
| Git | `main` theo `origin/main`; working tree sạch lúc bắt đầu. HEAD `603fdf2d5a9f3d421ecc63be8b74538db7b0a000` — `docs: add approved implementation plan`; trước đó `ba379a9` — `chore: initial commit`. Không commit/push/branch trong setup |
| Repository | Chỉ `PLAN.md` (364 dòng, 15 mục) và `README.md` rỗng; đã đọc toàn bộ. Không source/tests/dependency/lock/migration/CI/Compose/runtime config |
| Agent instructions | Không `AGENTS.md`/`AGENTS.override.md` trong repo hoặc các thư mục cha đã kiểm tra; global `~/.codex/AGENTS.md` tồn tại nhưng rỗng, không global override. Setup bổ sung root `AGENTS.md`; không tạo override |
| Project skills | Chưa có `.agents/skills`, `.codex` hoặc skills trong repo; `~/.agents/skills` cũng không tồn tại khi kiểm tra |
| System skills | Đã kiểm kê `~/.codex/skills/.system`: imagegen, openai-docs, plugin-creator, skill-creator, skill-installer; còn `review-agent` trên đĩa nhưng không nằm trong catalog skill của phiên này. Sự tồn tại trên đĩa không chứng minh tool/skill đã được nạp hoặc có quyền sử dụng |
| Superpowers | Có catalog/plugin và SKILL.md của 14 skill liệt kê bên dưới; đọc `using-superpowers`, hướng dẫn Codex đi kèm và `verification-before-completion` cho task setup |
| Các plugin skill khác | Catalog có Product Design (index/audit/ideate/image-to-code/url-to-code), documents, PDF, presentations, spreadsheets/live Excel, template-creator, visualize. Không cần dùng các workflow artifact/UI cho setup Markdown này |
| Task cùng project | Đọc task **Plan**: bản cuối rút 22 mục xuống 15, chốt 6 giai đoạn/25 task/một release. **Review** chỉ có nhận nhiệm vụ, chưa có findings. Công cụ đọc các lượt mới của **Ask** trả items rỗng nên không có nội dung mới kiểm chứng được; không suy diễn yêu cầu từ summary. Các phương án cũ không vượt PLAN hiện tại |

Snapshot lịch sử này không bảo đảm môi trường phiên khác vẫn có cùng plugin. Agent sau phải dùng skill có trong catalog thực tế, đọc instruction hiện hành và kiểm Git state trước sửa. Không sao chép đường dẫn máy phát triển vào runtime config.

## Superpowers đã bao phủ gì

| Nhóm | Skill hiện có | Phạm vi đã được bao phủ |
|---|---|---|
| Nhận task, thiết kế và plan | `using-superpowers`, `brainstorming`, `writing-plans` | Tìm skill, làm rõ yêu cầu, thiết kế và lập kế hoạch |
| Thực hiện / phối hợp | `executing-plans`, `subagent-driven-development`, `dispatching-parallel-agents` | Thực hiện plan, phân việc theo dependency khi được phép |
| Cô lập / kết thúc nhánh | `using-git-worktrees`, `finishing-a-development-branch` | Git workspace và integration theo authorization; không vượt cấm branch/commit/push của setup |
| Chất lượng code | `test-driven-development`, `systematic-debugging`, `requesting-code-review`, `receiving-code-review`, `verification-before-completion` | TDD, điều tra nguyên nhân, review và evidence trước claim hoàn thành |
| Tác giả skill | `writing-skills` | Cách viết/kiểm tra skill; system `skill-creator`/`skill-installer` phục vụ task riêng được cho phép |

Task Setup ban đầu dùng spec/approval đã cho và chỉ trích xuất tài liệu, không triển khai sản phẩm. Task repair hai skill áp dụng `skill-creator`, `writing-skills` và `verification-before-completion`, giữ RED baseline đã được chấp nhận và revalidate tuần tự. Nếu skill đề nghị hành động ngoài scope, ưu tiên yêu cầu trực tiếp; không suy ra quyền commit/push/cài skill từ workflow. Không tạo skill “workflow nexa” bao trùm mọi thay đổi vì sẽ trùng Superpowers và che invariant.

## Kết quả Skill Creator hiện tại

Task **Skill Create** đã tạo và validated hai project skills bằng validator chính thức và agent-level scenarios. RED baseline trước khi tạo skill được giữ là hợp lệ theo yêu cầu repair; không xóa/tạo lại skill để lặp RED. Cây file hiện tại được ghi tại [project structure](project-structure.md).

| Skill / candidate | Trạng thái hiện tại | Trigger, đầu ra và boundary |
|---|---|---|
| [benchmarking-scheduler-fairness](../.agents/skills/benchmarking-scheduler-fairness/SKILL.md) | Đã tạo và validated | Chạy/review fairness, aging, reservation, queue scalability hoặc load evidence; matched seed/trace/config, Jain window, raw metrics và gate mapping theo PLAN §5–§6, ACC-08–11/29/35. Không trigger cho ordinary scheduler implementation/generic testing; không đổi thuật toán/threshold hoặc dùng simulator chứng minh runtime |
| [verifying-recovery-fencing](../.agents/skills/verifying-recovery-fencing/SKILL.md) | Đã tạo và validated | Kiểm thử/tổng hợp lease/fence/stale callback/control race/quarantine/checkpoint recovery/reconciliation evidence; timeline và đối soát theo PLAN §9, INV-08–17, ACC-12–23/31. Không trigger cho generic debugging/planning/documentation review; không tự mở rộng quyền fault injection |
| Checkpoint compatibility audit | Chưa tạo | Chưa cần tách độc lập: provenance/compatibility/fallback đã thuộc recovery skill, [contracts](contracts.md) và acceptance; chưa có adapter/harness thực tế cho workflow audit riêng lặp lại |
| Release evidence assembly | Chưa tạo | Chỉ cân nhắc khi có release workflow và artifacts thực tế cùng nhu cầu tổng hợp lặp lại; hiện dễ trùng acceptance checklist và `verification-before-completion`. Không tự publish/tag/push hoặc gán pass |

**Instruction-only skills có thể được tạo trước product code và benchmark/recovery harness.** Chúng hướng dẫn lựa chọn bằng chứng, kiểm tra invariant và báo cáo phần thiếu; không được bịa command hoặc kết quả khi harness chưa tồn tại. Validity về cấu trúc/hướng dẫn và agent-level behavioral validation không đồng nghĩa runtime acceptance của sản phẩm đã đạt.

Status sản phẩm chỉ dùng `specified`, `not-run`, `blocked`, `pass`, `fail` theo [acceptance](acceptance.md), tách applicability khỏi status. Specification-only chưa bắt đầu nghiệm thu là `specified`; gate áp dụng cho implementation nhưng chưa chạy là `not-run`; prerequisite cụ thể ngăn kiểm chứng là `blocked` kèm lý do. Chỉ ghi `pass` khi evidence đúng revision/config/environment đáp ứng toàn bộ tiêu chí; `fail` khi evidence cho thấy tiêu chí không đạt. Không skill nào được tuyên bố benchmark, recovery, fencing hoặc project acceptance `pass` chỉ vì skill đã validated.

## Nội dung phải ở AGENTS và docs, không đưa vào skill

`AGENTS.md` giữ thứ tự nguồn sự thật, scope/stack/boundary, invariant bắt buộc, authorization, bảo toàn thay đổi user, cấm tự commit/push và điều kiện claim hoàn tất. Quy tắc này áp dụng dù skill nào có được trigger hay không. PLAN giữ quyết định, contract giữ API/schema/state, invariant giữ constraints, acceptance giữ tiêu chí/evidence/môi trường, ADR giữ lý do quyết định; skill chỉ hỗ trợ workflow kỹ thuật lặp lại dựa trên các nguồn đó.

## Điều kiện còn lại trước triển khai

B01 đã tạo wire schema/signature/state guards, traceability và [inventory môi trường](environment-inventory.md) trong contract `1.0.0-b01`. Remediation, focused rereview và fresh verification đã đóng R-03, R-05 và R-09 nên [ACC-01](acceptance.md) là `pass`; B02 không còn contract-blocked và có thể bootstrap source/dependencies/lockfiles/CI theo PLAN cùng các prerequisite môi trường của chính task đó.

Linux benchmark host, hai cấu hình Linux, từng image architecture, NVIDIA GPU, CI/GHCR access và nhân lực vận hành vẫn chưa được xác nhận. Thời điểm cần bổ sung và gate bị ảnh hưởng nằm trong inventory B01. GPU chưa có không chặn lõi CPU nhưng chặn claim GPU verified. Chỉ cần quyết định user mới nếu phát sinh thay đổi ngoài PLAN hoặc mâu thuẫn thực sự; hiện B01 không ghi nhận blocker contract như vậy.

## Kiểm chứng Task Setup ban đầu — historical

Các số liệu dưới đây thuộc task setup tài liệu trước Skill Creator; không phải kết quả revalidation hai skill hoặc phạm vi thay đổi của task repair hiện tại.

- Đã đọc diff README và diff `--no-index` của từng file mới; `git diff --check` và whitespace check cho file untracked không phát hiện lỗi.
- Kiểm tra 36 liên kết nội bộ, code fences, ID gate và dấu hiệu nội dung chưa hoàn chỉnh: hợp lệ; 39 gate ACC-01–ACC-39 đều `specified`. AGENTS dưới 8 KiB.
- `git check-ignore --no-index` với 81 path mẫu: 44 path runtime/cache/secret/output bị ignore và 37 path cần giữ không bị ignore, gồm locks/migrations/ADR/skills/examples/benchmark scripts/fixtures/raw evidence.
- Đối chiếu scope: chỉ README, AGENTS, `.gitignore` và 6 tài liệu trong `docs/`; không source/test/dependency/skill hoặc thư mục skeleton. PLAN giữ SHA-256 `693c9bc89b73e83c0132b31f167928014ef3137e0d630cee82a9baf49a4e8ff5`; Git HEAD/branch/index không thay đổi.
- Đối chiếu thuật ngữ/threshold/state/invariant với PLAN hiện tại, không phát hiện mâu thuẫn cần quyết định mới. Không chạy test sản phẩm vì repository chưa có implementation/test command; kiểm chứng tài liệu không phải evidence product acceptance.

## Revalidation sau repair/reconciliation

Trình tự cuối đã thực hiện: **fairness validator → positive → negative → edge → recovery validator → positive → negative → edge**. Mỗi skill được đánh giá bởi subagent đọc bản SKILL.md sau sửa; kết quả dưới đây là behavioral validation bằng dữ liệu tình huống, không phải benchmark/fault test của sản phẩm. RED baseline cũ được giữ, không xóa hoặc tạo lại skill.

### benchmarking-scheduler-fairness

| Kiểm tra | Kết quả |
|---|---|
| Validator chính thức | `quick_validate.py`: exit 0, `Skill is valid!` |
| Positive | Đạt: nhận đúng yêu cầu review benchmark; tính weighted Jain = 1 cho 5 seed tình huống; chỉ rõ thiếu baseline cost/timeline/query-plan evidence và không dùng virtual-clock simulator để công bố API đạt 100 accepted/s |
| Negative | Đạt: loại trừ scheduler tie-breaker implementation với ordinary unit tests và generic parsing-helper unit testing |
| Edge | Đạt: specification-only → `specified`; implementation có check áp dụng nhưng chưa chạy → `not-run`; yêu cầu load verification thiếu PostgreSQL → `blocked`. Không dùng validator/deadline để gán `pass` |

### verifying-recovery-fencing

| Kiểm tra | Kết quả |
|---|---|
| Validator chính thức | `quick_validate.py`: exit 0, `Skill is valid!` |
| Positive | Đạt: nhận đúng lease-expiry/stale-callback review; đánh giá trace tình huống là `fail` vì release/reallocate trước cleanup và chấp nhận fence cũ; yêu cầu checkpoint manifest/reconciliation thay vì dựa vào log restore |
| Negative | Đạt: loại trừ generic JSON debugging, planning trang profile và grammar/link review không có recovery evidence |
| Edge | Đạt: chỉ specification → `specified`; fencing check áp dụng nhưng chưa chạy → `not-run`; verification thiếu PostgreSQL/Docker harness → `blocked`. Không tuyên bố recovery/project acceptance `pass` từ skill validity hoặc áp lực thời gian |

Validator dùng PyYAML đã có sẵn từ task Skill Create; task repair không cài dependency hay thay cấu hình người dùng. Đối chiếu snapshot đầu task repair xác nhận chỉ sửa đúng README, project structure, agent setup và hai SKILL.md; không thêm/xóa file hoặc thư mục, các file bị cấm và Git HEAD/branch/index giữ nguyên. Review diff, `git diff --check`, 49 liên kết Markdown và tìm kiếm chính xác chuỗi trạng thái cũ đều đạt; hai skill chỉ dùng năm status chuẩn. Task repair đó không tạo runtime evidence. Sau B01 contract review riêng, chỉ ACC-01 là `pass`; ACC-02–ACC-39 vẫn `specified`.
