using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace AudioRecorder
{
    /// <summary>An active audio endpoint: a microphone or line-in (input), or speakers/headphones (output).</summary>
    internal sealed class AudioDevice
    {
        public string Id;
        public string Name;
        public bool IsOutput;
        public bool IsDefault;
    }

    /// <summary>Minimal Windows Core Audio (WASAPI) interop.</summary>
    internal static class Wasapi
    {
        public const int ShareModeShared = 0;
        public const uint StreamFlagsLoopback = 0x00020000;
        public const uint StreamFlagsSrcDefaultQuality = 0x08000000;
        public const uint StreamFlagsAutoConvertPcm = 0x80000000;
        public const uint BufferFlagsSilent = 0x2;

        const uint DeviceStateActive = 0x1;
        const uint ClsCtxAll = 0x17;
        const ushort VtLpwstr = 31;

        static readonly Guid IID_IAudioClient = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
        public static readonly Guid IID_IAudioCaptureClient = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
        static readonly Guid FriendlyNameFormatId = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0");

        public static IMMDeviceEnumerator CreateEnumerator()
        {
            return (IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();
        }

        /// <summary>Lists active inputs then outputs, each with the default device first.</summary>
        public static List<AudioDevice> ListDevices()
        {
            var result = new List<AudioDevice>();
            IMMDeviceEnumerator enumerator = CreateEnumerator();
            try
            {
                foreach (EDataFlow flow in new[] { EDataFlow.Capture, EDataFlow.Render })
                {
                    string defaultId = GetDefaultDeviceId(enumerator, flow);
                    IMMDeviceCollection collection;
                    enumerator.EnumAudioEndpoints(flow, DeviceStateActive, out collection);
                    var group = new List<AudioDevice>();
                    try
                    {
                        uint count;
                        collection.GetCount(out count);
                        for (uint i = 0; i < count; i++)
                        {
                            IMMDevice device;
                            collection.Item(i, out device);
                            try
                            {
                                var info = new AudioDevice();
                                device.GetId(out info.Id);
                                info.Name = GetFriendlyName(device) ?? "Unknown device";
                                info.IsOutput = flow == EDataFlow.Render;
                                info.IsDefault = info.Id == defaultId;
                                group.Add(info);
                            }
                            finally { Release(device); }
                        }
                    }
                    finally { Release(collection); }

                    group.Sort((a, b) => a.IsDefault != b.IsDefault
                        ? (a.IsDefault ? -1 : 1)
                        : string.Compare(a.Name, b.Name, StringComparison.CurrentCultureIgnoreCase));
                    result.AddRange(group);
                }
            }
            finally { Release(enumerator); }
            return result;
        }

        public static IAudioClient Activate(IMMDevice device)
        {
            Guid iid = IID_IAudioClient;
            object client;
            device.Activate(ref iid, ClsCtxAll, IntPtr.Zero, out client);
            return (IAudioClient)client;
        }

        public static object GetService(IAudioClient client, Guid iid)
        {
            object service;
            client.GetService(ref iid, out service);
            return service;
        }

        /// <summary>Allocates a WAVEFORMATEX for integer PCM; free it with Marshal.FreeCoTaskMem.</summary>
        public static IntPtr AllocPcmFormat(int sampleRate, int channels, int bitsPerSample)
        {
            short blockAlign = (short)(channels * bitsPerSample / 8);
            IntPtr format = Marshal.AllocCoTaskMem(18);
            Marshal.WriteInt16(format, 0, 1); // WAVE_FORMAT_PCM
            Marshal.WriteInt16(format, 2, (short)channels);
            Marshal.WriteInt32(format, 4, sampleRate);
            Marshal.WriteInt32(format, 8, sampleRate * blockAlign);
            Marshal.WriteInt16(format, 12, blockAlign);
            Marshal.WriteInt16(format, 14, (short)bitsPerSample);
            Marshal.WriteInt16(format, 16, 0);
            return format;
        }

        public static void Release(object comObject)
        {
            if (comObject != null && Marshal.IsComObject(comObject)) Marshal.ReleaseComObject(comObject);
        }

        /// <summary>Turns common Core Audio failures into short, readable messages.</summary>
        public static string Describe(Exception ex)
        {
            switch ((uint)ex.HResult)
            {
                case 0x88890004: return "disconnected";
                case 0x8889000A: return "in use by another app (exclusive mode)";
                case 0x88890008: return "audio format not supported";
                case 0x88890010: return "Windows Audio service is not running";
                case 0x80070005: return "access denied (check Windows microphone privacy settings)";
                case 0x80070490: return "device not found";
                default: return ex.Message;
            }
        }

        static string GetDefaultDeviceId(IMMDeviceEnumerator enumerator, EDataFlow flow)
        {
            IMMDevice device;
            try { enumerator.GetDefaultAudioEndpoint(flow, ERole.Console, out device); }
            catch (COMException) { return null; } // no device of this kind
            try
            {
                string id;
                device.GetId(out id);
                return id;
            }
            finally { Release(device); }
        }

        static string GetFriendlyName(IMMDevice device)
        {
            IPropertyStore store;
            device.OpenPropertyStore(0 /* STGM_READ */, out store);
            try
            {
                var key = new PropertyKey { FormatId = FriendlyNameFormatId, PropertyId = 14 };
                PropVariant value;
                store.GetValue(ref key, out value);
                try { return value.Type == VtLpwstr ? Marshal.PtrToStringUni(value.Pointer) : null; }
                finally { PropVariantClear(ref value); }
            }
            finally { Release(store); }
        }

        [DllImport("ole32.dll")]
        static extern int PropVariantClear(ref PropVariant value);
    }

    internal enum EDataFlow { Render = 0, Capture = 1 }

    internal enum ERole { Console = 0, Multimedia = 1, Communications = 2 }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PropertyKey
    {
        public Guid FormatId;
        public int PropertyId;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PropVariant
    {
        public ushort Type;
        public ushort Reserved1, Reserved2, Reserved3;
        public IntPtr Pointer;
        public IntPtr Pointer2;
    }

    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    internal class MMDeviceEnumeratorComObject { }

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDeviceEnumerator
    {
        void EnumAudioEndpoints(EDataFlow dataFlow, uint stateMask, out IMMDeviceCollection devices);
        void GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice device);
        void GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice device);
        void RegisterEndpointNotificationCallback(IntPtr client);
        void UnregisterEndpointNotificationCallback(IntPtr client);
    }

    [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDeviceCollection
    {
        void GetCount(out uint count);
        void Item(uint index, out IMMDevice device);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IMMDevice
    {
        void Activate(ref Guid iid, uint clsCtx, IntPtr activationParams, [MarshalAs(UnmanagedType.IUnknown)] out object instance);
        void OpenPropertyStore(uint access, out IPropertyStore store);
        void GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
        void GetState(out uint state);
    }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IPropertyStore
    {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAudioClient
    {
        void Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, IntPtr format, IntPtr sessionGuid);
        void GetBufferSize(out uint frames);
        void GetStreamLatency(out long latency);
        void GetCurrentPadding(out uint frames);
        [PreserveSig] int IsFormatSupported(int shareMode, IntPtr format, out IntPtr closestMatch);
        void GetMixFormat(out IntPtr format);
        void GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
        void Start();
        void Stop();
        void Reset();
        void SetEventHandle(IntPtr eventHandle);
        void GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }

    [ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    internal interface IAudioCaptureClient
    {
        void GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
        void ReleaseBuffer(uint frames);
        void GetNextPacketSize(out uint frames);
    }
}
