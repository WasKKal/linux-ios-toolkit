# Unity Linux → iOS IPA 全链路手册（可移植知识库）

> **文档用途**：把「在无 macOS / 无 Xcode 的纯 Linux 环境里，用 Unity Linux Editor 开发游戏并编译打包出可侧载 IPA」的全部知识沉淀下来。**本文档自包含**——拿到新对话里作为唯一上下文即可照做，不依赖本次会话的任何其它文件。
> **编写依据**：2026-11-09 ~ 11-10 在 Ubuntu 22.04 沙箱里跑通的完整实测（Unity 2022.3.7f1 / 2022.3.62f1，Personal 授权，真实工程导出 → 编译 → 链接 → 签名 → IPA，后续 Build 4~10 与启动入口热修复）。
> **已知条件**：新对话环境**已有 iOS SDK**（如 `/opt/ios-sdk/sdks/iPhoneOS16.5.sdk`）。其余组件按 §2 补齐。
> **本轮定位**：这是上一轮 **S7（Linux iOS 出包链路 + 启动入口）的通用化推进成果**——把项目专属实现抽象成可复用的方法论、命令、模板与门禁。

---

## 0. 给新对话的使用说明

1. 把本文件整份作为上下文/附件提供给新对话，并说明：「环境已有 iOS SDK，目标是开发一个 Unity 新游戏并出 iOS IPA」。
2. 严格按顺序执行：**§2 环境自检 → §3 编辑器与授权 → §4 建工程/写玩法 → §5 导出 Xcode 工程 → §6 编译链接打包 → §7 验证 → §8 侧载**。
3. 附录 A 的 `linux_ipa_build.py` 是**已通过 smoke 自检的完整通用编排器**（arm64 / ad-hoc 签名 / 真实入口），保存成文件即可用；文档主体解释它每一步为什么这么写。
4. **三个不可省略的门禁**（都是踩过坑换来的）：
   - 授权必须真的 `Successfully activated ULF license`，不能只把 `.ulf` 放进目录；
   - 关键源码（`main.mm` / `static_main.mm`）编译失败必须**立即终止**，不能 `keep-going` 跳过；
   - 打包前必须校验 `LC_MAIN` 入口**不是** `mov x0,#0; ret` 空桩且含 `bl` 调用。

---

## 1. 原理：为什么 Linux 能出 iOS 包

**关键事实（实测推翻了常见误解）**：

- Unity 官方说 IL2CPP 不支持跨平台交叉编译，因此「Linux 直接产出 iOS Player」不行；**但 Linux Editor 可以 `BuildPipeline.BuildPlayer(BuildTarget.iOS)` 导出一个完整的 Xcode 工程**。GameCI FAQ 原文：*"Unity on linux can generate an xcode project"*。实测输出 `BuildResult=Succeeded`、约 640 MB 工程，含 IL2CPP 生成的游戏 C++、`Libraries/*.a`、`Data/`、私有引擎头。
- 官方剩下的「Xcode 工程 → IPA」一步需要 macOS 的 `xcodebuild`/`codesign`。**Linux 上把这一步自己补上即可**，不需要 Apple 工具：

```
Unity Linux Editor (batchmode)
  └─ BuildPlayer(iOS) → Xcode 工程（Classes/ Libraries/ Il2CppOutputProject/ Data/ pbxproj）
        └─ 自制编排器（替代 xcodebuild）
             1. 解析 project.pbxproj（build settings / frameworks / 变量展开）
             2. clang -target arm64-apple-ios → 编译 ObjC/ObjC++ 与 IL2CPP C++ → .o
             3. ld64.lld -arch arm64 -adhoc_codesign → 链接 Mach-O（自带 ad-hoc 签名）
             4. 组装 .app（Info.plist / Data / LaunchScreen）
             5. zip Payload/ → .ipa
             6. Mach-O / 入口 / 包结构校验（门禁）
```

| 能做 | 不能做 |
|---|---|
| 纯 Linux 产出 **arm64 + ad-hoc 签名**的可侧载 IPA | App Store / TestFlight 分发（需 Apple 签名与 provisioning profile） |
| 编译真实 IL2CPP 游戏代码与场景数据 | 真机调试、模拟器运行、Xcode Instruments |
| 用 clang/ld64.lld 完整替代 xcodebuild 的编译链接签名段 | `actool`/`ibtool`/storyboard 编译、Swift、动态 framework 加载 |
| 侧载后真机运行确认 | 生成带开发者证书的有效签名（需 SideStore/AltStore 重签） |

**行业现状**：`game-ci/unity-builder` 等开源方案都停在「导出 Xcode 工程」；声称能纯 Linux 出 IPA 的 `kxbuild` 是闭源 private beta；社区通用做法是「Linux 导出 + 云 Mac 编译」。本手册走的是自己补编排器的硬核路线，已实测闭环。

---

## 2. 环境自检与补齐（iOS SDK 已有，其余照此补）

| 组件 | 期望 | 自检 | 缺失时 |
|---|---|---|---|
| iOS SDK | `/opt/ios-sdk/sdks/iPhoneOS16.5.sdk`，含 `System/Library/Frameworks/UIKit.framework`、`usr/lib/libSystem.tbd`、`libobjc.tbd` | `ls /opt/ios-sdk/sdks/` | **已有**，记录实际版本号（影响 `IOS_SDK_VERSION`） |
| clang | **≥ 18**（实测 18.1.3） | `clang --version` | `apt install -y clang-18 lld-18 llvm-18`，或 `wget https://apt.llvm.org/llvm.sh && bash llvm.sh 18` |
| ld64.lld | `/usr/lib/llvm-18/bin/ld64.lld` | `ls -l /usr/lib/llvm-18/bin/ld64.lld` | 随 `lld-18` 安装 |
| zip / unzip | 系统自带 | `which zip unzip` | `apt install -y zip unzip` |
| Python 3 | ≥ 3.9（`plistlib`/`struct`/`zipfile`/`concurrent.futures`） | `python3 --version` | 一般自带 |
| Docker | 可 `docker run` | `docker run --rm hello-world` | 安装 docker；见 §3 镜像 |
| 磁盘 / 内存 | ≥ 40 GB 空闲、≥ 16 GB RAM | `df -h`、`free -g` | 导出工程约 640 MB，镜像 9~11 GB，编译中间产物数 GB |

**环境变量**（全部可通过 `export` 覆盖附录 A 脚本的默认值）：

```bash
export IOS_SDK=/opt/ios-sdk/sdks/iPhoneOS16.5.sdk
export CLANG=clang
export LD64=/usr/lib/llvm-18/bin/ld64.lld
export IPHONEOS_DEPLOYMENT_TARGET=12.0     # 必须与 pbxproj 一致
export IOS_SDK_VERSION=16.5                # 必须与 SDK 目录版本一致
export UNITY_VERSION_PATCH=7               # 2022.3.7f1→7；2022.3.62f1→62
```

**先跑一次工具链自检**（不需要 Unity、不需要授权，几分钟内出结果）：

```bash
python3 linux_ipa_build.py smoke --out /tmp/Smoke.ipa --work /tmp/smoke_work
# 期望：[MACHO] arm64=True exec=True signed=True
#       [ENTRY] bl>=1 空桩=False ok=True
#       [DONE] 工具链自检通过
```

> 若 smoke 失败，先修环境再往下走；不要带着坏工具链去排查 Unity。

---

## 3. Unity Linux Editor 与授权（最容易卡住的一环）

### 3.1 编辑器镜像

GameCI 提供带 `iOSSupport` 的 Linux 编辑器镜像，命名 `unityci/editor:ubuntu-<Unity版本>-ios-<修订号>`：

| 版本 | 镜像 | 体积 | 备注 |
|---|---|---|---|
| 2022.3.7f1 | `unityci/editor:ubuntu-2022.3.7f1-ios-1` | 8.83 GB | 稳定基线，实测全链路 |
| 2022.3.62f1 | `unityci/editor:ubuntu-2022.3.62f1-ios-3` | 10.8 GB | 升级版；公开头改成 framework 风格 `#import`（见 §6.3） |

- 选择原则：**与工程 `ProjectSettings/ProjectVersion.txt` 保持一致**。换引擎会触发全量资源重导入（首次可能十几分钟，且沙箱休眠会中断构建，建议复用带 `Library/Bee` 缓存的工程副本续跑）。
- 镜像关键路径：`/opt/unity/Editor/Unity`、`/opt/unity/Editor/Data/Resources/Licensing/Client/Unity.Licensing.Client`、`/opt/unity/Editor/Data/PlaybackEngines/iOSSupport/`。
- **网络**：实测 `registry-1.docker.io` 直连不通但腾讯云容器镜像加速源可达。做法：在 `/etc/docker/daemon.json` 配 `registry-mirrors`（用当前可用的 Docker Hub 加速地址），`systemctl restart docker` 后重拉；`docker pull` 本身通常可走加速通道，`manifest inspect` 直连失败不代表拉不下来。

### 3.2 Personal 授权机制（重要纠错，别走弯路）

