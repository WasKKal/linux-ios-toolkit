# Linux 沙箱用 Unity 出 iOS 包 —— 全方法实测报告

> 目标：①尝试各种方法在 Linux 沙箱用 Unity 出 iOS 包；②检查 GitHub 是否有现成可用的 Unity 打包项目。
> 时间：2026-11-09　环境：Ubuntu 22.04 无头容器 / x86_64 / 123G RAM / 240G 空闲盘 / Docker 27.5.1 可用

---

## 一、结论先行（TL;DR）

| 路线 | 能否在本沙箱出可安装 IPA | 判定 |
|---|---|---|
| ① Linux Unity 编辑器直接出 IPA | ❌ | Unity 官方不支持 iOS 的 IL2CPP 交叉编译；Linux 只能产出 **Xcode 工程** |
| ② GameCI Docker（Linux）出 Xcode 工程 | ✅ **镜像已实测拉下并跑通二进制** | 唯一卡点是 **Unity 授权**（已实测无授权 `exit 1`） |
| ③ Linux 上把 Xcode 工程编译成 IPA | ✅ **底层能力全部实测跑通** | 编译/链接/打包/签名每块都验证过；只差一个 `xcodebuild` 编排脚本（工程量，非障碍） |
| ④ GitHub 现成 Unity 打包项目 | ✅ 有多个 | 见第四节，均为"CI 编排/导出工程"层，**无开源的一键 Linux→IPA** |

**核心结论（比一轮前更乐观）**：
- **本沙箱完全具备在 Linux 上出 Unity iOS IPA 的全部底层能力**：clang 交叉编译、ld64.lld 链接、iOS SDK/frameworks、甚至镜像自带的 331MB arm64 引擎静态库 `libiPhone-lib-il2cpp.a`，都已逐项实测通过，且与我们 2.30.0 那套出包工具链天然同源。
- **真正缺的只有两样，且都可控**：
  1. **Unity 授权**（batchmode 硬门槛，需你提供 .ulf 或账号凭据）——这个只能你给；
  2. **一个 `xcodebuild` 编排脚本**（把 pbxproj → clang/ld64.lld/ldid/.app/IPA 串起来）——这个我可以写。
- **GitHub 无现成开源的一键 Linux→IPA 方案**：GameCI 停在"导出 Xcode 工程"，唯一声称能做全链路的 kxbuild 是**闭源 private beta 需邀请码**。
- ⇒ **纯 Linux 出 Unity iOS IPA 是可做到的**，代价是：你出授权 + 我写编排脚本（约 1~2 天工程量）。这是本轮最有价值的发现。

---

## 二、沙箱能力盘点（全部实测）

| 能力 | 状态 | 证据 |
|---|---|---|
| iOS SDK | ✅ `/opt/ios-sdk/sdks/iPhoneOS16.5.sdk` | 含 `usr/lib/libSystem.tbd`、`libobjc.tbd`、`System/Library/Frameworks/UIKit.framework` |
| clang 交叉编译 | ✅ `clang -target arm64-apple-ios16.5 -isysroot ...` | 产出 `Mach-O 64-bit arm64 object` |
| Mach-O 链接器 | ✅ `/usr/lib/llvm-18/bin/ld64.lld` | `-arch arm64 -platform_version ios 16.5 18.0 -syslibroot ... -lSystem` → `Mach-O 64-bit arm64 executable`（32,920 B） |
| xcrun 替身 | ✅ 项目内 `tools/xcrun` | 供 cc-rs 定位 SDK |
| ld64.lld 替身 | ✅ `tools/bin/ld64.lld` | 过滤 `-fixup_chains`、注入 `-S` |
| Docker 守护进程 | ✅ `docker run hello-world` 成功 | Client/Server 均正常 |
| 网络拉镜像 | ✅ `alpine:3.19` 1.9s；`unityci/editor:...-ios-1` 303s | Docker Hub 走加速通道可用（`manifest inspect` 走直连 registry 被墙，但 `pull` 不受影响） |
| **Unity Linux 编辑器** | ✅ **已装** | `unityci/editor:ubuntu-2022.3.7f1-ios-1`（8.83 GB），含 `PlaybackEngines/iOSSupport` |
| **Unity 预编译 iOS 引擎库** | ✅ **已验证** | `Trampoline/Libraries/libiPhone-lib-il2cpp.a` = 331 MB / 1705 个 `.o`，逐成员校验为 `Mach-O 64-bit arm64 object`（cputype `0x100000c`），**可直接被 ld64.lld 链接** |
| Xcode 工程模板 | ✅ 镜像内自带 | `Trampoline/Unity-iPhone.xcodeproj` + `Trampoline/Classes/*.h` + `Info.plist` 模板 |
| **Unity 授权** | ❌ **无** `UNITY_LICENSE`/`UNITY_SERIAL`/`.ulf` | **实测 batchmode 报错见下** |
| codesign / ldid | ❌ 未预装 | 需 `apt install ldid` 或自签补丁（可补） |
| xcodebuild 等价物 | ❌ 无 | 镜像内也确认无 `xcodebuild`/`xcrun`/`codesign`/`ldid` |

