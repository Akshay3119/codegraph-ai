export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function clearIngestedData(): Promise<void> {
  const res = await fetch(`${API}/ingest`, { method: "DELETE" });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(
      typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`
    );
  }
}
