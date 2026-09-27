# process_loopback.py
"""Windows 11 Process-specific WASAPI Loopback audio capture.

Captures audio strictly from a specified process tree (e.g., Spotify.exe)
without capturing system-wide sounds, Discord, browser, or games.
"""

import ctypes
from ctypes import (
    HRESULT,
    POINTER,
    Structure,
    WINFUNCTYPE,
    byref,
    c_longlong,
    c_ubyte,
    c_void_p,
    c_wchar_p,
    wintypes,
)
import math
import os
import threading
import time
from typing import Optional
import uuid

import numpy as np
from scipy.signal import resample_poly

# Windows COM Constants & Flags
COINIT_MULTITHREADED = 0x0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
TH32CS_SNAPPROCESS = 0x00000002


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, guid_str: str) -> "GUID":
        u = uuid.UUID(guid_str)
        return cls(
            u.time_low,
            u.time_mid,
            u.time_hi_version,
            (c_ubyte * 8)(*u.bytes[8:]),
        )


IID_IAudioClient = GUID.from_str("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID.from_str("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
CLSID_MMDeviceEnumerator = GUID.from_str("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID.from_str("{A95664D2-9614-4F35-A746-DE8DB63617E6}")


class WAVEFORMATEX(Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", wintypes.DWORD),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [
        ("ActivationType", wintypes.DWORD),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class BLOB(Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("pBlobData", c_void_p),
    ]


class PROPVARIANT(Structure):
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("blob", BLOB),
    ]


class PROCESSENTRY32(Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


# COM Interface definitions
class IMMDeviceEnumeratorVtbl(Structure):
    _fields_ = [
        ("QueryInterface", c_void_p),
        ("AddRef", c_void_p),
        ("Release", WINFUNCTYPE(wintypes.ULONG, c_void_p)),
        ("EnumAudioEndpoints", c_void_p),
        (
            "GetDefaultAudioEndpoint",
            WINFUNCTYPE(
                HRESULT,
                c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                POINTER(c_void_p),
            ),
        ),
    ]


class IMMDeviceEnumerator(Structure):
    _fields_ = [("lpVtbl", POINTER(IMMDeviceEnumeratorVtbl))]


class IMMDeviceVtbl(Structure):
    _fields_ = [
        ("QueryInterface", c_void_p),
        ("AddRef", c_void_p),
        ("Release", WINFUNCTYPE(wintypes.ULONG, c_void_p)),
        (
            "Activate",
            WINFUNCTYPE(
                HRESULT,
                c_void_p,
                POINTER(GUID),
                wintypes.DWORD,
                c_void_p,
                POINTER(c_void_p),
            ),
        ),
    ]


class IMMDevice(Structure):
    _fields_ = [("lpVtbl", POINTER(IMMDeviceVtbl))]


class IAudioClientVtbl(Structure):
    _fields_ = [
        ("QueryInterface", c_void_p),
        ("AddRef", c_void_p),
        ("Release", WINFUNCTYPE(wintypes.ULONG, c_void_p)),
        (
            "Initialize",
            WINFUNCTYPE(
                HRESULT,
                c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                c_longlong,
                c_longlong,
                c_void_p,
                POINTER(GUID),
            ),
        ),
        ("GetBufferSize", WINFUNCTYPE(HRESULT, c_void_p, POINTER(wintypes.UINT))),
        ("GetStreamLatency", c_void_p),
        ("GetCurrentPadding", WINFUNCTYPE(HRESULT, c_void_p, POINTER(wintypes.UINT))),
        (
            "IsFormatSupported",
            WINFUNCTYPE(
                HRESULT, c_void_p, wintypes.DWORD, c_void_p, POINTER(c_void_p)
            ),
        ),
        ("GetMixFormat", WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))),
        ("GetDevicePeriod", c_void_p),
        ("Start", WINFUNCTYPE(HRESULT, c_void_p)),
        ("Stop", WINFUNCTYPE(HRESULT, c_void_p)),
        ("Reset", WINFUNCTYPE(HRESULT, c_void_p)),
        ("SetEventHandle", WINFUNCTYPE(HRESULT, c_void_p, wintypes.HANDLE)),
        (
            "GetService",
            WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p)),
        ),
    ]


class IAudioClient(Structure):
    _fields_ = [("lpVtbl", POINTER(IAudioClientVtbl))]


class IAudioCaptureClientVtbl(Structure):
    _fields_ = [
        ("QueryInterface", c_void_p),
        ("AddRef", c_void_p),
        ("Release", WINFUNCTYPE(wintypes.ULONG, c_void_p)),
        (
            "GetBuffer",
            WINFUNCTYPE(
                HRESULT,
                c_void_p,
                POINTER(c_void_p),
                POINTER(wintypes.UINT),
                POINTER(wintypes.DWORD),
                POINTER(c_longlong),
                POINTER(c_longlong),
            ),
        ),
        ("ReleaseBuffer", WINFUNCTYPE(HRESULT, c_void_p, wintypes.UINT)),
        (
            "GetNextPacketSize",
            WINFUNCTYPE(HRESULT, c_void_p, POINTER(wintypes.UINT)),
        ),
    ]


