# 倪师 AI · 容器镜像
#
# 构建： docker build -t nishi-ai .
# 运行： docker run -d --name nishi-ai -p 8790:8790 --env-file .env \
#          -v $(pwd)/data:/app/data nishi-ai
#
# 选 slim 是因为本应用**零第三方依赖**，不需要构建工具链。

FROM python:3.12-slim

# 非 root 运行
RUN useradd -r -u 10001 -m -s /usr/sbin/nologin nishi

WORKDIR /app

# 只拷贝运行必需内容（不含 .git / docs / 测试）
COPY src/ ./src/
COPY knowledge/ ./knowledge/
COPY media/ ./media/
COPY AGENT.md README.md ./

# data 含会话（用户健康信息）与认证数据，权限收紧为 700
RUN mkdir -p /app/data && chmod 700 /app/data && chown -R nishi:nishi /app

USER nishi

ENV NHX_HOST=0.0.0.0 \
    NHX_PORT=8790 \
    NHX_DATA_DIR=/app/data \
    NHX_KNOWLEDGE_DIR=/app/knowledge \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8790

# 健康检查：容器编排据此判断存活
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/api/health', timeout=4).status==200 else 1)"

CMD ["python3", "src/nhx_app/server.py"]
