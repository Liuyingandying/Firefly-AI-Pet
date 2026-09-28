# -*- coding: utf-8 -*-
"""check_environment.py — Voice Service 环境检查器（只检测，不修复、不修改任何状态）。

覆盖 Golden Environment 暴露过的全部故障面：
  Windows / Python 版本 / torch+CUDA / GPU / parselmouth / edge-tts /
  sounddevice 设备 / 模型存在 + SHA256 / 8300 端口 / 系统 Commit 状态。

Commit 检测是旧主机三类真实故障（parselmouth DLL「页面文件太小」、
cuDNN "GET was unable to find an engine"、numpy _ArrayMemoryError）的共同
根因面——只看物理 RAM 会漏报，必须查 CommitLimit/CommitFree。

用法（任意机器）:
  python scripts/check_environment.py                 # 全量检测
  python scripts/check_environment.py --models-only   # 只校验模型
退出码: 0 = 无 FAIL；1 = 有 FAIL（WARNING 不影响退出码）。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import socket
import struct
import sys
import urllib.request
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
CHECKS: list[dict] = []


def record(name: str, ok: bool | None, detail: str, level: str = "INFO") -> None:
    """level: INFO / WARNING / FAIL / SKIP"""
    CHECKS.append({"check": name, "ok": ok, "level": level, "detail": detail})
    mark = {"INFO": "·", "WARNING": "!", "FAIL": "X", "SKIP": "-"}[level]
    print(f"[{mark}] {name}: {detail}")


# ---------------------------------------------------------------- OS / Python
def check_os() -> None:
    import platform
    is_win = platform.system() == "Windows"
    record("os.windows", is_win, platform.platform(),
           "INFO" if is_win else "WARNING")


def check_python() -> None:
    v = sys.version_info
    ok = (v.major, v.minor) == (3, 10)
    record("python.version", ok, f"{v.major}.{v.minor}.{v.micro} ({sys.executable})",
           "INFO" if ok else "WARNING")


# ---------------------------------------------------------------- torch/CUDA
def check_torch() -> None:
    try:
        import torch
    except Exception as e:
        record("torch.import", False, f"import failed: {e}", "FAIL")
        return
    record("torch.version", True, torch.__version__)
    cuda_ok = torch.cuda.is_available()
    if cuda_ok:
        name = torch.cuda.get_device_name(0)
        cap = ".".join(map(str, torch.cuda.get_device_capability(0)))
        record("torch.cuda", True, f"{name} (SM {cap}, devices={torch.cuda.device_count()})")
    else:
        record("torch.cuda", False,
               "CUDA unavailable - service will fall back to CPU (unverified path)", "WARNING")


# ---------------------------------------------------------------- 关键 import
def check_parselmouth() -> None:
    """旧主机曾因 Commit 耗尽在此报『页面文件太小』DLL 错。"""
    try:
        import parselmouth  # noqa: F401
        record("parselmouth.import", True, "ok")
    except ImportError as e:
        msg = str(e)
        if "页面文件太小" in msg or "page file" in msg.lower():
            record("parselmouth.import", False,
                   f"DLL load failed (commit pressure signature): {msg}", "FAIL")
        else:
            record("parselmouth.import", False, str(e), "FAIL")


def check_edge_tts() -> None:
    try:
        import edge_tts  # noqa: F401
        record("edge-tts.import", True, getattr(edge_tts, "__version__", "ok"))
    except Exception as e:
        record("edge-tts.import", False, str(e), "FAIL")


def check_sounddevice() -> None:
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        outs = [d["name"] for d in devs if d.get("max_output_channels", 0) > 0]
        if outs:
            record("sounddevice.output_devices", True, f"{len(outs)} output device(s): {outs[0]}")
        else:
            record("sounddevice.output_devices", False, "no output device found", "WARNING")
    except Exception as e:
        record("sounddevice", False, str(e), "WARNING")


# ---------------------------------------------------------------- 模型
REQUIRED_MODELS = [
    ("firefly-chinese.pth", "models/firefly/firefly-chinese.pth",
     "f59e5cb51343c84a412bc9f82397563e9c33380c5372d1f0b4b1b84579ae8ca3"),
    ("firefly-chinese_v2.index", "models/firefly/firefly-chinese_v2.index",
     "4b708b783ebbf1aae24a138e778387982fa02946c7460e0b7820079405c501fd"),
    ("hubert_base.pt", "models/hubert_base.pt",
     "f54b40fd2802423a5643779c4861af1e9ee9c1564dc9d32f54f20b5ffba7db96"),
    ("rmvpe.pt", "models/rmvpe.pt",
     "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193"),
]


def check_models(sha: bool) -> None:
    for name, rel, expect in REQUIRED_MODELS:
        p = MODULE_ROOT / rel
        if not p.exists():
            record(f"model.{name}", False, f"missing: {rel} (see models/README.md)", "FAIL")
            continue
        if not sha:
            record(f"model.{name}", True, f"present ({p.stat().st_size} bytes, sha not checked)")
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        got = h.hexdigest()
        record(f"model.{name}", got == expect,
               f"sha256 {'MATCH' if got == expect else f'MISMATCH got={got}'}")


# ---------------------------------------------------------------- 服务/端口
def check_port_and_health() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    listening = s.connect_ex(("127.0.0.1", 8300)) == 0
    s.close()
    if not listening:
        record("service.port8300", None, "not listening (service not started - informational)", "SKIP")
        return
    record("service.port8300", True, "listening")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8300/health", timeout=5) as r:
            body = json.loads(r.read().decode("utf-8"))
        record("service.health", body.get("status") == "ok", json.dumps(body, ensure_ascii=False))
    except Exception as e:
        record("service.health", False, f"port open but /health failed: {e}", "FAIL")


# ---------------------------------------------------------------- Commit 状态
def check_commit() -> None:
    """系统提交内存（Commit Limit / Commit Free）。只看物理 RAM 会漏报。"""
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    if sys.platform != "win32":
        record("system.commit", None, "non-Windows: skipped", "SKIP")
        return
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        record("system.commit", False, "GlobalMemoryStatusEx failed", "WARNING")
        return
    limit_gb = st.ullTotalPageFile / 2**30      # = Commit Limit
    free_gb = st.ullAvailPageFile / 2**30       # = Commit Free
    charge_gb = (st.ullTotalPageFile - st.ullAvailPageFile) / 2**30
    ratio = free_gb / limit_gb if limit_gb else 0.0
    detail = (f"CommitLimit={limit_gb:.1f}GB Charge={charge_gb:.1f}GB "
              f"Free={free_gb:.1f}GB ({ratio:.0%})")
    if ratio < 0.05:
        record("system.commit", False,
               f"{detail} — CRITICAL: 旧主机实测该区间出现过 parselmouth DLL / "
               "cuDNN engine / numpy alloc 三类失败，先释放内存再启动服务", "FAIL")
    elif ratio < 0.15:
        record("system.commit", False,
               f"{detail} — WARNING: commit 余量偏低（旧主机 0.8/40.6GB 时推理实际失败过）", "WARNING")
    else:
        record("system.commit", True, detail)


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-only", action="store_true")
    ap.add_argument("--json", action="store_true", help="print JSON report at end")
    args = ap.parse_args()

    print(f"=== Firefly Voice environment check — {MODULE_ROOT} ===")
    check_os()
    check_python()
    if not args.models_only:
        check_torch()
        check_parselmouth()
        check_edge_tts()
        check_sounddevice()
        check_port_and_health()
        check_commit()
    check_models(sha=not args.models_only)

    fails = [c for c in CHECKS if c["level"] == "FAIL"]
    warns = [c for c in CHECKS if c["level"] == "WARNING"]
    print(f"=== summary: {len(CHECKS)} checks, {len(fails)} FAIL, {len(warns)} WARNING ===")
    if args.json:
        print(json.dumps(CHECKS, ensure_ascii=False, indent=1))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
