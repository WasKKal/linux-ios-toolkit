#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_dylib.py — 纯 Linux（免 macOS）用 iOS SDK 编译 / ad-hoc 签名 / deb 打包 iOS dylib。

三大能力之一：通过 IOSSDK 编译打包 Dylib。
实测环境：Ubuntu 22.04 / clang 18.1.3 / ld64.lld (LLVM 18) / iPhoneOS16.5.sdk

用法：
  # 1) 自检（不需要真实工程，造一个临时源码跑通全链路）
  python3 build_dylib.py smoke

  # 2) 编译单个/多个源码为 arm64 dylib（ad-hoc 签名）
  python3 build_dylib.py build \
      --src src/a.c src/b.mm \
      --out libfoo.dylib \
      --name foo \
      --sysroot /opt/ios-sdk/sdks/iPhoneOS16.5.sdk

  # 3) 顺带打成 Theos 风格 deb（安装到 /Library/MobileSubstrate/DynamicLibraries）
  python3 build_dylib.py build --src src/*.m --out libfoo.dylib --name foo \
      --deb --bundle com.example.foo --min-ios 12.0

  # 4) 校验一个 dylib 是否为 arm64 + 已签名
  python3 build_dylib.py verify --file libfoo.dylib

环境变量（可被命令行覆盖）：
  IOS_SDK     iOS SDK 路径，未设则自动探测
  CLANG       clang 可执行文件，默认 clang
  LD64        ld64.lld 路径，未设则自动探测 /usr/lib/llvm-*/bin/ld64.lld
  IPHONEOS_DEPLOYMENT_TARGET  最低系统版本，默认 12.0
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MH_MAGIC_64 = 0xFEEDFACF
CPU_TYPE_ARM64 = 0x0100000C
MH_DYLIB = 6
LC_CODE_SIGNATURE = 0x1D
DEFAULT_DEPLOY = "12.0"
VALID_SRC = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------
def detect_sdk(explicit: str | None) -> str:
    if explicit and Path(explicit).is_dir():
        return explicit
    if os.environ.get("IOS_SDK") and Path(os.environ["IOS_SDK"]).is_dir():
        return os.environ["IOS_SDK"]
    bases = [
        "/opt/ios-sdk/sdks", "/sandbox/workspace/sdk", "/usr/local/ios-sdk",
        str(Path.cwd() / "sdk"), str(Path.home() / "sdk"), "/sdk",
    ]
    for base in bases:
        if not Path(base).is_dir():
            continue
        hits = sorted(Path(base).glob("iPhoneOS*.sdk"), reverse=True)
        if hits:
            return str(hits[0])
    raise RuntimeError("未找到 iOS SDK，请用 --sysroot 或 IOS_SDK 指定")


def detect_ld64(explicit: str | None) -> str:
    if explicit and Path(explicit).exists():
        return explicit
    if os.environ.get("LD64") and Path(os.environ["LD64"]).exists():
        return os.environ["LD64"]
    hits = sorted(Path("/usr/lib").glob("llvm-*/bin/ld64.lld"),
                  key=lambda p: _llvm_ver(p), reverse=True)
    if hits:
        return str(hits[0])
    found = shutil.which("ld64.lld")
    if found:
        return found
    raise RuntimeError("未找到 ld64.lld，请用 --ld64 或 LD64 指定")


def _llvm_ver(p: Path) -> int:
    m = re.search(r"llvm-(\d+)", str(p))
    return int(m.group(1)) if m else 0


def sdk_version(sysroot: str) -> str:
    m = re.search(r"iPhoneOS(\d+\.\d+)\.sdk", sysroot)
    return m.group(1) if m else "16.5"


