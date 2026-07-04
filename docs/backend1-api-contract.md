# Backend 1 API Contract

更新时间：2026-07-04

本文档记录后端 1 当前已经落库并可用于联调的项目、阶段版本和决策 API。业务数据以 PostgreSQL 为准；LangGraph checkpoint 只用于恢复 Agent 执行。

## Stage Keys

路径中的 `stage_key` 支持大小写不敏感的短横线/下划线写法，服务端会标准化为大写下划线：

```text
intake
directions
logo
vi
ip
materials
review
proposal
```

非法 stage key 返回：

```json
{"detail": "Invalid stage key: nope"}
```

HTTP 状态码为 `422`。

## Projects

### POST `/api/v1/projects`

创建项目并排队首个 `INTAKE` StageRun。

请求体：

```json
{
  "name": "云山咖啡",
  "requirement_text": "做一个现代、克制的咖啡品牌。",
  "structured_fields": {
    "industry": "精品咖啡",
    "target_audiences": ["城市通勤者"]
  },
  "reference_artifact_ids": []
}
```

成功：`201`

```json
{
  "project": {
    "id": "project-uuid",
    "workspace_id": "local-workspace",
    "name": "云山咖啡",
    "current_stage": "INTAKE",
    "status": "ACTIVE",
    "version": 1
  },
  "stage_run": {
    "id": "run-uuid",
    "project_id": "project-uuid",
    "stage": "INTAKE",
    "status": "QUEUED",
    "attempt": 0,
    "error_code": null,
    "result_version_id": null
  }
}
```

失败：

- `422`：项目名为空或 `structured_fields` 包含不支持的 BrandSpec 字段。

### GET `/api/v1/projects`

返回当前 workspace 下项目列表，按 `updated_at desc` 排序。

### GET `/api/v1/projects/{project_id}`

返回项目详情、BrandSpec 和所有 StageRun 的简要信息。

失败：

- `404`：项目不存在或不属于当前 workspace。

### GET `/api/v1/projects/{project_id}/state`

页面刷新恢复接口。返回项目、BrandSpec、每个阶段最新 StageRun、每个阶段最新 StageVersion 和所有 Decision。

关键响应字段：

```json
{
  "project": {},
  "brand_spec": {},
  "current_stage": "LOGO",
  "stage_runs": {
    "DIRECTIONS": {"status": "SUCCEEDED"},
    "LOGO": {"status": "QUEUED"}
  },
  "versions": {
    "DIRECTIONS": {"status": "GENERATED"},
    "LOGO": {"status": "STALE"}
  },
  "decisions": []
}
```

`versions.*.status` 可能为：

- `GENERATED`：当前仍有效。
- `STALE`：上游阶段已有新选择，旧下游输出仅可作为历史版本展示。

失败：

- `404`：项目不存在或不属于当前 workspace。

## Stage Versions

### GET `/api/v1/projects/{project_id}/stages/{stage_key}/versions`

返回某项目某阶段的版本历史，按 `version_no desc` 排序。

成功：`200`

每个版本包含：

```json
{
  "id": "version-uuid",
  "project_id": "project-uuid",
  "stage_run_id": "run-uuid",
  "stage": "DIRECTIONS",
  "version_no": 1,
  "schema_version": 1,
  "input_refs": {},
  "output": {},
  "status": "GENERATED",
  "created_at": "2026-07-04T00:00:00Z"
}
```

失败：

- `404`：项目不存在或不属于当前 workspace。
- `422`：stage key 非法。

## Decisions

### POST `/api/v1/projects/{project_id}/stages/{stage_key}/decisions`

项目级阶段决策入口。当前 worker milestone 只执行 `stage_key=directions` 的 `SELECT_VERSION`，用于选择一个 Directions 版本中的方向并排队下一阶段 `LOGO` StageRun。

请求体：

```json
{
  "version_id": "directions-version-uuid",
  "selected_item_id": "direction-a",
  "action": "SELECT_VERSION"
}
```

确认类决策的契约骨架已经固定，但当前 milestone 暂不执行后续阶段：

```json
{
  "version_id": "vi-version-uuid",
  "action": "CONFIRM_VERSION",
  "confirmed": true
}
```

成功：`202`

```json
{
  "decision": {
    "stage": "DIRECTIONS",
    "action": "SELECT_VERSION",
    "source_version_id": "directions-version-uuid",
    "selected_item_id": "direction-a",
    "resulting_stage_run_id": "logo-run-uuid"
  },
  "stage_run": {
    "id": "logo-run-uuid",
    "stage": "LOGO",
    "status": "QUEUED"
  }
}
```

当前支持的 `action`：

- `SELECT_VERSION`：必须传 `selected_item_id`。当前仅 `directions` 会真实排队下一阶段。
- `CONFIRM_VERSION`：必须传 `confirmed=true`。当前只做请求/归属校验，然后返回 milestone 未支持的 `409`。

