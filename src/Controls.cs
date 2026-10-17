using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

namespace AudioRecorder
{
    /// <summary>The big pill-shaped Record / Stop button.</summary>
    internal sealed class RecordButton : Button
    {
        bool recording, hovered, pressed;

        public RecordButton()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            FlatStyle = FlatStyle.Flat;
            FlatAppearance.BorderSize = 0;
            UseVisualStyleBackColor = false;
            Cursor = Cursors.Hand;
            Font = new Font("Segoe UI Semibold", 11f);
            Text = "Record";
        }

        public bool Recording
        {
            get { return recording; }
            set
            {
                if (recording == value) return;
                recording = value;
                Text = value ? "Stop" : "Record";
                Invalidate();
            }
        }

        protected override void OnMouseEnter(EventArgs e) { hovered = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { hovered = pressed = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnMouseDown(MouseEventArgs e) { pressed = e.Button == MouseButtons.Left; Invalidate(); base.OnMouseDown(e); }
        protected override void OnMouseUp(MouseEventArgs e) { pressed = false; Invalidate(); base.OnMouseUp(e); }
        protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }
        protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
        protected override void OnLostFocus(EventArgs e) { Invalidate(); base.OnLostFocus(e); }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.Clear(Parent != null ? Parent.BackColor : SystemColors.Control);
            g.SmoothingMode = SmoothingMode.AntiAlias;

            Color fill = !Enabled ? Color.FromArgb(228, 228, 231)
                : recording ? Color.FromArgb(39, 39, 42)
                : Color.FromArgb(220, 38, 38);
            if (Enabled && pressed) fill = Draw.Mix(fill, Color.Black, 0.15f);
            else if (Enabled && hovered) fill = Draw.Mix(fill, Color.White, 0.12f);
            Color ink = Enabled ? Color.White : Color.FromArgb(161, 161, 170);

            var bounds = new RectangleF(0.5f, 0.5f, Width - 1.5f, Height - 1.5f);
            using (GraphicsPath pill = Draw.RoundedRect(bounds, bounds.Height / 2f))
            using (var brush = new SolidBrush(fill))
                g.FillPath(brush, pill);

            if (Focused && ShowFocusCues)
            {
                RectangleF ring = RectangleF.Inflate(bounds, -3f, -3f);
                using (GraphicsPath path = Draw.RoundedRect(ring, ring.Height / 2f))
                using (var pen = new Pen(Color.FromArgb(150, Color.White), 1.5f))
                    g.DrawPath(pen, path);
            }

            float glyph = (float)Math.Round(Height * 0.24f);
            float gap = (float)Math.Round(Height * 0.16f);
            Size textSize = TextRenderer.MeasureText(g, Text, Font, Size.Empty, TextFormatFlags.NoPadding);
            float left = (float)Math.Round((Width - (glyph + gap + textSize.Width)) / 2f);
            float top = (float)Math.Round((Height - glyph) / 2f);
            using (var brush = new SolidBrush(ink))
            {
                if (recording)
                    using (GraphicsPath square = Draw.RoundedRect(new RectangleF(left, top, glyph, glyph), glyph * 0.2f))
                        g.FillPath(brush, square);
                else
                    g.FillEllipse(brush, left, top, glyph, glyph);
            }
            var textOrigin = new Point((int)(left + glyph + gap), (Height - textSize.Height) / 2);
            TextRenderer.DrawText(g, Text, Font, textOrigin, ink, fill, TextFormatFlags.NoPadding);
        }
    }

    /// <summary>A thin bar showing a device's current loudness on a -60..0 dBFS scale.</summary>
    internal sealed class LevelMeter : Control
    {
        static readonly Color Track = Color.FromArgb(228, 228, 231);
        static readonly Color Signal = Color.FromArgb(34, 197, 94);
        static readonly Color Clipping = Color.FromArgb(220, 38, 38);

        float position;

        public LevelMeter()
        {
            SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
            TabStop = false;
        }

        /// <summary>Peak amplitude from 0 to 1.</summary>
        public float Level
        {
            set
            {
                float p = ToPosition(value);
                if (Math.Abs(p - position) < 0.004f) return;
                position = p;
                Invalidate();
            }
        }

        static float ToPosition(float amplitude)
        {
            if (amplitude <= 0.001f) return 0f;
            double db = 20 * Math.Log10(amplitude);
            return (float)Math.Max(0, Math.Min(1, (db + 60) / 60));
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.Clear(Parent != null ? Parent.BackColor : BackColor);
            g.SmoothingMode = SmoothingMode.AntiAlias;
            var track = new RectangleF(0.5f, 0.5f, Width - 1f, Height - 1f);
            float radius = track.Height / 2f;
            using (GraphicsPath path = Draw.RoundedRect(track, radius))
            using (var brush = new SolidBrush(Track))
                g.FillPath(brush, path);
            if (position <= 0) return;
            var bar = new RectangleF(track.X, track.Y, Math.Max(track.Height, track.Width * position), track.Height);
            using (GraphicsPath path = Draw.RoundedRect(bar, radius))
            using (var brush = new SolidBrush(position > 0.98f ? Clipping : Signal))
                g.FillPath(brush, path);
        }
    }

    internal static class Draw
    {
        public static GraphicsPath RoundedRect(RectangleF r, float radius)
        {
            var path = new GraphicsPath();
            float d = Math.Min(radius * 2, Math.Min(r.Width, r.Height));
            if (d <= 0)
            {
                path.AddRectangle(r);
                return path;
            }
            path.AddArc(r.X, r.Y, d, d, 180, 90);
            path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }

        public static Color Mix(Color a, Color b, float amount)
        {
            return Color.FromArgb(
                (int)(a.R + (b.R - a.R) * amount),
                (int)(a.G + (b.G - a.G) * amount),
                (int)(a.B + (b.B - a.B) * amount));
        }
    }
}