| 事项 | 现状 |
|---|---|
| Personal 激活方式 | **只能 Named User Licensing（NUL）**：Unity ID 在线登录 |
| Personal 序列号 | **没有** |
| 网页手动激活（`.alf` → `license.unity3d.com/manual` → `.ulf`） | **对 Personal 已关闭**，页面原文 `Unity no longer supports manual activation of Personal licenses.`；仅 Pro/Enterprise 可用 |
| `Unity -batchmode -username/-password` | **对 Personal 无效**（这是旧资料误导点） |
| 可行路径 | Editor 自带的 `Unity.Licensing.Client --activate-ulf --username <email> --password <pwd>`，无 GUI 在线换取 `.ulf`（实测一次通过，未触发验证码） |
| Hub 兜底 | 云桌面/任意可装 Hub 的机器登录 → `Licenses → Add → Get a free personal license` → 复制 `.ulf`；`.ulf` 路径：Linux `~/.local/share/unity3d/Unity/Unity_lic.ulf`（**不是** `~/.config`），Windows `C:\ProgramData\Unity\Unity_lic.ulf`，macOS `/Library/Application Support/Unity/Unity_lic.ulf` |
| 绑定 | `.ulf` 含 `MachineBindings`（hostname + machine-id），**换容器/换机器必须重新在线激活**；不绑定 Unity 版本/操作系统 |
| 时效 | `com.unity.editor.time-limited-license`：Personal 是限时许可，长期构建前用 `--update-ulf --username --password` 续期 |

**最致命的坑**：该镜像 `Entrypoint=null`、`Cmd=["/bin/bash"]`，**没有 license 自动注入脚本**。只传 `UNITY_LICENSE` 环境变量给 `docker run` 不生效，Unity 仍报：

```
[Licensing::Client] Error: No ULF license found., Token not found in cache
No valid Unity Editor license found. Please activate your license.   → exit 1
```

必须**先把 `.ulf` 落盘到容器内 `/root/.local/share/unity3d/Unity/Unity_lic.ulf`，再在同一容器里跑一次 `--activate-ulf`**。

### 3.3 完整命令模板（授权 + 导出一次做完）

```bash
# 1) 只需做一次：在线激活并取出 .ulf
export UNITY_EMAIL="你的Unity ID邮箱" UNITY_PASSWORD="密码"
mkdir -p /workspace/unity_license
docker run --rm -v /workspace/unity_license:/out \
  -e UNITY_EMAIL -e UNITY_PASSWORD \
  -e DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
  --entrypoint bash unityci/editor:ubuntu-2022.3.7f1-ios-1 -c '
    C=/opt/unity/Editor/Data/Resources/Licensing/Client/Unity.Licensing.Client
    "$C" --activate-ulf --username "$UNITY_EMAIL" --password "$UNITY_PASSWORD"
    cp "$(find / -name Unity_lic.ulf -type f | head -1)" /out/Unity_lic.ulf
  '

# 2) 每次构建：注入授权 + 导出 iOS Xcode 工程
docker run --rm \
  -e UNITY_EMAIL -e UNITY_PASSWORD \
  -e DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
  -e IOS_EXPORT_DIR=/out/iOSBuild \
  -v /workspace/unity_license:/lic:ro \
  -v "$PWD/MyGame:/project" -v "$PWD/export:/out" \
  --entrypoint bash unityci/editor:ubuntu-2022.3.7f1-ios-1 -c '
    mkdir -p /root/.local/share/unity3d/Unity
    cp /lic/Unity_lic.ulf /root/.local/share/unity3d/Unity/Unity_lic.ulf
    C=/opt/unity/Editor/Data/Resources/Licensing/Client/Unity.Licensing.Client
    "$C" --activate-ulf --username "$UNITY_EMAIL" --password "$UNITY_PASSWORD"
    /opt/unity/Editor/Unity -batchmode -nographics -quit \
      -projectPath /project -buildTarget iOS \
      -executeMethod BuildIos.Export -logFile /dev/stdout
  '
```

> 安全：`.ulf` 绑定账号，**不要提交公开仓库/不要外发**；建议注册专用构建小号。遇到新设备邮箱验证码/2FA 就走 Hub 兜底。

**Linux 侧的非致命噪声日志**（不用管）：`Native extension for iOS target not found`、`Access token is unavailable; failed to update`、`Curl error 42`、`[SignatureVerifier] Application signature verification not supported on this platform`。判据只看 `Exiting batchmode successfully now!` / `UNITY_EXIT=0` 与 `BuildResult=Succeeded`。

---

## 4. 从零开发新游戏：Linux 无 GUI 工作流

Unity Linux Editor 没有图形界面，**所有内容都要用脚本或文本文件生成**。这是与常规 Unity 开发最大的差异。

### 4.1 创建工程

```bash
# 方式 A：命令行创建空工程（Unity 2022 支持）
/opt/unity/Editor/Unity -batchmode -nographics -quit -createProject /workspace/MyGame

# 方式 B：手工构造最小目录（最可控，避免版本差异）
MyGame/
├── Assets/
│   ├── Scripts/            # 运行时 C#（MonoBehaviour）
│   ├── Editor/             # 构建脚本（必须放这里，才能 using UnityEditor）
│   ├── Shaders/            # 自制 ShaderLab
│   └── Scenes/             # .unity 场景
├── ProjectSettings/
│   └── ProjectVersion.txt  # m_EditorVersion: 2022.3.7f1（与镜像一致）
└── Packages/
    └── manifest.json
```

`Packages/manifest.json` 最小可用（**缺 `com.unity.ugui` 会让 UI 编译失败**）：

```json
{
  "dependencies": {
    "com.unity.modules.ui": "1.0.0",
    "com.unity.modules.imgui": "1.0.0",
    "com.unity.modules.physics": "1.0.0",
    "com.unity.modules.audio": "1.0.0",
    "com.unity.modules.animation": "1.0.0",
    "com.unity.modules.particlesystem": "1.0.0",
    "com.unity.ugui": "1.0.0"
  }
}
```

> 旧 Unity 工程（如 2017 反编译工程）升级到 2022 时，还要额外补：UNET/VR/PSP2 兼容 shim、移出 `Assets/Editor` 里平台专属脚本、修复旧 API（`FindObjectsByType` 等）、把旧内置 shader 的 `fileID` 换成自制 shader——这类工程迁移成本很高，新游戏建议直接按上面最小结构起步。

### 4.2 程序化创建场景与玩法（无 GUI 的关键技巧）

不要手写 `.unity` YAML（guid 极难维护）。在 `Assets/Editor/` 里用 Editor 脚本建场景、挂组件、保存，并把场景注册进 Build Settings：

```csharp
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

public static class GameBootstrap
{
    [MenuItem("Tools/Create Game Scene")]
    public static void Create()
    {
        var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
        var go = new GameObject("GameRoot");
        go.AddComponent<GameMain>();                 // 你的 MonoBehaviour
        var cam = new GameObject("Main Camera");
        cam.tag = "MainCamera";
        cam.AddComponent<Camera>();
        cam.AddComponent<AudioListener>();
        cam.transform.position = new Vector3(0, 1, -10);
        System.IO.Directory.CreateDirectory("Assets/Scenes");
        EditorSceneManager.SaveScene(scene, "Assets/Scenes/Main.unity");
        EditorBuildSettings.scenes = new[] {
            new EditorBuildSettingsScene("Assets/Scenes/Main.unity", true)
        };
        AssetDatabase.SaveAssets();
    }
}
```

最小玩法脚本（用 `OnGUI` 可避开 UI 资源依赖，适合先跑通链路）：

```csharp
using UnityEngine;

public class GameMain : MonoBehaviour
{
    private string _msg = "Hello from Unity on Linux";
    void OnGUI()
    {
        GUI.Label(new Rect(20, 20, 600, 60), _msg);
        if (GUI.Button(new Rect(20, 100, 220, 80), "Tap me"))
            _msg = "Tapped at " + Time.time.ToString("F1");
    }
    void Update()
    {
        // iOS 没有键盘鼠标：用 Input.touchCount / Input.GetTouch，或自己写 OnGUI 按钮
        if (Input.touchCount > 0 && Input.GetTouch(0).phase == TouchPhase.Began)
            _msg = "Touch: " + Input.GetTouch(0).position;
    }
}
```

### 4.3 移动端开发必须提前考虑的差异

| 主题 | 要点 |
|---|---|
| 输入 | 没有键盘（别写 `Input.GetKeyDown(KeyCode.E)` 做拾取）、没有鼠标右键；用 `Input.touches` / `Input.GetMouseButton(0)`（iOS 会映射单点触摸）/ `OnGUI` 按钮 / 虚拟摇杆 |
| 字体 | Unity 内置字体**不含中文字形**，中文会变豆腐块；随包放子集化 OTF/TTF（例如从 Noto Sans CJK 提取用字），并用 `Font.CreateDynamicFontFromOSFont` 做回退 |
| Shader | 旧内置 shader 的 legacy `fileID` 在 Unity 2022 + iOS Metal 上解析异常 → **品红**；自制 ShaderLab 显式写 `Queue`/`ZWrite`/`ZTest`；用 `Shader.Find` 的 shader 要进 **Always Included Shaders**（`GraphicsSettings.asset`），否则打包后丢失 |
| 深度/队列 | 天空穹顶用 `ZWrite Off` + `ZTest Always` 且放 Background 队列；透明水面别用 `ZTest LEqual` 覆盖其它材质；按 **shader 名**判定要禁用的旧水面，别按对象名替换材质 |
| 资源 | 付费插件用免费/自制替代；避开 ComputeShader、依赖 macOS 原生扩展的插件、动态加载 `UnityFramework.framework` |
| 画面 | 横竖屏、`Screen.safeArea`、分辨率适配；程序化噪声贴图用 `tex2Dlod` 而非隐式导数（避免 shader 编译错误） |
| 性能 | 移动 GPU 上把网格密度、FFT 水面、后处理降级；CPU 侧波场用解析梯度给木筏做俯仰/横摇，比 GPU 回读便宜 |

