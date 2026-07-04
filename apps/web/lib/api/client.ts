import type {
  IntakeAnswersRequest,
  ProjectCreateRequest,
  ProjectCreateResponse,
  ProjectDetailResponse,
  ProjectResponse,
  ResumeStageRunResponse,
  StageRunDetailResponse,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function getApiBaseUrl() {
  const envBase = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "");

  if (typeof window !== "undefined") {
    return envBase || "/api/v1";
  }

  return envBase && envBase.startsWith("http") ? envBase : "http://localhost:8000/api/v1";
}

function formatApiMessage(status: number, detail: unknown) {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          return String(item.msg);
        }
        return JSON.stringify(item);
      })
      .join("；");
  }
  return `请求失败，状态码 ${status}`;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    headers,
  });

  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json") ? await response.json() : await response.text();

  if (!response.ok) {
    const detail = body && typeof body === "object" && "detail" in body ? body.detail : body;
    throw new ApiError(formatApiMessage(response.status, detail), response.status, detail);
  }

  return body as T;
}

export const apiClient = {
  listProjects() {
    return requestJson<ProjectResponse[]>("/projects");
  },

  createProject(payload: ProjectCreateRequest) {
    return requestJson<ProjectCreateResponse>("/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getProject(projectId: string) {
    return requestJson<ProjectDetailResponse>(`/projects/${projectId}`);
  },

  getStageRun(stageRunId: string) {
    return requestJson<StageRunDetailResponse>(`/stage-runs/${stageRunId}`);
  },

  submitIntakeAnswers(stageRunId: string, payload: IntakeAnswersRequest) {
    return requestJson<ResumeStageRunResponse>(`/stage-runs/${stageRunId}/intake-answers`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
};
