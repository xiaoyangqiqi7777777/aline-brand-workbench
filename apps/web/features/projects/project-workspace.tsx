"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";

import { Button, Dialog, ErrorState, LoadingState } from "@/components/ui";
import { apiClient, ApiError } from "@/lib/api/client";
import { pollStageRun } from "@/lib/api/polling";
import type {
  IntakeAnswer,
  ProjectCreateRequest,
  ProjectDetailResponse,
  ProjectResponse,
  ResumeStageRunResponse,
  StageRunDetailResponse,
  StageRunResponse,
} from "@/lib/api/types";

import { ProjectDetail } from "./project-detail";
import { ProjectForm } from "./project-form";
import { ProjectList } from "./project-list";

const LAST_PROJECT_KEY = "brand-agent-studio:last-project-id";
const RUNNING_STATUSES = new Set(["QUEUED", "RUNNING"]);
const STAGE_RANK: Record<string, number> = {
  INTAKE: 1,
  DIRECTIONS: 2,
  LOGO: 3,
  VI: 4,
  IP: 5,
  MATERIALS: 6,
  REVIEW: 7,
  PROPOSAL: 8,
};

function sortProjects(projects: ProjectResponse[]) {
  return [...projects].sort(
    (left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime(),
  );
}

function pickLatestRun(runs: StageRunResponse[]) {
  return runs
    .map((run, index) => ({ index, rank: STAGE_RANK[run.stage] ?? 0, run }))
    .sort((left, right) => right.rank - left.rank || right.index - left.index)[0]?.run;
}

function toRunDetail(run: StageRunResponse | ResumeStageRunResponse): StageRunDetailResponse {
  return {
    id: run.id,
    project_id: run.project_id,
    stage: run.stage,
    status: run.status,
    attempt: "attempt" in run ? run.attempt : 0,
    error_code: "error_code" in run ? run.error_code : null,
    error_message: null,
    result_version_id: "result_version_id" in run ? run.result_version_id : null,
    result: null,
  };
}

function getErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "发生未知错误";
}

type ProjectWorkspaceProps = {
  initialProjectId?: string;
};

