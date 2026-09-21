#!/usr/bin/env bash
# 安装 Ollama(源码/安装包见 https://github.com/ollama/ollama)并下载本地兜底模型。
# 用法: scripts/setup_local_model.sh [模型名,默认 qwen2.5:7b]
# 说明: Ollama 程序发布在 GitHub;模型权重由 Ollama 模型库(ollama.com/library)分发,不在 GitHub 上。
set -euo pipefail
MODEL="${1:-qwen2.5:7b}"

if ! command -v ollama >/dev/null 2>&1; then
  echo ">> 未检测到 ollama,开始安装…"
  if [[ "${OSTYPE:-}" == darwin* ]] && command -v brew >/dev/null 2>&1; then
    brew install ollama
  elif [[ "${OSTYPE:-}" == linux* ]]; then
    curl -fsSL https://ollama.com/install.sh | sh
  else
    echo "请手动从 https://github.com/ollama/ollama/releases 下载并安装 Ollama,然后重新运行本脚本。" >&2
    exit 1
  fi
fi

if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo ">> 启动 ollama 服务…"
  nohup ollama serve >"${TMPDIR:-/tmp}/ollama.log" 2>&1 &
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo ">> 下载模型 $MODEL(约 4-5GB,请耐心等待)…"
ollama pull "$MODEL"
echo ">> 完成。在应用「设置 → 模型服务 → Ollama」点击模型的「测试」即可验证。"
