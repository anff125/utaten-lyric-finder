"""Audio capture loop and faster-whisper ASR worker."""

import math
import os
import threading
import time
from typing import Any, cast

from utaten_finder.audio.cuda_loader import prepare_windows_cuda_dll_paths
from utaten_finder.core import config, events

with config.startup_progress_stage(0.20, 0.30, "載入音訊重採樣模組 (SciPy)..."):
    from scipy.signal import resample_poly

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


def set_asr_listening(active: bool) -> None:
    """Enable or pause ASR transcription without tearing down the loaded model."""
    global _asr_listening_active
    _asr_listening_active = bool(active)


def request_asr_stop() -> None:
    """Signal the ASR capture loops to terminate cleanly."""
    global _asr_listening_active
    _asr_listening_active = False
    _asr_stop_event.set()


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


def _emit_asr_status(ready: bool, device_mode: str, error: str = "") -> None:
    events.bus.emit(
        "asr_status",
        {
            "ready": ready,
            "device_mode": device_mode,
            "model": config.ASR_MODEL_SIZE,
            "error": error,
        },
    )


def _asr_worker_loop(
    on_recognized_text, init_done_event: threading.Event | None = None
):
    global _asr_disabled_logged, _asr_active_device, _asr_started

    prepare_windows_cuda_dll_paths()

    if not ASR_CORE_DEPS_AVAILABLE:
        _asr_active_device = ""
        _emit_asr_status(False, "", error=ASR_DEPS_ERROR)
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
        _emit_asr_status(False, "", error="缺少音訊錄製套件")
        config.report_startup_progress(
            1.0, "未啟用 ASR：缺少音訊錄製套件", newline=True
        )
        print("ℹ️ 未啟用 ASR：需要 pyaudiowpatch 或 soundcard 其中之一")
        if init_done_event is not None:
            init_done_event.set()
        _asr_started = False
        return

    try:
        # 開啟 faster-whisper 的下載進度條
        import faster_whisper.utils
        from tqdm.auto import tqdm

        faster_whisper.utils.disabled_tqdm = tqdm

        model_path = config.ASR_MODEL_SIZE
        if not os.path.isdir(model_path):
            try:
                model_path = faster_whisper.utils.download_model(
                    config.ASR_MODEL_SIZE, local_files_only=True
                )
            except Exception:
                config.report_startup_progress(
                    0.68,
                    f"首次使用，正在下載 Whisper-{config.ASR_MODEL_SIZE} 模型...",
                    newline=True,
                )
                model_path = faster_whisper.utils.download_model(
                    config.ASR_MODEL_SIZE, local_files_only=False
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
        _emit_asr_status(True, _asr_active_device)
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
                _emit_asr_status(True, "cpu")
                config.report_startup_progress(
                    1.0,
                    f"Whisper-{config.ASR_MODEL_SIZE} (CPU 模式) 已就緒！",
                    newline=True,
                )
                print(f"✅ Whisper-{config.ASR_MODEL_SIZE} 已以 CPU 模式加載")
            except Exception as cpu_e:
                _asr_active_device = ""
                _emit_asr_status(False, "", error=str(cpu_e))
                config.report_startup_progress(
                    1.0, "CPU 模式也無法加載 Whisper 模型", newline=True
                )
                print(f"❌ CPU 模式也無法加載 Whisper 模型: {cpu_e}")
                if init_done_event is not None:
                    init_done_event.set()
                _asr_started = False
                return
        else:
            _asr_active_device = ""
            _emit_asr_status(False, "", error=error_text)
            config.report_startup_progress(
                1.0, "無法加載 Whisper 模型", newline=True
            )
            print(f"❌ 無法加載 Whisper 模型: {e}")
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

    def _process_audio_chunk(mono_float32):
        nonlocal model, recognition_count, chunk_counter
        global _asr_disabled_logged, _asr_active_device

        if not _asr_listening_active or _asr_stop_event.is_set():
            return

        if not config.ASR_ENABLED:
            if not _asr_disabled_logged:
                print("ℹ️ ASR 自動捲動已關閉，暫停語音辨識。")
                _asr_disabled_logged = True
            return

        _asr_disabled_logged = False

        audio_data = np.clip(mono_float32, -1.0, 1.0).astype(np.float32)

        volume = abs(audio_data).mean()
        if volume < 0.0001:
            if config.DEBUG_RECOGNITION:
                print("🔇 [靜音] 目前無音訊輸入，跳過本輪辨識。")
            return

        try:
            segments, _ = model.transcribe(
                audio_data,
                language=config.ASR_LANGUAGE,
                vad_filter=False,
                beam_size=10,
                best_of=5,
            )

            try:
                result_text = "".join(
                    segment.text for segment in segments if segment and segment.text
                ).strip()
            except Exception as e:
                error_text = str(e)
                if "cublas" in error_text.lower() or "cuda" in error_text.lower():
                    raise
                print(f"⚠️ Whisper 迭代 segment 時發生錯誤: {e}")
                result_text = ""

            hallucinations = [
                "ご視聴ありがとうございました",
                "視聴ありがとうございました",
                "チャンネル登録",
                "高評価",
                "よろしくお願いします",
            ]
            for h in hallucinations:
                result_text = result_text.replace(h, "")
            result_text = result_text.strip()

            if result_text:
                recognition_count += 1
                if config.DEBUG_RECOGNITION:
                    print(f"🎤 [#{recognition_count}] 識別: {result_text}")
                callback = _asr_callback or on_recognized_text
                if callback is not None:
                    callback(result_text)

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
                    _emit_asr_status(True, "cpu")
                    print("✅ 已切換為 CPU 模式運作")
                except Exception as cpu_e:
                    print(f"❌ 切換 CPU 模式失敗: {cpu_e}")
            else:
                print(f"⚠️ Whisper 辨識發生內部錯誤: {e}")

    def _run_spotify_process_loop():
        nonlocal source_name
        last_missing_logged = 0.0
        capture = SpotifyProcessAudioCapture(target_sample_rate=sample_rate)

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
                    )
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
                frames_per_buffer = max(512, int(device_rate * step_seconds))
                source_name = device["name"]

                stream = pa.open(
                    format=pyaudio_mod.paFloat32,
                    channels=device_channels,
                    rate=device_rate,
                    input=True,
                    frames_per_buffer=frames_per_buffer,
                    input_device_index=int(device["index"]),
                )

                print(f"🎙️ ASR 已啟動，來源: {source_name} [全系統混音 Loopback]")
                print(
                    f"🔊 開始錄音，使用滑動視窗 (大小: {block_seconds}s, 步進: {step_seconds}s)..."
                )

                audio_buffer = np.zeros(block_frames, dtype=np.float32)
                buffered_frames = 0

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
                        frames_per_buffer, exception_on_overflow=False
                    )
                    data = np.frombuffer(raw, dtype=np.float32)
                    if device_channels > 1:
                        data = data.reshape(-1, device_channels)[:, 0]

                    mono = _resample_if_needed(data, device_rate, sample_rate)
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

            print(f"🎙️ ASR 已啟動，來源: {speaker.name} [全系統混音 Loopback]")
            print(
                f"🔊 開始錄音，使用滑動視窗 (大小: {block_seconds}s, 步進: {step_seconds}s)..."
            )
            audio_buffer = np.zeros(block_frames, dtype=np.float32)
            buffered_frames = 0
            with loopback_mic.recorder(
                samplerate=sample_rate,
                channels=1,
                blocksize=step_frames,
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

                    data = recorder.record(numframes=step_frames)
                    if data.ndim > 1:
                        data = data[:, 0]

                    audio_buffer = _advance_sliding_window(
                        audio_buffer, data, step_frames
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
            _emit_asr_status(True, _asr_active_device)
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
