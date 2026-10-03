// Report numbers: up to 3 decimals, vi-VN separators.
const NUMBER = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 3 });

export function formatNumber(value: number): string {
  return NUMBER.format(value);
}
