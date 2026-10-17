using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Windows.Forms;

namespace AudioRecorder
{
    internal sealed class MainForm : Form
    {
        public const string AppName = "Audio Recorder";

        static readonly Color Ink = Color.FromArgb(24, 24, 27);
        static readonly Color Muted = Color.FromArgb(113, 113, 122);
        static readonly Color Divider = Color.FromArgb(228, 228, 231);
        static readonly Color Danger = Color.FromArgb(220, 38, 38);
        static readonly Color Link = Color.FromArgb(37, 99, 235);

        sealed class DeviceRow
        {
            public AudioDevice Device;
            public CheckBox Check;
            public LevelMeter Meter;
            public DeviceRecorder Recorder;
            public float Level;
            public bool ShowsError;
        }

        readonly Settings settings = Settings.Load();
        readonly float scale;
        readonly RecordButton recordButton = new RecordButton();
        readonly Label timeLabel = new Label();
        readonly LinkLabel statusLabel = new LinkLabel();
        readonly Label inputsHeader = new Label();
        readonly Label outputsHeader = new Label();
        readonly Label noInputsLabel = new Label();
        readonly Label noOutputsLabel = new Label();
        readonly LinkLabel refreshLink = new LinkLabel();
        readonly Label folderCaption = new Label();
        readonly LinkLabel folderLink = new LinkLabel();
        readonly LinkLabel changeFolderLink = new LinkLabel();
        readonly ToolTip toolTip = new ToolTip();
        readonly Timer timer = new Timer();
        readonly Stopwatch clock = new Stopwatch();
        readonly List<DeviceRow> rows = new List<DeviceRow>();
        readonly List<int> dividers = new List<int>();
        RecordingSession session;
        string savedFolder, savedSummary;
        int ticks;

