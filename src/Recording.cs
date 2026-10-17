using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace AudioRecorder
{
    /// <summary>Every track is recorded in this format; Windows converts each device's native format to it.</summary>
    internal static class Pcm
    {
        public const int SampleRate = 48000;
        public const int Channels = 2;
        public const int BitsPerSample = 16;
        public const int BlockAlign = Channels * BitsPerSample / 8;
        public const int BytesPerSecond = SampleRate * BlockAlign;
    }

    /// <summary>One recording: a timestamped folder with a WAV file per device, plus a mix of all of them.</summary>
    internal sealed class RecordingSession
    {
        public readonly string Folder;
        public readonly List<DeviceRecorder> Recorders = new List<DeviceRecorder>();
        readonly ManualResetEvent startSignal = new ManualResetEvent(false);
        Mixer mixer;

        RecordingSession(string folder)
        {
            Folder = folder;
        }

        public long BytesWritten
        {
            get
            {
                long total = mixer != null ? mixer.BytesWritten : 0;
                foreach (DeviceRecorder recorder in Recorders) total += recorder.BytesWritten;
                return total;
            }
        }

        public static RecordingSession Start(string rootFolder, IList<AudioDevice> devices)
        {
            var session = new RecordingSession(CreateSessionFolder(rootFolder));
            var queues = new List<SampleQueue>();
            var usedNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (AudioDevice device in devices)
            {
                SampleQueue queue = devices.Count > 1 ? new SampleQueue() : null;
                if (queue != null) queues.Add(queue);
                string name = UniqueName((device.IsOutput ? "Output - " : "Input - ") + SafeFileName(device.Name), usedNames);
                session.Recorders.Add(new DeviceRecorder(device, Path.Combine(session.Folder, name + ".wav"), session.startSignal, queue));
            }
            if (queues.Count > 1) session.mixer = new Mixer(Path.Combine(session.Folder, "All devices mixed.wav"), queues);

            foreach (DeviceRecorder recorder in session.Recorders) recorder.Start();
            // Let every device finish opening, then start them all at once so the tracks line up.
            int deadline = Environment.TickCount + 3000;
            foreach (DeviceRecorder recorder in session.Recorders) recorder.WaitUntilReady(Math.Max(0, deadline - Environment.TickCount));
            session.startSignal.Set();
            if (session.mixer != null) session.mixer.Start();
            return session;
        }

        public void Stop()
        {
            foreach (DeviceRecorder recorder in Recorders) recorder.RequestStop();
            startSignal.Set();
            foreach (DeviceRecorder recorder in Recorders) recorder.Join();
            if (mixer != null) mixer.StopAndJoin();
            try
            {
                if (Directory.GetFileSystemEntries(Folder).Length == 0) Directory.Delete(Folder);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }

        static string CreateSessionFolder(string rootFolder)
        {
            string baseName = Path.Combine(rootFolder, DateTime.Now.ToString("yyyy-MM-dd HH-mm-ss", CultureInfo.InvariantCulture));
            string folder = baseName;
            for (int i = 2; Directory.Exists(folder); i++) folder = baseName + " (" + i + ")";
            Directory.CreateDirectory(folder);
            return folder;
        }

        static string SafeFileName(string name)
        {
            char[] invalid = Path.GetInvalidFileNameChars();
            var safe = new StringBuilder(name.Length);
            foreach (char c in name) safe.Append(Array.IndexOf(invalid, c) >= 0 ? '_' : c);
            return safe.ToString().Trim().TrimEnd('.');
        }

        static string UniqueName(string name, HashSet<string> used)
        {
            string candidate = name;
            for (int i = 2; !used.Add(candidate); i++) candidate = name + " (" + i + ")";
            return candidate;
        }
    }

    /// <summary>Records one device to a WAV file on its own thread. Outputs are captured in loopback mode.</summary>
    internal sealed class DeviceRecorder
    {
        const long HnsPerSecond = 10000000;          // Core Audio uses 100 ns units
        const long CaptureBufferHns = 2 * HnsPerSecond;
        const long IdleAfterHns = HnsPerSecond / 5;   // no loopback data for 200 ms: nothing is playing
        const long IdleLagHns = HnsPerSecond / 10;    // ...so fill silence up to 100 ms ago
        const long ResumeAfterHns = HnsPerSecond / 10;
        const long MinGapFrames = Pcm.SampleRate / 100;

        public readonly AudioDevice Device;
        public readonly string FilePath;
        readonly SampleQueue mixQueue;
        readonly ManualResetEvent startSignal;
        readonly ManualResetEvent ready = new ManualResetEvent(false);
        readonly byte[] silence = new byte[Pcm.BytesPerSecond / 10];
        byte[] buffer = new byte[Pcm.BytesPerSecond];
        Thread thread;
        WavWriter wav;
        long startHns, lastDataHns, framesWritten, bytesWritten;
        int peak;
        volatile bool stopRequested;
        volatile string error;

        public DeviceRecorder(AudioDevice device, string filePath, ManualResetEvent startSignal, SampleQueue mixQueue)
        {
            Device = device;
            FilePath = filePath;
            this.startSignal = startSignal;
            this.mixQueue = mixQueue;
        }

        /// <summary>Set when the device failed or was disconnected; recording of this device has ended.</summary>
        public string Error { get { return error; } }

        public long BytesWritten { get { return Interlocked.Read(ref bytesWritten); } }

        /// <summary>Loudest sample since the last call, from 0 to 1.</summary>
        public float TakePeak()
        {
            return Interlocked.Exchange(ref peak, 0) / 32768f;
        }

        public void Start()
        {
            thread = new Thread(Run) { IsBackground = true, Name = "Recorder: " + Device.Name };
            thread.SetApartmentState(ApartmentState.MTA);
            thread.Start();
        }

        public void WaitUntilReady(int timeoutMs) { ready.WaitOne(timeoutMs); }

        public void RequestStop() { stopRequested = true; }

        public void Join() { if (thread != null) thread.Join(); }

        void Run()
        {
            IMMDeviceEnumerator enumerator = null;
            IMMDevice device = null;
            IAudioClient client = null;
            IAudioCaptureClient capture = null;
            try
            {
                enumerator = Wasapi.CreateEnumerator();
                enumerator.GetDevice(Device.Id, out device);
                client = Wasapi.Activate(device);
                uint flags = Wasapi.StreamFlagsAutoConvertPcm | Wasapi.StreamFlagsSrcDefaultQuality;
                if (Device.IsOutput) flags |= Wasapi.StreamFlagsLoopback;
                IntPtr format = Wasapi.AllocPcmFormat(Pcm.SampleRate, Pcm.Channels, Pcm.BitsPerSample);
                try { client.Initialize(Wasapi.ShareModeShared, flags, CaptureBufferHns, 0, format, IntPtr.Zero); }
                finally { Marshal.FreeCoTaskMem(format); }
                capture = (IAudioCaptureClient)Wasapi.GetService(client, Wasapi.IID_IAudioCaptureClient);
                wav = new WavWriter(FilePath);

                ready.Set();
                startSignal.WaitOne();
                if (stopRequested) return;
                client.Start();
                startHns = lastDataHns = NowHns();

                while (!stopRequested)
                {
                    Drain(capture);
                    if (Device.IsOutput && NowHns() - lastDataHns > IdleAfterHns) PadUntil(NowHns() - IdleLagHns);
                    Thread.Sleep(10);
                }
                Drain(capture);
                if (Device.IsOutput && NowHns() - lastDataHns > ResumeAfterHns) PadUntil(NowHns());
                client.Stop();
            }
            catch (Exception ex)
            {
                error = Wasapi.Describe(ex);
            }
            finally
            {
                ready.Set();
                if (wav != null)
                {
                    try { wav.Dispose(); }
                    catch (Exception ex) { if (error == null) error = ex.Message; }
                }
                if (mixQueue != null) mixQueue.Close();
                Wasapi.Release(capture);
                Wasapi.Release(client);
                Wasapi.Release(device);
                Wasapi.Release(enumerator);
            }
        }

        void Drain(IAudioCaptureClient capture)
        {
            uint frames;
            capture.GetNextPacketSize(out frames);
            while (frames != 0)
            {
                IntPtr data;
                uint flags;
                ulong devicePosition, qpcPosition;
                capture.GetBuffer(out data, out frames, out flags, out devicePosition, out qpcPosition);
                int count = (int)frames * Pcm.BlockAlign;
                if (buffer.Length < count) buffer = new byte[count];
                bool silent = (flags & Wasapi.BufferFlagsSilent) != 0;
                if (silent) Array.Clear(buffer, 0, count);
                else Marshal.Copy(data, buffer, 0, count);
                capture.ReleaseBuffer(frames);

                if (Device.IsOutput)
                {
                    // Loopback delivers nothing while the device is quiet. When sound resumes,
                    // fill the gap with silence so the track stays in step with the clock.
                    long now = NowHns();
                    if (now - lastDataHns > ResumeAfterHns)
                    {
                        long packetStart = (long)qpcPosition;
                        if (Math.Abs(now - packetStart) > HnsPerSecond) packetStart = now - frames * HnsPerSecond / Pcm.SampleRate;
                        PadUntil(packetStart);
                    }
                    lastDataHns = now;
                }

                Write(buffer, count);
                if (!silent) UpdatePeak(buffer, count);
                capture.GetNextPacketSize(out frames);
            }
        }

        void PadUntil(long hns)
        {
            long expectedHns = startHns + framesWritten * HnsPerSecond / Pcm.SampleRate;
            long missingFrames = (hns - expectedHns) * Pcm.SampleRate / HnsPerSecond;
            while (missingFrames >= MinGapFrames)
            {
                int frames = (int)Math.Min(missingFrames, silence.Length / Pcm.BlockAlign);
                Write(silence, frames * Pcm.BlockAlign);
                missingFrames -= frames;
            }
        }

        void Write(byte[] data, int count)
        {
            wav.Write(data, 0, count);
            if (mixQueue != null) mixQueue.Write(data, count);
            framesWritten += count / Pcm.BlockAlign;
            Interlocked.Add(ref bytesWritten, count);
        }

        void UpdatePeak(byte[] data, int count)
        {
            int max = 0;
            for (int i = 0; i + 1 < count; i += 2)
            {
                int sample = (short)(data[i] | (data[i + 1] << 8));
                if (sample < 0) sample = -sample;
                if (sample > max) max = sample;
            }
            int current;
            do
            {
                current = peak;
                if (max <= current) return;
            } while (Interlocked.CompareExchange(ref peak, max, current) != current);
        }

        static long NowHns()
        {
            long ticks = Stopwatch.GetTimestamp(), frequency = Stopwatch.Frequency;
            return ticks / frequency * HnsPerSecond + ticks % frequency * HnsPerSecond / frequency;
        }
    }

    /// <summary>Sums every device's samples into one extra track.</summary>
    internal sealed class Mixer
    {
        const int MaxLagSamples = Pcm.SampleRate * Pcm.Channels * 2; // a stalled device delays the mix by at most 2 s

        readonly List<SampleQueue> inputs;
        readonly WavWriter wav;
        Thread thread;
        int[] sums = new int[0];
        byte[] output = new byte[0];
        volatile bool stopRequested;

        public Mixer(string path, List<SampleQueue> inputs)
        {
            this.inputs = inputs;
            wav = new WavWriter(path);
        }

        public long BytesWritten { get { return wav.TotalBytes; } }

        public void Start()
        {
            thread = new Thread(Run) { IsBackground = true, Name = "Mixer" };
            thread.Start();
        }

        public void StopAndJoin()
        {
            stopRequested = true;
            if (thread != null) thread.Join();
            else wav.Dispose();
        }

        void Run()
        {
            try
            {
                while (!stopRequested)
                {
                    Thread.Sleep(100);
                    MixAvailable(false);
                }
                MixAvailable(true);
            }
            catch (Exception) { } // e.g. disk full: the per-device tracks report their own errors
            finally
            {
                try { wav.Dispose(); }
                catch (Exception) { }
            }
        }

        void MixAvailable(bool flush)
        {
            // Mix as far as every still-running device has delivered.
            int ready = int.MaxValue, buffered = 0;
            foreach (SampleQueue queue in inputs)
            {
                bool closed = queue.IsClosed;
                int count = queue.Count;
                buffered = Math.Max(buffered, count);
                if (!closed) ready = Math.Min(ready, count);
            }
            int n = flush || ready == int.MaxValue ? buffered : Math.Max(ready, buffered - MaxLagSamples);
            if (n <= 0) return;

            if (sums.Length < n)
            {
                sums = new int[n];
                output = new byte[n * 2];
            }
            else Array.Clear(sums, 0, n);

            foreach (SampleQueue queue in inputs) queue.MixInto(sums, n);
            for (int i = 0; i < n; i++)
            {
                int v = sums[i];
                if (v > short.MaxValue) v = short.MaxValue;
                else if (v < short.MinValue) v = short.MinValue;
                output[2 * i] = (byte)v;
                output[2 * i + 1] = (byte)(v >> 8);
            }
            wav.Write(output, 0, n * 2);
        }
    }

    /// <summary>Thread-safe FIFO of 16-bit samples feeding the mixer.</summary>
    internal sealed class SampleQueue
    {
        readonly object sync = new object();
        short[] samples = new short[Pcm.SampleRate * Pcm.Channels];
        int head, count;
        volatile bool closed;

        public bool IsClosed { get { return closed; } }

        public int Count { get { lock (sync) return count; } }

        public void Close() { closed = true; }

        public void Write(byte[] data, int byteCount)
        {
            int n = byteCount / 2;
            lock (sync)
            {
                if (count + n > samples.Length) Grow(count + n);
                int tail = (head + count) % samples.Length;
                int first = Math.Min(n, samples.Length - tail);
                Buffer.BlockCopy(data, 0, samples, tail * 2, first * 2);
                Buffer.BlockCopy(data, first * 2, samples, 0, (n - first) * 2);
                count += n;
            }
        }

        /// <summary>Removes up to <paramref name="n"/> samples, adding them into <paramref name="sums"/>.</summary>
        public void MixInto(int[] sums, int n)
        {
            lock (sync)
            {
                int take = Math.Min(n, count);
                for (int i = 0; i < take; i++) sums[i] += samples[(head + i) % samples.Length];
                head = (head + take) % samples.Length;
                count -= take;
            }
        }

        void Grow(int needed)
        {
            var larger = new short[Math.Max(needed, samples.Length * 2)];
            int first = Math.Min(count, samples.Length - head);
            Array.Copy(samples, head, larger, 0, first);
            Array.Copy(samples, 0, larger, first, count - first);
            samples = larger;
            head = 0;
        }
    }

    /// <summary>Writes 16-bit PCM WAV files, continuing in a new part file before the 4 GB format limit.</summary>
    internal sealed class WavWriter : IDisposable
    {
        const long MaxDataBytes = 4000000000L;
        const long HeaderRefreshBytes = Pcm.BytesPerSecond * 5L;

        readonly string firstPath;
        readonly List<string> paths = new List<string>();
        FileStream stream;
        long dataBytes, bytesSinceHeader, totalBytes;

        public WavWriter(string path)
        {
            firstPath = path;
            Open(path);
        }

        public long TotalBytes { get { return Interlocked.Read(ref totalBytes); } }

        public void Write(byte[] data, int offset, int count)
        {
            while (count > 0)
            {
                if (dataBytes == MaxDataBytes) StartNextPart();
                int n = (int)Math.Min(count, MaxDataBytes - dataBytes);
                stream.Write(data, offset, n);
                dataBytes += n;
                bytesSinceHeader += n;
                Interlocked.Add(ref totalBytes, n);
                offset += n;
                count -= n;
            }
            // Keep the header current so a crash or power cut still leaves a playable file.
            if (bytesSinceHeader >= HeaderRefreshBytes) UpdateHeader();
        }

        public void Dispose()
        {
            if (stream == null) return;
            UpdateHeader();
            stream.Dispose();
            stream = null;
            if (totalBytes == 0)
                foreach (string path in paths)
                {
                    try { File.Delete(path); }
                    catch (IOException) { }
                    catch (UnauthorizedAccessException) { }
                }
        }

        void StartNextPart()
        {
            UpdateHeader();
            stream.Dispose();
            string name = Path.GetFileNameWithoutExtension(firstPath) + " (part " + (paths.Count + 1) + ").wav";
            Open(Path.Combine(Path.GetDirectoryName(firstPath), name));
        }

        void Open(string path)
        {
            stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 1 << 16);
            paths.Add(path);
            dataBytes = 0;
            var header = new byte[44];
            WriteAscii(header, 0, "RIFF");
            WriteUInt32(header, 4, 36);
            WriteAscii(header, 8, "WAVE");
            WriteAscii(header, 12, "fmt ");
            WriteUInt32(header, 16, 16);
            WriteUInt16(header, 20, 1); // PCM
            WriteUInt16(header, 22, Pcm.Channels);
            WriteUInt32(header, 24, Pcm.SampleRate);
            WriteUInt32(header, 28, Pcm.BytesPerSecond);
            WriteUInt16(header, 32, Pcm.BlockAlign);
            WriteUInt16(header, 34, Pcm.BitsPerSample);
            WriteAscii(header, 36, "data");
            WriteUInt32(header, 40, 0);
            stream.Write(header, 0, header.Length);
        }

        void UpdateHeader()
        {
            long position = stream.Position;
            var field = new byte[4];
            WriteUInt32(field, 0, (uint)(36 + dataBytes));
            stream.Seek(4, SeekOrigin.Begin);
            stream.Write(field, 0, 4);
            WriteUInt32(field, 0, (uint)dataBytes);
            stream.Seek(40, SeekOrigin.Begin);
            stream.Write(field, 0, 4);
            stream.Seek(position, SeekOrigin.Begin);
            stream.Flush();
            bytesSinceHeader = 0;
        }

        static void WriteAscii(byte[] target, int offset, string text)
        {
            for (int i = 0; i < text.Length; i++) target[offset + i] = (byte)text[i];
        }

        static void WriteUInt16(byte[] target, int offset, int value)
        {
            target[offset] = (byte)value;
            target[offset + 1] = (byte)(value >> 8);
        }

        static void WriteUInt32(byte[] target, int offset, uint value)
        {
            target[offset] = (byte)value;
            target[offset + 1] = (byte)(value >> 8);
            target[offset + 2] = (byte)(value >> 16);
            target[offset + 3] = (byte)(value >> 24);
        }
    }
}
