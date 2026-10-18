import ctypes
import math  # 新增
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

from scipy.signal import resample_poly  # 新增

import config


def _prepare_windows_cuda_dll_paths():
    """Register pip-installed NVIDIA DLL folders so Windows loader can resolve them."""
    if os.name != "nt":
        return

    candidate_bases = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidate_bases.append(Path(meipass) / "nvidia")
    else:
        candidate_bases.append(Path(sys.prefix) / "Lib" / "site-packages" / "nvidia")
        candidate_bases.append(
            Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages" / "nvidia"
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

        # 預先加載 DLL 至進程空間，確保後續 LoadLibrary 調用直接命中
        for dll_file in dll_dir.glob("*.dll"):
            try:
                ctypes.CDLL(str(dll_file))
            except Exception:
                pass


# 在導入 faster_whisper / ctranslate2 前先設定好 DLL 路徑
_prepare_windows_cuda_dll_paths()

try:
    import numpy as np
    from faster_whisper import WhisperModel

    ASR_CORE_DEPS_AVAILABLE = True
    ASR_DEPS_ERROR = ""
except Exception as _import_error:
    ASR_CORE_DEPS_AVAILABLE = False
    ASR_DEPS_ERROR = str(_import_error)

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
    from process_loopback import (
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


def _asr_worker_loop(on_recognized_text):
    global _asr_disabled_logged

    print("ℹ️  ASR worker 啟動中...")

    _prepare_windows_cuda_dll_paths()

    if not ASR_CORE_DEPS_AVAILABLE:
        print(f"ℹ️ 未啟用 ASR：缺少套件 ({ASR_DEPS_ERROR})")
        return

    if not (PYAUDIO_LOOPBACK_AVAILABLE or SOUNDCARD_AVAILABLE):
        print("ℹ️ 未啟用 ASR：需要 pyaudiowpatch 或 soundcard 其中之一")
        return

    print(f"🎧 正在加載 Whisper-{config.ASR_MODEL_SIZE} 模型...")
    try:
        # 開啟 faster-whisper 的下載進度條
        import faster_whisper.utils
        from tqdm.auto import tqdm

        faster_whisper.utils.disabled_tqdm = tqdm

        model = WhisperModel(
            model_size_or_path=config.ASR_MODEL_SIZE,
            device=config.ASR_DEVICE,
            compute_type=config.ASR_COMPUTE_TYPE,
        )

        # 暖身推理以確認 CUDA / cuBLAS 真正可用（若缺少 DLL 或 GPU 異常，在此立即攔截並 fallback）
        dummy_audio = np.zeros(16000, dtype=np.float32)
        warmup_segs, _ = model.transcribe(dummy_audio, language="ja")
        _ = list(warmup_segs)

        print(f"✅ Whisper-{config.ASR_MODEL_SIZE} 模型已加載")
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
            print(f"⚠️ GPU 載入或推理失敗，改用 CPU 模式重試: {e}")
            try:
                model = WhisperModel(
                    model_size_or_path=config.ASR_MODEL_SIZE,
                    device="cpu",
                    compute_type="int8",
                )
                print(f"✅ Whisper-{config.ASR_MODEL_SIZE} 已以 CPU 模式加載")
            except Exception as cpu_e:
                print(f"❌ CPU 模式也無法加載 Whisper 模型: {cpu_e}")
                return
        else:
            print(f"❌ 無法加載 Whisper 模型: {e}")
            return

    def _resample_if_needed(samples, in_rate, out_rate):
        if in_rate == out_rate:
            return samples
        if samples.size == 0:
            return samples

        # 找出輸入與輸出頻率的最大公因數 (GCD)，用來化簡比例
        # 例如 96000 轉 16000 -> up=1, down=6
        gcd_val = math.gcd(in_rate, out_rate)
        up = out_rate // gcd_val
        down = in_rate // gcd_val

        # 使用多相濾波重採樣 (Polyphase filtering)，音質遠勝線性插值
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
    chunk_counter = 0  # 1. 新增：用來為音訊片段檔名標號的計數器

    def _process_audio_chunk(mono_float32):
        nonlocal model, recognition_count, chunk_counter
        global _asr_disabled_logged

        if not config.ASR_ENABLED:
            if not _asr_disabled_logged:
                print("ℹ️ ASR 自動捲動已關閉，暫停語音辨識。")
                _asr_disabled_logged = True
            return

        _asr_disabled_logged = False

        audio_data = np.clip(mono_float32, -1.0, 1.0).astype(np.float32)

        volume = abs(audio_data).mean()
        if volume < 0.0001:
            print("🔇 [系統偵測] 收到的音訊完全沒聲音，可能抓到錯誤的音效裝置！")

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

            # 新增：過濾常見的 Whisper 幻覺字眼
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
                on_recognized_text(result_text)

        except Exception as e:
            error_text = str(e)
            if "cublas" in error_text.lower() or "cuda" in error_text.lower():
                print(f"⚠️ Whisper GPU 辨識失敗，自動降級切換至 CPU 模式: {e}")
                try:
                    model = WhisperModel(
                        model_size_or_path=config.ASR_MODEL_SIZE,
                        device="cpu",
                        compute_type="int8",
                    )
                    print(f"✅ 已切換為 CPU 模式運作")
                except Exception as cpu_e:
                    print(f"❌ 切換 CPU 模式失敗: {cpu_e}")
            else:
                print(f"⚠️ Whisper 辨識發生內部錯誤: {e}")

    def _run_spotify_process_loop():
        nonlocal source_name
        last_missing_logged = 0.0
        capture = SpotifyProcessAudioCapture(target_sample_rate=sample_rate)

        try:
            while True:
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
                        time.sleep(1.0)
                        continue

                    if not capture.start(pid):
                        now = time.time()
                        if now - last_missing_logged > 5.0:
                            print(
                                f"⚠️ [Process Loopback] 初始化 Spotify 音訊捕獲失敗 (PID={pid})，重試中..."
                            )
                            last_missing_logged = now
                        time.sleep(1.0)
                        continue

                    source_name = f"Spotify.exe (PID: {pid})"
                    print(f"🎙️ ASR 已啟動，來源: {source_name} [獨立進程 Loopback]")
                    print(
                        f"🔊 開始錄音，使用滑動視窗 (大小: {block_seconds}s, 步進: {step_seconds}s)..."
                    )

                audio_buffer = np.zeros(block_frames, dtype=np.float32)
                buffered_frames = 0

                while capture.is_target_alive():
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

                while True:
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
                time.sleep(2.0)
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
                time.sleep(2.0)
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
                while True:
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

    while True:
        mode = config.AUDIO_SOURCE_MODE
        spotify_only = getattr(config, "CAPTURE_SPOTIFY_PROCESS_ONLY", True)

        if mode == "spotify" and spotify_only and PROCESS_LOOPBACK_AVAILABLE:
            _run_spotify_process_loop()
        else:
            _run_default_loopback_loop()

        time.sleep(0.5)


def ensure_asr_worker_started(on_recognized_text):
    global _asr_thread, _asr_started

    if not config.ASR_ENABLED or _asr_started:
        return

    _asr_thread = threading.Thread(
        target=_asr_worker_loop,
        args=(on_recognized_text,),
        daemon=True,
    )
    _asr_thread.start()
    _asr_started = True
