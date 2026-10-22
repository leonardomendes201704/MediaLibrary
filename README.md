# MediaLibrary

Uma biblioteca de mídia pessoal com backend em Python/FastAPI, painel administrativo web, galeria web e aplicativo Android (mobile e TV). O sistema gerencia vídeos locais, integra-se ao Reddit para downloads de conteúdo e oferece streaming adaptativo com suporte a busca temporal (seek).

## Visão Geral

O MediaLibrary é uma solução completa para gerenciamento, organização e reprodução de vídeos armazenados localmente. O sistema inclui:

- **Backend robusto** em FastAPI com API RESTful
- **Painel administrativo web** para gerenciamento completo
- **Galeria web** organizada por categorias
- **Aplicativo Android** com versões mobile e TV (Fire TV)
- **Integração com Reddit** para download automático de vídeos
- **Pipeline automatizado** de ingestão, transcodificação e geração de thumbnails
- **Streaming inteligente** com suporte a HTTP Range requests

## Tecnologias

### Backend
- **Python 3.10+** com FastAPI
- **SQLite** como banco de dados principal
- **FFmpeg/FFprobe** para processamento de vídeo e análise de metadados
- **SQLAlchemy** para ORM e acesso ao banco de dados
- **Uvicorn** como servidor ASGI

### Frontend Web
- **JavaScript vanilla** (sem frameworks)
- **HTML5/CSS3** responsivo
- Templates renderizados no servidor

### Aplicativo Android
- **Kotlin** nativo
- **ExoPlayer** para reprodução de vídeo
- **Retrofit** para comunicação com API
- Suporte para mobile e Android TV (Fire TV)

## Estrutura do Projeto

```
MediaLibrary/
├── backend/                    # Backend Python/FastAPI
│   ├── app/
│   │   ├── api/
│   │   │   └── router.py      # API REST completa
│   │   ├── core/
│   │   │   └── config.py      # Configurações do sistema
│   │   ├── db/
│   │   │   ├── models.py      # Schema do banco de dados
│   │   │   ├── schemas.py     # Contratos request/response
│   │   │   └── runtime_migrations.py  # Migrações automáticas
│   │   ├── services/
│   │   │   ├── ingest_service.py       # Ingestão de vídeos
│   │   │   ├── job_service.py          # Gerenciamento de jobs
│   │   │   ├── feed_service.py         # Feed e filtros
│   │   │   ├── ffmpeg_service.py       # Transcode e thumbnails
│   │   │   ├── streaming.py            # Streaming de vídeo
│   │   │   ├── reddit_service.py       # Integração Reddit
│   │   │   └── reddit_download_service.py
│   │   ├── web/
│   │   │   ├── templates/     # HTML templates
│   │   │   └── static/        # CSS e JavaScript
│   │   └── main.py            # Bootstrap da aplicação
│   ├── requirements.txt
│   └── scripts/               # Scripts utilitários
├── android-app/               # Aplicativo Android
│   └── app/src/main/java/com/leonardo/medialibrary/
│       ├── data/
│       │   ├── remote/
│       │   │   └── BackendApi.kt      # Contrato Retrofit
│       │   └── repository/
│       │       └── MediaRepositoryImpl.kt
│       └── ui/
│           ├── common/
│           │   └── FeedViewModel.kt
│           ├── mobile/
│           │   └── MobileFeedFragment.kt
│           └── tv/
│               └── TvPlayerFragment.kt
└── scripts/                   # Scripts de build e execução
```

## Funcionalidades Principais

### Gerenciamento de Vídeos
- **Ingestão automática**: Monitora pastas configuradas e ingere novos vídeos automaticamente
- **Transcodificação**: Converte vídeos para formato otimizado para streaming
- **Thumbnails**: Geração automática de thumbnails
- **Metadados**: Extração automática de duração, resolução, codec de áudio/vídeo
- **Detecção de áudio**: Identifica se o vídeo possui trilha de áudio
- **Reprocessamento**: Permite reprocessar vídeos individualmente ou em lote

### Organização
- **Pastas**: Cadastro e monitoramento de pastas de origem
- **Categorias**: Categorização automática baseada em palavras-chave
- **Favoritos**: Marcação de vídeos favoritos
- **Visualizados**: Controle de progresso de visualização
- **Busca**: Filtros por categoria, status e outros critérios

