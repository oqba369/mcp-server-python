import os
import hmac
import html
import time
import uuid
import shutil
import subprocess
import platform
import zipfile
from pathlib import Path
from typing import Any

import httpx
import imageio_ffmpeg
import yt_dlp

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    Response,
    RedirectResponse,
    HTMLResponse,
    FileResponse,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload


MCP_API_TOKEN = os.environ.get("MCP_API_TOKEN")
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")

YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REDIRECT_URI = os.environ.get("YOUTUBE_REDIRECT_URI")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN")


YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]


MEDIA_DIR = Path(
    os.environ.get(
        "MEDIA_DIR",
        "/tmp/youtube_mcp_media",
    )
)

MEDIA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

MAX_REMOTE_FILE_BYTES = int(
    os.environ.get(
        "MAX_REMOTE_FILE_BYTES",
        str(500 * 1024 * 1024),
    )
)

MEDIA_URL_TTL_SECONDS = int(
    os.environ.get(
        "MEDIA_URL_TTL_SECONDS",
        "3600",
    )
)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

MEDIA_TOKENS: dict[str, dict[str, Any]] = {}


# ============================================================
# DENO / YT-DLP RUNTIME
# ============================================================

DENO_DIR = Path(
    os.environ.get(
        "DENO_DIR",
        "/tmp/youtube_mcp_bin",
    )
)

DENO_EXE = DENO_DIR / "deno"

DENO_DOWNLOAD_TIMEOUT = int(
    os.environ.get(
        "DENO_DOWNLOAD_TIMEOUT",
        "180",
    )
)


# ============================================================
# MCP
# ============================================================

mcp = FastMCP(
    "youtube-mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(
            RENDER_EXTERNAL_HOSTNAME
        ),
        allowed_hosts=(
            [RENDER_EXTERNAL_HOSTNAME]
            if RENDER_EXTERNAL_HOSTNAME
            else []
        ),
    ),
)


# ============================================================
# OAUTH
# ============================================================

def _oauth_config() -> dict:

    if not YOUTUBE_CLIENT_ID:
        raise RuntimeError(
            "Missing YOUTUBE_CLIENT_ID."
        )

    if not YOUTUBE_CLIENT_SECRET:
        raise RuntimeError(
            "Missing YOUTUBE_CLIENT_SECRET."
        )

    if not YOUTUBE_REDIRECT_URI:
        raise RuntimeError(
            "Missing YOUTUBE_REDIRECT_URI."
        )

    return {
        "web": {
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "auth_uri": (
                "https://accounts.google.com/o/oauth2/auth"
            ),
            "token_uri": (
                "https://oauth2.googleapis.com/token"
            ),
            "redirect_uris": [
                YOUTUBE_REDIRECT_URI
            ],
        }
    }


def _oauth_flow(
    state: str | None = None,
) -> Flow:

    flow = Flow.from_client_config(
        _oauth_config(),
        scopes=YOUTUBE_SCOPES,
        state=state,
        autogenerate_code_verifier=False,
    )

    flow.redirect_uri = YOUTUBE_REDIRECT_URI

    return flow


def _youtube_credentials() -> Credentials:

    if not YOUTUBE_REFRESH_TOKEN:

        raise RuntimeError(
            "YouTube is not authorized yet. "
            "Open /oauth/start and authorize."
        )

    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        scopes=YOUTUBE_SCOPES,
    )

    credentials.refresh(
        GoogleAuthRequest()
    )

    return credentials


def _youtube():

    return build(
        "youtube",
        "v3",
        credentials=_youtube_credentials(),
        cache_discovery=False,
    )


def _youtube_analytics():

    return build(
        "youtubeAnalytics",
        "v2",
        credentials=_youtube_credentials(),
        cache_discovery=False,
    )


# ============================================================
# YOUTUBE HELPERS
# ============================================================

def _my_channel_id() -> str:

    result = (
        _youtube()
        .channels()
        .list(
            part="id",
            mine=True,
        )
        .execute()
    )

    items = result.get(
        "items",
        [],
    )

    if not items:
        raise RuntimeError(
            "No YouTube channel found "
            "for this account."
        )

    return items[0]["id"]


def _my_uploads_playlist_id() -> str:

    result = (
        _youtube()
        .channels()
        .list(
            part="contentDetails",
            mine=True,
        )
        .execute()
    )

    items = result.get(
        "items",
        [],
    )

    if not items:
        raise RuntimeError(
            "No YouTube channel found."
        )

    return (
        items[0]
        ["contentDetails"]
        ["relatedPlaylists"]
        ["uploads"]
    )


def _get_owned_video(
    video_id: str,
    part: str = "snippet,status",
) -> dict:

    result = (
        _youtube()
        .videos()
        .list(
            part=part,
            id=video_id,
        )
        .execute()
    )

    items = result.get(
        "items",
        [],
    )

    if not items:

        raise RuntimeError(
            f"Video not found: {video_id}"
        )

    video = items[0]

    if (
        video
        .get("snippet", {})
        .get("channelId")
        != _my_channel_id()
    ):

        raise PermissionError(
            "This media operation is limited "
            "to videos owned by the authenticated "
            "YouTube channel."
        )

    return video


# ============================================================
# FILE HELPERS
# ============================================================

def _safe_filename(
    name: str,
) -> str:

    keep = []

    for ch in name:

        if (
            ch.isalnum()
            or ch in "._-"
        ):
            keep.append(ch)

        else:
            keep.append("_")

    result = (
        "".join(keep)
        .strip("._")
    )

    return (
        result[:180]
        or f"file_{uuid.uuid4().hex[:8]}"
    )


def _cleanup_expired_media() -> None:

    now = time.time()

    for token, info in list(
        MEDIA_TOKENS.items()
    ):

        if info["expires"] <= now:
            MEDIA_TOKENS.pop(
                token,
                None,
            )

    cutoff = (
        now
        - max(
            MEDIA_URL_TTL_SECONDS * 2,
            7200,
        )
    )

    for path in MEDIA_DIR.glob("*"):

        try:

            if (
                path.is_file()
                and path.stat().st_mtime
                < cutoff
            ):

                path.unlink(
                    missing_ok=True
                )

            elif (
                path.is_dir()
                and path.stat().st_mtime
                < cutoff
            ):

                shutil.rmtree(
                    path,
                    ignore_errors=True,
                )

        except OSError:
            pass