---

## 5. 导出 iOS Xcode 工程

### 5.1 `Assets/Editor/BuildIos.cs` 完整模板（可直接复制）

```csharp
using System;
using System.IO;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

public static class BuildIos
{
    private static readonly string[] Scenes = { "Assets/Scenes/Main.unity" };

    public static void Export()
    {
        string outDir = Environment.GetEnvironmentVariable("IOS_EXPORT_DIR");
        if (string.IsNullOrEmpty(outDir)) outDir = "/out/iOSBuild";

        Debug.Log("[PROBE] Editor version: " + Application.unityVersion);
        Debug.Log("[PROBE] IsBuildTargetSupported(iOS): " +
                  BuildPipeline.IsBuildTargetSupported(BuildTargetGroup.iOS, BuildTarget.iOS));
        Debug.Log("[PROBE] iOS scripting backend: " +
                  PlayerSettings.GetScriptingBackend(BuildTargetGroup.iOS));

        foreach (string s in Scenes)
            if (!File.Exists(s)) throw new Exception("Scene missing: " + s);

        var buildScenes = new EditorBuildSettingsScene[Scenes.Length];
        for (int i = 0; i < Scenes.Length; i++)
            buildScenes[i] = new EditorBuildSettingsScene(Scenes[i], true);
        EditorBuildSettings.scenes = buildScenes;

        // 必配 PlayerSettings（Linux 无 Inspector，只能代码设置）
        PlayerSettings.productName = "MyGame";
        PlayerSettings.SetApplicationIdentifier(BuildTargetGroup.iOS, "com.example.mygame");
        PlayerSettings.iOS.targetOSVersionString = "12.0";
        PlayerSettings.SetScriptingBackend(BuildTargetGroup.iOS,
                                           ScriptingImplementation.IL2CPP);
        AssetDatabase.SaveAssets();

        var options = new BuildPlayerOptions {
            scenes = Scenes,
            locationPathName = outDir,
            target = BuildTarget.iOS,
            options = BuildOptions.None
        };
        BuildReport report = BuildPipeline.BuildPlayer(options);
        Debug.Log("[PROBE] BuildResult=" + report.summary.result
                  + " totalErrors=" + report.summary.totalErrors
                  + " totalWarnings=" + report.summary.totalWarnings
                  + " totalSize=" + report.summary.totalSize);
        if (report.summary.result != BuildResult.Succeeded)
            throw new Exception("BuildPlayer did not succeed: " + report.summary.result);
        Debug.Log("[PROBE] SUCCESS: iOS Xcode project at " + outDir);
    }
}
```

### 5.2 导出产物结构与成功判据

| 目录 | 体积量级 | 内容 |
|---|---|---|
| `Libraries/` | ~398 MB | `libiPhone-lib.a`(347MB)、`libil2cpp.a`(68MB)、`baselib.a`；另有 `Classes/` 私有头 |
| `Il2CppOutputProject/Source/il2cppOutput/` | ~238 MB | **IL2CPP 生成的游戏逻辑 C++**（几十~几百个 `.cpp`），这才是游戏本体 |
| `Data/` | 几 MB | `level0`、`globalgamemanagers`、`Managed/Metadata/global-metadata.dat`、`boot.config` |
| `Classes/` | ~700 KB | `main.mm`、`UnityAppController.mm`、`Prefix.pch`、私有头 |
| 其它 | — | `Info.plist`、`Unity-iPhone.xcodeproj/project.pbxproj`、`MainApp/main.mm`、`UnityFramework/` |

成功判据：`BuildResult=Succeeded`、`totalErrors=0`、`UNITY_EXIT=0`、`error CS=0`、`Shader error=0`。**导出工程必须是「带授权的完整工程」**——仅镜像里的 `Trampoline/` 模板缺 il2cpp 产物，只能用来演示解析。

---

## 6. Linux 侧替代 xcodebuild（S7 核心，逐环节精细）

> 本节是上一轮 S7 的通用化主线。附录 A 的脚本就是本节的完整实现；这里解释每一步「为什么」，因为新版本 Unity / 新 SDK 很容易在这里踩坑。

### 6.1 pbxproj 解析与变量展开

`project.pbxproj` 是 OpenStep plist，正则抽取够用（无需 `plutil`）。必须抽取：`PRODUCT_NAME`、`EXECUTABLE_NAME`、`PRODUCT_BUNDLE_IDENTIFIER`、`IPHONEOS_DEPLOYMENT_TARGET`、`ARCHS`、`CLANG_ENABLE_OBJC_ARC`、frameworks、`Sources` 阶段源文件。

**必做**：递归展开 `$(VAR)`。Unity 写的是 `PRODUCT_NAME = "$(PRODUCT_NAME_APP)"`，不展开会生成名为 `$(PRODUCT_NAME_APP).app` 的目录，安装器直接失败。

### 6.2 源码与静态库收集规则（三要三不要）

| 要收 | 不要收 |
|---|---|
| `Classes/**/*.mm|m|cpp|c`、`Libraries/**/*.mm|m|cpp|c` | `Il2CppOutputProject/IL2CPP/**` 运行时源码（**已预编译进 `libil2cpp.a`，再编会重复定义**） |
| `Il2CppOutputProject/Source/**/*.cpp|c`（游戏逻辑） | `MainApp/main.mm`（官方版动态加载 `UnityFramework.framework`，与静态单可执行文件方案冲突） |
| `Libraries/**/*.a`（`libiPhone-lib.a` / `libil2cpp.a` / `baselib.a`） | 任何 `Tests` 路径 |

`MainApp/` 下其它 `.mm` 要收（含 `UnityFramework` 类实现），否则 `[UnityFramework getInstance]` 运行时失败。

### 6.3 framework 头映射层（**Unity 2022.3.62f1+ 必做，S7 事故根因**）

- 7f1 用 `#import "UnityAppController.h"`（引号）；**62f1 起改成 framework 风格 `#import <UnityFramework/UnityAppController.h>`**。Xcode 有 framework header map，Linux clang 没有。
- 现象：`Classes/main.mm` 与生成的 `static_main.mm` 一起报 `fatal error: 'UnityFramework/UnityAppController.h' file not found` → 两个入口源码被 `--keep-going` 跳过 → `_main` 变 undefined → 补桩生成 `mov x0,#0; ret` → **链接成功但启动即退出**（`App returned from its main function with code 0.`）。
- 修复（附录 A `fw_header_shim()`）：扫描导出工程里所有 `<UnityFramework/X.h>`，在 `work/fwheaders/UnityFramework/` 生成转发头 `#include "<绝对路径>/X.h"`，找不到就写占位空头，并把 `work/fwheaders` **插入 include 路径首位**。7f1 无需此层（扫描结果为空，自动跳过）。

### 6.4 编译宏（7 个，缺一不可）

```text
UNITY_IOS=1
IL2CPP_TARGET_ARM64=1
UNITY_VERSION_VER=2022
UNITY_VERSION_MAJ=3
UNITY_VERSION_MIN=<补丁号>          # 7f1→7，62f1→62；用 UNITY_VERSION_PATCH 环境变量覆盖
BASELIB_INLINE_NAMESPACE=il2cpp_baselib
RUNTIME_IL2CPP=1
```

| 宏 | 缺失后果 |
|---|---|
| `UNITY_VERSION_VER/MAJ/MIN` | `UnityTrampolineConfigure.h` 直接 `#error`（来自 pbxproj 的 `OTHER_CFLAGS`） |
| `BASELIB_INLINE_NAMESPACE=il2cpp_baselib` | `baselib::ReentrantLock` 与 `il2cpp-object-internals.h` 二义性 |
| `RUNTIME_IL2CPP=1` | `g_CodegenRegistration` 被 `#if` 屏蔽，`libil2cpp.a` 的 `vm::Runtime::Init` 引用落空 |
| `UNITY_VERSION_MIN` 与导出工程补丁号不符 | 版本校验头报错 / 结构不匹配 |

### 6.5 include 路径（对齐 pbxproj 的 `HEADER_SEARCH_PATHS`）

```text
<work>/fwheaders                                    （头映射层，放首位）
<proj>/Classes, Classes/UI, Classes/Unity, Classes/PluginBase, Classes/Native
<proj>/Libraries, Libraries/bdwgc/include
<proj>/Il2CppOutputProject/Source/il2cppOutput
<proj>/Il2CppOutputProject/IL2CPP/libil2cpp, libil2cpp/pch
<proj>/Il2CppOutputProject/IL2CPP/external/baselib/Include
<proj>/Il2CppOutputProject/IL2CPP/external/baselib/Platforms/IOS/Include   ← 缺它 BaselibPlatformSpecificEnvironment.h 找不到
<proj>/Il2CppOutputProject/IL2CPP/external/xxHash, libmono
<proj>/Libraries/external/baselib/Platforms/IOS/Include
```

### 6.6 静态版 main（为什么不能用官方 MainApp/main.mm）

官方 `MainApp/main.mm` 动态加载 `Frameworks/UnityFramework.framework`；本方案把全部代码**静态链进单个可执行文件**，因此排除官方 main，自己生成一个直接取单例的 `static_main.mm`：

