// Show/hide matrix for job controls (docs/web-ui.md §8). A UI hint only: the backend decides.
import type { DesiredState, Job, JobState, Role } from "../../api/types";

type JobStates = Pick<Job, "state" | "desired_state">;

const TERMINAL = new Set<JobState>(["SUCCEEDED", "FAILED", "CANCELLED"]);
const CANCELLABLE = new Set<JobState>([
  "QUEUED",
  "DISPATCHING",
  "RUNNING",
  "PAUSING",
  "PAUSED",
  "RECOVERING",
  "RETRY_WAIT",
]);

export function isTerminal(state: JobState): boolean {
  return TERMINAL.has(state);
}

export interface ActionAvailability {
  cancel: boolean;
  /** disabled = shown with the reason "Template này không hỗ trợ tạm dừng" (UX-A07). */
  pause: "hidden" | "enabled" | "disabled";
  resume: boolean;
  retry: boolean;
}

export function availableActions(job: JobStates, checkpointable: boolean): ActionAvailability {
  const desired: DesiredState = job.desired_state;
  const pauseEligible = job.state === "RUNNING" && desired === "RUNNING";
  return {
    cancel: CANCELLABLE.has(job.state) && desired !== "CANCELLED",
    pause: pauseEligible ? (checkpointable ? "enabled" : "disabled") : "hidden",
    resume: job.state === "PAUSED" && desired === "PAUSED",
    retry: job.state === "FAILED",
  };
}

export function canControl(job: Pick<Job, "user_id">, session: { user_id: string }, role: Role): boolean {
  return role === "TENANT_ADMIN" || job.user_id === session.user_id;
}

/** Explains a desired state the backend has not confirmed yet. */
export function pendingNote(job: JobStates): string | null {
  if (isTerminal(job.state)) return null;
  if (job.desired_state === "CANCELLED") return "Đã yêu cầu hủy, chờ hệ thống xác nhận dừng";
  if (job.state === "PAUSING" || (job.desired_state === "PAUSED" && job.state !== "PAUSED")) {
    return "Đã yêu cầu tạm dừng, chờ checkpoint";
  }
  if (job.state === "RECOVERING") {
    return "Hệ thống đang khôi phục job sau sự cố; chưa xác nhận lần chạy trước đã dừng";
  }
  if (job.state === "PAUSED" && job.desired_state === "RUNNING") {
    return "Đã yêu cầu tiếp tục, chờ hệ thống xếp lịch";
  }
  return null;
}

export type DetailTab = "progress" | "result" | "events" | "logs" | "config";

export const DETAIL_TABS: DetailTab[] = ["progress", "result", "events", "logs", "config"];

export function defaultTab(state: JobState): DetailTab {
  return state === "SUCCEEDED" ? "result" : "progress";
}
