from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings, get_settings
from app.db.models import Video


logger = logging.getLogger("channel-runtime")


@dataclass(slots=True)
class ChannelRuntimeState:
    category: str
    channel_key: str
    playlist_hash: str
    process: subprocess.Popen[str] | None = None
    started_at: float = 0.0
    last_access_at: float = 0.0


class ChannelRuntimeService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._states: dict[str, ChannelRuntimeState] = {}
        self._lock = threading.RLock()

    def _creation_flags(self) -> int:
        if os.name != "nt":
            return 0
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def channel_dir(self, channel_key: str) -> Path:
        return self.settings.channel_hls_dir / channel_key

    def manifest_path(self, channel_key: str) -> Path:
        return self.channel_dir(channel_key) / "index.m3u8"

    def log_path(self, channel_key: str) -> Path:
        return self.channel_dir(channel_key) / "ffmpeg.log"

    def resolve_output_path(self, channel_key: str, filename: str) -> Path:
        base_dir = self.channel_dir(channel_key).resolve()
        target = (base_dir / filename).resolve()
        try:
            target.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError("Invalid channel asset path") from exc
        return target

    def ensure_channel(self, channel_key: str, category: str, videos: list[Video]) -> Path:
        if not videos:
            raise ValueError("Channel has no videos")

        with self._lock:
            self._prune_stale_channels_locked()
            manifest_path = self.manifest_path(channel_key)
            playlist_hash = self._playlist_hash(videos)
            state = self._states.get(channel_key)
            now = time.time()

            if (
                state
                and state.playlist_hash == playlist_hash
                and state.process is not None
                and state.process.poll() is None
                and manifest_path.exists()
            ):
                state.last_access_at = now
                return manifest_path

            if state:
                self._stop_state_locked(state, remove_dir=False)

            channel_dir = self.channel_dir(channel_key)
            channel_dir.mkdir(parents=True, exist_ok=True)
            self._clear_channel_dir(channel_dir)

            concat_list_path = channel_dir / "inputs.txt"
            segment_pattern = str(channel_dir / "seg_%05d.ts")
            log_path = self.log_path(channel_key)
            self._write_concat_list(concat_list_path, videos)

            stderr_handle = log_path.open("w", encoding="utf-8")
            try:
                process = subprocess.Popen(
                    self._ffmpeg_command(concat_list_path, manifest_path, segment_pattern),
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_handle,
                    creationflags=self._creation_flags(),
                )
            finally:
                stderr_handle.close()
            state = ChannelRuntimeState(
                category=category,
                channel_key=channel_key,
                playlist_hash=playlist_hash,
                process=process,
                started_at=now,
                last_access_at=now,
            )
            self._states[channel_key] = state
            logger.info("Started live channel %s for category=%s pid=%s", channel_key, category, process.pid)

            self._wait_for_manifest(manifest_path, process, log_path)
            return manifest_path

    def stop_all(self) -> None:
        with self._lock:
            states = list(self._states.values())
            for state in states:
                self._stop_state_locked(state, remove_dir=False)
            self._states.clear()

    def _playlist_hash(self, videos: list[Video]) -> str:
        payload = "|".join(f"{video.id}:{video.derived_path or ''}:{video.updated_at.isoformat()}" for video in videos)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _clear_channel_dir(self, channel_dir: Path) -> None:
        for child in channel_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    logger.debug("Failed to remove stale channel artifact: %s", child, exc_info=True)

    def _write_concat_list(self, path: Path, videos: list[Video]) -> None:
        lines: list[str] = []
        for video in videos:
            source = Path(str(video.derived_path or "")).resolve()
            normalized = source.as_posix().replace("'", "'\\''")
            lines.append(f"file '{normalized}'")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ffmpeg_command(self, concat_list_path: Path, manifest_path: Path, segment_pattern: str) -> list[str]:
        hls_time = max(2, int(self.settings.channel_hls_time_seconds))
        frame_rate = max(24, int(self.settings.channel_hls_frame_rate))
        gop = hls_time * frame_rate
        scale = (
            f"scale={self.settings.channel_hls_width}:{self.settings.channel_hls_height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={self.settings.channel_hls_width}:{self.settings.channel_hls_height}:(ow-iw)/2:(oh-ih)/2:black"
        )
        return [
            self.settings.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-y",
            "-re",
            "-stream_loop",
            "-1",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list_path),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            scale,
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
            "-r",
            str(frame_rate),
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{hls_time})",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-b:a",
            self.settings.channel_hls_audio_bitrate,
            "-sn",
            "-dn",
            "-f",
            "hls",
            "-hls_time",
            str(hls_time),
            "-hls_list_size",
            str(self.settings.channel_hls_list_size),
            "-hls_allow_cache",
            "0",
            "-hls_segment_type",
            "mpegts",
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+program_date_time+independent_segments",
            "-hls_segment_filename",
            segment_pattern,
            str(manifest_path),
        ]

    def _wait_for_manifest(self, manifest_path: Path, process: subprocess.Popen[str], log_path: Path) -> None:
        deadline = time.time() + max(3, int(self.settings.channel_hls_startup_timeout_seconds))
        while time.time() < deadline:
            if manifest_path.exists() and manifest_path.stat().st_size > 0:
                return
            if process.poll() is not None:
                stderr = ""
                try:
                    stderr = log_path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    stderr = ""
                raise RuntimeError(stderr or "ffmpeg channel process exited before manifest was ready")
            time.sleep(0.25)
        raise RuntimeError("Timed out waiting for live channel manifest")

    def _prune_stale_channels_locked(self) -> None:
        now = time.time()
        stale_keys = [
            key
            for key, state in self._states.items()
            if (now - state.last_access_at) > max(30, int(self.settings.channel_hls_idle_seconds))
        ]
        for key in stale_keys:
            state = self._states.pop(key, None)
            if state:
                self._stop_state_locked(state, remove_dir=False)

    def _stop_state_locked(self, state: ChannelRuntimeState, remove_dir: bool) -> None:
        process = state.process
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            except OSError:
                logger.debug("Failed to terminate channel process pid=%s", process.pid, exc_info=True)
        if remove_dir:
            shutil.rmtree(self.channel_dir(state.channel_key), ignore_errors=True)
        logger.info("Stopped live channel %s for category=%s", state.channel_key, state.category)
