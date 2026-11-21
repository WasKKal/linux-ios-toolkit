#!/usr/bin/env bash
# 在 Linux 沙箱里用 Unity ID 在线激活 Personal 许可，导出 Unity_lic.ulf
#
# 用法：
#   export UNITY_EMAIL="你的Unity ID邮箱"
#   export UNITY_PASSWORD="密码"
#   bash activate_unity_license.sh [输出目录，默认 /workspace/unity_license]
#
# 原理：GameCI 镜像自带 Unity.Licensing.Client 1.12.1，
#       --activate-ulf（不带 --serial）走 Named User Licensing 在线激活 Personal，
#       不需要 Unity Hub / GUI，也不走已对 Personal 关闭的手动激活网页。
# 注意：.ulf 与 hostname + machine-id 绑定，换机器需带 UNITY_EMAIL/PASSWORD 重新激活。

set -euo pipefail

: "${UNITY_EMAIL:?请先 export UNITY_EMAIL（Unity ID 邮箱）}"
: "${UNITY_PASSWORD:?请先 export UNITY_PASSWORD（Unity ID 密码）}"

OUT="${1:-/workspace/unity_license}"
IMAGE="${UNITY_IMAGE:-unityci/editor:ubuntu-2022.3.7f1-ios-1}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

echo "[*] 镜像: $IMAGE"
echo "[*] 输出: $OUT/Unity_lic.ulf"

docker run --rm \
  -v "$OUT:/out" \
  -e UNITY_EMAIL -e UNITY_PASSWORD \
  -e DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
  --entrypoint bash \
  "$IMAGE" -c '
    set -euo pipefail
    C=/opt/unity/Editor/Data/Resources/Licensing/Client/Unity.Licensing.Client
    echo "[*] $($C --version)"
    EXTRA=""
    if [ "${UNITY_LIC_DEBUG:-0}" = "1" ]; then EXTRA="--debug"; fi
    timeout 300 "$C" --activate-ulf $EXTRA \
      --username "$UNITY_EMAIL" --password "$UNITY_PASSWORD" || {
        echo "[!] 在线激活失败：常见原因是新设备邮箱验证码 / 2FA / 密码错误" >&2
        exit 1
      }
    ulf=$(find / -name "Unity_lic.ulf" -type f 2>/dev/null | head -1 || true)
    if [ -z "$ulf" ]; then
      echo "[!] 客户端已返回，但没找到 Unity_lic.ulf" >&2
      exit 1
    fi
    cp "$ulf" /out/Unity_lic.ulf
    echo "[+] 已写出 /out/Unity_lic.ulf ($(wc -c < "$ulf") bytes)"
  '

if [ ! -s "$OUT/Unity_lic.ulf" ]; then
  echo "[!] 输出文件为空，激活未成功" >&2
  exit 1
fi

echo
echo "[✓] 授权文件: $OUT/Unity_lic.ulf"
echo "    注入构建环境："
echo "      export UNITY_LICENSE=\"\$(cat $OUT/Unity_lic.ulf)\""
echo "      export UNITY_EMAIL=\"\$UNITY_EMAIL\""
echo "      export UNITY_PASSWORD=\"\$UNITY_PASSWORD\""
echo
echo "[i] 安全：.ulf 绑定账号，不要提交公开仓库；建议使用专用构建小号。"
