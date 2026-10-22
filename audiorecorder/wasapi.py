"""Minimal Windows Core Audio (WASAPI) bindings, using only ctypes."""
from __future__ import annotations

import ctypes
import threading
import uuid
from ctypes import POINTER, WINFUNCTYPE, byref, c_int, c_int64, c_uint32, c_uint64, c_void_p, c_wchar_p
from ctypes import wintypes
from dataclasses import dataclass

SHAREMODE_SHARED = 0
STREAMFLAGS_LOOPBACK = 0x00020000
STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
BUFFERFLAGS_SILENT = 0x2

_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_DEVICE_STATE_ACTIVE = 0x1
_RENDER, _CAPTURE = 0, 1
_ROLE_CONSOLE = 0
_STGM_READ = 0
_VT_LPWSTR = 31

_ole32 = ctypes.WinDLL("ole32")
_kernel32 = ctypes.WinDLL("kernel32")


class GUID(ctypes.Structure):
    _fields_ = [("data1", c_uint32), ("data2", ctypes.c_uint16), ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


def guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("format_id", GUID), ("property_id", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("pointer", c_void_p), ("pointer2", c_void_p)]


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("format_tag", wintypes.WORD),
        ("channels", wintypes.WORD),
        ("samples_per_sec", wintypes.DWORD),
        ("avg_bytes_per_sec", wintypes.DWORD),
        ("block_align", wintypes.WORD),
        ("bits_per_sample", wintypes.WORD),
        ("size", wintypes.WORD),
    ]


_ole32.CoInitializeEx.argtypes = [c_void_p, wintypes.DWORD]
_ole32.CoInitializeEx.restype = ctypes.HRESULT
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, wintypes.DWORD, POINTER(GUID), POINTER(c_void_p)]
_ole32.CoCreateInstance.restype = ctypes.HRESULT
_ole32.CoTaskMemFree.argtypes = [c_void_p]
_ole32.CoTaskMemFree.restype = None
_ole32.PropVariantClear.argtypes = [POINTER(PROPVARIANT)]
_ole32.PropVariantClear.restype = ctypes.HRESULT
_kernel32.QueryPerformanceCounter.argtypes = [POINTER(c_int64)]
_kernel32.QueryPerformanceFrequency.argtypes = [POINTER(c_int64)]

_CLSID_MMDeviceEnumerator = guid("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDeviceEnumerator = guid("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAudioClient = guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
_IID_IAudioCaptureClient = guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317")
_PKEY_Device_FriendlyName = PROPERTYKEY(guid("a45c254e-df1c-4efd-8020-67d146a850e0"), 14)

_ERROR_MESSAGES = {
    0x88890004: "disconnected",
    0x8889000A: "in use by another app (exclusive mode)",
    0x88890008: "audio format not supported",
    0x88890010: "Windows Audio service is not running",
    0x80070005: "access denied (check Windows microphone privacy settings)",
    0x80070490: "device not found",
}


@dataclass(frozen=True)
class AudioDevice:
    """An active endpoint: a microphone or line-in (input), or speakers/headphones (output)."""
    id: str
    name: str
    is_output: bool
    is_default: bool


def init_thread() -> None:
    """Every thread that touches Core Audio must call this first (and uninit_thread when done)."""
    _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)


def uninit_thread() -> None:
    _ole32.CoUninitialize()


def describe(error: Exception) -> str:
    """Turns common Core Audio failures into short, readable messages."""
    code = getattr(error, "winerror", None)
    if code is not None and (code & 0xFFFFFFFF) in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code & 0xFFFFFFFF]
    return str(error)


_frequency = c_int64()
_kernel32.QueryPerformanceFrequency(byref(_frequency))


def now_hns() -> int:
    """The performance counter in 100 ns units: the clock Core Audio timestamps packets with."""
    counter = c_int64()
    _kernel32.QueryPerformanceCounter(byref(counter))
    return counter.value * 10_000_000 // _frequency.value


def pcm_format(sample_rate: int, channels: int, bits_per_sample: int) -> WAVEFORMATEX:
    block_align = channels * bits_per_sample // 8
    return WAVEFORMATEX(1, channels, sample_rate, sample_rate * block_align, block_align, bits_per_sample, 0)


def list_devices() -> list[AudioDevice]:
    """Active inputs then outputs, each with the default device first."""
    # Runs on its own thread so the caller's (e.g. the UI thread's) COM state is left alone.
    result: dict = {}

    def work() -> None:
        init_thread()
        try:
            result["devices"] = _list_devices()
        except Exception as error:  # re-raised on the calling thread
            result["error"] = error
        finally:
            uninit_thread()

    thread = threading.Thread(target=work, name="List audio devices")
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["devices"]


def open_audio_client(device_id: str) -> AudioClient:
    with _DeviceEnumerator.create() as enumerator, enumerator.device(device_id) as device:
        return device.activate_audio_client()


def _list_devices() -> list[AudioDevice]:
    devices = []
    with _DeviceEnumerator.create() as enumerator:
        for flow in (_CAPTURE, _RENDER):
            default_id = enumerator.default_device_id(flow)
            group = []
            for device in enumerator.devices(flow):
                with device:
                    device_id = device.id()
                    name = device.friendly_name() or "Unknown device"
                group.append(AudioDevice(device_id, name, flow == _RENDER, device_id == default_id))
            group.sort(key=lambda d: (not d.is_default, d.name.casefold()))
            devices.extend(group)
    return devices


def _method(index: int, *argtypes):
    """A COM method: entry `index` of the interface's vtable, returning an HRESULT (failures raise OSError)."""
    prototype = WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)

    def call(self, *args):
        return prototype(self._vtable_entry(index))(self.pointer, *args)

    return call