# ---------------------------------------------------------------------------
# Mach-O 校验
# ---------------------------------------------------------------------------
def verify_macho(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在: {path}"}
    d = p.read_bytes()
    if len(d) < 32:
        return {"ok": False, "error": "文件过短，不是 Mach-O"}
    magic, cpu, sub, ft, ncmds, szcmds, flags, res = struct.unpack_from("<IiiIIIII", d, 0)
    if magic != MH_MAGIC_64:
        return {"ok": False, "error": f"非 64 位 Mach-O magic: {hex(magic)}"}
    off, cmds = 32, {}
    for _ in range(ncmds):
        if off + 8 > len(d):
            break
        cmd, cs = struct.unpack_from("<II", d, off)
        cmds[cmd] = cmds.get(cmd, 0) + 1
        off += cs
    signed = cmds.get(LC_CODE_SIGNATURE, 0) > 0
    ok = (cpu == CPU_TYPE_ARM64) and (ft == MH_DYLIB)
    return {
        "ok": ok,
        "arm64": cpu == CPU_TYPE_ARM64,
        "dylib": ft == MH_DYLIB,
        "filetype": ft,
        "ncmds": ncmds,
        "signed": signed,
        "size": len(d),
        "error": "" if ok else f"cpu={hex(cpu & 0xffffffff)} filetype={ft}，期望 arm64 dylib",
    }


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------
def compile_sources(srcs: list[str], out: str, name: str, *,
                    sysroot: str, ld64: str, clang: str, deploy: str,
                    extra_cflags: list[str], extra_ldflags: list[str],
                    adhoc: bool, install_name: str | None, verbose: bool) -> dict:
    src_paths = []
    for s in srcs:
        p = Path(s)
        if p.is_dir():
            src_paths.extend(sorted(x for x in p.rglob("*") if x.suffix.lower() in VALID_SRC))
        elif p.suffix.lower() in VALID_SRC:
            src_paths.append(p)
        else:
            raise RuntimeError(f"不支持的源码类型: {s}")
    if not src_paths:
        raise RuntimeError("没有可编译的源码（支持 .c/.cc/.cpp/.cxx/.m/.mm）")

    out_path = Path(out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ver = sdk_version(sysroot)
    minver = deploy or os.environ.get("IPHONEOS_DEPLOYMENT_TARGET", DEFAULT_DEPLOY)
    target = f"arm64-apple-ios{minver}"
    iname = install_name or f"@rpath/lib{name}.dylib"

    with tempfile.TemporaryDirectory(prefix="dylib_build_") as td:
        objs = []
        for i, src in enumerate(src_paths):
            obj = Path(td) / f"o_{i}_{src.stem}.o"
            cmd = [clang, "-target", target, "-isysroot", sysroot,
                   "-O2", "-fPIC", "-fvisibility=hidden", "-c", str(src), "-o", str(obj)]
            cmd += extra_cflags
            _run(cmd, verbose, f"编译 {src.name}")
            objs.append(str(obj))

        link = [ld64, "-dylib", "-arch", "arm64",
                "-platform_version", "ios", minver, ver,
                "-syslibroot", sysroot, "-install_name", iname,
                "-o", str(out_path)] + objs
        # 系统框架（Foundation 等）在纯 C 场景不需要；有 .m/.mm 时按需经 extra_ldflags 加 -framework
        link += extra_ldflags
        if adhoc:
            link += ["-adhoc_codesign"]
        _run(link, verbose, "链接 dylib" + (" + ad-hoc 签名" if adhoc else ""))

    result = verify_macho(out_path)
    result["out"] = str(out_path)
    result["sources"] = [str(x) for x in src_paths]
    return result


def _run(cmd: list[str], verbose: bool, label: str):
    if verbose:
        print(f"  [{label}] " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"{label} 失败（exit {r.returncode}）:\n{msg[:2000]}")


# ---------------------------------------------------------------------------
# deb 打包（Theos 风格）
# ---------------------------------------------------------------------------
def make_deb(dylib: str, deb_out: str, *, bundle: str, name: str,
             min_ios: str, version: str = "1.0") -> str:
    dylib_p = Path(dylib).resolve()
    deb_p = Path(deb_out).resolve()
    deb_p.parent.mkdir(parents=True, exist_ok=True)
    # Theos 标准布局：文件直接位于 /Library/MobileSubstrate/DynamicLibraries/
    inst_rel = "Library/MobileSubstrate/DynamicLibraries"
    control_text = (
        f"Package: {bundle}\n"
        f"Name: {name}\n"
        f"Version: {version}\n"
        f"Architecture: iphoneos-arm\n"
        f"Maintainer: Linux iOS Toolkit <toolkit@localhost>\n"
        f"Description: {name} dylib built on Linux (ad-hoc signed)\n"
        f"Depends: mobilesubstrate\n"
    )
    filter_text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n\t<key>Filter</key>\n\t<dict>\n'
        '\t\t<key>Bundles</key>\n\t\t<array>\n'
        '\t\t\t<string>com.apple.springboard</string>\n'
        '\t\t</array>\n\t</dict>\n</dict>\n</plist>\n'
    )

    with tempfile.TemporaryDirectory(prefix="dylib_deb_") as td:
        root = Path(td)
        if shutil.which("dpkg-deb"):
            # dpkg-deb 布局：bdir/DEBIAN/control + bdir/<安装路径>
            bdir = root / "bdir"
            deb_dir = bdir / "DEBIAN"
            deb_dir.mkdir(parents=True, exist_ok=True)
            (deb_dir / "control").write_text(control_text, encoding="utf-8")
            dst = bdir / inst_rel
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dylib_p, dst / dylib_p.name)
            (dst / f"{name}.plist").write_text(filter_text, encoding="utf-8")
            subprocess.run(["dpkg-deb", "--build", str(bdir), str(deb_p)],
                           check=True, capture_output=True, text=True)
        else:
            # 纯 ar 回退：debian-binary + control.tar.gz + data.tar.gz
            payload = root / "payload"
            dst = payload / inst_rel
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dylib_p, dst / dylib_p.name)
            (dst / f"{name}.plist").write_text(filter_text, encoding="utf-8")
            ctl_dir = root / "ctl"
            ctl_dir.mkdir(exist_ok=True)
            (ctl_dir / "control").write_text(control_text, encoding="utf-8")
            data_tgz = root / "data.tar.gz"
            ctl_tgz = root / "control.tar.gz"
            subprocess.run(["tar", "-czf", str(data_tgz), "-C", str(payload), "."], check=True)
            subprocess.run(["tar", "-czf", str(ctl_tgz), "-C", str(ctl_dir), "control"], check=True)
            deb_bin = root / "debian-binary"
            deb_bin.write_text("2.0\n", encoding="utf-8")
            if deb_p.exists():
                deb_p.unlink()
            subprocess.run(["ar", "rc", str(deb_p), str(deb_bin), str(ctl_tgz), str(data_tgz)],
                           check=True)
    return str(deb_p)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_build(args):
    sysroot = detect_sdk(args.sysroot)
    ld64 = detect_ld64(args.ld64)
    clang = args.clang or os.environ.get("CLANG") or shutil.which("clang") or "clang"
    print(f"[ENV] SDK={sysroot} ({sdk_version(sysroot)})")
    print(f"[ENV] clang={clang} ld64={ld64}")
    result = compile_sources(
        args.src, args.out, args.name,
        sysroot=sysroot, ld64=ld64, clang=clang, deploy=args.min_ios,
        extra_cflags=args.cflag or [], extra_ldflags=args.ldflag or [],
        adhoc=not args.no_sign, install_name=args.install_name, verbose=args.verbose,
    )
    print(f"[DYLIB] {result['out']}  arm64={result['arm64']} "
          f"dylib={result['dylib']} signed={result['signed']} "
          f"size={result['size'] // 1024}KB  sources={len(result['sources'])}")
    if not result["ok"]:
        print("[FAIL] " + result["error"], file=sys.stderr)
        return 2
    if not result["signed"] and not args.no_sign:
        print("[FAIL] 请求签名但产物无 LC_CODE_SIGNATURE", file=sys.stderr)
        return 2

    if args.deb:
        deb = make_deb(args.out, args.deb_out or (str(Path(args.out).with_suffix('.deb'))),
                       bundle=args.bundle or f"com.local.{args.name}",
                       name=args.name, min_ios=args.min_ios)
        sz = Path(deb).stat().st_size // 1024
        print(f"[DEB] {deb}  {sz}KB")
    return 0