### Feed e Reprodução
- **Feed personalizado**: Ordenação por data, nome, tamanho, duração
- **Paginação**: Carregamento eficiente com cursor-based pagination
- **Streaming adaptativo**: Suporte a seek (busca temporal) via HTTP Range
- **Marcação automática**: Marca como visualizado ao atingir 50% (web) ou 70% (Android)
- **Sincronização**: Estado de visualização e favoritos sincronizados entre dispositivos

### Integração com Reddit
- **Subscrições**: Configure subreddits para monitoramento automático
- **Download automático**: Baixa vídeos de posts do Reddit
- **Filtros**: Configure critérios de download (subreddit, keywords, etc.)
- **Gerenciamento**: Painel para gerenciar downloads e subscrições

### Pipeline Automatizado
- **Folder Watcher**: Monitora pastas em tempo real por novos arquivos
- **Job Queue**: Sistema de fila para processamento assíncrono
- **Workers**: Processamento em background com controle de concorrência
- **Retry**: Tentativas automáticas em caso de falha

## Instalação e Configuração

### Pré-requisitos

- **Python 3.10+**
- **FFmpeg** instalado e disponível no PATH
- **Node.js** (opcional, para desenvolvimento frontend)
- **Android Studio** (para compilar o app Android)

### Backend

1. Clone o repositório:
```bash
git clone <repository-url>
cd MediaLibrary
```

2. Instale as dependências:
```bash
cd backend
pip install -r requirements.txt
```

3. Configure as variáveis de ambiente:
```bash
cp .env.example .env
# Edite o arquivo .env conforme necessário
```

Principais configurações:
- `DATABASE_URL`: Caminho para o banco SQLite
- `MEDIA_FOLDER`: Pasta raiz para armazenamento de vídeos
- `THUMBNAIL_FOLDER`: Pasta para thumbnails
- `FFMPEG_PATH`: Caminho para o executável do FFmpeg
- `WORKERS`: Número de workers para processamento paralelo

4. Execute o backend:
```bash
# Desenvolvimento
python -m uvicorn app.main:app --reload --port 8000

# Ou use o script PowerShell
.\scripts\run_dev.ps1
```

5. Acesse o painel admin:
- URL: `http://localhost:8000/admin`
- API docs: `http://localhost:8000/docs`

### Build do Executável Windows

Para criar um executável standalone do backend:

```bash
.\scripts\build_backend_tray_exe.bat
```

O executável será gerado em `dist/MediaLibraryBackendTray.exe`.

Para executar o executável:
```bash
.\scripts\run_backend_tray_exe.bat
```

### Aplicativo Android

1. Abra o projeto no Android Studio:
```bash
cd android-app
```

2. Sincronize as dependências Gradle

3. Configure a URL do backend em `BackendApi.kt`

4. Compile e execute:
- Use o build debug para testes
- Gere APKs assinados para produção

Build via script:
```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_android_debug.ps1
```

## Uso

### Painel Administrativo (`/admin`)

O painel admin oferece controle completo sobre o sistema:

- **Dashboard**: KPIs e estatísticas gerais
  - Total de pastas e vídeos
  - Vídeos com/sem áudio
  - Vídeos visualizados e favoritos
  - Status dos jobs
  - Espaço em disco utilizado

- **Controle**: Iniciar/pausar/parar o watcher e workers
- **Pastas**: Adicionar/remover pastas monitoradas
- **Vídeos**: Gerenciar vídeos, reprocessar, deletar
- **Jobs**: Visualizar fila de processamento
- **Configurações**: Ajustar parâmetros do sistema
- **Reddit**: Configurar integrações com Reddit
- **Anotações**: Bloco de texto para notas pessoais

### Galeria Web (`/admin/category`)

Interface de visualização e reprodução:

- Navegação por categorias
- Filtro por favoritos
- Player de vídeo com controles
- Marcação automática de visualização (50% de progresso)
- Toggle de favorito
- Badges indicando status (visualizado, favorito, sem áudio)

### Aplicativo Android

#### Mobile
- Feed scrollável de vídeos
- Player fullscreen
- Gestos para controle de volume e brilho
- Marcação automática aos 70% de progresso
- Suporte a orientação retrato e paisagem

#### TV (Fire TV)
- Interface otimada para controle remoto
- Navegação por D-pad
- Player fullscreen
- Marcação automática aos 70% de progresso
- Suporte a Fire TV Remote

### API REST