export function ProjectWorkspace({ initialProjectId }: ProjectWorkspaceProps) {
  const router = useRouter();
  const [activeRun, setActiveRun] = useState<StageRunDetailResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [isLoadingDetail, setIsLoadingDetail] = useState(false);
  const [isLoadingProjects, setIsLoadingProjects] = useState(true);
  const [isPolling, setIsPolling] = useState(false);
  const [isSubmittingAnswers, setIsSubmittingAnswers] = useState(false);
  const [isSubmittingProject, setIsSubmittingProject] = useState(false);
  const [pollingRunId, setPollingRunId] = useState<string | null>(null);
  const [projectDetail, setProjectDetail] = useState<ProjectDetailResponse | null>(null);
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(
    initialProjectId ?? null,
  );

  const selectedProjectName = useMemo(
    () => projects.find((project) => project.id === selectedProjectId)?.name ?? "未选择项目",
    [projects, selectedProjectId],
  );

  const loadProjectDetail = useCallback(async (projectId: string) => {
    setIsLoadingDetail(true);
    setErrorMessage(null);
    try {
      const detail = await apiClient.getProject(projectId);
      setProjectDetail(detail);
      const latestRun = pickLatestRun(detail.stage_runs);
      if (!latestRun) {
        setActiveRun(null);
        setPollingRunId(null);
        return;
      }

      const runDetail = await apiClient.getStageRun(latestRun.id);
      setActiveRun(runDetail);
      if (RUNNING_STATUSES.has(runDetail.status)) {
        setPollingRunId(runDetail.id);
      } else {
        setPollingRunId(null);
      }
    } catch (error) {
      setActiveRun(null);
      setErrorMessage(getErrorMessage(error));
      setPollingRunId(null);
      setProjectDetail(null);
    } finally {
      setIsLoadingDetail(false);
    }
  }, []);

  const loadProjects = useCallback(async () => {
    setIsLoadingProjects(true);
    setErrorMessage(null);
    try {
      const nextProjects = sortProjects(await apiClient.listProjects());
      setProjects(nextProjects);
      setSelectedProjectId((current) => {
        if (initialProjectId && nextProjects.some((project) => project.id === initialProjectId)) {
          return initialProjectId;
        }
        if (current && nextProjects.some((project) => project.id === current)) {
          return current;
        }
        const remembered =
          typeof window !== "undefined" ? window.localStorage.getItem(LAST_PROJECT_KEY) : null;
        if (remembered && nextProjects.some((project) => project.id === remembered)) {
          return remembered;
        }
        return nextProjects[0]?.id ?? null;
      });
    } catch (error) {
      setErrorMessage(getErrorMessage(error));
    } finally {
      setIsLoadingProjects(false);
    }
  }, [initialProjectId]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    if (initialProjectId) {
      setSelectedProjectId(initialProjectId);
    }
  }, [initialProjectId]);

  useEffect(() => {
    if (!selectedProjectId) {
      setProjectDetail(null);
      setActiveRun(null);
      return;
    }

    window.localStorage.setItem(LAST_PROJECT_KEY, selectedProjectId);
    void loadProjectDetail(selectedProjectId);
  }, [loadProjectDetail, selectedProjectId]);

  useEffect(() => {
    if (!pollingRunId) {
      return;
    }

    const controller = new AbortController();
    setIsPolling(true);
    pollStageRun(pollingRunId, {
      signal: controller.signal,
      onUpdate: setActiveRun,
    })
      .then((run) => {
        setActiveRun(run);
        setPollingRunId(null);
        void loadProjectDetail(run.project_id);
        void loadProjects();
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setErrorMessage(getErrorMessage(error));
          setPollingRunId(null);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsPolling(false);
        }
      });

    return () => {
      controller.abort();
      setIsPolling(false);
    };
  }, [loadProjectDetail, loadProjects, pollingRunId]);

  async function handleCreateProject(payload: ProjectCreateRequest) {
    setIsSubmittingProject(true);
    setErrorMessage(null);
    try {
      const created = await apiClient.createProject(payload);
      setProjects((current) => sortProjects([created.project, ...current]));
      setSelectedProjectId(created.project.id);
      setActiveRun(toRunDetail(created.stage_run));
      setPollingRunId(created.stage_run.id);
      setIsCreateOpen(false);
      router.push(`/projects/${created.project.id}`);
    } catch (error) {
      setErrorMessage(getErrorMessage(error));
    } finally {
      setIsSubmittingProject(false);
    }
  }

  async function handleSubmitIntakeAnswers(intakeRunId: string, answers: IntakeAnswer[]) {
    setIsSubmittingAnswers(true);
    setErrorMessage(null);
    try {
      const resumedRun = await apiClient.submitIntakeAnswers(intakeRunId, { answers });
      setActiveRun(toRunDetail(resumedRun));
      setPollingRunId(resumedRun.id);
    } catch (error) {
      setErrorMessage(getErrorMessage(error));
    } finally {
      setIsSubmittingAnswers(false);
    }
  }

  function handleSelectProject(projectId: string) {
    setActiveRun(null);
    setPollingRunId(null);
    setSelectedProjectId(projectId);
    router.push(`/projects/${projectId}`);
  }

  function handleRefresh() {
    void loadProjects();
    if (selectedProjectId) {
      void loadProjectDetail(selectedProjectId);
    }
  }

  return (
    <main className="workspace-shell">
      <aside className="workspace-sidebar">
        <header className="brand-header">
          <span className="eyebrow">Brand Agent Studio</span>
          <h1>品牌项目</h1>
          <p>创建品牌项目，补齐生成前的信息。</p>
        </header>

        <div className="sidebar-actions">
          <Button onClick={() => setIsCreateOpen(true)}>新建项目</Button>
          <Button onClick={handleRefresh} variant="secondary">
            刷新
          </Button>
        </div>

        {isLoadingProjects ? (
          <LoadingState title="正在读取项目列表" />
        ) : (
          <ProjectList
            onSelect={handleSelectProject}
            projects={projects}
            selectedProjectId={selectedProjectId}
          />
        )}
      </aside>

      <section className="workspace-content">
        <header className="content-topbar">
          <span>当前项目</span>
          <strong>{selectedProjectName}</strong>
          {isPolling ? <em>正在轮询任务状态</em> : null}
        </header>

        {errorMessage ? (
          <ErrorState actionLabel="重新加载" onAction={handleRefresh} title="操作失败">
            {errorMessage}
          </ErrorState>
        ) : null}

        <ProjectDetail
          activeRun={activeRun}
          isLoading={isLoadingDetail}
          isPolling={isPolling}
          isSubmittingAnswers={isSubmittingAnswers}
          onRefresh={handleRefresh}
          onSubmitIntakeAnswers={handleSubmitIntakeAnswers}
          project={projectDetail}
        />
      </section>

      <Dialog isOpen={isCreateOpen} onClose={() => setIsCreateOpen(false)} title="新建项目">
        <ProjectForm isSubmitting={isSubmittingProject} onSubmit={handleCreateProject} />
      </Dialog>
    </main>
  );
}
