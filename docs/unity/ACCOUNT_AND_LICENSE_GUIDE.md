# Unity 账号注册与授权获取指引（v2）

> 针对本轮问题：**「只能在浏览器操作，如何取得授权」**
> 更新：2026-11-09。依据 Unity 官方现行文档 + 沙箱内 Unity 镜像实测。
>
> **结论先行**：Unity Personal 的授权**不是从网页下载一个文件**，而是「用 Unity ID 在线登录一次」（Named User Licensing）。你只需在浏览器里注册账号；**在线激活这一步可以交给 Linux 沙箱或云端 CI 代跑**，产出 `Unity_lic.ulf`。旧的「网页上传 `.alf` 换 `.ulf`」流程对 Personal 已被官方关闭，别在这条路上浪费时间。

---

## 一、2026 年现行机制（已核实，与旧资料有冲突）

| 事项 | 现状 |
|---|---|
| Personal 激活方式 | **只能 Named User Licensing（NUL）**：用 Unity ID 在线登录 Unity Hub，自动激活 |
| 序列号 | Personal **没有**序列号 |
| 手动/离线激活 | **Personal 不支持**，官方要求升级 Pro 才能离线激活 |
| 手动激活网页 | `https://license.unity3d.com/manual` 页面顶部原文：`Unity no longer supports manual activation of Personal licenses.` |
| 旧 GameCI 技巧 | 旧文档里「DevTools 删掉 `display:none` 露出 Personal 选项」已失效；新版 GameCI Activation 文档已删除该技巧，改为只推荐「Hub 激活后复制 `.ulf`」 |
| Hub 激活产物 | 会落盘 `Unity_lic.ulf`（纯文本 XML），可复制给 CI / 沙箱使用 |

> Unity 官方支持页原文：`Personal licenses can be activated only through Named User Licensing ... manual or offline activation isn't available either.`

### 一个重要的实测纠错

之前 v1 指南写「Unity 2020+ Personal 无法靠用户名+密码在无头环境自动激活」——**这只对 `Unity -batchmode -username/-password` 这条 Editor 命令行参数成立**，不代表没有别的路。

我在沙箱的 `unityci/editor:ubuntu-2022.3.7f1-ios-1` 镜像里实测：

```
/opt/unity/Editor/Data/Resources/Licensing/Client/Unity.Licensing.Client  版本 1.12.1+9338cad
```

其 `--help` 明确提供：

```
--activate-ulf   Acquire Unity ULF license file. Add --serial parameter to activate a PRO license
--username / --password   Must be used together
--accessToken            可替代用户名密码
--update-ulf / --return-ulf
```

不带凭据调用时的实测回显：

```
Usage: --activate-ulf should be used with --accessToken or --username and --password
```

也就是说：**Editor 自带的 Licensing Client 支持「用 Unity ID 账号密码在线换取 Personal `.ulf`」，全程无需 Hub、无需 GUI、无需浏览器手动激活门户**。这是本项目最短的授权路径。

---

## 二、所以「只能在浏览器操作」到底要做什么

一句话：**注册 Unity ID，然后把「在线登录」这件事外包给能跑命令的一方**。

你在浏览器里真正需要做的只有：

1. 打开 https://id.unity.com/ 注册并验证邮箱（免费 Personal 即可）。
2. 选择下面任一条路径完成在线激活。
3. 如遇到「新设备验证码」，去邮箱（浏览器里）查 6 位码。
4. 把生成的 `.ulf` 全文交给我（或告诉我文件路径）。

---

## 三、三条可执行路径

| 路径 | 你要在浏览器里做的事 | 凭据是否经过我 | 可靠性 | 适合 |
|---|---|---|---|---|
| **A 交给我激活（推荐）** | 注册账号 → 把邮箱+密码发我 → 可能收一次验证码 | 是 | 高（已实测命令） | 想最快闭环 |
| **B GitHub Actions** | 新建私有仓库 → 粘贴 workflow → 填 Secrets → 点 Run → 复制 `.ulf` | **否**（只进 GitHub 加密 Secret） | 中高 | 不想把密码给第三方 |
| **C 云桌面装 Unity Hub** | 浏览器登录一台云 Windows / 远程电脑 → Hub 登录 → 复制 `.ulf` | 否 | 最高（纯官方流程） | A/B 被验证码或 2FA 卡住时兜底 |
| D 网页手动激活 | 上传 `.alf` → 网页下载 `.ulf` | — | **仅 Pro/Enterprise** | 付费许可 |

