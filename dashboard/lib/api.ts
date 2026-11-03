const TOKEN_KEY = "llmgate_admin_token";

export function gatewayUrl(): string {
  return (process.env.NEXT_PUBLIC_GATEWAY_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
}

export function getToken(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return window.sessionStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  window.sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.sessionStorage.removeItem(TOKEN_KEY);
}

export async function adminFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  headers.set("authorization", `Bearer ${token}`);
  if (init.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(`${gatewayUrl()}${path}`, { ...init, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}