def _publish_media(
    path: Path,
    ttl_seconds: int | None = None,
) -> dict:

    _cleanup_expired_media()

    path = path.resolve()

    if (
        not path.exists()
        or not path.is_file()
    ):

        raise FileNotFoundError(
            str(path)
        )

    token = uuid.uuid4().hex

    expires_in = (
        ttl_seconds
        or MEDIA_URL_TTL_SECONDS
    )

    MEDIA_TOKENS[token] = {
        "path": str(path),
        "expires": (
            time.time()
            + expires_in
        ),
    }

    host = (
        RENDER_EXTERNAL_HOSTNAME
    )

    url = (
        f"https://{host}"
        f"/media/{token}/{path.name}"
        if host
        else None
    )

    return {
        "media_id": token,
        "filename": path.name,
        "size_bytes": (
            path.stat().st_size
        ),
        "expires_in_seconds": (
            expires_in
        ),
        "url": url,
    }


def _resolve_media(
    media_id: str,
) -> Path:

    _cleanup_expired_media()

    info = MEDIA_TOKENS.get(
        media_id
    )

    if not info:

        raise RuntimeError(
            "Media ID is missing or expired. "
            "Generate/download the media again."
        )

    path = Path(
        info["path"]
    )

    if not path.exists():

        raise RuntimeError(
            "The temporary media file "
            "no longer exists."
        )

    return path


def _download_url(
    url: str,
    destination: Path,
    max_bytes: int = MAX_REMOTE_FILE_BYTES,
) -> Path:

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    total = 0

    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=httpx.Timeout(
            30.0,
            read=300.0,
        ),
        headers={
            "User-Agent": (
                "YouTube-MCP/1.0"
            )
        },
    ) as response:

        response.raise_for_status()

        content_length = (
            response.headers.get(
                "content-length"
            )
        )

        if (
            content_length
            and int(content_length)
            > max_bytes
        ):

            raise RuntimeError(
                "Remote file is too large. "
                f"Limit is {max_bytes} bytes."
            )

        with destination.open(
            "wb"
        ) as f:

            for chunk in (
                response.iter_bytes(
                    1024 * 1024
                )
            ):

                total += len(chunk)

                if total > max_bytes:

                    destination.unlink(
                        missing_ok=True
                    )

                    raise RuntimeError(
                        "Remote file exceeded "
                        f"limit of {max_bytes} bytes."
                    )

                f.write(chunk)

    return destination


# ============================================================
# FFMPEG
# ============================================================

def _run_ffmpeg(
    args: list[str],
    timeout: int = 600,
) -> dict:

    result = subprocess.run(
        [FFMPEG_EXE] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:

        raise RuntimeError(
            "ffmpeg failed:\n"
            + result.stderr[-5000:]
        )

    return {
        "returncode": (
            result.returncode
        ),
        "stdout": (
            result.stdout[-5000:]
        ),
        "stderr": (
            result.stderr[-5000:]
        ),
    }


# ============================================================
# DENO / EJS
# ============================================================

def _deno_download_url() -> str:

    machine = (
        platform.machine()
        .lower()
    )

    if machine in {
        "x86_64",
        "amd64",
    }:

        asset = (
            "deno-x86_64-"
            "unknown-linux-gnu.zip"
        )

    elif machine in {
        "aarch64",
        "arm64",
    }:

        asset = (
            "deno-aarch64-"
            "unknown-linux-gnu.zip"
        )

    else:

        raise RuntimeError(
            "Unsupported CPU architecture "
            "for automatic Deno install: "
            f"{machine}"
        )

    return (
        "https://github.com/"
        "denoland/deno/releases/"
        "latest/download/"
        + asset
    )


def _deno_version(
    executable: Path,
) -> str:

    result = subprocess.run(
        [
            str(executable),
            "--version",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:

        raise RuntimeError(
            "Deno exists but could not "
            "be executed: "
            + (
                result.stderr
                or result.stdout
            )[-2000:]
        )

    return (
        result.stdout
        or ""
    ).strip()


def _ensure_deno() -> Path:

    # User-supplied path first
    configured = os.environ.get(
        "DENO_BIN"
    )

    if configured:

        path = Path(configured)

        if path.exists():

            _deno_version(path)

            return path

    # Existing Deno on PATH
    found = shutil.which(
        "deno"
    )

    if found:

        path = Path(found)

        _deno_version(path)

        return path

    # Cached temporary Deno
    if DENO_EXE.exists():

        try:

            _deno_version(
                DENO_EXE
            )

            return DENO_EXE

        except Exception:

            DENO_EXE.unlink(
                missing_ok=True
            )

    # Install latest Deno
    DENO_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    zip_path = (
        DENO_DIR
        / "deno.zip"
    )

    url = (
        _deno_download_url()
    )

    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=httpx.Timeout(
            30.0,
            read=float(
                DENO_DOWNLOAD_TIMEOUT
            ),
        ),
        headers={
            "User-Agent": (
                "YouTube-MCP/1.0"
            )
        },
    ) as response:

        response.raise_for_status()

        with zip_path.open(
            "wb"
        ) as f:

            for chunk in (
                response.iter_bytes(
                    1024 * 1024
                )
            ):

                f.write(chunk)

    try:

        with zipfile.ZipFile(
            zip_path
        ) as archive:

            members = {
                Path(name).name: name
                for name
                in archive.namelist()
            }

            member = members.get(
                "deno"
            )

            if not member:

                raise RuntimeError(
                    "Downloaded Deno archive "
                    "did not contain the "
                    "deno executable."
                )

            with (
                archive.open(
                    member
                ) as src,
                DENO_EXE.open(
                    "wb"
                ) as dst,
            ):

                shutil.copyfileobj(
                    src,
                    dst,
                )

        DENO_EXE.chmod(
            0o755
        )

    finally:

        zip_path.unlink(
            missing_ok=True
        )

    _deno_version(
        DENO_EXE
    )

    return DENO_EXE


def _youtube_ydl_options(
    outtmpl: str,
) -> dict[str, Any]:

    deno = _ensure_deno()

    return {
        "outtmpl": outtmpl,

        "format": (
            "bv*+ba/b"
        ),

        "merge_output_format": (
            "mp4"
        ),

        "ffmpeg_location": (
            FFMPEG_EXE
        ),

        "quiet": True,

        "no_warnings": True,

        "noplaylist": True,

        "js_runtimes": {
            "deno": {
                "path": str(deno)
            }
        },

        "remote_components": {
            "ejs:npm"
        },

        "socket_timeout": 30,

        "retries": 3,

        "fragment_retries": 3,
    }


# ============================================================
# UPLOAD HELPER
# ============================================================

def _upload_video_file(
    path: Path,
    title: str,
    description: str = "",
    privacy_status: str = "private",
    category_id: str = "22",
    tags: list[str] | None = None,
    made_for_kids: bool | None = None,
) -> dict:

    if privacy_status not in {
        "private",
        "unlisted",
        "public",
    }:

        raise ValueError(
            "privacy_status must be "
            "private, unlisted, or public."
        )

    body: dict[str, Any] = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": (
                privacy_status
            )
        },
    }

    if tags:

        body[
            "snippet"
        ][
            "tags"
        ] = tags

    if made_for_kids is not None:

        body[
            "status"
        ][
            "selfDeclaredMadeForKids"
        ] = made_for_kids

    media = MediaFileUpload(
        str(path),
        chunksize=(
            8 * 1024 * 1024
        ),
        resumable=True,
    )

    request = (
        _youtube()
        .videos()
        .insert(
            part=(
                "snippet,status"
            ),
            body=body,
            media_body=media,
        )
    )

    response = None

    while response is None:

        _, response = (
            request.next_chunk()
        )

    return response