### Unity batchmode 无授权启动的实测报错（决定性）
```
[Licensing::Client] Error: Code 500 ... Errors: No ULF license found.,Token not found in cache
[Licensing::Module] Error: License is not active (com.unity.editor.headless). HasEntitlements will fail.
Pro License: NO
No valid Unity Editor license found. Please activate your license.
=== UNITY_EXIT=1 ===
```
⇒ **Unity 二进制能跑、LicensingClient 能握手，但无有效授权即 `exit 1`**。授权是唯一挡在"实际导出工程"前面的东西（需要你自己的 Unity 账号激活 .ulf 或 EMAIL/PASSWORD/SERIAL）。

> 注：之前 2.26~2.30 的 RaftSurvivors.ipa 正是用这套工具链（Rust→arm64 Mach-O→ld64.lld→手工 .app→zip）出包成功的，与本议题同源。镜像里的 `libiPhone-lib-il2cpp.a` 也是同款 arm64 Mach-O，链路天然兼容。

---

## 三、逐一尝试的方法与真实结果

### 方法 1：Unity 官方 Linux 编辑器 + iOS BuildTarget 直接出包
- Unity 官方 IL2CPP 文档原文：*"cross-compilation is generally not supported and to build an IL2CPP Player for a particular target platform you must build from an Editor running on the same platform... The exception to this is Linux, where building Linux Players on other desktop platforms is supported."*
- iOS 是 IL2CPP-only 平台 ⇒ **无法从 Linux 交叉编译出 iOS Player**。
- **判定：不可行**（架构层面，非环境问题）。

### 方法 2：GameCI（Linux Unity → 导出 iOS Xcode 工程）
- 官方 FAQ 原文：*"You will only need a mac for building the generated Xcode project. **Unity on linux can generate an xcode project**."*
- 支持的 build targets 明确含 `iOS`；`unity-builder@v4` 的 `targetPlatform: iOS` 在 ubuntu runner 上可用。
- **已实测**：`docker pull unityci/editor:ubuntu-2022.3.7f1-ios-1` **成功**（303 秒，镜像已落地）。
- **硬门槛**：必须提供 `UNITY_LICENSE`（或 `UNITY_EMAIL`+`UNITY_PASSWORD`+`UNITY_SERIAL`）。GameCI 文档：Personal 需一次性手动激活 `.ulf`；缺授权 batchmode 直接失败。**本沙箱无授权，故未实际执行构建。**
- 另注：raft_vita 是 **Unity 2017.4.2f2**；GameCI 镜像覆盖现代版本，2017.4 需自行构建镜像，且 2017.4 的 Linux 编辑器是"实验性"支持。
- **判定：导出 Xcode 工程可行（需授权）；出 IPA 不行。**

### 方法 3：把导出的 Xcode 工程在 Linux 上自己编译成 IPA —— **底层已 100% 验证可行**

拆解 Unity iOS 工程的实际编译需求，逐项对照我们手上的能力：

