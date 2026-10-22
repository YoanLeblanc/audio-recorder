"""The Audio Recorder window."""
from __future__ import annotations

import ctypes
import math
import os
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import postprocess, wasapi
from .recording import DeviceRecorder, RecordingSession
from .settings import Settings

APP_NAME = "Audio Recorder"
RECORDING_TITLE = "● Recording — " + APP_NAME

WHITE = "#ffffff"
INK = "#18181b"
INK_SOFT = "#3f3f46"
MUTED = "#71717a"
DIVIDER = "#e4e4e7"
LINK = "#2563eb"
GREEN = "#22c55e"
RED, RED_HOVER, RED_PRESSED = "#dc2626", "#ef4444", "#b91c1c"
DARK, DARK_HOVER, DARK_PRESSED = "#27272a", "#3f3f46", "#18181b"
DISABLED_BG, DISABLED_FG = "#e4e4e7", "#a1a1aa"

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
_kernel32.SetThreadExecutionState.restype = wintypes.DWORD
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_user32 = ctypes.WinDLL("user32")
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_ES_CONTINUOUS, _ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
_ERROR_ALREADY_EXISTS = 183


def main() -> None:
    _mutex = _kernel32.CreateMutexW(None, False, "AudioRecorder-SingleInstance-5c1f0e2a")
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        _activate_running_instance()
        return
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except (OSError, AttributeError):
        pass
    # Group under our own taskbar icon rather than Python's.
    ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID("AudioRecorder")
    root = tk.Tk()
    App(root)
    root.mainloop()


