"""The recording engine: one WAV file per device, plus a mix of every device, in a timestamped folder."""
from __future__ import annotations

import re
import struct
import threading
import time
from array import array
from datetime import datetime
from itertools import zip_longest
from pathlib import Path

from . import wasapi
from .wasapi import AudioDevice

# Every track is recorded in this format; Windows converts each device's native format to it.
SAMPLE_RATE = 48000
CHANNELS = 2
BITS_PER_SAMPLE = 16
BLOCK_ALIGN = CHANNELS * BITS_PER_SAMPLE // 8
BYTES_PER_SECOND = SAMPLE_RATE * BLOCK_ALIGN

HNS_PER_SECOND = 10_000_000  # Core Audio counts time in 100 ns units

MIX_NAME = "All devices mixed"


class RecordingSession:
    """Records several devices at once into one folder."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.recorders: list[DeviceRecorder] = []
        self._start_signal = threading.Event()
        self._mixer: Mixer | None = None

    @classmethod
    def start(cls, root_folder: Path, devices: list[AudioDevice]) -> RecordingSession:
        session = cls(_create_session_folder(root_folder))
        queues = []
        used_names: set[str] = set()
        for device in devices:
            queue = SampleQueue() if len(devices) > 1 else None
            if queue is not None:
                queues.append(queue)
            prefix = "Output - " if device.is_output else "Input - "
            name = _unique_name(prefix + _safe_file_name(device.name), used_names)
            session.recorders.append(DeviceRecorder(device, session.folder / f"{name}.wav", session._start_signal, queue))
        if len(queues) > 1:
            session._mixer = Mixer(session.folder / f"{MIX_NAME}.wav", queues)

        for recorder in session.recorders:
            recorder.start()
        # Let every device finish opening, then start them all at once so the tracks line up.
        deadline = time.monotonic() + 3
        for recorder in session.recorders:
            recorder.wait_until_ready(max(0.0, deadline - time.monotonic()))
        session._start_signal.set()
        if session._mixer is not None:
            session._mixer.start()
        return session

    @property
    def bytes_written(self) -> int:
        total = self._mixer.bytes_written if self._mixer is not None else 0
        return total + sum(recorder.bytes_written for recorder in self.recorders)

    def stop(self) -> None:
        for recorder in self.recorders:
            recorder.request_stop()
        self._start_signal.set()
        for recorder in self.recorders:
            recorder.join()
        if self._mixer is not None:
            self._mixer.stop_and_join()
        try:
            self.folder.rmdir()  # only succeeds when nothing was recorded
        except OSError:
            pass


class DeviceRecorder:
    """Records one device to a WAV file on its own thread. Outputs are captured in loopback mode."""

    CAPTURE_BUFFER_HNS = 2 * HNS_PER_SECOND     # slack for slow disks
    IDLE_AFTER_HNS = HNS_PER_SECOND // 5        # no loopback data for 200 ms: nothing is playing,
    IDLE_LAG_HNS = HNS_PER_SECOND // 10         # so fill silence up to 100 ms ago
    RESUME_AFTER_HNS = HNS_PER_SECOND // 10
    MIN_GAP_FRAMES = SAMPLE_RATE // 100

    def __init__(self, device: AudioDevice, path: Path, start_signal: threading.Event, mix_queue: SampleQueue | None):
        self.device = device
        self.path = path
        self.error: str | None = None  # set when the device failed or was disconnected
        self.bytes_written = 0
        self._start_signal = start_signal
        self._mix_queue = mix_queue
        self._ready = threading.Event()
        self._stop_requested = False
        self._peak = 0
        self._wav: WavWriter | None = None
        self._start_hns = self._last_data_hns = self._frames_written = 0
        self._thread = threading.Thread(target=self._run, name=f"Recorder: {device.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait_until_ready(self, timeout: float) -> None:
        self._ready.wait(timeout)

    def request_stop(self) -> None:
        self._stop_requested = True

    def join(self) -> None:
        self._thread.join()

    def take_peak(self) -> float:
        """The loudest sample since the last call, from 0 to 1."""
        peak, self._peak = self._peak, 0
        return peak / 32768

    def _run(self) -> None:
        wasapi.init_thread()
        client = capture = None
        try:
            client = wasapi.open_audio_client(self.device.id)
            flags = wasapi.STREAMFLAGS_AUTOCONVERTPCM | wasapi.STREAMFLAGS_SRC_DEFAULT_QUALITY
            if self.device.is_output:
                flags |= wasapi.STREAMFLAGS_LOOPBACK
            client.initialize(flags, self.CAPTURE_BUFFER_HNS, wasapi.pcm_format(SAMPLE_RATE, CHANNELS, BITS_PER_SAMPLE))
            capture = client.capture_client()
            self._wav = WavWriter(self.path)

            self._ready.set()
            self._start_signal.wait()
            if self._stop_requested:
                return
            client.start()
            self._start_hns = self._last_data_hns = wasapi.now_hns()

            while not self._stop_requested:
                self._drain(capture)
                if self.device.is_output and wasapi.now_hns() - self._last_data_hns > self.IDLE_AFTER_HNS:
                    self._pad_until(wasapi.now_hns() - self.IDLE_LAG_HNS)
                time.sleep(0.01)
            self._drain(capture)
            if self.device.is_output and wasapi.now_hns() - self._last_data_hns > self.RESUME_AFTER_HNS:
                self._pad_until(wasapi.now_hns())
            client.stop()
        except Exception as error:
            self.error = wasapi.describe(error)
        finally:
            self._ready.set()
            if self._wav is not None:
                try:
                    self._wav.close()
                except OSError as error:
                    self.error = self.error or str(error)
            if self._mix_queue is not None:
                self._mix_queue.close()
            for com_object in (capture, client):
                if com_object is not None:
                    com_object.release()
            wasapi.uninit_thread()

    def _drain(self, capture: wasapi.CaptureClient) -> None:
        while capture.next_packet_frames():
            data, silent, captured_hns = capture.read(BLOCK_ALIGN)
            if self.device.is_output:
                # Loopback delivers nothing while the device is quiet. When sound resumes,
                # fill the gap with silence so the track stays in step with the clock.
                now = wasapi.now_hns()
                if now - self._last_data_hns > self.RESUME_AFTER_HNS:
                    if abs(now - captured_hns) > HNS_PER_SECOND:
                        captured_hns = now - len(data) // BLOCK_ALIGN * HNS_PER_SECOND // SAMPLE_RATE
                    self._pad_until(captured_hns)
                self._last_data_hns = now
            self._write(data)
            if data and not silent:
                samples = memoryview(data).cast("h")
                self._peak = max(self._peak, max(samples), -min(samples))

    def _pad_until(self, hns: int) -> None:
        expected_hns = self._start_hns + self._frames_written * HNS_PER_SECOND // SAMPLE_RATE
        missing_frames = (hns - expected_hns) * SAMPLE_RATE // HNS_PER_SECOND
        while missing_frames >= self.MIN_GAP_FRAMES:
            frames = min(missing_frames, SAMPLE_RATE // 10)
            self._write(bytes(frames * BLOCK_ALIGN))
            missing_frames -= frames

    def _write(self, data: bytes) -> None:
        self._wav.write(data)
        if self._mix_queue is not None:
            self._mix_queue.put(data)
        self._frames_written += len(data) // BLOCK_ALIGN
        self.bytes_written += len(data)


class Mixer:
    """Sums every device's samples into one extra track."""

    MAX_LAG_BYTES = BYTES_PER_SECOND * 2  # a stalled device delays the mix by at most 2 s

    def __init__(self, path: Path, inputs: list[SampleQueue]):
        self._inputs = inputs
        self._wav = WavWriter(path)
        self._stop_requested = False
        self._thread = threading.Thread(target=self._run, name="Mixer", daemon=True)

    @property
    def bytes_written(self) -> int:
        return self._wav.total_bytes

    def start(self) -> None:
        self._thread.start()

    def stop_and_join(self) -> None:
        self._stop_requested = True
        if self._thread.is_alive():
            self._thread.join()
        else:
            self._wav.close()

    def _run(self) -> None:
        try:
            while not self._stop_requested:
                time.sleep(0.1)
                self._mix_available(flush=False)
            self._mix_available(flush=True)
        except Exception:
            pass  # e.g. disk full: the per-device tracks report their own errors
        finally:
            try:
                self._wav.close()
            except OSError:
                pass

    def _mix_available(self, flush: bool) -> None:
        # Mix as far as every still-running device has delivered.
        ready, buffered = None, 0
        for queue in self._inputs:
            closed = queue.closed
            count = len(queue)
            buffered = max(buffered, count)
            if not closed:
                ready = count if ready is None else min(ready, count)
        size = buffered if flush or ready is None else max(ready, buffered - self.MAX_LAG_BYTES)
        size -= size % BLOCK_ALIGN
        if size <= 0:
            return
        tracks = [array("h", queue.take(size)) for queue in self._inputs]
        sums = map(sum, zip_longest(*tracks, fillvalue=0))
        mixed = array("h", (32767 if v > 32767 else -32768 if v < -32768 else v for v in sums))
        self._wav.write(mixed.tobytes())


