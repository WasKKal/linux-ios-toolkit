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
