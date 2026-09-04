import os
import hmac
import html
import time
import uuid
import shutil
import subprocess
import platform
import zipfile
import socket
import ssl
import sys
import json
import re
import ipaddress
import hashlib
import importlib.metadata
from urllib.parse import quote, urlsplit
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg
import yt_dlp

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, RedirectResponse, HTMLResponse, FileResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

MCP_API_TOKEN = os.environ.get('MCP_API_TOKEN')
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
YOUTUBE_CLIENT_ID = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REDIRECT_URI = os.environ.get('YOUTUBE_REDIRECT_URI')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

# Full Netscape-format YouTube cookies content.
# Primary variable requested by the user is lowercase: cookies
# YOUTUBE_COOKIES is supported as a fallback alias.
YOUTUBE_COOKIES = os.environ.get('cookies') or os.environ.get('YOUTUBE_COOKIES')


SERVER_BUILD = '2026-09-04-proxy-cookie-debug-v5'

# Optional residential/ISP proxy for yt-dlp YouTube traffic.
# Prefer the split variables so credentials are URL-encoded safely.
YOUTUBE_PROXY = os.environ.get('YOUTUBE_PROXY')
YOUTUBE_PROXY_SCHEME = os.environ.get('YOUTUBE_PROXY_SCHEME', 'http').strip().lower()
YOUTUBE_PROXY_HOST = os.environ.get('YOUTUBE_PROXY_HOST')
YOUTUBE_PROXY_PORT = os.environ.get('YOUTUBE_PROXY_PORT')
YOUTUBE_PROXY_USERNAME = os.environ.get('YOUTUBE_PROXY_USERNAME')
YOUTUBE_PROXY_PASSWORD = os.environ.get('YOUTUBE_PROXY_PASSWORD')

YOUTUBE_SCOPES = [
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/youtube.force-ssl',
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/yt-analytics-monetary.readonly',
]

MEDIA_DIR = Path(os.environ.get('MEDIA_DIR', '/tmp/youtube_mcp_media'))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MAX_REMOTE_FILE_BYTES = int(os.environ.get('MAX_REMOTE_FILE_BYTES', str(500 * 1024 * 1024)))
MEDIA_URL_TTL_SECONDS = int(os.environ.get('MEDIA_URL_TTL_SECONDS', '3600'))
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
MEDIA_TOKENS: dict[str, dict[str, Any]] = {}

DENO_DIR = Path(os.environ.get('DENO_DIR', '/tmp/youtube_mcp_bin'))
DENO_EXE = DENO_DIR / 'deno'
DENO_DOWNLOAD_TIMEOUT = int(os.environ.get('DENO_DOWNLOAD_TIMEOUT', '180'))

mcp = FastMCP(
    'youtube-mcp',
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(RENDER_EXTERNAL_HOSTNAME),
        allowed_hosts=[RENDER_EXTERNAL_HOSTNAME] if RENDER_EXTERNAL_HOSTNAME else [],
    ),
)

def _oauth_config() -> dict:
    if not YOUTUBE_CLIENT_ID:
        raise RuntimeError('Missing YOUTUBE_CLIENT_ID.')
    if not YOUTUBE_CLIENT_SECRET:
        raise RuntimeError('Missing YOUTUBE_CLIENT_SECRET.')
    if not YOUTUBE_REDIRECT_URI:
        raise RuntimeError('Missing YOUTUBE_REDIRECT_URI.')
    return {'web': {'client_id': YOUTUBE_CLIENT_ID, 'client_secret': YOUTUBE_CLIENT_SECRET, 'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': [YOUTUBE_REDIRECT_URI]}}

def _oauth_flow(state: str | None = None) -> Flow:
    flow = Flow.from_client_config(_oauth_config(), scopes=YOUTUBE_SCOPES, state=state, autogenerate_code_verifier=False)
    flow.redirect_uri = YOUTUBE_REDIRECT_URI
    return flow

def _youtube_credentials() -> Credentials:
    if not YOUTUBE_REFRESH_TOKEN:
        raise RuntimeError('YouTube is not authorized yet. Open /oauth/start and authorize.')
    credentials = Credentials(token=None, refresh_token=YOUTUBE_REFRESH_TOKEN, token_uri='https://oauth2.googleapis.com/token', client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET, scopes=YOUTUBE_SCOPES)
    credentials.refresh(GoogleAuthRequest())
    return credentials

def _youtube():
    return build('youtube', 'v3', credentials=_youtube_credentials(), cache_discovery=False)

def _youtube_analytics():
    return build('youtubeAnalytics', 'v2', credentials=_youtube_credentials(), cache_discovery=False)

def _my_channel_id() -> str:
    result = _youtube().channels().list(part='id', mine=True).execute()
    items = result.get('items', [])
    if not items:
        raise RuntimeError('No YouTube channel found for this account.')
    return items[0]['id']

def _my_uploads_playlist_id() -> str:
    result = _youtube().channels().list(part='contentDetails', mine=True).execute()
    items = result.get('items', [])
    if not items:
        raise RuntimeError('No YouTube channel found.')
    return items[0]['contentDetails']['relatedPlaylists']['uploads']

def _get_owned_video(video_id: str, part: str = 'snippet,status') -> dict:
    result = _youtube().videos().list(part=part, id=video_id).execute()
    items = result.get('items', [])
    if not items:
        raise RuntimeError(f'Video not found: {video_id}')
    video = items[0]
    if video.get('snippet', {}).get('channelId') != _my_channel_id():
        raise PermissionError('This media operation is limited to videos owned by the authenticated YouTube channel.')
    return video

def _safe_filename(name: str) -> str:
    keep = []
    for ch in name:
        if ch.isalnum() or ch in '._-':
            keep.append(ch)
        else:
            keep.append('_')
    result = ''.join(keep).strip('._')
    return result[:180] or f'file_{uuid.uuid4().hex[:8]}'

def _cleanup_expired_media() -> None:
    now = time.time()
    for token, info in list(MEDIA_TOKENS.items()):
        if info['expires'] <= now:
            MEDIA_TOKENS.pop(token, None)
    cutoff = now - max(MEDIA_URL_TTL_SECONDS * 2, 7200)
    for path in MEDIA_DIR.glob('*'):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
            elif path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

def _publish_media(path: Path, ttl_seconds: int | None = None) -> dict:
    _cleanup_expired_media()
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    token = uuid.uuid4().hex
    expires_in = ttl_seconds or MEDIA_URL_TTL_SECONDS
    MEDIA_TOKENS[token] = {'path': str(path), 'expires': time.time() + expires_in}
    host = RENDER_EXTERNAL_HOSTNAME
    url = f'https://{host}/media/{token}/{path.name}' if host else None
    return {'media_id': token, 'filename': path.name, 'size_bytes': path.stat().st_size, 'expires_in_seconds': expires_in, 'url': url}

def _resolve_media(media_id: str) -> Path:
    _cleanup_expired_media()
    info = MEDIA_TOKENS.get(media_id)
    if not info:
        raise RuntimeError('Media ID is missing or expired. Generate/download the media again.')
    path = Path(info['path'])
    if not path.exists():
        raise RuntimeError('The temporary media file no longer exists.')
    return path

def _download_url(url: str, destination: Path, max_bytes: int = MAX_REMOTE_FILE_BYTES) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with httpx.stream('GET', url, follow_redirects=True, timeout=httpx.Timeout(30.0, read=300.0), headers={'User-Agent': 'YouTube-MCP/1.0'}) as response:
        response.raise_for_status()
        content_length = response.headers.get('content-length')
        if content_length and int(content_length) > max_bytes:
            raise RuntimeError(f'Remote file is too large. Limit is {max_bytes} bytes.')
        with destination.open('wb') as f:
            for chunk in response.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError(f'Remote file exceeded limit of {max_bytes} bytes.')
                f.write(chunk)
    return destination

