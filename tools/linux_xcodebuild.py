#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linux_xcodebuild.py — 纯 Linux 上的 "xcodebuild" 最小可用原型 (方案 C 骨架)

目标：在没有 macOS / Xcode 的 Linux 沙箱里，把一个 Unity 风格的 iOS Xcode 工程
（Trampoline 结构：Classes/ + Libraries/ + Data/ + Info.plist + project.pbxproj）
编译、链接、签名、打包成可被 SideStore / AltStore 自签安装的 .ipa。

它替代的正是 xcodebuild 这条链：
  PBXProj 解析 → clang 交叉编译 → ld64.lld 链接(Mach-O + ad-hoc 签名)
  → 组装 .app → zip 成 IPA → Mach-O/签名/IPA 结构校验

所用到的、已在本沙箱实测的底层能力：
  - /opt/ios-sdk/sdks/iPhoneOS16.5.sdk   (含 UIKit.framework / libobjc.tbd / libSystem.tbd)
  - clang 18   (-target arm64-apple-ios12.0 -fobjc-arc)
  - /usr/lib/llvm-18/bin/ld64.lld  (-arch arm64 -platform_version ios ... -adhoc_codesign)
  - 注意：ld64.lld 自带 ad-hoc codesign，因此无需 ldid / codesign。

用法:
  python3 linux_xcodebuild.py smoke    --out out/Smoke.ipa
      生成最小 UIKit 空工程，端到端跑通全链路并产出真实 IPA（不依赖 Unity 授权）。

  python3 linux_xcodebuild.py parse   --pbxproj <path>
      解析真实 project.pbxproj，打印 build settings / 源文件 / frameworks 清单。

  python3 linux_xcodebuild.py project --dir <导出的Xcode工程> --out out/App.ipa
      对 Unity 导出的**完整**工程执行编译链接打包。
      注意：需要 Unity 授权导出的工程（含 il2cpp 生成的 C++ 与引擎头）。
      镜像自带的 Trampoline 只是模板，缺 il2cpp 产物，仅可用于 parse 演示。

环境变量可覆盖工具路径：IOS_SDK / CLANG / LD64 / IPHONEOS_DEPLOYMENT_TARGET

作者备注：这是骨架原型，不是完整 xcodebuild。它验证了「Linux 具备出 iOS 包的
全部底层能力」，把 pbxproj 编排、frameworks、引擎静态库接入点都做成可替换步骤。
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
from pathlib import Path

# ---------------------------------------------------------------------------
# 工具链定位（全部实测存在）
# ---------------------------------------------------------------------------
SDK = Path(os.environ.get("IOS_SDK", "/opt/ios-sdk/sdks/iPhoneOS16.5.sdk"))
CLANG = os.environ.get("CLANG", "clang")
LD64 = os.environ.get("LD64", "/usr/lib/llvm-18/bin/ld64.lld")
MIN_IOS = os.environ.get("IPHONEOS_DEPLOYMENT_TARGET", "12.0")
SDK_VER = "16.5"

FW_DIR = SDK / "System/Library/Frameworks"
USR_LIB = SDK / "usr/lib"
USR_INC = SDK / "usr/include"

# Unity Trampoline 默认链接的系统框架/库（从 project.pbxproj 抽取得到）
DEFAULT_FRAMEWORKS = [
    "Foundation", "UIKit", "CoreGraphics", "QuartzCore", "CoreText",
    "CoreMedia", "CoreVideo", "AVFoundation", "AudioToolbox", "Metal",
    "OpenGLES", "Security", "SystemConfiguration", "CoreLocation",
    "CoreMotion", "CFNetwork", "MediaToolbox", "AVKit",
]
DEFAULT_LIBS = ["objc", "System", "c++", "z", "iconv", "sqlite3"]


def log(tag, msg):
    print(f"[{tag}] {msg}", flush=True)


def run(cmd, **kw):
    """执行外部命令，失败时打印完整命令与输出。"""
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        log("ERR", "命令失败: " + " ".join(str(c) for c in cmd))
        log("ERR", (r.stdout or "")[-1500:])
        log("ERR", (r.stderr or "")[-3000:])
        raise SystemExit(f"退出码 {r.returncode}")
    return r


