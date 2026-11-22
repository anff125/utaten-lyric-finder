"""Audio capture loop and faster-whisper ASR worker."""

from datetime import datetime
import functools
import inspect
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, cast
import wave

from utaten_finder.audio.cuda_loader import prepare_windows_cuda_dll_paths
from utaten_finder.core import config, events
from utaten_finder.lyrics.matching import filter_asr_segments
from utaten_finder.lyrics.trace import sanitize_trace_slug

with config.startup_progress_stage(0.20, 0.30, "載入音訊重採樣模組 (SciPy)..."):
    from scipy.signal import resample_poly

    from utaten_finder.audio.process_loopback import (
        apply_vocal_bandpass,
        downmix_to_mono,
    )

# 在導入 faster_whisper / ctranslate2 前先設定好 DLL 路徑
prepare_windows_cuda_dll_paths(report_progress=True)

try:
    with config.startup_progress_stage(
        0.50, 0.62, "載入 Faster-Whisper 語音辨識引擎..."
    ):
        import numpy as np
        from faster_whisper import WhisperModel

    ASR_CORE_DEPS_AVAILABLE = True
    ASR_DEPS_ERROR = ""
except Exception as _import_error:
    ASR_CORE_DEPS_AVAILABLE = False
    ASR_DEPS_ERROR = str(_import_error)

config.report_startup_progress(0.64, "檢查系統音訊擷取裝置 (WASAPI / Loopback)...")

try:
    import pyaudiowpatch as pyaudio

    PYAUDIO_LOOPBACK_AVAILABLE = True
except Exception:
    pyaudio = None
    PYAUDIO_LOOPBACK_AVAILABLE = False

try:
    import soundcard as sc

    SOUNDCARD_AVAILABLE = True
except Exception:
    sc = None
    SOUNDCARD_AVAILABLE = False

try:
    from utaten_finder.audio.process_loopback import (
        SpotifyProcessAudioCapture,
        find_spotify_process_id,
        is_process_loopback_supported,
    )

    PROCESS_LOOPBACK_AVAILABLE = is_process_loopback_supported()
except Exception:
    PROCESS_LOOPBACK_AVAILABLE = False

_asr_thread = None
_asr_started = False
_asr_disabled_logged = False
_asr_callback = None
_asr_listening_active = True
_asr_stop_event = threading.Event()
_asr_active_device = ""
_asr_fallback_cpu = False

AUDIO_LEVEL_MIN_INTERVAL_SECONDS = 0.08
AUDIO_METER_CHUNK_SECONDS = 0.08


def compute_audio_level(samples: Any) -> tuple[float, float, bool]:
    """Compute (rms, peak, silent) energy metrics in [0.0, 1.0] for VU metering."""
    if not ASR_CORE_DEPS_AVAILABLE:
        return (0.0, 0.0, True)

    arr = np.asarray(samples, dtype=np.float32)
    if arr.size == 0:
        return (0.0, 0.0, True)

    arr = np.clip(np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0)
    rms = float(np.sqrt(np.mean(np.square(arr))))
    peak = float(np.max(np.abs(arr)))
    silent = rms < 0.0002 and peak < 0.001
    return (min(1.0, max(0.0, rms)), min(1.0, max(0.0, peak)), silent)


class AudioLevelEmitter:
    """Throttled EventBus publisher for real-time audio VU meter readings."""

    def __init__(self, min_interval: float = AUDIO_LEVEL_MIN_INTERVAL_SECONDS) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._last_emit_at = 0.0
        self._last_source = ""
        self._has_emitted = False

    def emit_samples(
        self, samples: Any, source_name: str, *, force: bool = False
    ) -> bool:
        rms, peak, silent = compute_audio_level(samples)
        return self.emit_values(rms, peak, silent, source_name, force=force)

    def emit_values(
        self,
        rms: float,
        peak: float,
        silent: bool,
        source_name: str,
        *,
        force: bool = False,
    ) -> bool:
        now = time.monotonic()
        if (
            not force
            and self._has_emitted
            and source_name == self._last_source
            and (now - self._last_emit_at) < self._min_interval
        ):
            return False

        self._has_emitted = True
        self._last_emit_at = now
        self._last_source = source_name
        clamped_rms = round(min(1.0, max(0.0, float(rms))), 4)
        clamped_peak = round(min(1.0, max(0.0, float(peak))), 4)
        events.bus.emit(
            "audio_level",
            {
                "rms": clamped_rms,
                "peak": clamped_peak,
                "silent": bool(silent),
                "source_name": source_name,
            },
        )
        return True


def _emit_silent_audio_level(source_name: str = "") -> None:
    events.bus.emit(
        "audio_level",
        {"rms": 0.0, "peak": 0.0, "silent": True, "source_name": source_name},
    )


