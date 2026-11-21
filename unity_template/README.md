# Unity 最小工程模板

这是一个「能被 Linux Unity Editor 导出成 iOS Xcode 工程」的最小骨架。

## 目录

```
unity_template/
├── Assets/Editor/BuildIos.cs   # batchmode 导出入口
├── Packages/manifest.json      # 仅内置模块，无付费依赖
└── ProjectSettings/            # 由 Unity 首次打开时生成（未随仓库提交）
```

##  workflow（完整链路）

1. **制作工程**：用 Unity Linux Editor（Docker 镜像 `unityci/editor:ubuntu-<版本>-ios`）打开你的工程，
   编写场景/脚本，把 `Assets/Editor/BuildIos.cs` 放进工程。

2. **授权**（仅首次，Personal 走在线激活）：
   ```bash
   source tools/env.sh
   bash tools/activate_unity_license.sh   # --activate-ulf，详见 docs/unity/ACCOUNT_AND_LICENSE_GUIDE.md
   ```

3. **导出 Xcode 工程**：
   ```bash
   IOS_EXPORT_DIR=/out/iOSBuild \
   IOS_BUILD_SCENES="Assets/Scenes/Main.unity" \
   IOS_BUNDLE_ID=com.example.game \
   Unity -batchmode -quit -nographics \
     -projectPath /proj -executeMethod BuildIos.Export
   ```
   产物是**自包含 Xcode 工程**（IL2CPP 生成的 C++ + `*.a` + `Data/`），不是 IPA。

4. **出 IPA**（纯 Linux，免 Mac）：
   ```bash
   cd <导出目录>
   cp <repo>/tools/{env.sh,linux_ipa_build.py} .
   source ./env.sh && export UNITY_VERSION_PATCH=62   # 若用 2022.3.62f1
   python3 linux_ipa_build.py smoke                    # 先自检
   python3 linux_ipa_build.py project --dir ./iOSBuild \
       --out ./App.ipa --workers 2 --use-project-pch
   ```

## 关键坑（血泪）

- **必须 `--use-project-pch`**：否则 `Keyboard.mm` 成片报 `undeclared identifier`。
- **62f1+ framework 头映射**：设 `UNITY_VERSION_PATCH=62`，编排器内置 `<UnityFramework/X.h>` 兼容。
- **入口门禁**：出包必须校验 `LC_MAIN` 入口不是 `mov x0,#0; ret` 空桩且含 `bl` 调用；
  详见 `docs/ipa/HOTFIX_CODE0_REPORT.md`（「启动即退出 code 0」事故复盘）。
- **内存小**：`--workers 2` 或 `BUILD_WORKERS=2`，避免 IL2CPP 并行编译 OOM。

## 边界

Linux Editor 能导出 Xcode 工程 → 我们补上 clang/ld64 链接段产出 ad-hoc IPA，
但产物**不能上架 App Store/TestFlight**，侧载需 SideStore/AltStore 重签。
卸载/真机画面手感无法在沙箱验证。