@dataclass
class _DeviceRow:
    device: wasapi.AudioDevice
    selected: tk.BooleanVar
    check: ttk.Checkbutton
    meter: LevelMeter
    recorder: DeviceRecorder | None = None
    level: float = 0.0
    shows_error: bool = False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        self.scale = root.winfo_fpixels("1i") / 96
        self.session: RecordingSession | None = None
        self.rows: list[_DeviceRow] = []
        self.saved_folder: Path | None = None
        self.saved_summary: str | None = None
        self.started_at = 0.0
        self.ticks = 0
        self.button_hovered = False
        self.body_font = tkfont.Font(root, family="Segoe UI", size=9)
        self.bold_font = tkfont.Font(root, family="Segoe UI Semibold", size=9)
        self.link_hover_font = tkfont.Font(root, family="Segoe UI", size=9, underline=True)
        self.content_width = self.px(412)
        self.name_width = self.content_width - self.px(84 + 16 + 26)  # room for the meter and the tick box

        # Every recording becomes one MP3 plus a transcript, including any left unfinished last time.
        self.processor = postprocess.Processor(delete_wav=self.settings.delete_wav)
        for folder in postprocess.pending_recordings(self.settings.folder):
            self.processor.add(folder)

        self._build()
        self.refresh_devices()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(50, self._tick)

    # ----- layout -------------------------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.title(APP_NAME)
        root.configure(bg=WHITE)
        root.resizable(False, False)
        self.icons = [_make_icon(root, size) for size in (48, 32, 16)]
        root.iconphoto(True, *self.icons)

        style = ttk.Style(root)
        for name, color, disabled_color in (("Device.TCheckbutton", INK, INK_SOFT), ("DeviceError.TCheckbutton", RED, RED)):
            style.configure(name, background=WHITE, foreground=color, font=self.body_font)
            style.map(name, foreground=[("disabled", disabled_color)], background=[("active", WHITE)])

        outer = tk.Frame(root, bg=WHITE, padx=self.px(24), pady=self.px(22))
        outer.pack(fill="both", expand=True)
        tk.Frame(outer, width=self.content_width, height=0, bg=WHITE).pack()  # fixes the window width

        header = tk.Frame(outer, bg=WHITE)
        header.pack(fill="x")
        self.record_button = tk.Button(
            header, font=("Segoe UI Semibold", 11), width=10, pady=self.px(9), fg=WHITE, activeforeground=WHITE,
            relief="flat", bd=0, highlightthickness=0, command=self.toggle_recording)
        self.record_button.pack(side="left")
        self.record_button.bind("<Enter>", lambda _: self._paint_button(hovered=True))
        self.record_button.bind("<Leave>", lambda _: self._paint_button(hovered=False))

        info = tk.Frame(header, bg=WHITE)
        info.pack(side="left", padx=(self.px(20), 0))
        self.time_label = tk.Label(info, text=_format_elapsed(0), font=("Segoe UI Semibold", 22), fg=MUTED, bg=WHITE, bd=0, padx=0, pady=0)
        self.time_label.pack(anchor="w")
        status_row = tk.Frame(info, bg=WHITE)
        status_row.pack(anchor="w")
        self.status_label = tk.Label(status_row, font=self.body_font, fg=MUTED, bg=WHITE, bd=0, padx=0, pady=0)
        self.status_label.pack(side="left")
        self.open_link = self._link(status_row, "Open folder", lambda: _open_folder(self.saved_folder))
        # Shows how far the MP3 and transcript are. Its space is always reserved so the window never jumps.
        self.progress_bar = ProgressBar(info, self.content_width - self.record_button.winfo_reqwidth() - self.px(20),
                                        self.px(6))
        self.progress_bar.pack(anchor="w", pady=(self.px(7), 0))

        self._divider(outer, top=self.px(20), bottom=self.px(12))
        self.devices_frame = tk.Frame(outer, bg=WHITE)
        self.devices_frame.pack(fill="x")
        self.devices_frame.columnconfigure(0, weight=1)
        self._divider(outer, top=self.px(12), bottom=self.px(12))

        footer = tk.Frame(outer, bg=WHITE)
        footer.pack(fill="x")
        tk.Label(footer, text="Save to", font=self.body_font, fg=MUTED, bg=WHITE, bd=0, padx=0).pack(side="left")
        self.change_link = self._link(footer, "Change…", self.choose_folder)
        self.change_link.pack(side="right")
        self.folder_link = self._link(footer, "", lambda: _open_folder(self.settings.folder))
        self.folder_link.pack(side="left", padx=(self.px(6), 0))
        self._show_folder()

    def refresh_devices(self) -> None:
        if self.session is not None:
            return
        problem = None
        try:
            devices = wasapi.list_devices()
        except Exception as error:
            devices, problem = [], "Could not list audio devices: " + wasapi.describe(error)

        for child in self.devices_frame.winfo_children():
            child.destroy()
        self.rows = []
        line = 0
        for outputs, title in ((False, "Microphones & inputs"), (True, "Speakers & outputs")):
            tk.Label(self.devices_frame, text=title, font=self.bold_font, fg=INK, bg=WHITE, bd=0, padx=0).grid(
                row=line, column=0, sticky="w", pady=(self.px(10) if outputs else 0, self.px(4)))
            if not outputs:
                self.refresh_link = self._link(self.devices_frame, "Refresh", self.refresh_devices)
                self.refresh_link.grid(row=line, column=1, sticky="e")
            line += 1
            group = [device for device in devices if device.is_output == outputs]
            if not group:
                tk.Label(self.devices_frame, text="No devices found", font=self.body_font, fg=MUTED, bg=WHITE, bd=0, padx=0).grid(
                    row=line, column=0, sticky="w", pady=self.px(2))
                line += 1
            for device in group:
                selected = tk.BooleanVar(self.root, value=device.id not in self.settings.excluded_devices)
                row = _DeviceRow(device, selected, ttk.Checkbutton(self.devices_frame, variable=selected),
                                 LevelMeter(self.devices_frame, self.px(84), self.px(6)))
                row.check.configure(command=lambda r=row: self._on_device_toggled(r))
                row.check.grid(row=line, column=0, sticky="w", pady=self.px(2))
                row.meter.grid(row=line, column=1, sticky="e", padx=(self.px(16), 0))
                self._reset_row(row)
                self.rows.append(row)
                line += 1

        self.saved_folder = self.saved_summary = None
        self._update_state()
        if problem:
            self._set_status(problem)

    # ----- recording ----------------------------------------------------------------------------

    def toggle_recording(self) -> None:
        if self.session is None:
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self) -> None:
        selected = [row for row in self.rows if row.selected.get()]
        if not selected:
            return
        for row in self.rows:
            self._reset_row(row)
        self._busy(True)
        try:
            self.session = RecordingSession.start(self.settings.folder, [row.device for row in selected])
        except Exception as error:
            messagebox.showerror(APP_NAME, f"Could not start recording.\n\n{error}", parent=self.root)
            return
        finally:
            self._busy(False)
        for row, recorder in zip(selected, self.session.recorders):
            row.recorder = recorder
        self.saved_folder = self.saved_summary = None
        self.started_at = time.monotonic()
        _kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)  # keep the PC awake
        self._update_state()

    def stop_recording(self) -> None:
        if self.session is None:
            return
        self._busy(True)
        try:
            self.session.stop()
        finally:
            self._busy(False)
        _kernel32.SetThreadExecutionState(_ES_CONTINUOUS)

        for row in self.rows:
            if row.recorder is not None and row.recorder.error and not row.shows_error:
                self._show_row_error(row, row.recorder.error)
            row.recorder = None
            row.level = 0.0
            row.meter.set_level(0.0)
        folder = self.session.folder
        files = list(folder.glob("*.wav")) if folder.exists() else []
        if files:
            size = sum(file.stat().st_size for file in files)
            self.saved_folder = folder
            self.saved_summary = f"Saved {_plural(len(files), 'file')} · {_format_size(size)}"
            self.processor.add(folder)
        else:
            self.saved_folder, self.saved_summary = None, "Nothing was recorded"
        self.session = None
        self._update_state()

    def _tick(self) -> None:
        try:
            while not self.processor.results.empty():
                self.saved_folder, self.saved_summary = self.processor.results.get()
            if self.session is not None:
                self._update_recording()
            else:
                self._update_status()
        finally:
            self.root.after(50, self._tick)

    def _update_recording(self) -> None:
        self.time_label.configure(text=_format_elapsed(time.monotonic() - self.started_at))
        for row in self.rows:
            if row.recorder is None:
                continue
            row.level = max(row.recorder.take_peak(), row.level * 0.8)
            row.meter.set_level(row.level)
            if row.recorder.error and not row.shows_error:
                self._show_row_error(row, row.recorder.error)
        if all(recorder.error for recorder in self.session.recorders):
            self.stop_recording()  # every device failed or was unplugged
            return
        self.ticks += 1
        if self.ticks % 10 == 0:
            self._update_status()

    # ----- state --------------------------------------------------------------------------------

    def _update_state(self) -> None:
        recording = self.session is not None
        for row in self.rows:
            row.check.state(["disabled"] if recording else ["!disabled"])
        self._enable_link(self.refresh_link, not recording)
        self._enable_link(self.change_link, not recording)
        self.time_label.configure(fg=RED if recording else MUTED)
        self.root.title(RECORDING_TITLE if recording else APP_NAME)
        self._paint_button()
        self._update_status()

    def _update_status(self) -> None:
        processing = self.processor.status
        if self.session is None and processing is not None:
            text, fraction = processing
            self._set_status(text)
            self.progress_bar.show(fraction)
            return
        self.progress_bar.hide()
        if self.session is not None:
            failed = sum(1 for recorder in self.session.recorders if recorder.error)
            text = (f"Recording {_plural(len(self.session.recorders) - failed, 'device')}"
                    f" · {_format_size(self.session.bytes_written)}")
            if failed:
                text += f" · {failed} stopped"
            self._set_status(text)
        elif self.saved_summary is not None:
            self._set_status(self.saved_summary, show_open_link=self.saved_folder is not None)
        else:
            count = sum(1 for row in self.rows if row.selected.get())
            self._set_status(f"Ready to record {_plural(count, 'device')}" if count else "Select a device to record")

    def _set_status(self, text: str, show_open_link: bool = False) -> None:
        if self.status_label.cget("text") == text and bool(self.open_link.winfo_manager()) == show_open_link:
            return
        self.status_label.configure(text=text)
        if show_open_link:
            self.open_link.pack(side="left", padx=(self.px(10), 0))
        else:
            self.open_link.pack_forget()

    def _paint_button(self, hovered: bool | None = None) -> None:
        if hovered is not None:
            self.button_hovered = hovered
        recording = self.session is not None
        enabled = recording or any(row.selected.get() for row in self.rows)
        if not enabled:
            background = pressed = DISABLED_BG
        elif recording:
            background, pressed = (DARK_HOVER if self.button_hovered else DARK), DARK_PRESSED
        else:
            background, pressed = (RED_HOVER if self.button_hovered else RED), RED_PRESSED
        self.record_button.configure(
            text="■  Stop" if recording else "●  Record", bg=background, activebackground=pressed,
            state="normal" if enabled else "disabled", disabledforeground=DISABLED_FG, cursor="hand2" if enabled else "")

    def _on_device_toggled(self, row: _DeviceRow) -> None:
        if row.selected.get():
            self.settings.excluded_devices.discard(row.device.id)
        else:
            self.settings.excluded_devices.add(row.device.id)
        self.settings.save()
        self._update_state()

    def _reset_row(self, row: _DeviceRow) -> None:
        row.shows_error = False
        row.level = 0.0
        row.meter.set_level(0.0)
        suffix = "  (default)" if row.device.is_default else ""
        row.check.configure(style="Device.TCheckbutton", text=self._fit_name(row.device.name, suffix))

    def _show_row_error(self, row: _DeviceRow, message: str) -> None:
        row.shows_error = True
        row.check.configure(style="DeviceError.TCheckbutton", text=self._fit_name(row.device.name, " — " + message))

    def _fit_name(self, name: str, suffix: str) -> str:
        return _ellipsize(name, self.body_font, self.name_width - self.body_font.measure(suffix)) + suffix

    # ----- folder -------------------------------------------------------------------------------

    def choose_folder(self) -> None:
        if self.session is not None:
            return
        chosen = filedialog.askdirectory(
            parent=self.root, title="Choose where recordings are saved", mustexist=False,
            initialdir=str(self.settings.folder) if self.settings.folder.exists() else None)
        if not chosen:
            return
        self.settings.folder = Path(os.path.normpath(chosen))
        self.settings.save()
        self._show_folder()

    def _show_folder(self) -> None:
        available = (self.content_width - self.body_font.measure("Save to") - self.body_font.measure("Change…")
                     - self.px(30))
        self.folder_link.configure(text=_ellipsize_middle(str(self.settings.folder), self.body_font, available))

    def close(self) -> None:
        if self.session is not None:
            if not messagebox.askyesno(APP_NAME, "A recording is in progress. Stop recording and quit?", parent=self.root):
                return
            self.stop_recording()
        if self.processor.busy and not messagebox.askyesno(
                APP_NAME, "The MP3 and transcript are still being made. Quit anyway?\n\n"
                          "They will be finished the next time you open Audio Recorder.", parent=self.root):
            return
        self.processor.stop()
        self.root.destroy()

    # ----- helpers ------------------------------------------------------------------------------

    def px(self, pixels: float) -> int:
        return round(pixels * self.scale)

    def _busy(self, busy: bool) -> None:
        self.root.configure(cursor="watch" if busy else "")
        self.root.update_idletasks()

    def _divider(self, parent: tk.Widget, top: int, bottom: int) -> None:
        tk.Frame(parent, height=1, bg=DIVIDER).pack(fill="x", pady=(top, bottom))

    def _link(self, parent: tk.Widget, text: str, action) -> tk.Label:
        label = tk.Label(parent, text=text, font=self.body_font, fg=LINK, disabledforeground=MUTED, bg=WHITE,
                         cursor="hand2", bd=0, padx=0, pady=0)
        enabled = lambda: str(label.cget("state")) != "disabled"
        label.bind("<Button-1>", lambda _: action() if enabled() else None)
        label.bind("<Enter>", lambda _: label.configure(font=self.link_hover_font) if enabled() else None)
        label.bind("<Leave>", lambda _: label.configure(font=self.body_font))
        return label

    @staticmethod
    def _enable_link(label: tk.Label, enabled: bool) -> None:
        label.configure(state="normal" if enabled else "disabled", cursor="hand2" if enabled else "")


