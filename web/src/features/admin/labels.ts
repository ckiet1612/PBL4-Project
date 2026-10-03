// Vietnamese labels for admin enums and known event/audit names; unknown values are shown raw.
import type { AllocationState, WorkerAdminState, WorkerHealth } from "../../api/types";
import type { Tone } from "../../app/labels";

export const HEALTH_LABELS: Record<WorkerHealth, string> = {
  STARTING: "Đang khởi động",
  READY: "Sẵn sàng",
  SUSPECT: "Nghi ngờ (mất heartbeat)",
  UNAVAILABLE: "Không khả dụng",
};

export const HEALTH_TONE: Record<WorkerHealth, Tone> = {
  STARTING: "pending",
  READY: "success",
  SUSPECT: "pending",
  UNAVAILABLE: "danger",
};

export const ADMIN_STATE_LABELS: Record<WorkerAdminState, string> = {
  ENABLED: "Đang bật",
  DRAINING: "Ngừng nhận job",
  DISABLED: "Đã tắt",
};

export const ADMIN_STATE_TONE: Record<WorkerAdminState, Tone> = {
  ENABLED: "success",
  DRAINING: "pending",
  DISABLED: "neutral",
};

export const ALLOCATION_STATE_LABELS: Record<AllocationState, string> = {
  HELD: "Đang giữ",
  QUARANTINED: "Chờ xác nhận dọn dẹp (QUARANTINED)",
  RELEASED: "Đã trả",
};

const RECOVERY_EVENT_LABELS: Record<string, string> = {
  ATTEMPT_FAILED: "Lần chạy thất bại",
  ATTEMPT_FENCED: "Lần chạy bị thu hồi quyền (fence)",
  ATTEMPT_LOST: "Mất liên lạc với lần chạy",
  LEASE_REVOKED: "Lease bị thu hồi",
  ALLOCATION_RELEASED: "Tài nguyên đã được trả",
  RETRY_READY: "Sẵn sàng chạy lại",
  RETRY_BLOCKED: "Không thể chạy lại",
  PAUSE_ABORTED: "Tạm dừng bị hủy",
  CHECKPOINT_CORRUPT: "Checkpoint hỏng",
  CHECKPOINT_INCOMPATIBLE: "Checkpoint không tương thích",
  CHECKPOINT_REJECTED: "Checkpoint bị từ chối",
  CHECKPOINT_RESTORE_SELECTED: "Chọn checkpoint để khôi phục",
  CHECKPOINT_FALLBACK_TO_INPUT: "Không dùng được checkpoint, chạy lại từ đầu vào",
  CHECKPOINT_RESTORE_UNAVAILABLE: "Không có checkpoint để khôi phục",
};

export function recoveryEventLabel(type: string): string {
  return RECOVERY_EVENT_LABELS[type] ?? type;
}

export const AUDIT_ACTION_LABELS: Record<string, string> = {
  "admin.allocation.list": "Xem allocation",
  "admin.audit.list": "Xem audit",
  "admin.fairness.query": "Xem báo cáo fairness",
  "admin.job.get": "Xem chi tiết job",
  "admin.job.list": "Xem hàng chờ",
  "admin.membership.delete": "Xóa thành viên",
  "admin.membership.list": "Xem thành viên",
  "admin.membership.upsert": "Thêm/đổi vai trò thành viên",
  "admin.policy.global.get": "Xem chính sách hệ thống",
  "admin.policy.global.update": "Sửa chính sách hệ thống",
  "admin.policy.tenant.get": "Xem chính sách tenant",
  "admin.policy.tenant.update": "Sửa chính sách tenant",
  "admin.recovery_event.list": "Xem sự kiện khôi phục",
  "admin.tenant.create": "Tạo tenant",
  "admin.tenant.get": "Xem tenant",
  "admin.tenant.list": "Xem danh sách tenant",
  "admin.tenant.update": "Sửa tenant",
  "admin.user.create": "Tạo user",
  "admin.user.get": "Xem user",
  "admin.user.list": "Xem danh sách user",
  "admin.user.update": "Sửa user",
  "admin.worker.disable": "Tắt worker",
  "admin.worker.drain": "Ngừng nhận job trên worker",
  "admin.worker.enable": "Bật lại worker",
  "admin.worker.get": "Xem worker",
  "admin.worker.list": "Xem danh sách worker",
  "auth.login": "Đăng nhập",
  "auth.logout": "Đăng xuất",
  "identity.bootstrap_admin": "Tạo quản trị viên đầu tiên",
  "job.cancel": "Hủy job",
  "job.pause": "Tạm dừng job",
  "job.resume": "Tiếp tục job",
  "job.retry": "Chạy lại job",
  "job.submit": "Gửi job",
  "token.create": "Tạo token CLI",
  "token.revoke": "Thu hồi token CLI",
  "artifact.upload.commit": "Tải dữ liệu lên",
  "template.version.register": "Đăng ký phiên bản template",
  "worker.bootstrap_credential": "Cấp credential cho worker",
  "worker.bootstrap_window.reopen": "Mở lại cửa sổ đăng ký worker",
  "worker.lock": "Khóa worker",
};

export function auditActionLabel(action: string): string {
  return AUDIT_ACTION_LABELS[action] ?? action;
}