def _run_ffmpeg(args: list[str], timeout: int = 600) -> dict:
    result = subprocess.run([FFMPEG_EXE] + args, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError('ffmpeg failed:\n' + result.stderr[-5000:])
    return {'returncode': result.returncode, 'stdout': result.stdout[-5000:], 'stderr': result.stderr[-5000:]}

def _deno_download_url() -> str:
    machine = platform.machine().lower()
    if machine in {'x86_64', 'amd64'}:
        asset = 'deno-x86_64-unknown-linux-gnu.zip'
    elif machine in {'aarch64', 'arm64'}:
        asset = 'deno-aarch64-unknown-linux-gnu.zip'
    else:
        raise RuntimeError(f'Unsupported CPU architecture for automatic Deno install: {machine}')
    return f'https://github.com/denoland/deno/releases/latest/download/{asset}'

def _deno_version(executable: Path) -> str:
    result = subprocess.run([str(executable), '--version'], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError('Deno exists but could not be executed: ' + (result.stderr or result.stdout)[-2000:])
    return (result.stdout or '').strip()

def _ensure_deno() -> Path:
    # Prefer a user-supplied Deno path, then PATH, then install a temporary copy.
    configured = os.environ.get('DENO_BIN')
    if configured:
        path = Path(configured)
        if path.exists():
            _deno_version(path)
            return path

    found = shutil.which('deno')
    if found:
        path = Path(found)
        _deno_version(path)
        return path

    if DENO_EXE.exists():
        try:
            _deno_version(DENO_EXE)
            return DENO_EXE
        except Exception:
            DENO_EXE.unlink(missing_ok=True)

    DENO_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DENO_DIR / 'deno.zip'
    url = _deno_download_url()

    with httpx.stream(
        'GET',
        url,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=float(DENO_DOWNLOAD_TIMEOUT)),
        headers={'User-Agent': 'YouTube-MCP/1.0'},
    ) as response:
        response.raise_for_status()
        with zip_path.open('wb') as f:
            for chunk in response.iter_bytes(1024 * 1024):
                f.write(chunk)

    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = {Path(name).name: name for name in archive.namelist()}
            member = members.get('deno')
            if not member:
                raise RuntimeError('Downloaded Deno archive did not contain the deno executable.')
            with archive.open(member) as src, DENO_EXE.open('wb') as dst:
                shutil.copyfileobj(src, dst)
        DENO_EXE.chmod(0o755)
    finally:
        zip_path.unlink(missing_ok=True)

    _deno_version(DENO_EXE)
    return DENO_EXE

def _youtube_proxy_url() -> str | None:
    # A complete YOUTUBE_PROXY URL overrides the split variables.
    if YOUTUBE_PROXY:
        return YOUTUBE_PROXY.strip() or None

    if not YOUTUBE_PROXY_HOST:
        return None

    if not YOUTUBE_PROXY_PORT:
        raise RuntimeError('YOUTUBE_PROXY_PORT is required when YOUTUBE_PROXY_HOST is set.')

    allowed_schemes = {'http', 'https', 'socks4', 'socks5', 'socks5h'}
    if YOUTUBE_PROXY_SCHEME not in allowed_schemes:
        raise RuntimeError(
            'YOUTUBE_PROXY_SCHEME must be one of: ' + ', '.join(sorted(allowed_schemes))
        )

    auth = ''
    if YOUTUBE_PROXY_USERNAME is not None:
        user = quote(YOUTUBE_PROXY_USERNAME, safe='')
        password = quote(YOUTUBE_PROXY_PASSWORD or '', safe='')
        auth = f'{user}:{password}@'

    return f'{YOUTUBE_PROXY_SCHEME}://{auth}{YOUTUBE_PROXY_HOST}:{YOUTUBE_PROXY_PORT}'


def _redact_proxy_secrets(text: str) -> str:
    value = str(text)
    proxy = _youtube_proxy_url()
    if proxy:
        value = value.replace(proxy, '[YOUTUBE_PROXY_REDACTED]')
    if YOUTUBE_PROXY_PASSWORD:
        value = value.replace(YOUTUBE_PROXY_PASSWORD, '[PROXY_PASSWORD_REDACTED]')
    return value


def _youtube_ydl_options(outtmpl: str) -> dict[str, Any]:
    deno = _ensure_deno()
    options: dict[str, Any] = {
        'outtmpl': outtmpl,
        'format': 'bv*+ba/b',
        'merge_output_format': 'mp4',
        'ffmpeg_location': FFMPEG_EXE,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        # YouTube now requires an external JS runtime for player challenges.
        'js_runtimes': {'deno': {'path': str(deno)}},
        # Let yt-dlp fetch the matching EJS challenge scripts when needed.
        'remote_components': {'ejs:npm'},
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
    }

    proxy = _youtube_proxy_url()
    if proxy:
        # yt-dlp supports HTTP/HTTPS/SOCKS proxy URLs through this option.
        options['proxy'] = proxy

    cookie_file = _ensure_cookie_file()
    if cookie_file:
        options['cookiefile'] = str(cookie_file)

    return options



def _normalized_cookie_text() -> str | None:
    if not YOUTUBE_COOKIES:
        return None

    value = YOUTUBE_COOKIES.strip()

    # Render normally preserves real newlines. This fallback also supports
    # a value pasted with literal "\n" sequences instead of real line breaks.
    if '\n' not in value and '\\n' in value:
        value = value.replace('\\r\\n', '\n').replace('\\n', '\n')

    return value.strip() + '\n'


def _cookie_file_path() -> Path:
    return MEDIA_DIR / 'youtube_cookies.txt'


def _ensure_cookie_file() -> Path | None:
    cookie_text = _normalized_cookie_text()
    if not cookie_text:
        return None

    first_line = cookie_text.splitlines()[0].strip() if cookie_text.splitlines() else ''
    if first_line not in {'# Netscape HTTP Cookie File', '# HTTP Cookie File'}:
        raise RuntimeError(
            'The cookies environment variable is configured, but it does not appear '
            'to be a Netscape cookies.txt export. The first line must be '
            '"# Netscape HTTP Cookie File" or "# HTTP Cookie File".'
        )

    path = _cookie_file_path()
    current = None
    try:
        if path.exists():
            current = path.read_text(encoding='utf-8')
    except Exception:
        current = None

    if current != cookie_text:
        path.write_text(cookie_text, encoding='utf-8')

    try:
        path.chmod(0o600)
    except OSError:
        pass

    return path


def _cookie_status_public() -> dict:
    configured = bool(YOUTUBE_COOKIES)
    if not configured:
        return {
            'configured': False,
            'valid_netscape_header': False,
            'file_ready': False,
            'cookie_line_count': 0,
            'youtube_domain_present': False,
            'google_domain_present': False,
        }

    try:
        cookie_text = _normalized_cookie_text() or ''
        lines = [line for line in cookie_text.splitlines() if line.strip()]
        first_line = lines[0].strip() if lines else ''
        cookie_rows = [
            line for line in lines
            if not line.lstrip().startswith('#') and '\t' in line
        ]

        youtube_present = False
        google_present = False
        domains: set[str] = set()

        for row in cookie_rows:
            parts = row.split('\t')
            if parts:
                domain = parts[0].lstrip('.').lower()
                domains.add(domain)
                if domain == 'youtube.com' or domain.endswith('.youtube.com'):
                    youtube_present = True
                if domain == 'google.com' or domain.endswith('.google.com'):
                    google_present = True

        path = _ensure_cookie_file()

        return {
            'configured': True,
            'valid_netscape_header': first_line in {
                '# Netscape HTTP Cookie File',
                '# HTTP Cookie File',
            },
            'file_ready': bool(path and path.exists()),
            'cookie_line_count': len(cookie_rows),
            'youtube_domain_present': youtube_present,
            'google_domain_present': google_present,
            'domain_count': len(domains),
            'file_mode_octal': (
                oct(path.stat().st_mode & 0o777)
                if path and path.exists()
                else None
            ),
        }
    except Exception as exc:
        return {
            'configured': True,
            'valid_netscape_header': False,
            'file_ready': False,
            'error_type': type(exc).__name__,
            'error': _redact_proxy_secrets(str(exc)),
        }


def _proxy_public_config() -> dict:
    """Return proxy configuration without exposing credentials."""
    try:
        proxy = _youtube_proxy_url()
    except Exception as exc:
        return {
            'configured': False,
            'valid': False,
            'error': _redact_proxy_secrets(str(exc)),
        }

    if not proxy:
        return {
            'configured': False,
            'valid': True,
            'scheme': None,
            'host': None,
            'port': None,
            'username_configured': False,
            'password_configured': False,
            'source': None,
        }

    parsed = urlsplit(proxy)
    return {
        'configured': True,
        'valid': True,
        'scheme': parsed.scheme,
        'host': parsed.hostname,
        'port': parsed.port,
        'username_configured': parsed.username is not None,
        'password_configured': parsed.password is not None,
        'source': 'YOUTUBE_PROXY' if bool(YOUTUBE_PROXY) else 'split_environment_variables',
    }


def _resolve_host_public(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addresses:
            addresses.append(addr)
    return addresses


def _tcp_check(host: str, port: int, timeout: float = 10.0) -> dict:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            latency_ms = round((time.perf_counter() - started) * 1000, 1)
            return {'ok': True, 'latency_ms': latency_ms}
    except Exception as exc:
        return {
            'ok': False,
            'latency_ms': round((time.perf_counter() - started) * 1000, 1),
            'error': _redact_proxy_secrets(str(exc)),
        }


def _debug_http_get(url: str, use_proxy: bool, timeout: float = 25.0, max_body_chars: int = 5000) -> dict:
    proxy = _youtube_proxy_url() if use_proxy else None
    started = time.perf_counter()
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/140.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        with httpx.Client(
            proxy=proxy,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 15.0)),
            headers=headers,
            trust_env=False,
        ) as client:
            response = client.get(url)
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            body = response.text[:max_body_chars]
            return {
                'ok': 200 <= response.status_code < 400,
                'status_code': response.status_code,
                'elapsed_ms': elapsed_ms,
                'final_url': str(response.url),
                'content_type': response.headers.get('content-type'),
                'server': response.headers.get('server'),
                'via': response.headers.get('via'),
                'content_length_header': response.headers.get('content-length'),
                'received_bytes': len(response.content),
                'body': body,
                'used_proxy': bool(proxy),
            }
    except Exception as exc:
        return {
            'ok': False,
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
            'used_proxy': bool(proxy),
            'error_type': type(exc).__name__,
            'error': _redact_proxy_secrets(str(exc)),
        }


