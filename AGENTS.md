# AGENTS.md

## Objetivo deste guia

- Este arquivo e a porta de entrada para entender o projeto sem precisar ler arquivos grandes por inteiro.
- Primeiro classifique o pedido por area: backend API, painel admin web, galeria web, pipeline/watcher/jobs, feed Android, Reddit ou schema do banco.
- Nao comecar lendo `backend/app/api/router.py` ou `backend/app/web/static/admin.js` inteiros. Em arquivos grandes, localizar primeiro a ancora certa com busca por endpoint, funcao, classe de schema, id de template ou nome de service.
- Ordem padrao de investigacao: contrato -> handler/rota -> service -> model -> consumidor frontend/android -> template.
- Se a alteracao tocar payload JSON, verificar sempre se existe consumidor no web admin, na galeria web ou no Android.

## Mapa rapido do monorepo

- `backend/app/main.py`: bootstrap do FastAPI, monta `/static`, serve `/admin` e `/admin/category`, instancia services no startup e guarda em `app.state`.
- `backend/app/api/router.py`: API REST inteira. E o arquivo central do backend e concentra endpoints de health, control, folders, videos, feed, jobs, stats e Reddit.
- `backend/app/core/config.py`: configuracoes por env, caminho do SQLite, diretorios de output/thumbs, workers e opcoes de transcode.
- `backend/app/db/models.py`: schema real do SQLite via SQLAlchemy.
- `backend/app/db/schemas.py`: contratos request/response da API.
- `backend/app/db/runtime_migrations.py`: colunas e indices adicionados no startup para compatibilidade.
- `backend/app/services/ingest_service.py`: ingestao, reprocessamento, limpeza e operacoes de alto nivel sobre videos.
- `backend/app/services/job_service.py`: fila de jobs, worker e stats da fila.
- `backend/app/services/feed_service.py`: filtros, ordenacao, paginacao e categorias do feed.
- `backend/app/services/ffmpeg_service.py`: ffprobe, transcode, copy-faststart, thumbnails.
- `backend/app/services/streaming.py`: streaming com suporte a Range.
- `backend/app/services/reddit_service.py` e `backend/app/services/reddit_download_service.py`: integracao e downloads do Reddit.
- `backend/app/web/templates/index.html`: shell do painel admin.
- `backend/app/web/templates/category_gallery.html`: shell da galeria por categoria/favoritos.
- `backend/app/web/static/admin.js`: frontend do admin. Carrega stats, control, folders, videos, jobs, config e Reddit.
- `backend/app/web/static/category_gallery.js`: frontend da galeria web, badges, playback e marcacao de watched/favorite.
- `android-app/app/src/main/java/com/leonardo/medialibrary/data/remote/BackendApi.kt`: contrato Retrofit consumido pelo app Android.
- `android-app/app/src/main/java/com/leonardo/medialibrary/data/repository/MediaRepositoryImpl.kt`: ponte entre Retrofit/DataStore e UI.
- `android-app/app/src/main/java/com/leonardo/medialibrary/ui/common/FeedViewModel.kt`: estado compartilhado e acoes do feed no Android.
- `android-app/app/src/main/java/com/leonardo/medialibrary/ui/mobile/MobileFeedFragment.kt`: player/feed mobile.
- `android-app/app/src/main/java/com/leonardo/medialibrary/ui/tv/TvPlayerFragment.kt`: player/feed TV.

## Como o sistema sobe

- O backend sobe em `backend/app/main.py`.
- No startup ele faz `Base.metadata.create_all`, roda `ensure_runtime_schema(engine)`, instancia `FFmpegService`, `JobService`, `IngestService`, `JobWorker`, `FolderWatcherService`, `RedditService` e `RedditDownloadService`.
- `JobWorker`, `FolderWatcherService` e os services compartilhados ficam em `app.state` e sao lidos pelas rotas.
- `/admin` e `/admin/category` sao paginas shell renderizadas no servidor; os dados dinamicos vem de chamadas JS para `/api/...`.

## Fonte de verdade por dominio