class _Unknown:
    """Owns one reference to a COM interface pointer."""
    _release = WINFUNCTYPE(wintypes.ULONG, c_void_p)

    def __init__(self, pointer: int):
        self.pointer = pointer

    def _vtable_entry(self, index: int) -> int:
        vtable = ctypes.cast(self.pointer, POINTER(c_void_p))[0]
        return ctypes.cast(vtable, POINTER(c_void_p))[index]

    def release(self) -> None:
        if self.pointer:
            self._release(self._vtable_entry(2))(self.pointer)
            self.pointer = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class _DeviceEnumerator(_Unknown):
    _enum_audio_endpoints = _method(3, c_int, wintypes.DWORD, POINTER(c_void_p))
    _get_default_audio_endpoint = _method(4, c_int, c_int, POINTER(c_void_p))
    _get_device = _method(5, c_wchar_p, POINTER(c_void_p))

    @classmethod
    def create(cls) -> _DeviceEnumerator:
        pointer = c_void_p()
        _ole32.CoCreateInstance(byref(_CLSID_MMDeviceEnumerator), None, _CLSCTX_ALL, byref(_IID_IMMDeviceEnumerator), byref(pointer))
        return cls(pointer.value)

    def devices(self, flow: int) -> list[_Device]:
        pointer = c_void_p()
        self._enum_audio_endpoints(flow, _DEVICE_STATE_ACTIVE, byref(pointer))
        with _DeviceCollection(pointer.value) as collection:
            return [collection.item(i) for i in range(collection.count())]

    def default_device_id(self, flow: int) -> str | None:
        pointer = c_void_p()
        try:
            self._get_default_audio_endpoint(flow, _ROLE_CONSOLE, byref(pointer))
        except OSError:
            return None  # no device of this kind
        with _Device(pointer.value) as device:
            return device.id()

    def device(self, device_id: str) -> _Device:
        pointer = c_void_p()
        self._get_device(device_id, byref(pointer))
        return _Device(pointer.value)


class _DeviceCollection(_Unknown):
    _get_count = _method(3, POINTER(c_uint32))
    _item = _method(4, c_uint32, POINTER(c_void_p))

    def count(self) -> int:
        count = c_uint32()
        self._get_count(byref(count))
        return count.value

    def item(self, index: int) -> _Device:
        pointer = c_void_p()
        self._item(index, byref(pointer))
        return _Device(pointer.value)


class _Device(_Unknown):
    _activate = _method(3, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))
    _open_property_store = _method(4, wintypes.DWORD, POINTER(c_void_p))
    _get_id = _method(5, POINTER(c_void_p))

    def id(self) -> str:
        text = c_void_p()
        self._get_id(byref(text))
        try:
            return ctypes.wstring_at(text.value)
        finally:
            _ole32.CoTaskMemFree(text)

    def friendly_name(self) -> str | None:
        pointer = c_void_p()
        self._open_property_store(_STGM_READ, byref(pointer))
        with _PropertyStore(pointer.value) as store:
            return store.get_string(_PKEY_Device_FriendlyName)

    def activate_audio_client(self) -> AudioClient:
        pointer = c_void_p()
        self._activate(byref(_IID_IAudioClient), _CLSCTX_ALL, None, byref(pointer))
        return AudioClient(pointer.value)


class _PropertyStore(_Unknown):
    _get_value = _method(5, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))

    def get_string(self, key: PROPERTYKEY) -> str | None:
        value = PROPVARIANT()
        self._get_value(byref(key), byref(value))
        try:
            return ctypes.wstring_at(value.pointer) if value.vt == _VT_LPWSTR else None
        finally:
            _ole32.PropVariantClear(byref(value))


class AudioClient(_Unknown):
    _initialize = _method(3, c_int, wintypes.DWORD, c_int64, c_int64, POINTER(WAVEFORMATEX), c_void_p)
    _start = _method(10)
    _stop = _method(11)
    _get_service = _method(14, POINTER(GUID), POINTER(c_void_p))

    def initialize(self, stream_flags: int, buffer_hns: int, wave_format: WAVEFORMATEX) -> None:
        self._initialize(SHAREMODE_SHARED, stream_flags, buffer_hns, 0, byref(wave_format), None)

    def start(self) -> None:
        self._start()

    def stop(self) -> None:
        self._stop()

    def capture_client(self) -> CaptureClient:
        pointer = c_void_p()
        self._get_service(byref(_IID_IAudioCaptureClient), byref(pointer))
        return CaptureClient(pointer.value)


class CaptureClient(_Unknown):
    _get_buffer = _method(3, POINTER(c_void_p), POINTER(c_uint32), POINTER(c_uint32), POINTER(c_uint64), POINTER(c_uint64))
    _release_buffer = _method(4, c_uint32)
    _get_next_packet_size = _method(5, POINTER(c_uint32))

    def __init__(self, pointer: int):
        super().__init__(pointer)
        self._data, self._frames, self._flags = c_void_p(), c_uint32(), c_uint32()
        self._device_position, self._qpc_position = c_uint64(), c_uint64()

    def next_packet_frames(self) -> int:
        self._get_next_packet_size(byref(self._frames))
        return self._frames.value

    def read(self, block_align: int) -> tuple[bytes, bool, int]:
        """Returns the next packet: (audio bytes, whether it is silence, capture time in 100 ns units)."""
        self._get_buffer(byref(self._data), byref(self._frames), byref(self._flags),
                         byref(self._device_position), byref(self._qpc_position))
        size = self._frames.value * block_align
        silent = bool(self._flags.value & BUFFERFLAGS_SILENT)
        data = bytes(size) if silent or not size else ctypes.string_at(self._data.value, size)
        self._release_buffer(self._frames.value)
        return data, silent, self._qpc_position.value
