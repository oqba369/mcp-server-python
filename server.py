#!/usr/bin/env python3
"""Patch MCP8 server.py to use a no-cookie fallback ladder for unrestricted public YouTube analysis.

Usage from the repository root:
    python patch_mcp8_public_downloader_v12.py

The patch is intentionally limited to server.py. It leaves server_base.py and the
owned/authenticated downloader unchanged.
"""
from __future__ import annotations

import ast
from pathlib import Path

TARGET = Path("server.py")
OLD_BUILD = 'SERVER_BUILD = "2026-09-05-research-dataset-v11"'
NEW_BUILD = 'SERVER_BUILD = "2026-09-05-research-dataset-v12-public-download"'
MARKER = "# MCP8_PUBLIC_DOWNLOADER_V12"

OVERRIDE = r'''

# MCP8_PUBLIC_DOWNLOADER_V12
# Public-video analysis deliberately does not reuse the authenticated cookie jar.
# Cookies remain available to server_base.video_download_my_video and other owned-video flows.
def _public_youtube_ydl_options(
    outtmpl: str,
    max_height: int,
    player_client: str,
    *,
    skip_webpage: bool = False,
    impersonate: bool = False,
) -> dict[str, Any]:
    deno = _base._ensure_deno()
    youtube_args: dict[str, list[str]] = {
        "player_client": [player_client],
    }
    if skip_webpage:
        youtube_args["player_skip"] = ["webpage", "configs"]

    options: dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": (
            f"bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}]/b"
        ),
        "merge_output_format": "mp4",
        "ffmpeg_location": _base.FFMPEG_EXE,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "js_runtimes": {"deno": {"path": str(deno)}},
        "remote_components": {"ejs:npm"},
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {"youtube": youtube_args},
        # IMPORTANT: no cookiefile here. Public unrestricted downloads should not
        # inherit the account-authenticated cookie jar from _youtube_ydl_options().
    }

    proxy = _base._youtube_proxy_url()
    if proxy:
        options["proxy"] = proxy

    # curl-cffi is already installed in this service. Use browser impersonation
    # only on fallback attempts so the ordinary path remains simple/stable.
    if impersonate:
        options["impersonate"] = "chrome"

    return options


def _clean_public_attempt_files(base: Path) -> None:
    for candidate in _base.MEDIA_DIR.glob(base.name + ".*"):
        try:
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        except OSError:
            pass


def _download_public_video_impl_v12(
    video_id_or_url: str,
    max_height: int = 720,
    max_duration_seconds: int = 1800,
) -> dict:
    video_id = _base._extract_youtube_video_id(video_id_or_url)
    max_height = max(144, min(int(max_height), 2160))
    max_duration_seconds = max(1, min(int(max_duration_seconds), 7200))

    # Use the official API only for metadata/restriction checks. This does not
    # supply playback cookies to yt-dlp.
    response = _base._youtube().videos().list(
        part="snippet,contentDetails,status,liveStreamingDetails",
        id=video_id,
    ).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError(f"Video not found: {video_id}")

    video = items[0]
    snippet = video.get("snippet", {})
    status = video.get("status", {})
    content = video.get("contentDetails", {})

    if status.get("privacyStatus") != "public":
        raise PermissionError("video_download_public_video only accepts public videos.")
    if snippet.get("liveBroadcastContent") != "none" or video.get("liveStreamingDetails"):
        raise PermissionError("Live or scheduled YouTube videos are not accepted by this analysis downloader.")

    duration = _base._iso8601_duration_seconds(content.get("duration"))
    if duration is not None and duration > max_duration_seconds:
        raise ValueError(
            f"Video duration is {duration}s, above max_duration_seconds={max_duration_seconds}."
        )

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    embeddable = bool(status.get("embeddable"))
    made_for_kids = bool(status.get("madeForKids"))

    # Current yt-dlp YouTube clients differ in PO-token requirements. For a
    # genuinely public, unrestricted analysis target, first prefer clients that
    # can operate without the authenticated cookie jar. web_embedded is tried
    # only when YouTube itself marks the video embeddable.
    attempts: list[tuple[str, bool, bool]] = []
    if embeddable:
        attempts.append(("web_embedded", False, False))
    attempts.append(("tv", False, False))
    if not made_for_kids:
        attempts.append(("android_vr", False, False))

    # If YouTube challenges the initial webpage/config request, retry the two
    # most useful public clients while avoiding that initial webpage fetch and
    # using curl-cffi browser impersonation. Still no cookies are supplied.
    if embeddable:
        attempts.append(("web_embedded", True, True))
    attempts.append(("tv", True, True))

    failures: list[dict[str, str | bool]] = []
    for player_client, skip_webpage, impersonate in attempts:
        base = _base.MEDIA_DIR / f"ytpub_{video_id}_{uuid.uuid4().hex}"
        outtmpl = str(base) + ".%(ext)s"
        try:
            options = _public_youtube_ydl_options(
                outtmpl,
                max_height,
                player_client,
                skip_webpage=skip_webpage,
                impersonate=impersonate,
            )
            with _base.yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(watch_url, download=True)

            candidates = [
                path
                for path in _base.MEDIA_DIR.glob(base.name + ".*")
                if path.is_file()
                and not path.name.endswith((".part", ".ytdl", ".temp"))
            ]
            candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            if not candidates:
                raise RuntimeError("yt-dlp completed but produced no final media file.")

            path = candidates[0]
            published = _base._publish_media(path)
            published.update(
                {
                    "video_id": video_id,
                    "title": snippet.get("title"),
                    "source_watch_url": watch_url,
                    "duration_seconds": info.get("duration") or duration,
                    "extractor": info.get("extractor_key") or info.get("extractor"),
                    "max_height": max_height,
                    "download_profile": player_client,
                    "cookies_used": False,
                    "public_analysis_download": True,
                }
            )
            return published
        except Exception as exc:
            failures.append(
                {
                    "player_client": player_client,
                    "skip_webpage": skip_webpage,
                    "impersonate": impersonate,
                    "error": _base._redact_proxy_secrets(str(exc))[-1200:],
                }
            )
            _clean_public_attempt_files(base)

    short_failures = " | ".join(
        f"{item['player_client']}"
        f"{'/skip' if item['skip_webpage'] else ''}: {item['error']}"
        for item in failures
    )
    raise RuntimeError(
        "Could not download this unrestricted public YouTube video after the no-cookie "
        "public-client fallback ladder. " + short_failures
    )


# server.py imports the base tool first. Replace only this one registration while
# preserving the same public MCP tool name and arguments, so reconnecting the
# connector should not be necessary merely for this implementation change.
try:
    mcp.remove_tool("video_download_public_video")
except Exception:
    # FastMCP >=1.27 exposes remove_tool publicly. If a future build changes this,
    # fail loudly rather than silently retaining the broken inherited tool.
    raise RuntimeError("Could not replace inherited video_download_public_video tool.")


@mcp.tool(name="video_download_public_video")
def video_download_public_video(
    video_id_or_url: str,
    max_height: int = 720,
    max_duration_seconds: int = 1800,
) -> dict:
    """Download an unrestricted public YouTube video for permitted analysis.

    This public path intentionally avoids authenticated YouTube cookies. It does
    not bypass private, members-only, paid, DRM-protected, age-restricted, or live
    content. Pass the returned media_id to the existing video analysis tools.
    """
    return _download_public_video_impl_v12(
        video_id_or_url,
        max_height=max_height,
        max_duration_seconds=max_duration_seconds,
    )
'''


def main() -> None:
    if not TARGET.exists():
        raise SystemExit(f"{TARGET} not found. Run this script from the repository root.")

    source = TARGET.read_text(encoding="utf-8")
    if MARKER in source:
        print("server.py already contains MCP8 public downloader v12; no changes made.")
        return

    if OLD_BUILD in source:
        source = source.replace(OLD_BUILD, NEW_BUILD, 1)
    elif NEW_BUILD not in source:
        raise SystemExit("Expected v11 SERVER_BUILD marker was not found; refusing to patch an unknown revision.")

    anchor = '\nif __name__ == "__main__":\n'
    if anchor not in source:
        raise SystemExit('Could not find final if __name__ == "__main__" anchor.')

    patched = source.replace(anchor, OVERRIDE + anchor, 1)
    ast.parse(patched, filename=str(TARGET))

    backup = TARGET.with_suffix(".py.v11.bak")
    if not backup.exists():
        backup.write_text(TARGET.read_text(encoding="utf-8"), encoding="utf-8")
    TARGET.write_text(patched, encoding="utf-8")
    print(f"Patched {TARGET} successfully; syntax check passed. Backup: {backup}")


if __name__ == "__main__":
    main()
