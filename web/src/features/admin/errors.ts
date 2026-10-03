// Admin part of the error mapping (docs/web-ui.md "Ánh xạ lỗi"): shared B17 text plus admin wording.
import { ApiError } from "../../api/client";
import { describeError, type ErrorView } from "../../api/errors";

export const ADMIN_REQUIRED = "Cần quyền quản trị hệ thống";
export const POLICY_CONFLICT_HINT = "Hãy ngừng nhận job (drain) hoặc chờ job đang chạy kết thúc rồi thử lại";

export function adminErrorView(error: unknown): ErrorView {
  const view = describeError(error);
  if (view.code === "permission_denied") return { ...view, title: ADMIN_REQUIRED, hint: null };
  return view;
}

/** 412: the form was reloaded with the server's object; the next send is a new intent. */
export function conflictMessage(before: number, after: number): string {
  return `Đối tượng vừa được thay đổi (v${before} → v${after}). Kiểm tra rồi gửi lại.`;
}

const ALIASES: Record<string, string> = {
  "tenant slug": "slug",
  username: "username",
  "display name": "display_name",
  password: "password",
  "global outstanding limit": "global_outstanding_limit",
};

/** Field of a 400/422 validation message, so the message is shown next to it; null otherwise. */
export function validationField<F extends string>(error: unknown, fields: readonly F[]): F | null {
  if (!(error instanceof ApiError) || error.code !== "validation_failed" || !error.serverMessage) return null;
  const message = error.serverMessage;
  const lower = message.toLowerCase();
  for (const [phrase, field] of Object.entries(ALIASES)) {
    if (lower.includes(phrase) && (fields as readonly string[]).includes(field)) return field as F;
  }
  // Policy messages start with the wire name: "<field> must be positive", "resource_limit.cpu_millis …".
  const leading = /^([a-z_]+)/.exec(message)?.[1];
  return fields.find((field) => field === leading) ?? null;
}

/** Server text of a 400/422 shown next to the field validationField() picked. */
export function serverFieldMessage(error: unknown): string | null {
  return error instanceof ApiError ? error.serverMessage : null;
}

/** Contract ETags are the strong version `"v<version>"`. */
export function versionFromEtag(etag: string | null): number | null {
  const match = etag === null ? null : /^"v(\d+)"$/.exec(etag);
  return match ? Number(match[1]) : null;
}
