"""After a recording: merge it into one MP3, turn every voice into text, then remove the WAV files.

Needs faster-whisper (requirements.txt): offline speech-to-text, plus PyAV for the MP3 encoding.
Can also be run by hand on a recording folder:  python -m audiorecorder.postprocess "<folder>" [--keep-wav]
"""
from __future__ import annotations

import importlib.util
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .recording import BLOCK_ALIGN, BYTES_PER_SECOND, BITS_PER_SAMPLE, CHANNELS, MIX_NAME, SAMPLE_RATE

MODEL = "large-v3-turbo"  # Whisper, run offline on the CPU
MODEL_FOLDER = Path(__file__).resolve().parent.parent / "models" / "whisper-large-v3-turbo"  # shipped in the repo
MP3_KBPS = 128
SILENCE_PEAK = 64             # a track whose loudest sample stays below this (of 32768) holds no sound
SAME_TURN_GAP_SECONDS = 1.5   # one speaker's consecutive sentences closer than this form one paragraph

_SESSION_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}( \(\d+\))?$")
_PART_SUFFIX = re.compile(r" \(part (\d+)\)$")

Progress = Callable[[str, "float | None"], None]  # (what is happening, fraction done or None if unknown)


@dataclass
class Track:
    name: str          # file name without extension, e.g. "Input - Microphone (USB PnP Audio Device)"
    parts: list[Path]  # the WAV file, then its "(part 2)", "(part 3)"... continuations
    label: str = ""    # who is speaking in the transcript, e.g. "Microphone"


@dataclass
class Line:
    start: float
    end: float
    speaker: str
    text: str


def is_available() -> bool:
    """Whether the MP3 and speech-to-text packages are installed (checked without loading them)."""
    return all(importlib.util.find_spec(name) is not None for name in ("av", "faster_whisper"))


def default_languages() -> list[str]:
    """The Windows display language (e.g. "fr") plus English."""
    import ctypes

    name = ctypes.create_unicode_buffer(85)
    ctypes.WinDLL("kernel32").GetUserDefaultLocaleName(name, len(name))
    return list(dict.fromkeys([name.value.split("-")[0].lower() or "en", "en"]))


def pending_recordings(root_folder: Path) -> list[Path]:
    """Recording folders that still hold WAV files but have no transcript yet, oldest first."""
    if not root_folder.is_dir():
        return []
    return [folder for folder in sorted(root_folder.iterdir())
            if folder.is_dir() and _SESSION_FOLDER.match(folder.name)
            and any(folder.glob("*.wav")) and not (folder / f"{folder.name}.txt").exists()]