# ============================================================
# BASIC TOOLS
# ============================================================

@mcp.tool()
def hello(
    name: str,
) -> str:

    return f"Hello, {name}!"


@mcp.tool()
def youtube_auth_status() -> dict:

    return {
        "client_configured": bool(
            YOUTUBE_CLIENT_ID
            and YOUTUBE_CLIENT_SECRET
            and YOUTUBE_REDIRECT_URI
        ),
        "authorized": bool(
            YOUTUBE_REFRESH_TOKEN
        ),
        "scopes": YOUTUBE_SCOPES,
    }


@mcp.tool()
def youtube_my_channel() -> dict:

    result = (
        _youtube()
        .channels()
        .list(
            part=(
                "snippet,"
                "statistics,"
                "contentDetails,"
                "status,"
                "brandingSettings"
            ),
            mine=True,
        )
        .execute()
    )

    items = result.get(
        "items",
        [],
    )

    return {
        "found": bool(items),
        "channel": (
            items[0]
            if items
            else None
        ),
    }


# ============================================================
# VIDEOS
# ============================================================

@mcp.tool()
def youtube_list_videos(
    max_results: int = 50,
) -> dict:

    max_results = max(
        1,
        min(
            int(max_results),
            50,
        ),
    )

    playlist_id = (
        _my_uploads_playlist_id()
    )

    playlist_result = (
        _youtube()
        .playlistItems()
        .list(
            part=(
                "contentDetails,"
                "snippet,status"
            ),
            playlistId=(
                playlist_id
            ),
            maxResults=(
                max_results
            ),
        )
        .execute()
    )

    video_ids = [
        item[
            "contentDetails"
        ][
            "videoId"
        ]
        for item
        in playlist_result.get(
            "items",
            [],
        )
    ]

    if not video_ids:

        return {
            "count": 0,
            "videos": [],
        }

    videos_result = (
        _youtube()
        .videos()
        .list(
            part=(
                "snippet,"
                "contentDetails,"
                "status,"
                "statistics,"
                "liveStreamingDetails,"
                "paidProductPlacementDetails"
            ),
            id=",".join(
                video_ids
            ),
        )
        .execute()
    )

    videos = videos_result.get(
        "items",
        [],
    )

    for video in videos:

        video["watchUrl"] = (
            "https://www.youtube.com/watch?v="
            + video["id"]
        )

    return {
        "count": len(videos),
        "videos": videos,
    }


@mcp.tool()
def youtube_get_video(
    video_id: str,
) -> dict:

    result = (
        _youtube()
        .videos()
        .list(
            part=(
                "snippet,"
                "contentDetails,"
                "status,"
                "statistics,"
                "player,"
                "recordingDetails,"
                "liveStreamingDetails,"
                "paidProductPlacementDetails"
            ),
            id=video_id,
        )
        .execute()
    )

    items = result.get(
        "items",
        [],
    )

    if not items:

        return {
            "found": False,
            "video_id": video_id,
        }

    video = items[0]

    video["watchUrl"] = (
        "https://www.youtube.com/watch?v="
        + video_id
    )

    return {
        "found": True,
        "video": video,
    }