def _parse_cloudflare_trace(body: str) -> dict:
    result: dict[str, str] = {}
    for line in (body or '').splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip()
    return result


def _extract_ipify(body: str) -> str | None:
    try:
        data = json.loads(body)
        value = data.get('ip')
        return str(value) if value else None
    except Exception:
        value = (body or '').strip()
        try:
            ipaddress.ip_address(value)
            return value
        except Exception:
            return None


def _server_file_fingerprint() -> dict:
    try:
        path = Path(__file__).resolve()
        data = path.read_bytes()
        return {
            'path': str(path),
            'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data),
            'mtime_unix': path.stat().st_mtime,
        }
    except Exception as exc:
        return {'error': str(exc)}


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def _yt_dlp_cli_diagnostic(video_id: str, use_proxy: bool = True, timeout_seconds: int = 120) -> dict:
    _get_owned_video(video_id)
    deno = _ensure_deno()
    url = f'https://www.youtube.com/watch?v={video_id}'
    command = [
        sys.executable,
        '-m',
        'yt_dlp',
        '-v',
        '--simulate',
        '--no-playlist',
        '--socket-timeout',
        '30',
        '--js-runtimes',
        f'deno:{deno}',
        '--remote-components',
        'ejs:npm',
        '--print',
        '%(id)s | %(title)s | %(duration)s | %(extractor)s',
    ]

    proxy = _youtube_proxy_url() if use_proxy else None
    if proxy:
        command.extend(['--proxy', proxy])

    cookie_file = _ensure_cookie_file()
    if cookie_file:
        command.extend(['--cookies', str(cookie_file)])

    command.append(url)
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(30, min(int(timeout_seconds), 240)),
            env=os.environ.copy(),
        )
        combined = (result.stdout or '') + '\n' + (result.stderr or '')
        combined = _redact_proxy_secrets(combined)
        return {
            'ok': result.returncode == 0,
            'returncode': result.returncode,
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
            'used_proxy': bool(proxy),
            'deno_path': str(deno),
            'log_tail': combined[-18000:],
        }
    except Exception as exc:
        return {
            'ok': False,
            'elapsed_ms': round((time.perf_counter() - started) * 1000, 1),
            'used_proxy': bool(proxy),
            'error_type': type(exc).__name__,
            'error': _redact_proxy_secrets(str(exc)),
        }


def _diagnose_ytdlp_log(log_text: str) -> list[str]:
    text = (log_text or '').lower()
    issues: list[str] = []

    if 'no supported javascript runtime' in text or 'js challenge providers' in text and 'deno (unavailable)' in text:
        issues.append('javascript_runtime_not_detected')
    if 'sign in to confirm you’re not a bot' in text or "sign in to confirm you're not a bot" in text:
        issues.append('youtube_bot_challenge')
    if 'login_required' in text:
        issues.append('youtube_login_required')
    if 'failed to extract any player response' in text:
        issues.append('youtube_player_response_extraction_failed')
    if 'proxy authentication required' in text or '407' in text:
        issues.append('proxy_authentication_failed')
    if 'connection refused' in text:
        issues.append('connection_refused')
    if 'timed out' in text or 'timeout' in text:
        issues.append('network_timeout')
    if 'certificate verify failed' in text or 'ssl' in text and 'certificate' in text:
        issues.append('tls_or_certificate_problem')
    if 'requested format is not available' in text:
        issues.append('format_selection_problem')
    return issues


