import type { MealSpec, RunRow } from "./types";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {}

async function asJson<T>(resp: Response): Promise<T> {
  if (resp.ok) return resp.json() as Promise<T>;
  let detail = `${resp.status} ${resp.statusText}`;
  try {
    const body = await resp.json();
    if (Array.isArray(body.detail)) {
      detail = body.detail.map((d: { loc: string[]; msg: string }) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ");
    } else if (body.detail) {
      detail = String(body.detail);
    }
  } catch {
    // keep the status text
  }
  throw new ApiError(detail);
}

export async function startRun(body: MealSpec | { text: string }): Promise<{ run_id: string }> {
  let resp: Response;
  try {
    resp = await fetch(`${API_URL}/api/runs`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(`Can't reach the MealCart server at ${API_URL}. Start it with: uvicorn app.main:app --app-dir backend`);
  }
  return asJson(resp);
}

export async function getRun(runId: string): Promise<RunRow | null> {
  const resp = await fetch(`${API_URL}/api/runs/${runId}`);
  if (resp.status === 404) return null;
  return asJson(resp);
}

export function eventsUrl(runId: string, since: number): string {
  return `${API_URL.replace(/^http/, "ws")}/api/runs/${runId}/events?since=${since}`;
}