@mcp.tool()
def youtube_update_video(
    video_id: str,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    category_id: str | None = None,
    privacy_status: str | None = None,
    publish_at: str | None = None,
    made_for_kids: bool | None = None,
    contains_synthetic_media: bool | None = None,
) -> dict:

    existing = _get_owned_video(
        video_id,
        part="snippet,status",
    )

    body: dict[str, Any] = {
        "id": video_id
    }

    parts: list[str] = []

    if any(
        v is not None
        for v in [
            title,
            description,
            tags,
            category_id,
        ]
    ):

        snippet = (
            existing["snippet"]
        )

        new_snippet: dict[str, Any] = {
            "title": (
                snippet["title"]
            ),
            "description": (
                snippet.get(
                    "description",
                    "",
                )
            ),
            "categoryId": (
                snippet[
                    "categoryId"
                ]
            ),
        }

        if "tags" in snippet:

            new_snippet[
                "tags"
            ] = snippet[
                "tags"
            ]

        if title is not None:
            new_snippet["title"] = title

        if description is not None:
            new_snippet[
                "description"
            ] = description

        if tags is not None:
            new_snippet["tags"] = tags

        if category_id is not None:
            new_snippet[
                "categoryId"
            ] = category_id

        body[
            "snippet"
        ] = new_snippet

        parts.append(
            "snippet"
        )

    if any(
        v is not None
        for v in [
            privacy_status,
            publish_at,
            made_for_kids,
            contains_synthetic_media,
        ]
    ):

        old_status = (
            existing.get(
                "status",
                {},
            )
        )

        new_status: dict[str, Any] = {}

        for field in [
            "privacyStatus",
            "embeddable",
            "license",
            "publicStatsViewable",
            "selfDeclaredMadeForKids",
            "containsSyntheticMedia",
        ]:

            if field in old_status:
                new_status[
                    field
                ] = old_status[
                    field
                ]

        if privacy_status is not None:

            if privacy_status not in {
                "private",
                "unlisted",
                "public",
            }:

                raise ValueError(
                    "privacy_status must be "
                    "private, unlisted, or public."
                )

            new_status[
                "privacyStatus"
            ] = privacy_status

        if publish_at is not None:
            new_status[
                "publishAt"
            ] = publish_at

        if made_for_kids is not None:
            new_status[
                "selfDeclaredMadeForKids"
            ] = made_for_kids

        if contains_synthetic_media is not None:
            new_status[
                "containsSyntheticMedia"
            ] = (
                contains_synthetic_media
            )

        body[
            "status"
        ] = new_status

        parts.append(
            "status"
        )

    if not parts:

        return {
            "updated": False,
            "message": (
                "No fields were supplied."
            ),
        }

    response = (
        _youtube()
        .videos()
        .update(
            part=",".join(
                parts
            ),
            body=body,
        )
        .execute()
    )

    return {
        "updated": True,
        "video": response,
    }


@mcp.tool()
def youtube_delete_video(
    video_id: str,
    confirm: bool = False,
) -> dict:

    _get_owned_video(
        video_id
    )

    if not confirm:

        return {
            "deleted": False,
            "requires_confirmation": True,
            "message": (
                "Call again with confirm=true "
                "to permanently delete."
            ),
        }

    (
        _youtube()
        .videos()
        .delete(
            id=video_id
        )
        .execute()
    )

    return {
        "deleted": True,
        "video_id": video_id,
    }


@mcp.tool()
def youtube_rate_video(
    video_id: str,
    rating: str,
) -> dict:

    if rating not in {
        "like",
        "dislike",
        "none",
    }:

        raise ValueError(
            "rating must be "
            "like, dislike, or none."
        )

    (
        _youtube()
        .videos()
        .rate(
            id=video_id,
            rating=rating,
        )
        .execute()
    )

    return {
        "video_id": video_id,
        "rating": rating,
    }


@mcp.tool()
def youtube_upload_video_from_url(
    source_url: str,
    title: str,
    description: str = "",
    privacy_status: str = "private",
    category_id: str = "22",
    tags: list[str] | None = None,
    made_for_kids: bool | None = None,
) -> dict:

    suffix = (
        Path(
            source_url.split(
                "?"
            )[0]
        ).suffix
        or ".mp4"
    )

    path = (
        MEDIA_DIR
        / (
            "upload_"
            + uuid.uuid4().hex
            + suffix
        )
    )

    try:

        _download_url(
            source_url,
            path,
        )

        result = (
            _upload_video_file(
                path,
                title,
                description,
                privacy_status,
                category_id,
                tags,
                made_for_kids,
            )
        )

        return {
            "uploaded": True,
            "video": result,
            "watch_url": (
                "https://www.youtube.com/watch?v="
                + result["id"]
                if result.get("id")
                else None
            ),
        }

    finally:

        path.unlink(
            missing_ok=True
        )


@mcp.tool()
def youtube_set_thumbnail_from_url(
    video_id: str,
    image_url: str,
) -> dict:

    _get_owned_video(
        video_id
    )

    suffix = (
        Path(
            image_url.split(
                "?"
            )[0]
        ).suffix
        or ".jpg"
    )

    path = (
        MEDIA_DIR
        / (
            "thumb_"
            + uuid.uuid4().hex
            + suffix
        )
    )

    try:

        _download_url(
            image_url,
            path,
            max_bytes=(
                10 * 1024 * 1024
            ),
        )

        result = (
            _youtube()
            .thumbnails()
            .set(
                videoId=video_id,
                media_body=(
                    MediaFileUpload(
                        str(path),
                        resumable=False,
                    )
                ),
            )
            .execute()
        )

        return {
            "updated": True,
            "result": result,
        }

    finally:

        path.unlink(
            missing_ok=True
        )


# ============================================================
# PLAYLISTS
# ============================================================

@mcp.tool()
def youtube_list_playlists(
    max_results: int = 50,
) -> dict:

    return (
        _youtube()
        .playlists()
        .list(
            part=(
                "snippet,"
                "status,"
                "contentDetails"
            ),
            mine=True,
            maxResults=max(
                1,
                min(
                    int(max_results),
                    50,
                ),
            ),
        )
        .execute()
    )


@mcp.tool()
def youtube_create_playlist(
    title: str,
    description: str = "",
    privacy_status: str = "private",
) -> dict:

    if privacy_status not in {
        "private",
        "unlisted",
        "public",
    }:

        raise ValueError(
            "Invalid privacy_status."
        )

    return (
        _youtube()
        .playlists()
        .insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": title,
                    "description": description,
                },
                "status": {
                    "privacyStatus": (
                        privacy_status
                    )
                },
            },
        )
        .execute()
    )


@mcp.tool()
def youtube_add_video_to_playlist(
    playlist_id: str,
    video_id: str,
    position: int | None = None,
) -> dict:

    snippet: dict[str, Any] = {
        "playlistId": playlist_id,
        "resourceId": {
            "kind": "youtube#video",
            "videoId": video_id,
        },
    }

    if position is not None:
        snippet["position"] = int(
            position
        )

    return (
        _youtube()
        .playlistItems()
        .insert(
            part="snippet",
            body={
                "snippet": snippet
            },
        )
        .execute()
    )