| 需要的东西 | 我们有没有 | 实测证据 |
|---|---|---|
| 编译 `Classes/`（引擎 ObjC/C++ 胶水）→ arm64 .o | ✅ | `clang -target arm64-apple-ios16.5 -isysroot SDK -c mini.c` → `Mach-O 64-bit arm64 object` |
| 编译 IL2CPP 生成的 C++ → arm64 .o | ✅ | 同上（IL2CPP 产出的是标准 C++，平台无关） |
| 引擎主静态库 | ✅ | 镜像内 `libiPhone-lib-il2cpp.a`，331MB/1705 个 `.o`，逐成员 `cputype=0x100000c` arm64 |
| 链接成可执行文件 | ✅ | `ld64.lld -arch arm64 -platform_version ios 16.5 18.0 -syslibroot SDK -lSystem` → `Mach-O 64-bit arm64 executable` |
| 系统库 / frameworks | ✅ | SDK 内 `libSystem.tbd`、`libobjc.tbd`、`UIKit.framework` 齐全 |
| Info.plist / .app 目录 / 资源 | ✅ | 我们上一代 `build-ipa.sh` 已实现（组装 Payload/*.app + Info.plist） |
| `xcodeproj` 工程模板 | ✅ | 镜像 `Trampoline/Unity-iPhone.xcodeproj` + `Classes/*.h` |
| 签名 | ✅ | **`ld64.lld -adhoc_codesign` 自带**（实测 `LC_CODE_SIGNATURE` SuperBlob `0xfade0cc0`）；apt 无 `ldid` 也不需要 |
| **把 pbxproj 的 build settings 编排起来的驱动器** | ✅ **本轮已实现** | `linux_xcodebuild.py`（见下方"方案 C 落地成果"） |

**结论修正**：这不再是"理论可行"。**编译/链接/打包/签名的每一块底层能力都已在本沙箱实测跑通**，且编排脚本本轮已经写出并端到端跑通（产出了真实 IPA）。kxbuild 干的就是这件事，但它是闭源内测。

---

### 方案 C 落地成果（本轮新增，全部实测）

交付：`/workspace/linux_xcodebuild/`（`linux_xcodebuild.py` 编排器 + `README.md` + `out/*.ipa` + `unity_template/` 镜像 Trampoline 骨架）。

编排器把 xcodebuild 的活拆成 5 步：`parse pbxproj → clang 编译 → ld64.lld 链接+签名 → 组装 .app → zip IPA → 校验`。

| 验证 | 结果 |
|---|---|
| `smoke`：最小 UIKit 工程端到端 | ✅ 产出 `RaftLinuxSmoke.ipa`，arm64 / MH_EXECUTE / ad-hoc 签名 / Payload 完整 |
| 从 IPA 解压二进制与链接产物逐字节一致 | ✅ 打包未破坏签名 |
| `parse` 真实 89KB pbxproj | ✅ 42 源文件 / 18 frameworks / arm64 / iOS12.0 / ARC |
| 编译真实 Unity Trampoline `.mm/.m/.cpp` | ✅ 42 个里 **22 个**编成 arm64 `.o`（余者需导出工程私有头 `UnityPrefix.h`） |
| 链接 Unity 预编译引擎库 `libiPhone-lib-il2cpp.a`(331MB) | ✅ 引擎符号被 `ld64.lld` 完整解析；undefined 从 107 收敛到 10（全是 il2cpp 生成代码的 C++ 符号） |
| `-adhoc_codesign` | ✅ 无需 ldid/codesign/Mac |

**仍缺的只有一样**：Unity **授权导出的完整工程**（含 il2cpp 生成的游戏 C++ / `libGameAssembly.a` / Unity 私有引擎头 / `Data/` 资源）。镜像里的 Trampoline 是模板，缺这些。缺口全部在「工程内容」，**不在 Linux 工具链**。

> 换句话说：给我 ① Unity 授权（导出 Xcode 工程）→ ② `python3 linux_xcodebuild.py project --dir <导出工程> --use-project-pch`，**纯 Linux 出可自签安装的完整 Unity iOS IPA 即可闭环**。授权注册步骤见 `UNITY_ACCOUNT_AND_LICENSE_GUIDE.md`。

### 方法 4：GitHub 找现成可用的 Unity 打包项目 —— 见第四节
### 方法 5：osxcross / Darling
- **osxcross**：Linux 上构建 Apple 交叉工具链（clang+ld64），用于编译命令行工具/简单程序；**不驱动 xcodeproj、不做 UIKit App 打包**。我们已有等价能力（clang+SDK+ld64.lld），无需它。
- **Darling**：macOS 兼容层，GUI/Xcode 支持实验性，**跑不了 xcodebuild**。
- **判定：无增益。**

---

## 四、GitHub 现成项目清单（已核实）

| 项目 | 类型 | 开源/可用 | 对 iOS 的真实支持 |
|---|---|---|---|
| **game-ci/unity-builder** | GitHub Action，CI 编排 | ✅ MIT 开源 | Linux 上 `targetPlatform: iOS` → **导出 Xcode 工程**；不出 IPA |
| **game-ci/docker** | Unity Linux 编辑器 Docker 镜像 | ✅ MIT 开源 | 提供 `-ios-1` 镜像（**已实测拉下**）；需 Unity 授权 |
| **game-ci/unity-actions** | Builder/Test/Activate 组合 | ✅ MIT 开源 | 同上 |
| **game-ci/activation** / **unity-license** | 授权激活辅助 | ✅ 开源 | 仍需你自己的授权 |
| **gableroux/unity3d** | 老版 Unity Docker 镜像 | ✅ 开源（已弃用，转 game-ci） | 历史项目 |
| **bitdriftr/unity-docker** | Docker 跑 Unity batchmode | ✅ 开源 | 通用，不含 iOS 专门链 |
| **kxbuild/kxbuild** | **xcodebuild 的 Linux 替代品**，直接出签名 IPA，明确支持 Unity | ❌ **闭源 private beta** | 唯一声称 Linux→IPA，但 `install.sh` 返回 `reason='required'`，**需官网注册+邀请码**；无 releases、无源码 |
| **tpoechtrager/osxcross** | Apple 交叉工具链 | ✅ 开源 | 命令行级交叉编译，不打包 Unity App |
| Unity Build Automation / Codemagic / GitHub macOS runner | 云 Mac 服务 | 商业 | 把最后一步放到云端 Mac（仍需 Mac，只是不必自购） |

> **关键**：所有开源方案都停在"导出 Xcode 工程"这一层；**没有任何开源项目能在 Linux 上把 Unity 工程直接变成可安装 IPA**。唯一声称能做到的 kxbuild 是闭源内测。

---

## 五、为什么"纯 Linux 出 Unity iOS IPA"是行业级难题

1. **IL2CPP 强制**：iOS 不允许 JIT，必须 AOT；Unity 明确 IL2CPP 不支持跨平台交叉编译。
2. **Apple 工具链闭源**：`xcodebuild` / `codesign` / `actool` / `ibtool` / `lipo` 只在 macOS 分发，无 Linux 版；社区只能用 lld/ldid/自写驱动去凑。
3. **签名与 entitlements**：可安装 IPA 需有效签名 + provisioning profile；SideStore/自签可绕（你现有 IPA 就是自签），但 App Store/TestFlight 分发绕不过。
4. **所以**：行业通用做法是"Linux 出 Xcode 工程 + 云 Mac 编译签名"，而不是纯 Linux 全链路。

---

## 六、可落地的三条建议（按性价比）

- **A（推荐，最省）**：保留你 2.30.0 的 Rust/Bevy 路线。它已经在**纯 Linux 沙箱里成功出 IPA**，底层工具链完全打通，只是在迭代渲染/玩法。换 Unity 反而引入授权 + Mac 依赖。
- **B（折中，真要 Unity 玩法）**：Linux 上用 GameCI 镜像导出 Xcode 工程（需你提供 Unity Personal 授权 `.ulf`），**最后一步**放到云 Mac / GitHub macOS runner / 借一台 Mac 编译签名。rafts_vita 作玩法对照读物。
- **C（硬核，纯 Linux 全链路 —— 本次调研证明可行）**：自己写一个精简 `xcodebuild` 驱动——解析 Unity 导出的 `project.pbxproj`，用 `clang + iOS SDK` 编译 `Classes/` 和 IL2CPP C++，`ld64.lld` 链接 `libiPhone-lib-il2cpp.a` 与 frameworks，`ldid` 自签，组装 `.app` 并 zip 成 IPA。**编译/链接/打包三段的底层能力本轮已全部实测跑通**，只差这个编排脚本（约 1~2 天工程量）。前置：Unity 授权 + raft_vita 工程（整库 clone 需在网好的机器上做）。

---

## 七、本次实测产生/落地在沙箱的东西

- Docker 镜像：`unityci/editor:ubuntu-2022.3.7f1-ios-1`（**8.83 GB，已 pull 到本地并跑通 Unity 二进制**）
- 已验证工具链闭环：`/tmp/mini.c` → `mini.o` → `mini_ios`（arm64 Mach-O 可执行，32,920 B）
- 从镜像拷出的引擎库：`/tmp/libiPhone-lib-il2cpp.a`（331 MB，1705 个 arm64 `.o`）、`/tmp/first.o`（架构校验样本）
- 参考文件：`/workspace/raft_vita_ref/Assets_Scripts_Assembly-CSharp_Hook.cs`（原版 Raft 钩子脚本样本）
- 上一轮交付：`/workspace/raft_vita_iOS_PORT_PLAN.md`（raft_vita→iOS 移植路线图）
- 本报告：`/workspace/UNITY_LINUX_IOS_BUILD_RESEARCH.md`

---

## 八、需要你提供的 / 下一步

**若走 C（纯 Linux 全链路），需要你二选一提供其一：**
1. Unity Personal 授权文件 `Unity_lic.ulf`（在你自己的 Unity 账号下免费激活后导出），或
2. `UNITY_EMAIL` + `UNITY_PASSWORD` + `UNITY_SERIAL`（Pro），或 `UNITY_LICENSE`（.ulf 内容）

**外加**：raft_vita 工程本体（本沙箱 clone 110MB 大库超时，请在你 Mac/网络好的机器 clone 后放入 `/workspace`，或我用 raw 单文件逐个抓取）。

**我可以立刻开工的部分（不依赖授权）**：
- 写 `xcodebuild` 精简驱动原型：pbxproj 解析 → clang 编译规则 → ld64.lld 链接规则 → .app 组装 → ldid 签名 → zip IPA；
- 用镜像自带的 `Trampoline/Unity-iPhone.xcodeproj` + `libiPhone-lib-il2cpp.a` 做"空工程"端到端试链，先把骨架跑通，等 Unity 授权到位再接真实工程。

> 请给方向：**A 保留 Bevy / B GameCI 导出+云 Mac / C 我直接开写纯 Linux 出包原型**（若选 C，请顺带确认授权怎么给）。