- Folders: `db/models.py` + rotas `/folders` em `api/router.py` + watcher em `services/watcher_service.py`.
- Videos: `db/models.py` + rotas `/videos*` em `api/router.py` + regras em `services/ingest_service.py`.
- Presenca de audio por video: usar `Video.has_audio` como fonte de verdade; ele e preenchido pelo `ffprobe` em `services/ffmpeg_service.py` e persistido no ingest em `services/ingest_service.py`. `audio_codec` continua util para exibir o codec quando houver audio.
- Feed: rotas `/feed` e `/feed/categories` em `api/router.py` + logica de listagem em `services/feed_service.py`.
- Jobs: `services/job_service.py` e rota `/jobs`; stats da fila saem de `worker.queue_stats(session)`.
- Stats do dashboard: `StatsOut` em `db/schemas.py` + `GET /stats` em `api/router.py` + `loadHealthAndStats()` em `web/static/admin.js`.
- Config: `core/config.py` para env/processo e `/config` para K/V persistido no app.
- Anotacoes do admin: ficam na tabela `app_config`, persistidas via `GET/PUT /config` com a chave `admin_notes`; o frontend usa a secao `Anotacoes` e a funcao `loadConfig()` em `web/static/admin.js`.
- Reddit: rotas `/reddit/*` em `api/router.py` + services dedicados em `backend/app/services`.
- Android backend contract: `data/remote/BackendApi.kt`. Se mudar endpoint consumido pelo app, revisar esse arquivo primeiro.

## Roteiro de triagem por tipo de pedido

- Se o pedido mencionar `KPI`, `cards do topo`, `dashboard admin`, `stats`, `painel`: comecar por `StatsOut`, depois `GET /stats`, depois `loadHealthAndStats()`, e por ultimo `index.html` se houver mudanca visual/cache.
- Se o pedido mencionar `folders`, `watcher`, `pipeline`, `start/pause/stop`, `monitoramento`: ir para as rotas `/control/*` e `/folders`, depois `JobWorker`, `FolderWatcherService` e `IngestService`.
- Se o pedido mencionar `videos`, `favoritos`, `visualizados`, `watched`, `favorite`, `reprocessar`, `deletar`, `cleanup`: ir para as rotas `/videos*`, depois `Video` em `db/models.py`, depois `category_gallery.js` e `FeedViewModel` se houver impacto no playback.
- Se o pedido mencionar `audio`, `som`, `video sem som`, `video com audio`, `codec de audio`: comecar por `Video.has_audio` e `Video.audio_codec` em `db/models.py`, depois `FFmpegService.probe()`, `IngestService._process_path()`, `/videos`, `/videos/backfill-audio`, e por fim `/stats` se tambem houver KPI.
- Se o pedido mencionar `feed`, `ordem`, `paginacao`, `cursor`, `categorias`, `filtro`: abrir `/feed` e `/feed/categories` em `api/router.py`, depois `services/feed_service.py`, depois consumidores `category_gallery.js`, `BackendApi.kt`, `MediaRepositoryImpl.kt` e `FeedViewModel.kt`.
- Se o pedido mencionar `stream`, `seek`, `thumbnail`, `Range`: ir para `/videos/{id}/stream`, `/videos/{id}/thumbnail`, `services/streaming.py` e `services/ffmpeg_service.py`.
- Se o pedido mencionar `admin web`, `folders/videos/jobs/config no painel`, mas nao `stats`: ir direto para a funcao `load...` correspondente em `admin.js`:
  `loadControlStatus()`, `loadFolders()`, `loadVideos()`, `loadJobs()`, `loadConfig()`, `loadRedditConfigStatus()`, `loadRedditSubscriptions()`, `loadCurrentRedditDownloadStatus()`.
- Se o pedido mencionar `anotacoes`, `notes`, `bloco de texto`, `textarea no admin`: comecar por a secao `Anotacoes` em `web/templates/index.html`, depois `loadConfig()` e o submit de `notes-form` em `web/static/admin.js`; a persistencia continua em `/config` com a chave `admin_notes`.
- Se o pedido mencionar `galeria`, `categoria`, `pagina de favoritos`, `badge`, `player web`: comecar por `category_gallery.html` e `category_gallery.js`, depois olhar as rotas `/feed`, `/videos/{id}/watched` e `/videos/{id}/favorite`.
- Se o pedido mencionar `Android`, `mobile`, `TV`, `Fire TV`, `player do app`: comecar por `MainActivity.kt`, depois `FeedViewModel.kt`, e seguir para `MobileFeedFragment.kt` ou `TvPlayerFragment.kt`. Se envolver API, revisar tambem `BackendApi.kt` e `MediaRepositoryImpl.kt`.
- Se o pedido mencionar `Reddit`, `subscriptions`, `download`: comecar pelas rotas `/reddit/*`, depois `reddit_service.py`, `reddit_download_service.py` e as funcoes `loadReddit...` em `admin.js`.
- Se o pedido mencionar `banco`, `coluna`, `migracao`, `schema SQLite`: comecar por `db/models.py`, depois `db/runtime_migrations.py`, depois `db/schemas.py` se o payload da API mudar.

