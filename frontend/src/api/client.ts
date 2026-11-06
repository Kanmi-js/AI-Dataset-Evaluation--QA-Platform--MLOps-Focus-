// Thin fetch wrapper around the FastAPI backend.
//
// All requests go through the Vite dev proxy / nginx at the same origin under
// /api/v1, so no base URL host is needed. The bearer token is persisted in
// localStorage and attached automatically.

import type {
  DataItem,
  DataItemDetail,
  Dataset,
  DatasetWithItems,
  Token,
  User,
} from "./types";

const API_PREFIX = "/api/v1";
const TOKEN_KEY = "mlops_qa_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const resp = await fetch(`${API_PREFIX}${path}`, { ...options, headers });

  if (resp.status === 401) {
    setToken(null);
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

function json(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export const api = {
  // --- Auth ---
  async register(email: string, password: string, fullName?: string): Promise<User> {
    return request<User>("/auth/register", json({
      email,
      password,
      full_name: fullName ?? null,
    }));
  },

  async login(email: string, password: string): Promise<Token> {
    // OAuth2 password flow expects form-encoded body with `username`.
    const form = new URLSearchParams();
    form.set("username", email);
    form.set("password", password);
    const token = await request<Token>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    });
    setToken(token.access_token);
    return token;
  },

  async me(): Promise<User> {
    return request<User>("/auth/me");
  },

  logout(): void {
    setToken(null);
  },

  // --- Datasets ---
  async listDatasets(): Promise<Dataset[]> {
    return request<Dataset[]>("/datasets");
  },

  async createDataset(name: string, description?: string): Promise<Dataset> {
    return request<Dataset>("/datasets", json({ name, description: description ?? null }));
  },

  async getDataset(id: number): Promise<DatasetWithItems> {
    return request<DatasetWithItems>(`/datasets/${id}`);
  },

  async deleteDataset(id: number): Promise<void> {
    return request<void>(`/datasets/${id}`, { method: "DELETE" });
  },

  // --- Uploads & items ---
  async uploadItems(datasetId: number, files: FileList | File[]): Promise<DataItem[]> {
    const form = new FormData();
    Array.from(files).forEach((f) => form.append("files", f));
    return request<DataItem[]>(`/datasets/${datasetId}/items`, {
      method: "POST",
      body: form,
    });
  },

  async getItem(itemId: number): Promise<DataItemDetail> {
    return request<DataItemDetail>(`/items/${itemId}`);
  },

  async saveAnnotations(
    itemId: number,
    annotations: Record<string, unknown>,
    markReviewed = true,
  ): Promise<DataItem> {
    return request<DataItem>(`/items/${itemId}/annotations`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ annotations, mark_reviewed: markReviewed }),
    });
  },

  async reprocessItem(itemId: number): Promise<DataItem> {
    return request<DataItem>(`/items/${itemId}/reprocess`, { method: "POST" });
  },
};
