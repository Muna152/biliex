"""检查本机 CUDA 运行时到底能不能被 ctranslate2 加载 —— GPU 用不上时先跑这个。

为什么不能只看 `asr status` 或 `ctranslate2.get_cuda_device_count()`：
那两者在"缺 DLL"时**照样给正面信号**（设备计数走的是驱动 API `nvcuda.dll`，
由显卡驱动装在 System32，一直都在）。真正会卡的只有加载 cuBLAS 那一步，
而这一步必须用 **`kernel32!LoadLibraryA`** 来判 —— ctranslate2 内部就是这个名字加载的。

为什么不能用 `ctypes.WinDLL` 来判（实测踩过）：
* `ctypes` 走 `LoadLibraryExW(..., LOAD_WITH_ALTERED_SEARCH_PATH)`，
  它**认** `os.add_dll_directory`，而 `LoadLibraryA` **不认** —— 两者结论可以完全相反；
* 更阴的是它会**把 DLL 先加载进进程**，之后 ctranslate2 按名字加载直接命中已加载模块，
  于是"根本没配置好"也会显示成成功。

用法::

    python tools/cuda_dll_check.py            # 人类可读
    python tools/cuda_dll_check.py --json     # 给 agent / 脚本

退出码：0 = cuBLAS 能被 LoadLibraryA 加载；1 = 不能（此时用 GPU 必然会退回 CPU）。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from biliex import asr  # noqa: E402

# ctranslate2 会按名字加载的库（前两个是 ctranslate2.dll 写死的动态加载名，
# 第三个是它自带的 cudnn 转发层，会把后面四个子库拉起来）
REQUIRED = ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll")

WINERR = {
    2: "ERROR_FILE_NOT_FOUND",
    3: "ERROR_PATH_NOT_FOUND",
    126: "ERROR_MOD_NOT_FOUND",
    127: "ERROR_PROC_NOT_FOUND",
    193: "ERROR_BAD_EXE_FORMAT",
    8: "ERROR_NOT_ENOUGH_MEMORY",
    1114: "ERROR_DLL_INIT_FAILED",
}


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryA.restype = ctypes.c_void_p
    kernel32.LoadLibraryA.argtypes = [ctypes.c_char_p]
    return kernel32


def loadlib(name: str, kernel32) -> tuple[bool, str]:
    """与 ctranslate2 完全相同的加载调用。"""
    ctypes.set_last_error(0)
    handle = kernel32.LoadLibraryA(name.encode("ascii"))
    if handle:
        return True, "ok"
    err = ctypes.get_last_error()
    return False, f"err={err}({WINERR.get(err, '?')})"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("这个检查只针对 Windows（其它平台的动态库搜索是另一套）")
        return 0

    kernel32 = _kernel32()
    found = asr.cuda_runtime_dirs()
    before = {name: loadlib(name, kernel32) for name in REQUIRED}
    registered = asr.ensure_cuda_dll_dirs()
    after = {name: loadlib(name, kernel32) for name in REQUIRED}

    report: dict = {
        "cuda_runtime_dirs_found": [str(d) for d in found],
        "cuda_runtime_dirs_registered": registered,
        "load_library_a": {
            name: {"before": before[name], "after": after[name]} for name in REQUIRED
        },
        "ok": all(after[name][0] for name in REQUIRED),
        "install_hint": asr.CUDA_INSTALL_HINT,
    }

    try:
        import ctranslate2  # noqa: PLC0415

        report["ctranslate2_version"] = ctranslate2.__version__
        report["cuda_device_count"] = int(ctranslate2.get_cuda_device_count())
        report["supported_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cuda"))
    except Exception as exc:  # noqa: BLE001
        report["ctranslate2_error"] = f"{type(exc).__name__}: {exc}"

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("发现的 CUDA 运行时目录：" + (f"{len(found)} 个" if found else "**没有**"))
        for d in found:
            print(f"    {d}")
        print("cuda_runtime_dirs() 之前 / ensure_cuda_dll_dirs() 之后的 LoadLibraryA：")
        for name in REQUIRED:
            b_ok, b_err = before[name]
            a_ok, a_err = after[name]
            print(
                f"    {name:<22} 前 {'OK    ' if b_ok else 'FAIL  '} "
                f"后 {'OK' if a_ok else 'FAIL'}   ({'—' if b_ok else b_err} → {'—' if a_ok else a_err})"
            )
        if "cuda_device_count" in report:
            print(
                f"ctranslate2 {report['ctranslate2_version']}："
                f"可见 CUDA 设备 {report['cuda_device_count']} 个；"
                f"可用精度 {', '.join(report['supported_compute_types'])}"
            )
        if report["ok"]:
            print("\n结论：cuBLAS/cuDNN 能被加载 → GPU 路径**具备**跑通条件。")
        else:
            print(
                "\n结论：cuBLAS/cuDNN **加载不了** → 用 GPU 必然退回 CPU。\n"
                "      可见设备数是驱动 API 给的，不代表能用。\n"
                f"      补法：{asr.CUDA_INSTALL_HINT}"
            )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
