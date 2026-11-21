# linux-ios-toolkit

> **纯 Linux、免 macOS** 的 iOS 工具链合集：用 iOS SDK 编译打包 dylib / IPA，用 Unity Linux Editor 制作并打包游戏工程。
> 全部在真实沙箱（Ubuntu 22.04 / clang 18 / ld64.lld / iPhoneOS16.5.sdk）实测跑通。

## 三大能力

| 能力 | 工具 | 产物 | 状态 |
|---|---|---|---|
| **① 编译打包 Dylib** | `tools/build_dylib.py` | arm64 Mach-O dylib（ad-hoc 签名）+ Theos deb | ✅ smoke 通过 |
| **② 编译打包 IPA** | `tools/linux_ipa_build.py` | arm64 ad-hoc 签名 IPA | ✅ smoke + 真实工程通过 |
| **③ Unity 制作/打包游戏** | Unity Linux Editor + `BuildIos.cs` + 编排器 | Xcode 工程 → IPA | ✅ 端到端实测（见报告） |

核心思路：Unity 官方说 IL2CPP 不支持交叉编译，那只是「Linux 直出 iOS Player」；
Linux Editor 仍能用 `BuildPipeline.BuildPlayer(iOS)` 导出**自包含 Xcode 工程**（IL2CPP C++ + `.a` + `Data/`）。
本仓库用 `clang` + `ld64.lld -adhoc_codesign` 补上 Xcode 那一段，从而**全程不需要 Mac**。

```
Unity 工程 ──Linux Editor──> Xcode 工程 ──linux_ipa_build.py──> IPA
   (②③)                    (IL2CPP C++/lib*.a/Data)   (pbxproj解析→clang→ld64→签名→门禁)

源码 .c/.m/.mm ──build_dylib.py──> arm64 dylib ──> Theos deb
   (①)              (clang→ld64→adhoc签名)
```

## 环境前置

```bash
# 需要具备（新对话若已有 iOSSDK，只需 2、3、4）
# 1. iOS SDK：iPhoneOS*.sdk（如 /opt/ios-sdk/sdks/iPhoneOS16.5.sdk）
# 2. clang ≥ 18       3. ld64.lld（/usr/lib/llvm-*/bin/ld64.lld）     4. zip / python3 / dpkg-deb|ar

# 一条命令自动探测 SDK / clang / ld64：
source tools/env.sh
```

`env.sh` 会尝试多个常见 SDK 路径并取最高 LLVM 版 ld64，适配不同沙盒布局。

## 快速开始

### ① Dylib（最简单，先跑这个验证环境）

```bash
python3 tools/build_dylib.py smoke
python3 tools/build_dylib.py build --src examples/hello_dylib/hello.c \
    --out libhello.dylib --name hello --deb --bundle com.example.hello
python3 tools/build_dylib.py verify --file libhello.dylib
```

详见 [`docs/dylib/DYLIB_GUIDE.md`](docs/dylib/DYLIB_GUIDE.md)。

### ② IPA（对已有 Unity 导出的 Xcode 工程）

```bash
source tools/env.sh
python3 tools/linux_ipa_build.py smoke                              # 工具链自检
python3 tools/linux_ipa_build.py parse --pbxproj <工程>/project.pbxproj
python3 tools/linux_ipa_build.py project --dir <导出的iOSBuild目录> \
    --out App.ipa --workers 2 --use-project-pch
```

详见 [`docs/ipa/UNITY_LINUX_IPA_COOKBOOK.md`](docs/ipa/UNITY_LINUX_IPA_COOKBOOK.md)（11 节完整手册）。

### ③ Unity 制作并打包游戏

```bash
# 1) 拷贝 unity_template 到你的工程，按模板放入 BuildIos.cs
# 2) 首次授权（Personal 在线激活）
bash tools/activate_unity_license.sh
# 3) batchmode 导出 Xcode 工程
IOS_EXPORT_DIR=/out/iOSBuild IOS_BUILD_SCENES="Assets/Scenes/Main.unity" \
  Unity -batchmode -quit -nographics -projectPath /proj -executeMethod BuildIos.Export
# 4) 用 ② 的编排器把 Xcode 工程转成 IPA
```

详见 [`unity_template/README.md`](unity_template/README.md) 与 [`docs/unity/`](docs/unity/)。

## 目录结构

```
.
├── tools/
│   ├── build_dylib.py            # ① dylib 编译/签名/deb（原创，smoke 通过）
│   ├── linux_ipa_build.py        # ② 通用 IPA 编排器（smoke/parse/project）
│   ├── env.sh                    # 自适应环境探测（SDK/clang/ld64）
│   ├── activate_unity_license.sh # Unity Personal 在线授权
│   └── linux_xcodebuild.py       # 实战版工具链（含 S7 热修复实现，参考）
├── docs/
│   ├── dylib/DYLIB_GUIDE.md              # ① 完整指南
│   ├── ipa/UNITY_LINUX_IPA_COOKBOOK.md   # ② 主手册（自包含，1290+ 行）
│   ├── ipa/{E2E_REPORT,BUILD_RESEARCH,HOTFIX_CODE0_REPORT}.md
│   └── unity/{ACCOUNT_AND_LICENSE_GUIDE,UE_LINUX_SANDBOX_COOKBOOK}.md
├── unity_template/               # ③ Unity 最小工程模板 + BuildIos.cs
├── examples/hello_dylib/         # ① 最小示例源码
└── .github/workflows/            # CI（占位，见下）
```

## 三道不可绕过的门禁（最重要）

出 IPA 时工具链强制校验，缺一会导致「链接成功但侧载秒退 code 0」：

1. **授权必须真正生效**：`Successfully activated ULF license`。Personal 只有 NUL 在线激活一条路，网页手动激活已关闭，`-username/-password` 无效。
2. **入口源码编译失败立即终止**：`main.mm` / `static_main.mm` 不能用 `--keep-going` 跳过。
3. **LC_MAIN 入口门禁**：校验入口不是 `mov x0,#0; ret` 空桩，且函数体内至少 1 条 `bl`。

完整因果链见 [`docs/ipa/HOTFIX_CODE0_REPORT.md`](docs/ipa/HOTFIX_CODE0_REPORT.md)。

## 已验证边界

- ✅ 纯 Linux 产出 **ad-hoc 签名** arm64 IPA / dylib，无需 Mac
- ✅ Unity 2022.3.7f1 / 2022.3.62f1 导出工程均可出包（62f1 需 framework 头映射）
- ✅ clang 18 / 19 兼容；小内存沙盒用 `--workers 2` + `-O0`
- ❌ ad-hoc 包**不能上架 App Store / TestFlight**；侧载需 SideStore/AltStore 重签
- ❌ 沙箱无法真机调试画面/手感
- ❌ **iOS 目标仍需一台 Mac 的环节**不适用本仓库：UE 出 iOS 必须 Mac（见 UE cookbook）；本仓库解决的是 Unity 导出工程 + dylib 这条 Linux 可闭环的链路

## 许可

MIT。工具用于合法 iOS 开发、逆向学习与自有设备测试，使用者须遵守当地法律与 Apple 许可协议。
