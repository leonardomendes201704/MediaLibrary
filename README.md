# MediaLibrary

Monorepo para ingestao, catalogacao, reproducao e distribuicao local de videos, com:

- backend FastAPI + SQLite + FFmpeg
- painel admin web
- galeria web por categoria/favoritos/nao vistos
- app Android para mobile e TV
- integracao com Reddit para downloads
- build Windows em tray (`MediaLibraryBackendTray.exe`)

## Estrutura

```text
MediaLibrary/
|- backend/
|  |- app/
|  |  |- api/
|  |  |- core/
|  |  |- db/
|  |  |- services/
|  |  `- web/
|  |- requirements.txt
|  |- requirements-tray.txt
|  `- tray_app.py
|- android-app/
|- scripts/
|- tools/
|- AGENTS.md
`- README.md
```

## Principais recursos

- monitoramento de pastas com watcher e fila de jobs
- ingestao resiliente com deduplicacao e reprocessamento
- transcode/remux com FFmpeg e geracao de thumbs
- feed com filtros por categoria, favoritos, vistos e nao vistos
- player web com atalhos de teclado
- downloads do Reddit por categoria, usuario e periodo
- historico para evitar re-download de videos Reddit ja processados
- reset completo da base preservando configuracoes
- build do backend como executavel de tray no Windows

## Backend

Stack principal:

- FastAPI
- SQLAlchemy
- SQLite
- Watchdog
- FFmpeg / ffprobe

Arquivos centrais:

- `backend/app/main.py`
- `backend/app/api/router.py`
- `backend/app/db/models.py`
- `backend/app/db/schemas.py`
- `backend/app/services/ingest_service.py`
- `backend/app/services/feed_service.py`
- `backend/app/services/reddit_download_service.py`

## Frontend web

Superficies:

- admin: `/admin`
- galeria por categoria/favoritos/nao vistos: `/admin/category`

Arquivos principais:

- `backend/app/web/templates/index.html`
- `backend/app/web/templates/category_gallery.html`
- `backend/app/web/static/admin.js`
- `backend/app/web/static/category_gallery.js`
- `backend/app/web/static/styles.css`

## Android

O app Android compartilha o mesmo backend e atende mobile e TV.

Arquivos principais:

- `android-app/app/src/main/java/com/leonardo/medialibrary/data/remote/BackendApi.kt`
- `android-app/app/src/main/java/com/leonardo/medialibrary/data/repository/MediaRepositoryImpl.kt`
- `android-app/app/src/main/java/com/leonardo/medialibrary/ui/common/FeedViewModel.kt`
- `android-app/app/src/main/java/com/leonardo/medialibrary/ui/mobile/MobileFeedFragment.kt`
- `android-app/app/src/main/java/com/leonardo/medialibrary/ui/tv/TvPlayerFragment.kt`

## Como rodar o backend

Pre-requisitos:

- Python 3.11, 3.12 ou 3.13
- FFmpeg e ffprobe instalados no sistema

Setup:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup_backend.ps1
```

Execucao:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_backend.ps1
```

URLs:

- admin: `http://127.0.0.1:8000/admin`
- docs: `http://127.0.0.1:8000/docs`
- health: `http://127.0.0.1:8000/api/health`

## Build do executavel Windows

Gerar:

```bat
scripts\build_backend_tray_exe.bat
```

Executar:

```bat
scripts\run_backend_tray_exe.bat
```

Saida:

- `dist\MediaLibraryBackendTray.exe`

## Observacoes de runtime

- os bancos SQLite ativos normalmente ficam em `backend/media_library.db` quando o tray roda empacotado
- artefatos de runtime e downloads nao fazem parte do versionamento
- configuracoes persistidas do app ficam em SQLite (`app_config`)

## Repositorio

Este workspace foi preparado para ser publicado em:

- `https://github.com/leonardomendes201704/MediaLibrary`
