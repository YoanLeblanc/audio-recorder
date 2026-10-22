"""The app's settings, kept in %APPDATA%\\AudioRecorder\\settings.json."""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import byref, c_void_p
from dataclasses import dataclass, field
from pathlib import Path

from .wasapi import guid

_SETTINGS_FILE = Path(os.environ.get("APPDATA", Path.home())) / "AudioRecorder" / "settings.json"


@dataclass
class Settings:
    folder: Path                                              # where recordings are saved
    excluded_devices: set[str] = field(default_factory=set)  # devices the user unticked
    delete_wav: bool = True  # remove the WAV files once the MP3 and transcript are made

    @classmethod
    def load(cls) -> Settings:
        settings = cls(_documents_folder() / "Audio Recordings")
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            if data.get("folder"):
                settings.folder = Path(data["folder"])
            settings.excluded_devices = set(data.get("excluded_devices", []))
            settings.delete_wav = bool(data.get("delete_wav", True))
        except (OSError, ValueError, AttributeError):
            pass
        return settings

    def save(self) -> None:
        data = {"folder": str(self.folder), "excluded_devices": sorted(self.excluded_devices), "delete_wav": self.delete_wav}
        try:
            _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass


def _documents_folder() -> Path:
    """The real Documents folder, which may have been moved (for example into OneDrive)."""
    shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
    path = c_void_p()
    folder_id = guid("FDD39AD0-238F-46AF-ADB4-6C85480369C7")  # FOLDERID_Documents
    if shell32.SHGetKnownFolderPath(byref(folder_id), 0, None, byref(path)) == 0:
        try:
            return Path(ctypes.wstring_at(path.value))
        finally:
            ole32.CoTaskMemFree(path)
    return Path.home() / "Documents"
