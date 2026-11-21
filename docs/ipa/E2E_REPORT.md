# Unity → Linux → iOS IPA 端到端出包报告

> 执行时间：2026-11-09 14:15–14:45 (GMT+8)
> 结论：**在纯 Linux 沙箱（无 macOS / 无 Xcode）里，用 Unity 2022.3.7f1 Personal 授权导出的真实 iOS 工程，完整跑通「导出 → 编译 → 链接 → ad-hoc 签名 → IPA」全链路，产出结构合法、带真实 `_main` 与 il2cpp 游戏代码的可自签 IPA。**

---

## 一、结果一览

| 阶段 | 命令 / 动作 | 结果 |
|---|---|---|
| 1. 授权 | `Unity.Licensing.Client --activate-ulf` | 已在前一轮完成，`Unity_lic.ulf` 2610 B |
| 2. 导出工程 | `Unity -batchmode -buildTarget iOS -executeMethod BuildIos.Export` | `Build Finished, Result: Success.` / `BuildResult=Succeeded totalErrors=0 totalWarnings=1` / `totalSize=664592032` |
| 3. 源码编译 | `linux_xcodebuild.py project` + clang 18 | **116/116 全部成功**（41 Trampoline + 74 il2cpp 生成 + 1 静态 main），16 线程并行 |
| 4. 链接 | `ld64.lld -arch arm64 -platform_version ios 12.0 16.5 -adhoc_codesign` | 116 `.o` + 3 `.a`（347MB/68MB/1.7MB）+ 18 frameworks → **62,768,304 B arm64 `MH_EXECUTE`** |
| 5. 打包 | 组装 `.app` → `Payload/` → zip | **`/workspace/RaftUnityLinux.ipa`，20,998,840 B** |
| 6. 校验 | Mach-O / plist / zip / 签名 | 全通过（见下） |

### 最终产物独立校验

```
IPA 大小                 20,998,840 B
zip 完整性               OK
.app 目录                Payload/RaftLinuxProbe.app
CFBundleExecutable       RaftLinuxProbe
CFBundleIdentifier       com.raft.linuxprobe
MinimumOSVersion         12.0
Mach-O magic             0xfeedfacf   cpu 0x100000c (arm64)   filetype 2 (MH_EXECUTE)
LC_CODE_SIGNATURE        True（ld64.lld -adhoc_codesign 生成）
可执行文件               62,768,304 B, sha256 b81616c9db6b4ac6a4798427…
Data/globalgamemanagers  True
Data/Managed/Metadata/global-metadata.dat  True
真实 _main               T _main
真实 g_CodegenRegistration  D _g_CodegenRegistration
```

---

## 二、关键发现：Linux 上 Unity 能直接出 iOS 工程

此前 GameCI 文档与社区资料普遍认为 iOS 构建必须 macOS runner。本轮实测推翻了这个前提：

```
[PROBE] activeBuildTarget: iOS
[PROBE] IsBuildTargetSupported(iOS): True
[PROBE] iOS scripting backend: IL2CPP
BuildPlayer: start building target 9
Starting: iOSPlayerBuildProgram.exe ...
[ 48/2554] WriteText /out/iOSBuild/Unity-iPhone.xcodeproj/project.pbxproj
Build Finished, Result: Success.
UNITY_EXIT=0
```

导出工程共 **640 MB**：

| 目录 | 体积 | 内容 |
|---|---|---|
| `Libraries/` | 398 MB | `libiPhone-lib.a` 347 MB、`libil2cpp.a` 68 MB、`baselib.a` 1.7 MB |
| `Il2CppOutputProject/` | 238 MB | IL2CPP 运行时源码 + `Source/il2cppOutput/` **74 个游戏逻辑 C++** |
| `Data/` | 4.1 MB | `level0`、`globalgamemanagers`、`global-metadata.dat`、`boot.config` |
| `Classes/` | 688 KB | `main.mm`、`UnityAppController.mm`、私有头 `Unity/UnityInterface.h`、`Prefix.pch` |
| 其他 | — | `Info.plist`、`Unity-iPhone.xcodeproj/project.pbxproj`、`MainApp/main.mm`、`UnityFramework/` |

日志里 `Native extension for iOS target not found`、`Access token is unavailable`、`Curl error 42` 均为 Linux 侧非致命提示，进程正常退出。

---

## 三、编排器为吃下真实工程所做的 7 处改动

`linux_xcodebuild.py` 原本只针对镜像里 84 MB 的 `Trampoline` 模板，本轮全部改造完成：

| # | 改动 | 原因 |
|---|---|---|
| 1 | 源码收集增加 `Il2CppOutputProject/Source/**/*.cpp\|.c` | 74 个 il2cpp 生成的游戏逻辑才是游戏本体；`IL2CPP/` 下的运行时源码已预编译进 `libil2cpp.a`，再编会重复定义 |
| 2 | include 路径对齐 `project.pbxproj` 的 `HEADER_SEARCH_PATHS` | 尤其 `external/baselib/Platforms/IOS/Include`，缺它 `BaselibPlatformSpecificEnvironment.h` 找不到 |
| 3 | 按语言分流编译 | ObjC/ObjC++ 注入工程 `Prefix.pch`；C/C++ 不注入（生成代码自带 `pch-cpp.hpp`），且只对 ObjC 加 `-fblocks/-fobjc-arc` |
| 4 | 生成代码 `-O0` + 16 线程并行 | 50 MB 生成代码串行编译过慢 |
| 5 | 补齐三组宏 | `UNITY_VERSION_VER/MAJ/MIN=2022/3/7`（缺一个 `UnityTrampolineConfigure.h` 直接 `#error`）；`BASELIB_INLINE_NAMESPACE=il2cpp_baselib`（消除 `baselib::ReentrantLock` 二义性）；`RUNTIME_IL2CPP=1`（缺它 `g_CodegenRegistration` 被 `#if` 屏蔽，libil2cpp 的 `Runtime::Init` 引用落空） |
| 6 | 替换 `MainApp/main.mm` | 官方版本动态加载 `Frameworks/UnityFramework.framework`；本编排器把全部代码静态链进单个可执行文件，改为直接 `[UnityFramework getInstance]` + `runUIApplicationMainWithArgc:`，从而得到**真实 `_main`** 而非桩 |
| 7 | 展开 pbxproj 的 `$(VAR)` + 自动补桩 | `PRODUCT_NAME = "$(PRODUCT_NAME_APP)"` 不展开会生成名为 `$(...)` 的 `.app`；链接器报 undefined 时自动解析符号名并生成 arm64 汇编桩，最多 3 轮 |

