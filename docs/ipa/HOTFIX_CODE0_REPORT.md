# Build 10 热修复报告 — 启动即退出（App returned from its main function with code 0）

**日期**：2026-11-10
**影响包**：`RaftVitaBuild10_Unity2022_3_62.ipa`（Unity 2022.3.62f1 升级版）
**未受影响**：`RaftVitaBuild10.ipa`（Unity 2022.3.7f1 稳定基线）
**状态**：✅ 已修复并重新出包，两版入口均验证通过

---

## 一、报错与定位

用户侧载后报 `App returned from its main function with code 0.` —— 应用 `main()` **直接返回 0**，没有进入 `UIApplicationMain` 运行循环。

我上一轮把 2022.3.62f1 升级版标为「推荐侧载」，这个推荐是错的，根因就在该包里。从 IPA 中解出主可执行文件，解析 `LC_MAIN` 的 `entryoff` 并读取入口机器码：

| 包 | `LC_MAIN` | 入口前两条指令 | 判定 |
|---|---|---|---|
| `RaftVitaBuild10.ipa`（7f1） | `0x3c3ddc` | `a9bd57f6` `a9014ff4` = `stp x22,x21,[sp,#-0x30]!` … | 真实 `main` |
| `RaftVitaBuild10_Unity2022_3_62.ipa`（62f1，修复前） | `0x18bc2c0` | `d2800000` `d65f03c0` = **`mov x0,#0` / `ret`** | **空桩** |

反汇编对照：

```
坏包 _main:                          修复后 62f1 _main:
  +00  d2800000  mov x0, #0           +00  a9bd57f6  stp x22,x21,[sp,#-0x30]!
  +04  d65f03c0  ret                  +04  a9014ff4  stp x20,x19,[sp,#0x10]
                                       +08  a9027bfd  stp x29,x30,[sp,#0x20]
                                       +0c  910083fd  add  x29,sp,#0x20
                                       +18  bl  ...   （多条调用，进入 ObjC 运行时）
```

`mov x0,#0; ret` 正是 Linux 编译链路 `linux_xcodebuild.py::make_stub()` 为 undefined symbol 生成的 arm64 空桩（`mov x0, #0` / `ret`）。

---

## 二、完整因果链

1. **引擎升级引入头文件写法变化**：Unity 2022.3.62f1 把公开头改成 framework 风格尖括号导入：
   ```objc
   #import <UnityFramework/UnityAppController.h>   // 62f1
   ```
   而 7f1 是 `#import "UnityAppController.h"`（引号）。Xcode 有 framework header map 能解析；纯 Linux 的 clang 没有。

2. **两个关键源码编译失败**：`Classes/main.mm`（`@implementation UnityFramework` 实现）和脚本生成的 `static_main.mm`（真正定义 `int main`）都 `#include "UnityFramework/UnityFramework.h"`，进而触发上述尖括号导入，报
   ```
   fatal error: 'UnityFramework/UnityAppController.h' file not found
   ```

3. **`--keep-going` 静默跳过**：构建脚本把编译失败记为 `[WARN] 2 个源码编译失败并跳过：main.mm, static_main.mm...` 后继续，未终止。

4. **链接补空桩**：`_main` 变成 undefined symbol，`link()` 的桩补全循环为它生成 `mov x0,#0; ret`，日志仍打印「补 1 组平台桩后链接成功」、`arm64=True signed=True`、`BuildResult=Succeeded`。

5. **验证脚本漏检**：上一轮 70 项验证覆盖了结构、签名、包内标记、IL2CPP 调用链，但**没有检查入口机器码**，于是「链接成功」被误当成「可启动」。

> 结论：这是一次典型的**「链接成功 ≠ 可运行」**假阳性。`main.mm` 缺失还意味着 `UnityFramework` 类实现根本没进包，即便入口被修好，运行时 `[UnityFramework getInstance]` 也会失败 —— 必须让源码真正编译，不能只补一个能进 `UIApplicationMain` 的桩。

---

## 三、修复（`linux_xcodebuild/linux_xcodebuild.py`）