def _upload_video_file(path: Path, title: str, description: str = '', privacy_status: str = 'private', category_id: str = '22', tags: list[str] | None = None, made_for_kids: bool | None = None) -> dict:
    if privacy_status not in {'private', 'unlisted', 'public'}:
        raise ValueError('privacy_status must be private, unlisted, or public.')
    body: dict[str, Any] = {'snippet': {'title': title, 'description': description, 'categoryId': category_id}, 'status': {'privacyStatus': privacy_status}}
    if tags:
        body['snippet']['tags'] = tags
    if made_for_kids is not None:
        body['status']['selfDeclaredMadeForKids'] = made_for_kids
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
    request = _youtube().videos().insert(part='snippet,status', body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response

@mcp.tool()
def hello(name: str) -> str:
    return f'Hello, {name}!'

@mcp.tool()
def youtube_auth_status() -> dict:
    return {'client_configured': bool(YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REDIRECT_URI), 'authorized': bool(YOUTUBE_REFRESH_TOKEN), 'scopes': YOUTUBE_SCOPES}

@mcp.tool()
def youtube_my_channel() -> dict:
    result = _youtube().channels().list(part='snippet,statistics,contentDetails,status,brandingSettings', mine=True).execute()
    items = result.get('items', [])
    return {'found': bool(items), 'channel': items[0] if items else None}

@mcp.tool()
def youtube_list_videos(max_results: int = 50) -> dict:
    max_results = max(1, min(int(max_results), 50))
    playlist_id = _my_uploads_playlist_id()
    playlist_result = _youtube().playlistItems().list(part='contentDetails,snippet,status', playlistId=playlist_id, maxResults=max_results).execute()
    video_ids = [item['contentDetails']['videoId'] for item in playlist_result.get('items', [])]
    if not video_ids:
        return {'count': 0, 'videos': []}
    videos_result = _youtube().videos().list(part='snippet,contentDetails,status,statistics,liveStreamingDetails,paidProductPlacementDetails', id=','.join(video_ids)).execute()
    videos = videos_result.get('items', [])
    for video in videos:
        video['watchUrl'] = f"https://www.youtube.com/watch?v={video['id']}"
    return {'count': len(videos), 'videos': videos}

@mcp.tool()
def youtube_get_video(video_id: str) -> dict:
    result = _youtube().videos().list(part='snippet,contentDetails,status,statistics,player,recordingDetails,liveStreamingDetails,paidProductPlacementDetails', id=video_id).execute()
    items = result.get('items', [])
    if not items:
        return {'found': False, 'video_id': video_id}
    video = items[0]
    video['watchUrl'] = f'https://www.youtube.com/watch?v={video_id}'
    return {'found': True, 'video': video}

@mcp.tool()
def youtube_update_video(video_id: str, title: str | None = None, description: str | None = None, tags: list[str] | None = None, category_id: str | None = None, privacy_status: str | None = None, publish_at: str | None = None, made_for_kids: bool | None = None, contains_synthetic_media: bool | None = None) -> dict:
    existing = _get_owned_video(video_id, part='snippet,status')
    body: dict[str, Any] = {'id': video_id}
    parts: list[str] = []
    if any(v is not None for v in [title, description, tags, category_id]):
        snippet = existing['snippet']
        new_snippet: dict[str, Any] = {'title': snippet['title'], 'description': snippet.get('description', ''), 'categoryId': snippet['categoryId']}
        if 'tags' in snippet:
            new_snippet['tags'] = snippet['tags']
        if title is not None: new_snippet['title'] = title
        if description is not None: new_snippet['description'] = description
        if tags is not None: new_snippet['tags'] = tags
        if category_id is not None: new_snippet['categoryId'] = category_id
        body['snippet'] = new_snippet
        parts.append('snippet')
    if any(v is not None for v in [privacy_status, publish_at, made_for_kids, contains_synthetic_media]):
        old_status = existing.get('status', {})
        new_status: dict[str, Any] = {}
        for field in ['privacyStatus', 'embeddable', 'license', 'publicStatsViewable', 'selfDeclaredMadeForKids', 'containsSyntheticMedia']:
            if field in old_status:
                new_status[field] = old_status[field]
        if privacy_status is not None:
            if privacy_status not in {'private', 'unlisted', 'public'}:
                raise ValueError('privacy_status must be private, unlisted, or public.')
            new_status['privacyStatus'] = privacy_status
        if publish_at is not None: new_status['publishAt'] = publish_at
        if made_for_kids is not None: new_status['selfDeclaredMadeForKids'] = made_for_kids
        if contains_synthetic_media is not None: new_status['containsSyntheticMedia'] = contains_synthetic_media
        body['status'] = new_status
        parts.append('status')
    if not parts:
        return {'updated': False, 'message': 'No fields were supplied.'}
    response = _youtube().videos().update(part=','.join(parts), body=body).execute()
    return {'updated': True, 'video': response}

@mcp.tool()
def youtube_delete_video(video_id: str, confirm: bool = False) -> dict:
    _get_owned_video(video_id)
    if not confirm:
        return {'deleted': False, 'requires_confirmation': True, 'message': 'Call again with confirm=true to permanently delete.'}
    _youtube().videos().delete(id=video_id).execute()
    return {'deleted': True, 'video_id': video_id}

@mcp.tool()
def youtube_rate_video(video_id: str, rating: str) -> dict:
    if rating not in {'like', 'dislike', 'none'}:
        raise ValueError('rating must be like, dislike, or none.')
    _youtube().videos().rate(id=video_id, rating=rating).execute()
    return {'video_id': video_id, 'rating': rating}

@mcp.tool()
def youtube_upload_video_from_url(source_url: str, title: str, description: str = '', privacy_status: str = 'private', category_id: str = '22', tags: list[str] | None = None, made_for_kids: bool | None = None) -> dict:
    suffix = Path(source_url.split('?')[0]).suffix or '.mp4'
    path = MEDIA_DIR / f'upload_{uuid.uuid4().hex}{suffix}'
    try:
        _download_url(source_url, path)
        result = _upload_video_file(path, title, description, privacy_status, category_id, tags, made_for_kids)
        return {'uploaded': True, 'video': result, 'watch_url': f"https://www.youtube.com/watch?v={result['id']}" if result.get('id') else None}
    finally:
        path.unlink(missing_ok=True)

@mcp.tool()
def youtube_set_thumbnail_from_url(video_id: str, image_url: str) -> dict:
    _get_owned_video(video_id)
    suffix = Path(image_url.split('?')[0]).suffix or '.jpg'
    path = MEDIA_DIR / f'thumb_{uuid.uuid4().hex}{suffix}'
    try:
        _download_url(image_url, path, max_bytes=10 * 1024 * 1024)
        result = _youtube().thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(path), resumable=False)).execute()
        return {'updated': True, 'result': result}
    finally:
        path.unlink(missing_ok=True)

@mcp.tool()
def youtube_list_playlists(max_results: int = 50) -> dict:
    return _youtube().playlists().list(part='snippet,status,contentDetails', mine=True, maxResults=max(1, min(int(max_results), 50))).execute()

@mcp.tool()
def youtube_create_playlist(title: str, description: str = '', privacy_status: str = 'private') -> dict:
    if privacy_status not in {'private', 'unlisted', 'public'}:
        raise ValueError('Invalid privacy_status.')
    return _youtube().playlists().insert(part='snippet,status', body={'snippet': {'title': title, 'description': description}, 'status': {'privacyStatus': privacy_status}}).execute()

@mcp.tool()
def youtube_add_video_to_playlist(playlist_id: str, video_id: str, position: int | None = None) -> dict:
    snippet: dict[str, Any] = {'playlistId': playlist_id, 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}
    if position is not None:
        snippet['position'] = int(position)
    return _youtube().playlistItems().insert(part='snippet', body={'snippet': snippet}).execute()

@mcp.tool()
def youtube_remove_playlist_item(playlist_item_id: str, confirm: bool = False) -> dict:
    if not confirm:
        return {'removed': False, 'requires_confirmation': True}
    _youtube().playlistItems().delete(id=playlist_item_id).execute()
    return {'removed': True, 'playlist_item_id': playlist_item_id}

@mcp.tool()
def youtube_list_comments(video_id: str, max_results: int = 50, order: str = 'time') -> dict:
    return _youtube().commentThreads().list(part='snippet,replies', videoId=video_id, maxResults=max(1, min(int(max_results), 100)), order=order, textFormat='plainText').execute()

@mcp.tool()
def youtube_reply_to_comment(parent_comment_id: str, text: str) -> dict:
    return _youtube().comments().insert(part='snippet', body={'snippet': {'parentId': parent_comment_id, 'textOriginal': text}}).execute()

@mcp.tool()
def youtube_moderate_comment(comment_id: str, moderation_status: str, ban_author: bool = False, confirm: bool = False) -> dict:
    if moderation_status not in {'heldForReview', 'published', 'rejected'}:
        raise ValueError('Invalid moderation_status.')
    if not confirm:
        return {'changed': False, 'requires_confirmation': True}
    _youtube().comments().setModerationStatus(id=comment_id, moderationStatus=moderation_status, banAuthor=ban_author).execute()
    return {'changed': True, 'comment_id': comment_id, 'moderation_status': moderation_status, 'ban_author': ban_author}

@mcp.tool()
def youtube_delete_comment(comment_id: str, confirm: bool = False) -> dict:
    if not confirm:
        return {'deleted': False, 'requires_confirmation': True}
    _youtube().comments().delete(id=comment_id).execute()
    return {'deleted': True, 'comment_id': comment_id}

@mcp.tool()
def youtube_list_captions(video_id: str) -> dict:
    return _youtube().captions().list(part='snippet', videoId=video_id).execute()

@mcp.tool()
def youtube_download_caption(caption_id: str, file_format: str = 'srt') -> dict:
    allowed = {'srt', 'vtt', 'ttml', 'sbv'}
    if file_format not in allowed:
        raise ValueError(f'file_format must be one of {sorted(allowed)}')
    path = MEDIA_DIR / f'caption_{uuid.uuid4().hex}.{file_format}'
    request = _youtube().captions().download(id=caption_id, tfmt=file_format)
    with path.open('wb') as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return _publish_media(path)

