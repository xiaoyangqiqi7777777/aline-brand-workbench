# Brand Agent Studio

面向五人团队协作的品牌生成工作台。当前 `main` 提供开发环境基线，不包含业务功能。

## 环境要求

所有成员只需安装：

- Git
- Docker Desktop（包含 Docker Compose）

可选的本地开发工具：

- Node.js 22（见 `.nvmrc`）
- Python 3.12（见 `.python-version`）
- uv

## 第一次启动

```bash
cp .env.example .env
./scripts/check-environment.sh
docker compose up --build
```

启动完成后可访问：

- 统一入口：<http://localhost:8080>
- Web：<http://localhost:3000>
- API 文档：<http://localhost:8000/api/docs>
- API 就绪检查：<http://localhost:8000/api/v1/health/ready>
- MinIO 控制台：<http://localhost:9001>

停止环境：

```bash
docker compose down
```

需要同时删除本地数据库、缓存和对象存储数据时：

```bash
docker compose down -v
```

## 本地运行

前端：

```bash
npm ci
npm run dev:web
```

后端：

```bash
uv sync
uv run uvicorn app.main:app --app-dir apps/api --reload --port 8000
```

## 目录边界

```text
apps/web/                  Next.js Web
apps/api/                  FastAPI API
backend/agents/            Agent、Schema、Prompt、LangGraph
backend/providers/         模型 Provider
backend/infrastructure/    数据库与对象存储适配器
backend/exports/           PDF、PPTX、ZIP 导出
infra/nginx/               本地统一入口
infra/migrations/          数据库迁移
scripts/                   开发与验收脚本
tests/                     后端与 E2E 测试
```

## 分支与提交

- 从最新 `main` 创建个人功能分支，不直接在 `main` 开发。
- 分支使用 `feat/frontend-1-*`、`feat/frontend-2-*`、`feat/backend-1-*` 等角色前缀。
- 不提交 `.env`、密钥、数据库文件、对象存储数据、依赖目录或构建产物。
- API/Schema 变更应同时提交契约、迁移（如需要）、测试和说明。
