# 文献透镜后端 · 容器镜像
# 适用：微信云开发「云托管(CloudBase Run)」/ 任意容器平台（腾讯云 TCR、阿里 ACR、Docker Hub…）
# 说明：serve.py 是标准库 HTTP 服务，原样跑在容器里即可，无需改业务代码。
FROM python:3.11-slim

WORKDIR /app

# 装依赖（PyMuPDF 等均有 manylinux wheel，无需编译工具链）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 业务代码（serve.py + config_loader/llm_client/analyzer/... + 默认 config.json）
COPY scripts/ /app/

# 启动脚本：初始化持久卷、按需注入 LLM 配置、再 exec serve.py
# 注意：docker-entrypoint.sh 在仓库根目录（不在 scripts/ 内），需单独 COPY
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# 运行时环境变量（CloudBase Run 会注入 PORT；这里给个默认 80）
ENV LENS_DATA=/data/lens_data.json \
    LENS_HTML=/data/literature_lens.html \
    HOST=0.0.0.0 \
    PORT=80

EXPOSE 80

ENTRYPOINT ["/app/docker-entrypoint.sh"]