## Estrategia de leitura em arquivos grandes

- Em `backend/app/api/router.py`, localizar primeiro pelo endpoint exato (`@router.get("/stats")`, `@router.put("/videos/{video_id}/watched")`, etc.) ou pelo nome da funcao.
- Em `backend/app/web/static/admin.js`, localizar pela funcao `load...` ou `render...` relacionada ao pedido.
- Em `backend/app/web/static/category_gallery.js`, localizar por `markVideoWatched`, `markVideoFavorite`, `loadMore`, `renderThumbCard` ou pelo endpoint consumido.
- Em `FeedViewModel.kt`, localizar pela acao do usuario (`markCurrentAsWatched`, carregamento de feed, toggle favorite, delete).
- Nao abrir o arquivo inteiro se a ancora for suficiente para entender o fluxo local.

## Superficies web e seus dados

- O admin e servido em `/admin` e usa `backend/app/web/templates/index.html` + `backend/app/web/static/admin.js`.
- A galeria e servida em `/admin/category` e usa `backend/app/web/templates/category_gallery.html` + `backend/app/web/static/category_gallery.js`.
- `window.API_BASE` e definido no template e usado pelo JS.
- Sempre que mudar JS servido pelo backend, atualizar a versao `?v=` do asset no template correspondente para evitar cache antigo no navegador.

## KPIs do dashboard admin

- O dashboard admin fica em `/admin`.
- O conteiner visual dos cards e `#stats-grid` em `backend/app/web/templates/index.html`.
- Os cards sao montados no navegador pela funcao `loadHealthAndStats()` em `backend/app/web/static/admin.js`.
- Essa funcao busca `GET /health`, `GET /stats` e `GET /feed/categories`, mas os KPIs do topo vem de `GET /stats`.
- O chip `Favoritos` na nuvem de categorias nao vem de `/feed/categories`; ele e sintetizado no frontend a partir de `stats.videos_favorites`.

## Fonte de verdade dos KPIs

- O contrato JSON de `/stats` fica em `backend/app/db/schemas.py`, na classe `StatsOut`.
- A agregacao dos KPIs fica em `backend/app/api/router.py`, na rota `@router.get("/stats")`.
- Os valores sao calculados direto do banco SQLite via SQLAlchemy, principalmente sobre as tabelas/modelos `Folder` e `Video`.
- Os KPIs de jobs (`jobs_pending`, `jobs_running`, `jobs_failed`) vem de `worker.queue_stats(session)`.
- O contexto `Processando agora` (`processing_current_category`, `processing_current_folder`, `processing_current_video_id`) tambem sai de `/stats`, mas nao e um card; ele e preenchido por `_current_processing_context(...)`.

## Significado atual dos KPIs

- `folders_total`: total de pastas cadastradas.
- `folders_active`: total de pastas ativas.
- `videos_total`: videos nao deletados.
- `videos_ready`: videos nao deletados com status `ready` ou `duplicate`.
- `videos_with_audio`: videos nao deletados com status `ready` ou `duplicate` e `has_audio = true`.
- `videos_without_audio`: videos nao deletados com status `ready` ou `duplicate` e `has_audio = false`.
- `videos_watched`: videos nao deletados com status `ready` ou `duplicate` e `is_watched = true`.
- `videos_watched_not_favorited`: videos nao deletados com status `ready` ou `duplicate`, `is_watched = true` e `is_favorite = false`.
- `videos_favorites`: videos nao deletados com status `ready` ou `duplicate` e `is_favorite = true`.
- `ready_storage_bytes`: espaco ocupado pelos assets prontos, calculado no backend por `_ready_storage_bytes(...)`.
- `videos_pending`: videos nao deletados com status `new` ou `pending`.
- `videos_processing`: videos nao deletados com status `processing`.
- `videos_error`: videos nao deletados com status `error`.
- `jobs_pending`, `jobs_running`, `jobs_failed`: estado da fila de jobs, nao contagem da tabela `videos`.

## Como watched e favorite sao persistidos