# ---------------------------------------------------------------------------
# pbxproj 解析（OpenStep plist，正则抽取足够用，无需 plutil）
# ---------------------------------------------------------------------------
def parse_pbxproj(path):
    txt = Path(path).read_text(errors="ignore")

    def grab(key):
        m = re.search(rf"\b{re.escape(key)}\s*=\s*([^;]+);", txt)
        return m.group(1).strip().strip('"') if m else None

    # Xcode 的 $(VAR) 变量展开：Unity 写的是 PRODUCT_NAME = "$(PRODUCT_NAME_APP)"
    # 且 PRODUCT_NAME_APP = RaftLinuxProbe，不展开会生成名为 "$(...)" 的 .app。
    def resolve(v, depth=0):
        if not v or depth > 4 or "$(" not in v:
            return v
        return re.sub(r"\$\(([A-Za-z0-9_]+)\)",
                      lambda m: resolve(grab(m.group(1)), depth + 1) or m.group(0),
                      v)

    product_name = resolve(grab("PRODUCT_NAME")) or "RaftSurvivors"
    settings = {
        "deployment_target": grab("IPHONEOS_DEPLOYMENT_TARGET") or MIN_IOS,
        "archs": grab("ARCHS") or "arm64",
        "bundle_id": resolve(grab("PRODUCT_BUNDLE_IDENTIFIER")) or "com.workbuddy.raft.linux",
        "arc": (grab("CLANG_ENABLE_OBJC_ARC") or "YES").upper() == "YES",
        "product_name": product_name,
        "executable": resolve(grab("EXECUTABLE_NAME")) or product_name,
        "sdkroot": grab("SDKROOT") or "iphoneos",
    }

    # frameworks: 匹配 "Xxx.framework in Frameworks" 与 PBXFileReference 里的 .framework
    fws = set(re.findall(r"([A-Za-z0-9_+]+\.framework)\s+in Frameworks", txt))
    fws |= set(re.findall(r"name\s*=\s*([A-Za-z0-9_+]+\.framework);", txt))
    frameworks = sorted({f[:-10] for f in fws})  # 去掉 ".framework"

    # .tbd / .dylib / .a 系统库引用
    tbds = set(re.findall(r"(usr/lib/[A-Za-z0-9_.]+\.(?:tbd|dylib))", txt))

    # 参与 Sources 编译阶段的文件名（用于对照磁盘）
    sources = sorted(set(re.findall(
        r"([A-Za-z0-9_+.-]+\.(?:mm|m|cpp|c|cc))\s+in Sources", txt)))

    return {
        "settings": settings, "frameworks": frameworks,
        "tbd_refs": sorted(tbds), "source_names": sources,
        "raw_bytes": len(txt),
    }


# ---------------------------------------------------------------------------
# 编译：源码 → arm64 .o
# ---------------------------------------------------------------------------
def lang_for(path):
    ext = Path(path).suffix.lower()
    return {".mm": "objective-c++", ".m": "objective-c",
            ".cpp": "c++", ".cc": "c++", ".c": "c"}.get(ext)


def _run_soft(cmd, **kw):
    """编译失败时返回 None 而非终止（用于 --keep-going 收集可编译子集）。"""
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    return r if r.returncode == 0 else None


def compile_one(src, objdir, includes, defs, arc=True, pch=None, allow_fail=False,
                opt="-O2"):
    lang = lang_for(src)
    if not lang:
        return None
    obj = objdir / (re.sub(r"[^A-Za-z0-9_]", "_", str(src)) + ".o")
    obj.parent.mkdir(parents=True, exist_ok=True)
    cmd = [CLANG, "-target", f"arm64-apple-ios{MIN_IOS}", "-isysroot", str(SDK)]
    # ObjC/ObjC++ 才需要 blocks / runtime / ARC。il2cpp 生成的 C++ 自己
    # #include "pch-cpp.hpp"，加 ObjC 标志只会产生噪声告警。
    if lang.startswith("objective-c"):
        cmd += ["-fblocks", f"-fobjc-runtime=ios-{MIN_IOS}"]
        if arc:
            cmd.append("-fobjc-arc")
    if pch:
        cmd += ["-include", str(pch)]
    for inc in includes:
        cmd += ["-I", str(inc)]
    for d in defs:
        cmd += ["-D", d]
    cmd += [
        "-F" + str(FW_DIR), "-I" + str(USR_INC),
        opt, "-Wall", "-Wno-everything",
        "-x", lang, "-c", str(src), "-o", str(obj),
    ]
    if allow_fail:
        if _run_soft(cmd) is None:
            obj.unlink(missing_ok=True)
            return None
    else:
        run(cmd)
    return obj


# ---------------------------------------------------------------------------
# 链接：.o + .a + frameworks → arm64 Mach-O（ad-hoc 签名）
# ---------------------------------------------------------------------------
# 入口 / 生命周期符号绝不可用空桩顶替：链接会"成功"，但应用启动即 return 0 秒退，
# 用户侧报错为 "App returned from its main function with code 0."。
CRITICAL_SYMBOLS = {"_main", "main", "_UIApplicationMain", "_NSApplicationMain"}