---

## 四、路径 A：交给我，沙箱在线激活（最短）

### 已准备好的脚本

`/workspace/linux_xcodebuild/activate_unity_license.sh`

```bash
export UNITY_EMAIL="你的Unity ID邮箱"
export UNITY_PASSWORD="密码"
bash /workspace/linux_xcodebuild/activate_unity_license.sh
# → /workspace/unity_license/Unity_lic.ulf
```

脚本内部实际执行（等价于）：

```bash
docker run --rm -v "/workspace/unity_license:/out" \
  -e UNITY_EMAIL -e UNITY_PASSWORD \
  -e DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
  --entrypoint bash unityci/editor:ubuntu-2022.3.7f1-ios-1 -c '
    C=/opt/unity/Editor/Data/Resources/Licensing/Client/Unity.Licensing.Client
    "$C" --activate-ulf --username "$UNITY_EMAIL" --password "$UNITY_PASSWORD"
    cp "$(find / -name Unity_lic.ulf -type f | head -1)" /out/Unity_lic.ulf
  '
```

### 实测到的机器绑定信息

`Unity.Licensing.Client --showContext` 回显包含：

```
EnvironmentHostname = 33f6ee6f5ec1
Legacy.MachineBinding1 / 2 = ...
```

即 `.ulf` 与 **hostname + machine-id 哈希**关联。因此：

- `.ulf` 本身**不绑定操作系统、不绑定 Unity 版本**（GameCI 明确允许在 Windows 激活、Linux 使用）；
- 但换一台机器首次使用时，需要用 `UNITY_EMAIL` + `UNITY_PASSWORD` **重新在线激活一次**。这也是 GameCI 要求三个 Secret 都给的原因。

### 风险与建议

- Unity 对新设备登录可能发 6 位邮箱验证码。若激活失败，把邮件里的码给我，或改走路径 C。
- 若账号开了 2FA，命令行方式容易被卡，直接走路径 C。
- **安全建议**：注册一个专用于构建的 Unity 小号，别用主账号密码；`.ulf` 与账号绑定，不要提交公开仓库、不要外发；用完可改密码。

---

## 五、路径 B：全程浏览器点按钮，凭据不经过我

已准备好 workflow：`/workspace/unity_license.yml`

浏览器里的操作步骤：

1. 打开 https://github.com/ ，注册/登录，**新建一个 Private 仓库**（名字随意，如 `unity-license`）。
2. 在仓库网页点 `Add file → Create new file`，文件名填 `.github/workflows/activation.yml`，把 `unity_license.yml` 的内容整段粘贴进去，提交。
3. `Settings → Secrets and variables → Actions → New repository secret`，添加两个：
   - `UNITY_EMAIL` = Unity ID 邮箱
   - `UNITY_PASSWORD` = Unity ID 密码
4. 打开 `Actions` 标签 → 选中 `取得 Unity Personal 授权 (.ulf)` → 点 `Run workflow`。
5. 跑完后两种取回方式（手机浏览器也能用）：
   - 展开该 step 的日志，复制 `ULF-BASE64` / `ULF-TEXT` 段落里的文本；
   - 或下载 `Unity-license` artifact（zip）解压出 `Unity_lic.ulf`。
6. 把 `.ulf` 全文贴给我即可。

> workflow 用的就是上面实测过的同一个镜像 + `--activate-ulf`，只是把执行地点换成 GitHub 云端 runner。
> 镜像约 8.8 GB，首次拉取需几分钟。私有仓库日志仅你可见；**取回 `.ulf` 后建议删掉该次 workflow run**（日志里含许可全文与 base64）。

---

## 六、路径 C：官方 Hub 流程（兜底）