@mcp.tool()
def youtube_remove_playlist_item(
    playlist_item_id: str,
    confirm: bool = False,
) -> dict:

    if not confirm:

        return {
            "removed": False,
            "requires_confirmation": True,
        }

    (
        _youtube()
        .playlistItems()
        .delete(
            id=playlist_item_id
        )
        .execute()
    )

    return {
        "removed": True,
        "playlist_item_id": (
            playlist_item_id
        ),
    }


# ============================================================
# COMMENTS
# ============================================================

@mcp.tool()
def youtube_list_comments(
    video_id: str,
    max_results: int = 50,
    order: str = "time",
) -> dict:

    return (
        _youtube()
        .commentThreads()
        .list(
            part=(
                "snippet,replies"
            ),
            videoId=video_id,
            maxResults=max(
                1,
                min(
                    int(max_results),
                    100,
                ),
            ),
            order=order,
            textFormat="plainText",
        )
        .execute()
    )


@mcp.tool()
def youtube_reply_to_comment(
    parent_comment_id: str,
    text: str,
) -> dict:

    return (
        _youtube()
        .comments()
        .insert(
            part="snippet",
            body={
                "snippet": {
                    "parentId": (
                        parent_comment_id
                    ),
                    "textOriginal": text,
                }
            },
        )
        .execute()
    )


@mcp.tool()
def youtube_moderate_comment(
    comment_id: str,
    moderation_status: str,
    ban_author: bool = False,
    confirm: bool = False,
) -> dict:

    if moderation_status not in {
        "heldForReview",
        "published",
        "rejected",
    }:

        raise ValueError(
            "Invalid moderation_status."
        )

    if not confirm:

        return {
            "changed": False,
            "requires_confirmation": True,
        }

    (
        _youtube()
        .comments()
        .setModerationStatus(
            id=comment_id,
            moderationStatus=(
                moderation_status
            ),
            banAuthor=ban_author,
        )
        .execute()
    )

    return {
        "changed": True,
        "comment_id": comment_id,
        "moderation_status": (
            moderation_status
        ),
        "ban_author": ban_author,
    }


@mcp.tool()
def youtube_delete_comment(
    comment_id: str,
    confirm: bool = False,
) -> dict:

    if not confirm:

        return {
            "deleted": False,
            "requires_confirmation": True,
        }

    (
        _youtube()
        .comments()
        .delete(
            id=comment_id
        )
        .execute()
    )

    return {
        "deleted": True,
        "comment_id": comment_id,
    }


# ============================================================
# CAPTIONS
# ============================================================

@mcp.tool()
def youtube_list_captions(
    video_id: str,
) -> dict:

    return (
        _youtube()
        .captions()
        .list(
            part="snippet",
            videoId=video_id,
        )
        .execute()
    )


@mcp.tool()
def youtube_download_caption(
    caption_id: str,
    file_format: str = "srt",
) -> dict:

    allowed = {
        "srt",
        "vtt",
        "ttml",
        "sbv",
    }

    if file_format not in allowed:

        raise ValueError(
            "file_format must be one of "
            f"{sorted(allowed)}"
        )

    path = (
        MEDIA_DIR
        / (
            "caption_"
            + uuid.uuid4().hex
            + "."
            + file_format
        )
    )

    request = (
        _youtube()
        .captions()
        .download(
            id=caption_id,
            tfmt=file_format,
        )
    )

    with path.open(
        "wb"
    ) as f:

        downloader = MediaIoBaseDownload(
            f,
            request,
        )

        done = False

        while not done:
            _, done = (
                downloader.next_chunk()
            )

    return _publish_media(
        path
    )


@mcp.tool()
def youtube_upload_caption_from_url(
    video_id: str,
    source_url: str,
    language: str,
    name: str = "",
    is_draft: bool = False,
) -> dict:

    _get_owned_video(
        video_id
    )

    suffix = (
        Path(
            source_url.split(
                "?"
            )[0]
        ).suffix
        or ".srt"
    )

    path = (
        MEDIA_DIR
        / (
            "caption_upload_"
            + uuid.uuid4().hex
            + suffix
        )
    )

    try:

        _download_url(
            source_url,
            path,
            max_bytes=(
                20 * 1024 * 1024
            ),
        )

        body = {
            "snippet": {
                "videoId": video_id,
                "language": language,
                "name": name,
                "isDraft": is_draft,
            }
        }

        return (
            _youtube()
            .captions()
            .insert(
                part="snippet",
                body=body,
                media_body=(
                    MediaFileUpload(
                        str(path),
                        resumable=False,
                    )
                ),
            )
            .execute()
        )

    finally:

        path.unlink(
            missing_ok=True
        )


# ============================================================
# SUBSCRIPTIONS / SEARCH / ANALYTICS
# ============================================================

@mcp.tool()
def youtube_list_subscriptions(
    max_results: int = 50,
) -> dict:

    return (
        _youtube()
        .subscriptions()
        .list(
            part=(
                "snippet,contentDetails"
            ),
            mine=True,
            maxResults=max(
                1,
                min(
                    int(max_results),
                    50,
                ),
            ),
        )
        .execute()
    )


@mcp.tool()
def youtube_subscribe(
    channel_id: str,
) -> dict:

    return (
        _youtube()
        .subscriptions()
        .insert(
            part="snippet",
            body={
                "snippet": {
                    "resourceId": {
                        "kind": (
                            "youtube#channel"
                        ),
                        "channelId": (
                            channel_id
                        ),
                    }
                }
            },
        )
        .execute()
    )


@mcp.tool()
def youtube_unsubscribe(
    subscription_id: str,
    confirm: bool = False,
) -> dict:

    if not confirm:

        return {
            "deleted": False,
            "requires_confirmation": True,
        }

    (
        _youtube()
        .subscriptions()
        .delete(
            id=subscription_id
        )
        .execute()
    )

    return {
        "deleted": True,
        "subscription_id": (
            subscription_id
        ),
    }


@mcp.tool()
def youtube_search(
    query: str,
    resource_type: str = "video",
    max_results: int = 20,
) -> dict:

    if resource_type not in {
        "video",
        "channel",
        "playlist",
    }:

        raise ValueError(
            "resource_type must be "
            "video, channel, or playlist."
        )

    return (
        _youtube()
        .search()
        .list(
            part="snippet",
            q=query,
            type=resource_type,
            maxResults=max(
                1,
                min(
                    int(max_results),
                    50,
                ),
            ),
        )
        .execute()
    )


