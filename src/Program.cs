using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

namespace AudioRecorder
{
    internal static class Program
    {
        [STAThread]
        static void Main()
        {
            bool firstInstance;
            using (new Mutex(true, "AudioRecorder-SingleInstance-5c1f0e2a", out firstInstance))
            {
                if (!firstInstance)
                {
                    ActivateRunningInstance();
                    return;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new MainForm());
            }
        }

        static void ActivateRunningInstance()
        {
            Process current = Process.GetCurrentProcess();
            foreach (Process other in Process.GetProcessesByName(current.ProcessName))
            {
                if (other.Id == current.Id || other.MainWindowHandle == IntPtr.Zero) continue;
                NativeMethods.ShowWindow(other.MainWindowHandle, NativeMethods.SwRestore);
                NativeMethods.SetForegroundWindow(other.MainWindowHandle);
                return;
            }
        }
    }

    /// <summary>The save folder and the devices the user unticked, kept in %APPDATA%\AudioRecorder.</summary>
    internal sealed class Settings
    {
        public string Folder;
        public readonly HashSet<string> ExcludedDevices = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        static string FilePath
        {
            get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "AudioRecorder", "settings.txt"); }
        }

        public static Settings Load()
        {
            var settings = new Settings();
            try
            {
                if (File.Exists(FilePath))
                    foreach (string line in File.ReadAllLines(FilePath))
                    {
                        if (line.StartsWith("folder=", StringComparison.Ordinal)) settings.Folder = line.Substring(7);
                        else if (line.StartsWith("exclude=", StringComparison.Ordinal)) settings.ExcludedDevices.Add(line.Substring(8));
                    }
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
            if (string.IsNullOrWhiteSpace(settings.Folder))
                settings.Folder = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "Audio Recordings");
            return settings;
        }

        public void Save()
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(FilePath));
                var lines = new List<string> { "folder=" + Folder };
                foreach (string id in ExcludedDevices) lines.Add("exclude=" + id);
                File.WriteAllLines(FilePath, lines);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    internal static class NativeMethods
    {
        public const uint EsContinuous = 0x80000000;
        public const uint EsSystemRequired = 0x00000001;
        public const int SwRestore = 9;

        /// <summary>Keeps the PC from going to sleep while a recording is running.</summary>
        [DllImport("kernel32.dll")]
        public static extern uint SetThreadExecutionState(uint flags);

        [DllImport("user32.dll")]
        public static extern bool ShowWindow(IntPtr hwnd, int command);

        [DllImport("user32.dll")]
        public static extern bool SetForegroundWindow(IntPtr hwnd);
    }
}