Unity 官方唯一「保证能处理验证码 / 2FA」的方式，是让 Unity Hub 用 Unity ID 在线登录一次。你只需要一台**能通过浏览器访问**的、能装 Hub 的机器：云 Windows 桌面、远程家里电脑、公司的虚拟机等都算（GitHub Codespaces 里跑 Linux Hub 需要 `xvfb-run`，较折腾，不推荐）。

1. 浏览器进入云桌面 → 打开 https://unity.com/download 下载安装 Unity Hub。
2. 用 Unity ID 登录（网页弹窗里完成验证码 / 2FA）。
3. `Preferences → Licenses → Add → Get a free personal license`，走完向导。
4. 复制 `.ulf` 全文：

| 系统 | 路径 |
|---|---|
| Windows | `C:\ProgramData\Unity\Unity_lic.ulf` |
| macOS | `/Library/Application Support/Unity/Unity_lic.ulf` |
| Linux | `~/.local/share/unity3d/Unity/Unity_lic.ulf`（**不是** `~/.config`） |

> GameCI 特别提醒：Hub 列表里显示 Personal **不代表 `.ulf` 已落盘**，必须真的点一次 `Add` 走完激活流程。

---

## 七、如果你用的是 Pro / Enterprise（付费）

网页手动激活**仍然完全可用**，这条才真正是「纯浏览器换文件」：

```
本地/沙箱生成 .alf  →  浏览器打开 https://license.unity3d.com/manual
→ 登录 Unity ID → 上传 .alf → 填序列号 → 下载 .ulf
```

生成 `.alf` 的命令（沙箱已具备）：

```bash
Unity -batchmode -nographics -quit -createManualActivationFile
# 或 Licensing Client: --generate-alf-request --activation-file-path ...
```

注意：Unity Hub 3.21.1 / Licensing Client ≥ 1.18.3 才完整支持新版手动激活；本镜像自带的是 1.12.1，仅用于在线 `--activate-ulf`，如需手动激活请按 Unity 支持页替换新版客户端。

---

## 八、拿到 `.ulf` 之后（我这边怎么接）

> ⚠️ **实测纠错（2026-11-09）**：该镜像 `Entrypoint=null`、`Cmd=["/bin/bash"]`，**没有** GameCI 的 license 自动注入脚本。只传 `UNITY_LICENSE` 环境变量**不会生效**，Unity 仍会报无授权。必须先把 `.ulf` 落盘到 `~/.local/share/unity3d/Unity/Unity_lic.ulf`，并因 `MachineBindings` 与 hostname 绑定而在新容器里用账号密码重新在线激活一次。

```bash
set -a; . /path/to/unity_creds.env; set +a   # UNITY_EMAIL / UNITY_PASSWORD

docker run --rm \
  -e UNITY_EMAIL -e UNITY_PASSWORD \
  -v /workspace/unity_license:/lic:ro \
  -v "$PWD/project:/project" -v "$PWD/out:/out" \
  --entrypoint bash unityci/editor:ubuntu-2022.3.7f1-ios-1 -c '
    export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
    mkdir -p /root/.local/share/unity3d/Unity
    cp /lic/Unity_lic.ulf /root/.local/share/unity3d/Unity/Unity_lic.ulf
    C=/opt/unity/Editor/Data/Resources/Licensing/Client/Unity.Licensing.Client
    "$C" --activate-ulf --username "$UNITY_EMAIL" --password "$UNITY_PASSWORD"
    /opt/unity/Editor/Unity -batchmode -nographics -quit \
      -projectPath /project -buildTarget iOS \
      -executeMethod Build.ExportIos -logFile /dev/stdout
  '
```

> 同一容器内重复启动可省略 `--activate-ulf`；换容器/换机器必须重新激活，或用 `--update-ulf`。

导出真实 iOS Xcode 工程后，交给已跑通的 `linux_xcodebuild` 原型：clang 交叉编译 → `ld64.lld` 链接 → 组装 `.app` → 自签 → `.ipa`。

---

## 九、常见坑速查（v2 更新）

