import { readFileSync } from "node:fs";
import { join } from "node:path";

// Shape written by scripts/b17_e2e_stack.py. Passwords exist only in that 0600 file and in
// memory here; specs never print them and storageState files stay in the harness state dir.
export interface SeedUser {
  username: string;
  password: string;
  user_id: string;
  system_admin: boolean;
  memberships: Record<string, "MEMBER" | "TENANT_ADMIN">;
}

export interface SeedArtifact {
  artifact_id: string;
  tenant_id: string;
  kind: string;
  media_type: string;
  checksum: string;
  size_bytes: number;
}

export interface Fixture {
  tier: "w1" | "w2";
  run_id: string;
  base_url: string;
  storage_dir: string;
  tenants: { a: string; b: string; q: string };
  users: Record<UserKey, SeedUser>;
  templates: Record<string, string>;
  artifacts: Record<string, SeedArtifact>;
  worker_id: string;
}

export type UserKey =
  | "admin"
  | "member_a"
  | "member_a2"
  | "admin_a"
  | "member_b"
  | "multi"
  | "quota"
  | "login"
  | "mobile";

/** Users that get a stored browser session; `login` always signs in through the form. */
export const SESSION_USERS: readonly UserKey[] = [
  "admin",
  "member_a",
  "member_a2",
  "admin_a",
  "member_b",
  "multi",
  "quota",
  "mobile",
];

let cached: Fixture | undefined;

export function fixture(): Fixture {
  if (cached) return cached;
  const path = process.env.NEXA_B17_FIXTURE;
  if (!path) throw new Error("NEXA_B17_FIXTURE is not set: run through scripts/b17_e2e_stack.py");
  cached = JSON.parse(readFileSync(path, "utf8")) as Fixture;
  return cached;
}

export function storageState(user: UserKey): string {
  return join(fixture().storage_dir, `${user}.json`);
}
