import json
import re
import subprocess
import tempfile
from pathlib import Path

from .adb_client import AdbClient


POWERSHELL_OCR = r"""param([string]$ImagePath)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 })[0]
function AwaitOp($op, [Type]$type) {
  $asTask = $asTaskGeneric.MakeGenericMethod($type)
  $task = $asTask.Invoke($null, @($op))
  $task.Wait()
  return $task.Result
}
$file = AwaitOp ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = AwaitOp ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = AwaitOp ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = AwaitOp ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$lang = [Windows.Globalization.Language]::new('zh-Hans')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { throw 'Windows OCR engine is unavailable.' }
$result = AwaitOp ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $result.Lines) {
  $left = 999999
  $top = 999999
  $right = 0
  $bottom = 0
  foreach ($word in $line.Words) {
    $rect = $word.BoundingRect
    if ($rect.X -lt $left) { $left = $rect.X }
    if ($rect.Y -lt $top) { $top = $rect.Y }
    if (($rect.X + $rect.Width) -gt $right) { $right = $rect.X + $rect.Width }
    if (($rect.Y + $rect.Height) -gt $bottom) { $bottom = $rect.Y + $rect.Height }
  }
  $lines += [pscustomobject]@{
    text = $line.Text
    left = [int]$left
    top = [int]$top
    right = [int]$right
    bottom = [int]$bottom
    centerX = [int](($left + $right) / 2)
    centerY = [int](($top + $bottom) / 2)
  }
}
[pscustomobject]@{ text = $result.Text; lines = $lines } | ConvertTo-Json -Depth 4 -Compress
"""


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def clean_ocr_text(text: str) -> str:
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"[ \t]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ocr_image(path: str | Path) -> dict:
    image_path = str(Path(path).resolve())
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as file:
        file.write(POWERSHELL_OCR)
        script_path = file.name
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path, "-ImagePath", image_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    if result.returncode != 0:
        return {"ok": False, "text": "", "lines": [], "error": (result.stderr or result.stdout).strip()}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "text": "", "lines": [], "error": f"ocr_json_parse_failed:{exc}", "raw": result.stdout}
    text = clean_ocr_text(payload.get("text", ""))
    lines = payload.get("lines", [])
    for line in lines:
        line["compactText"] = compact_text(line.get("text", ""))
    return {"ok": True, "text": text, "lines": lines, "error": ""}


def ocr_screenshot(adb: AdbClient, output_dir: str | Path, label: str) -> dict:
    path = Path(output_dir, f"{label}.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not adb.screenshot(path):
        return {"ok": False, "text": "", "lines": [], "screenshot": str(path), "error": "screenshot_failed"}
    result = ocr_image(path)
    result["screenshot"] = str(path)
    return result