- A persistencia de watched acontece em `PUT /videos/{video_id}/watched` em `backend/app/api/router.py`.
- A presenca de audio e inferida no ingest pelo `ffprobe`; o backend nao analisa silencio real, apenas se existe stream/trilha de audio.
- Para catalogo legado, existe o backfill explicito `POST /videos/backfill-audio?limit=...`, que tenta preencher `has_audio` e `audio_codec` em lotes sem retranscodificar.
- Essa rota atualiza `Video.is_watched`, `Video.watched_at` e `Video.watched_ratio`.
- A persistencia de favorite acontece em `PUT /videos/{video_id}/favorite` em `backend/app/api/router.py`.
- Essa rota atualiza `Video.is_favorite` e `Video.favorite_at`.
- No web player da galeria (`backend/app/web/static/category_gallery.js`), o video e marcado como visualizado quando a reproducao atinge pelo menos `50%`.
- No Android, mobile e TV marcam como visualizado quando a reproducao atinge `70%`:
  - `android-app/app/src/main/java/com/leonardo/medialibrary/ui/mobile/MobileFeedFragment.kt`
  - `android-app/app/src/main/java/com/leonardo/medialibrary/ui/tv/TvPlayerFragment.kt`
- Ambos passam pela chamada de repositorio usada por `FeedViewModel.markCurrentAsWatched(...)`.
- A limpeza em lote de assistidos nao favoritados passa por `POST /videos/cleanup/watched-unfavorited`.

## Checklist de alteracao por area

1. Se mudar payload de API: atualizar `db/schemas.py`, a rota correspondente em `api/router.py` e todo consumidor relevante (`admin.js`, `category_gallery.js`, `BackendApi.kt`, `MediaRepositoryImpl.kt`, `FeedViewModel.kt`).
2. Se mudar schema do banco: atualizar `db/models.py` e `db/runtime_migrations.py`; se expor o dado via API, atualizar tambem `db/schemas.py`.
3. Se mudar KPI/card do admin: atualizar `StatsOut`, `GET /stats`, `loadHealthAndStats()` e subir `admin.js?v=...` em `index.html`.
4. Se mudar galeria web: atualizar `category_gallery.js`; se o HTML shell mudar, revisar `category_gallery.html`.
5. Se mudar contrato consumido pelo Android: revisar `BackendApi.kt` primeiro, depois `MediaRepositoryImpl.kt`, `FeedViewModel.kt` e o fragment relevante.
6. Se mudar logica de ingestao/transcode: revisar `IngestService`, `FFmpegService`, possiveis rotas em `router.py` e impactos em `Video.status`.
7. Se mudar configuracao global: revisar `core/config.py`, `GET/PUT /config` e qualquer UI que exiba ou edite esses valores.
8. Se mudar a area `Anotacoes`: revisar `index.html`, `admin.js`, estilos do `textarea` em `styles.css` e lembrar que o valor vive em `app_config.value` com chave `admin_notes`.

## Validacao rapida recomendada

- Backend Python: `python -m py_compile backend/app/api/router.py backend/app/db/schemas.py`
- Se alterar models/migrations/services, incluir tambem os arquivos Python tocados no `py_compile`.
- Frontend web: `node --check backend/app/web/static/admin.js` e/ou `node --check backend/app/web/static/category_gallery.js`
- Android: se tocar codigo Kotlin, preferir pelo menos `powershell -ExecutionPolicy Bypass -File scripts/build_android_debug.ps1`
- FFmpeg/local setup: scripts uteis em `backend/scripts/check_ffmpeg.ps1`, `backend/scripts/run_dev.ps1`, `scripts/run_backend.ps1`

## Build do executavel

- Para compilar o executavel Windows do backend tray, usar `scripts/build_backend_tray_exe.bat` a partir da raiz do repo.
- O build gera `dist/MediaLibraryBackendTray.exe`.
- Para abrir o executavel gerado, usar `scripts/run_backend_tray_exe.bat`.

## Regras praticas para futuras instrucoes

- Se o pedido mencionar um endpoint exato, partir desse endpoint e nao do topo do arquivo.
- Se o pedido mencionar um comportamento visivel no admin, identificar primeiro qual funcao `load...` ou `render...` controla aquela secao.
- Se o pedido mencionar um comportamento visivel no Android, identificar primeiro se ele nasce em `FeedViewModel`, `MobileFeedFragment` ou `TvPlayerFragment`.
- Se o pedido mencionar `favoritos` no admin, lembrar que o card sintetico da nuvem usa `videos_favorites`, nao `/feed/categories`.
- Se o pedido mencionar `visualizados`, confirmar se a expectativa e baseada em `is_watched` persistido, em `watched_ratio`, em playback web 50%, em playback Android 70% ou em algum criterio novo de analytics.
