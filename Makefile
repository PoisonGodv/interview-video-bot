.PHONY: install test lint fmt integration eval web

# 本地兜底入口(CI 在 .github/workflows/ci.yml, Gitea 镜像同样读取)
install:            ## 安装依赖(含 dev)
	uv sync --extra dev
	uv run playwright install chromium

test:               ## 离线单测(不联网/不调 LLM/不起浏览器)
	uv run pytest -q

lint:               ## ruff 静态检查
	uv run ruff check src tests

fmt:                ## ruff 自动修复
	uv run ruff check --fix src tests

integration:        ## 集成测试(需要 ffmpeg/网络/chromium)
	uv run pytest -q --run-integration

eval:               ## 生成质量评测(需要 LLM_API_KEY)
	uv run ivb eval

web:                ## 启动 Web 操作台
	uv run ivb web
