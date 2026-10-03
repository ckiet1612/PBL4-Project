// Global nav items: tenant areas when there is a tenant, Quản trị for system admins (B18-R13).
export interface NavItem {
  to: string;
  label: string;
}

export function globalNavItems(tenantId: string | null, systemAdmin: boolean): NavItem[] {
  const items: NavItem[] = [];
  if (tenantId !== null) {
    items.push({ to: `/t/${tenantId}/jobs`, label: "Jobs" }, { to: `/t/${tenantId}/data`, label: "Dữ liệu" });
  }
  if (systemAdmin) items.push({ to: "/admin", label: "Quản trị" });
  return items;
}
