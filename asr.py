import os
import sys
import threading
import math
from typing import Any, cast
from scipy.signal import resample_poly
import config


def _prepare_windows_cuda_dll_paths():
    """Register pip-installed NVIDIA DLL folders so Windows loader can resolve them."""
    if os.name != "nt":
        return

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        base = os.path.join(meipass, "nvidia")
    else:
        base = os.path.join(
            os.path.dirname(__file__), ".venv", "Lib", "site-packages", "nvidia"
        )

    dll_dirs = [
        os.path.join(base, "cublas", "bin"),
        os.path.join(base, "cudnn", "bin"),
        os.path.join(base, "cuda_runtime", "bin"),
        os.path.join(base, "cufft", "bin"),
        os.path.join(base, "curand", "bin"),
    ]

    for dll_dir in dll_dirs:
        if not os.path.isdir(dll_dir):
            continue
        try:
            os.add_dll_directory(dll_dir)
        except Exception as e:
            print(f"⚠️ 無法加入 DLL 目錄 {dll_dir}: {e}")


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

_asr_thread = None
_asr_started = False
_asr_disabled_logged = False

_HALLUCINATION_PHRASES = (
    "ご視聴ありがとうございました",
    "視聴ありがとうございました",
    "チャンネル登録",
    "高評価",
    "よろしくお願いします",
)

_CUDA_LOAD_ERROR_KEYWORDS = (
    "cublas64_12.dll",
    "cudnn",
    "cannot be loaded",
    "loadlibrary",
)


def _is_cuda_load_error(error_text: str) -> bool:
    error_lower = error_text.lower()
    return any(keyword in error_lower for keyword in _CUDA_LOAD_ERROR_KEYWORDS)


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
        import faster_whisper.utils
        from tqdm.auto import tqdm

        faster_whisper.utils.disabled_tqdm = tqdm

        model = WhisperModel(
            model_size_or_path=config.ASR_MODEL_SIZE,
            device=config.ASR_DEVICE,
            compute_type=config.ASR_COMPUTE_TYPE,
        )
        print(f"✅ Whisper-{config.ASR_MODEL_SIZE} 模型已加載")
    except Exception as e:
        error_text = str(e)
        if _is_cuda_load_error(error_text):
            print(f"⚠️ GPU 載入失敗，改用 CPU 模式重試: {e}")
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

    blocksize = int(config.ASR_SAMPLE_RATE * config.ASR_BLOCK_SECONDS)
    recognition_count = 0

    def _process_audio_chunk(mono_float32):
        nonlocal recognition_count
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

            result_text = "".join(segment.text for segment in segments).strip()

            for h in _HALLUCINATION_PHRASES:
                result_text = result_text.replace(h, "")
            result_text = result_text.strip()

            if result_text:
                recognition_count += 1
                if config.DEBUG:
                    print(f"🎤 [#{recognition_count}] 識別: {result_text}")
                on_recognized_text(result_text)

        except Exception as e:
            print(f"⚠️ Whisper 辨識發生內部錯誤: {e}")

    if PYAUDIO_LOOPBACK_AVAILABLE:
        if pyaudio is None:
            return
        pyaudio_mod = cast(Any, pyaudio)
        pa = pyaudio_mod.PyAudio()
        stream = None
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio_mod.paWASAPI)
            device = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

            if not device.get("isLoopbackDevice", False):
                for loopback in pa.get_loopback_device_info_generator():
                    if device["name"] in loopback["name"]:
                        device = loopback
                        break

            device_rate = int(device["defaultSampleRate"])
            device_channels = max(1, min(2, int(device.get("maxInputChannels", 1))))
            frames_per_buffer = max(512, int(device_rate * config.ASR_BLOCK_SECONDS))

            stream = pa.open(
                format=pyaudio_mod.paFloat32,
                channels=device_channels,
                rate=device_rate,
                input=True,
                frames_per_buffer=frames_per_buffer,
                input_device_index=int(device["index"]),
            )

            print(f"🎙️ ASR 已啟動，來源: {device['name']}")
            print(f"🔊 開始錄音，每 {config.ASR_BLOCK_SECONDS}s 處理一個區塊...")

            while True:
                raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                data = np.frombuffer(raw, dtype=np.float32)
                if device_channels > 1:
                    data = data.reshape(-1, device_channels)[:, 0]

                mono = _resample_if_needed(data, device_rate, config.ASR_SAMPLE_RATE)
                _process_audio_chunk(mono)
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            pa.terminate()
        return

    if sc is None:
        return

    speaker = sc.default_speaker()
    if speaker is None:
        print("ℹ️ 未啟用 ASR：找不到預設喇叭(loopback來源)")
        return

    loopback_mic = None
    try:
        loopback_mic = sc.get_microphone(speaker.id, include_loopback=True)
    except Exception:
        pass

    if loopback_mic is None:
        for mic in sc.all_microphones(include_loopback=True):
            if speaker.name in mic.name:
                loopback_mic = mic
                break

    if loopback_mic is None:
        print("ℹ️ 未啟用 ASR：找不到對應的 loopback 錄音裝置")
        return

    print(f"🎙️ ASR 已啟動，來源: {speaker.name}")
    with loopback_mic.recorder(
        samplerate=config.ASR_SAMPLE_RATE,
        channels=1,
        blocksize=blocksize,
    ) as recorder:
        print(f"🔊 開始錄音，每 {config.ASR_BLOCK_SECONDS}s 處理一個區塊...")
        while True:
            data = recorder.record(numframes=blocksize)
            if data.ndim > 1:
                data = data[:, 0]
            _process_audio_chunk(data.astype(np.float32))


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
