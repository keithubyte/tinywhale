#!/usr/bin/env bash
# 免安装启动 DeepSeek Native。
#
#   ./run.sh --demo                 本地样例数据，不需要登录
#   ./run.sh                        登录后使用真实账号
#   ./run.sh --fresh                清空 .profile（包括登录状态）
#
# 所有数据都在本目录 .profile 下，删除即干净。
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
profile="$here/.profile"

usage() {
    cat <<'EOF'
用法: ./run.sh [--demo] [--fresh] [--token TOKEN] [--api-base URL] [--pow js|python]

  (无参数)   登录后使用真实账号；数据保存在 .profile
  --demo     本地样例数据，不需要账号
  --fresh    清空 .profile（包括登录状态）后启动
  --token    直接使用给定 token（主要用于 mock 测试）
  --api-base 覆盖 API 地址（默认 https://chat.deepseek.com）
  --pow      PoW 求解方式：js（默认，隐藏 WebKit）或 python（慢，兜底）
EOF
}

fresh=0
passthru=()
for arg in "$@"; do
    case "$arg" in
        --fresh) fresh=1 ;;
        --help|-h) usage; exit 0 ;;
        *) passthru+=("$arg") ;;
    esac
done

if [ "$fresh" = 1 ] && [ -n "${profile:-}" ]; then
    rm -rf -- "$profile"
fi

mkdir -p "$profile/config" "$profile/data" "$profile/cache"

# Node 运行时以压缩包随仓库提供，首次运行解压到 vendor/node/node。
node_bin="$here/vendor/node/node"
if [ ! -x "$node_bin" ] && [ -f "$here/vendor/node/node.xz" ]; then
    echo "[deepseek-native] 正在解压内置 Node 运行时…" >&2
    xz -dc "$here/vendor/node/node.xz" > "$node_bin"
    chmod +x "$node_bin"
fi
export DSD_CONFIG_DIR="$profile/config/deepseek-native"
export DSD_DATA_DIR="$profile/data/deepseek-native"
export XDG_CACHE_HOME="$profile/cache"

# 系统缺 WebKit 6.0 的 GI 数据时，使用随目录附带的 typelib
if ! python3 -c "import gi; gi.require_version('WebKit','6.0')" >/dev/null 2>&1; then
    export GI_TYPELIB_PATH="$here/vendor/typelib${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
fi

# WebKit 6.0 默认用 bwrap 隔离网页进程；受限环境里自动关闭（只用于登录和 PoW）
if command -v bwrap >/dev/null 2>&1 && bwrap --unshare-user --dev-bind / / true >/dev/null 2>&1; then
    :
else
    echo "[deepseek-native] 当前环境无法创建 WebKit 沙箱，已禁用它。" >&2
    export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
fi

exec python3 "$here/app.py" ${passthru[@]+"${passthru[@]}"}