### 宏修复前后的对比

| 修复前 | 修复后 |
|---|---|
| 39 个源码失败（含全部 ObjC 与 `main.mm`） | 116/116 成功 |
| 链接 undefined 大量 + `_main` 缺失 | 仅剩 4 个平台符号 |
| `.app` 名为 `$(PRODUCT_NAME_APP).app` | `RaftLinuxProbe.app` |

---

## 四、诚实说明：4 个平台桩

链接最终剩 4 个 undefined，已由自动补桩解决，**均为 Linux 导出引擎库引用、但未提供实现的 macOS/Xcode 侧平台函数**，不是游戏逻辑：

| 符号 | 桩返回值 | 说明 |
|---|---|---|
| `__Z21GetReadableStackTracePciPKPvi` | 0 | 诊断栈回溯格式化 |
| `__Z26GetStacktracetNativeOptionPPviib` | 0 | 栈回溯开关 |
| `__ZN3Geo21GetiOSAppDocumentsDirEv` | 0 | 沙盒 Documents 路径 |
| `___isPlatformVersionAtLeast` | 1 | `@available` 运行时判定，返回真值避免 Unity 误判低系统版本 |

用 `llvm-nm --defined-only` 核实过：这 4 个符号在 `libiPhone-lib.a` / `libil2cpp.a` / `baselib.a` 中**确实都不存在**，且导出工程源码里也没有实现（Unity 官方 Xcode 流程由 `UnityFramework` target 的平台层提供）。空场景不会调用到它们；若后续工程用到对应诊断/路径功能，需要补真实实现。`___isPlatformVersionAtLeast` 桩返回 1 是刻意设计。

> 相比上一轮的 `UnityEngineProbe.ipa`（10 个 undefined 全靠空桩、无游戏逻辑、无 `_main`），本轮 IPA 的 `_main`、`g_CodegenRegistration`、74 个 il2cpp 生成代码单元、真实 `Data/` 场景数据全部是**真实编译产物**。

---

## 五、仍未覆盖的边界

1. **场景是空的**：Unity 工程只放了一个 `ProbeRoot` 空物体，用于验证链路，不是 Raft 游戏。
2. **未在真机/模拟器运行验证**：沙箱没有 iOS 设备。校验只到 Mach-O / 签名 / 包结构层面。
3. **完整 `raft_vita` 工程仍未取得**：此前整库克隆超时，仅拿到 `Hook.cs` 样本；没有场景/资源/脚本齐全的工程可替换。
4. **ad-hoc 签名**：需经 SideStore / AltStore 用个人证书重签后安装。
5. **Unity Personal 是限时许可**（`com.unity.editor.time-limited-license`），长期构建前用 `--update-ulf` 重新激活。
6. **法律提醒**：`raft_vita` 是商业游戏反编译产物，仅可作个人学习/算法对照，不可去品牌化后发布或商用。

---

## 六、复现路径

```bash
# 1) 授权（一次性）
export UNITY_EMAIL=... UNITY_PASSWORD=...
bash /workspace/linux_xcodebuild/activate_unity_license.sh

# 2) Unity 在 Linux 上导出真实 iOS Xcode 工程
#    Editor 脚本调用 BuildPipeline.BuildPlayer(BuildTarget.iOS)，见最小工程 BuildIos.cs
#    产物含 Classes/ + Libraries/*.a + Il2CppOutputProject/ + Data/ + project.pbxproj

# 3) 自制 xcodebuild 编译链接签名打包
python3 /workspace/linux_xcodebuild/linux_xcodebuild.py project \
  --dir <导出的Xcode工程> --out /workspace/RaftUnityLinux.ipa \
  --work <临时工作目录> --use-project-pch --keep-going
```

---

## 七、文件清单

| 文件 | 说明 |
|---|---|
| `/workspace/RaftUnityLinux.ipa` | **本轮最终产物**（空场景 Unity IPA，arm64 + ad-hoc 签名） |
| `/workspace/UNITY_LINUX_IOS_E2E_REPORT.md` | 本报告 |
| `/workspace/linux_xcodebuild/linux_xcodebuild.py` | 已改造的编排器（真实工程可用） |
| `/workspace/linux_xcodebuild/README.md` | 使用说明与验证表（已更新） |
| `/workspace/unity_license/ios_export.log` | Unity `BuildPlayer(iOS)` 完整日志 |
| `/workspace/unity_license/linux_build.log` | 编译/链接/打包完整日志 |
| `/workspace/unity_license/Unity_lic.ulf` | 授权文件（敏感，勿外发） |
| `/workspace/unity_license/ACTIVATION_REPORT.md` | 授权激活与 batchmode 验证报告 |