| 坑 | 表现 | 处理 |
|---|---|---|
| 想在网页手动激活 Personal | 页面写着不再支持 / 只有 Pro 选项 | **放弃这条路**，改 A/B/C |
| 用 `Unity -username/-password` 激活 | Personal 激活失败 | 改用 Licensing Client `--activate-ulf` |
| 只登录 Hub 没点 Add | 找不到 `.ulf` | `Licenses → Add → Get a free personal license` 走完 |
| 在 `~/.config` 找 `.ulf` | 找不到 | 正确路径 `~/.local/share/unity3d/Unity/Unity_lic.ulf` |
| 换机器后 `.ulf` 无效 | `license already in use` / 校验失败 | 用 `UNITY_EMAIL`+`UNITY_PASSWORD` 重新在线激活，或 `--update-ulf` |
| 新设备验证码 / 2FA | 命令行激活卡住或报认证失败 | 走路径 C（Hub 网页登录） |
| 把 `.ulf` 提交到公开仓库 | 账号许可被滥用 | 只放私有 Secret，用完删 workflow run |

---

## 十、最短路径清单

1. 浏览器打开 https://id.unity.com/ 注册 Unity ID（免费 Personal）。
2. **最省事**：把邮箱+密码发我 → 我跑 `activate_unity_license.sh` → 几分钟后给你 `.ulf` 并继续出包。
   **或** 不想给密码：按第五节在浏览器里跑 GitHub Actions，把 `.ulf` 贴给我。
3. 遇到验证码把 6 位码给我；若开了 2FA，改走第六节云桌面 Hub。
4. 授权到位后，方案 C 的「Unity 导出真实 iOS 工程」这一步即可补上，端到端闭环。

---

## 十一、本轮执行结果（2026-11-09 实测，授权已到位）

用户提供 Unity ID 后，**无需验证码**，路径 A 一次跑通：

| 项 | 结果 |
|---|---|
| 在线激活 | `Successfully activated ULF license` / `ACTIVATE_RC=0` |
| 授权文件 | `/workspace/unity_license/Unity_lic.ulf`，2610 字节，合法 XML（含 `License`、`MachineBindings`、`StartDate`/`StopDate`、xmldsig 签名） |
| Entitlements | `License Type: ULF`；`com.unity.editor.platforms.ios` ✅、`com.unity.editor.legacy.team-license`、`com.unity.editor.time-limited-license` |
| Unity batchmode | `Pro License: NO`（Personal 生效）、`Successfully updated license`、`Successfully resolved entitlements` |
| iOS 平台 | `Targeting platform: iOS`、加载 `iOSSupport/UnityEditor.iOS.Extensions.dll` |
| 退出状态 | `Exiting batchmode successfully now!` / `UNITY_EXIT=0` |
| 原阻塞错误 | `No valid Unity Editor license found` **已消失** |

完整日志：`/workspace/unity_license/batchmode_verify.log`；执行报告：`/workspace/unity_license/ACTIVATION_REPORT.md`。

### 两点必须记住

- **`com.unity.editor.time-limited-license`**：Personal 是**限时许可**，长期不用或换机器后要用 `--update-ulf --username --password` 重新激活；批量构建前建议先跑一次。
- **`Native extension for iOS target not found`**：Linux 上缺少 Mac 侧原生扩展的正常警告，不影响 Editor 启动与 iOS 模块加载；但它提示 Unity 在 Linux 上能否完整 `BuildPlayer` 到 iOS 仍需单独验证。

### 剩余缺口（2026-11-09 更新）

授权与**整条出包链路均已打通**：`BuildPipeline.BuildPlayer(iOS)` 在 Linux 上直接成功导出完整 Xcode 工程，`linux_xcodebuild.py project` 对真实工程完成 116 个源码编译 → `ld64.lld` 链接 → ad-hoc 签名 → IPA，产出 `/workspace/RaftUnityLinux.ipa`（详见 `UNITY_LINUX_IOS_E2E_REPORT.md`）。

当前**唯一实质缺口**是工程内容本身：

1. 一个完整的 Unity 工程（场景 / 脚本 / 资源）。本轮用的是空场景探针；此前调研 `raft_vita` 整库克隆超时，仅取到 `Hook.cs` 样本，**尚无完整工程可导出**。
2. 许可维护：Personal 是限时许可，长期构建前用 `--update-ulf --username --password` 重新激活。
