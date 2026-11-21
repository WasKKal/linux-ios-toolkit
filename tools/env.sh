#!/usr/bin/env bash
# ============================================================================
# env.sh —— 自适应环境探测（source 后即可用）
#
#   source ./env.sh
#   python3 linux_ipa_build.py smoke
#   python3 linux_ipa_build.py project --dir ./iOSBuild --out ./App.ipa
#
# 解决的问题：手册默认的 /opt/ios-sdk/... 与部分沙盒的
#   /sandbox/workspace/sdk/iPhoneOS16.5.sdk 不一致，LLVM 版本也可能是 18/19。
#   本脚本自动找 SDK、clang、ld64.lld，不写死任何路径。
#
# 已实测环境：
#   A) /opt/ios-sdk/sdks/iPhoneOS16.5.sdk + clang 18.1.3 + /usr/lib/llvm-18/bin/ld64.lld
#   B) /sandbox/workspace/sdk/iPhoneOS16.5.sdk + clang 19.1.7 + /usr/lib/llvm-19/bin/ld64.lld
# 两者都能通过 smoke 自检。
#
# 导出给 linux_ipa_build.py 的变量：
#   IOS_SDK / CLANG / LD64 / IOS_SDK_VERSION /
#   IPHONEOS_DEPLOYMENT_TARGET / UNITY_VERSION_PATCH / BUILD_WORKERS(可选)
# ============================================================================

# ---- 1) iOS SDK ------------------------------------------------------------
if [ -n "${IOS_SDK:-}" ] && [ -d "$IOS_SDK" ]; then
  :   # 调用方已显式指定，尊重之
else
  for base in /opt/ios-sdk/sdks /sandbox/workspace/sdk "$PWD/sdk" "$HOME/sdk" \
              /workspace/sdk /usr/local/ios-sdk /sandbox/sdk /sdk; do
    [ -d "$base" ] || continue
    hit="$(find "$base" -maxdepth 3 -type d -name 'iPhoneOS*.sdk' 2>/dev/null | sort -V | tail -1)"
    if [ -n "$hit" ]; then IOS_SDK="$hit"; break; fi
  done
fi

# ---- 2) SDK 版本号（从目录名 iPhoneOS16.5.sdk 解析） -----------------------
if [ -z "${IOS_SDK_VERSION:-}" ] && [ -n "${IOS_SDK:-}" ]; then
  v="$(basename "$IOS_SDK" | sed -E 's/^iPhoneOS([0-9]+\.[0-9]+)\.sdk$/\1/')"
  case "$v" in [0-9]*.[0-9]*) IOS_SDK_VERSION="$v" ;; *) IOS_SDK_VERSION="16.5" ;; esac
fi
export IOS_SDK_VERSION="${IOS_SDK_VERSION:-16.5}"

# ---- 3) clang --------------------------------------------------------------
export CLANG="${CLANG:-$(command -v clang 2>/dev/null || echo clang)}"

# ---- 4) ld64.lld（取已安装的最高 LLVM 版本） -------------------------------
if [ -z "${LD64:-}" ] || [ ! -e "$LD64" ]; then
  hit="$(ls -1 /usr/lib/llvm-*/bin/ld64.lld 2>/dev/null | sort -V | tail -1)"
  if [ -n "$hit" ]; then
    LD64="$hit"
  else
    LD64="$(command -v ld64.lld 2>/dev/null || true)"
  fi
fi

# ---- 5) 部署目标 / 引擎修订号 ----------------------------------------------
export IPHONEOS_DEPLOYMENT_TARGET="${IPHONEOS_DEPLOYMENT_TARGET:-12.0}"
export UNITY_VERSION_PATCH="${UNITY_VERSION_PATCH:-7}"
# 62f1 导出的工程请用：UNITY_VERSION_PATCH=62 source ./env.sh

# ---- 6) 低内存沙盒：限制并行编译线程 ---------------------------------------
# 3GB 内存沙盒并行编译 200+ 个 IL2CPP .cpp 会 OOM；降到 2 线程即可。
# 不设时编排器 auto_workers() 会自行按 MemAvailable 计算。
if [ -z "${BUILD_WORKERS:-}" ]; then
  mem_kb="$(awk '/MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  mem_gb=$((mem_kb / 1048576))
  if [ "$mem_gb" -lt 6 ]; then
    BUILD_WORKERS=2
  elif [ "$mem_gb" -lt 12 ]; then
    BUILD_WORKERS=4
  fi
fi
if [ -n "${BUILD_WORKERS:-}" ]; then export BUILD_WORKERS; else unset BUILD_WORKERS; fi

export IOS_SDK CLANG LD64

# ---- 7) 汇总与告警 ----------------------------------------------------------
echo "[env] IOS_SDK   = ${IOS_SDK:-<未找到>}"
echo "[env] SDK_VER   = $IOS_SDK_VERSION  deploy >= $IPHONEOS_DEPLOYMENT_TARGET"
echo "[env] CLANG     = $CLANG  ($("$CLANG" --version 2>/dev/null | head -1))"
echo "[env] LD64      = ${LD64:-<未找到>}"
echo "[env] PATCH     = $UNITY_VERSION_PATCH  (7f1=7 / 62f1=62)"
echo "[env] WORKERS   = ${BUILD_WORKERS:-auto(按内存自适应)}"
if [ ! -d "${IOS_SDK:-}" ]; then
  echo "[env][WARN] 没找到 iOS SDK，请手动: export IOS_SDK=/path/to/iPhoneOS16.5.sdk"
fi
if [ ! -e "${LD64:-}" ]; then
  echo "[env][WARN] 没找到 ld64.lld，请装 LLVM >=18 或手动 export LD64=/path/ld64.lld"
fi