class AsrWavRecorder:
    """Thread-safe 16kHz mono 16-bit PCM WAV recorder for offline ASR evaluation."""

    def __init__(
        self,
        recordings_dir: Path | None = None,
        sample_rate: int | None = None,
        max_files: int | None = None,
    ) -> None:
        self._recordings_dir = (
            Path(recordings_dir) if recordings_dir is not None else None
        )
        self._sample_rate = int(sample_rate) if sample_rate is not None else None
        self._max_files = max_files
        self._lock = threading.Lock()
        self._writer: wave.Wave_write | None = None
        self._current_path: Path | None = None
        self._current_slug: str = ""

    @property
    def current_path(self) -> Path | None:
        with self._lock:
            return self._current_path

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._writer is not None

    def _resolve_dir(self) -> Path:
        return (
            self._recordings_dir
            if self._recordings_dir is not None
            else config.RECORDINGS_DIR
        )

    def _resolve_sample_rate(self) -> int:
        if self._sample_rate is not None:
            return max(8000, self._sample_rate)
        return max(8000, int(getattr(config, "ASR_SAMPLE_RATE", 16000)))

    def _resolve_max_files(self) -> int:
        if self._max_files is not None:
            return int(self._max_files)
        return int(getattr(config, "ASR_RECORD_WAV_MAX_FILES", 20))

    def _prune_old_files_locked(self, rec_dir: Path) -> None:
        max_files = self._resolve_max_files()
        if max_files <= 0:
            return
        try:
            wav_files = list(rec_dir.glob("*.wav"))
        except OSError:
            return
        if len(wav_files) <= max_files:
            return

        def _sort_key(path: Path) -> tuple[float, str]:
            try:
                return (path.stat().st_mtime, path.name)
            except OSError:
                return (0.0, path.name)

        wav_files.sort(key=_sort_key)
        excess = len(wav_files) - max_files
        current_resolved = (
            self._current_path.resolve() if self._current_path is not None else None
        )
        for old_path in wav_files:
            if excess <= 0:
                break
            try:
                if (
                    current_resolved is not None
                    and old_path.resolve() == current_resolved
                ):
                    continue
                old_path.unlink()
                excess -= 1
            except OSError:
                continue

    def _close_locked(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        self._current_path = None
        self._current_slug = ""

    def _open_locked(self, slug: str) -> bool:
        rec_dir = self._resolve_dir()
        safe_slug = sanitize_trace_slug(slug or "asr_capture")
        try:
            rec_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = f"{stamp}_{safe_slug}"
            candidate = rec_dir / f"{base_name}.wav"
            suffix = 2
            while candidate.exists():
                candidate = rec_dir / f"{base_name}_{suffix}.wav"
                suffix += 1
            writer = wave.open(str(candidate), "wb")
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(self._resolve_sample_rate())
        except Exception as exc:
            print(f"⚠️ [AsrWav] 無法建立 WAV 錄音檔: {exc}")
            self._writer = None
            self._current_path = None
            self._current_slug = ""
            return False

        self._writer = writer
        self._current_path = candidate
        self._current_slug = safe_slug
        self._prune_old_files_locked(rec_dir)
        print(f"💾 [AsrWav] 開始錄製 16kHz WAV: {candidate}")
        return True

    def write_step(
        self,
        mono_float32: Any,
        *,
        track_slug: str = "",
        enabled: bool | None = None,
    ) -> bool:
        """Append mono float32 samples as 16-bit PCM frames to the active WAV file."""
        is_enabled = (
            bool(enabled)
            if enabled is not None
            else bool(getattr(config, "ASR_RECORD_WAV_ENABLED", False))
        )
        with self._lock:
            if not is_enabled:
                if self._writer is not None:
                    self._close_locked()
                return False

            arr = np.asarray(mono_float32, dtype=np.float32).reshape(-1)
            if arr.size == 0:
                return False

            norm_slug = sanitize_trace_slug(track_slug or "asr_capture")
            if self._writer is None or norm_slug != self._current_slug:
                self._close_locked()
                if not self._open_locked(norm_slug):
                    return False

            clipped = np.clip(
                np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0),
                -1.0,
                1.0,
            )
            pcm16 = np.round(clipped * 32767.0).astype("<i2")
            try:
                assert self._writer is not None
                self._writer.writeframes(pcm16.tobytes())
                return True
            except Exception as exc:
                print(f"⚠️ [AsrWav] 寫入 WAV 失敗: {exc}")
                self._close_locked()
                return False

    def close(self, reason: str = "stopped") -> None:
        """Close the active WAV writer cleanly if open."""
        with self._lock:
            if self._writer is not None:
                path = self._current_path
                self._close_locked()
                if path is not None:
                    print(f"💾 [AsrWav] 已關閉錄音檔 ({reason}): {path}")


_asr_context_provider: Callable[[], dict[str, Any] | None] | None = None
_asr_context_lock = threading.Lock()
_wav_recorder = AsrWavRecorder()


def set_asr_context_provider(
    provider: Callable[[], dict[str, Any] | None] | None,
) -> None:
    """Register a thread-safe callback providing live lyrics context for ASR."""
    global _asr_context_provider
    with _asr_context_lock:
        _asr_context_provider = provider


def _get_current_asr_context() -> dict[str, Any]:
    with _asr_context_lock:
        provider = _asr_context_provider
    if provider is None:
        return {}
    try:
        ctx = provider()
        return dict(ctx) if isinstance(ctx, dict) else {}
    except Exception:
        return {}


def close_asr_wav_recording(reason: str = "stopped") -> None:
    """Close any active 16kHz WAV recording file cleanly."""
    _wav_recorder.close(reason=reason)


def set_asr_listening(active: bool) -> None:
    """Enable or pause ASR transcription without tearing down the loaded model."""
    global _asr_listening_active
    _asr_listening_active = bool(active)
    if not _asr_listening_active:
        close_asr_wav_recording("paused")
        _emit_silent_audio_level()


def request_asr_stop() -> None:
    """Signal the ASR capture loops to terminate cleanly."""
    global _asr_listening_active
    _asr_listening_active = False
    _asr_stop_event.set()
    close_asr_wav_recording("stopped")
    _emit_silent_audio_level()


