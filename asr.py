import os
import sys
import threading
import time
import math  # 新增
import scipy.io.wavfile as wavfile
from scipy.signal import resample_poly  # 新增
import config

from config import (
    ASR_BLOCK_SECONDS,
    ASR_COMPUTE_TYPE,
    ASR_DEVICE,
    ASR_LANGUAGE,
    ASR_MODEL_SIZE,
    ASR_SAMPLE_RATE,
)

try:
    import numpy as np
    from faster_whisper import WhisperModel
    import huggingface_hub
    from tqdm.auto import tqdm

    try:
        from faster_whisper.utils import _MODELS as _FASTER_WHISPER_MODELS
    except Exception:
        _FASTER_WHISPER_MODELS = {}

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

_MODEL_ALLOW_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]


def _notify_status(on_asr_status, payload):
    if not on_asr_status:
        return
    try:
        on_asr_status(payload)
    except Exception:
        # UI callback 失敗不應中斷 ASR worker
        pass


def _resolve_model_repo_id(size_or_id):
    if "/" in size_or_id:
        return size_or_id
    return _FASTER_WHISPER_MODELS.get(size_or_id)


def _ensure_model_downloaded(model_size, on_asr_status=None):
    """Ensure model files exist locally and report download progress when needed."""
    repo_id = _resolve_model_repo_id(model_size)
    if repo_id is None:
        return model_size

    try:
        cached_path = huggingface_hub.snapshot_download(
            repo_id,
            local_files_only=True,
            allow_patterns=_MODEL_ALLOW_PATTERNS,
        )
        _notify_status(
            on_asr_status,
            {
                "stage": "cached",
                "text": f"ASR 模型已存在本機快取 ({model_size})",
            },
        )
        return cached_path
    except Exception:
        pass

    _notify_status(
        on_asr_status,
        {
            "stage": "downloading",
            "text": f"正在下載 ASR 模型: {model_size}",
            "progress": 0.0,
            "indeterminate": True,
        },
    )

    last_emit_at = 0.0

    class _DownloadProgressTqdm(tqdm):
        def update(self, n=1):
            nonlocal last_emit_at
            ret = super().update(n)
            now = time.monotonic()
            if now - last_emit_at < 0.1:
                return ret
            last_emit_at = now
            total = getattr(self, "total", None)
            current = getattr(self, "n", 0)
            if total:
                progress = max(0.0, min(1.0, float(current) / float(total)))
                _notify_status(
                    on_asr_status,
                    {
                        "stage": "downloading",
                        "text": f"正在下載 ASR 模型: {model_size} ({int(progress * 100)}%)",
                        "progress": progress,
                        "indeterminate": False,
                    },
                )
            else:
                _notify_status(
                    on_asr_status,
                    {
                        "stage": "downloading",
                        "text": f"正在下載 ASR 模型: {model_size}",
                        "progress": 0.0,
                        "indeterminate": True,
                    },
                )
            return ret

        def close(self):
            try:
                total = getattr(self, "total", None)
                if total:
                    _notify_status(
                        on_asr_status,
                        {
                            "stage": "downloading",
                            "text": f"正在下載 ASR 模型: {model_size} (100%)",
                            "progress": 1.0,
                            "indeterminate": False,
                        },
                    )
            finally:
                super().close()

    return huggingface_hub.snapshot_download(
        repo_id,
        allow_patterns=_MODEL_ALLOW_PATTERNS,
        tqdm_class=_DownloadProgressTqdm,
    )


def _prepare_windows_cuda_dll_paths():
    """Register pip-installed NVIDIA DLL folders so Windows loader can resolve them."""
    if os.name != "nt":
        return

    # 判斷是否為 PyInstaller 打包後的環境
    if getattr(sys, "frozen", False):
        # 執行檔模式：路徑為 PyInstaller 解壓縮的暫存目錄
        base = os.path.join(sys._MEIPASS, "nvidia")
    else:
        # 開發模式：路徑為你的虛擬環境
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