Documentação completa disponível em `/docs` (Swagger UI).

Endpoints principais:
- `GET /health` - Health check
- `GET /stats` - Estatísticas do sistema
- `GET /folders` - Listar pastas
- `POST /folders` - Adicionar pasta
- `GET /videos` - Listar vídeos
- `PUT /videos/{id}/watched` - Marcar como visualizado
- `PUT /videos/{id}/favorite` - Marcar como favorito
- `GET /feed` - Feed de vídeos com filtros
- `GET /feed/categories` - Listar categorias
- `POST /jobs` - Gerenciar jobs
- `GET /reddit/subscriptions` - Gerenciar Reddit

## Arquitetura

### Fluxo de Ingestão

1. **Folder Watcher** detecta novo arquivo em pasta monitorada
2. Arquivo é adicionado à tabela `videos` com status `pending`
3. **Job Worker** pega o job da fila
4. **FFmpegService** executa ffprobe para extrair metadados
5. Vídeo é transcoded para formato otimizado (se necessário)
6. Thumbnail é gerada
7. Metadados são persistidos no banco
8. Status é atualizado para `ready`

### Streaming

O sistema utiliza HTTP Range requests para permitir:
- Seek preciso em qualquer ponto do vídeo
- Playback eficiente sem download completo
- Suporte a players HTML5 e ExoPlayer
- Byte-range serving para chunks de vídeo

### Banco de Dados

Schema principal:
- **Folder**: Pastas monitoradas
- **Video**: Metadados dos vídeos (status, duração, resolução, has_audio, is_watched, is_favorite, etc.)
- **Job**: Jobs de processamento
- **AppConfig**: Configurações chave-valor persistentes

Migrações são aplicadas automaticamente no startup via `runtime_migrations.py`.

## Desenvolvimento

### Validação de Código

Backend Python:
```bash
python -m py_compile backend/app/api/router.py
python -m py_compile backend/app/db/schemas.py
```

Frontend web:
```bash
node --check backend/app/web/static/admin.js
node --check backend/app/web/static/category_gallery.js
```

Android:
```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_android_debug.ps1
```

### Cache de Assets

Ao modificar JavaScript ou CSS servido pelo backend, atualize a versão no template:
```html
<script src="/static/admin.js?v=2"></script>
```

Isso evita cache antigo no navegador.

## Fontes de Verdade

Por domínio, os arquivos centrais são:

- **Folders**: `db/models.py` + rotas `/folders` + `watcher_service.py`
- **Videos**: `db/models.py` + rotas `/videos` + `ingest_service.py`
- **Áudio**: `Video.has_audio` (fonte de verdade) + `FFmpegService.probe()`
- **Feed**: `/feed` em `router.py` + `feed_service.py`
- **Jobs**: `job_service.py` + rota `/jobs`
- **Stats**: `StatsOut` em `schemas.py` + `GET /stats` + `loadHealthAndStats()`
- **Config**: `core/config.py` + `/config` (K/V persistente)
- **Reddit**: Rotas `/reddit/*` + `reddit_service.py`
- **Android**: `BackendApi.kt` → `MediaRepositoryImpl.kt` → `FeedViewModel.kt`

## Troubleshooting

### FFmpeg não encontrado
- Verifique se FFmpeg está instalado: `ffmpeg -version`
- Configure `FFMPEG_PATH` no `.env`
- Use o script `backend/scripts/check_ffmpeg.ps1` para diagnóstico

### Vídeos sem áudio
- O backend detecta presença de áudio via ffprobe durante ingestão
- Para backfill em catálogo existente: `POST /videos/backfill-audio?limit=100`
- `audio_codec` mostra o codec específico quando há áudio

### Jobs presos na fila
- Verifique logs do worker
- Reinicie os workers via painel admin (Control → Stop/Start)
- Cheque espaço em disco e permissões

### Problemas de cache no browser
- Atualize a versão `?v=` nos templates
- Force refresh com Ctrl+F5
- Limpe cache do navegador

## Contribuição

1. Faça checkout de uma nova branch a partir da `main`
2. Implemente suas mudanças
3. Execute validações (py_compile, node --check)
4. Commit seguindo o padrão do projeto
5. Abra um Pull Request

## Licença

Este projeto é privado. Consulte o proprietário para permissões de uso.

## Contato

Para dúvidas, sugestões ou problemas, abra uma issue no repositório.

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)