def get_asr_device_mode() -> str:
    """Return the active inference device mode ('cuda', 'cpu', or '')."""
    return _asr_active_device


def _resolve_model_device(model_obj: Any, fallback: str = "cpu") -> str:
    inner = getattr(model_obj, "model", None)
    detected = getattr(inner, "device", None)
    if isinstance(detected, str) and detected.strip():
        return detected.strip().lower()
    if config.ASR_DEVICE in {"cuda", "cpu"}:
        return config.ASR_DEVICE
    return fallback


def _emit_asr_status(
    ready: bool, device_mode: str, error: str = "", fallback: bool = False
) -> None:
    events.bus.emit(
        "asr_status",
        {
            "ready": ready,
            "device_mode": device_mode,
            "model": config.ASR_MODEL_SIZE,
            "error": error,
            "fallback": bool(fallback),
        },
    )


def _safe_float(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return round(numeric, digits)


@functools.lru_cache(maxsize=64)
def _inspect_callback_accepts_meta(callback: Any) -> bool:
    try:
        sig = inspect.signature(callback)
    except (ValueError, TypeError):
        return False

    positional_count = 0
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            return True
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_count += 1
    return positional_count >= 2


def _callback_accepts_meta(callback: Any) -> bool:
    if callback is None:
        return False
    try:
        return _inspect_callback_accepts_meta(callback)
    except TypeError:
        return _inspect_callback_accepts_meta.__wrapped__(callback)


def _invoke_asr_callback(
    callback: Any, text: str, asr_meta: dict[str, Any] | None = None
) -> None:
    if callback is None:
        return
    if asr_meta is not None and _callback_accepts_meta(callback):
        callback(text, asr_meta)
    else:
        callback(text)


WHISPER_REQUIRED_MODEL_FILES: tuple[str, ...] = ("model.bin", "config.json")


def find_missing_whisper_model_files(
    model_dir: str | Path | None,
) -> list[str]:
    """Return required faster-whisper model files that are missing or empty in model_dir."""
    if not model_dir:
        return list(WHISPER_REQUIRED_MODEL_FILES)
    base = Path(model_dir)
    try:
        if not base.is_dir():
            return list(WHISPER_REQUIRED_MODEL_FILES)
    except OSError:
        return list(WHISPER_REQUIRED_MODEL_FILES)

    missing: list[str] = []
    for filename in WHISPER_REQUIRED_MODEL_FILES:
        file_path = base / filename
        try:
            if not file_path.is_file() or file_path.stat().st_size <= 0:
                missing.append(filename)
        except OSError:
            missing.append(filename)
    return missing


def is_whisper_model_dir_complete(model_dir: str | Path | None) -> bool:
    """Verify that a local faster-whisper model directory has non-empty model.bin and config.json."""
    return len(find_missing_whisper_model_files(model_dir)) == 0


def format_model_download_troubleshooting_hint(
    model_size: str, cache_dir: str | Path | None = None
) -> str:
    """Return actionable troubleshooting guidance for Hugging Face download/cache failures."""
    cache_hint = (
        f"快取目錄 ({cache_dir})"
        if cache_dir
        else "Hugging Face 快取目錄 (%USERPROFILE%\\.cache\\huggingface\\hub)"
    )
    return (
        f"💡 [排解建議] 若下載或載入 Whisper-{model_size} 卡住、逾時或快取損毀：\n"
        f"   1. 請檢查網路連線，或手動刪除殘缺的 {cache_hint} 後重新啟動。\n"
        "   2. 若遭遇 Hugging Face 匿名下載限流 (Rate Limit / 429)，請至 https://huggingface.co/settings/tokens 建立免費 Read 權限 Token，\n"
        '      並於 PowerShell 設定 `$env:HF_TOKEN="hf_..."` 後再執行 `.\\start.bat`，或將 `HF_TOKEN` 加入 Windows 使用者環境變數。'
    )


def resolve_whisper_model_path(
    model_size_or_path: str,
    download_fn: Callable[..., str] | None = None,
) -> str:
    """Resolve and validate local Whisper model directory, re-downloading if cache is incomplete."""
    if download_fn is None:
        import faster_whisper.utils

        download_fn = faster_whisper.utils.download_model

    if os.path.isdir(model_size_or_path):
        missing_local = find_missing_whisper_model_files(model_size_or_path)
        if missing_local:
            hint = format_model_download_troubleshooting_hint(
                model_size_or_path, model_size_or_path
            )
            raise RuntimeError(
                f"指定的本地 Whisper 模型目錄不完整（缺少或為空: {', '.join(missing_local)}）: "
                f"{model_size_or_path}\n{hint}"
            )
        return model_size_or_path

    cached_path: str | None = None
    missing_cached: list[str] = []
    try:
        cached_path = download_fn(model_size_or_path, local_files_only=True)
        missing_cached = find_missing_whisper_model_files(cached_path)
        if not missing_cached:
            return str(cached_path)
        print(
            f"⚠️ 本地 Whisper-{model_size_or_path} 快取不完整"
            f"（缺少或損毀: {', '.join(missing_cached)}，路徑: {cached_path}），"
            "正在自動重新補全下載..."
        )
    except Exception:
        cached_path = None

    progress_msg = (
        f"本地快取不完整，正在補全下載 Whisper-{model_size_or_path} 模型..."
        if missing_cached
        else f"首次使用，正在下載 Whisper-{model_size_or_path} 模型..."
    )
    config.report_startup_progress(0.68, progress_msg, newline=True)

    try:
        downloaded_path = download_fn(
            model_size_or_path, local_files_only=False
        )
    except Exception as exc:
        hint = format_model_download_troubleshooting_hint(
            model_size_or_path, cached_path
        )
        print(hint)
        raise RuntimeError(
            f"下載 Whisper-{model_size_or_path} 模型失敗: {exc}\n{hint}"
        ) from exc

    missing_after = find_missing_whisper_model_files(downloaded_path)
    if missing_after:
        hint = format_model_download_troubleshooting_hint(
            model_size_or_path, downloaded_path
        )
        print(hint)
        raise RuntimeError(
            f"Whisper-{model_size_or_path} 模型下載後仍缺少必要檔案"
            f" ({', '.join(missing_after)}): {downloaded_path}\n{hint}"
        )

    return str(downloaded_path)


def build_asr_initial_prompt(
    asr_ctx: dict[str, Any] | None, model_name: str | None = None
) -> str:
    """Build upcoming lyric initial_prompt, automatically disabled on distilled models."""
    if not getattr(config, "ASR_INITIAL_PROMPT_ENABLED", True):
        return ""
    active_model = (
        config.ASR_MODEL_SIZE if model_name is None else str(model_name)
    )
    if config.is_distilled_asr_model(active_model):
        return ""
    if not isinstance(asr_ctx, dict):
        return ""
    raw_prompt = str(asr_ctx.get("initial_prompt") or "").strip()
    max_prompt_chars = int(
        getattr(config, "ASR_INITIAL_PROMPT_MAX_CHARS", 120)
    )
    if raw_prompt and max_prompt_chars > 0:
        return raw_prompt[:max_prompt_chars]
    return ""


def build_transcribe_kwargs(
    asr_ctx: dict[str, Any] | None = None,
    *,
    device_mode: str = "",
    model_name: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Construct faster-whisper transcribe kwargs and effective initial_prompt string."""
    initial_prompt = build_asr_initial_prompt(asr_ctx, model_name=model_name)
    beam_size = int(getattr(config, "ASR_BEAM_SIZE", 5))
    best_of = int(getattr(config, "ASR_BEST_OF", 5))
    if device_mode == "cpu":
        beam_size = min(beam_size, 2)
        best_of = min(best_of, 2)
    condition_prev = bool(
        getattr(config, "ASR_CONDITION_ON_PREVIOUS_TEXT", False)
    )

    transcribe_kwargs: dict[str, Any] = {
        "language": config.ASR_LANGUAGE,
        "vad_filter": False,
        "beam_size": max(1, beam_size),
        "best_of": max(1, best_of),
        "condition_on_previous_text": condition_prev,
    }
    if initial_prompt:
        transcribe_kwargs["initial_prompt"] = initial_prompt
    return transcribe_kwargs, initial_prompt


def _asr_worker_loop(
    on_recognized_text, init_done_event: threading.Event | None = None
):
    global _asr_disabled_logged, _asr_active_device, _asr_started, _asr_fallback_cpu

    prepare_windows_cuda_dll_paths()

    if not ASR_CORE_DEPS_AVAILABLE:
        _asr_active_device = ""
        _asr_fallback_cpu = False
        _emit_asr_status(False, "", error=ASR_DEPS_ERROR, fallback=False)
        config.report_startup_progress(
            1.0, f"未啟用 ASR：缺少套件 ({ASR_DEPS_ERROR})", newline=True
        )
        print(f"ℹ️ 未啟用 ASR：缺少套件 ({ASR_DEPS_ERROR})")
        if init_done_event is not None:
            init_done_event.set()
        _asr_started = False
        return

    if not (PYAUDIO_LOOPBACK_AVAILABLE or SOUNDCARD_AVAILABLE):
        _asr_active_device = ""
        _asr_fallback_cpu = False
        _emit_asr_status(False, "", error="缺少音訊錄製套件", fallback=False)
        config.report_startup_progress(
            1.0, "未啟用 ASR：缺少音訊錄製套件", newline=True
        )
        print("ℹ️ 未啟用 ASR：需要 pyaudiowpatch 或 soundcard 其中之一")
        if init_done_event is not None:
            init_done_event.set()
        _asr_started = False
        return

    model_path = config.ASR_MODEL_SIZE
    try:
        # 開啟 faster-whisper 的下載進度條
        import faster_whisper.utils
        from tqdm.auto import tqdm

        faster_whisper.utils.disabled_tqdm = tqdm

        model_path = resolve_whisper_model_path(
            config.ASR_MODEL_SIZE,
            download_fn=faster_whisper.utils.download_model,
        )

        with config.startup_progress_stage(
            0.70, 0.85, f"正在加載 Whisper-{config.ASR_MODEL_SIZE} 模型..."
        ):
            model = WhisperModel(
                model_size_or_path=model_path,
                device=config.ASR_DEVICE,
                compute_type=config.ASR_COMPUTE_TYPE,
            )

        # 暖身推理以確認 CUDA / cuBLAS 真正可用（若缺少 DLL 或 GPU 異常，在此立即攔截並 fallback）
        with config.startup_progress_stage(
            0.86, 0.98, f"正在執行 Whisper-{config.ASR_MODEL_SIZE} 暖身推理..."
        ):
            dummy_audio = np.zeros(16000, dtype=np.float32)
            warmup_segs, _ = model.transcribe(dummy_audio, language="ja")
            _ = list(warmup_segs)

        _asr_active_device = _resolve_model_device(model)
        _asr_fallback_cpu = False
        _emit_asr_status(True, _asr_active_device, fallback=False)
        config.report_startup_progress(
            1.0, f"Whisper-{config.ASR_MODEL_SIZE} 模型已就緒！", newline=True
        )
        print(f"✅ Whisper-{config.ASR_MODEL_SIZE} 模型已加載 ({_asr_active_device})")
    except Exception as e:
        error_text = str(e)
        # If CUDA runtime/DLL is not available, retry once on CPU to keep ASR usable.
        if (
            "cublas" in error_text.lower()
            or "cudnn" in error_text.lower()
            or "cannot be loaded" in error_text.lower()
            or "loadlibrary" in error_text.lower()
            or "cuda" in error_text.lower()
        ):
            config.report_startup_progress(
                0.88, "GPU 載入失敗，改用 CPU 模式重試...", newline=True
            )
            print(f"⚠️ GPU 載入或推理失敗，改用 CPU 模式重試: {e}")
            try:
                with config.startup_progress_stage(
                    0.88,
                    0.98,
                    f"正在以 CPU 模式加載 Whisper-{config.ASR_MODEL_SIZE}...",
                ):
                    model = WhisperModel(
                        model_size_or_path=model_path,
                        device="cpu",
                        compute_type="int8",
                    )
                _asr_active_device = "cpu"
                _asr_fallback_cpu = True
                _emit_asr_status(True, "cpu", fallback=True)
                config.report_startup_progress(
                    1.0,
                    f"Whisper-{config.ASR_MODEL_SIZE} (CPU 模式) 已就緒！",
                    newline=True,
                )
                print(f"✅ Whisper-{config.ASR_MODEL_SIZE} 已以 CPU 模式加載")
            except Exception as cpu_e:
                cpu_err_text = str(cpu_e)
                _asr_active_device = ""
                _asr_fallback_cpu = False
                _emit_asr_status(False, "", error=cpu_err_text, fallback=False)
                config.report_startup_progress(
                    1.0, "CPU 模式也無法加載 Whisper 模型", newline=True
                )
                print(f"❌ CPU 模式也無法加載 Whisper 模型: {cpu_e}")
                if "HF_TOKEN" not in cpu_err_text:
                    print(
                        format_model_download_troubleshooting_hint(
                            config.ASR_MODEL_SIZE, model_path
                        )
                    )
                if init_done_event is not None:
                    init_done_event.set()
                _asr_started = False
                return
        else:
            _asr_active_device = ""
            _asr_fallback_cpu = False
            _emit_asr_status(False, "", error=error_text, fallback=False)
            config.report_startup_progress(
                1.0, "無法加載 Whisper 模型", newline=True
            )
            print(f"❌ 無法加載 Whisper 模型: {e}")
            if "HF_TOKEN" not in error_text:
                print(
                    format_model_download_troubleshooting_hint(
                        config.ASR_MODEL_SIZE, model_path
                    )
                )
            if init_done_event is not None:
                init_done_event.set()
            _asr_started = False
            return
    finally:
        if init_done_event is not None:
            init_done_event.set()

    def _resample_if_needed(samples, in_rate, out_rate):
        if in_rate == out_rate:
            return samples
        if samples.size == 0:
            return samples

        gcd_val = math.gcd(in_rate, out_rate)
        up = out_rate // gcd_val
        down = in_rate // gcd_val
        resampled_audio = resample_poly(samples, up, down)
        return resampled_audio.astype(np.float32)

    def _fit_audio_length(samples, target_length):
        if target_length <= 0:
            return samples[:0]
        if samples.size == target_length:
            return samples
        if samples.size > target_length:
            return samples[:target_length]

        padded = np.zeros(target_length, dtype=np.float32)
        padded[: samples.size] = samples
        return padded

    def _advance_sliding_window(audio_buffer, new_data, step_frames):
        new_data = _fit_audio_length(
            np.asarray(new_data, dtype=np.float32), step_frames
        )
        audio_buffer = np.roll(audio_buffer, -step_frames)
        audio_buffer[-step_frames:] = new_data
        return audio_buffer

    source_name = ""
    sample_rate = config.ASR_SAMPLE_RATE
    block_seconds = config.ASR_BLOCK_SECONDS
    step_seconds = block_seconds / 2
    block_frames = int(block_seconds * sample_rate)
    step_frames = int(step_seconds * sample_rate)
    recognition_count = 0
    chunk_counter = 0
    silent_wav_steps = 0
    level_emitter = AudioLevelEmitter()

    def _record_captured_step(step_samples: Any) -> None:
        nonlocal silent_wav_steps
        wav_enabled = bool(getattr(config, "ASR_RECORD_WAV_ENABLED", False))
        if (
            not _asr_listening_active
            or _asr_stop_event.is_set()
            or not config.ASR_ENABLED
            or not wav_enabled
        ):
            silent_wav_steps = 0
            if _wav_recorder.is_recording:
                reason = "asr_disabled" if not config.ASR_ENABLED else "disabled"
                _wav_recorder.close(reason)
            return

        arr = np.asarray(step_samples, dtype=np.float32)
        if arr.size == 0 or float(np.max(np.abs(arr))) < 1e-4:
            silent_wav_steps += 1
            if not _wav_recorder.is_recording or silent_wav_steps > 1:
                return
        else:
            silent_wav_steps = 0

        ctx = _get_current_asr_context()
        slug = str(ctx.get("track_slug") or "")
        _wav_recorder.write_step(arr, track_slug=slug, enabled=True)

    def _process_audio_chunk(mono_float32):
        nonlocal model, recognition_count, chunk_counter
        global _asr_disabled_logged, _asr_active_device, _asr_fallback_cpu

        if not _asr_listening_active or _asr_stop_event.is_set():
            return

        if not config.ASR_ENABLED:
            if _wav_recorder.is_recording:
                close_asr_wav_recording("asr_disabled")
            if not _asr_disabled_logged:
                print("ℹ️ ASR 自動捲動已關閉，暫停語音辨識。")
                _asr_disabled_logged = True
            return

        _asr_disabled_logged = False

        audio_data = np.clip(mono_float32, -1.0, 1.0).astype(np.float32)
        if getattr(config, "ASR_VOCAL_BANDPASS_ENABLED", False):
            audio_data = apply_vocal_bandpass(
                audio_data,
                sample_rate=sample_rate,
                lowcut_hz=float(
                    getattr(config, "ASR_VOCAL_BANDPASS_LOW_HZ", 80.0)
                ),
                highcut_hz=float(
                    getattr(config, "ASR_VOCAL_BANDPASS_HIGH_HZ", 7600.0)
                ),
            )

        volume = abs(audio_data).mean()
        if volume < 0.0001:
            if config.DEBUG_RECOGNITION:
                print("🔇 [靜音] 目前無音訊輸入，跳過本輪辨識。")
            return

        audio_rms, audio_peak, _ = compute_audio_level(audio_data)

        try:
            asr_ctx = _get_current_asr_context()
            transcribe_kwargs, initial_prompt = build_transcribe_kwargs(
                asr_ctx,
                device_mode=_asr_active_device,
                model_name=config.ASR_MODEL_SIZE,
            )

            t0 = time.perf_counter()
            segments, _ = model.transcribe(audio_data, **transcribe_kwargs)

            try:
                seg_list = [
                    segment for segment in segments if segment is not None
                ]
            except Exception as e:
                error_text = str(e)
                if "cublas" in error_text.lower() or "cuda" in error_text.lower():
                    raise
                print(f"⚠️ Whisper 迭代 segment 時發生錯誤: {e}")
                seg_list = []

            infer_ms = round((time.perf_counter() - t0) * 1000.0, 1)

            segment_details: list[dict[str, Any]] = []
            for segment in seg_list:
                seg_text = str(getattr(segment, "text", "") or "")
                segment_details.append(
                    {
                        "text": seg_text,
                        "start": _safe_float(
                            getattr(segment, "start", None), 3
                        ),
                        "end": _safe_float(getattr(segment, "end", None), 3),
                        "avg_logprob": _safe_float(
                            getattr(segment, "avg_logprob", None), 4
                        ),
                        "no_speech_prob": _safe_float(
                            getattr(segment, "no_speech_prob", None), 4
                        ),
                        "compression_ratio": _safe_float(
                            getattr(segment, "compression_ratio", None), 4
                        ),
                    }
                )

            logprobs = [
                s["avg_logprob"]
                for s in segment_details
                if s["avg_logprob"] is not None
            ]
            no_speeches = [
                s["no_speech_prob"]
                for s in segment_details
                if s["no_speech_prob"] is not None
            ]
            comp_ratios = [
                s["compression_ratio"]
                for s in segment_details
                if s["compression_ratio"] is not None
            ]

            filter_result = filter_asr_segments(
                segment_details,
                lyrics_corpus=str(asr_ctx.get("lyrics_corpus") or ""),
                min_avg_logprob=float(
                    getattr(config, "ASR_MIN_AVG_LOGPROB", -0.90)
                ),
                max_no_speech_prob=float(
                    getattr(config, "ASR_MAX_NO_SPEECH_PROB", 0.60)
                ),
                max_compression_ratio=float(
                    getattr(config, "ASR_MAX_COMPRESSION_RATIO", 1.35)
                ),
            )
            raw_text_before_filter = filter_result["raw_text_before_filter"]
            hallucinations_removed = filter_result["hallucinations_removed"]
            filtered_segments = filter_result["filtered_segments"]
            result_text = filter_result["text"]

            asr_meta: dict[str, Any] = {
                "text": result_text,
                "raw_text_before_filter": raw_text_before_filter,
                "hallucinations_removed": hallucinations_removed,
                "filtered_segments": filtered_segments,
                "segment_filtered": True,
                "initial_prompt": initial_prompt,
                "infer_ms": infer_ms,
                "audio_rms": round(audio_rms, 4),
                "audio_peak": round(audio_peak, 4),
                "device_mode": _asr_active_device,
                "model": config.ASR_MODEL_SIZE,
                "segments": segment_details,
                "avg_logprob_mean": round(sum(logprobs) / len(logprobs), 4)
                if logprobs
                else None,
                "no_speech_prob_max": round(max(no_speeches), 4)
                if no_speeches
                else None,
                "compression_ratio_max": round(max(comp_ratios), 4)
                if comp_ratios
                else None,
            }

            callback = _asr_callback or on_recognized_text
            if result_text:
                recognition_count += 1
                if config.DEBUG_RECOGNITION:
                    print(f"🎤 [#{recognition_count}] 識別: {result_text}")
                _invoke_asr_callback(callback, result_text, asr_meta)
            elif (
                config.MATCH_TRACE_ENABLED
                and callback is not None
                and _callback_accepts_meta(callback)
            ):
                callback("", asr_meta)

        except Exception as e:
            error_text = str(e)
            if "cublas" in error_text.lower() or "cuda" in error_text.lower():
                print(f"⚠️ Whisper GPU 辨識失敗，自動降級切換至 CPU 模式: {e}")
                try:
                    model = WhisperModel(
                        model_size_or_path=model_path,
                        device="cpu",
                        compute_type="int8",
                    )
                    _asr_active_device = "cpu"
                    _asr_fallback_cpu = True
                    _emit_asr_status(True, "cpu", fallback=True)
                    print("✅ 已切換為 CPU 模式運作")
                except Exception as cpu_e:
                    print(f"❌ 切換 CPU 模式失敗: {cpu_e}")
            else:
                print(f"⚠️ Whisper 辨識發生內部錯誤: {e}")

    def _run_spotify_process_loop():
        nonlocal source_name
        last_missing_logged = 0.0
        capture = SpotifyProcessAudioCapture(target_sample_rate=sample_rate)

        def _on_loopback_packet(packet_samples: Any) -> None:
            if _asr_listening_active and not _asr_stop_event.is_set():
                level_emitter.emit_samples(packet_samples, source_name)

        try:
            while not _asr_stop_event.is_set():
                if (
                    config.AUDIO_SOURCE_MODE != "spotify"
                    or not getattr(config, "CAPTURE_SPOTIFY_PROCESS_ONLY", True)
                ):
                    capture.stop()
                    return

                if not capture.is_target_alive():
                    capture.stop()
                    pid = find_spotify_process_id()
                    if pid is None:
                        if _asr_listening_active:
                            level_emitter.emit_values(
                                0.0, 0.0, True, "等待 Spotify 啟動..."
                            )
                        now = time.time()
                        if now - last_missing_logged > 5.0:
                            print("🎧 [Process Loopback] 等待 Spotify 啟動中...")
                            last_missing_logged = now
                        _asr_stop_event.wait(1.0)
                        continue

                    if not capture.start(pid):
                        now = time.time()
                        if now - last_missing_logged > 5.0:
                            print(
                                f"⚠️ [Process Loopback] 初始化 Spotify 音訊捕獲失敗 (PID={pid})，重試中..."
                            )
                            last_missing_logged = now
                        _asr_stop_event.wait(1.0)
                        continue

                    source_name = f"Spotify.exe (PID: {pid})"
                    print(f"🎙️ ASR 已啟動，來源: {source_name} [獨立進程 Loopback]")
                    print(
                        f"🔊 開始錄音，使用滑動視窗 (大小: {block_seconds}s, 步進: {step_seconds}s)..."
                    )

                audio_buffer = np.zeros(block_frames, dtype=np.float32)
                buffered_frames = 0

                while capture.is_target_alive() and not _asr_stop_event.is_set():
                    if (
                        config.AUDIO_SOURCE_MODE != "spotify"
                        or not getattr(config, "CAPTURE_SPOTIFY_PROCESS_ONLY", True)
                    ):
                        capture.stop()
                        return

                    mono_step = capture.read_audio_step(
                        step_frames=step_frames,
                        timeout_ms=int(step_seconds * 1500),
                        on_packet=_on_loopback_packet,
                        stop_event=_asr_stop_event,
                    )
                    _record_captured_step(mono_step)
                    audio_buffer = _advance_sliding_window(
                        audio_buffer, mono_step, step_frames
                    )
                    buffered_frames = min(
                        block_frames, buffered_frames + step_frames
                    )

                    if buffered_frames >= block_frames:
                        _process_audio_chunk(audio_buffer.copy())

                capture.stop()
        finally:
            capture.stop()

    def _run_default_loopback_loop():
        nonlocal source_name

        if PYAUDIO_LOOPBACK_AVAILABLE and pyaudio is not None:
            pyaudio_mod = cast(Any, pyaudio)
            pa = pyaudio_mod.PyAudio()
            stream = None
            try:
                wasapi_info = pa.get_host_api_info_by_type(pyaudio_mod.paWASAPI)
                device = pa.get_device_info_by_index(
                    wasapi_info["defaultOutputDevice"]
                )

                if not device.get("isLoopbackDevice", False):
                    for loopback in pa.get_loopback_device_info_generator():
                        if device["name"] in loopback["name"]:
                            device = loopback
                            break

                device_rate = int(device["defaultSampleRate"])
                device_channels = max(
                    1, min(2, int(device.get("maxInputChannels", 1)))
                )
                meter_frames = max(512, int(device_rate * AUDIO_METER_CHUNK_SECONDS))
                step_device_frames = max(
                    meter_frames, int(device_rate * step_seconds)
                )
                source_name = device["name"]

                stream = pa.open(
                    format=pyaudio_mod.paFloat32,
                    channels=device_channels,
                    rate=device_rate,
                    input=True,
                    frames_per_buffer=meter_frames,
                    input_device_index=int(device["index"]),
                )

                print(f"🎙️ ASR 已啟動，來源: {source_name} [全系統混音 Loopback]")
                print(
                    f"🔊 開始錄音，使用滑動視窗 (大小: {block_seconds}s, 步進: {step_seconds}s)..."
                )

                audio_buffer = np.zeros(block_frames, dtype=np.float32)
                buffered_frames = 0
                step_chunks: list[Any] = []
                step_collected = 0

                while not _asr_stop_event.is_set():
                    if (
                        config.AUDIO_SOURCE_MODE == "spotify"
                        and getattr(
                            config, "CAPTURE_SPOTIFY_PROCESS_ONLY", True
                        )
                        and PROCESS_LOOPBACK_AVAILABLE
                    ):
                        break

                    raw = stream.read(
                        meter_frames, exception_on_overflow=False
                    )
                    data = np.frombuffer(raw, dtype=np.float32)
                    if device_channels > 1:
                        data = downmix_to_mono(data, device_channels)

                    if _asr_listening_active and not _asr_stop_event.is_set():
                        level_emitter.emit_samples(data, source_name)

                    step_chunks.append(data)
                    step_collected += data.size
                    if step_collected < step_device_frames:
                        continue

                    combined = (
                        np.concatenate(step_chunks)
                        if len(step_chunks) > 1
                        else step_chunks[0]
                    )
                    step_data = combined[:step_device_frames]
                    remainder = combined[step_device_frames:]
                    step_chunks = [remainder] if remainder.size > 0 else []
                    step_collected = int(remainder.size)

                    mono = _resample_if_needed(step_data, device_rate, sample_rate)
                    _record_captured_step(mono)
                    audio_buffer = _advance_sliding_window(
                        audio_buffer, mono, step_frames
                    )
                    buffered_frames = min(
                        block_frames, buffered_frames + step_frames
                    )

                    if buffered_frames >= block_frames:
                        _process_audio_chunk(audio_buffer.copy())
            finally:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
                pa.terminate()
            return

        if sc is not None:
            speaker = sc.default_speaker()
            if speaker is None:
                print("ℹ️ 未啟用 ASR：找不到預設喇叭(loopback來源)")
                _asr_stop_event.wait(2.0)
                return

            loopback_mic = None
            try:
                loopback_mic = sc.get_microphone(
                    speaker.id, include_loopback=True
                )
            except Exception:
                pass

            if loopback_mic is None:
                for mic in sc.all_microphones(include_loopback=True):
                    if speaker.name in mic.name:
                        loopback_mic = mic
                        break

            if loopback_mic is None:
                print("ℹ️ 未啟用 ASR：找不到對應的 loopback 錄音裝置")
                _asr_stop_event.wait(2.0)
                return

            source_name = speaker.name
            print(f"🎙️ ASR 已啟動，來源: {source_name} [全系統混音 Loopback]")
            print(
                f"🔊 開始錄音，使用滑動視窗 (大小: {block_seconds}s, 步進: {step_seconds}s)..."
            )
            meter_frames = max(512, int(sample_rate * AUDIO_METER_CHUNK_SECONDS))
            audio_buffer = np.zeros(block_frames, dtype=np.float32)
            buffered_frames = 0
            step_chunks = []
            step_collected = 0
            rec_channels = max(
                1, min(2, int(getattr(loopback_mic, "channels", 1) or 1))
            )
            with loopback_mic.recorder(
                samplerate=sample_rate,
                channels=rec_channels,
                blocksize=meter_frames,
            ) as recorder:
                while not _asr_stop_event.is_set():
                    if (
                        config.AUDIO_SOURCE_MODE == "spotify"
                        and getattr(
                            config, "CAPTURE_SPOTIFY_PROCESS_ONLY", True
                        )
                        and PROCESS_LOOPBACK_AVAILABLE
                    ):
                        break

                    data = recorder.record(numframes=meter_frames)
                    if data.ndim > 1 or rec_channels > 1:
                        data = downmix_to_mono(data, rec_channels)

                    if _asr_listening_active and not _asr_stop_event.is_set():
                        level_emitter.emit_samples(data, source_name)

                    step_chunks.append(data)
                    step_collected += data.size
                    if step_collected < step_frames:
                        continue

                    combined = (
                        np.concatenate(step_chunks)
                        if len(step_chunks) > 1
                        else step_chunks[0]
                    )
                    step_data = combined[:step_frames]
                    remainder = combined[step_frames:]
                    step_chunks = [remainder] if remainder.size > 0 else []
                    step_collected = int(remainder.size)

                    _record_captured_step(step_data)
                    audio_buffer = _advance_sliding_window(
                        audio_buffer, step_data, step_frames
                    )
                    buffered_frames = min(
                        block_frames, buffered_frames + step_frames
                    )

                    if buffered_frames >= block_frames:
                        _process_audio_chunk(audio_buffer.copy())

    while not _asr_stop_event.is_set():
        mode = config.AUDIO_SOURCE_MODE
        spotify_only = getattr(config, "CAPTURE_SPOTIFY_PROCESS_ONLY", True)

        if mode == "spotify" and spotify_only and PROCESS_LOOPBACK_AVAILABLE:
            _run_spotify_process_loop()
        else:
            _run_default_loopback_loop()

        _asr_stop_event.wait(0.5)

    close_asr_wav_recording("worker_exit")
    _asr_started = False


def ensure_asr_worker_started(on_recognized_text):
    global _asr_thread, _asr_started, _asr_callback, _asr_listening_active

    _asr_callback = on_recognized_text

    if not config.ASR_ENABLED:
        return

    _asr_stop_event.clear()
    _asr_listening_active = True

    if _asr_thread is not None and _asr_thread.is_alive():
        _asr_started = True
        config.report_startup_progress(
            1.0, f"Whisper-{config.ASR_MODEL_SIZE} 模型已就緒！", newline=True
        )
        if _asr_active_device:
            _emit_asr_status(
                True, _asr_active_device, fallback=_asr_fallback_cpu
            )
        return

    init_done_event = threading.Event()
    _asr_thread = threading.Thread(
        target=_asr_worker_loop,
        args=(on_recognized_text, init_done_event),
        daemon=True,
    )
    _asr_thread.start()
    _asr_started = True
    init_done_event.wait()
