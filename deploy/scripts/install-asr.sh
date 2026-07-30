#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${RUNNINGHUB_APP_DIR:-/opt/runninghub-video}"
RELEASE_DIR="${RUNNINGHUB_RELEASE_DIR:-$APP_DIR}"
APP_USER="${RUNNINGHUB_LINUX_USER:-rhvideo}"
RUNTIME_DIR="$APP_DIR/.asr-runtime"
VENV_DIR="$RUNTIME_DIR/venv"
MODEL_DIR="$APP_DIR/data/asr-models"
ASR_HOME="$APP_DIR/data/asr-home"
PIP_CACHE_DIR="$RUNTIME_DIR/pip-cache"
REQUIREMENTS="$RELEASE_DIR/asr_service/requirements.txt"
STAMP_FILE="$RUNTIME_DIR/requirements.sha256"
TORCH_VERSION="${ASR_TORCH_VERSION:-2.11.0}"
TORCH_INDEX_URL="${ASR_TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
PYPI_INDEX_URL="${ASR_PYPI_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"
PIP_TIMEOUT="${ASR_PIP_TIMEOUT_SECONDS:-300}"
PIP_RETRIES="${ASR_PIP_RETRIES:-12}"

if [[ "$APP_DIR" != "/opt/runninghub-video" ]]; then
    echo "拒绝安装到非生产项目目录：$APP_DIR" >&2
    exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
    echo "ASR 安装脚本必须由 root 运行，以便创建隔离目录和设置权限。" >&2
    exit 1
fi
if [[ ! -f "$REQUIREMENTS" ]]; then
    echo "缺少 ASR 依赖清单：$REQUIREMENTS" >&2
    exit 1
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    echo "缺少主项目 Python：$APP_DIR/.venv/bin/python" >&2
    exit 1
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "缺少项目用户：$APP_USER" >&2
    exit 1
fi

install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$RUNTIME_DIR"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$MODEL_DIR"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$ASR_HOME"
install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$PIP_CACHE_DIR"

desired_hash="$(
    {
        sha256sum "$REQUIREMENTS" | awk '{print $1}'
        printf 'torch==%s\ntorchaudio==%s\nindex=%s\n' \
            "$TORCH_VERSION" "$TORCH_VERSION" "$TORCH_INDEX_URL"
        printf 'pypi=%s\n' "$PYPI_INDEX_URL"
    } | sha256sum | awk '{print $1}'
)"
installed_hash=""
if [[ -f "$STAMP_FILE" ]]; then
    installed_hash="$(tr -d '\r\n' < "$STAMP_FILE")"
fi

runtime_ready=0
if [[ "$installed_hash" == "$desired_hash" && -x "$VENV_DIR/bin/python" ]]; then
    if sudo -u "$APP_USER" "$VENV_DIR/bin/python" -c \
        "import importlib.util,sys; names=('fastapi','funasr','modelscope','psutil','torch','torchaudio','uvicorn'); sys.exit(0 if all(importlib.util.find_spec(name) for name in names) else 1)" \
        >/dev/null 2>&1; then
        runtime_ready=1
    fi
fi

if [[ "$runtime_ready" -eq 1 ]]; then
    echo "ASR 隔离环境已经就绪，依赖未变化，跳过安装。"
else
    build_dir="$RUNTIME_DIR/venv-build-$$"
    old_dir="$RUNTIME_DIR/venv-previous"
    case "$build_dir" in
        "$RUNTIME_DIR"/venv-build-*) ;;
        *) echo "ASR 临时环境路径异常" >&2; exit 1 ;;
    esac
    rm -rf -- "$build_dir"
    cleanup_build() {
        if [[ -n "${build_dir:-}" && -d "$build_dir" ]]; then
            rm -rf -- "$build_dir"
        fi
    }
    trap cleanup_build EXIT
    sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m venv "$build_dir"
    sudo -u "$APP_USER" env \
      HOME="$ASR_HOME" \
      PIP_CACHE_DIR="$PIP_CACHE_DIR" \
      "$build_dir/bin/python" -m pip install \
        --disable-pip-version-check \
        --timeout "$PIP_TIMEOUT" \
        --retries "$PIP_RETRIES" \
        --index-url "$PYPI_INDEX_URL" \
        "filelock" \
        "typing-extensions>=4.10.0" \
        "sympy>=1.13.3" \
        "networkx>=2.5.1" \
        "jinja2" \
        "fsspec>=0.8.5"
    sudo -u "$APP_USER" env \
      HOME="$ASR_HOME" \
      PIP_CACHE_DIR="$PIP_CACHE_DIR" \
      "$build_dir/bin/python" -m pip install \
        --disable-pip-version-check \
        --timeout "$PIP_TIMEOUT" \
        --retries "$PIP_RETRIES" \
        --no-deps \
        --index-url "$TORCH_INDEX_URL" \
        "torch==$TORCH_VERSION+cpu" "torchaudio==$TORCH_VERSION+cpu"
    sudo -u "$APP_USER" env \
      HOME="$ASR_HOME" \
      PIP_CACHE_DIR="$PIP_CACHE_DIR" \
      "$build_dir/bin/python" -m pip install \
        --disable-pip-version-check \
        --timeout "$PIP_TIMEOUT" \
        --retries "$PIP_RETRIES" \
        --index-url "$PYPI_INDEX_URL" \
        -r "$REQUIREMENTS"
    sudo -u "$APP_USER" "$build_dir/bin/python" -c \
        "import fastapi, funasr, modelscope, psutil, torch, torchaudio, uvicorn; assert not torch.cuda.is_available()"
    sudo -u "$APP_USER" "$build_dir/bin/python" -m pip check

    rm -rf -- "$old_dir"
    if [[ -d "$VENV_DIR" ]]; then
        mv -- "$VENV_DIR" "$old_dir"
    fi
    if ! mv -- "$build_dir" "$VENV_DIR"; then
        if [[ -d "$old_dir" ]]; then
            mv -- "$old_dir" "$VENV_DIR"
        fi
        exit 1
    fi
    build_dir=""
    trap - EXIT
    printf '%s\n' "$desired_hash" > "$STAMP_FILE"
    chown "$APP_USER:$APP_USER" "$STAMP_FILE"
    chmod 640 "$STAMP_FILE"
    rm -rf -- "$old_dir"
    echo "ASR 隔离环境安装完成。"
fi

append_env_if_missing() {
    local name="$1"
    local value="$2"
    if ! grep -q "^${name}=" "$APP_DIR/.env"; then
        printf '%s=%s\n' "$name" "$value" >> "$APP_DIR/.env"
    fi
}

if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "缺少生产配置：$APP_DIR/.env" >&2
    exit 1
fi
append_env_if_missing "LONG_AUDIO_ALIGNMENT_PROVIDER" "funasr_http"
append_env_if_missing "ASR_BASE_URL" "http://127.0.0.1:18084"
append_env_if_missing "ASR_REQUEST_TIMEOUT_SECONDS" "1800"
if ! grep -q '^ASR_SHARED_TOKEN=.\+' "$APP_DIR/.env"; then
    if grep -q '^ASR_SHARED_TOKEN=' "$APP_DIR/.env"; then
        echo "ASR_SHARED_TOKEN 已存在但为空，请人工设置随机密钥。" >&2
        exit 1
    fi
    printf 'ASR_SHARED_TOKEN=%s\n' "$(openssl rand -hex 32)" >> "$APP_DIR/.env"
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "ASR 环境检查通过：$VENV_DIR"
echo "ASR 模型缓存目录：$MODEL_DIR"