class LevelMeter(tk.Canvas):
    """A thin bar showing a device's loudness on a -60..0 dBFS scale."""

    def __init__(self, parent: tk.Widget, width: int, height: int):
        super().__init__(parent, width=width, height=height, bg=WHITE, highlightthickness=0, bd=0)
        self._width, self._height = width, height
        self._position = 0.0
        self.create_rectangle(0, 0, width, height, fill=DIVIDER, width=0)
        self._bar = self.create_rectangle(0, 0, 0, height, fill=GREEN, width=0)

    def set_level(self, amplitude: float) -> None:
        position = 0.0 if amplitude <= 0.001 else min(1.0, max(0.0, (20 * math.log10(amplitude) + 60) / 60))
        if abs(position - self._position) < 0.004:
            return
        self._position = position
        self.coords(self._bar, 0, 0, round(self._width * position), self._height)
        self.itemconfigure(self._bar, fill=RED if position > 0.98 else GREEN)


class ProgressBar(tk.Canvas):
    """A thin bar that fills up; when a step's length is unknown, a short piece slides back and forth."""

    def __init__(self, parent: tk.Widget, width: int, height: int):
        super().__init__(parent, width=width, height=height, bg=WHITE, highlightthickness=0, bd=0)
        self._width, self._height = width, height
        self._sweep = 0.0
        self._track = self.create_rectangle(0, 0, width, height, fill=DIVIDER, width=0, state="hidden")
        self._bar = self.create_rectangle(0, 0, 0, height, fill=LINK, width=0, state="hidden")

    def show(self, fraction: float | None) -> None:
        """Call on every UI tick; `fraction` is from 0 to 1, or None while the length is unknown."""
        if fraction is None:
            self._sweep = (self._sweep + 0.04) % 2
            piece = self._width * 0.25
            left = (self._width - piece) * (self._sweep if self._sweep <= 1 else 2 - self._sweep)
            self.coords(self._bar, left, 0, left + piece, self._height)
        else:
            self.coords(self._bar, 0, 0, round(self._width * min(1.0, max(0.0, fraction))), self._height)
        self.itemconfigure(self._track, state="normal")
        self.itemconfigure(self._bar, state="normal")

    def hide(self) -> None:
        self.itemconfigure(self._track, state="hidden")
        self.itemconfigure(self._bar, state="hidden")