@mcp.tool()
def youtube_analytics_report(
    start_date: str,
    end_date: str,
    metrics: str = (
        "views,"
        "estimatedMinutesWatched,"
        "averageViewDuration"
    ),
    dimensions: str | None = None,
    filters: str | None = None,
    sort: str | None = None,
    max_results: int = 200,
) -> dict:

    kwargs: dict[str, Any] = {
        "ids": "channel==MINE",
        "startDate": start_date,
        "endDate": end_date,
        "metrics": metrics,
        "maxResults": max(
            1,
            min(
                int(max_results),
                200,
            ),
        ),
    }

    if dimensions:
        kwargs[
            "dimensions"
        ] = dimensions

    if filters:
        kwargs[
            "filters"
        ] = filters

    if sort:
        kwargs[
            "sort"
        ] = sort

    return (
        _youtube_analytics()
        .reports()
        .query(
            **kwargs
        )
        .execute()
    )


# ============================================================
# GENERIC YOUTUBE / LIVE API
# ============================================================

GENERIC_METHODS: dict[str, set[str]] = {

    "activities": {
        "list"
    },

    "channels": {
        "list",
        "update",
    },

    "channelSections": {
        "list",
        "insert",
        "update",
        "delete",
    },

    "commentThreads": {
        "list",
        "insert",
    },

    "comments": {
        "list",
        "insert",
        "update",
        "delete",
        "setModerationStatus",
        "markAsSpam",
    },

    "i18nLanguages": {
        "list"
    },

    "i18nRegions": {
        "list"
    },

    "playlistItems": {
        "list",
        "insert",
        "update",
        "delete",
    },

    "playlists": {
        "list",
        "insert",
        "update",
        "delete",
    },

    "search": {
        "list"
    },

    "subscriptions": {
        "list",
        "insert",
        "delete",
    },

    "videoCategories": {
        "list"
    },

    "videos": {
        "list",
        "update",
        "delete",
        "rate",
        "getRating",
        "reportAbuse",
    },

    "liveBroadcasts": {
        "list",
        "insert",
        "update",
        "delete",
        "bind",
        "transition",
    },

    "liveStreams": {
        "list",
        "insert",
        "update",
        "delete",
    },

    "liveChatMessages": {
        "list",
        "insert",
        "delete",
    },

    "liveChatModerators": {
        "list",
        "insert",
        "delete",
    },

    "liveChatBans": {
        "insert",
        "delete",
    },
}


READ_ONLY_GENERIC_METHODS = {
    "list",
    "getRating",
}


@mcp.tool()
def youtube_api_call(
    resource: str,
    method: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    confirm: bool = False,
) -> dict:

    allowed = (
        GENERIC_METHODS.get(
            resource
        )
    )

    if (
        not allowed
        or method not in allowed
    ):

        raise ValueError(
            "Unsupported resource/method. "
            f"Allowed methods for {resource}: "
            f"{sorted(allowed) if allowed else 'none'}"
        )

    if (
        method not in
        READ_ONLY_GENERIC_METHODS
        and not confirm
    ):

        return {
            "executed": False,
            "requires_confirmation": True,
            "resource": resource,
            "method": method,
        }

    resource_obj = getattr(
        _youtube(),
        resource,
    )()

    method_fn = getattr(
        resource_obj,
        method,
    )

    kwargs = dict(
        params or {}
    )

    if body is not None:
        kwargs["body"] = body

    result = (
        method_fn(
            **kwargs
        )
        .execute()
    )

    return {
        "executed": True,
        "resource": resource,
        "method": method,
        "result": result,
    }


# ============================================================
# MEDIA RUNTIME STATUS
# ============================================================

@mcp.tool()
def media_runtime_status(
    install_deno_if_missing: bool = False,
) -> dict:

    deno_path = shutil.which(
        "deno"
    )

    deno_version = None

    if install_deno_if_missing:

        deno = _ensure_deno()

        deno_path = str(deno)

        deno_version = (
            _deno_version(deno)
        )

    elif deno_path:

        try:

            deno_version = (
                _deno_version(
                    Path(deno_path)
                )
            )

        except Exception as exc:

            deno_version = (
                f"error: {exc}"
            )

    elif DENO_EXE.exists():

        try:

            deno_path = str(
                DENO_EXE
            )

            deno_version = (
                _deno_version(
                    DENO_EXE
                )
            )

        except Exception as exc:

            deno_version = (
                f"error: {exc}"
            )

    return {
        "ffmpeg": FFMPEG_EXE,
        "deno_path": deno_path,
        "deno_version": (
            deno_version
        ),
        "yt_dlp_version": (
            getattr(
                getattr(
                    yt_dlp,
                    "version",
                    None,
                ),
                "__version__",
                "unknown",
            )
        ),
        "note": (
            "Call with "
            "install_deno_if_missing=true "
            "to install/test Deno on Render."
        ),
    }


# ============================================================
# DOWNLOAD OWNED VIDEO
# ============================================================

@mcp.tool()
def video_download_my_video(
    video_id: str,
) -> dict:

    video = _get_owned_video(
        video_id
    )

    title = (
        video["snippet"]["title"]
    )

    base = (
        MEDIA_DIR
        / (
            f"yt_{video_id}_"
            + uuid.uuid4().hex
        )
    )

    outtmpl = (
        str(base)
        + ".%(ext)s"
    )

    url = (
        "https://www.youtube.com/watch?v="
        + video_id
    )

    try:

        ydl_opts = (
            _youtube_ydl_options(
                outtmpl
            )
        )

        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = (
                ydl.extract_info(
                    url,
                    download=True,
                )
            )

        candidates = [

            p

            for p in MEDIA_DIR.glob(
                base.name + ".*"
            )

            if (
                p.is_file()
                and not p.name.endswith(
                    (
                        ".part",
                        ".ytdl",
                        ".temp",
                    )
                )
            )
        ]

        candidates.sort(
            key=lambda p: (
                p.stat().st_mtime
            ),
            reverse=True,
        )

        if not candidates:

            raise RuntimeError(
                "yt-dlp finished but "
                "no final output file "
                "was found."
            )

        path = candidates[0]

        published = (
            _publish_media(
                path
            )
        )

        published.update(
            {
                "video_id": video_id,
                "title": title,
                "source_watch_url": url,
                "duration_seconds": (
                    info.get(
                        "duration"
                    )
                ),
                "extractor": (
                    info.get(
                        "extractor_key"
                    )
                    or info.get(
                        "extractor"
                    )
                ),
            }
        )

        return published

    except Exception as exc:

        raise RuntimeError(
            "Could not download this owned "
            "YouTube video. Public/unlisted "
            "videos should work with the "
            "Deno/EJS runtime. Private or "
            "otherwise restricted videos may "
            "still require the original source "
            "file from Drive/object storage. "
            "Details: "
            + str(exc)
        ) from exc