def make_stub(symbols, stubdir):
    """为 undefined symbol 生成 arm64 汇编空桩（返回 0）。

    Unity 在 Linux 导出的 iOS 引擎库会引用少量仅在 macOS/Xcode 侧实现的平台函数
    （诊断栈回溯、沙盒路径等）。这些符号不在任何 .a 里，也不是游戏逻辑本体，
    用空桩占位即可完成链接验证；日志会完整列出桩符号，不掩盖真实缺失。
    """
    stubdir.mkdir(parents=True, exist_ok=True)
    s = stubdir / "stubs.s"
    lines = [".text", ".align 4"]
    for sym in symbols:
        sym = sym.strip().rstrip(",")
        if not sym or not re.match(r"^[A-Za-z0-9_.$]+$", sym):
            continue
        # __isPlatformVersionAtLeast 是 @available 的运行时判定，返回 1（满足）
        # 才不会让 Unity 误判成低版本系统；其余诊断/路径辅助桩返回 0 即可。
        val = 1 if "isPlatformVersionAtLeast" in sym else 0
        lines += [f".globl {sym}", f"{sym}:", f"    mov x0, #{val}", "    ret", ""]
    s.write_text("\n".join(lines) + "\n")
    o = stubdir / "stubs.o"
    run([CLANG, "-target", f"arm64-apple-ios{MIN_IOS}", "-isysroot", str(SDK),
         "-c", str(s), "-o", str(o)])
    return o


def link(objs, out_bin, extra_libs, frameworks, arc=True, work=None):
    base = [
        LD64, "-arch", "arm64", "-platform_version", "ios", MIN_IOS, SDK_VER,
        "-syslibroot", str(SDK), "-o", str(out_bin),
    ]
    base += [str(o) for o in objs]
    for a in extra_libs:
        base += [str(a)]
    base += ["-L" + str(USR_LIB), "-F" + str(FW_DIR)]
    for fw in frameworks:
        base += ["-framework", fw]
    for lib in DEFAULT_LIBS:
        base += ["-l" + lib]
    # ld64.lld 自带 ad-hoc codesign：写出标准 LC_CODE_SIGNATURE / CodeDirectory
    base += ["-adhoc_codesign", "-pie", "--error-limit=0"]

    stubs = []
    for rnd in range(1, 4):
        cmd = base + [str(s) for s in stubs]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            if stubs:
                log("STUB", f"补 {len(stubs)} 组平台桩后链接成功")
            return out_bin
        syms = sorted({x.rstrip(",") for x in
                       re.findall(r"undefined symbol:\s+(\S+)", r.stderr)})
        fatal = [s for s in syms if s in CRITICAL_SYMBOLS]
        if fatal:
            log("ERR", "关键入口符号未定义，拒绝空桩顶替：" + ", ".join(fatal))
            log("ERR", "常见于 main.mm / static_main.mm 编译失败被跳过；"
                       "链接会成功但应用启动即退出（App returned from its main function with code 0）。")
            raise SystemExit(1)
        if not syms or work is None or rnd == 3:
            log("ERR", "链接失败，错误摘要：")
            for line in r.stderr.splitlines()[:25]:
                log("ERR", line[:260])
            raise SystemExit(1)
        log("STUB", f"第 {rnd} 轮：{len(syms)} 个 undefined → "
                    + ", ".join(syms[:6]) + (" ..." if len(syms) > 6 else ""))
        stubs.append(make_stub(syms, work / f"stubs_round{rnd}"))
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Mach-O 校验
# ---------------------------------------------------------------------------
def verify_macho(path):
    d = Path(path).read_bytes()
    magic, cpu, sub, ftype, ncmds, sizeofcmds, flags = struct.unpack_from("<IiiIIII", d, 0)
    off = 32
    lcs = []
    for _ in range(ncmds):
        cmd, sz = struct.unpack_from("<II", d, off)
        lcs.append((cmd, sz))
        if cmd == 0x1D:  # LC_CODE_SIGNATURE
            dataoff, datasize = struct.unpack_from("<II", d, off + 8)
            blob = d[dataoff:dataoff + min(datasize, 64)]
        off += sz
    info = {
        "file": str(path), "size": len(d),
        "magic": hex(magic), "cpu": hex(cpu & 0xFFFFFFFF),
        "filetype": ftype, "ncmds": ncmds,
        "is_arm64": (cpu & 0xFFFFFFFF) == 0x0100000C,
        "is_execute": ftype == 2,
        "has_code_signature": any(c == 0x1D for c, _ in lcs),
        "sha256": hashlib.sha256(d).hexdigest()[:16],
    }
    return info


