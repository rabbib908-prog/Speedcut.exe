$ErrorActionPreference = "Stop"
python -m pip install --upgrade pyinstaller
if (-not (Test-Path ffmpeg.exe)) {
  Invoke-WebRequest "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile ff.zip
  Expand-Archive ff.zip -DestinationPath ff -Force
  foreach ($n in "ffmpeg.exe","ffprobe.exe") { Copy-Item (Get-ChildItem ff -Recurse -Filter $n | Select-Object -First 1).FullName . }
}
python -m PyInstaller --onefile --noconsole --name SpeedCut --add-binary "ffmpeg.exe;." --add-binary "ffprobe.exe;." speedcut.py
Write-Host "DONE: dist\SpeedCut.exe"