# ============================================================
# OTHER MEDIA TOOLS
# ============================================================

@mcp.tool()
def media_fetch_source_url(
    source_url: str,
    filename: str = "source.bin",
) -> dict:

    filename = (
        _safe_filename(
            filename
        )
    )

    path = (
        MEDIA_DIR
        / (
            uuid.uuid4().hex
            + "_"
            + filename
        )
    )

    _download_url(
        source_url,
        path,
    )

    return _publish_media(
        path
    )


@mcp.tool()
def video_probe(
    media_id: str,
) -> dict:

    path = _resolve_media(
        media_id
    )

    result = subprocess.run(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-i",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    return {
        "media_id": media_id,
        "filename": path.name,
        "size_bytes": (
            path.stat().st_size
        ),
        "ffmpeg_info": (
            result.stderr
            or ""
        )[-12000:],
    }


@mcp.tool()
def video_extract_frames(
    media_id: str,
    interval_seconds: float = 5.0,
    max_frames: int = 12,
) -> dict:

    if interval_seconds <= 0:

        raise ValueError(
            "interval_seconds must be > 0."
        )

    max_frames = max(
        1,
        min(
            int(max_frames),
            50,
        ),
    )

    source = _resolve_media(
        media_id
    )

    job_dir = (
        MEDIA_DIR
        / (
            "frames_"
            + uuid.uuid4().hex
        )
    )

    job_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_pattern = (
        job_dir
        / "frame_%03d.jpg"
    )

    _run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-vf",
            (
                "fps=1/"
                + str(
                    float(
                        interval_seconds
                    )
                )
            ),
            "-frames:v",
            str(max_frames),
            "-q:v",
            "2",
            str(output_pattern),
        ],
        timeout=600,
    )

    frames = [
        _publish_media(path)

        for path in sorted(
            job_dir.glob(
                "frame_*.jpg"
            )
        )
    ]

    return {
        "source_media_id": (
            media_id
        ),
        "interval_seconds": (
            interval_seconds
        ),
        "count": len(frames),
        "frames": frames,
    }


@mcp.tool()
def video_extract_audio(
    media_id: str,
    audio_format: str = "mp3",
) -> dict:

    source = _resolve_media(
        media_id
    )

    if audio_format not in {
        "mp3",
        "wav",
        "m4a",
    }:

        raise ValueError(
            "audio_format must be "
            "mp3, wav, or m4a."
        )

    output = (
        MEDIA_DIR
        / (
            "audio_"
            + uuid.uuid4().hex
            + "."
            + audio_format
        )
    )

    if audio_format == "mp3":

        codec_args = [
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "3",
        ]

    elif audio_format == "wav":

        codec_args = [
            "-codec:a",
            "pcm_s16le",
        ]

    else:

        codec_args = [
            "-codec:a",
            "aac",
            "-b:a",
            "160k",
        ]

    _run_ffmpeg(
        [
            "-y",
            "-i",
            str(source),
            "-vn",
            *codec_args,
            str(output),
        ],
        timeout=600,
    )

    return _publish_media(
        output
    )


@mcp.tool()
def video_get_clip(
    media_id: str,
    start_seconds: float,
    duration_seconds: float,
) -> dict:

    if start_seconds < 0:

        raise ValueError(
            "start_seconds must be >= 0."
        )

    if (
        duration_seconds <= 0
        or duration_seconds > 300
    ):

        raise ValueError(
            "duration_seconds must be "
            "between 0 and 300."
        )

    source = _resolve_media(
        media_id
    )

    output = (
        MEDIA_DIR
        / (
            "clip_"
            + uuid.uuid4().hex
            + ".mp4"
        )
    )

    _run_ffmpeg(
        [
            "-y",
            "-ss",
            str(
                float(
                    start_seconds
                )
            ),
            "-i",
            str(source),
            "-t",
            str(
                float(
                    duration_seconds
                )
            ),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-c",
            "copy",
            str(output),
        ],
        timeout=600,
    )

    result = (
        _publish_media(
            output
        )
    )

    result.update(
        {
            "start_seconds": (
                start_seconds
            ),
            "duration_seconds": (
                duration_seconds
            ),
        }
    )

    return result


@mcp.tool()
def media_cleanup(
    confirm: bool = False,
) -> dict:

    if not confirm:

        return {
            "cleaned": False,
            "requires_confirmation": True,
        }

    count = 0

    for path in list(
        MEDIA_DIR.glob("*")
    ):

        try:

            if path.is_dir():

                shutil.rmtree(
                    path,
                    ignore_errors=True,
                )

            else:

                path.unlink(
                    missing_ok=True
                )

            count += 1

        except OSError:
            pass

    MEDIA_TOKENS.clear()

    return {
        "cleaned": True,
        "items_removed": count,
    }


# ============================================================
# HEALTH
# ============================================================

@mcp.custom_route(
    "/health",
    methods=["GET"],
)
async def health(
    request: Request,
) -> Response:

    return JSONResponse(
        {
            "status": "ok",
            "service": "youtube-mcp",

            "youtube_client_configured": bool(
                YOUTUBE_CLIENT_ID
                and YOUTUBE_CLIENT_SECRET
                and YOUTUBE_REDIRECT_URI
            ),

            "youtube_authorized": bool(
                YOUTUBE_REFRESH_TOKEN
            ),

            "ffmpeg": FFMPEG_EXE,

            "deno_on_path": (
                shutil.which(
                    "deno"
                )
            ),

            "deno_cached": (
                str(DENO_EXE)
                if DENO_EXE.exists()
                else None
            ),
        }
    )


