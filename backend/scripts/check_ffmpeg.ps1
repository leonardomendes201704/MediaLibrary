Write-Output "FFmpeg version:"
$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobeCmd = Get-Command ffprobe -ErrorAction SilentlyContinue

if (-not $ffmpegCmd -or -not $ffprobeCmd) {
    Write-Error "FFmpeg nao encontrado. Instale ffmpeg e ffprobe e garanta que estejam no PATH, ou configure FFMPEG_BIN/FFPROBE_BIN."
    exit 1
}

Write-Output "FFmpeg version:"
ffmpeg -version | Select-Object -First 1
Write-Output "FFprobe version:"
ffprobe -version | Select-Object -First 1