```objc
#include <UIKit/UIKit.h>
#include <mach-o/ldsyms.h>
#include "UnityFramework/UnityFramework.h"
int main(int argc, char* argv[]) {
  @autoreleasepool {
    id ufw = [UnityFramework getInstance];
    [ufw setExecuteHeader: &_mh_execute_header];
    [ufw runUIApplicationMainWithArgc: argc argv: argv];
  }
  return 0;
}
```

这样才得到**真实 `_main`**（`T _main`）并进入 `UIApplicationMain` 运行循环。

### 6.7 clang 编译参数与语言分流

```bash
clang -target arm64-apple-ios12.0 -isysroot $IOS_SDK \
      -F$SDK/System/Library/Frameworks -I$SDK/usr/include \
      -I<所有 include 路径> -D<7 个宏> \
      -O2 -Wall -Wno-everything -x <lang> -c <src> -o <obj>
```

| 语言 | 附加 | PCH | 优化 |
|---|---|---|---|
| `.mm/.m`（ObjC/ObjC++） | `-fblocks -fobjc-runtime=ios-12.0 -fobjc-arc` | 注入 `Classes/Prefix.pch`（或最小 PCH：Foundation + UIKit） | `-O2` |
| `.cpp/.c`（IL2CPP 生成） | **不加** ObjC 标志（否则噪声告警） | **不注入**（生成代码自带 `pch-cpp.hpp`） | `-O0`（50MB 源码，串行太慢） |

用 `ThreadPoolExecutor` 并行（`min(16, cpu_count)`）。**`main.mm` / `static_main.mm` 编译失败必须立即终止**，不能被 `--keep-going` 跳过。

### 6.8 ld64.lld 链接 + ad-hoc 签名 + 补桩/禁桩

```bash
ld64.lld -arch arm64 -platform_version ios 12.0 16.5 \
  -syslibroot $IOS_SDK -o <binary> \
  <所有 .o> <Libraries/*.a> \
  -L$SDK/usr/lib -F$SDK/System/Library/Frameworks \
  -framework Foundation -framework UIKit ... （约 19 个） \
  -lobjc -lSystem -lc++ -lz -liconv -lsqlite3 \
  -adhoc_codesign -pie --error-limit=0
```

- **`-adhoc_codesign` 是关键**：`ld64.lld` 自带 ad-hoc 签名，写出标准 `LC_CODE_SIGNATURE` + CodeDirectory（SuperBlob `0xfade0cc0`）。**不需要 `ldid`/`codesign`/Mac**。
- **自动补桩**：链接报 `undefined symbol:` 时解析符号名、生成 arm64 汇编桩（`mov x0,#N; ret`），最多 3 轮。实测最终稳定剩 4 个平台辅助符号，都不是游戏逻辑：

  | 符号 | 桩返回值 | 说明 |
  |---|---|---|
  | `GetReadableStackTrace` | 0 | 诊断栈回溯 |
  | `GetStacktracetNativeOption` | 0 | 栈回溯开关 |
  | `Geo::GetiOSAppDocumentsDir` | 0 | 沙盒路径 |
  | `___isPlatformVersionAtLeast` | **1** | `@available` 运行时判定，返回真值避免误判低系统版本 |

  已用 `llvm-nm --defined-only` 核实它们确实不在任何 `.a` 里；空场景不会调用到，用到对应功能时需补真实实现。

- **禁桩（S7 硬门禁）**：`CRITICAL_SYMBOLS = {_main, main, _UIApplicationMain, _NSApplicationMain}`。一旦这些符号出现在 undefined 列表，**直接报错退出**，绝不生成空桩。因为「链接成功 ≠ 可运行」。

### 6.9 入口门禁与 Mach-O 校验

打包前必须两项都过：

| 检查 | 判据 |
|---|---|
| `verify_macho` | `magic=0xfeedfacf`、`cputype=0x0100000c`(arm64)、`filetype=2`(MH_EXECUTE)、含 `LC_CODE_SIGNATURE`(0x1D) |
| `verify_entrypoint` | 找到 `LC_MAIN`(0x80000028) 的 `entryoff`；读入口 96 字节：**不是** `mov x0,#0 / ret`（`0xD2800000`/`0x52800000` + `0xD65F03C0`）、不是立即 `ret`、且**至少一条 `bl`**（`w & 0xFC000000 == 0x94000000`） |

坏包入口：`d2800000 d65f03c0`。好包入口：`a9bd57f6 a9014ff4 …`（真实压栈）+ 多条 `bl`。

### 6.10 组装 `.app` / `Info.plist` / IPA

- `Info.plist` 从工程模板读取并替换 `${EXECUTABLE_NAME}` / `${PRODUCT_BUNDLE_IDENTIFIER}` / `${PRODUCT_NAME}`；必须键：`CFBundleExecutable`、`CFBundleIdentifier`、`CFBundlePackageType=APPL`、`MinimumOSVersion`、`CFBundleSupportedPlatforms=["iPhoneOS"]`、`LSRequiresIPhoneOS=true`、`UIRequiredDeviceCapabilities=["arm64"]`、`UILaunchStoryboardName`。
- **二进制保持 `ld64.lld` 刚签完名的原样，绝不追加/改写任何字节**，否则破坏 CodeDirectory 哈希覆盖；构建标记放 `Info.plist` 自定义字段。
- `Data/` 原样拷进 `.app/Data/`；`LaunchScreen.storyboard` 用空壳占位即可。
- 打包：在临时目录建 `Payload/<Product>.app`，`zip -q -r -X <ipa> Payload`（`-X` 去掉多余扩展字段）。

---

## 7. 验证清单（出包门禁）

### 7.1 可复制的独立验证脚本

```python
import zipfile, struct, sys
p = sys.argv[1]
z = zipfile.ZipFile(p); names = z.namelist()
# 定位主可执行文件：排除目录条目，取 Payload/*.app/ 下无扩展名且 >1MB 的文件
binname = next((i.filename for i in z.infolist()
                if i.filename.startswith('Payload/') and i.filename.count('/') == 2
                and not i.filename.endswith('/') and '.' not in i.filename.split('/')[-1]
                and i.file_size > 1_000_000), None)
d = z.read(binname)
magic, cpu, sub, ftype, ncmds = struct.unpack_from('<IiiII', d, 0)
off, entry, has_sig, bls = 32, None, False, 0
for _ in range(ncmds):
    cmd, sz = struct.unpack_from('<II', d, off)
    if cmd == 0x80000028: entry = struct.unpack_from('<Q', d, off + 8)[0]  # LC_MAIN
    if cmd == 0x1D: has_sig = True                                          # LC_CODE_SIGNATURE
    off += sz
if entry is not None:
    code = d[entry:entry + 96]
    ws = struct.unpack('<%dI' % (len(code)//4), code[:len(code)//4*4])
    stub = len(ws) >= 2 and ws[0] in (0x52800000, 0xD2800000) and ws[1] == 0xD65F03C0
    bls = sum(1 for w in ws if (w & 0xFC000000) == 0x94000000)
else:
    stub = True
checks = {
  'zip+Payload': any(n.startswith('Payload/') for n in names),
  'Info.plist': any(n.endswith('Info.plist') for n in names),
  'Mach-O 64': d[:4] == b'\xcf\xfa\xed\xfe',
  'arm64': (cpu & 0xFFFFFFFF) == 0x0100000C,
  'MH_EXECUTE': ftype == 2,
  'LC_CODE_SIGNATURE': has_sig,
  'LC_MAIN': entry is not None,
  '入口非空桩': not stub,
  '入口含 bl': bls >= 1,
  'global-metadata.dat': any('global-metadata.dat' in n for n in names),
  'Unity 版本串': (b'2022.3.7f1' in d or b'2022.3.62f1' in d),
}
for k, v in checks.items(): print(('PASS' if v else 'FAIL'), k)
sys.exit(0 if all(checks.values()) else 1)
```

### 7.2 完整检查表

结构层：zip 完整、`Payload/*.app`、`Info.plist`、`Data/level0`、`globalgamemanagers`、`global-metadata.dat`。
二进制层：Mach-O 64 / arm64 / MH_EXECUTE / `LC_CODE_SIGNATURE` / `LC_MAIN` / 入口非空桩 + `bl` / 含 `runUIApplicationMainWithArgc` 与 `UIApplicationMain` 字符串。
内容层：IL2CPP 生成的游戏符号、随包资源、自制 shader 属性、本地化词条（中文需 UTF-8 明文进 `global-metadata.dat`）。
日志层：`BuildResult=Succeeded`、`error CS=0`、`Shader error=0`、**无 `[STUB] _main`**、**无 `main.mm 编译失败并跳过`**、`[HEADER]` 与 `[ENTRY] ok=True` 存在。

### 7.3 S7 事故复盘（务必记住的因果链）

```
引擎升级 62f1 → 公开头改 <UnityFramework/X.h>
  → Linux clang 无 header map
  → Classes/main.mm + static_main.mm 编译失败
  → --keep-going 静默跳过
  → _main 变 undefined
  → 补桩生成 mov x0,#0; ret
  → 链接成功、arm64=True、signed=True、BuildResult=Succeeded（全是假阳性）
  → 侧载报 App returned from its main function with code 0.
```

**教训**：① 链接成功不等于可运行；② 关键源码失败必须终止；③ 入口机器码必须做出包门禁；④ 换 Unity 补丁版本时检查公开头导入风格是否变化，头映射层会自动扫描生成，并同步 `UNITY_VERSION_PATCH`。

