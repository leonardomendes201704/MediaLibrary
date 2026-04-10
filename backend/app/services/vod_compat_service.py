from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from app.core.config import Settings, get_settings
from app.db.models import Video


class VodCompatService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.RLock()

    def ensure_hls_manifest(self, video: Video) -> Path:
        if not video.derived_path:
            raise ValueError("Video has no derived path")

        source_path = Path(video.derived_path).resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Derived video not found: {source_path}")

        output_dir = self.output_dir(video.id)
        manifest_path = output_dir / "index.m3u8"
        with self._lock:
            if self._is_usable_hls_output(manifest_path, source_path):
                return manifest_path

            output_dir.mkdir(parents=True, exist_ok=True)
            self._clear_output_dir(output_dir)

            result = subprocess.run(
                self._ffmpeg_hls_command(video, source_path, manifest_path),
                text=True,
                capture_output=True,
                check=False,
                creationflags=self._creation_flags(),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "ffmpeg failed to create VOD HLS stream")
            if not manifest_path.exists():
                raise RuntimeError("VOD HLS manifest was not created")
            return manifest_path

    def ensure_compatible_path(self, video: Video) -> Path:
        if not video.derived_path:
            raise ValueError("Video has no derived path")

        source_path = Path(video.derived_path).resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Derived video not found: {source_path}")

        if video.has_audio:
            return source_path

        output_path = self.output_path(video.id)
        with self._lock:
            if self._is_usable_output(output_path, source_path):
                return output_path

            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = output_path.with_suffix(".tmp.mp4")
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

            result = subprocess.run(
                self._ffmpeg_command(source_path, temp_path),
                text=True,
                capture_output=True,
                check=False,
                creationflags=self._creation_flags(),
            )
            if result.returncode != 0:
                temp_path.unlink(missing_ok=True)
                raise RuntimeError(result.stderr.strip() or "ffmpeg failed to create VOD compatibility file")

            temp_path.replace(output_path)
            return output_path

    def output_path(self, video_id: int) -> Path:
        return self.settings.video_vod_dir / f"{video_id}.mp4"

    def output_dir(self, video_id: int) -> Path:
        return self.settings.video_vod_dir / str(video_id)

    def manifest_path(self, video_id: int) -> Path:
        return self.output_dir(video_id) / "index.m3u8"

    def resolve_output_path(self, video_id: int, filename: str) -> Path:
        base_dir = self.output_dir(video_id).resolve()
        target = (base_dir / filename).resolve()
        try:
            target.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError("Invalid VOD asset path") from exc
        return target

    def _is_usable_output(self, output_path: Path, source_path: Path) -> bool:
        return (
            output_path.exists()
            and output_path.is_file()
            and output_path.stat().st_size > 0
            and output_path.stat().st_mtime >= source_path.stat().st_mtime
        )

    def _is_usable_hls_output(self, manifest_path: Path, source_path: Path) -> bool:
        return (
            manifest_path.exists()
            and manifest_path.is_file()
            and manifest_path.stat().st_size > 0
            and manifest_path.stat().st_mtime >= source_path.stat().st_mtime
        )

    def _clear_output_dir(self, output_dir: Path) -> None:
        for child in output_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def _ffmpeg_command(self, source_path: Path, output_path: Path) -> list[str]:
        return [
            self.settings.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            self.settings.transcode_audio_bitrate,
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

    def _ffmpeg_hls_command(self, video: Video, source_path: Path, manifest_path: Path) -> list[str]:
        segment_pattern = str(self.output_dir(video.id) / "seg_%05d.ts")
        video_codec = (video.video_codec or "").strip().lower()
        command = [
            self.settings.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
        ]
        if not video.has_audio:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=48000:cl=stereo",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                ]
            )
        else:
            command.extend(
                [
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                ]
            )

        if video_codec == "h264":
            command.extend(["-c:v", "copy"])
        else:
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    self.settings.channel_hls_preset,
                    "-profile:v",
                    "high",
                    "-level",
                    "4.1",
                    "-pix_fmt",
                    "yuv420p",
                ]
            )

        command.extend(
            [
                "-c:a",
                "aac",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-b:a",
                self.settings.channel_hls_audio_bitrate,
            ]
        )
        if not video.has_audio:
            command.append("-shortest")

        command.extend(
            [
                "-sn",
                "-dn",
                "-f",
                "hls",
                "-hls_time",
                str(max(2, int(self.settings.channel_hls_time_seconds))),
                "-hls_list_size",
                "0",
                "-hls_playlist_type",
                "vod",
                "-hls_allow_cache",
                "1",
                "-hls_segment_type",
                "mpegts",
                "-hls_flags",
                "independent_segments",
                "-hls_segment_filename",
                segment_pattern,
                str(manifest_path),
            ]
        )
        return command

    def _creation_flags(self) -> int:
        if os.name != "nt":
            return 0
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
