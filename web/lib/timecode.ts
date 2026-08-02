export function formatTimecode(seconds: number): string {
  const value = Math.max(0, Math.round(seconds));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor(value / 60);
  if (hours) {
    return `${hours}:${String(minutes % 60).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
  }
  return `${minutes}:${String(value % 60).padStart(2, "0")}`;
}

export function parseTimecode(value: string): number | null {
  const trimmed = value.trim();
  if (!/^\d+(?::\d{1,2}){0,2}$/.test(trimmed)) return null;

  const parts = trimmed.split(":").map(Number);
  if (parts.some((part) => !Number.isFinite(part))) return null;
  if (parts.length > 1 && parts.slice(1).some((part) => part >= 60)) return null;

  if (parts.length === 3) {
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }
  if (parts.length === 2) {
    return parts[0] * 60 + parts[1];
  }
  return parts[0];
}
