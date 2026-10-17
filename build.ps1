# Builds bin\AudioRecorder.exe with the C# compiler that ships with Windows (.NET Framework 4.x).
# Nothing needs to be installed. Run:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function New-AppIcon([string]$path) {
    Add-Type -AssemblyName System.Drawing
    $sizes = 256, 48, 32, 24, 16
    $images = foreach ($size in $sizes) {
        $bmp = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.Clear([System.Drawing.Color]::Transparent)
        # Dark rounded square with a red "record" dot.
        $d = [Math]::Max(2, [int]($size * 0.44))
        $m = $size - 1
        $square = New-Object System.Drawing.Drawing2D.GraphicsPath
        $square.AddArc(0, 0, $d, $d, 180, 90)
        $square.AddArc($m - $d, 0, $d, $d, 270, 90)
        $square.AddArc($m - $d, $m - $d, $d, $d, 0, 90)
        $square.AddArc(0, $m - $d, $d, $d, 90, 90)
        $square.CloseFigure()
        $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(24, 24, 27))), $square)
        $dot = $size * 0.48
        $offset = ($size - $dot) / 2
        $g.FillEllipse((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(239, 68, 68))), $offset, $offset, $dot, $dot)
        $g.Dispose()
        $stream = New-Object System.IO.MemoryStream
        $bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        , $stream.ToArray()
    }

    $file = [System.IO.File]::Create($path)
    $writer = New-Object System.IO.BinaryWriter $file
    $writer.Write([UInt16]0); $writer.Write([UInt16]1); $writer.Write([UInt16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $writer.Write([byte]($sizes[$i] % 256)); $writer.Write([byte]($sizes[$i] % 256))
        $writer.Write([byte]0); $writer.Write([byte]0)
        $writer.Write([UInt16]1); $writer.Write([UInt16]32)
        $writer.Write([UInt32]$images[$i].Length); $writer.Write([UInt32]$offset)
        $offset += $images[$i].Length
    }
    foreach ($image in $images) { $writer.Write($image) }
    $writer.Dispose()
}

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }

$icon = Join-Path $root 'app.ico'
if (-not (Test-Path $icon)) { New-AppIcon $icon }

$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Force $bin | Out-Null
$sources = Get-ChildItem (Join-Path $root 'src') -Filter *.cs | ForEach-Object { $_.FullName }

& $csc /nologo /codepage:65001 /target:winexe /optimize+ `
    "/out:$bin\AudioRecorder.exe" "/win32icon:$icon" "/win32manifest:$root\app.manifest" `
    /r:System.dll /r:System.Core.dll /r:System.Drawing.dll /r:System.Windows.Forms.dll `
    $sources
if ($LASTEXITCODE -ne 0) { throw "Build failed." }
Write-Host "Built $bin\AudioRecorder.exe"