class IAudioCaptureClient(Structure):
    _fields_ = [("lpVtbl", POINTER(IAudioCaptureClientVtbl))]


class IActivateAudioInterfaceAsyncOperationVtbl(Structure):
    _fields_ = [
        ("QueryInterface", c_void_p),
        ("AddRef", c_void_p),
        ("Release", WINFUNCTYPE(wintypes.ULONG, c_void_p)),
        (
            "GetActivateResult",
            WINFUNCTYPE(HRESULT, c_void_p, POINTER(HRESULT), POINTER(c_void_p)),
        ),
    ]


class IActivateAudioInterfaceAsyncOperation(Structure):
    _fields_ = [("lpVtbl", POINTER(IActivateAudioInterfaceAsyncOperationVtbl))]


QueryInterface_proto = WINFUNCTYPE(
    HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p)
)
AddRef_proto = WINFUNCTYPE(wintypes.ULONG, c_void_p)
Release_proto = WINFUNCTYPE(wintypes.ULONG, c_void_p)
ActivateCompleted_proto = WINFUNCTYPE(HRESULT, c_void_p, c_void_p)


class IActivateAudioInterfaceCompletionHandlerVtbl(Structure):
    _fields_ = [
        ("QueryInterface", QueryInterface_proto),
        ("AddRef", AddRef_proto),
        ("Release", Release_proto),
        ("ActivateCompleted", ActivateCompleted_proto),
    ]


class IActivateAudioInterfaceCompletionHandler(Structure):
    _fields_ = [
        ("lpVtbl", POINTER(IActivateAudioInterfaceCompletionHandlerVtbl))
    ]