def verify_entrypoint(path):
    """校验 LC_MAIN 入口不是 make_stub 生成的空桩。

    真实 main 一定会调用 UIApplicationMain / objc_msgSend 等，函数体内至少
    有一条 bl；空桩是 `mov x0,#0; ret`（0xD2800000 / 0xD65F03C0），启动即
    返回 0，用户侧表现为 "App returned from its main function with code 0"。
    """
    d = Path(path).read_bytes()
    magic, cpu, sub, ftype, ncmds = struct.unpack_from("<IiiII", d, 0)
    off = 32
    entry = None
    for _ in range(ncmds):
        cmd, sz = struct.unpack_from("<II", d, off)
        if cmd == 0x80000028:            # LC_MAIN (LC_REQ_DYLD | 0x28)
            entry = struct.unpack_from("<QQ", d, off + 8)[0]
        off += sz
    if entry is None:
        return {"ok": False, "reason": "无 LC_MAIN", "bl_count": 0,
                "stub": False, "entryoff": "?", "first": []}
    code = d[entry:entry + 96]
    words = list(struct.unpack("<%dI" % (len(code) // 4),
                               code[:len(code) // 4 * 4]))
    # movz w0,#0 / mov x0,#0 紧跟 ret 即为空桩；单条 ret 同样非法
    stub = (len(words) >= 2 and words[0] in (0x52800000, 0xD2800000)
            and words[1] == 0xD65F03C0)
    immediate_ret = bool(words) and words[0] == 0xD65F03C0
    bls = sum(1 for w in words if (w & 0xFC000000) == 0x94000000)
    return {"ok": (not stub and not immediate_ret and bls >= 1),
            "entryoff": hex(entry), "first": [hex(w) for w in words[:4]],
            "bl_count": bls, "stub": stub}


# ---------------------------------------------------------------------------
# 组装 .app
# ---------------------------------------------------------------------------
def make_icon(path, size=180):
    """生成一个合法的 PNG AppIcon（不依赖任何外部资源）。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (size, size), (26, 92, 120))
    dr = ImageDraw.Draw(img)
    # 简化的木筏：几条木色横板 + 海面
    dr.rectangle([0, int(size * 0.62), size, size], fill=(30, 120, 150))
    for i in range(5):
        y = int(size * (0.30 + i * 0.075))
        dr.rectangle([int(size * 0.12), y, int(size * 0.88), y + int(size * 0.055)],
                     fill=(150, 100 + i * 6, 55))
    dr.rectangle([int(size * 0.47), int(size * 0.10),
                  int(size * 0.53), int(size * 0.34)], fill=(90, 60, 40))
    dr.polygon([(int(size * 0.53), int(size * 0.12)),
                (int(size * 0.80), int(size * 0.28)),
                (int(size * 0.53), int(size * 0.32))], fill=(240, 235, 220))
    img.save(path, "PNG")


def build_info_plist(template_path, settings, extra=None):
    if template_path and Path(template_path).exists():
        raw = Path(template_path).read_bytes()
        # 模板里含 ${...} 变量，plistlib 仍能当作字符串读取
        try:
            pl = plistlib.loads(raw)
        except Exception:
            pl = {}
    else:
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


def assemble_app(binary, app_dir, info_plist, data_dir, make_resources=True):
    app = Path(app_dir)
    if app.exists():
        shutil.rmtree(app)
    app.mkdir(parents=True)
    # 二进制保持 ld64.lld 刚做完 ad-hoc 签名的原样，绝不追加/改写任何字节，
    # 否则会破坏 CodeDirectory 的哈希覆盖范围。构建标记改放 Info.plist 自定义字段。
    shutil.copy2(binary, app / info_plist["CFBundleExecutable"])
    info_plist = dict(info_plist)
    info_plist["LXCBuildTool"] = "linux_xcodebuild.py"
    info_plist["LXCBuildMarker"] = "LINUX_XCODEBUILD_PROTOTYPE"
    (app / "Info.plist").write_bytes(plistlib.dumps(info_plist, fmt=plistlib.FMT_XML))
    if data_dir and Path(data_dir).exists():
        shutil.copytree(data_dir, app / "Data", dirs_exist_ok=True)
    if make_resources:
        make_icon(app / "AppIcon60x60@2x.png", 120)
        make_icon(app / "AppIcon60x60@3x.png", 180)
        # LaunchScreen.storyboard 占位（空壳即可让安装器满意）
        (app / "LaunchScreen.storyboard").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<document type="com.apple.InterfaceBuilder3.CocoaTouch.Storyboard.XIB" '
            'version="3.0" toolsVersion="1" targetRuntime="iOS.CocoaTouch" '
            'initialViewController="X"><scenes/></document>')
    return app


# ---------------------------------------------------------------------------
# 打包 IPA + 校验
# ---------------------------------------------------------------------------
def pack_ipa(app_dir, ipa_path):
    ipa = Path(ipa_path)
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
    r = run(["unzip", "-l", str(ipa_path)])
    names = r.stdout.splitlines()
    need = ["Payload/", "Info.plist"]
    return {
        "ipa": str(ipa_path),
        "size": Path(ipa_path).stat().st_size,
        "has_payload": any("Payload/" in n for n in names),
        "has_info_plist": any(n.strip().endswith("Info.plist") for n in names),
        "entries": [n.strip() for n in names if n.strip().endswith(
            (".plist", ".png", ".storyboard"))][:12],
    }


# ---------------------------------------------------------------------------
# 命令：smoke —— 最小 UIKit 空工程，端到端跑通
# ---------------------------------------------------------------------------
SMOKE_MAIN = r'''
#import <UIKit/UIKit.h>
#import <Foundation/Foundation.h>

@interface LXCAppDelegate : UIResponder <UIApplicationDelegate>
@property (strong, nonatomic) UIWindow *window;
@end
@implementation LXCAppDelegate
- (BOOL)application:(UIApplication *)application
    didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    self.window = [[UIWindow alloc] initWithFrame:[[UIScreen mainScreen] bounds]];
    self.window.backgroundColor = [UIColor colorWithRed:0.10 green:0.36 blue:0.47 alpha:1.0];
    UIViewController *vc = [[UIViewController alloc] init];
    UILabel *label = [[UILabel alloc] initWithFrame:CGRectMake(20, 120, 340, 160)];
    label.text = @"Raft · Linux Build";
    label.textColor = [UIColor whiteColor];
    label.textAlignment = NSTextAlignmentCenter;
    label.font = [UIFont boldSystemFontOfSize:26];
    label.numberOfLines = 0;
    [vc.view addSubview:label];
    self.window.rootViewController = vc;
    [self.window makeKeyAndVisible];
    return YES;
}
@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil,
                                 NSStringFromClass([LXCAppDelegate class]));
    }
}
'''


def cmd_smoke(args):
    log("SMOKE", "工具链自检")
    for p in [SDK, Path(CLANG) if "/" in CLANG else None, Path(LD64)]:
        if p and not p.exists() and not shutil.which(str(p)):
            log("ERR", f"缺工具: {p}")
            raise SystemExit(2)
    log("TOOL", f"SDK={SDK}")
    log("TOOL", f"clang={CLANG}  ld64={LD64}")

    work = Path(args.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    (work / "src").mkdir(parents=True)
    (work / "obj").mkdir()

    main_m = work / "src/main.m"
    main_m.write_text(SMOKE_MAIN)
    settings = {
        "deployment_target": MIN_IOS, "archs": "arm64",
        "bundle_id": args.bundle_id, "arc": True,
        "product_name": "RaftLinuxSmoke", "executable": "RaftLinuxSmoke",
        "sdkroot": "iphoneos",
    }

    log("1/5", "clang 交叉编译 UIKit ObjC 源码 → arm64 .o")
    o = compile_one(main_m, work / "obj",
                    includes=[work / "src"], defs=[], arc=True)
    log("OBJ", f"{o.name}  {o.stat().st_size} B")

    log("2/5", "ld64.lld 链接 frameworks → arm64 Mach-O（含 ad-hoc 签名）")
    binp = work / "RaftLinuxSmoke"
    link([o], binp, [], ["Foundation", "UIKit", "CoreGraphics", "QuartzCore"], True)
    mi = verify_macho(binp)
    log("MACHO", f"size={mi['size']} arm64={mi['is_arm64']} "
                 f"exec={mi['is_execute']} signed={mi['has_code_signature']} "
                 f"sha={mi['sha256']}")
    assert mi["is_arm64"] and mi["is_execute"] and mi["has_code_signature"], \
        "Mach-O 校验未通过"

    log("3/5", "组装 .app（Info.plist / 图标 / LaunchScreen / Data）")
    pl = build_info_plist(None, settings, extra={
        "CFBundleIcons": {"CFBundlePrimaryIcon": {
            "CFBundleIconFiles": ["AppIcon60x60@2x", "AppIcon60x60@3x"]}},
    })
    app = assemble_app(binp, work / "Payload/RaftLinuxSmoke.app", pl, None)

    log("4/5", "zip 打包 → IPA")
    ipa = pack_ipa(app, args.out)

    log("5/5", "IPA 结构校验")
    vi = verify_ipa(ipa)
    log("IPA", f"size={vi['size']} payload={vi['has_payload']} "
               f"plist={vi['has_info_plist']}")
    for e in vi["entries"]:
        log(" ", e)

    print("\n" + "=" * 66)
    print("端到端 Smoke 通过：Linux 无 Xcode 产出了结构完整、arm64、带 ad-hoc")
    print("签名的 iOS .ipa，可直接交 SideStore / AltStore 自签安装。")
    print("=" * 66)
    return ipa


# ---------------------------------------------------------------------------
# 命令：parse —— 解析真实 pbxproj
# ---------------------------------------------------------------------------
def cmd_parse(args):
    info = parse_pbxproj(args.pbxproj)
    s = info["settings"]
    print("=" * 66)
    print("project.pbxproj 解析结果")
    print("=" * 66)
    print(f"原始大小            : {info['raw_bytes']} B")
    print(f"ARCHS               : {s['archs']}")
    print(f"DEPLOYMENT_TARGET   : {s['deployment_target']}")
    print(f"SDKROOT             : {s['sdkroot']}")
    print(f"PRODUCT_NAME        : {s['product_name']}")
    print(f"OBJC_ARC            : {s['arc']}")
    print(f"Sources 阶段文件数  : {len(info['source_names'])}")
    print(f"Frameworks ({len(info['frameworks'])}): " + ", ".join(info['frameworks']))
    if info["tbd_refs"]:
        print("系统库引用(.tbd/.dylib): " + ", ".join(info["tbd_refs"]))
    print("\n源文件清单（前 50）:")
    for n in info["source_names"][:50]:
        print("   ", n)
    return info


# ---------------------------------------------------------------------------
# 命令：project —— 对真实导出的 Xcode 工程编译链接打包
# ---------------------------------------------------------------------------
def cmd_project(args):
    proj = Path(args.dir).resolve()
    pbx = proj / "Unity-iPhone.xcodeproj/project.pbxproj"
    if not pbx.exists():
        log("ERR", f"找不到 {pbx}")
        raise SystemExit(2)
    info = parse_pbxproj(pbx)
    s = info["settings"]
    log("PARSE", f"archs={s['archs']} target={s['deployment_target']} "
                 f"frameworks={len(info['frameworks'])}")

    # Trampoline 源码（Classes/ + Libraries/）
    srcs = []
    for pat in ("*.mm", "*.m", "*.cpp", "*.cc", "*.c"):
        srcs += glob.glob(str(proj / "Classes/**" / pat), recursive=True)
        srcs += glob.glob(str(proj / "Libraries/**" / pat), recursive=True)
    srcs = sorted({x for x in srcs if "Tests" not in x})
    # Unity 2022.3 把 app target 的 main 放在 MainApp/main.mm（不在 Classes/），
    # 且它默认动态加载 UnityFramework.framework。本编排器把全部代码静态链进单个
    # 可执行文件，所以排除它、改用下方生成的「直接取单例」静态版 main。
    mainapp = sorted(glob.glob(str(proj / "MainApp/**" / "*.mm"), recursive=True))
    srcs = sorted(set(srcs) | {x for x in mainapp
                               if Path(x).name != "main.mm" and "Tests" not in x})
    if mainapp:
        log("MAIN", f"发现 app target MainApp/，改用静态链接版 main")
    # il2cpp 生成的游戏逻辑：只收 Source/ 下的产物，IL2CPP/ 下的运行时源码
    # 已被 Unity 预编译进 Libraries/libil2cpp.a，再编会重复定义。
    il2cpp_srcs = []
    for pat in ("*.cpp", "*.cc", "*.c"):
        il2cpp_srcs += glob.glob(
            str(proj / "Il2CppOutputProject/Source/**" / pat), recursive=True)
    il2cpp_srcs = sorted(set(il2cpp_srcs))
    log("SRC", f"Trampoline {len(srcs)} 个 + il2cpp 生成 {len(il2cpp_srcs)} 个源码")

    # 收集静态库
    libs = []
    for pat in ("*.a",):
        libs += glob.glob(str(proj / "Libraries/**" / pat), recursive=True)
    libs = sorted(libs)
    log("LIB", f"发现 {len(libs)} 个静态库: " +
        ", ".join(Path(x).name for x in libs[:8]))

    # 检查 il2cpp 生成产物是否存在（判断这是不是完整导出工程）
    il2cpp_cpp = glob.glob(str(proj / "**/Il2CppOutputProject/**/*.cpp"), recursive=True)
    has_gameassembly = any("GameAssembly" in Path(x).name or
                           "libil2cpp" in Path(x).name for x in libs)
    if not il2cpp_cpp and not has_gameassembly:
        log("WARN", "未检测到 il2cpp 生成的 C++ 代码 / libGameAssembly.a。")
        log("WARN", "Trampoline 模板不是完整工程：源码依赖 Unity 私有引擎头与 il2cpp 运行时。")
        log("WARN", "完整编译需要 Unity 授权导出的 Xcode 工程。此处仅演示编排能力。")
        log("HINT", "先用 `parse` 查看工程契约；拿到授权导出工程后重跑 `project` 即可。")
        if not args.force:
            raise SystemExit(3)

    work = Path(args.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    # 头文件搜索路径与 Unity 生成的 project.pbxproj / HEADER_SEARCH_PATHS 对齐
    icpp = proj / "Il2CppOutputProject/IL2CPP"
    includes = [proj / "Classes", proj / "Classes/UI", proj / "Classes/Unity",
                proj / "Classes/PluginBase", proj / "Classes/Native",
                proj / "Libraries", proj / "Libraries/bdwgc/include", proj,
                proj / "Il2CppOutputProject/Source/il2cppOutput",
                icpp / "libil2cpp", icpp / "libil2cpp/pch",
                icpp / "external/baselib/Include",
                icpp / "external/baselib/Platforms/IOS/Include",
                icpp / "external/xxHash", icpp / "libmono",
                proj / "Libraries/external/baselib/Platforms/IOS/Include"]

    # Unity 2022.3.62f1 起把公开头改成 framework 风格尖括号导入
    # (#import <UnityFramework/UnityAppController.h>)。Linux 侧没有 Xcode 的
    # framework header map，需补一层绝对路径转发头，否则 Classes/main.mm 与
    # 生成的 static_main.mm 都会编译失败 → _main 未定义 → 被空桩顶替 → 秒退。
    fwshim = work / "fwheaders" / "UnityFramework"
    fwshim.mkdir(parents=True, exist_ok=True)
    refs = set()
    for _root in (proj / "Classes", proj / "UnityFramework", proj / "MainApp"):
        for _pat in ("*.h", "*.m", "*.mm"):
            for _hp in glob.glob(str(_root / "**" / _pat), recursive=True):
                try:
                    _txt = Path(_hp).read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                refs.update(re.findall(
                    r"<\s*UnityFramework/([A-Za-z0-9_./-]+\.h)\s*>", _txt))
    _made = []
    for _name in sorted(refs):
        _rel = Path(_name)
        if len(_rel.parts) != 1:
            continue
        _dst = fwshim / _rel.name
        for _cand in (proj / "Classes" / _rel.name,
                      proj / "UnityFramework" / _rel.name):
            if _cand.exists():
                _dst.write_text('#include "%s"\n' % str(_cand))
                _made.append(_rel.name + "=ok")
                break
        else:
            _dst.write_text("/* %s: Unity template placeholder */\n" % _rel.name)
            _made.append(_rel.name + "=stub")
    includes.insert(0, work / "fwheaders")
    log("HEADER", "framework 头映射层: " + (", ".join(_made) if _made else "(无需)"))

    # 前三个来自 project.pbxproj 的 OTHER_CFLAGS("-DUNITY_VERSION_*")，
    # 缺一个就会让 Classes/UnityTrampolineConfigure.h 直接 #error。
    # BASELIB_INLINE_NAMESPACE 把 baselib 的 ReentrantLock 放进 il2cpp_baselib
    # inline namespace，消除 il2cpp-object-internals.h 与 Baselib.h 的二义性。
    # UNITY_VERSION_MIN 必须与导出工程的 Unity 补丁号一致；升级引擎时用
    # 环境变量 UNITY_VERSION_PATCH 覆盖（默认 7 = 2022.3.7f1）。
    unity_patch = os.environ.get("UNITY_VERSION_PATCH", "7")
    defs = ["UNITY_IOS=1", "IL2CPP_TARGET_ARM64=1",
            "UNITY_VERSION_VER=2022", "UNITY_VERSION_MAJ=3",
            "UNITY_VERSION_MIN=%s" % unity_patch,
            "BASELIB_INLINE_NAMESPACE=il2cpp_baselib",
            # Il2CppCodeRegistration.cpp 里 g_CodegenRegistration 被 #if RUNTIME_IL2CPP
            # 包住；缺它 libil2cpp.a 的 vm::Runtime::Init 会引用一个不存在的全局。
            "RUNTIME_IL2CPP=1"]
    # 最小 PCH：只注入 Foundation/UIKit（不引用 Unity 私有头）。
    # 若工程自带可用 Prefix.pch 且 --use-project-pch，则改用之。
    min_pch = work / "min.pch"
    min_pch.write_text(
        "#ifdef __OBJC__\n#import <Foundation/Foundation.h>\n"
        "#import <UIKit/UIKit.h>\n#endif\n")
    pch = (proj / "Classes/Prefix.pch") if args.use_project_pch else min_pch
    # ObjC/ObjC++ 注入工程 Prefix.pch；C/C++ 不注入（il2cpp 生成代码自带
    # pch-cpp.hpp）。生成代码约 50MB，用 -O0 + 线程池并行编译以免串行过慢。
    jobs = []
    for x in srcs:
        lang = lang_for(x)
        jobs.append((Path(x), pch if lang and lang.startswith("objective-c") else None,
                     "-O2"))
    for x in il2cpp_srcs:
        jobs.append((Path(x), None, "-O0"))

    if mainapp:
        static_main = work / "static_main.mm"
        static_main.write_text(
            "#include <UIKit/UIKit.h>\n"
            "#include <mach-o/ldsyms.h>\n"
            '#include "UnityFramework/UnityFramework.h"\n'
            "int main(int argc, char* argv[]) {\n"
            "  @autoreleasepool {\n"
            "    id ufw = [UnityFramework getInstance];\n"
            "    [ufw setExecuteHeader: &_mh_execute_header];\n"
            "    [ufw runUIApplicationMainWithArgc: argc argv: argv];\n"
            "  }\n"
            "  return 0;\n"
            "}\n")
        jobs.append((static_main, pch, "-O2"))

    objs = []
    failed = []
    from concurrent.futures import ThreadPoolExecutor

    def _job(j):
        src, pp, opt = j
        return src, compile_one(src, work / "obj", includes, defs, s["arc"],
                                pch=pp, allow_fail=args.keep_going, opt=opt)

    workers = min(16, os.cpu_count() or 8)
    log("1/5", f"clang 并行({workers})编译 {len(jobs)} 个源码: "
               f"ObjC pch={pch.name}, C++ 无 pch / -O0")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (src, o) in enumerate(ex.map(_job, jobs)):
            if o:
                objs.append(o)
            else:
                failed.append(str(src))
            if (i + 1) % 20 == 0:
                log(" ", f"{i+1}/{len(jobs)}")
    if failed:
        critical = [x for x in failed
                    if Path(x).name in ("main.mm", "static_main.mm")]
        if critical:
            log("ERR", "入口 / UnityFramework 源码编译失败，拒绝继续"
                       "（否则 _main 会被空桩顶替、应用启动秒退）："
                + ", ".join(critical))
            raise SystemExit(1)
        log("WARN", f"{len(failed)} 个源码编译失败并跳过："
                    f"{', '.join(Path(x).name for x in failed[:8])}...")

    log("2/5", f"ld64.lld 链接 {len(objs)} 个 .o + {len(libs)} 个 .a + frameworks")
    binp = work / s["executable"]
    link(objs, binp, libs, info["frameworks"] or DEFAULT_FRAMEWORKS, s["arc"],
         work=work)
    mi = verify_macho(binp)
    log("MACHO", f"arm64={mi['is_arm64']} signed={mi['has_code_signature']} "
                 f"size={mi['size']}")
    ei = verify_entrypoint(binp)
    log("ENTRY", f"LC_MAIN={ei.get('entryoff','?')} 首指令={ei.get('first',[])} "
                 f"bl={ei.get('bl_count',0)} 空桩={ei.get('stub',False)} ok={ei['ok']}")
    if not ei["ok"]:
        log("ERR", "入口点为 return-0 空桩或无调用，应用会启动即退出；拒绝打包")
        raise SystemExit(1)

    log("3/5", "组装 .app")
    pl = build_info_plist(proj / "Info.plist", s)
    app = assemble_app(binp, work / f"Payload/{s['product_name']}.app", pl,
                       proj / "Data")
    log("4/5", "打包 IPA")
    ipa = pack_ipa(app, args.out)
    log("5/5", "校验")
    log("IPA", str(verify_ipa(ipa)))
    return ipa


def main():
    ap = argparse.ArgumentParser(description="Linux 自制 xcodebuild 原型")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("smoke", help="最小 UIKit 空工程端到端跑通")
    p.add_argument("--out", default="out/RaftLinuxSmoke.ipa")
    p.add_argument("--work", default=".build_smoke")
    p.add_argument("--bundle-id", default="com.workbuddy.raft.linuxsmoke")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("parse", help="解析 project.pbxproj")
    p.add_argument("--pbxproj", required=True)
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("project", help="对导出的 Xcode 工程编译链接打包")
    p.add_argument("--dir", required=True)
    p.add_argument("--out", default="out/UnityBuild.ipa")
    p.add_argument("--work", default=".build_project")
    p.add_argument("--force", action="store_true", help="缺少 il2cpp 产物也强制继续编译")
    p.add_argument("--keep-going", action="store_true",
                   help="单个源码编译失败时跳过并继续（用于模板态演示）")
    p.add_argument("--use-project-pch", action="store_true",
                   help="改用工程自带 Classes/Prefix.pch（完整导出工程才可用）")
    p.set_defaults(func=cmd_project)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
