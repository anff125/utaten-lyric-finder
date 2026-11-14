"""Windows CUDA DLL path registration and resilient preloading."""

import ctypes
import os
from pathlib import Path
import sys
import time

from utaten_finder.core import config

_PRELOADED_CUDA_DLLS: set[str] = set()
_CUDA_DLL_PRIORITY_PREFIXES = (
    (0, ("cublaslt", "nvrtc-builtins", "cudnn64", "cudnn_graph", "cudnn_ops")),
    (1, ("cublas64", "cudnn_heuristic", "cudnn_cnn", "cudnn_adv")),
)


def _suppress_windows_loader_popups() -> None:
    """Prevent Windows from showing modal Bad Image popups during DLL probing."""
    try:
        kernel32 = ctypes.windll.kernel32
        err_flags = 0x0001 | 0x8000  # SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX
        prev_mode = kernel32.SetErrorMode(err_flags)
        kernel32.SetErrorMode(prev_mode | err_flags)
    except Exception:
        pass


def _cuda_dll_load_priority(dll_path: Path) -> tuple[int, str]:
    """Return sort key so leaf CUDA DLLs load before DLLs that depend on them."""
    lower_name = dll_path.name.lower()
    for priority, prefixes in _CUDA_DLL_PRIORITY_PREFIXES:
        if lower_name.startswith(prefixes):
            return (priority, lower_name)
    return (2, lower_name)


def _preload_cuda_dll(dll_file: Path, frac: float, report_progress: bool) -> None:
    """Preload a CUDA DLL, retrying briefly if Smart App Control is scanning it."""
    dll_key = str(dll_file)
    if dll_key in _PRELOADED_CUDA_DLLS:
        return

    max_retries = 6
    for attempt in range(max_retries + 1):
        try:
            ctypes.CDLL(dll_key)
            _PRELOADED_CUDA_DLLS.add(dll_key)
            return
        except OSError as err:
            if getattr(err, "winerror", None) != 577:
                return
            if attempt >= max_retries:
                print(
                    f"⚠️ Windows 智慧應用程式控制暫時攔截 {dll_file.name} (WinError 577)，"
                    "稍後若 GPU 初始化失敗將自動切換至 CPU 模式。"
                )
                return
            retry_num = attempt + 1
            if report_progress:
                config.report_startup_progress(
                    frac,
                    f"等待 Windows 安全性驗證 CUDA 函式庫 ({dll_file.name}, 重試 {retry_num}/{max_retries})...",
                )
            time.sleep(2.5)
        except Exception:
            return


def prepare_windows_cuda_dll_paths(report_progress: bool = False) -> None:
    """Register pip-installed NVIDIA DLL folders so Windows loader can resolve them."""
    if os.name != "nt":
        return

    _suppress_windows_loader_popups()

    candidate_bases = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidate_bases.append(Path(meipass) / "nvidia")
    else:
        candidate_bases.append(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia")
        candidate_bases.append(
            config.PROJECT_ROOT / ".venv" / "Lib" / "site-packages" / "nvidia"
        )
        try:
            import site

            for sp in site.getsitepackages():
                candidate_bases.append(Path(sp) / "nvidia")
        except Exception:
            pass

    dll_dirs = []
    for base in candidate_bases:
        if not base.is_dir():
            continue
        for bin_dir in base.rglob("bin"):
            if bin_dir.is_dir() and bin_dir not in dll_dirs:
                dll_dirs.append(bin_dir)

    dll_files = []
    for dll_dir in dll_dirs:
        dir_str = str(dll_dir)
        try:
            os.add_dll_directory(dir_str)
        except Exception:
            pass

        # ctranslate2 的 C++ 核心在 Windows 上依賴 PATH 尋找 cublas64_12.dll
        current_path = os.environ.get("PATH", "")
        if dir_str.lower() not in current_path.lower():
            os.environ["PATH"] = dir_str + os.pathsep + current_path

        for dll_file in dll_dir.glob("*.dll"):
            if dll_file.name.lower().endswith(".alt.dll"):
                continue
            if dll_file not in dll_files:
                dll_files.append(dll_file)

    dll_files.sort(key=_cuda_dll_load_priority)

    total_dlls = len(dll_files)
    for idx, dll_file in enumerate(dll_files, start=1):
        frac = 0.30 + 0.20 * (idx / total_dlls) if total_dlls > 0 else 0.50
        if report_progress and total_dlls > 0:
            config.report_startup_progress(
                frac, f"預載入 CUDA 加速函式庫 ({idx}/{total_dlls})..."
            )
        # 預先加載 DLL 至進程空間，確保後續 LoadLibrary 調用直接命中
        _preload_cuda_dll(dll_file, frac, report_progress)
