import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import AppConfig


logger = logging.getLogger("ffmpeg")

TRANSCODE_QUALITY_PERCENT_KEY = "transcode_quality_percent"
TRANSCODE_QUALITY_MIN_PERCENT = 0
TRANSCODE_QUALITY_MAX_PERCENT = 100
TRANSCODE_QUALITY_MIN_CRF = 18
TRANSCODE_QUALITY_MAX_CRF = 32


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def quality_percent_to_crf(percent: int, base_crf: int) -> int:
    clamped = _clamp_int(percent, TRANSCODE_QUALITY_MIN_PERCENT, TRANSCODE_QUALITY_MAX_PERCENT)
    normalized_base_crf = _clamp_int(base_crf, TRANSCODE_QUALITY_MIN_CRF, TRANSCODE_QUALITY_MAX_CRF)
    span = TRANSCODE_QUALITY_MAX_CRF - normalized_base_crf
    if span <= 0:
        return normalized_base_crf
    ratio = (100 - clamped) / 100
    return _clamp_int(
        int(round(normalized_base_crf + (ratio * span))),
        TRANSCODE_QUALITY_MIN_CRF,
        TRANSCODE_QUALITY_MAX_CRF,
    )


@dataclass
class ProbeResult:
    container: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    has_audio: bool
    audio_codec: str | None
    video_profile: str | None
    video_pix_fmt: str | None
    video_level: int | None
    bitrate: int | None


@dataclass
class ProcessingPlan:
    mode: str
    reason: str