        public MainForm()
        {
            Text = AppName;
            Font = new Font("Segoe UI", 9f);
            BackColor = Color.White;
            ForeColor = Ink;
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.None;
            DoubleBuffered = true;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
            catch (Exception) { }
            using (Graphics g = CreateGraphics()) scale = g.DpiX / 96f;

            recordButton.TabIndex = 0;
            recordButton.Click += delegate { ToggleRecording(); };

            timeLabel.AutoSize = true;
            timeLabel.Font = new Font("Segoe UI Semibold", 22f);
            timeLabel.ForeColor = Muted;
            timeLabel.Text = FormatElapsed(TimeSpan.Zero);

            statusLabel.AutoSize = false;
            statusLabel.AutoEllipsis = true;
            statusLabel.ForeColor = Muted;
            StyleLink(statusLabel);
            statusLabel.LinkClicked += delegate { OpenFolder(savedFolder); };

            StyleHeader(inputsHeader, "Microphones & inputs");
            StyleHeader(outputsHeader, "Speakers & outputs");
            StyleNote(noInputsLabel, "No input devices found");
            StyleNote(noOutputsLabel, "No output devices found");

            refreshLink.Text = "Refresh";
            refreshLink.AutoSize = true;
            StyleLink(refreshLink);
            refreshLink.LinkClicked += delegate { RefreshDevices(); };

            folderCaption.Text = "Save to";
            folderCaption.AutoSize = true;
            folderCaption.ForeColor = Muted;
            folderLink.AutoSize = false;
            folderLink.AutoEllipsis = true;
            StyleLink(folderLink);
            folderLink.LinkClicked += delegate { OpenFolder(settings.Folder); };
            changeFolderLink.Text = "Change…";
            changeFolderLink.AutoSize = true;
            StyleLink(changeFolderLink);
            changeFolderLink.LinkClicked += delegate { ChooseFolder(); };

            Controls.AddRange(new Control[]
            {
                recordButton, timeLabel, statusLabel, inputsHeader, refreshLink, noInputsLabel,
                outputsHeader, noOutputsLabel, folderCaption, folderLink, changeFolderLink
            });

            timer.Interval = 50;
            timer.Tick += delegate { OnTimerTick(); };
            timer.Start();

            RefreshDevices();
            ActiveControl = recordButton;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            base.OnPaint(e);
            using (var pen = new Pen(Divider))
                foreach (int y in dividers) e.Graphics.DrawLine(pen, S(24), y, ClientSize.Width - S(24), y);
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (session != null && e.CloseReason == CloseReason.UserClosing)
            {
                DialogResult answer = MessageBox.Show(this, "A recording is in progress. Stop recording and quit?",
                    AppName, MessageBoxButtons.YesNo, MessageBoxIcon.Question);
                if (answer != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
            }
            StopRecording();
            base.OnFormClosing(e);
        }

        void ToggleRecording()
        {
            if (session == null) StartRecording();
            else StopRecording();
        }

        void StartRecording()
        {
            List<DeviceRow> selected = rows.Where(r => r.Check.Checked).ToList();
            if (selected.Count == 0) return;
            foreach (DeviceRow row in rows) ResetRow(row);

            Cursor = Cursors.WaitCursor;
            try
            {
                session = RecordingSession.Start(settings.Folder, selected.Select(r => r.Device).ToList());
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "Could not start recording.\n\n" + ex.Message, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            finally { Cursor = Cursors.Default; }

            for (int i = 0; i < selected.Count; i++) selected[i].Recorder = session.Recorders[i];
            savedFolder = savedSummary = null;
            clock.Restart();
            NativeMethods.SetThreadExecutionState(NativeMethods.EsContinuous | NativeMethods.EsSystemRequired);
            UpdateState();
            OnTimerTick();
        }

        void StopRecording()
        {
            if (session == null) return;
            Cursor = Cursors.WaitCursor;
            try { session.Stop(); }
            finally { Cursor = Cursors.Default; }
            clock.Stop();
            NativeMethods.SetThreadExecutionState(NativeMethods.EsContinuous);

            foreach (DeviceRow row in rows)
            {
                if (row.Recorder != null && row.Recorder.Error != null && !row.ShowsError) ShowRowError(row, row.Recorder.Error);
                row.Recorder = null;
                row.Level = 0;
                row.Meter.Level = 0;
            }
            string[] files = Directory.Exists(session.Folder) ? Directory.GetFiles(session.Folder, "*.wav") : new string[0];
            if (files.Length > 0)
            {
                long size = files.Sum(f => new FileInfo(f).Length);
                savedFolder = session.Folder;
                savedSummary = "Saved " + Plural(files.Length, "file") + " · " + FormatSize(size);
            }
            else
            {
                savedFolder = null;
                savedSummary = "Nothing was recorded";
            }
            session = null;
            UpdateState();
        }

        void OnTimerTick()
        {
            if (session == null) return;
            timeLabel.Text = FormatElapsed(clock.Elapsed);
            foreach (DeviceRow row in rows)
            {
                if (row.Recorder == null) continue;
                row.Level = Math.Max(row.Recorder.TakePeak(), row.Level * 0.8f);
                row.Meter.Level = row.Level;
                if (row.Recorder.Error != null && !row.ShowsError) ShowRowError(row, row.Recorder.Error);
            }
            if (session.Recorders.All(r => r.Error != null))
            {
                StopRecording();
                return;
            }
            if (++ticks % 10 == 0) UpdateStatus();
        }

        void RefreshDevices()
        {
            if (session != null) return;
            List<AudioDevice> devices;
            string problem = null;
            try { devices = Wasapi.ListDevices(); }
            catch (Exception ex)
            {
                devices = new List<AudioDevice>();
                problem = "Could not list audio devices: " + Wasapi.Describe(ex);
            }

            SuspendLayout();
            foreach (DeviceRow row in rows)
            {
                Controls.Remove(row.Check);
                Controls.Remove(row.Meter);
                row.Check.Dispose();
                row.Meter.Dispose();
            }
            rows.Clear();
            foreach (AudioDevice device in devices)
            {
                var row = new DeviceRow { Device = device, Check = new CheckBox(), Meter = new LevelMeter() };
                row.Check.AutoSize = false;
                row.Check.AutoEllipsis = true;
                row.Check.UseMnemonic = false;
                row.Check.Checked = !settings.ExcludedDevices.Contains(device.Id);
                row.Check.CheckedChanged += delegate { OnDeviceToggled(row); };
                ResetRow(row);
                rows.Add(row);
                Controls.Add(row.Check);
                Controls.Add(row.Meter);
            }
            ArrangeControls();
            ResumeLayout();
            savedFolder = savedSummary = null;
            UpdateState();
            if (problem != null) SetStatus(problem, null);
        }

        void OnDeviceToggled(DeviceRow row)
        {
            if (row.Check.Checked) settings.ExcludedDevices.Remove(row.Device.Id);
            else settings.ExcludedDevices.Add(row.Device.Id);
            settings.Save();
            UpdateState();
        }

        void ArrangeControls()
        {
            int width = S(460), margin = S(24), right = width - margin;

            recordButton.Bounds = new Rectangle(margin, margin, S(128), S(48));
            int textLeft = recordButton.Right + S(20);
            timeLabel.Location = new Point(textLeft - S(3), recordButton.Top - S(5));
            statusLabel.Bounds = new Rectangle(textLeft, timeLabel.Bottom - S(7), right - textLeft, S(18));
            statusLabel.BringToFront();

            dividers.Clear();
            int y = recordButton.Bottom + S(22);
            dividers.Add(y);
            y = ArrangeSection(inputsHeader, noInputsLabel, false, y + S(16), margin, right);
            refreshLink.Location = new Point(right - refreshLink.Width, inputsHeader.Top);
            y = ArrangeSection(outputsHeader, noOutputsLabel, true, y + S(12), margin, right);
            y += S(12);
            dividers.Add(y);

            y += S(14);
            folderCaption.Location = new Point(margin, y);
            changeFolderLink.Location = new Point(right - changeFolderLink.Width, y);
            int folderLeft = folderCaption.Right + S(2);
            folderLink.Bounds = new Rectangle(folderLeft, y, changeFolderLink.Left - S(12) - folderLeft, folderCaption.Height);
            folderLink.Text = settings.Folder;

            ClientSize = new Size(width, y + folderCaption.Height + margin - S(4));
            Invalidate();
        }

        int ArrangeSection(Label header, Label emptyNote, bool outputs, int y, int left, int right)
        {
            int rowHeight = S(26), meterWidth = S(84);
            header.Location = new Point(left, y);
            y = header.Bottom + S(4);
            bool any = false;
            foreach (DeviceRow row in rows)
            {
                if (row.Device.IsOutput != outputs) continue;
                any = true;
                row.Check.Bounds = new Rectangle(left, y, right - left - meterWidth - S(16), rowHeight);
                row.Meter.Bounds = new Rectangle(right - meterWidth, y + (rowHeight - S(6)) / 2, meterWidth, S(6));
                y += rowHeight;
            }
            emptyNote.Visible = !any;
            if (!any)
            {
                emptyNote.Location = new Point(left, y + S(2));
                y = emptyNote.Bottom + S(2);
            }
            return y;
        }

        void UpdateState()
        {
            bool recording = session != null;
            recordButton.Recording = recording;
            recordButton.Enabled = recording || rows.Any(r => r.Check.Checked);
            // AutoCheck rather than Enabled, so device names stay readable (and errors red) while recording.
            foreach (DeviceRow row in rows) row.Check.AutoCheck = !recording;
            refreshLink.Enabled = !recording;
            changeFolderLink.Enabled = !recording;
            timeLabel.ForeColor = recording ? Danger : Muted;
            Text = recording ? "● Recording — " + AppName : AppName;
            UpdateStatus();
        }

        void UpdateStatus()
        {
            if (session != null)
            {
                int failed = session.Recorders.Count(r => r.Error != null);
                string text = "Recording " + Plural(session.Recorders.Count - failed, "device") + " · " + FormatSize(session.BytesWritten);
                if (failed > 0) text += " · " + failed + " stopped";
                SetStatus(text, null);
            }
            else if (savedSummary != null)
            {
                SetStatus(savedSummary, savedFolder != null ? "Open folder" : null);
            }
            else
            {
                int selected = rows.Count(r => r.Check.Checked);
                SetStatus(selected == 0 ? "Select a device to record" : "Ready to record " + Plural(selected, "device"), null);
            }
        }

        void SetStatus(string text, string linkText)
        {
            string full = linkText == null ? text : text + "   " + linkText;
            if (statusLabel.Text == full) return;
            statusLabel.Text = full;
            statusLabel.LinkArea = linkText == null ? new LinkArea(0, 0) : new LinkArea(full.Length - linkText.Length, linkText.Length);
        }

        void ResetRow(DeviceRow row)
        {
            row.ShowsError = false;
            row.Check.ForeColor = Ink;
            row.Check.Text = row.Device.IsDefault ? row.Device.Name + "  (default)" : row.Device.Name;
            toolTip.SetToolTip(row.Check, row.Device.Name);
            row.Level = 0;
            row.Meter.Level = 0;
        }

        void ShowRowError(DeviceRow row, string message)
        {
            row.ShowsError = true;
            row.Check.ForeColor = Danger;
            row.Check.Text = row.Device.Name + " — " + message;
            toolTip.SetToolTip(row.Check, row.Device.Name + "\n" + message);
        }

        void ChooseFolder()
        {
            using (var dialog = new FolderBrowserDialog())
            {
                dialog.Description = "Choose where recordings are saved.";
                dialog.SelectedPath = settings.Folder;
                dialog.ShowNewFolderButton = true;
                if (dialog.ShowDialog(this) != DialogResult.OK) return;
                settings.Folder = dialog.SelectedPath;
                settings.Save();
                folderLink.Text = settings.Folder;
            }
        }

        void OpenFolder(string path)
        {
            if (string.IsNullOrEmpty(path)) return;
            try
            {
                Directory.CreateDirectory(path);
                Process.Start("explorer.exe", "\"" + path + "\"");
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, ex.Message, AppName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }

        int S(int pixels)
        {
            return (int)Math.Round(pixels * scale);
        }

        static void StyleLink(LinkLabel link)
        {
            link.UseMnemonic = false;
            link.LinkColor = Link;
            link.VisitedLinkColor = Link;
            link.ActiveLinkColor = Color.FromArgb(29, 78, 216);
            link.DisabledLinkColor = Muted;
            link.LinkBehavior = LinkBehavior.HoverUnderline;
        }

        static void StyleHeader(Label label, string text)
        {
            label.AutoSize = true;
            label.UseMnemonic = false;
            label.Font = new Font("Segoe UI Semibold", 9f);
            label.Text = text;
        }

        static void StyleNote(Label label, string text)
        {
            label.AutoSize = true;
            label.ForeColor = Muted;
            label.Text = text;
        }

        static string FormatElapsed(TimeSpan t)
        {
            return ((int)t.TotalHours).ToString("00") + ":" + t.Minutes.ToString("00") + ":" + t.Seconds.ToString("00");
        }

        static string FormatSize(long bytes)
        {
            const double MB = 1024 * 1024, GB = MB * 1024;
            if (bytes < MB) return (bytes / 1024) + " KB";
            if (bytes < GB) return (bytes / MB).ToString(bytes < 10 * MB ? "0.0" : "0") + " MB";
            return (bytes / GB).ToString("0.0") + " GB";
        }

        static string Plural(int count, string word)
        {
            return count + " " + word + (count == 1 ? "" : "s");
        }
    }
}
