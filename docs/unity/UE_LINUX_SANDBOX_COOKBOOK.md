# Unreal Engine 在 Linux 沙箱中的开发与打包实战手册

> 实测环境（2026-11-20）：Ubuntu 22.04 容器 / 宿主 123GB RAM、32 核 AMD EPYC 9754、
> 203GB 可用磁盘、无 GPU、Xvfb 可用、Docker 27.5（腾讯云镜像源）。
> 引擎：UE **5.8.4**（社区容器镜像 `wukakuki/unreal-engine:dev-slim-5.8.4`，70.2GB）。

---

## 0. 一句话结论

| 事项 | 本沙箱能否做 | 说明 |
|---|---|---|
| UE 引擎编译/运行 | ✅ | 资源充足；容器镜像已内置引擎 + clang 20.1.8 + dotnet 10 |
| 打包 **Linux** 可执行程序 | ✅ | `RunUAT BuildCookRun -platform=Linux` 全链路走通（见 §4） |
| 无 GUI 启动 Editor | ⚠️ 受限 | 需 Xvfb + 软件渲染（`Mesa llvmpipe`），慢，仅适合程序化操作 |
| 打包 **Android** | ✅ 理论可行 | 需装 Android NDK/SDK，本次未实测 |
| 打包 **iOS / IPA** | ❌ 不可 | 官方与社区一致：签名构建必须有 macOS + Xcode；见 §6 |

---

## 1. 为什么上一版判断被推翻

最初基于「3GB 内存小沙盒」的印象判断 UE 跑不动。实测本沙箱是 **123GB 内存 / 32 核**，
资源远超 UE Editor 官方推荐（32GB + 独显）。**唯一真正的门槛不是硬件，而是引擎获取**：
UE 源码仓库 `EpicGames/UnrealEngine` 是私有的，官方容器在 `ghcr.io`，两者都要 Epic 账号
关联 GitHub + PAT。所以走了**不依赖账号的社区镜像**路线。

---

## 2. 获取引擎：三条路线

| 路线 | 前置 | 本沙箱可用性 |
|---|---|---|
| 官方 ghcr `ghcr.io/epicgames/unreal-engine:dev-5.8.x` | Epic 账号 + 关联 GitHub + PAT（read:packages） | 需你的 token；当前无登录态 |
| 官方源码编译 `git clone EpicGames/UnrealEngine` | 同上，编译 1-3 小时 | 需你的 token |
| **社区镜像 `wukakuki/unreal-engine`** | 无；Docker Hub 公开 | ✅ **已用此路线**，走腾讯云镜像源拉取成功 |

社区镜像标签覆盖 4.27 ~ 5.8.4，含 `dev` / `dev-slim` / `runtime` 等变体。
本机 `daemon.json` 配了 `registry-mirrors: ["https://mirror.ccs.tencentyun.com"]`，
直连 `auth.docker.io` 超时但镜像源可正常拉取。

```bash
docker pull wukakuki/unreal-engine:dev-slim-5.8.4   # 70.2GB
```

---

## 3. 容器内关键路径（实测）

| 项目 | 路径 |
|---|---|
| 引擎根 | `/home/ue4/UnrealEngine`（环境变量 `UNREAL_ENGINE_ROOT`） |
| 版本文件 | `Engine/Build/Build.version`（5.8.4, CL 58902052） |
| 构建脚本 | `Engine/Build/BatchFiles/RunUAT.sh`、`RunUBT.sh`、`RunDotnet.sh` |
| dotnet | `Engine/Binaries/ThirdParty/DotNet/10.0/linux-x64/dotnet`（脚本自动 setup） |
| C++ 编译器 | **引擎内置** `Engine/Extras/ThirdPartyNotUE/SDKs/HostLinux/Linux_x64/v26_clang-20.1.8-rockylinux8` |
| 容器用户 | `ue4`，uid/gid = **1000** |

> 宿主挂载的工程目录必须先 `chown -R 1000:1000`，否则 UBT 报 `Permission denied: /project/Saved`。

---

## 4. 从零建一个可打包的 C++ 工程（踩坑全记录）

工程名 `SandUEProbe`，目录 `/workspace/UE_PROBE`。**一个都不能少的 6 类文件**：