@mcp.tool()
def youtube_upload_caption_from_url(video_id: str, source_url: str, language: str, name: str = '', is_draft: bool = False) -> dict:
    _get_owned_video(video_id)
    suffix = Path(source_url.split('?')[0]).suffix or '.srt'
    path = MEDIA_DIR / f'caption_upload_{uuid.uuid4().hex}{suffix}'
    try:
        _download_url(source_url, path, max_bytes=20 * 1024 * 1024)
        body = {'snippet': {'videoId': video_id, 'language': language, 'name': name, 'isDraft': is_draft}}
        return _youtube().captions().insert(part='snippet', body=body, media_body=MediaFileUpload(str(path), resumable=False)).execute()
    finally:
        path.unlink(missing_ok=True)

@mcp.tool()
def youtube_list_subscriptions(max_results: int = 50) -> dict:
    return _youtube().subscriptions().list(part='snippet,contentDetails', mine=True, maxResults=max(1, min(int(max_results), 50))).execute()

@mcp.tool()
def youtube_subscribe(channel_id: str) -> dict:
    return _youtube().subscriptions().insert(part='snippet', body={'snippet': {'resourceId': {'kind': 'youtube#channel', 'channelId': channel_id}}}).execute()

@mcp.tool()
def youtube_unsubscribe(subscription_id: str, confirm: bool = False) -> dict:
    if not confirm:
        return {'deleted': False, 'requires_confirmation': True}
    _youtube().subscriptions().delete(id=subscription_id).execute()
    return {'deleted': True, 'subscription_id': subscription_id}

@mcp.tool()
def youtube_search(query: str, resource_type: str = 'video', max_results: int = 20) -> dict:
    if resource_type not in {'video', 'channel', 'playlist'}:
        raise ValueError('resource_type must be video, channel, or playlist.')
    return _youtube().search().list(part='snippet', q=query, type=resource_type, maxResults=max(1, min(int(max_results), 50))).execute()

@mcp.tool()
def youtube_analytics_report(start_date: str, end_date: str, metrics: str = 'views,estimatedMinutesWatched,averageViewDuration', dimensions: str | None = None, filters: str | None = None, sort: str | None = None, max_results: int = 200) -> dict:
    kwargs: dict[str, Any] = {'ids': 'channel==MINE', 'startDate': start_date, 'endDate': end_date, 'metrics': metrics, 'maxResults': max(1, min(int(max_results), 200))}
    if dimensions: kwargs['dimensions'] = dimensions
    if filters: kwargs['filters'] = filters
    if sort: kwargs['sort'] = sort
    return _youtube_analytics().reports().query(**kwargs).execute()

GENERIC_METHODS: dict[str, set[str]] = {
    'activities': {'list'},
    'channels': {'list', 'update'},
    'channelSections': {'list', 'insert', 'update', 'delete'},
    'commentThreads': {'list', 'insert'},
    'comments': {'list', 'insert', 'update', 'delete', 'setModerationStatus', 'markAsSpam'},
    'i18nLanguages': {'list'},
    'i18nRegions': {'list'},
    'playlistItems': {'list', 'insert', 'update', 'delete'},
    'playlists': {'list', 'insert', 'update', 'delete'},
    'search': {'list'},
    'subscriptions': {'list', 'insert', 'delete'},
    'videoCategories': {'list'},
    'videos': {'list', 'update', 'delete', 'rate', 'getRating', 'reportAbuse'},
    'liveBroadcasts': {'list', 'insert', 'update', 'delete', 'bind', 'transition'},
    'liveStreams': {'list', 'insert', 'update', 'delete'},
    'liveChatMessages': {'list', 'insert', 'delete'},
    'liveChatModerators': {'list', 'insert', 'delete'},
    'liveChatBans': {'insert', 'delete'},
}
READ_ONLY_GENERIC_METHODS = {'list', 'getRating'}

@mcp.tool()
def youtube_api_call(resource: str, method: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, confirm: bool = False) -> dict:
    allowed = GENERIC_METHODS.get(resource)
    if not allowed or method not in allowed:
        raise ValueError(f'Unsupported resource/method. Allowed methods for {resource}: {sorted(allowed) if allowed else "none"}')
    if method not in READ_ONLY_GENERIC_METHODS and not confirm:
        return {'executed': False, 'requires_confirmation': True, 'resource': resource, 'method': method}
    resource_obj = getattr(_youtube(), resource)()
    method_fn = getattr(resource_obj, method)
    kwargs = dict(params or {})
    if body is not None:
        kwargs['body'] = body
    result = method_fn(**kwargs).execute()
    return {'executed': True, 'resource': resource, 'method': method, 'result': result}



def _debug_proxy_tcp_impl(timeout_seconds: float = 10.0) -> dict:
    config = _proxy_public_config()
    if not config.get('configured'):
        return {'ok': False, 'proxy': config, 'error': 'No YouTube proxy is configured.'}
    host = config.get('host')
    port = config.get('port')
    if not host or not port:
        return {'ok': False, 'proxy': config, 'error': 'Proxy host or port is missing.'}
    return {
        'proxy': config,
        'tcp': _tcp_check(str(host), int(port), max(1.0, min(float(timeout_seconds), 30.0))),
    }


def _debug_exit_ip_impl(use_proxy: bool = True) -> dict:
    ipify = _debug_http_get('https://api.ipify.org?format=json', use_proxy=use_proxy, max_body_chars=1000)
    cloudflare = _debug_http_get('https://www.cloudflare.com/cdn-cgi/trace', use_proxy=use_proxy, max_body_chars=4000)
    ipify_ip = _extract_ipify(ipify.get('body', '')) if ipify.get('ok') else None
    cf_trace = _parse_cloudflare_trace(cloudflare.get('body', '')) if cloudflare.get('ok') else {}
    ipify.pop('body', None)
    cloudflare.pop('body', None)
    return {
        'used_proxy': use_proxy,
        'proxy_config': _proxy_public_config(),
        'ipify': {**ipify, 'ip': ipify_ip},
        'cloudflare': {
            **cloudflare,
            'ip': cf_trace.get('ip'),
            'colo': cf_trace.get('colo'),
            'loc': cf_trace.get('loc'),
            'tls': cf_trace.get('tls'),
            'http': cf_trace.get('http'),
        },
    }


def _debug_compare_exit_ips_impl() -> dict:
    direct = _debug_exit_ip_impl(use_proxy=False)
    proxied = _debug_exit_ip_impl(use_proxy=True)
    direct_ip = direct.get('ipify', {}).get('ip') or direct.get('cloudflare', {}).get('ip')
    proxy_ip = proxied.get('ipify', {}).get('ip') or proxied.get('cloudflare', {}).get('ip')
    return {
        'direct': direct,
        'proxied': proxied,
        'direct_ip': direct_ip,
        'proxy_ip': proxy_ip,
        'different_exit_ip': bool(direct_ip and proxy_ip and direct_ip != proxy_ip),
        'proxy_verified': bool(proxy_ip and direct_ip and proxy_ip != direct_ip),
    }


def _debug_proxy_http_impl() -> dict:
    tests = {
        'google_generate_204': _debug_http_get('https://www.google.com/generate_204', use_proxy=True, max_body_chars=500),
        'youtube_generate_204': _debug_http_get('https://www.youtube.com/generate_204', use_proxy=True, max_body_chars=500),
        'youtube_thumbnail_cdn': _debug_http_get('https://i.ytimg.com/', use_proxy=True, max_body_chars=500),
        'github': _debug_http_get('https://github.com/', use_proxy=True, max_body_chars=500),
        'npm_registry': _debug_http_get('https://registry.npmjs.org/yt-dlp-ejs', use_proxy=True, max_body_chars=500),
    }
    for result in tests.values():
        result.pop('body', None)
    return {
        'proxy': _proxy_public_config(),
        'tests': tests,
        'all_ok': all(v.get('ok') for v in tests.values()),
    }