class SampleQueue:
    """Thread-safe FIFO of audio bytes feeding the mixer."""

    def __init__(self):
        self.closed = False
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    def put(self, data: bytes) -> None:
        with self._lock:
            self._buffer += data

    def take(self, size: int) -> bytes:
        with self._lock:
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            return chunk

    def close(self) -> None:
        self.closed = True


class WavWriter:
    """Writes 16-bit PCM WAV files, continuing in a new part file before the 4 GB format limit."""

    MAX_DATA_BYTES = 4_000_000_000
    HEADER_REFRESH_BYTES = BYTES_PER_SECOND * 5

    def __init__(self, path: Path):
        self.total_bytes = 0
        self._first_path = path
        self._paths: list[Path] = []
        self._file = None
        self._data_bytes = self._bytes_since_header = 0
        self._open(path)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self._data_bytes == self.MAX_DATA_BYTES:
                self._start_next_part()
            size = min(len(view), self.MAX_DATA_BYTES - self._data_bytes)
            self._file.write(view[:size])
            self._data_bytes += size
            self._bytes_since_header += size
            self.total_bytes += size
            view = view[size:]
        # Keep the header current so a crash or power cut still leaves a playable file.
        if self._bytes_since_header >= self.HEADER_REFRESH_BYTES:
            self._update_header()

    def close(self) -> None:
        if self._file is None:
            return
        self._update_header()
        self._file.close()
        self._file = None
        if self.total_bytes == 0:
            for path in self._paths:
                path.unlink(missing_ok=True)

    def _start_next_part(self) -> None:
        self._update_header()
        self._file.close()
        part = len(self._paths) + 1
        self._open(self._first_path.with_name(f"{self._first_path.stem} (part {part}).wav"))

    def _open(self, path: Path) -> None:
        self._file = open(path, "xb", buffering=1 << 16)
        self._paths.append(path)
        self._data_bytes = 0
        self._file.write(struct.pack(
            "<4sI4s4sIHHIIHH4sI", b"RIFF", 36, b"WAVE", b"fmt ", 16, 1, CHANNELS, SAMPLE_RATE,
            BYTES_PER_SECOND, BLOCK_ALIGN, BITS_PER_SAMPLE, b"data", 0))

    def _update_header(self) -> None:
        position = self._file.tell()
        self._file.seek(4)
        self._file.write(struct.pack("<I", 36 + self._data_bytes))
        self._file.seek(40)
        self._file.write(struct.pack("<I", self._data_bytes))
        self._file.seek(position)
        self._file.flush()
        self._bytes_since_header = 0


def _create_session_folder(root_folder: Path) -> Path:
    base = root_folder / datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    folder, attempt = base, 2
    while folder.exists():
        folder = base.with_name(f"{base.name} ({attempt})")
        attempt += 1
    folder.mkdir(parents=True)
    return folder


def _safe_file_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")


def _unique_name(name: str, used: set[str]) -> str:
    candidate, attempt = name, 2
    while candidate.casefold() in used:
        candidate = f"{name} ({attempt})"
        attempt += 1
    used.add(candidate.casefold())
    return candidate