def find_spotify_process_id() -> Optional[int]:
    """Find the root/main process ID for Spotify.exe.

    Returns:
        int: Process ID if found, otherwise None.
    """
    if os.name != "nt":
        return None

    kernel32 = ctypes.windll.kernel32
    h_snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h_snapshot == -1 or h_snapshot is None:
        return None

    spotify_procs = {}  # pid -> ppid
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)

    try:
        if kernel32.Process32First(h_snapshot, byref(pe)):
            while True:
                exe_name = pe.szExeFile.decode("utf-8", errors="ignore").lower()
                if exe_name == "spotify.exe":
                    spotify_procs[pe.th32ProcessID] = pe.th32ParentProcessID
                if not kernel32.Process32Next(h_snapshot, byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(h_snapshot)

    if not spotify_procs:
        return None

    # Find root Spotify process (parent is NOT another Spotify process)
    root_pids = [
        pid for pid, ppid in spotify_procs.items() if ppid not in spotify_procs
    ]
    if root_pids:
        return root_pids[0]

    return next(iter(spotify_procs.keys()))


def is_process_loopback_supported() -> bool:
    """Check if the current Windows OS supports WASAPI Process Loopback."""
    if os.name != "nt":
        return False
    try:
        mmdevapi = ctypes.WinDLL("mmdevapi.dll")
        return hasattr(mmdevapi, "ActivateAudioInterfaceAsync")
    except Exception:
        return False


class SpotifyProcessAudioCapture:
    """Captures audio from Spotify process tree using Windows 11 WASAPI Process Loopback."""

    def __init__(self, target_sample_rate: int = 16000):
        self.target_sample_rate = target_sample_rate
        self.target_pid: Optional[int] = None
        self._ole32 = ctypes.windll.ole32
        self._kernel32 = ctypes.windll.kernel32
        self._mmdevapi = ctypes.WinDLL("mmdevapi.dll")

        self._audio_client = None
        self._capture_client = None
        self._h_event = None
        self._is_running = False
        self._device_sample_rate = 48000
        self._device_channels = 2

    def start(self, pid: Optional[int] = None) -> bool:
        """Initialize and start capturing from the Spotify process tree.

        Args:
            pid: Optional Spotify PID. If None, automatically searches.

        Returns:
            bool: True if started successfully, False otherwise.
        """
        if pid is None:
            pid = find_spotify_process_id()

        if pid is None:
            return False

        self.target_pid = pid

        try:
            self._ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        except Exception:
            pass

        # 1. Query default render endpoint format
        p_enum_void = c_void_p()
        hr = self._ole32.CoCreateInstance(
            byref(CLSID_MMDeviceEnumerator),
            None,
            1,
            byref(IID_IMMDeviceEnumerator),
            byref(p_enum_void),
        )
        if hr != 0 or not p_enum_void.value:
            return False

        enumerator = ctypes.cast(p_enum_void.value, POINTER(IMMDeviceEnumerator))
        p_dev_void = c_void_p()
        hr = enumerator.contents.lpVtbl.contents.GetDefaultAudioEndpoint(
            enumerator, 0, 0, byref(p_dev_void)  # eRender=0, eConsole=0
        )
        enumerator.contents.lpVtbl.contents.Release(enumerator)

        if hr != 0 or not p_dev_void.value:
            return False

        device = ctypes.cast(p_dev_void.value, POINTER(IMMDevice))
        p_client_void = c_void_p()
        hr = device.contents.lpVtbl.contents.Activate(
            device, byref(IID_IAudioClient), 1, None, byref(p_client_void)
        )
        device.contents.lpVtbl.contents.Release(device)

        if hr != 0 or not p_client_void.value:
            return False

        default_client = ctypes.cast(
            p_client_void.value, POINTER(IAudioClient)
        )
        p_mix_format_void = c_void_p()
        hr = default_client.contents.lpVtbl.contents.GetMixFormat(
            default_client, byref(p_mix_format_void)
        )
        default_client.contents.lpVtbl.contents.Release(default_client)

        if hr != 0 or not p_mix_format_void.value:
            return False

        wfx = ctypes.cast(p_mix_format_void.value, POINTER(WAVEFORMATEX)).contents
        self._device_sample_rate = wfx.nSamplesPerSec
        self._device_channels = wfx.nChannels

        # 2. Activate Process Loopback on target PID
        act_params = AUDIOCLIENT_ACTIVATION_PARAMS()
        act_params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        act_params.ProcessLoopbackParams.TargetProcessId = self.target_pid
        act_params.ProcessLoopbackParams.ProcessLoopbackMode = (
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        )

        propvar = PROPVARIANT()
        propvar.vt = 65  # VT_BLOB
        propvar.blob.cbSize = ctypes.sizeof(act_params)
        propvar.blob.pBlobData = ctypes.cast(byref(act_params), c_void_p)

        completion_event = threading.Event()
        activated_client_ptr = [None]

        def _QueryInterface(this, riid_ptr, ppvObject):
            ppvObject.contents.value = this
            return 0

        def _AddRef(this):
            return 1

        def _Release(this):
            return 1

        def _ActivateCompleted(this, activateOperation_ptr):
            op = ctypes.cast(
                activateOperation_ptr,
                POINTER(IActivateAudioInterfaceAsyncOperation),
            )
            hr_res = HRESULT()
            punk = c_void_p()
            hr_op = op.contents.lpVtbl.contents.GetActivateResult(
                activateOperation_ptr, byref(hr_res), byref(punk)
            )
            if hr_op == 0 and hr_res.value == 0 and punk.value:
                activated_client_ptr[0] = ctypes.cast(
                    punk.value, POINTER(IAudioClient)
                )
            completion_event.set()
            return 0

        handler_vtbl = IActivateAudioInterfaceCompletionHandlerVtbl(
            QueryInterface=QueryInterface_proto(_QueryInterface),
            AddRef=AddRef_proto(_AddRef),
            Release=Release_proto(_Release),
            ActivateCompleted=ActivateCompleted_proto(_ActivateCompleted),
        )
        handler = IActivateAudioInterfaceCompletionHandler(
            lpVtbl=ctypes.pointer(handler_vtbl)
        )

        self._mmdevapi.ActivateAudioInterfaceAsync.restype = HRESULT
        self._mmdevapi.ActivateAudioInterfaceAsync.argtypes = [
            c_wchar_p,
            POINTER(GUID),
            POINTER(PROPVARIANT),
            POINTER(IActivateAudioInterfaceCompletionHandler),
            POINTER(c_void_p),
        ]

        async_op = c_void_p()
        hr = self._mmdevapi.ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(IID_IAudioClient),
            byref(propvar),
            byref(handler),
            byref(async_op),
        )

        if hr != 0 or not completion_event.wait(timeout=3.0):
            return False

        self._audio_client = activated_client_ptr[0]
        if self._audio_client is None:
            return False

        # 3. Initialize AudioClient with event callback
        self._h_event = self._kernel32.CreateEventW(None, False, False, None)
        buffer_duration_100ns = 10000000  # 1 second

        hr = self._audio_client.contents.lpVtbl.contents.Initialize(
            self._audio_client,
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            buffer_duration_100ns,
            0,
            p_mix_format_void,
            None,
        )
        if hr != 0:
            self.stop()
            return False

        hr = self._audio_client.contents.lpVtbl.contents.SetEventHandle(
            self._audio_client, self._h_event
        )
        if hr != 0:
            self.stop()
            return False

        p_cap_void = c_void_p()
        hr = self._audio_client.contents.lpVtbl.contents.GetService(
            self._audio_client,
            byref(IID_IAudioCaptureClient),
            byref(p_cap_void),
        )
        if hr != 0 or not p_cap_void.value:
            self.stop()
            return False

        self._capture_client = ctypes.cast(
            p_cap_void.value, POINTER(IAudioCaptureClient)
        )

        hr = self._audio_client.contents.lpVtbl.contents.Start(
            self._audio_client
        )
        if hr != 0:
            self.stop()
            return False

        self._is_running = True
        return True

    def read_audio_step(
        self, step_frames: int, timeout_ms: int = 1500
    ) -> np.ndarray:
        """Reads and resamples accumulated audio chunk of target step_frames (16kHz mono).

        Args:
            step_frames: Number of samples in target sample rate (e.g., 16kHz).
            timeout_ms: Max wait time for incoming packets.

        Returns:
            np.ndarray: float32 mono audio array with length == step_frames.
        """
        if not self._is_running or self._capture_client is None:
            return np.zeros(step_frames, dtype=np.float32)

        raw_samples = []
        collected_device_frames = 0
        needed_device_frames = int(
            step_frames * (self._device_sample_rate / self.target_sample_rate)
        )

        p_data = c_void_p()
        num_frames = wintypes.UINT()
        flags = wintypes.DWORD()
        dev_pos = c_longlong()
        qpc_pos = c_longlong()
        packet_size = wintypes.UINT()

        start_time = time.time()
        max_duration_sec = timeout_ms / 1000.0

        while (
            collected_device_frames < needed_device_frames
            and (time.time() - start_time) < max_duration_sec
        ):
            # Wait for next audio event
            res = self._kernel32.WaitForSingleObject(self._h_event, 50)
            if res != 0 and res != 258:  # Not WAIT_OBJECT_0 and not WAIT_TIMEOUT
                break

            while True:
                hr = self._capture_client.contents.lpVtbl.contents.GetNextPacketSize(
                    self._capture_client, byref(packet_size)
                )
                if hr != 0 or packet_size.value == 0:
                    break

                hr = self._capture_client.contents.lpVtbl.contents.GetBuffer(
                    self._capture_client,
                    byref(p_data),
                    byref(num_frames),
                    byref(flags),
                    byref(dev_pos),
                    byref(qpc_pos),
                )
                if hr != 0:
                    break

                frames = num_frames.value
                if frames > 0:
                    collected_device_frames += frames
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                        raw_samples.append(
                            np.zeros(
                                frames * self._device_channels,
                                dtype=np.float32,
                            )
                        )
                    else:
                        buf_type = ctypes.c_float * (
                            frames * self._device_channels
                        )
                        raw_buf = buf_type.from_address(p_data.value)
                        raw_samples.append(
                            np.ctypeslib.as_array(raw_buf).copy()
                        )

                self._capture_client.contents.lpVtbl.contents.ReleaseBuffer(
                    self._capture_client, frames
                )

        if not raw_samples:
            return np.zeros(step_frames, dtype=np.float32)

        data = np.concatenate(raw_samples)
        if self._device_channels > 1:
            data = data.reshape(-1, self._device_channels)[:, 0]

        # Resample from _device_sample_rate to target_sample_rate (16kHz)
        if self._device_sample_rate != self.target_sample_rate:
            gcd_val = math.gcd(
                self._device_sample_rate, self.target_sample_rate
            )
            up = self.target_sample_rate // gcd_val
            down = self._device_sample_rate // gcd_val
            resampled = resample_poly(data, up, down).astype(np.float32)
        else:
            resampled = data.astype(np.float32)

        if resampled.size >= step_frames:
            return resampled[:step_frames]

        padded = np.zeros(step_frames, dtype=np.float32)
        padded[: resampled.size] = resampled
        return padded

    def is_target_alive(self) -> bool:
        """Check if target Spotify PID is still active."""
        if self.target_pid is None:
            return False
        active_pid = find_spotify_process_id()
        return active_pid is not None

    def stop(self):
        """Stop capture and release all COM resources."""
        self._is_running = False

        if self._audio_client is not None:
            try:
                self._audio_client.contents.lpVtbl.contents.Stop(
                    self._audio_client
                )
            except Exception:
                pass

        if self._capture_client is not None:
            try:
                self._capture_client.contents.lpVtbl.contents.Release(
                    self._capture_client
                )
            except Exception:
                pass
            self._capture_client = None

        if self._audio_client is not None:
            try:
                self._audio_client.contents.lpVtbl.contents.Release(
                    self._audio_client
                )
            except Exception:
                pass
            self._audio_client = None

        if self._h_event is not None:
            try:
                self._kernel32.CloseHandle(self._h_event)
            except Exception:
                pass
            self._h_event = None