---

## 8. 侧载与真机验证

1. IPA 是 **ad-hoc 签名**，必须用 **SideStore / AltStore** 用个人 Apple ID 证书重签后安装；重签会改写 `LC_CODE_SIGNATURE`，属正常。
2. 换包前**先卸载旧包**，避免 iOS 复用缓存的可执行文件/签名。
3. 真机重点验证：能否进入主循环（不秒退）、画面/材质正常（无品红/染蓝/覆盖）、触屏输入与按钮布局、中文字体、性能与发热。
4. 沙箱内**无法**做真机验证，所有结论只到 Mach-O / 签名 / 包结构 / 符号层。

---

## 9. 排错速查表

| 现象 | 根因 | 处理 |
|---|---|---|
| `No valid Unity Editor license found` / `exit 1` | 只传了 `UNITY_LICENSE` 环境变量，镜像无自动注入 | `.ulf` 落盘到 `/root/.local/share/unity3d/Unity/`，同容器再跑 `--activate-ulf` |
| 换容器后授权失效 | `MachineBindings` 绑定 hostname + machine-id | 用账号密码重新在线激活或 `--update-ulf` |
| Personal 网页手动激活找不到入口 | 官方已关闭 | 改 `--activate-ulf` 或 Hub 登录 |
| `Native extension for iOS target not found` | Linux 缺 Mac 侧原生扩展 | 非致命，忽略 |
| `.app` 名为 `$(PRODUCT_NAME_APP).app` | pbxproj 变量未展开 | 递归展开 `$(VAR)` |
| `UnityTrampolineConfigure.h #error` | 缺 `UNITY_VERSION_*` 宏 | 补齐 §6.4 七个宏 |
| `BaselibPlatformSpecificEnvironment.h not found` | 缺 iOS baselib include 路径 | 补 §6.5 路径 |
| `ReentrantLock` 二义性 | 缺 `BASELIB_INLINE_NAMESPACE` | 加宏 |
| `g_CodegenRegistration` undefined | 缺 `RUNTIME_IL2CPP=1` | 加宏 |
| 重复定义 / duplicate symbol | 把 `IL2CPP/` 运行时源码也编了 | 只编 `Il2CppOutputProject/Source/` |
| `UnityFramework/X.h file not found`（62f1+） | framework 风格尖括号导入 | §6.3 头映射层 |
| **启动即退出 `code 0`** | `_main` 被空桩顶替 | 关键源码失败即终止 + `CRITICAL_SYMBOLS` + `verify_entrypoint` |
| 品红 / 全屏染蓝 | 旧内置 shader `fileID` 在 Metal 失效 / 材质被全局替换 | 自制 ShaderLab 显式深度与队列，按 shader 名判定而非对象名 |
| 抬头天空消失 | 穹顶 `ZTest LEqual` | 恢复 `ZWrite Off` + `ZTest Always` + Background 队列 |
| shader 打包后丢失 | 未进 Always Included Shaders | 加进 `GraphicsSettings.asset` |
| 中文豆腐块 | 字体无 CJK 字形 | 随包子集化 CJK 字体 + 动态字体回退 |
| 链接成功但 `[STUB] _main` | 入口源码缺失 | 视为构建失败，不要交付 |
| 首次换引擎导出极慢/被中断 | 全量资源重导入 | 复用带 `Library/Bee` 缓存的工程副本增量续跑 |

---

## 10. 最短 SOP（从零到可安装 IPA）

```bash
# ── 0. 环境 ────────────────────────────────────────────────────────────────
python3 linux_ipa_build.py smoke --out /tmp/Smoke.ipa     # 工具链自检，必须先过

# ── 1. 授权（一次性） ────────────────────────────────────────────────────
export UNITY_EMAIL=... UNITY_PASSWORD=...
# 按 §3.3 第 1 段 docker run → 得到 Unity_lic.ulf

# ── 2. 工程 ───────────────────────────────────────────────────────────────
# 建 MyGame/（§4.1 目录 + manifest.json），写 Assets/Scripts/GameMain.cs、
# Assets/Editor/GameBootstrap.cs（§4.2）与 Assets/Editor/BuildIos.cs（§5.1）
# 先跑一次 -executeMethod GameBootstrap.Create 生成场景（或把建场景并入 BuildIos.Export）

# ── 3. 导出 ───────────────────────────────────────────────────────────────
# 按 §3.3 第 2 段 docker run → export/iOSBuild（完整 Xcode 工程）
python3 linux_ipa_build.py parse --pbxproj export/iOSBuild/Unity-iPhone.xcodeproj/project.pbxproj

# ── 4. 编译链接打包 ──────────────────────────────────────────────────────
export UNITY_VERSION_PATCH=7        # 或 62
python3 linux_ipa_build.py project --dir export/iOSBuild \
    --out MyGame.ipa --work /tmp/ipa_work --use-project-pch
# 门禁自动执行：Mach-O 校验 + LC_MAIN 入口校验，不过不产 IPA

# ── 5. 验证 + 侧载 ───────────────────────────────────────────────────────
python3 verify_ipa.py MyGame.ipa     # §7.1 脚本，应全 PASS
# SideStore / AltStore 重签安装；先卸载旧包
```

---

## 11. 边界与不可做事项

- **不能上架**：ad-hoc 签名 + 无 provisioning profile，App Store / TestFlight 必须回到 macOS/Xcode。
- **不能真机调试**：无 iOS 设备、无 Xcode Instruments；沙箱验证只到二进制与包结构层。
- **不是完整 xcodebuild**：不处理 storyboard/asset catalog 编译、Swift、CocoaPods、动态 framework；方案是「全部代码静态链进单个 Mach-O」。
- **4 个平台桩**：诊断栈回溯 / 沙盒路径 / `@available` 判定由汇编桩占位；依赖这些功能的玩法需补真实实现。
- **授权合规**：Personal 是限时 NUL 许可；`.ulf` 绑定账号，勿外发；商业游戏资产不可反编译后发布。
- **引擎版本耦合**：`UNITY_VERSION_MIN`、公开头导入风格、`UnityFramework.h` 布局随补丁版本变化；升级时逐项核对 §6.3/§6.4。

---

## 附录 A：通用编排器完整源码（`linux_ipa_build.py`，已 smoke 自检通过）

> 保存为 `linux_ipa_build.py` 即可运行。实测：Ubuntu 22.04 / clang 18.1.3 / `ld64.lld` (LLVM 18) / iPhoneOS16.5.sdk；`smoke` 输出 `arm64=True exec=True signed=True`、`bl=5 空桩=False ok=True`。
> 验证记录：本附录代码与配套脚本 `/workspace/UNITY_LINUX_IPA_TOOLKIT/linux_ipa_build.py` **逐字一致**，已单独做语法检查，并直接用「从本 MD 中提取出来的代码」以相对路径输出跑通 `smoke`；期间修掉了一个真实 bug（`pack_ipa` 在临时目录里执行 `zip`，相对输出路径会写进临时目录后被删，现已统一 `Path(...).resolve()`）。

<!--TOOLKIT_BEGIN-->

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linux_ipa_build.py — 纯 Linux 上替代 xcodebuild 的精简实现（通用版，无项目专属依赖）

把 Unity 在 Linux 上导出的 iOS Xcode 工程（Trampoline 结构）：
    Classes/ + Libraries/ + Il2CppOutputProject/ + Data/ + MainApp/
    + Info.plist + Unity-iPhone.xcodeproj/project.pbxproj
编译、链接、ad-hoc 签名、打包成可被 SideStore / AltStore 重签安装的 .ipa。

替代的正是 xcodebuild 这条链：
    pbxproj 解析 → clang 交叉编译 → ld64.lld 链接(Mach-O + ad-hoc 签名)
    → 组装 .app → zip 成 IPA → Mach-O / 入口 / IPA 结构校验

用法：
    python3 linux_ipa_build.py smoke    --out out/Smoke.ipa       # 工具链自检，不需要 Unity
    python3 linux_ipa_build.py parse    --pbxproj <path>           # 检查导出工程
    python3 linux_ipa_build.py project --dir <导出工程> --out App.ipa --use-project-pch

环境变量：
    IOS_SDK (默认 /opt/ios-sdk/sdks/iPhoneOS16.5.sdk)
    CLANG   (默认 clang，需 >= 18)
    LD64    (默认 /usr/lib/llvm-18/bin/ld64.lld)
    IPHONEOS_DEPLOYMENT_TARGET (默认 12.0)
    IOS_SDK_VERSION (默认 16.5)
    UNITY_VERSION_PATCH (默认 7；2022.3.62f1 用 62)