# ============================================================
# OAUTH ROUTES
# ============================================================

@mcp.custom_route(
    "/oauth/start",
    methods=["GET"],
)
async def oauth_start(
    request: Request,
) -> Response:

    try:

        flow = _oauth_flow()

    except Exception as exc:

        return JSONResponse(
            {
                "error": str(exc)
            },
            status_code=500,
        )

    authorization_url, state = (
        flow.authorization_url(
            access_type="offline",
            include_granted_scopes=(
                "true"
            ),
            prompt="consent",
        )
    )

    response = RedirectResponse(
        authorization_url,
        status_code=302,
    )

    response.set_cookie(
        "youtube_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax",
    )

    return response


@mcp.custom_route(
    "/oauth/callback",
    methods=["GET"],
)
async def oauth_callback(
    request: Request,
) -> Response:

    oauth_error = (
        request.query_params.get(
            "error"
        )
    )

    if oauth_error:

        return HTMLResponse(
            (
                "<h2>Authorization failed</h2>"
                f"<p>{html.escape(oauth_error)}</p>"
            ),
            status_code=400,
        )

    state = (
        request.query_params.get(
            "state"
        )
    )

    expected_state = (
        request.cookies.get(
            "youtube_oauth_state"
        )
    )

    if (
        not state
        or not expected_state
        or not hmac.compare_digest(
            state,
            expected_state,
        )
    ):

        return HTMLResponse(
            (
                "<h2>Invalid OAuth state.</h2>"
                "<p>Start again from "
                "<a href='/oauth/start'>"
                "/oauth/start"
                "</a>.</p>"
            ),
            status_code=400,
        )

    code = (
        request.query_params.get(
            "code"
        )
    )

    if not code:

        return HTMLResponse(
            "<h2>Missing authorization code.</h2>",
            status_code=400,
        )

    try:

        flow = _oauth_flow(
            state=state
        )

        flow.fetch_token(
            code=code
        )

        refresh_token = (
            flow.credentials
            .refresh_token
        )

    except Exception as exc:

        return HTMLResponse(
            (
                "<h2>Token exchange failed.</h2>"
                f"<pre>{html.escape(str(exc))}</pre>"
            ),
            status_code=500,
        )

    if not refresh_token:

        return HTMLResponse(
            (
                "<h2>No refresh token returned.</h2>"
                "<p>Start again from "
                "<a href='/oauth/start'>"
                "/oauth/start"
                "</a>.</p>"
            ),
            status_code=500,
        )

    response = HTMLResponse(
        (
            "<h2>YouTube authorization "
            "succeeded ✅</h2>"
            "<p>Save this value directly "
            "in Render as "
            "<b>YOUTUBE_REFRESH_TOKEN</b>."
            "</p>"
            "<textarea "
            "style='width:95%;height:140px;"
            "font-family:monospace;'>"
            + html.escape(
                refresh_token
            )
            + "</textarea>"
            "<p><b>Keep this token secret. "
            "Do not paste it into chat "
            "or GitHub.</b></p>"
            "<p>After saving it in Render, "
            "redeploy the service.</p>"
        )
    )

    response.delete_cookie(
        "youtube_oauth_state"
    )

    response.headers[
        "Cache-Control"
    ] = "no-store"

    return response


# ============================================================
# MEDIA DOWNLOAD ROUTE
# ============================================================

@mcp.custom_route(
    "/media/{token}/{filename}",
    methods=["GET"],
)
async def media_download_route(
    request: Request,
) -> Response:

    _cleanup_expired_media()

    token = (
        request.path_params[
            "token"
        ]
    )

    filename = (
        request.path_params[
            "filename"
        ]
    )

    info = MEDIA_TOKENS.get(
        token
    )

    if (
        not info
        or info["expires"]
        <= time.time()
    ):

        return JSONResponse(
            {
                "error": (
                    "Media link expired "
                    "or not found."
                )
            },
            status_code=404,
        )

    path = Path(
        info["path"]
    )

    if (
        not path.exists()
        or path.name != filename
    ):

        return JSONResponse(
            {
                "error": (
                    "Media file not found."
                )
            },
            status_code=404,
        )

    return FileResponse(
        str(path),
        filename=path.name,
        headers={
            "Cache-Control": (
                "private, max-age=300"
            )
        },
    )


# ============================================================
# MCP AUTH
# ============================================================

class BearerAuthMiddleware:

    def __init__(
        self,
        app: ASGIApp,
    ) -> None:

        self.app = app


    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ):

        public_prefixes = (
            "/health",
            "/oauth/start",
            "/oauth/callback",
            "/media/",
        )

        if (
            scope["type"] != "http"
            or any(
                scope["path"].startswith(p)
                for p in public_prefixes
            )
        ):

            await self.app(
                scope,
                receive,
                send,
            )

            return

        headers = dict(
            scope.get(
                "headers",
                [],
            )
        )

        auth = (
            headers.get(
                b"authorization",
                b"",
            )
            .decode()
        )

        if (
            MCP_API_TOKEN
            and hmac.compare_digest(
                auth,
                f"Bearer {MCP_API_TOKEN}",
            )
        ):

            await self.app(
                scope,
                receive,
                send,
            )

            return

        response = JSONResponse(
            {
                "jsonrpc": "2.0",
                "error": {
                    "code": -32001,
                    "message": (
                        "Unauthorized"
                    ),
                },
                "id": None,
            },
            status_code=401,
        )

        await response(
            scope,
            receive,
            send,
        )


# ============================================================
# APP
# ============================================================

def create_app():

    app = (
        mcp.streamable_http_app()
    )

    if MCP_API_TOKEN:

        app.add_middleware(
            BearerAuthMiddleware
        )

    return app


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    import uvicorn

    if not MCP_API_TOKEN:

        print(
            "WARNING: MCP_API_TOKEN "
            "is not set. "
            "The MCP endpoint is running "
            "without authentication."
        )

    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=port,
    )
