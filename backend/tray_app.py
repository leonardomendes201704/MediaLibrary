import argparse
import logging
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem


LOGGER_NAME = "tray"
TRAY_ICON_FILENAME = "media_library_tray.ico"


def resolve_backend_workdir() -> Path:
    env_dir = os.getenv("MEDIA_LIBRARY_BACKEND_DIR", "").strip()
    if env_dir:
        path = Path(env_dir).expanduser().resolve()
        if path.exists():
            return path

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [exe_dir / "backend", exe_dir.parent / "backend", exe_dir]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return exe_dir

    return Path(__file__).resolve().parent


def resolve_log_file() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        log_dir = Path(local_app_data) / "MediaLibrary"
    elif getattr(sys, "frozen", False):
        log_dir = Path(sys.executable).resolve().parent
    else:
        log_dir = Path(__file__).resolve().parent
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "backend_tray.log"


def setup_tray_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if not getattr(sys, "frozen", False):
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    logger.propagate = False
    return logger


def notify_error(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def ensure_console_streams() -> None:
    # In --windowed executables, stdout/stderr may be None.
    # Uvicorn's default formatter calls isatty() and crashes if the stream is missing.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", buffering=1)


def create_tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (15, 32, 56, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, 58, 58), radius=12, fill=(18, 49, 86, 255))
    draw.polygon([(18, 19), (46, 32), (18, 45)], fill=(238, 247, 255, 255))
    draw.rectangle((14, 14, 50, 50), outline=(94, 161, 255, 255), width=2)
    return image


def resolve_tray_icon_path() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent / TRAY_ICON_FILENAME,
        Path(getattr(sys, "_MEIPASS", "")) / TRAY_ICON_FILENAME,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


class BackendTrayApp:
    def __init__(self, host: str, port: int, open_browser_on_start: bool, log_file: Path) -> None:
        self.host = host
        self.port = port
        self.open_browser_on_start = open_browser_on_start
        self.admin_url = f"http://127.0.0.1:{self.port}/admin"
        self.docs_url = f"http://127.0.0.1:{self.port}/docs"
        self.health_url = f"http://127.0.0.1:{self.port}/api/health"
        self.log_file = log_file
        self.logger = logging.getLogger(LOGGER_NAME)
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._startup_error: str | None = None
        self._tray_icon_image = self._load_tray_icon_image()

        self.icon = Icon(
            name="MediaLibraryBackend",
            title="Media Library Backend",
            icon=self._tray_icon_image,
            menu=Menu(
                MenuItem("Abrir Admin", self._open_admin),
                MenuItem("Abrir API Docs", self._open_docs),
                MenuItem("Abrir Logs", self._open_logs),
                Menu.SEPARATOR,
                MenuItem("Sair", self._exit),
            ),
        )

    def run(self) -> None:
        self._start_backend()
        if self.open_browser_on_start:
            threading.Thread(target=self._open_admin_with_delay, daemon=True).start()
        self.icon.run(setup=self._setup_tray_icon)

    def _load_tray_icon_image(self) -> Image.Image:
        icon_path = resolve_tray_icon_path()
        if icon_path is not None:
            try:
                return Image.open(icon_path).convert("RGBA")
            except Exception:  # noqa: BLE001
                self.logger.warning("Failed to load tray icon from %s; using generated fallback", icon_path)
        return create_tray_image()

    @staticmethod
    def _setup_tray_icon(icon: Icon) -> None:
        icon.visible = True

    def _start_backend(self) -> None:
        if self._server_thread and self._server_thread.is_alive():
            return
        self._startup_error = None
        self._server_thread = threading.Thread(target=self._run_backend, name="backend-server", daemon=True)
        self._server_thread.start()
        self.logger.info("Backend thread started (host=%s port=%s)", self.host, self.port)

    def _run_backend(self) -> None:
        try:
            ensure_console_streams()
            from app.main import create_app

            app = create_app()
            config = uvicorn.Config(
                app=app,
                host=self.host,
                port=self.port,
                reload=False,
                log_level="info",
                access_log=False,
                use_colors=False,
                log_config=None,
            )
            self._server = uvicorn.Server(config)
            self._server.run()
        except Exception as exc:  # noqa: BLE001
            self._startup_error = str(exc)
            self.logger.error("Backend crashed: %s", exc)
            self.logger.error("Traceback:\n%s", traceback.format_exc())

    def _open_admin_with_delay(self) -> None:
        if self._wait_until_ready(timeout_seconds=40):
            self._open_admin(None, None)
            return
        message = (
            "Backend nao ficou pronto a tempo.\n\n"
            f"URL esperada: {self.admin_url}\n"
            f"Log: {self.log_file}\n"
        )
        if self._startup_error:
            message += f"\nUltimo erro: {self._startup_error}"
        notify_error("Media Library Backend", message)
        self.logger.warning("Backend not ready after startup timeout. admin=%s", self.admin_url)

    def _open_admin(self, _icon: Icon | None, _item: MenuItem | None) -> None:
        if not self._is_backend_responding():
            self._start_backend()
        self._wait_until_ready(timeout_seconds=10)
        self._open_url(self.admin_url)

    def _open_docs(self, _icon: Icon | None, _item: MenuItem | None) -> None:
        if not self._is_backend_responding():
            self._start_backend()
        self._wait_until_ready(timeout_seconds=10)
        self._open_url(self.docs_url)

    def _open_logs(self, _icon: Icon | None, _item: MenuItem | None) -> None:
        try:
            os.startfile(str(self.log_file))
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to open log file: %s", exc)

    def _exit(self, icon: Icon, _item: MenuItem | None) -> None:
        self._shutdown.set()
        self._stop_backend()
        icon.stop()

    def _stop_backend(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True

        thread = self._server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=8.0)

        # Fallback: ensure process exits even if uvicorn is hanging.
        if thread is not None and thread.is_alive():
            os._exit(0)

    def _wait_until_ready(self, timeout_seconds: int) -> bool:
        deadline = time.time() + max(1, timeout_seconds)
        while time.time() < deadline and not self._shutdown.is_set():
            if self._is_backend_responding():
                return True
            thread = self._server_thread
            if thread is not None and not thread.is_alive() and self._startup_error:
                return False
            time.sleep(0.5)
        return self._is_backend_responding()

    def _is_backend_responding(self) -> bool:
        try:
            req = urllib.request.Request(self.health_url, method="GET")
            with urllib.request.urlopen(req, timeout=1.2) as response:
                if response.status != 200:
                    return False
                payload = response.read(256).decode("utf-8", errors="ignore").lower()
                return '"status"' in payload and "ok" in payload
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _open_url(self, url: str) -> None:
        try:
            if os.name == "nt":
                os.startfile(url)  # type: ignore[attr-defined]
                return
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("os.startfile failed for url=%s error=%s", url, exc)
        webbrowser.open_new_tab(url)


def parse_args() -> argparse.Namespace:
    from app.core.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Media Library Backend Tray")
    parser.add_argument("--host", default=settings.backend_host, help="Backend host bind")
    parser.add_argument(
        "--port",
        type=int,
        default=settings.backend_port,
        help=f"Backend port (default: {settings.backend_port})",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not auto-open admin in browser on startup",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_file = resolve_log_file()
    logger = setup_tray_logging(log_file)
    backend_workdir = resolve_backend_workdir()
    os.chdir(backend_workdir)
    logger.info("Starting tray. workdir=%s host=%s port=%s", backend_workdir, args.host, args.port)

    app = BackendTrayApp(
        host=args.host,
        port=args.port,
        open_browser_on_start=not args.no_open_browser,
        log_file=log_file,
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