class FFmpegService:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory

    def missing_binaries(self) -> list[str]:
        missing: list[str] = []
        for env_name, binary in (("FFMPEG_BIN", self.settings.ffmpeg_bin), ("FFPROBE_BIN", self.settings.ffprobe_bin)):
            candidate = Path(binary).expanduser()
            if candidate.exists():
                continue
            if shutil.which(binary):
                continue
            missing.append(f"{env_name}={binary}")
        return missing

    def ensure_available(self) -> None:
        missing = self.missing_binaries()
        if not missing:
            return
        raise RuntimeError(
            "Dependencias externas ausentes: "
            + ", ".join(missing)
            + ". Instale o FFmpeg e garanta que ffmpeg/ffprobe estejam no PATH "
            + "ou configure os caminhos absolutos em FFMPEG_BIN e FFPROBE_BIN."
        )

    def _run_cmd(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        logger.debug("Running subprocess: %s", " ".join(cmd))
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            return subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                check=False,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            binary = cmd[0] if cmd else "comando"
            raise RuntimeError(
                f"Dependencia externa nao encontrada: '{binary}'. "
                "Instale o FFmpeg e garanta que ffmpeg/ffprobe estejam no PATH, "
                "ou configure FFMPEG_BIN e FFPROBE_BIN com caminhos absolutos."
            ) from exc

    def probe(self, input_path: str) -> ProbeResult:
        cmd = [
            self.settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration,bit_rate,format_name",
            "-show_streams",
            "-of",
            "json",
            input_path,
        ]
        result = self._run_cmd(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")

        payload = json.loads(result.stdout or "{}")
        format_obj = payload.get("format", {})
        streams = payload.get("streams", [])

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

        duration = format_obj.get("duration")
        bitrate = format_obj.get("bit_rate")

        return ProbeResult(
            container=(format_obj.get("format_name") or "").split(",")[0] or None,
            duration_seconds=float(duration) if duration not in (None, "N/A", "") else None,
            width=int(video_stream.get("width")) if video_stream.get("width") else None,
            height=int(video_stream.get("height")) if video_stream.get("height") else None,
            video_codec=video_stream.get("codec_name"),
            has_audio=bool(audio_stream),
            audio_codec=audio_stream.get("codec_name"),
            video_profile=video_stream.get("profile"),
            video_pix_fmt=video_stream.get("pix_fmt"),
            video_level=int(video_stream.get("level")) if video_stream.get("level") else None,
            bitrate=int(bitrate) if bitrate not in (None, "N/A", "") else None,
        )

    def build_plan(self, probe: ProbeResult) -> ProcessingPlan:
        compatible_video = (probe.video_codec or "").lower() == "h264"
        compatible_audio = not probe.has_audio or (probe.audio_codec or "").lower() == "aac"
        profile = (probe.video_profile or "").lower()
        pix_fmt = (probe.video_pix_fmt or "").lower()
        compatible_profile = "10" not in profile
        compatible_pix_fmt = pix_fmt in {"yuv420p", "yuvj420p"} or not pix_fmt
        compatible_level = probe.video_level is None or probe.video_level <= 42
        within_resolution = (
            (probe.width or 0) <= self.settings.transcode_max_width
            and (probe.height or 0) <= self.settings.transcode_max_height
        )
        within_bitrate = (
            (probe.bitrate or 0) <= self.settings.transcode_compatible_bitrate_limit if probe.bitrate else True
        )

        if compatible_video and compatible_profile and compatible_pix_fmt and compatible_level and within_resolution and within_bitrate:
            if compatible_audio:
                return ProcessingPlan(mode="remux_copy", reason="video_audio_compatible_remux")
            return ProcessingPlan(mode="remux_video_copy_audio_aac", reason="video_compatible_audio_convert_only")
        return ProcessingPlan(mode="transcode", reason="normalize_for_fire_android")

    def resolve_transcode_quality_percent(self) -> int:
        fallback = 100
        if self._session_factory is None:
            return fallback
        try:
            with self._session_factory() as session:
                value = session.scalar(
                    select(AppConfig.value).where(AppConfig.key == TRANSCODE_QUALITY_PERCENT_KEY)
                )
        except Exception:
            logger.exception("Failed to load transcode quality config; using fallback CRF=%s", self.settings.transcode_crf)
            return fallback

        if value is None:
            return fallback

        try:
            return _clamp_int(int(round(float(value))), TRANSCODE_QUALITY_MIN_PERCENT, TRANSCODE_QUALITY_MAX_PERCENT)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid transcode quality config value=%r; using fallback CRF=%s",
                value,
                self.settings.transcode_crf,
            )
            return fallback

    def resolve_transcode_crf(self) -> int:
        return quality_percent_to_crf(
            self.resolve_transcode_quality_percent(),
            self.settings.transcode_crf,
        )

    def optimize_for_streaming(self, source_path: str, output_path: str, plan: ProcessingPlan) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        if plan.mode == "remux_copy":
            cmd = [
                self.settings.ffmpeg_bin,
                "-y",
                "-i",
                source_path,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                output_path,
            ]
        elif plan.mode == "remux_video_copy_audio_aac":
            cmd = [
                self.settings.ffmpeg_bin,
                "-y",
                "-i",
                source_path,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                self.settings.transcode_audio_bitrate,
                "-movflags",
                "+faststart",
                output_path,
            ]
        else:
            scale = (
                f"scale='min(iw,{self.settings.transcode_max_width})':'min(ih,{self.settings.transcode_max_height})':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2"
            )
            resolved_crf = self.resolve_transcode_crf()
            cmd = [
                self.settings.ffmpeg_bin,
                "-y",
                "-i",
                source_path,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-threads",
                str(self.settings.transcode_threads),
                "-preset",
                self.settings.transcode_preset,
                "-crf",
                str(resolved_crf),
                "-maxrate",
                self.settings.transcode_video_maxrate,
                "-bufsize",
                self.settings.transcode_video_bufsize,
                "-profile:v",
                "high",
                "-level",
                "4.1",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "60",
                "-keyint_min",
                "60",
                "-sc_threshold",
                "0",
                "-vf",
                scale,
                "-c:a",
                "aac",
                "-b:a",
                self.settings.transcode_audio_bitrate,
                "-movflags",
                "+faststart",
                output_path,
            ]
        result = self._run_cmd(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg optimize failed: {result.stderr.strip()}")

    def generate_thumbnail(self, source_path: str, output_path: str) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.settings.ffmpeg_bin,
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            source_path,
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-1",
            "-q:v",
            "3",
            output_path,
        ]
        result = self._run_cmd(cmd)
        if result.returncode != 0:
            fallback_cmd = [
                self.settings.ffmpeg_bin,
                "-y",
                "-i",
                source_path,
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-1",
                "-q:v",
                "3",
                output_path,
            ]
            fallback = self._run_cmd(fallback_cmd)
            if fallback.returncode != 0:
                raise RuntimeError(f"thumbnail generation failed: {fallback.stderr.strip()}")

    def extract_first_frame(self, source_path: str, output_path: str) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.settings.ffmpeg_bin,
            "-y",
            "-i",
            source_path,
            "-frames:v",
            "1",
            "-vf",
            "scale='min(720,iw)':-2",
            "-q:v",
            "3",
            output_path,
        ]
        result = self._run_cmd(cmd)
        if result.returncode != 0:
            raise RuntimeError(f"first frame extraction failed: {result.stderr.strip()}")

    def create_image_slideshow(
        self,
        image_paths: list[Path],
        output_path: str,
        interval_seconds: float = 0.5,
        output_fps: int = 30,
    ) -> None:
        if not image_paths:
            raise ValueError("image_paths vazio")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds deve ser > 0")
        if output_fps <= 0:
            raise ValueError("output_fps deve ser > 0")

        self.ensure_available()
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        # Integer frames per slide keeps timing exact (e.g. 15 frames @ 30fps = 500ms).
        frames_per_slide = max(1, int(round(output_fps * interval_seconds)))
        input_fps = output_fps / frames_per_slide

        work_dir = Path(tempfile.mkdtemp(prefix="slideshow_frames_"))
        try:
            for index, source in enumerate(image_paths, start=1):
                dest = work_dir / f"{index:06d}.jpg"
                suffix = source.suffix.lower()
                if suffix in {".jpg", ".jpeg"}:
                    try:
                        os.link(source, dest)
                    except OSError:
                        shutil.copy2(source, dest)
                else:
                    convert = self._run_cmd(
                        [
                            self.settings.ffmpeg_bin,
                            "-y",
                            "-i",
                            str(source),
                            "-frames:v",
                            "1",
                            "-q:v",
                            "2",
                            str(dest),
                        ]
                    )
                    if convert.returncode != 0 or not dest.exists():
                        raise RuntimeError(
                            f"Falha ao preparar frame do slideshow: {source.name}: {convert.stderr.strip()}"
                        )

            scale = (
                "scale='min(iw,1280)':'min(ih,720)':force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,"
                "setsar=1"
            )
            resolved_crf = self.resolve_transcode_crf()
            cmd = [
                self.settings.ffmpeg_bin,
                "-y",
                "-framerate",
                f"{input_fps:.6f}".rstrip("0").rstrip("."),
                "-i",
                str(work_dir / "%06d.jpg"),
                "-an",
                "-vf",
                scale,
                "-r",
                str(output_fps),
                "-fps_mode",
                "cfr",
                "-c:v",
                "libx264",
                "-threads",
                str(self.settings.transcode_threads),
                "-preset",
                self.settings.transcode_preset,
                "-crf",
                str(resolved_crf),
                "-pix_fmt",
                "yuv420p",
                "-g",
                str(frames_per_slide),
                "-keyint_min",
                str(frames_per_slide),
                "-sc_threshold",
                "0",
                "-movflags",
                "+faststart",
                str(output),
            ]
            result = self._run_cmd(cmd)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg slideshow failed: {result.stderr.strip()}")
            if not output.exists() or output.stat().st_size <= 0:
                raise RuntimeError("ffmpeg slideshow nao gerou arquivo de saida")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
