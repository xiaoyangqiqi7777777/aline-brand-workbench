#!/usr/bin/env sh

set -eu

REQUIRED_PORTS="3000 5432 6379 8000 8080 9000 9001"

info() {
  printf '%s\n' "[check] $1"
}

fail() {
  printf '%s\n' "[error] $1" >&2
  exit 1
}

command -v git >/dev/null 2>&1 || fail "Git 未安装。"
command -v docker >/dev/null 2>&1 || fail "Docker 未安装。"

docker info >/dev/null 2>&1 || fail "Docker 服务未运行，请先启动 Docker Desktop。"
docker compose version >/dev/null 2>&1 || fail "Docker Compose 不可用。"

if [ ! -f .env ]; then
  fail "缺少 .env，请先执行：cp .env.example .env"
fi

info "Git: $(git --version)"
info "Docker: $(docker --version)"
info "Compose: $(docker compose version)"

if command -v lsof >/dev/null 2>&1; then
  for port in $REQUIRED_PORTS; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      fail "端口 $port 已被占用。可在 .env 中调整对应端口，或停止占用它的程序。"
    fi
  done
  info "默认端口均可用。"
else
  info "未找到 lsof，跳过端口占用检查。"
fi

docker compose config --quiet || fail "Compose 配置无效。"
info "Compose 配置有效。"

if command -v node >/dev/null 2>&1; then
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  [ "$node_major" = "22" ] || fail "本机 Node.js 应为 22.x，当前为 $(node --version)。"
  info "可选本地 Node.js: $(node --version)"
fi

if command -v python3 >/dev/null 2>&1; then
  python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  [ "$python_version" = "3.12" ] || fail "本机 Python 应为 3.12，当前为 $python_version。"
  info "可选本地 Python: $(python3 --version)"
fi

info "环境检查通过，可以执行：docker compose up --build"
