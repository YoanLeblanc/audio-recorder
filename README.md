# Audio Recorder

A tiny Windows app that records every microphone **and** everything you hear on every speaker/headset at the same time.

Run `bin\AudioRecorder.exe` (no installation; needs Windows 10 or 11). Tick the devices you want (all by default), press **Record**, press **Stop**.

Each recording goes into its own timestamped folder under `Documents\Audio Recordings` (changeable in the app):

```
2026-10-17 14-30-05\
  Input - Microphone (USB PnP Audio Device).wav
  Output - Headphones (...).wav
  All devices mixed.wav        <- everything summed into one track
```

- Format: WAV, 48 kHz, 16-bit stereo, about 690 MB per hour per file. Files over 4 GB continue in a "(part 2)" file.
- Outputs are recorded in loopback mode (exactly what the device plays). Silent stretches are kept, so all tracks stay in sync.
- The PC is kept awake while recording. Files are playable even if the app or PC crashes mid-recording.
- Devices plugged in during a recording are picked up on the next one (or press **Refresh**).

## Build

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

Uses the C# compiler that ships with Windows (.NET Framework 4.x), so nothing needs to be installed.