| # | 改动 | 作用 |
|---|---|---|
| 1 | 生成 **framework 头映射层** `work/fwheaders/UnityFramework/`，扫描导出工程里所有 `<UnityFramework/X.h>`，为 `Classes/X.h` / `UnityFramework/X.h` 生成绝对路径转发头，模板占位生成空头；并把它插入 `includes` 首位 | 让 62f1 的尖括号导入在 Linux 下可解析，`Classes/main.mm` 与 `static_main.mm` 都能编译 |
| 2 | 新增 `CRITICAL_SYMBOLS = {_main, main, _UIApplicationMain, _NSApplicationMain}`；`link()` 遇到这些符号 undefined 时**直接报错退出**，拒绝空桩 | 杜绝「入口被桩掉但链接成功」 |
| 3 | `main.mm` / `static_main.mm` 编译失败时**立即终止**，不再被 `--keep-going` 跳过 | 关键源码缺失不再静默 |
| 4 | 新增 `verify_entrypoint()`：解析 `LC_MAIN`，读取入口 96 字节，判定是否为 `mov x0,#0; ret` / 立即 `ret`，并要求函数体内至少一条 `bl`；打包前校验，不通过**拒绝产出 IPA** | 把「入口可用」变成硬性出包门禁 |

修复后 62f1 构建日志：
```
[HEADER] framework 头映射层: PublicHeader.h=stub, UnityAppController.h=ok, UnityFramework.h=ok
[STUB] 第 1 轮：4 个 undefined → GetReadableStackTrace..., GetStacktracetNativeOption..., GetiOSAppDocumentsDir..., __isPlatformVersionAtLeast   ← 已无 _main
[ENTRY] LC_MAIN=0x3da220 首指令=['0xa9bd57f6', ...] bl=4 空桩=False ok=True
```

---

## 四、验证结果

### 专项验证 `verify_hotfix.py`：**OK 30 / FAIL 0**
- 坏包对照：确为 `mov x0,#0; ret`，且 `runUIApplicationMainWithArgc` 选择器缺失（印证 `Classes/main.mm` 未编译）。
- 两个修复包：均存在 `_main` 符号、入口非桩、含 ≥4 条 `bl`、入口机器码与坏包不同、含 `runUIApplicationMainWithArgc` 与 `UIApplicationMain`。
- 构建日志：两版均有 `[HEADER]`、`[ENTRY] ok=True`、无 `[ERR]`、无 `main.mm` 跳过、未补 `_main` 桩、无 `error:`、`arm64=True signed=True`。

### 全套回归：**两版各 70/70 通过**
`verify_hotfix_7f1.py` / `verify_hotfix_62.py`（把打包日志指向热修复日志）均 `OK=70 FAIL=0`：IPA 结构与 ad-hoc 签名、包内中文串与功能标记、翻译表、7 按钮布局、IL2CPP 调用链、Build 9 特性不回退、Unity 导出日志全部照旧通过 —— 本次只改编译链路，**游戏功能零改动**。

| 包 | 引擎 | 大小 | 入口 |
|---|---|---|---|
| `RaftVitaBuild10_Unity2022_3_62.ipa` | 2022.3.62f1 | 52 MB | `0x1003da220`，真实 main |
| `RaftVitaBuild10.ipa` | 2022.3.7f1 | 51 MB | `0x1003c3ddc`，真实 main |

两个文件**已原地覆盖**，旧的损坏包已移到临时区备份（不在交付区）。

---

## 五、侧载建议

1. **先卸载设备上已安装的旧包**再装，避免 iOS 复用缓存的可执行文件 / 签名。
2. IPA 仍为 **ad-hoc 签名**，需 SideStore / AltStore 重签；重签会改写 `LC_CODE_SIGNATURE`，属正常。
3. 两个包现在都能正常进入 `UIApplicationMain`；**推荐继续用 2022.3.62f1 升级版**（引擎更新、导出 0 error / 0 warning）。若实机有异常，7f1 基线功能完全一致，可直接回退。
4. 进入游戏后请顺带确认：中文显示、语言切换与重启保留、PICK 拾取、云海与木筏起伏。

---

## 六、给后续构建的提示

以后若再看到 `[WARN] ... 编译失败并跳过` 或 `[STUB] ... _main`，构建现在会**直接失败而不是产出秒退包**。`verify_entrypoint()` 已作为出包门禁常驻；`CRITICAL_SYMBOLS` 可按需要扩充。升级 Unity 补丁版本时，记得 `UNITY_VERSION_PATCH=<补丁号>`，并检查新版本是否又改了 `UnityFramework.h` 的导入风格（头映射层会自动扫描生成，无需手工维护）。