def _asr_worker_loop(on_recognized_text, on_asr_status=None):
    global _asr_disabled_logged

    print("ℹ️  ASR worker 啟動中...")

    _prepare_windows_cuda_dll_paths()

    if not ASR_CORE_DEPS_AVAILABLE:
        print(f"ℹ️ 未啟用 ASR：缺少套件 ({ASR_DEPS_ERROR})")
        return

    if not (PYAUDIO_LOOPBACK_AVAILABLE or SOUNDCARD_AVAILABLE):
        print("ℹ️ 未啟用 ASR：需要 pyaudiowpatch 或 soundcard 其中之一")
        return

    print(f"🎧 正在加載 Whisper-{ASR_MODEL_SIZE} 模型...")
    model_source = ASR_MODEL_SIZE
    try:
        model_source = _ensure_model_downloaded(ASR_MODEL_SIZE, on_asr_status)
        _notify_status(
            on_asr_status,
            {
                "stage": "loading",
                "text": f"正在載入 ASR 模型: {ASR_MODEL_SIZE}",
                "indeterminate": True,
            },
        )
        model = WhisperModel(
            model_size_or_path=model_source,
            device=ASR_DEVICE,
            compute_type=ASR_COMPUTE_TYPE,
        )
        print(f"✅ Whisper-{ASR_MODEL_SIZE} 模型已加載")
        _notify_status(
            on_asr_status,
            {
                "stage": "ready",
                "text": f"ASR 模型就緒: {ASR_MODEL_SIZE}",
            },
        )
    except Exception as e:
        error_text = str(e)
        # If CUDA runtime/DLL is not available, retry once on CPU to keep ASR usable.
        if (
            "cublas64_12.dll" in error_text
            or "cudnn" in error_text.lower()
            or "cannot be loaded" in error_text.lower()
            or "loadlibrary" in error_text.lower()
        ):
            print(f"⚠️ GPU 載入失敗，改用 CPU 模式重試: {e}")
            try:
                model = WhisperModel(
                    model_size_or_path=model_source,
                    device="cpu",
                    compute_type="int8",
                )
                print(f"✅ Whisper-{ASR_MODEL_SIZE} 已以 CPU 模式加載")
                _notify_status(
                    on_asr_status,
                    {
                        "stage": "ready",
                        "text": f"ASR 模型就緒 (CPU): {ASR_MODEL_SIZE}",
                    },
                )
            except Exception as cpu_e:
                print(f"❌ CPU 模式也無法加載 Whisper 模型: {cpu_e}")
                _notify_status(
                    on_asr_status,
                    {
                        "stage": "error",
                        "text": f"ASR 模型載入失敗: {cpu_e}",
                    },
                )
                return
        else:
            print(f"❌ 無法加載 Whisper 模型: {e}")
            _notify_status(
                on_asr_status,
                {
                    "stage": "error",
                    "text": f"ASR 模型載入失敗: {e}",
                },
            )
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

    source_name = ""
    blocksize = int(ASR_SAMPLE_RATE * ASR_BLOCK_SECONDS)
    recognition_count = 0
    chunk_counter = 0  # 1. 新增：用來為音訊片段檔名標號的計數器

    def _process_audio_chunk(mono_float32):
        nonlocal recognition_count, chunk_counter
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

        # ==============================================================
        # 3. 新增：將音訊儲存到 debug_audio_chunks 資料夾底下
        # ==============================================================
        # chunk_counter += 1
        # debug_dir = "debug_audio_chunks"
        # if not os.path.exists(debug_dir):
        #     os.makedirs(debug_dir)

        # # 檔名格式例如：audio_chunk_0001.wav
        # file_path = os.path.join(debug_dir, f"audio_chunk_{chunk_counter:04d}.wav")
        # # 存檔 (audio_data 是 float32 格式，scipy wavfile 可以直接支援)
        # wavfile.write(file_path, ASR_SAMPLE_RATE, audio_data)
        # # ==============================================================

        try:
            segments, _ = model.transcribe(
                audio_data,
                language=ASR_LANGUAGE,
                vad_filter=False,
                beam_size=10,  # 增加搜尋寬度
                # initial_prompt="Lyrics, Songs, Japanese",  # 引導模型
                # condition_on_previous_text=True,  # 利用上下文
                best_of=5,  # 從多個候選中選最好的
            )

            result_text = "".join(segment.text for segment in segments).strip()

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
                if config.DEBUG:
                    print(f"🎤 [#{recognition_count}] 識別: {result_text}")
                on_recognized_text(result_text)

        except Exception as e:
            print(f"⚠️ Whisper 辨識發生內部錯誤: {e}")

    if PYAUDIO_LOOPBACK_AVAILABLE:
        pa = pyaudio.PyAudio()
        stream = None
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            device = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

            if not device.get("isLoopbackDevice", False):
                for loopback in pa.get_loopback_device_info_generator():
                    if device["name"] in loopback["name"]:
                        device = loopback
                        break

            device_rate = int(device["defaultSampleRate"])
            device_channels = max(1, min(2, int(device.get("maxInputChannels", 1))))
            frames_per_buffer = max(512, int(device_rate * ASR_BLOCK_SECONDS))
            source_name = device["name"]

            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=device_channels,
                rate=device_rate,
                input=True,
                frames_per_buffer=frames_per_buffer,
                input_device_index=int(device["index"]),
            )

            print(f"🎙️ ASR 已啟動，來源: {source_name}")
            print(f"🔊 開始錄音，每 {ASR_BLOCK_SECONDS}s 處理一個區塊...")

            while True:
                raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                data = np.frombuffer(raw, dtype=np.float32)
                if device_channels > 1:
                    data = data.reshape(-1, device_channels)[:, 0]

                mono = _resample_if_needed(data, device_rate, ASR_SAMPLE_RATE)
                _process_audio_chunk(mono)
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            pa.terminate()
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
        samplerate=ASR_SAMPLE_RATE,
        channels=1,
        blocksize=blocksize,
    ) as recorder:
        print(f"🔊 開始錄音，每 {ASR_BLOCK_SECONDS}s 處理一個區塊...")
        while True:
            data = recorder.record(numframes=blocksize)
            if data.ndim > 1:
                data = data[:, 0]
            _process_audio_chunk(data.astype(np.float32))


def ensure_asr_worker_started(on_recognized_text, on_asr_status=None):
    global _asr_thread, _asr_started

    if not config.ASR_ENABLED or _asr_started:
        return

    _asr_thread = threading.Thread(
        target=_asr_worker_loop,
        args=(on_recognized_text, on_asr_status),
        daemon=True,
    )
    _asr_thread.start()
    _asr_started = True
