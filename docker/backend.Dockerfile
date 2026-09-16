# === Claw 鹰爪量化交易系统 — 后端 Dockerfile ===
# 多阶段构建: 依赖安装 → 运行镜像

FROM python:3.12-slim AS base

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层(缓存优化)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY . .

# 非root用户
RUN groupadd -r claw && useradd -r -g claw claw && \
    chown -R claw:claw /app
USER claw

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