def cmd_verify(args):
    r = verify_macho(args.file)
    print(f"[VERIFY] {args.file}")
    print(f"  arm64={r.get('arm64')} dylib={r.get('dylib')} "
          f"signed={r.get('signed')} filetype={r.get('filetype')} "
          f"ncmds={r.get('ncmds')} size={r.get('size', 0) // 1024}KB")
    if not r["ok"]:
        print("  FAIL: " + r["error"], file=sys.stderr)
        return 2
    return 0


def cmd_smoke(args):
    print("[SMOKE] 造临时源码，跑通 编译→签名→deb→校验 全链路")
    with tempfile.TemporaryDirectory(prefix="dylib_smoke_") as td:
        root = Path(td)
        src = root / "hello.c"
        src.write_text(
            '#include <stdint.h>\n'
            '__attribute__((visibility("default")))\n'
            'int raft_smoke_add(int a,int b){return a+b+42;}\n', encoding="utf-8")
        out = root / "libsmoke.dylib"
        rc = cmd_build(argparse.Namespace(
            src=[str(src)], out=str(out), name="smoke",
            sysroot=None, ld64=None, clang=None, min_ios=args.min_ios or DEFAULT_DEPLOY,
            cflag=[], ldflag=[], no_sign=False, install_name=None, verbose=args.verbose,
            deb=True, deb_out=str(root / "smoke.deb"), bundle="com.local.smoke",
        ))
        if rc != 0:
            return rc
        r = verify_macho(out)
        assert r["ok"] and r["arm64"] and r["dylib"] and r["signed"], r
        assert (root / "smoke.deb").is_file(), "deb 未生成"
        print("[SMOKE] ✓ dylib arm64 + ad-hoc 签名 + deb 打包 全部通过")
        return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="纯 Linux 用 iOS SDK 编译/签名/打包 dylib")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="编译源码为 arm64 dylib")
    b.add_argument("--src", nargs="+", required=True, help="源码文件或目录")
    b.add_argument("--out", required=True, help="输出 dylib 路径")
    b.add_argument("--name", required=True, help="dylib 名（用于 install_name/deb）")
    b.add_argument("--sysroot", help="iOS SDK 路径")
    b.add_argument("--ld64", help="ld64.lld 路径")
    b.add_argument("--clang", help="clang 路径")
    b.add_argument("--min-ios", default=None, help="最低 iOS 版本（默认 12.0）")
    b.add_argument("--cflag", action="append", help="附加编译 flag（可多次）")
    b.add_argument("--ldflag", action="append", help="附加链接 flag（可多次，如 -framework Foundation）")
    b.add_argument("--no-sign", action="store_true", help="跳过 ad-hoc 签名")
    b.add_argument("--install-name", help="自定义 install name（默认 @rpath/lib<name>.dylib）")
    b.add_argument("--deb", action="store_true", help="同时打 Theos 风格 deb")
    b.add_argument("--deb-out", help="deb 输出路径")
    b.add_argument("--bundle", help="deb 包名（默认 com.local.<name>）")
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(func=cmd_build)

    v = sub.add_parser("verify", help="校验 dylib 为 arm64 + 签名")
    v.add_argument("--file", required=True)
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("smoke", help="端到端自检（造临时源码）")
    s.add_argument("--min-ios", default=None)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_smoke)
    return ap


def main():
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except RuntimeError as e:
        print("[ERROR] " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
