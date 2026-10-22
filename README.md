# Audio Recorder

A tiny Windows app that records every microphone **and** everything you hear on every speaker or headset, at the same time. When you press Stop, it merges everything into **one MP3** and writes a **transcript** of every voice, labelled by who was speaking.

Written in Python. Recording uses only the standard library (`tkinter` for the window, `ctypes` for Windows audio); the MP3 and transcript use [faster-whisper](https://github.com/SYSTRAN/faster-whisper), which runs fully offline.

## Set up

Needs Windows 10 or 11, Python 3.12 or newer from python.org, and Git for Windows (it includes Git LFS, which brings the 1.6 GB speech model).

```
git lfs install
git clone https://github.com/YoanLeblanc/audio-recorder.git
cd audio-recorder
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Already cloned without Git LFS? The transcript then fails with "speech model missing". Fix it in the app folder with `git lfs install` then `git lfs pull`.

## Run

```
.venv\Scripts\pythonw "Audio Recorder.pyw"
```

Tick the devices you want (all by default), press **Record**, press **Stop**. Each recording ends up in its own folder under `Documents\Audio Recordings` (changeable in the app):

```
2026-10-18 14-30-05\
  2026-10-18 14-30-05.mp3    <- every device mixed into one file (128 kbps)
  2026-10-18 14-30-05.txt    <- the transcript
```

The transcript lists who said what, in order:

```
[00:00:00] Microphone: Bonjour à tous, merci d'être présents pour cette réunion.
[00:00:11] Casque pour téléphone: Thanks for having me. Revenue grew by 12% compared to last quarter.
```

- **Speakers** are told apart by device: your microphone is you, each output (headset, speakers) is the other people. With speakers instead of headphones, your microphone also hears the others, so their words can appear twice.
- **Languages**: each device is transcribed in your Windows language or English, whichever it sounds like.
- **Speed**: on the CPU, about 10x faster than real time per device that has speech (a 1-hour call takes about 12 minutes). A progress bar with the time left shows under the timer. It runs in a separate low-priority process, so you can start a new recording meanwhile.
- **Speech model**: Whisper large-v3-turbo ships in this repo, in `models/whisper-large-v3-turbo` (1.6 GB, stored with [Git LFS](https://git-lfs.com), so clone with Git LFS installed). If it is missing, the app downloads it there once.
- **WAV files**: recordings are made as WAV (one per device plus a mix) and deleted once the MP3 and transcript exist. To keep them, set `"delete_wav": false` in `%APPDATA%\AudioRecorder\settings.json`. If you quit before the transcript is done, it is finished the next time the app starts.
- The PC is kept awake while recording. Devices plugged in during a recording are picked up on the next one (or press **Refresh**).

Without the packages from `requirements.txt`, the app still records, and keeps the WAV files.

To process a folder by hand: `.venv\Scripts\python -m audiorecorder.postprocess "<folder>" [--keep-wav]`

## Code

| File | What it does |
| --- | --- |
| `audiorecorder/wasapi.py` | Windows Core Audio (WASAPI) bindings via `ctypes`: list devices, capture, loopback |
| `audiorecorder/recording.py` | One capture thread per device, WAV writer, mixer |
| `audiorecorder/postprocess.py` | MP3 encoding and speech-to-text after each recording |
| `audiorecorder/ui.py` | The `tkinter` window |
| `audiorecorder/settings.py` | Save folder, unticked devices, whether to keep WAV files |
| `models/whisper-large-v3-turbo/` | The offline speech-to-text model (see `SOURCE.txt`) |
