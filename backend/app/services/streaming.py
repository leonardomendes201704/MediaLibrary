import mimetypes
from email.utils import formatdate
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse


CHUNK_SIZE = 1024 * 1024


def parse_range_header(range_header: str, file_size: int) -> tuple[int, int]:
    if not range_header.startswith("bytes="):
        raise HTTPException(status_code=416, detail="Invalid range unit")

    value = range_header.replace("bytes=", "").strip()
    if "-" not in value:
        raise HTTPException(status_code=416, detail="Invalid range format")

    start_raw, end_raw = value.split("-", maxsplit=1)
    try:
        if start_raw == "":
            # suffix range: bytes=-500
            suffix = int(end_raw)
            if suffix <= 0:
                raise HTTPException(status_code=416, detail="Invalid suffix range")
            start = max(file_size - suffix, 0)
            end = file_size - 1
            return start, end

        start = int(start_raw)
        end = int(end_raw) if end_raw else file_size - 1
    except ValueError as exc:
        raise HTTPException(status_code=416, detail="Invalid range values") from exc

    if start > end or start >= file_size:
        raise HTTPException(status_code=416, detail="Range not satisfiable")
    end = min(end, file_size - 1)
    return start, end


def _iter_file_range(path: Path, start: int, end: int):
    with path.open("rb") as file_obj:
        file_obj.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = file_obj.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def head_stream_with_range(path: str, range_header: str | None):
    path_obj = Path(path)
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail="File not found")
    stat = path_obj.stat()
    file_size = stat.st_size
    media_type = mimetypes.guess_type(path_obj.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    }

    if not range_header:
        headers["Content-Length"] = str(file_size)
        return Response(status_code=200, media_type=media_type, headers=headers)

    start, end = parse_range_header(range_header, file_size)
    content_length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    headers["Content-Length"] = str(content_length)
    headers["Cache-Control"] = "public, max-age=3600"
    return Response(status_code=206, media_type=media_type, headers=headers)


def stream_with_range(path: str, range_header: str | None):
    path_obj = Path(path)
    if not path_obj.exists():
        raise HTTPException(status_code=404, detail="File not found")
    file_size = path_obj.stat().st_size
    media_type = mimetypes.guess_type(path_obj.name)[0] or "application/octet-stream"

    if not range_header:
        return FileResponse(
            path=str(path_obj),
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
            },
        )

    start, end = parse_range_header(range_header, file_size)
    content_length = end - start + 1
    return StreamingResponse(
        _iter_file_range(path_obj, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
            "Cache-Control": "public, max-age=3600",
        },
    )