def _debug_youtube_watch_page_impl(video_id: str, use_proxy: bool = True) -> dict:
    _get_owned_video(video_id)
    url = f'https://www.youtube.com/watch?v={video_id}'
    result = _debug_http_get(url, use_proxy=use_proxy, max_body_chars=120000)
    body = result.pop('body', '') or ''
    lower = body.lower()
    title_match = re.search(r'<title>(.*?)</title>', body, flags=re.I | re.S)
    js_match = re.search(r'(?:"jsUrl"|"PLAYER_JS_URL")\s*:\s*"([^"]+base\.js[^"]*)"', body)
    if not js_match:
        js_match = re.search(r'(/s/player/[^"\']+/base\.js)', body)
    player_js_url = None
    if js_match:
        candidate = js_match.group(1).replace('\\u0026', '&').replace('\\/', '/')
        if candidate.startswith('//'):
            player_js_url = 'https:' + candidate
        elif candidate.startswith('/'):
            player_js_url = 'https://www.youtube.com' + candidate
        elif candidate.startswith('http://') or candidate.startswith('https://'):
            player_js_url = candidate

    return {
        **result,
        'video_id': video_id,
        'used_proxy': use_proxy,
        'page_title': html.unescape(title_match.group(1).strip()) if title_match else None,
        'contains_yt_initial_player_response': 'ytinitialplayerresponse' in lower,
        'contains_player_response_text': 'playerresponse' in lower,
        'contains_playability_status': 'playabilitystatus' in lower,
        'contains_login_required': 'login_required' in lower,
        'contains_bot_challenge': (
            'sign in to confirm you’re not a bot' in lower
            or "sign in to confirm you're not a bot" in lower
        ),
        'contains_consent_redirect': 'consent.youtube.com' in lower,
        'contains_recaptcha': 'recaptcha' in lower or 'g-recaptcha' in lower,
        'player_js_url_found': bool(player_js_url),
        'player_js_url': player_js_url,
        'html_sample': _redact_proxy_secrets(body[:1000]),
    }


def _debug_ytdlp_verbose_impl(video_id: str, use_proxy: bool = True, timeout_seconds: int = 120) -> dict:
    result = _yt_dlp_cli_diagnostic(video_id, use_proxy=use_proxy, timeout_seconds=timeout_seconds)
    result['detected_issues'] = _diagnose_ytdlp_log(result.get('log_tail', ''))
    return result


@mcp.tool()
def debug_build_info() -> dict:
    """Identify the exact server build running on Render."""
    return {
        'server_build': SERVER_BUILD,
        'server_file': _server_file_fingerprint(),
        'render_hostname': RENDER_EXTERNAL_HOSTNAME,
        'python_version': sys.version,
        'platform': platform.platform(),
    }


@mcp.tool()
def debug_config_status() -> dict:
    """Sanitized configuration report. Never returns secrets."""
    return {
        'server_build': SERVER_BUILD,
        'mcp_api_token_configured': bool(MCP_API_TOKEN),
        'youtube_client_id_configured': bool(YOUTUBE_CLIENT_ID),
        'youtube_client_secret_configured': bool(YOUTUBE_CLIENT_SECRET),
        'youtube_redirect_uri_configured': bool(YOUTUBE_REDIRECT_URI),
        'youtube_refresh_token_configured': bool(YOUTUBE_REFRESH_TOKEN),
        'youtube_cookies': _cookie_status_public(),
        'youtube_proxy': _proxy_public_config(),
        'media_dir': str(MEDIA_DIR),
        'media_url_ttl_seconds': MEDIA_URL_TTL_SECONDS,
        'max_remote_file_bytes': MAX_REMOTE_FILE_BYTES,
        'render_hostname': RENDER_EXTERNAL_HOSTNAME,
    }


