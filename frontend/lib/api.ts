export class ApiError extends Error {
  errorCode?: string;
  description?: string;
  constructor(message: string, errorCode?: string, description?: string) {
    super(message);
    this.errorCode = errorCode;
    this.description = description;
  }
}

export async function apiFetch<T = unknown>(
  path: string,
  options?: RequestInit
): Promise<T> {
  // localStorage는 브라우저 환경에서만 접근 가능
  const token = typeof window !== "undefined" ? localStorage.getItem("auth_token") : null;

  const headers: Record<string, string> = {
    ...(options?.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (!headers["Content-Type"] && options?.body) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}${path}`, {
      ...options,
      headers,
    });
  } catch (err) {
    throw new ApiError("서버에 연결하지 못했습니다.", "NET-001");
  }

  if (res.status === 401) {
    if (typeof window !== "undefined") {
      localStorage.removeItem("auth_token");
      window.location.href = "/auth/login";
    }
    throw new ApiError("인증이 만료되었습니다. 다시 로그인해 주세요.", "AUTH-401");
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const b = body as { detail?: string; message?: string; error_code?: string; description?: string };
    throw new ApiError(
      b.detail ?? b.message ?? `요청에 실패했습니다. (HTTP ${res.status})`,
      b.error_code,
      b.description
    );
  }

  return res.json();
}

export async function apiPost<T = unknown>(
  path: string,
  body?: unknown
): Promise<T> {
  return apiFetch<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
}

export async function apiPatch<T = unknown>(
  path: string,
  body: unknown
): Promise<T> {
  return apiFetch<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function apiPut<T = unknown>(
  path: string,
  body: unknown
): Promise<T> {
  return apiFetch<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
