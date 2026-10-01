// The only place with Vietnamese labels for backend enums. Pages never spell states themselves.
import type {
  ArtifactKind,
  AttemptState,
  FailureClass,
  JobEvent,
  JobState,
  Role,
  TokenScope,
  WaitingReason,
} from "../api/types";

export const JOB_STATE_LABELS: Record<JobState, string> = {
  QUEUED: "Đang xếp hàng",
  DISPATCHING: "Đang cấp phát",
  RUNNING: "Đang chạy",
  PAUSING: "Đang tạm dừng (chờ checkpoint)",
  PAUSED: "Đã tạm dừng",
  RECOVERING: "Đang khôi phục (chờ xác nhận)",
  RETRY_WAIT: "Chờ chạy lại tự động",
  CANCELLING: "Đang hủy (chờ xác nhận dừng)",
  SUCCEEDED: "Hoàn tất",
  FAILED: "Thất bại",
  CANCELLED: "Đã hủy",
};

/** Semantic group for styling; the text label is always shown as well. */
export type Tone = "neutral" | "active" | "pending" | "success" | "danger";

export const STATE_TONE: Record<JobState, Tone> = {
  QUEUED: "neutral",
  DISPATCHING: "active",
  RUNNING: "active",
  PAUSING: "pending",
  PAUSED: "neutral",
  RECOVERING: "pending",
  RETRY_WAIT: "neutral",
  CANCELLING: "pending",
  SUCCEEDED: "success",
  FAILED: "danger",
  CANCELLED: "neutral",
};

export const WAITING_REASON_LABELS: Record<NonNullable<WaitingReason>, string> = {
  waiting_for_worker: "Chờ worker sẵn sàng",
  waiting_for_capacity: "Chờ đủ tài nguyên trống trên máy chủ",
  waiting_for_quota: "Chờ hạn mức của tenant",
  waiting_for_reservation: "Đang được giữ chỗ tài nguyên, chờ đủ để chạy",
  waiting_for_retry: "Chờ tới thời điểm chạy lại",
  waiting_for_compatibility: "Chờ worker tương thích với template",
};

export function waitingReasonLabel(reason: WaitingReason): string | null {
  return reason === null ? null : WAITING_REASON_LABELS[reason];
}

export const ATTEMPT_STATE_LABELS: Record<AttemptState, string> = {
  CREATED: "Mới tạo",
  CLAIMED: "Worker đã nhận",
  STARTING: "Đang khởi động",
  RUNNING: "Đang chạy",
  CHECKPOINTING: "Đang ghi checkpoint",
  STOPPING: "Đang dừng (chờ xác nhận)",
  SUCCEEDED: "Hoàn tất",
  FAILED: "Thất bại",
  LOST: "Mất kết nối",
  CANCELLED: "Đã hủy",
};

export const FAILURE_CLASS_LABELS: Record<FailureClass, string> = {
  INFRASTRUCTURE: "Lỗi hạ tầng",
  TIMEOUT: "Quá thời gian",
  OOM: "Hết bộ nhớ",
  INVALID_INPUT: "Dữ liệu vào không hợp lệ",
  USER_CANCEL: "Người dùng hủy",
  INCOMPATIBLE: "Không tương thích",
  INTERNAL: "Lỗi nội bộ",
};

export const CHECKPOINT_STATE_LABELS: Record<"COMMITTED" | "REJECTED" | "CORRUPT", string> = {
  COMMITTED: "Đã ghi",
  REJECTED: "Bị từ chối",
  CORRUPT: "Hỏng",
};

export const ACTOR_LABELS: Record<JobEvent["actor_type"], string> = {
  USER: "Người dùng",
  ADMIN: "Quản trị viên",
  WORKER: "Worker",
  COORDINATOR: "Bộ điều phối",
  SYSTEM: "Hệ thống",
};

export const ARTIFACT_KIND_LABELS: Record<ArtifactKind, string> = {
  INPUT: "Đầu vào",
  DATASET: "Dataset",
  MODEL: "Model",
  CHECKPOINT_FILE: "Tệp checkpoint",
  CHECKPOINT_MANIFEST: "Manifest checkpoint",
  RESULT_FILE: "Tệp kết quả",
  RESULT_MANIFEST: "Manifest kết quả",
  CHUNK_OUTPUT_MANIFEST: "Manifest đầu ra theo chunk",
  LOG: "Log",
};

export const ROLE_LABELS: Record<Role, string> = {
  MEMBER: "Thành viên",
  TENANT_ADMIN: "Quản trị viên tenant",
};

export const TOKEN_SCOPE_LABELS: Record<TokenScope, string> = {
  "jobs:read": "Xem job",
  "jobs:write": "Tạo và điều khiển job",
  "artifacts:read": "Xem và tải dữ liệu",
  "artifacts:write": "Tải dữ liệu lên",
  "tokens:write": "Quản lý token",
  "admin:read": "Quản trị: xem",
  "admin:write": "Quản trị: thay đổi",
};