@mcp.tool()
def debug_runtime_status(install_deno_if_missing: bool = True) -> dict:
    """Check Python, yt-dlp/EJS, Deno, FFmpeg, disk and optional networking libraries."""
    deno_path = None
    deno_version = None
    deno_error = None
    if install_deno_if_missing:
        try:
            deno = _ensure_deno()
            deno_path = str(deno)
            deno_version = _deno_version(deno)
        except Exception as exc:
            deno_error = _redact_proxy_secrets(str(exc))

    ffmpeg_result = subprocess.run(
        [FFMPEG_EXE, '-version'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    disk = shutil.disk_usage(MEDIA_DIR)
    return {
        'server_build': SERVER_BUILD,
        'python_version': sys.version,
        'platform': platform.platform(),
        'yt_dlp_version': _package_version('yt-dlp'),
        'yt_dlp_ejs_version': _package_version('yt-dlp-ejs'),
        'curl_cffi_version': _package_version('curl-cffi'),
        'httpx_version': _package_version('httpx'),
        'certifi_version': _package_version('certifi'),
        'youtube_cookies': _cookie_status_public(),
        'deno_path': deno_path,
        'deno_version': deno_version,
        'deno_error': deno_error,
        'ffmpeg_path': FFMPEG_EXE,
        'ffmpeg_ok': ffmpeg_result.returncode == 0,
        'ffmpeg_version_line': (ffmpeg_result.stdout or ffmpeg_result.stderr).splitlines()[0] if (ffmpeg_result.stdout or ffmpeg_result.stderr) else None,
        'disk_total_bytes': disk.total,
        'disk_free_bytes': disk.free,
        'disk_used_bytes': disk.used,
    }



@mcp.tool()
def debug_cookie_status() -> dict:
    """Check whether YouTube cookies are configured and structurally usable without exposing values."""
    return {
        'server_build': SERVER_BUILD,
        'cookies_environment_name': (
            'cookies'
            if os.environ.get('cookies')
            else ('YOUTUBE_COOKIES' if os.environ.get('YOUTUBE_COOKIES') else None)
        ),
        'cookies': _cookie_status_public(),
    }


@mcp.tool()
def debug_proxy_dns() -> dict:
    """Resolve the configured proxy hostname without exposing credentials."""
    config = _proxy_public_config()
    if not config.get('configured'):
        return {'ok': False, 'proxy': config, 'error': 'No YouTube proxy is configured.'}
    host = config.get('host')
    try:
        addresses = _resolve_host_public(str(host))
        return {'ok': True, 'proxy': config, 'resolved_addresses': addresses}
    except Exception as exc:
        return {
            'ok': False,
            'proxy': config,
            'error_type': type(exc).__name__,
            'error': _redact_proxy_secrets(str(exc)),
        }


@mcp.tool()
def debug_proxy_tcp(timeout_seconds: float = 10.0) -> dict:
    """Test raw TCP reachability from Render to the configured proxy endpoint."""
    return _debug_proxy_tcp_impl(timeout_seconds)


@mcp.tool()
def debug_exit_ip(use_proxy: bool = True) -> dict:
    """Return the public exit IP seen by fixed public IP-echo services."""
    return _debug_exit_ip_impl(use_proxy)


@mcp.tool()
def debug_compare_exit_ips() -> dict:
    """Compare Render's normal public IP with the residential-proxy exit IP."""
    return _debug_compare_exit_ips_impl()


@mcp.tool()
def debug_proxy_http() -> dict:
    """Test HTTPS tunneling/authentication through the configured proxy."""
    return _debug_proxy_http_impl()


@mcp.tool()
def debug_youtube_watch_page(video_id: str, use_proxy: bool = True) -> dict:
    """Fetch the owned video's watch page and detect bot/login/player-response markers."""
    return _debug_youtube_watch_page_impl(video_id, use_proxy)


@mcp.tool()
def debug_ytdlp_verbose(video_id: str, use_proxy: bool = True, timeout_seconds: int = 120) -> dict:
    """Run yt-dlp in verbose simulate mode and return a sanitized diagnostic log."""
    return _debug_ytdlp_verbose_impl(video_id, use_proxy, timeout_seconds)


@mcp.tool()
def debug_ffmpeg() -> dict:
    """Verify FFmpeg execution independently of YouTube."""
    try:
        version = subprocess.run(
            [FFMPEG_EXE, '-version'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        filters = subprocess.run(
            [FFMPEG_EXE, '-hide_banner', '-filters'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            'ok': version.returncode == 0,
            'ffmpeg_path': FFMPEG_EXE,
            'version_line': (version.stdout or version.stderr).splitlines()[0] if (version.stdout or version.stderr) else None,
            'filters_command_ok': filters.returncode == 0,
        }
    except Exception as exc:
        return {'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)}


@mcp.tool()
def debug_temp_media() -> dict:
    """Inspect temporary media state without exposing file contents."""
    _cleanup_expired_media()
    files = []
    total = 0
    for path in sorted(MEDIA_DIR.rglob('*')):
        try:
            if path.is_file():
                size = path.stat().st_size
                total += size
                files.append({
                    'relative_path': str(path.relative_to(MEDIA_DIR)),
                    'size_bytes': size,
                    'mtime_unix': path.stat().st_mtime,
                })
        except OSError:
            pass
    return {
        'media_dir': str(MEDIA_DIR),
        'file_count': len(files),
        'total_bytes': total,
        'files': files[:100],
        'active_media_tokens': len(MEDIA_TOKENS),
    }


@mcp.tool()
def debug_read_text_media(media_id: str, max_chars: int = 10000) -> dict:
    """Read a temporary text response created by media_fetch_source_url."""
    path = _resolve_media(media_id)
    max_chars = max(100, min(int(max_chars), 50000))
    raw = path.read_bytes()[:max_chars * 4]
    text_value = raw.decode('utf-8', errors='replace')[:max_chars]
    return {
        'media_id': media_id,
        'filename': path.name,
        'size_bytes': path.stat().st_size,
        'text': _redact_proxy_secrets(text_value),
        'truncated': path.stat().st_size > len(raw),
    }


@mcp.tool()
def debug_full_youtube_pipeline(video_id: str) -> dict:
    """Run a focused end-to-end diagnostic and identify the most likely failure layer."""
    config = _proxy_public_config()
    cookie_status = _cookie_status_public()
    tcp = _debug_proxy_tcp_impl() if config.get('configured') else {'ok': False, 'error': 'proxy_not_configured'}
    exits = _debug_compare_exit_ips_impl() if config.get('configured') else {'proxy_verified': False}
    proxy_http = _debug_proxy_http_impl() if config.get('configured') else {'all_ok': False}
    watch = _debug_youtube_watch_page_impl(video_id, use_proxy=bool(config.get('configured')))
    ytdlp = _debug_ytdlp_verbose_impl(video_id, use_proxy=bool(config.get('configured')), timeout_seconds=120)

    likely_issue = 'unknown'
    recommendations: list[str] = []

    if not config.get('configured'):
        likely_issue = 'proxy_not_configured'
        recommendations.append('Set the YOUTUBE_PROXY_* environment variables and redeploy.')
    elif not tcp.get('tcp', {}).get('ok'):
        likely_issue = 'proxy_endpoint_unreachable'
        recommendations.append('Check proxy host, port, provider allowlist, and whether Render connections are permitted.')
    elif not exits.get('proxy_verified'):
        likely_issue = 'proxy_exit_ip_not_verified'
        recommendations.append('Check proxy authentication/configuration; proxied traffic is not showing a distinct exit IP.')
    elif not proxy_http.get('tests', {}).get('youtube_generate_204', {}).get('ok'):
        likely_issue = 'proxy_cannot_reach_youtube'
        recommendations.append('The proxy works at TCP level but cannot successfully tunnel HTTPS traffic to YouTube.')
    elif (
        ('youtube_bot_challenge' in ytdlp.get('detected_issues', [])
         or 'youtube_login_required' in ytdlp.get('detected_issues', []))
        and not cookie_status.get('file_ready')
    ):
        likely_issue = 'youtube_cookies_missing_or_invalid'
        recommendations.append(
            'YouTube requires an authenticated session. Configure the lowercase Render environment variable '
            '"cookies" with a valid Netscape cookies.txt export, then redeploy.'
        )
    elif watch.get('contains_bot_challenge') or 'youtube_bot_challenge' in ytdlp.get('detected_issues', []):
        likely_issue = 'youtube_bot_challenge_even_with_cookies'
        recommendations.append(
            'The proxy works and cookies are present, but YouTube still challenges the session. '
            'The exported cookies may be stale, incomplete, or tied to a different browser/IP session.'
        )
    elif watch.get('contains_login_required') or 'youtube_login_required' in ytdlp.get('detected_issues', []):
        likely_issue = 'youtube_login_required'
        recommendations.append('YouTube is requiring an authenticated browser session for this request.')
    elif 'javascript_runtime_not_detected' in ytdlp.get('detected_issues', []):
        likely_issue = 'deno_or_ejs_not_detected'
        recommendations.append('Fix Deno/EJS discovery before retrying the downloader.')
    elif 'youtube_player_response_extraction_failed' in ytdlp.get('detected_issues', []):
        likely_issue = 'yt_dlp_player_response_failure'
        recommendations.append('Network/proxy reachability works, but yt-dlp still cannot obtain a usable YouTube player response. Inspect the verbose log for client/playability status.')
    elif ytdlp.get('ok'):
        likely_issue = 'diagnostics_passed'
        recommendations.append('yt-dlp metadata extraction works; retry video_download_my_video.')
    else:
        recommendations.append('Inspect the verbose yt-dlp log and HTTP diagnostics returned here.')

    return {
        'server_build': SERVER_BUILD,
        'likely_issue': likely_issue,
        'recommendations': recommendations,
        'proxy_config': config,
        'cookie_status': cookie_status,
        'proxy_tcp': tcp,
        'exit_ip_check': exits,
        'proxy_http': proxy_http,
        'youtube_watch_page': watch,
        'ytdlp': ytdlp,
    }



@mcp.tool()
def media_runtime_status(install_deno_if_missing: bool = False) -> dict:
    deno_path = shutil.which('deno')
    deno_version = None
    if install_deno_if_missing:
        deno = _ensure_deno()
        deno_path = str(deno)
        deno_version = _deno_version(deno)
    elif deno_path:
        try:
            deno_version = _deno_version(Path(deno_path))
        except Exception as exc:
            deno_version = f'error: {exc}'
    elif DENO_EXE.exists():
        try:
            deno_path = str(DENO_EXE)
            deno_version = _deno_version(DENO_EXE)
        except Exception as exc:
            deno_version = f'error: {exc}'
    proxy = _youtube_proxy_url()
    return {
        'ffmpeg': FFMPEG_EXE,
        'deno_path': deno_path,
        'deno_version': deno_version,
        'yt_dlp_version': getattr(getattr(yt_dlp, 'version', None), '__version__', 'unknown'),
        'proxy_configured': bool(proxy),
        'proxy_scheme': YOUTUBE_PROXY_SCHEME if proxy else None,
        'proxy_host': YOUTUBE_PROXY_HOST if (proxy and not YOUTUBE_PROXY) else None,
        'note': 'Call with install_deno_if_missing=true to install/test Deno on Render.'
    }

@mcp.tool()
def video_download_my_video(video_id: str) -> dict:
    video = _get_owned_video(video_id)
    title = video['snippet']['title']
    base = MEDIA_DIR / f'yt_{video_id}_{uuid.uuid4().hex}'
    outtmpl = str(base) + '.%(ext)s'
    url = f'https://www.youtube.com/watch?v={video_id}'
    try:
        ydl_opts = _youtube_ydl_options(outtmpl)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        candidates = [
            p for p in MEDIA_DIR.glob(base.name + '.*')
            if p.is_file() and not p.name.endswith(('.part', '.ytdl', '.temp'))
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError('yt-dlp finished but no final output file was found.')

        path = candidates[0]
        published = _publish_media(path)
        published.update({
            'video_id': video_id,
            'title': title,
            'source_watch_url': url,
            'duration_seconds': info.get('duration'),
            'extractor': info.get('extractor_key') or info.get('extractor'),
        })
        return published
    except Exception as exc:
        details = _redact_proxy_secrets(str(exc))
        raise RuntimeError(
            'Could not download this owned YouTube video. Public/unlisted videos should work with the Deno/EJS runtime and configured proxy. '
            'Private or otherwise restricted videos may still require the original source file from Drive/object storage. '
            'Details: ' + details
        ) from exc

@mcp.tool()
def media_fetch_source_url(source_url: str, filename: str = 'source.bin') -> dict:
    filename = _safe_filename(filename)
    path = MEDIA_DIR / f'{uuid.uuid4().hex}_{filename}'
    _download_url(source_url, path)
    return _publish_media(path)

@mcp.tool()
def video_probe(media_id: str) -> dict:
    path = _resolve_media(media_id)
    result = subprocess.run([FFMPEG_EXE, '-hide_banner', '-i', str(path)], capture_output=True, text=True, timeout=60)
    return {'media_id': media_id, 'filename': path.name, 'size_bytes': path.stat().st_size, 'ffmpeg_info': (result.stderr or '')[-12000:]}

@mcp.tool()
def video_extract_frames(media_id: str, interval_seconds: float = 5.0, max_frames: int = 12) -> dict:
    if interval_seconds <= 0:
        raise ValueError('interval_seconds must be > 0.')
    max_frames = max(1, min(int(max_frames), 50))
    source = _resolve_media(media_id)
    job_dir = MEDIA_DIR / f'frames_{uuid.uuid4().hex}'
    job_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = job_dir / 'frame_%03d.jpg'
    _run_ffmpeg(['-y', '-i', str(source), '-vf', f'fps=1/{float(interval_seconds)}', '-frames:v', str(max_frames), '-q:v', '2', str(output_pattern)], timeout=600)
    frames = [_publish_media(path) for path in sorted(job_dir.glob('frame_*.jpg'))]
    return {'source_media_id': media_id, 'interval_seconds': interval_seconds, 'count': len(frames), 'frames': frames}

@mcp.tool()
def video_extract_audio(media_id: str, audio_format: str = 'mp3') -> dict:
    source = _resolve_media(media_id)
    if audio_format not in {'mp3', 'wav', 'm4a'}:
        raise ValueError('audio_format must be mp3, wav, or m4a.')
    output = MEDIA_DIR / f'audio_{uuid.uuid4().hex}.{audio_format}'
    if audio_format == 'mp3':
        codec_args = ['-codec:a', 'libmp3lame', '-q:a', '3']
    elif audio_format == 'wav':
        codec_args = ['-codec:a', 'pcm_s16le']
    else:
        codec_args = ['-codec:a', 'aac', '-b:a', '160k']
    _run_ffmpeg(['-y', '-i', str(source), '-vn', *codec_args, str(output)], timeout=600)
    return _publish_media(output)

@mcp.tool()
def video_get_clip(media_id: str, start_seconds: float, duration_seconds: float) -> dict:
    if start_seconds < 0:
        raise ValueError('start_seconds must be >= 0.')
    if duration_seconds <= 0 or duration_seconds > 300:
        raise ValueError('duration_seconds must be between 0 and 300.')
    source = _resolve_media(media_id)
    output = MEDIA_DIR / f'clip_{uuid.uuid4().hex}.mp4'
    _run_ffmpeg(['-y', '-ss', str(float(start_seconds)), '-i', str(source), '-t', str(float(duration_seconds)), '-map', '0:v?', '-map', '0:a?', '-c', 'copy', str(output)], timeout=600)
    result = _publish_media(output)
    result.update({'start_seconds': start_seconds, 'duration_seconds': duration_seconds})
    return result

@mcp.tool()
def media_cleanup(confirm: bool = False) -> dict:
    if not confirm:
        return {'cleaned': False, 'requires_confirmation': True}
    count = 0
    for path in list(MEDIA_DIR.glob('*')):
        try:
            if path.is_dir(): shutil.rmtree(path, ignore_errors=True)
            else: path.unlink(missing_ok=True)
            count += 1
        except OSError:
            pass
    MEDIA_TOKENS.clear()
    return {'cleaned': True, 'items_removed': count}

@mcp.custom_route('/health', methods=['GET'])
async def health(request: Request) -> Response:
    return JSONResponse({'status': 'ok', 'service': 'youtube-mcp', 'server_build': SERVER_BUILD, 'server_sha256': _server_file_fingerprint().get('sha256'), 'youtube_client_configured': bool(YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET and YOUTUBE_REDIRECT_URI), 'youtube_authorized': bool(YOUTUBE_REFRESH_TOKEN), 'ffmpeg': FFMPEG_EXE, 'deno_on_path': shutil.which('deno'), 'deno_cached': str(DENO_EXE) if DENO_EXE.exists() else None, 'youtube_proxy_configured': bool(_youtube_proxy_url()), 'youtube_proxy_public': _proxy_public_config(), 'youtube_cookies': _cookie_status_public()})

@mcp.custom_route('/oauth/start', methods=['GET'])
async def oauth_start(request: Request) -> Response:
    try:
        flow = _oauth_flow()
    except Exception as exc:
        return JSONResponse({'error': str(exc)}, status_code=500)
    authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    response = RedirectResponse(authorization_url, status_code=302)
    response.set_cookie('youtube_oauth_state', state, max_age=600, httponly=True, secure=True, samesite='lax')
    return response

@mcp.custom_route('/oauth/callback', methods=['GET'])
async def oauth_callback(request: Request) -> Response:
    oauth_error = request.query_params.get('error')
    if oauth_error:
        return HTMLResponse('<h2>Authorization failed</h2>' f'<p>{html.escape(oauth_error)}</p>', status_code=400)
    state = request.query_params.get('state')
    expected_state = request.cookies.get('youtube_oauth_state')
    if not state or not expected_state or not hmac.compare_digest(state, expected_state):
        return HTMLResponse("<h2>Invalid OAuth state.</h2><p>Start again from <a href='/oauth/start'>/oauth/start</a>.</p>", status_code=400)
    code = request.query_params.get('code')
    if not code:
        return HTMLResponse('<h2>Missing authorization code.</h2>', status_code=400)
    try:
        flow = _oauth_flow(state=state)
        flow.fetch_token(code=code)
        refresh_token = flow.credentials.refresh_token
    except Exception as exc:
        return HTMLResponse('<h2>Token exchange failed.</h2>' f'<pre>{html.escape(str(exc))}</pre>', status_code=500)
    if not refresh_token:
        return HTMLResponse("<h2>No refresh token returned.</h2><p>Start again from <a href='/oauth/start'>/oauth/start</a>.</p>", status_code=500)
    response = HTMLResponse("<h2>YouTube authorization succeeded ✅</h2><p>Save this value directly in Render as <b>YOUTUBE_REFRESH_TOKEN</b>.</p><textarea style='width:95%;height:140px;font-family:monospace;'>" + html.escape(refresh_token) + "</textarea><p><b>Keep this token secret. Do not paste it into chat or GitHub.</b></p><p>After saving it in Render, redeploy the service.</p>")
    response.delete_cookie('youtube_oauth_state')
    response.headers['Cache-Control'] = 'no-store'
    return response

@mcp.custom_route('/media/{token}/{filename}', methods=['GET'])
async def media_download_route(request: Request) -> Response:
    _cleanup_expired_media()
    token = request.path_params['token']
    filename = request.path_params['filename']
    info = MEDIA_TOKENS.get(token)
    if not info or info['expires'] <= time.time():
        return JSONResponse({'error': 'Media link expired or not found.'}, status_code=404)
    path = Path(info['path'])
    if not path.exists() or path.name != filename:
        return JSONResponse({'error': 'Media file not found.'}, status_code=404)
    return FileResponse(str(path), filename=path.name, headers={'Cache-Control': 'private, max-age=300'})

class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        public_prefixes = ('/health', '/oauth/start', '/oauth/callback', '/media/')
        if scope['type'] != 'http' or any(scope['path'].startswith(p) for p in public_prefixes):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get('headers', []))
        auth = headers.get(b'authorization', b'').decode()
        if MCP_API_TOKEN and hmac.compare_digest(auth, f'Bearer {MCP_API_TOKEN}'):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({'jsonrpc': '2.0', 'error': {'code': -32001, 'message': 'Unauthorized'}, 'id': None}, status_code=401)
        await response(scope, receive, send)

def create_app():
    app = mcp.streamable_http_app()
    if MCP_API_TOKEN:
        app.add_middleware(BearerAuthMiddleware)
    return app

if __name__ == '__main__':
    import uvicorn
    if not MCP_API_TOKEN:
        print('WARNING: MCP_API_TOKEN is not set. The MCP endpoint is running without authentication.')
    port = int(os.environ.get('PORT', '10000'))
    uvicorn.run(create_app(), host='0.0.0.0', port=port)