实测基线：Ubuntu 22.04 / clang 18.1.3 / LLVM 18 ld64.lld / iPhoneOS16.5.sdk
"""
import argparse
import glob
import hashlib
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SDK = Path(os.environ.get("IOS_SDK", "/opt/ios-sdk/sdks/iPhoneOS16.5.sdk"))
CLANG = os.environ.get("CLANG", "clang")
LD64 = os.environ.get("LD64", "/usr/lib/llvm-18/bin/ld64.lld")
MIN_IOS = os.environ.get("IPHONEOS_DEPLOYMENT_TARGET", "12.0")
SDK_VER = os.environ.get("IOS_SDK_VERSION", "16.5")
FW_DIR = SDK / "System/Library/Frameworks"
USR_LIB = SDK / "usr/lib"
USR_INC = SDK / "usr/include"

DEFAULT_FRAMEWORKS = [
    "Foundation", "UIKit", "CoreGraphics", "QuartzCore", "CoreText",
    "CoreMedia", "CoreVideo", "AVFoundation", "AudioToolbox", "Metal",
    "OpenGLES", "Security", "SystemConfiguration", "CoreLocation",
    "CoreMotion", "CFNetwork", "MediaToolbox", "AVKit",
]
DEFAULT_LIBS = ["objc", "System", "c++", "z", "iconv", "sqlite3"]

# 入口 / 生命周期符号绝不可用空桩顶替：链接会"成功"，但 App 启动即 return 0 秒退。
CRITICAL_SYMBOLS = {"_main", "main", "_UIApplicationMain", "_NSApplicationMain"}


def log(tag, msg):
    print("[%s] %s" % (tag, msg), flush=True)


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        log("ERR", "命令失败: " + " ".join(str(c) for c in cmd))
        log("ERR", (r.stdout or "")[-1200:])
        log("ERR", (r.stderr or "")[-2500:])
        raise SystemExit(r.returncode)
    return r


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1) pbxproj：OpenStep plist，正则抽取足够用；必须展开 $(VAR)
# ---------------------------------------------------------------------------
def parse_pbxproj(path):
    txt = Path(path).read_text(errors="ignore")

    def grab(key):
        m = re.search(r"\b%s\s*=\s*([^;]+);" % re.escape(key), txt)
        return m.group(1).strip().strip('"') if m else None

    def resolve(v, depth=0):
        if not v or depth > 4 or "$(" not in v:
            return v
        return re.sub(r"\$\(([A-Za-z0-9_]+)\)",
                      lambda m: resolve(grab(m.group(1)), depth + 1) or m.group(0), v)

    product = resolve(grab("PRODUCT_NAME")) or "UnityLinuxApp"
    settings = {
        "deployment_target": grab("IPHONEOS_DEPLOYMENT_TARGET") or MIN_IOS,
        "archs": grab("ARCHS") or "arm64",
        "bundle_id": resolve(grab("PRODUCT_BUNDLE_IDENTIFIER")) or "com.example.unitylinuxapp",
        "arc": (grab("CLANG_ENABLE_OBJC_ARC") or "YES").upper() == "YES",
        "product_name": product,
        "executable": resolve(grab("EXECUTABLE_NAME")) or product,
    }
    fws = set(re.findall(r"([A-Za-z0-9_+]+\.framework)\s+in Frameworks", txt))
    fws |= set(re.findall(r"name\s*=\s*([A-Za-z0-9_+]+\.framework);", txt))
    frameworks = sorted({f[:-10] for f in fws})
    return {"settings": settings, "frameworks": frameworks}


# ---------------------------------------------------------------------------
# 2) clang 交叉编译
# ---------------------------------------------------------------------------
def lang_for(path):
    return {".mm": "objective-c++", ".m": "objective-c",
            ".cpp": "c++", ".cc": "c++", ".c": "c"}.get(Path(path).suffix.lower())


def compile_one(src, objdir, includes, defs, arc=True, pch=None, opt="-O2",
                allow_fail=False):
    lang = lang_for(src)
    if not lang:
        return None
    obj = objdir / (re.sub(r"[^A-Za-z0-9_]", "_", str(src)) + ".o")
    obj.parent.mkdir(parents=True, exist_ok=True)
    cmd = [CLANG, "-target", "arm64-apple-ios%s" % MIN_IOS,
           "-isysroot", str(SDK)]
    if lang.startswith("objective-c"):
        cmd += ["-fblocks", "-fobjc-runtime=ios-%s" % MIN_IOS]
        if arc:
            cmd.append("-fobjc-arc")
    if pch:
        cmd += ["-include", str(pch)]
    for inc in includes:
        cmd += ["-I", str(inc)]
    for d in defs:
        cmd += ["-D", d]
    cmd += ["-F" + str(FW_DIR), "-I" + str(USR_INC), opt,
            "-Wall", "-Wno-everything", "-x", lang, "-c", str(src), "-o", str(obj)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        obj.unlink(missing_ok=True)
        if allow_fail:
            return None
        log("ERR", "编译失败 %s\n%s" % (src, r.stderr[-1800:]))
        raise SystemExit(1)
    return obj


# ---------------------------------------------------------------------------
# 3) 链接：.o + .a + frameworks → arm64 Mach-O（ld64.lld 自带 ad-hoc 签名）
# ---------------------------------------------------------------------------
def make_stub(symbols, stubdir):
    """为平台辅助符号生成 arm64 汇编桩；关键入口符号由 link() 提前拒绝。"""
    stubdir.mkdir(parents=True, exist_ok=True)
    s = stubdir / "stubs.s"
    lines = [".text", ".align 4"]
    for sym in symbols:
        sym = sym.strip().rstrip(",")
        if not sym or not re.match(r"^[A-Za-z0-9_.$]+$", sym):
            continue
        val = 1 if "isPlatformVersionAtLeast" in sym else 0
        lines += [".globl %s" % sym, "%s:" % sym,
                  "    mov x0, #%d" % val, "    ret", ""]
    s.write_text("\n".join(lines) + "\n")
    o = stubdir / "stubs.o"
    run([CLANG, "-target", "arm64-apple-ios%s" % MIN_IOS, "-isysroot", str(SDK),
         "-c", str(s), "-o", str(o)])
    return o


def link(objs, out_bin, extra_libs, frameworks, work):
    base = [LD64, "-arch", "arm64", "-platform_version", "ios", MIN_IOS, SDK_VER,
            "-syslibroot", str(SDK), "-o", str(out_bin)]
    base += [str(o) for o in objs] + [str(a) for a in extra_libs]
    base += ["-L" + str(USR_LIB), "-F" + str(FW_DIR)]
    for fw in frameworks:
        base += ["-framework", fw]
    for lib in DEFAULT_LIBS:
        base += ["-l" + lib]
    base += ["-adhoc_codesign", "-pie", "--error-limit=0"]

    stubs = []
    for rnd in range(1, 4):
        r = subprocess.run(base + [str(s) for s in stubs],
                           capture_output=True, text=True)
        if r.returncode == 0:
            if stubs:
                log("STUB", "补 %d 组平台桩后链接成功" % len(stubs))
            return out_bin
        syms = sorted({x.rstrip(",") for x in
                       re.findall(r"undefined symbol:\s+(\S+)", r.stderr)})
        fatal = [s for s in syms if s in CRITICAL_SYMBOLS]
        if fatal:
            log("ERR", "关键入口符号未定义，拒绝空桩顶替: " + ", ".join(fatal))
            log("ERR", "常见于 main.mm / static_main.mm 编译失败被跳过；"
                       "应修复头文件使其真正编译，而不是补桩。")
            raise SystemExit(1)
        if not syms or rnd == 3:
            log("ERR", "链接失败:\n" + r.stderr[:2500])
            raise SystemExit(1)
        log("STUB", "第 %d 轮 %d 个 undefined: %s" %
            (rnd, len(syms), ", ".join(syms[:5])))
        stubs.append(make_stub(syms, work / ("stubs_round%d" % rnd)))
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 4) Mach-O 与入口校验（出包门禁）
# ---------------------------------------------------------------------------
def verify_macho(path):
    d = Path(path).read_bytes()
    magic, cpu, sub, ftype, ncmds = struct.unpack_from("<IiiII", d, 0)
    off, lcs = 32, []
    for _ in range(ncmds):
        cmd, sz = struct.unpack_from("<II", d, off)
        lcs.append(cmd)
        off += sz
    return {"size": len(d), "magic": hex(magic), "cpu": hex(cpu & 0xFFFFFFFF),
            "is_arm64": (cpu & 0xFFFFFFFF) == 0x0100000C,
            "is_execute": ftype == 2,
            "has_code_signature": 0x1D in lcs,
            "sha256": hashlib.sha256(d).hexdigest()[:16]}


def verify_entrypoint(path):
    """LC_MAIN 入口不得是 `mov x0,#0; ret` 空桩，且函数体内必须有 bl 调用。"""
    d = Path(path).read_bytes()
    ncmds = struct.unpack_from("<I", d, 16)[0]
    off, entry = 32, None
    for _ in range(ncmds):
        cmd, sz = struct.unpack_from("<II", d, off)
        if cmd == 0x80000028:  # LC_MAIN
            entry = struct.unpack_from("<QQ", d, off + 8)[0]
        off += sz
    if entry is None:
        return {"ok": False, "reason": "无 LC_MAIN"}
    code = d[entry:entry + 96]
    words = list(struct.unpack("<%dI" % (len(code) // 4),
                               code[:len(code) // 4 * 4]))
    stub = (len(words) >= 2 and words[0] in (0x52800000, 0xD2800000)
            and words[1] == 0xD65F03C0)
    immediate_ret = bool(words) and words[0] == 0xD65F03C0
    bls = sum(1 for w in words if (w & 0xFC000000) == 0x94000000)
    return {"ok": (not stub and not immediate_ret and bls >= 1),
            "entryoff": hex(entry), "first": [hex(w) for w in words[:4]],
            "bl_count": bls, "stub": stub}


# ---------------------------------------------------------------------------
# 5) .app / Info.plist / IPA
# ---------------------------------------------------------------------------
def build_info_plist(template, settings, extra=None):
    pl = {}
    if template and Path(template).exists():
        try:
            pl = plistlib.loads(Path(template).read_bytes())
        except Exception:
            pl = {}

    def sub(v):
        if isinstance(v, str):
            return (v.replace("${EXECUTABLE_NAME}", settings["executable"])
                     .replace("${PRODUCT_BUNDLE_IDENTIFIER}", settings["bundle_id"])
                     .replace("${PRODUCT_NAME}", settings["product_name"]))
        return v

    pl = {k: sub(v) for k, v in pl.items()}
    pl.update({
        "CFBundleExecutable": settings["executable"],
        "CFBundleIdentifier": settings["bundle_id"],
        "CFBundleName": settings["product_name"],
        "CFBundleDisplayName": settings["product_name"],
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "1.0",
        "MinimumOSVersion": settings["deployment_target"],
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "LSRequiresIPhoneOS": True,
        "UIRequiredDeviceCapabilities": ["arm64"],
        "UILaunchStoryboardName": "LaunchScreen",
    })
    if extra:
        pl.update(extra)
    return pl


def assemble_app(binary, app_dir, plist, data_dir):
    app = Path(app_dir)
    if app.exists():
        shutil.rmtree(app)
    app.mkdir(parents=True)
    # 二进制保持 ld64.lld 刚做完 ad-hoc 签名的原样，绝不追加/改写任何字节，
    # 否则会破坏 CodeDirectory 的哈希覆盖范围。
    shutil.copy2(binary, app / plist["CFBundleExecutable"])
    plist = dict(plist)
    plist["LXCBuildTool"] = "linux_ipa_build.py"
    (app / "Info.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
    if data_dir and Path(data_dir).exists():
        shutil.copytree(data_dir, app / "Data", dirs_exist_ok=True)
    (app / "LaunchScreen.storyboard").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<document type="com.apple.InterfaceBuilder3.CocoaTouch.Storyboard.XIB" '
        'version="3.0" toolsVersion="1" targetRuntime="iOS.CocoaTouch" '
        'initialViewController="X"><scenes/></document>')
    return app


def pack_ipa(app_dir, ipa_path):
    # zip 在临时目录下执行，输出路径必须先转成绝对路径，否则相对路径会被解析到临时目录里
    ipa = Path(ipa_path).resolve()
    ipa.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        payload = Path(td) / "Payload"
        payload.mkdir()
        shutil.copytree(app_dir, payload / Path(app_dir).name)
        if ipa.exists():
            ipa.unlink()
        run(["zip", "-q", "-r", "-X", str(ipa), "Payload"], cwd=td)
    return ipa


def verify_ipa(ipa_path):
    with zipfile.ZipFile(ipa_path) as z:
        names = z.namelist()
    return {"size": Path(ipa_path).stat().st_size,
            "has_payload": any(n.startswith("Payload/") for n in names),
            "has_info_plist": any(n.endswith("Info.plist") for n in names),
            "has_metadata": any("global-metadata.dat" in n for n in names),
            "entries": len(names)}


# ---------------------------------------------------------------------------
# 6) 关键适配：framework 头映射 / 静态 main / 宏 / include 路径
# ---------------------------------------------------------------------------
def fw_header_shim(proj, work):
    """Unity 2022.3.62f1+ 把公开头改成 <UnityFramework/X.h>；Linux clang 没有
    Xcode 的 framework header map，需生成一层绝对路径转发头。"""
    shim = work / "fwheaders" / "UnityFramework"
    shim.mkdir(parents=True, exist_ok=True)
    refs = set()
    for root in (proj / "Classes", proj / "UnityFramework", proj / "MainApp"):
        for pat in ("*.h", "*.m", "*.mm"):
            for hp in glob.glob(str(root / "**" / pat), recursive=True):
                try:
                    txt = Path(hp).read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                refs.update(re.findall(
                    r"<\s*UnityFramework/([A-Za-z0-9_./-]+\.h)\s*>", txt))
    made = []
    for name in sorted(refs):
        rel = Path(name)
        if len(rel.parts) != 1:
            continue
        dst = shim / rel.name
        for cand in (proj / "Classes" / rel.name, proj / "UnityFramework" / rel.name):
            if cand.exists():
                dst.write_text('#include "%s"\n' % cand)
                made.append(rel.name + "=ok")
                break
        else:
            dst.write_text("/* %s: Unity template placeholder */\n" % rel.name)
            made.append(rel.name + "=stub")
    log("HEADER", "framework 头映射: " + (", ".join(made) if made else "(无需)"))
    return work / "fwheaders"


STATIC_MAIN = """#include <UIKit/UIKit.h>
#include <mach-o/ldsyms.h>
#include "UnityFramework/UnityFramework.h"
int main(int argc, char* argv[]) {
  @autoreleasepool {
    id ufw = [UnityFramework getInstance];
    [ufw setExecuteHeader: &_mh_execute_header];
    [ufw runUIApplicationMainWithArgc: argc argv: argv];
  }
  return 0;
}
"""


def build_defs():
    # 前三项来自 project.pbxproj 的 OTHER_CFLAGS，缺一个 UnityTrampolineConfigure.h 直接 #error
    patch = os.environ.get("UNITY_VERSION_PATCH", "7")
    return ["UNITY_IOS=1", "IL2CPP_TARGET_ARM64=1",
            "UNITY_VERSION_VER=2022", "UNITY_VERSION_MAJ=3",
            "UNITY_VERSION_MIN=%s" % patch,
            "BASELIB_INLINE_NAMESPACE=il2cpp_baselib",  # 消除 ReentrantLock 二义性
            "RUNTIME_IL2CPP=1"]  # 否则 g_CodegenRegistration 被 #if 屏蔽


def build_includes(proj, shim_root):
    icpp = proj / "Il2CppOutputProject/IL2CPP"
    inc = [shim_root, proj / "Classes", proj / "Classes/UI", proj / "Classes/Unity",
           proj / "Classes/PluginBase", proj / "Classes/Native",
           proj / "Libraries", proj / "Libraries/bdwgc/include", proj,
           proj / "Il2CppOutputProject/Source/il2cppOutput",
           icpp / "libil2cpp", icpp / "libil2cpp/pch",
           icpp / "external/baselib/Include",
           icpp / "external/baselib/Platforms/IOS/Include",
           icpp / "external/xxHash", icpp / "libmono",
           proj / "Libraries/external/baselib/Platforms/IOS/Include"]
    return inc


# ---------------------------------------------------------------------------
# 7) 主流程
# ---------------------------------------------------------------------------
def auto_workers():
    """按可用内存与 CPU 自适应并发，避免 3GB 级沙盒并行编译 OOM。"""
    cpu = min(16, os.cpu_count() or 8)
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                gb = int(line.split()[1]) / 1048576.0
                return max(1, min(cpu, int(gb / 1.2) or 1))
    except Exception:
        pass
    return cpu


def cmd_project(args):
    proj = Path(args.dir).resolve()
    pbx = proj / "Unity-iPhone.xcodeproj/project.pbxproj"
    if not pbx.exists():
        log("ERR", "找不到 " + str(pbx)); raise SystemExit(2)
    info = parse_pbxproj(pbx)
    s = info["settings"]
    frameworks = info["frameworks"] or DEFAULT_FRAMEWORKS
    log("PARSE", "archs=%s target=%s frameworks=%d product=%s" %
        (s["archs"], s["deployment_target"], len(frameworks), s["product_name"]))

    # 源码：Trampoline（Classes/ + Libraries/）+ il2cpp 生成的游戏 C++
    # 注意：Il2CppOutputProject/IL2CPP/ 下的运行时源码已预编译进 libil2cpp.a，再编会重复定义
    srcs = []
    for pat in ("*.mm", "*.m", "*.cpp", "*.cc", "*.c"):
        srcs += glob.glob(str(proj / "Classes/**" / pat), recursive=True)
        srcs += glob.glob(str(proj / "Libraries/**" / pat), recursive=True)
    srcs = sorted({x for x in srcs if "Tests" not in x})
    mainapp = sorted(glob.glob(str(proj / "MainApp/**" / "*.mm"), recursive=True))
    srcs = sorted(set(srcs) | {x for x in mainapp
                               if Path(x).name != "main.mm" and "Tests" not in x})
    il2cpp_srcs = []
    for pat in ("*.cpp", "*.cc", "*.c"):
        il2cpp_srcs += glob.glob(
            str(proj / "Il2CppOutputProject/Source/**" / pat), recursive=True)
    il2cpp_srcs = sorted(set(il2cpp_srcs))
    libs = sorted(glob.glob(str(proj / "Libraries/**" / "*.a"), recursive=True))
    log("SRC", "Trampoline %d + il2cpp %d, 静态库 %d" %
        (len(srcs), len(il2cpp_srcs), len(libs)))
    if not il2cpp_srcs and not any("libil2cpp" in x or "GameAssembly" in x for x in libs):
        log("ERR", "未检测到 il2cpp 产物：这不是 Unity 授权导出的完整工程。")
        if not args.force:
            raise SystemExit(3)

    work = Path(args.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    shim_root = fw_header_shim(proj, work)
    includes = build_includes(proj, shim_root)
    defs = build_defs()

    # ObjC 注入 PCH；C/C++ 不注入（生成代码自带 pch-cpp.hpp）
    project_pch = proj / "Classes/Prefix.pch"
    if args.use_project_pch and project_pch.exists():
        pch = project_pch
    else:
        pch = work / "min.pch"
        pch.write_text("#ifdef __OBJC__\n#import <Foundation/Foundation.h>\n"
                       "#import <UIKit/UIKit.h>\n#endif\n")
    jobs = []
    for x in srcs:
        lang = lang_for(x)
        jobs.append((Path(x), pch if lang and lang.startswith("objective-c") else None, "-O2"))
    for x in il2cpp_srcs:
        jobs.append((Path(x), None, "-O0"))  # 生成代码量大，-O0 + 并行
    if mainapp:
        sm = work / "static_main.mm"
        sm.write_text(STATIC_MAIN)
        jobs.append((sm, pch, "-O2"))

    objs, failed = [], []
    workers = (getattr(args, "workers", 0)
               or int(os.environ.get("BUILD_WORKERS", "0"))
               or auto_workers())

    def job(j):
        src, pp, opt = j
        return src, compile_one(src, work / "obj", includes, defs, s["arc"],
                                pch=pp, opt=opt, allow_fail=args.keep_going)

    log("1/5", "clang 并行(%d)编译 %d 个源码" % (workers, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (src, o) in enumerate(ex.map(job, jobs)):
            (objs if o else failed).append(o if o else str(src))
            if (i + 1) % 20 == 0:
                log(" ", "%d/%d" % (i + 1, len(jobs)))
    if failed:
        critical = [x for x in failed if Path(x).name in ("main.mm", "static_main.mm")]
        if critical:
            log("ERR", "入口源码编译失败，拒绝继续（否则 _main 会被空桩顶替）: "
                + ", ".join(critical))
            raise SystemExit(1)
        log("WARN", "%d 个源码编译失败并跳过: %s" %
            (len(failed), ", ".join(Path(x).name for x in failed[:8])))

    log("2/5", "ld64.lld 链接 %d 个 .o + %d 个 .a" % (len(objs), len(libs)))
    binp = work / s["executable"]
    link(objs, binp, libs, frameworks, work)
    mi = verify_macho(binp)
    log("MACHO", "arm64=%s signed=%s size=%d" %
        (mi["is_arm64"], mi["has_code_signature"], mi["size"]))
    if not (mi["is_arm64"] and mi["is_execute"] and mi["has_code_signature"]):
        log("ERR", "Mach-O 校验未通过"); raise SystemExit(1)
    ei = verify_entrypoint(binp)
    log("ENTRY", "LC_MAIN=%s first=%s bl=%d 空桩=%s ok=%s" %
        (ei.get("entryoff"), ei.get("first"), ei.get("bl_count", 0),
         ei.get("stub"), ei["ok"]))
    if not ei["ok"]:
        log("ERR", "入口为空桩或无调用，拒绝产出 IPA"); raise SystemExit(1)

    log("3/5", "组装 .app")
    pl = build_info_plist(proj / "Info.plist", s)
    app = assemble_app(binp, work / ("Payload/%s.app" % s["product_name"]),
                       pl, proj / "Data")
    log("4/5", "打包 IPA")
    ipa = pack_ipa(app, args.out)
    log("5/5", "校验 " + str(verify_ipa(ipa)))
    return ipa


# ---------------------------------------------------------------------------
# 8) smoke：不依赖 Unity，最小 UIKit 工程端到端自检
# ---------------------------------------------------------------------------
SMOKE_MAIN = r'''
#import <UIKit/UIKit.h>
#import <Foundation/Foundation.h>
@interface AppDelegate : UIResponder <UIApplicationDelegate>
@property (strong, nonatomic) UIWindow *window;
@end
@implementation AppDelegate
- (BOOL)application:(UIApplication *)application
    didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    self.window = [[UIWindow alloc] initWithFrame:[[UIScreen mainScreen] bounds]];
    self.window.backgroundColor = [UIColor colorWithRed:0.10 green:0.36 blue:0.47 alpha:1.0];
    UIViewController *vc = [[UIViewController alloc] init];
    UILabel *label = [[UILabel alloc] initWithFrame:CGRectMake(20, 120, 340, 160)];
    label.text = @"Unity Linux Build";
    label.textColor = [UIColor whiteColor];
    label.textAlignment = NSTextAlignmentCenter;
    label.font = [UIFont boldSystemFontOfSize:26];
    [vc.view addSubview:label];
    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];
    return YES;
}
@end
int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil,
                                 NSStringFromClass([AppDelegate class]));
    }
}
'''


def cmd_smoke(args):
    for p in (SDK, Path(LD64)):
        if not p.exists():
            log("ERR", "缺工具: " + str(p)); raise SystemExit(2)
    log("TOOL", "SDK=%s clang=%s ld64=%s" % (SDK, CLANG, LD64))
    work = Path(args.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    (work / "src").mkdir(parents=True)
    (work / "obj").mkdir()
    main_m = work / "src/main.m"
    main_m.write_text(SMOKE_MAIN)
    settings = {"deployment_target": MIN_IOS, "bundle_id": args.bundle_id,
                "product_name": "UnityLinuxSmoke", "executable": "UnityLinuxSmoke"}
    o = compile_one(main_m, work / "obj", [work / "src"], [], True)
    binp = work / "UnityLinuxSmoke"
    link([o], binp, [], ["Foundation", "UIKit", "CoreGraphics", "QuartzCore"], work)
    mi = verify_macho(binp)
    log("MACHO", "arm64=%s exec=%s signed=%s" %
        (mi["is_arm64"], mi["is_execute"], mi["has_code_signature"]))
    ei = verify_entrypoint(binp)
    log("ENTRY", "bl=%d 空桩=%s ok=%s" % (ei.get("bl_count", 0), ei.get("stub"), ei["ok"]))
    assert mi["is_arm64"] and mi["is_execute"] and mi["has_code_signature"] and ei["ok"]
    pl = build_info_plist(None, settings)
    app = assemble_app(binp, work / "Payload/UnityLinuxSmoke.app", pl, None)
    ipa = pack_ipa(app, args.out)
    log("IPA", str(verify_ipa(ipa)))
    log("DONE", "工具链自检通过 → " + str(ipa))
    return ipa


def main():
    ap = argparse.ArgumentParser(description="Linux 自制 xcodebuild（通用版）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("smoke", help="最小 UIKit 工程端到端自检（不需要 Unity）")
    p.add_argument("--out", default="out/Smoke.ipa")
    p.add_argument("--work", default=".build_smoke")
    p.add_argument("--bundle-id", default="com.example.unitylinuxsmoke")
    p.set_defaults(func=cmd_smoke)
    p = sub.add_parser("parse", help="解析 project.pbxproj")
    p.add_argument("--pbxproj", required=True)
    p.set_defaults(func=lambda a: print(parse_pbxproj(a.pbxproj)))
    p = sub.add_parser("project", help="编译 Unity 导出的 Xcode 工程 → IPA")
    p.add_argument("--dir", required=True)
    p.add_argument("--out", default="out/App.ipa")
    p.add_argument("--work", default=".build_project")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep-going", action="store_true")
    p.add_argument("--use-project-pch", action="store_true")
    p.add_argument("--workers", type=int, default=0,
                   help="并行编译线程数；0/省略=按内存自适应，也可用 BUILD_WORKERS")
    p.set_defaults(func=cmd_project)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

<!--TOOLKIT_END-->

---

## 附录 B：实测数据速查

| 项 | 实测值 |
|---|---|
| 导出工程 | 640 MB；`BuildResult=Succeeded` `totalErrors=0` |
| 源码编译 | 116/116（41 Trampoline + 74 il2cpp 生成 + 1 静态 main），16 线程并行；Build 10 为 236 个 |
| 静态库 | 3 个：`libiPhone-lib.a` 347 MB / `libil2cpp.a` 68 MB / `baselib.a` 1.7 MB |
| frameworks | 19 个 |
| 链接产物 | 62.8 MB（空场景探针）～ 113~115 MB（完整游戏）arm64 `MH_EXECUTE` |
| IPA | 21 MB（空场景）～ 53 MB（完整游戏），ad-hoc 签名 |
| 导出耗时 | 增量 1m30s ~ 2m40s（有 `Library/Bee` 缓存）；换引擎首次全量重导入十几分钟 |
| 验证 | 空场景全链路通过；Build 9 **81/81**；Build 10 两版各 **70/70**；热修复专项 **30/30**；最终 IPA 各 **15/15** |
| 入口 | 坏包 `d2800000 d65f03c0`；好包 `a9bd57f6 a9014ff4 …` + ≥3 条 `bl` |

## 附录 C：Mach-O 常量速查

| 常量 | 值 | 含义 |
|---|---|---|
| `MH_MAGIC_64` | `0xfeedfacf`（字节 `cf fa ed fe`） | 64 位 Mach-O，小端 |
| `CPU_TYPE_ARM64` | `0x0100000c` | arm64 |
| `MH_EXECUTE` | `2` | 可执行文件 |
| `LC_CODE_SIGNATURE` | `0x1D` | 签名 load command |
| `LC_MAIN` | `0x80000028` | 入口（`entryoff` 在 +8，uint64） |
| `BL` 指令掩码 | `(w & 0xFC000000) == 0x94000000` | 函数调用 |
| `mov x0,#0` / `ret` | `0xD2800000` / `0xD65F03C0` | 空桩特征 |
| CodeDirectory SuperBlob | `0xfade0cc0` | ad-hoc 签名 blob |