def _make_icon(root: tk.Tk, size: int) -> tk.PhotoImage:
    """Draws the app icon, a red dot on a dark rounded square, with 4x4 supersampling."""
    image = tk.PhotoImage(master=root, width=size, height=size)
    samples, corner, center, radius = 4, size * 0.22, size / 2, size * 0.24
    rows, transparent = [], []
    for y in range(size):
        row = []
        for x in range(size):
            in_square = in_dot = 0
            for sy in range(samples):
                for sx in range(samples):
                    px, py = x + (sx + 0.5) / samples, y + (sy + 0.5) / samples
                    dx, dy = max(corner - px, px - (size - corner), 0), max(corner - py, py - (size - corner), 0)
                    in_square += dx * dx + dy * dy <= corner * corner
                    in_dot += (px - center) ** 2 + (py - center) ** 2 <= radius * radius
            t = in_dot / samples ** 2
            row.append("#%02x%02x%02x" % (round(24 + 215 * t), round(24 + 44 * t), round(27 + 41 * t)))
            if in_square < samples ** 2 / 2:
                transparent.append((x, y))
        rows.append("{" + " ".join(row) + "}")
    image.put(" ".join(rows))
    for x, y in transparent:
        image.transparency_set(x, y, True)
    return image


def _activate_running_instance() -> None:
    for title in (APP_NAME, RECORDING_TITLE):
        window = _user32.FindWindowW(None, title)
        if window:
            _user32.ShowWindow(window, 9)  # SW_RESTORE
            _user32.SetForegroundWindow(window)
            return


def _open_folder(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)
    except OSError as error:
        messagebox.showerror(APP_NAME, str(error))


def _ellipsize(text: str, font: tkfont.Font, width: int) -> str:
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _ellipsize_middle(text: str, font: tkfont.Font, width: int) -> str:
    if font.measure(text) <= width:
        return text
    head, tail = text[: len(text) // 2], text[len(text) // 2:]
    while (head or tail) and font.measure(head + "…" + tail) > width:
        if len(head) >= len(tail):
            head = head[:-1]
        else:
            tail = tail[1:]
    return head + "…" + tail


def _format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def _format_size(size: int) -> str:
    mb, gb = 1024 ** 2, 1024 ** 3
    if size < mb:
        return f"{size // 1024} KB"
    if size < gb:
        return f"{size / mb:.1f} MB" if size < 10 * mb else f"{size / mb:.0f} MB"
    return f"{size / gb:.1f} GB"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"
