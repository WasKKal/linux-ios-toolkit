# Dylib 编译打包指南（纯 Linux / 免 macOS）

用 `tools/build_dylib.py` 在 Linux 上直接调用 iOS SDK 编译、ad-hoc 签名、deb 打包 iOS 动态库。
无需 Mac、无需 Xcode、无需 Theos。

## 原理

iOS dylib 本质是 **Mach-O MH_DYLIB（filetype=6）arm64 文件**。Linux 上只要具备三样东西就能产出：

1. **clang**：以 `-target arm64-apple-ios<minver> -isysroot <iPhoneOS.sdk>` 交叉编译源码为 Mach-O `.o`
2. **ld64.lld**：LLVM 自带的 ld64 兼容链接器，`-dylib` 链接成动态库，`-adhoc_codesign` 就地写入 `LC_CODE_SIGNATURE`（0x1d）
3. **iOS SDK**：提供系统头与 syscall 接口

ad-hoc 签名不是「省略签名」，而是用全零的 CodeDirectory 满足 iOS 加载器对签名结构的校验——**越狱环境（MobileSubstrate / Substitute）可直接加载**。未签名或签名结构残缺会导致 `dlopen` 失败。

## 环境前置

```bash
# 自动探测（推荐）：source tools/env.sh 会导出 IOS_SDK / CLANG / LD64
source tools/env.sh

# 或手动指定
export IOS_SDK=/opt/ios-sdk/sdks/iPhoneOS16.5.sdk
export LD64=/usr/lib/llvm-18/bin/ld64.lld
export CLANG=clang
```

脚本会按以下顺序自动探测：`IOS_SDK` 环境变量 → `/opt/ios-sdk/sdks` → `/sandbox/workspace/sdk` → `/usr/local/ios-sdk` → `$PWD/sdk` → `/sdk`；ld64 取 `/usr/lib/llvm-*/bin/ld64.lld` 中版本号最高者。

## 端到端自检

```bash
python3 tools/build_dylib.py smoke
```

预期输出：

```
[SMOKE] 造临时源码，跑通 编译→签名→deb→校验 全链路
[DYLIB] .../libsmoke.dylib  arm64=True dylib=True signed=True size=32KB
[DEB]   .../smoke.deb
[SMOKE] ✓ dylib arm64 + ad-hoc 签名 + deb 打包 全部通过
```

## 编译你自己的 dylib

```bash
# 单个 C 源码
python3 tools/build_dylib.py build \
  --src src/hello.c --out libfoo.dylib --name foo

# 多文件（.c/.m/.mm/.cpp 混合）+ Objective-C 依赖 Foundation
python3 tools/build_dylib.py build \
  --src src/a.m src/b.mm src/c.cpp \
  --ldflag -framework --ldflag Foundation \
  --out libfoo.dylib --name foo --min-ios 12.0

# 顺带打 Theos 风格 deb
python3 tools/build_dylib.py build \
  --src src/*.m --out libfoo.dylib --name foo \
  --deb --bundle com.example.foo --min-ios 12.0
```

关键参数：

| 参数 | 说明 |
|---|---|
| `--src` | 源码文件或目录（目录会递归收集 `.c/.cc/.cpp/.cxx/.m/.mm`） |
| `--out` | 输出 dylib 路径 |
| `--name` | 库名，用于 `@rpath/lib<name>.dylib` install_name 与 deb 文件名 |
| `--min-ios` | 最低系统版本（默认 12.0） |
| `--ldflag` | 附加链接参数，可多次；框架写法是两个参数 `--ldflag -framework --ldflag Foundation` |
| `--no-sign` | 跳过 ad-hoc 签名（仅调试，产物无法在越狱机加载） |
| `--deb` / `--bundle` | 打 Theos deb / 指定包名 |

## 校验

```bash
python3 tools/build_dylib.py verify --file libfoo.dylib
# arm64=True dylib=True signed=True filetype=6
```

## deb 安装布局

```
DEBIAN/control
Library/MobileSubstrate/DynamicLibraries/libfoo.dylib
Library/MobileSubstrate/DynamicLibraries/foo.plist   # Filter 通配，按需改成目标 bundle
```

`foo.plist` 默认注入 `com.apple.springboard`，按你的目标 App 修改 `Bundles` 数组。

## 已知边界

- ad-hoc 签名只能用于**越狱设备**；非越狱机需要有效开发者证书 + 对应 entitlements，本工具不提供
- dylib 依赖的私有 framework 需自行在 `--ldflag` 指定
- C++ 异常/RTTI 默认开启；纯 C tweak 无需关心
- `dpkg-deb` 不存在时自动回退到 `ar` 手工组包，布局一致

## 实测记录

Ubuntu 22.04 / clang 18.1.3 / ld64.lld (LLVM 18) / iPhoneOS16.5.sdk，
`smoke` 与多文件编译均通过：`filetype=6`、`LC_CODE_SIGNATURE=1`、deb 可被 `dpkg-deb -c` 正常解包。
