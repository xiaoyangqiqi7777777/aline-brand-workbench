import { Button, EmptyState, ErrorState, LoadingState } from "@/components/ui";
import { IntakeQuestions } from "@/features/intake/intake-questions";
import type {
  IntakeAnswer,
  IntakeResult,
  JsonValue,
  ProjectDetailResponse,
  StageRunDetailResponse,
  StageRunResponse,
} from "@/lib/api/types";

import { BRAND_SPEC_FIELDS } from "./fields";

type ProjectDetailProps = {
  activeRun: StageRunDetailResponse | null;
  isLoading: boolean;
  isPolling: boolean;
  isSubmittingAnswers: boolean;
  onRefresh: () => void;
  onSubmitIntakeAnswers: (intakeRunId: string, answers: IntakeAnswer[]) => Promise<void>;
  project: ProjectDetailResponse | null;
};

const statusLabels: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "生成中",
  SUCCEEDED: "已完成",
  FAILED: "失败",
};

function isIntakeResult(result: StageRunDetailResponse["result"]): result is IntakeResult {
  return Boolean(
    result &&
      typeof result === "object" &&
      "ready" in result &&
      "questions" in result &&
      Array.isArray(result.questions),
  );
}

function formatJsonValue(value: JsonValue | undefined) {
  if (value === undefined || value === null || value === "") {
    return "未填写";
  }
  if (Array.isArray(value)) {
    return value.length > 0 ? value.join("、") : "未填写";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function StageRunTimeline({ runs }: { runs: StageRunResponse[] }) {
  if (runs.length === 0) {
    return <EmptyState title="暂无任务">创建项目后会出现 Intake Run。</EmptyState>;
  }

  return (
    <ol className="run-list">
      {runs.map((run) => (
        <li key={run.id}>
          <span>
            <strong>{run.stage}</strong>
            <small>{run.id}</small>
          </span>
          <em className={`run-status run-status--${run.status.toLowerCase()}`}>
            {statusLabels[run.status] ?? run.status}
          </em>
        </li>
      ))}
    </ol>
  );
}

function ActiveRunPanel({
  activeRun,
  isPolling,
  isSubmittingAnswers,
  onSubmitIntakeAnswers,
}: Pick<
  ProjectDetailProps,
  "activeRun" | "isPolling" | "isSubmittingAnswers" | "onSubmitIntakeAnswers"
>) {
  if (!activeRun) {
    return <EmptyState title="暂无当前任务">请选择项目或创建新项目。</EmptyState>;
  }

  if (activeRun.status === "QUEUED" || activeRun.status === "RUNNING") {
    return (
      <LoadingState
        title={`${activeRun.stage} ${statusLabels[activeRun.status] ?? activeRun.status}`}
      />
    );
  }

  if (activeRun.status === "FAILED") {
    return (
      <ErrorState title={`${activeRun.stage} 任务失败`}>
        {activeRun.error_message ?? activeRun.error_code ?? "后端未返回错误信息。"}
      </ErrorState>
    );
  }

  if (activeRun.stage === "INTAKE" && isIntakeResult(activeRun.result)) {
    if (!activeRun.result.ready) {
      return (
        <IntakeQuestions
          intakeRunId={activeRun.id}
          isSubmitting={isSubmittingAnswers}
          onSubmit={onSubmitIntakeAnswers}
          result={activeRun.result}
        />
      );
    }

    return (
      <div className="success-panel">
        <span className="step-pill">Intake 完成</span>
        <h2>品牌信息已满足生成条件</h2>
        <p>当前 Intake Run 已成功完成。</p>
      </div>
    );
  }

  if (activeRun.stage === "DIRECTIONS" && activeRun.status === "SUCCEEDED") {
    return (
      <div className="success-panel">
        <span className="step-pill">品牌方向</span>
        <h2>品牌方向已生成</h2>
        <p>结果版本：{activeRun.result_version_id ?? "后端未返回版本 ID"}</p>
        <p>下一步可以进入品牌方向选择。</p>
      </div>
    );
  }

  return (
    <div className="success-panel">
      <span className="step-pill">{activeRun.stage}</span>
      <h2>{statusLabels[activeRun.status] ?? activeRun.status}</h2>
      {isPolling ? <p>正在同步最新状态。</p> : null}
    </div>
  );
}

export function ProjectDetail({
  activeRun,
  isLoading,
  isPolling,
  isSubmittingAnswers,
  onRefresh,
  onSubmitIntakeAnswers,
  project,
}: ProjectDetailProps) {
  if (isLoading) {
    return <LoadingState title="正在读取项目详情" />;
  }

  if (!project) {
    return <EmptyState title="请选择项目">左侧选择已有项目，或创建一个新项目。</EmptyState>;
  }

  return (
    <div className="detail-layout">
      <section className="detail-main">
        <header className="project-heading">
          <span className="step-pill">{project.current_stage}</span>
          <h1>{project.name}</h1>
          <p>
            项目状态：{project.status} · 版本 {project.version}
          </p>
          <Button onClick={onRefresh} variant="secondary">
            刷新状态
          </Button>
        </header>

        <ActiveRunPanel
          activeRun={activeRun}
          isPolling={isPolling}
          isSubmittingAnswers={isSubmittingAnswers}
          onSubmitIntakeAnswers={onSubmitIntakeAnswers}
        />
      </section>

      <aside className="detail-side">
        <section className="side-section">
          <h2>BrandSpec</h2>
          <dl className="spec-list">
            {BRAND_SPEC_FIELDS.map((field) => (
              <div key={field.key}>
                <dt>{field.label}</dt>
                <dd>{formatJsonValue(project.brand_spec[field.key])}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="side-section">
          <h2>Stage Runs</h2>
          <StageRunTimeline runs={project.stage_runs} />
        </section>
      </aside>
    </div>
  );
}
