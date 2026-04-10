Write-Output "FFmpeg version:"
ffmpeg -version | Select-Object -First 1
Write-Output "FFprobe version:"
ffprobe -version | Select-Object -First 1