class Transcriber:
    """Keeps the speech model loaded between recordings."""

    def __init__(self, languages: list[str] | None = None):
        self._model = None
        # Whisper guesses wildly on short or unclear audio (e.g. Russian for a French speaker),
        # so each track is transcribed in whichever of these languages it sounds most like.
        self.languages = languages or default_languages()

    def transcribe(self, tracks: list[Track], progress: Progress) -> list[Line]:
        if not tracks:
            return []
        model = self._load(progress)
        total = sum(_duration(part) for track in tracks for part in track.parts) or 1.0
        done, lines, started = 0.0, [], time.monotonic()
        progress("Transcribing… 0%", 0.0)
        for track in tracks:
            offset = 0.0  # every track starts at the same moment, so times line up across tracks
            for part in track.parts:
                audio = _speech_audio(part)
                try:
                    _, _, likelihoods = model.detect_language(audio, vad_filter=True, language_detection_segments=3)
                except Exception:  # e.g. no speech at all in this part
                    likelihoods = []
                language = max(self.languages, key=lambda code: dict(likelihoods).get(code, 0.0))
                segments, _ = model.transcribe(audio, language=language, vad_filter=True,
                                               condition_on_previous_text=False, word_timestamps=True)
                for segment in segments:
                    # Whisper can join one speaker's sentences across a pause in which someone else spoke,
                    # so split at pauses using the word timings.
                    for words in _split_at_pauses(segment.words or []):
                        text = "".join(word.word for word in words).strip()
                        if text:
                            lines.append(Line(offset + words[0].start, offset + words[-1].end, track.label, text))
                    fraction = min(0.99, (done + segment.end) / total)
                    progress(f"Transcribing… {int(100 * fraction)}%{_time_left(started, fraction)}", fraction)
                offset += _duration(part)
                done += _duration(part)
        return _merge_turns(sorted(lines, key=lambda line: line.start))

    def _load(self, progress: Progress):
        if self._model is None:
            from faster_whisper import WhisperModel, download_model

            if not _model_present():
                progress("Downloading speech model (1.6 GB, once)…", None)
                os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
                try:
                    download_model(MODEL, output_dir=str(MODEL_FOLDER))
                except Exception:
                    pass  # reported just below
                # When Hugging Face can't be reached, the download quietly leaves the folder as it was.
                if not _model_present():
                    raise RuntimeError('speech model missing. In the app folder, run "git lfs pull" '
                                       "(or connect to the internet so it can be downloaded).")
            progress("Loading speech model…", None)
            self._model = WhisperModel(str(MODEL_FOLDER), device="cpu", compute_type="int8",
                                       cpu_threads=max(1, (os.cpu_count() or 2) // 2))
        return self._model


def process_recording(folder: Path, transcriber: Transcriber, progress: Progress, delete_wav: bool = True) -> str:
    """Creates "<folder>.mp3" and "<folder>.txt" in the folder; returns a one-line summary."""
    tracks, mix = find_tracks(folder)
    if not tracks:
        raise ValueError("no WAV files to process")

    mp3 = folder / f"{folder.name}.mp3"
    encode_mp3((mix or tracks[0]).parts, mp3, lambda fraction: progress("Creating MP3…", fraction))

    progress("Looking for voices…", None)
    voiced = [track for track in tracks if not _is_silent(track)]
    lines = transcriber.transcribe(voiced, progress)
    transcript = folder / f"{folder.name}.txt"
    _write_transcript(transcript, folder.name, lines)

    if delete_wav:
        for track in tracks + ([mix] if mix else []):
            for part in track.parts:
                part.unlink(missing_ok=True)
    return "MP3 + transcript saved" if lines else "MP3 saved · no speech found"


def find_tracks(folder: Path) -> tuple[list[Track], Track | None]:
    """The per-device tracks (with transcript labels) and the mixed track, if any."""
    groups: dict[str, list[tuple[int, Path]]] = {}
    for path in folder.glob("*.wav"):
        match = _PART_SUFFIX.search(path.stem)
        base = path.stem[:match.start()] if match else path.stem
        groups.setdefault(base, []).append((int(match.group(1)) if match else 1, path))
    tracks = [Track(base, [path for _, path in sorted(parts)]) for base, parts in sorted(groups.items())]
    mix = next((track for track in tracks if track.name == MIX_NAME), None)
    tracks = [track for track in tracks if track is not mix]

    # "Input - Microphone (USB PnP Audio Device)" -> "Microphone", unless two devices would share a label.
    short = [re.sub(r"\s*\([^()]*\)$", "", re.sub(r"^(Input|Output) - ", "", t.name)) or t.name for t in tracks]
    for track, label in zip(tracks, short):
        track.label = label if short.count(label) == 1 else re.sub(r"^(Input|Output) - ", "", track.name)
    return tracks, mix


def encode_mp3(parts: list[Path], target: Path, on_progress: Callable[[float], None] = lambda _: None) -> None:
    import av
    import numpy as np

    total = sum(_duration(part) for part in parts) * BYTES_PER_SECOND or 1
    encoded = 0
    on_progress(0.0)
    unfinished = target.with_name(target.name + ".part")
    with av.open(str(unfinished), "w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=SAMPLE_RATE, layout="stereo")
        stream.bit_rate = MP3_KBPS * 1000
        for part in parts:
            for chunk in _pcm_chunks(part):
                frame = av.AudioFrame.from_ndarray(np.frombuffer(chunk, "<i2").reshape(1, -1), format="s16", layout="stereo")
                frame.sample_rate = SAMPLE_RATE
                container.mux(stream.encode(frame))  # PyAV converts to the encoder's sample format
                encoded += len(chunk)
                on_progress(min(1.0, encoded / total))
        container.mux(stream.encode(None))
    os.replace(unfinished, target)


class Processor:
    """Processes finished recordings one at a time, each in a separate low-priority process.

    A separate process keeps the heavy speech model from slowing down the window or a recording.
    Does nothing (the WAV files stay) when the packages are missing."""

    def __init__(self, delete_wav: bool):
        # What it is doing right now, for the UI: (text, fraction done or None if unknown).
        self.status: tuple[str, float | None] | None = None
        self.results: queue.Queue[tuple[Path, str]] = queue.Queue()  # (folder, summary) of finished jobs
        self._delete_wav = delete_wav
        self._available = is_available()
        self._jobs: queue.Queue[Path] = queue.Queue()
        self._queued: set[Path] = set()
        self._child: subprocess.Popen | None = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, name="Post-processing", daemon=True).start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._queued)

    def add(self, folder: Path) -> None:
        if not self._available:
            return
        with self._lock:
            if folder in self._queued:
                return
            self._queued.add(folder)
        self._jobs.put(folder)

    def stop(self) -> None:
        """Abandons the current job; it is picked up again the next time the app starts."""
        child = self._child
        if child is not None and child.poll() is None:
            child.kill()

    def _run(self) -> None:
        while True:
            folder = self._jobs.get()
            try:
                self.results.put((folder, self._process_in_child(folder)))
            except Exception as error:
                self.results.put((folder, f"MP3/transcript failed, WAV files kept: {error}"))
            finally:
                self.status = None
                with self._lock:
                    self._queued.discard(folder)

    def _process_in_child(self, folder: Path) -> str:
        command = [sys.executable, "-m", "audiorecorder.postprocess", "--report", str(folder)]
        if not self._delete_wav:
            command.append("--keep-wav")
        self._child = subprocess.Popen(
            command, cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        summary, last_output = None, "stopped"
        for line in self._child.stdout:
            kind, _, text = line.rstrip("\n").partition(" ")
            if kind == "status:":
                fraction, _, text = text.partition(" ")
                self.status = (text, None if fraction == "-" else float(fraction))
            elif kind == "done:":
                summary = text
            elif line.strip():
                # e.g. the last line of a Python error, without its "RuntimeError: " prefix
                last_output = re.sub(r"^\w+(Error|Exception): ", "", line.strip())
        if self._child.wait() != 0 or summary is None:
            raise RuntimeError(last_output)
        return summary


def _model_present() -> bool:
    # A clone made without Git LFS holds a ~130-byte placeholder instead of the 1.6 GB model.
    weights = MODEL_FOLDER / "model.bin"
    return weights.is_file() and weights.stat().st_size > 1_000_000


def _time_left(started: float, fraction: float) -> str:
    elapsed = time.monotonic() - started
    if fraction < 0.03 or elapsed < 3:
        return ""  # too early to tell
    left = elapsed * (1 - fraction) / fraction
    return " · less than a minute left" if left < 60 else f" · about {round(left / 60)} min left"


def _split_at_pauses(words: list) -> list[list]:
    runs: list[list] = []
    for word in words:
        if runs and word.start - runs[-1][-1].end < SAME_TURN_GAP_SECONDS:
            runs[-1].append(word)
        else:
            runs.append([word])
    return runs


def _merge_turns(lines: list[Line]) -> list[Line]:
    merged: list[Line] = []
    for line in lines:
        previous = merged[-1] if merged else None
        if previous and previous.speaker == line.speaker and line.start - previous.end < SAME_TURN_GAP_SECONDS:
            previous.text += " " + line.text
            previous.end = max(previous.end, line.end)
        else:
            merged.append(Line(line.start, line.end, line.speaker, line.text))
    return merged


def _write_transcript(path: Path, title: str, lines: list[Line]) -> None:
    body = "\n\n".join(f"[{_timestamp(line.start)}] {line.speaker}: {line.text}" for line in lines)
    path.write_text(f"Transcript — {title}\n\n{body or '(No speech detected.)'}\n", encoding="utf-8")


def _timestamp(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def _pcm_chunks(path: Path, size: int = 1 << 20):
    """The raw samples of one of our WAV files, read to the end even if the header is stale."""
    with open(path, "rb") as file:
        header = file.read(44)
        riff, wave_id, fmt_id, tag, channels, rate, _, bits, data_id = struct.unpack("<4s4x4s4s4xHHII2xH4s4x", header)
        if (riff, wave_id, fmt_id, data_id) != (b"RIFF", b"WAVE", b"fmt ", b"data") or (tag, channels, rate, bits) != (
                1, CHANNELS, SAMPLE_RATE, BITS_PER_SAMPLE):
            raise ValueError(f"{path.name} is not a recording made by this app")
        while chunk := file.read(size):
            yield chunk[:len(chunk) - len(chunk) % BLOCK_ALIGN]


def _speech_audio(path: Path):
    """The track as Whisper wants it: 16 kHz mono float samples."""
    import av
    import numpy as np

    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    pieces = []
    for chunk in _pcm_chunks(path):
        frame = av.AudioFrame.from_ndarray(np.frombuffer(chunk, "<i2").reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        pieces += [converted.to_ndarray()[0] for converted in resampler.resample(frame)]
    pieces += [converted.to_ndarray()[0] for converted in resampler.resample(None)]
    return np.concatenate(pieces) if pieces else np.zeros(0, np.float32)


def _duration(path: Path) -> float:
    return max(0, path.stat().st_size - 44) / BYTES_PER_SECOND


def _is_silent(track: Track) -> bool:
    import numpy as np

    for part in track.parts:
        for chunk in _pcm_chunks(part):
            samples = np.frombuffer(chunk, "<i2")
            if samples.size and (samples.max() >= SILENCE_PEAK or samples.min() <= -SILENCE_PEAK):
                return False
    return True


def main(arguments: list[str]) -> None:
    keep_wav = "--keep-wav" in arguments
    report = "--report" in arguments  # line-based progress for the app: "status: ..." then "done: ..."
    folders = [Path(argument) for argument in arguments if not argument.startswith("--")]
    if not folders:
        sys.exit(__doc__)
    if report:
        sys.stdout.reconfigure(encoding="utf-8")
    transcriber = Transcriber()
    for folder in folders:
        last = [""]

        def show(text: str, fraction: float | None) -> None:
            fraction_text = "-" if fraction is None else f"{fraction:.3f}"
            if (text, fraction_text) != last[0]:
                if report:
                    print(f"status: {fraction_text} {text}", flush=True)
                else:
                    percent = "" if fraction is None or "%" in text else f" {fraction:.0%}"
                    print(f"\r{text + percent:<60}", end="", flush=True)
                last[0] = (text, fraction_text)

        summary = process_recording(folder, transcriber, show, delete_wav=not keep_wav)
        print(f"done: {summary}" if report else f"\r{summary:<60}\n  {folder}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
