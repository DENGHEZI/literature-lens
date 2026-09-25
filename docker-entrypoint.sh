#!/bin/sh
# 文献透镜后端容器启动脚本
# - 初始化持久卷（/data）：空文献库、上传/输出/页图目录
# - 若设置了 LLM 环境变量，则生成 /data/config.json（api_key 用 ${ENV} 引用，不落明文）
# - 通过 LITERATURE_LENS_CONFIG 让 serve.py 加载该配置
# - 最后 exec serve.py（端口取 $PORT，由 CloudBase Run 注入）
set -e

mkdir -p /data /data/uploads /data/outputs /data/pages

# 首次运行初始化空文献库
if [ ! -f /data/lens_data.json ]; then
  echo '{"docs":[]}' > /data/lens_data.json
  echo "[entrypoint] 已初始化空文献库 /data/lens_data.json"
fi

# 可选：用环境变量注入分析用的 LLM（共享后端用同一个 Key）
if [ -n "$LENS_API_KEY" ]; then
  cat > /data/config.json <<EOF
{
  "active_provider": "${LENS_PROVIDER:-cloud}",
  "providers": [
    {
      "id": "${LENS_PROVIDER:-cloud}",
      "name": "云端共享模型",
      "base_url": "${LENS_API_BASE:-https://api.openai.com/v1}",
      "api_key": "\${LENS_API_KEY}",
      "model": "${LENS_MODEL:-gpt-4o-mini}"
    }
  ],
  "uploads": {"dir": "/data/uploads", "max_mb": 40},
  "output": {"dir": "/data/outputs"}
}
EOF
  export LITERATURE_LENS_CONFIG=/data/config.json
  echo "[entrypoint] 已生成云上 LLM 配置 -> /data/config.json（api_key 取自环境变量 LENS_API_KEY）"
else
  echo "[entrypoint] 未设置 LENS_API_KEY：后端仅提供 PDF 存储/页图，AI 解析需各端在「我的API设置」自行配置 Key"
fi

echo "[entrypoint] 启动 serve.py（PORT=${PORT:-80}）"
exec python serve.py
