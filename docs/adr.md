# ADR: ghi nhận quyết định kiến trúc

Dẫn xuất từ [PLAN.md](../PLAN.md) phần mở đầu, §11 và §13; **PLAN được ưu tiên nếu có mâu thuẫn**. ADR giải thích quyết định và hệ quả, không tự mở rộng scope hay thay thế approval. B01 đã tạo các ADR dưới đây để diễn giải quyết định PLAN và chi tiết contract được ủy quyền; không ADR nào thay PLAN.

| ADR | Trạng thái | Phạm vi |
|---|---|---|
| [ADR-0001](adr/0001-process-boundaries-and-trust.md) | accepted | Process boundary và trust zones đã khóa trong PLAN |
| [ADR-0002](adr/0002-authoritative-state-and-durable-blob-commit.md) | accepted | PostgreSQL authority, transaction và durable blob commit |
| [ADR-0003](adr/0003-worker-authority-fencing-and-cleanup.md) | accepted | Worker authority, fencing, deadline và cleanup |
| [ADR-0004](adr/0004-checkpoint-manifest-and-compatibility.md) | accepted | Manifest, safe restore và compatibility |

## Khi cần ADR

Ghi ADR khi quyết định ảnh hưởng boundary, persistence/transaction, protocol/compatibility, security, thuật toán hoặc failure guarantees. Không cần ADR cho chỉnh câu chữ, đổi tên cục bộ hay implementation chi tiết không đổi contract. ADR diễn giải lựa chọn đã có trong PLAN phải dẫn đúng mục và không trình bày như một quyết định mới.

Quyết định khác PLAN cần user duyệt và cập nhật PLAN trước khi được xem là accepted; task Setup không có quyền sửa PLAN. Đề xuất có thể được ghi nhưng không triển khai dựa trên đề xuất chưa duyệt. Khi contract thay đổi, đồng bộ [contracts](contracts.md), [invariants](invariants.md), [acceptance](acceptance.md), [project structure](project-structure.md) và dependencies B01–B25 có liên quan; không âm thầm giảm tiêu chí nghiệm thu.

## Quy ước và nội dung tối thiểu

ADR tương lai đặt trong `docs/adr/`, tên `NNNN-short-title.md`, số tăng dần và không tái sử dụng. Chỉ tạo file khi có nội dung thực; không dựng thư mục hoặc template rỗng. Nếu thay thế ADR accepted, giữ lịch sử và liên kết hai chiều đến ADR kế tiếp.

| Trường | Nội dung cần viết |
|---|---|
| ID, tiêu đề, ngày, trạng thái | Trạng thái: proposed, accepted, rejected hoặc superseded; người/nguồn duyệt phải được ghi đúng thực tế |
| Nguồn | Mục PLAN, yêu cầu trực tiếp và contract liên quan; chỉ rõ tái diễn đạt quyết định đã duyệt hay đề xuất đổi quyết định |
| Bối cảnh | Vấn đề cụ thể, constraint, failure scope và bằng chứng thúc đẩy quyết định |
| Lựa chọn | Các phương án thực tế, trade-off; không mở rộng Future Work vào backlog chính |
| Quyết định | Hành vi/boundary cụ thể và lý do chọn; không để câu hỏi mở dưới trạng thái accepted |
| Hệ quả | Security/concurrency/durability/fairness/operational effects; tương thích API/schema/checkpoint/image |
| Chuyển đổi | Migration/maintenance/backups/rollback conditions và task phụ thuộc, nếu có thay đổi dữ liệu/runtime |
| Nghiệm thu | Gate ID bị ảnh hưởng và evidence cần chứng minh; không coi ADR là evidence sản phẩm pass |

Không tự ghi tên người phê duyệt, quyết định chưa có căn cứ hoặc kết quả test chưa chạy. Nếu thiếu quyết định bắt buộc, ADR giữ proposed và nêu đúng vấn đề cần giải quyết; không biến nó thành instruction có hiệu lực trong `AGENTS.md`.