幂等规则：

- 对同一个 `version_id` 和同一个 `selected_item_id` 重复提交，返回原 Decision 和原 StageRun，不重复派发 worker。
- 对同一个 `version_id` 提交不同 `selected_item_id`，返回 `409`。

下游过期规则：

- 创建新决策时，会将该阶段之后的已有 StageVersion 标记为 `STALE`。
- 例如重新选择 Directions 后，旧 Logo / VI / IP / Materials / Review / Proposal 版本会变为 `STALE`。

状态转换：

- 首次创建新的 `LOGO` StageRun 时，`project.current_stage` 会立即推进为 `LOGO`，即使 worker 仍处于 `QUEUED`。

失败：

- `404`：项目不存在，或版本不存在/不属于该项目。
- `422`：stage key 非法、action 非法，或 action 所需字段缺失。
- `409`：版本阶段与路径阶段不一致、选择项不存在、重复选择冲突，或当前 milestone 暂不支持该 stage。

当前限制：

- `logo` 的 `SELECT_VERSION` 会校验 `selected_item_id` 是否存在于该 Logo 版本；校验通过后仍暂返回 `409`，因为 Logo 选择进入 VI 的落库执行仍需后端 2 的 workflow milestone 对齐。
- `vi` / `ip` / `materials` / `review` / `proposal` 的 `CONFIRM_VERSION` 暂返回 `409`，等待对应 worker milestone 对齐后再补执行。

## Stage Control Skeletons

### POST `/api/v1/projects/{project_id}/stages/{stage_key}/redo`

### POST `/api/v1/projects/{project_id}/stages/{stage_key}/skip`

这两个接口当前只固化契约和错误语义，尚不执行真实状态转换。

请求体可省略，也可以传：

```json
{
  "source_version_id": "stage-version-uuid",
  "reason": "try another option"
}
```

当前行为：

- 校验 project / workspace / stage。
- 如果传入 `source_version_id`，会校验该版本存在、属于当前项目、且阶段与路径 `stage_key` 一致。
- 校验通过后仍返回当前 milestone 未支持的 `409`，不创建 StageRun，不派发 worker。

失败：

- `404`：项目不存在或不属于当前 workspace。
- `404`：传入的 `source_version_id` 不存在或不属于当前项目。
- `422`：stage key 非法。
- `409`：传入的 `source_version_id` 阶段与路径阶段不一致。
- `409`：当前 worker milestone 暂不支持该 stage/action。

## Legacy StageRun Entrypoints

### POST `/api/v1/stage-runs/{stage_run_id}/intake-answers`

旧入口，用于提交 Intake 补问答案并排队 `DIRECTIONS` StageRun。前端 1 仍可使用。

请求体：

```json
{
  "answers": [
    {
      "field_path": "industry",
      "value": "茶饮"
    }
  ]
}
```

成功：`202`

返回新建或已存在的 `DIRECTIONS` StageRun。

状态转换：

- 首次创建新的 `DIRECTIONS` StageRun 时，`project.current_stage` 会立即推进为 `DIRECTIONS`，即使 worker 仍处于 `QUEUED`。
- 首次创建新的 `DIRECTIONS` StageRun 时，会将 `INTAKE` 之后的已有 StageVersion 标记为 `STALE`，包括旧 Directions / Logo / VI / IP / Materials / Review / Proposal 版本。

幂等规则：

- 同一个 Intake StageRun 和完全相同的 answers 重复提交，返回原 `DIRECTIONS` StageRun，不重复派发 worker。

错误语义：

- `404`：StageRun 不存在或不属于当前 workspace。
- `409`：StageRun 不是已成功的 `INTAKE`，或 Intake 没有可 resume 的结果。

### POST `/api/v1/stage-runs/{stage_run_id}/direction-selection`

旧入口，用于选择方向并排队 `LOGO` StageRun。建议新联调优先使用项目级：

```text
POST /api/v1/projects/{project_id}/stages/directions/decisions
```

旧入口错误语义：

- `404`：StageRun 不存在或不属于当前 workspace。
- `409`：StageRun 状态不允许选择、版本不匹配、选择项不存在或重复选择冲突。

### GET `/api/v1/stage-runs/{stage_run_id}`

返回 StageRun 详情和其 `result_version_id` 对应版本的 `output`。

失败：

- `404`：StageRun 不存在或不属于当前 workspace。

## Ownership Notes

- 后端 1 只返回 artifact ID 和版本输出，不拼 MinIO URL。
- 安全资产 URL / 短链由后端 3 storage adapter 提供。
- `redo` / `skip` 的真实执行和后续 VI/IP/Materials/Review/Proposal 落库，需要等待对应 Agent workflow milestone 对齐后再补。