```
SandUEProbe.uproject
Source/SandUEProbe.Target.cs          # Game 目标
Source/SandUEProbeEditor.Target.cs    # Editor 目标
Source/SandUEProbe/SandUEProbe.Build.cs     # ★ 模块定义，缺它报 "Could not find definition for module"
Source/SandUEProbe/SandUEProbe.cpp          # IMPLEMENT_PRIMARY_GAME_MODULE
Source/SandUEProbe/SandUEProbe.h
Config/DefaultEngine.ini              # GameDefaultMap + GlobalDefaultGameMode
```

三个必须做对的配置点（都是实测踩出来的）：

1. **Target.cs 加 `bOverrideBuildEnvironment = true;`**
   否则 UE 5.8 报 `SandUEProbe modifies the values of properties: [UndefinedIdentifierWarningLevel...]`，
   原因是 `DefaultBuildSettings = V5` 与引擎默认（Error 级警告）不一致。
2. **`SandUEProbe.Build.cs` 必须存在**且类名与 `.uproject` 的 Modules[].Name 一致。
   缺失时 UBT 报 RulesError：`Could not find definition for module 'SandUEProbe'`。
3. **`DefaultEngine.ini` 指定 GameMode**：
   `GlobalDefaultGameMode=/Script/SandUEProbe.SandUEProbeGameMode`，
   `GameDefaultMap=/Engine/Maps/Entry.Entry`。

打包命令（容器内执行）：

```bash
cd /home/ue4/UnrealEngine
bash Engine/Build/BatchFiles/RunUAT.sh BuildCookRun \
  -project=/project/SandUEProbe.uproject \
  -noP4 -platform=Linux -clientconfig=Shipping \
  -cook -build -stage -prereqs -pak -package -archive \
  -archivedirectory=/project/Saved/Archived \
  -utf8output -unattended
```

宿主侧一键调用见 `/workspace/UE_PROBE/build_linux.sh`。

---

## 5. 与 Unity Linux→iOS 链路的本质差异

| | Unity（已跑通） | Unreal |
|---|---|---|
| Linux Editor 导出中间产物 | ✅ 完整 Xcode 工程（IL2CPP C++ + .a + Data） | ❌ 无等价切口 |
| 自制 xcodebuild 替代 | ✅ clang + ld64.lld 可补链出 IPA | ❌ UAT 构建图绑死 Xcode/macOS |
| Metal 着色器编译 | 不需要（Unity 自己处理） | 必须，编译器只在 Xcode |
| 签名 | ad-hoc（`ld64.lld -adhoc_codesign`） | 需 Apple 证书 + provisioning profile + Keychain |

所以 Unity 那条「Linux 直出 IPA」的经验**无法迁移到 UE**。

---

## 6. iOS 出包边界（确凿结论）

- **官方文档**：UE iOS/tvOS 页面明确写「需要一台 macOS 机器为 Apple 生态的 C++ 项目做签名构建，
  调试也要 Xcode」。UE 容器官方 Quickstart 只演示 `build and package for Linux devices`。
- **专业 CI 也绕不开**：SpeedrunCI 等UE移动 CI 的做法是把 iOS 签名**调度到已注册的 Mac runner**，
  证书载入临时 Keychain；Linux 容器只负责 Linux/Android。
- **CSDN/社区实践**：iOS 证书需在 Mac 上导入 Keychain，`-certificate` + `-provisioningprofile`
  参数才能非交互；没有 Mac 就走 GitHub Actions `macos-*` runner / MacStadium / AWS Mac 实例。

**结论**：在本 Linux 沙箱里 UE 只能出 Linux/Android 包。要 iOS IPA，必须外接 Mac（云或物理），
走 `Linux 开发 → Mac 上 RunUAT -platform=iOS` 的链路。

---

## 7. 交付物索引

| 文件 | 内容 |
|---|---|
| `/workspace/UE_PROBE/` | 最小 C++ 工程（含 Build.cs 等全套配置） |
| `/workspace/UE_PROBE/build_linux.sh` | 容器内打包 Linux 的脚本 |
| `/workspace/UE_PROBE/uat_build*.log` | 三次打包日志（失败→失败→编译中，记录全部踩坑） |
| 本文件 | 可移植知识手册 |

---

## 8. 无 GPU 下启动 Editor（待验证方向）

沙箱无 GPU，但 `DISPLAY=:0` 且有 Xvfb。理论路径：
`xvfb-run -a UnrealEditor -nullrhi`（用 Null RHI 跳过图形），
或装 `mesa-utils` 用 llvmpipe 软件渲染。适合程序化建场景/批处理，不适合实时预览。
iOS 设备的画面手感无论如何都要真机确认。
