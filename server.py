"""
Thin MCP8 entrypoint.

The stable YouTube/media implementation lives in server_base.py.
This module extends it with research-oriented tools for building
viral-vs-control datasets, editing metrics, and a public-video downloader
that does not reuse authenticated YouTube cookies.
"""

import json
import math
import re
import statistics
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import server_base as _base
from server_base import *  # re-export the existing MCP app/tools for Render

SERVER_BUILD = "2026-09-05-research-dataset-v12-public-download"
_base.SERVER_BUILD = SERVER_BUILD


def _combined_server_fingerprint() -> dict:
    """Report the wrapper and preserved base implementation together."""
    try:
        wrapper_path = Path(__file__).resolve()
        base_path = Path(_base.__file__).resolve()
        wrapper_bytes = wrapper_path.read_bytes()
        base_bytes = base_path.read_bytes()

        import hashlib

        digest = hashlib.sha256()
        digest.update(wrapper_bytes)
        digest.update(b"\0")
        digest.update(base_bytes)
        return {
            "path": str(wrapper_path),
            "sha256": digest.hexdigest(),
            "size_bytes": len(wrapper_bytes) + len(base_bytes),
            "mtime_unix": max(wrapper_path.stat().st_mtime, base_path.stat().st_mtime),
            "wrapper_path": str(wrapper_path),
            "base_path": str(base_path),
            "wrapper_sha256": hashlib.sha256(wrapper_bytes).hexdigest(),
            "base_sha256": hashlib.sha256(base_bytes).hexdigest(),
        }
    except Exception as exc:
        return {"error": str(exc)}


_base._server_file_fingerprint = _combined_server_fingerprint


def _parse_published_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _percentile_rank(values: list[float], value: float) -> float:
    if len(values) <= 1:
        return 0.5
    ordered = sorted(values)
    below = sum(1 for item in ordered if item < value)
    equal = sum(1 for item in ordered if item == value)
    return (below + max(equal - 1, 0) / 2) / (len(ordered) - 1)


def _research_metrics(video: dict, subscriber_count: int | None, now: datetime) -> dict:
    snippet = video.get("snippet", {})
    stats = video.get("statistics", {})
    published = _parse_published_at(snippet.get("publishedAt"))

    age_days = None
    if published is not None:
        age_days = max((now - published).total_seconds() / 86400.0, 1.0 / 24.0)

    views = _safe_int(stats.get("viewCount"))
    likes = _safe_int(stats.get("likeCount"))
    comments = _safe_int(stats.get("commentCount"))

    views_per_day = (views / age_days) if age_days else 0.0
    like_rate = (likes / views) if views else 0.0
    comments_per_1000_views = (comments * 1000.0 / views) if views else 0.0
    views_per_subscriber = (
        views / subscriber_count
        if subscriber_count and subscriber_count > 0
        else None
    )

    score = math.log1p(views_per_day)
    score += 0.35 * math.log1p(max(like_rate * 1000.0, 0.0))
    score += 0.15 * math.log1p(max(comments_per_1000_views, 0.0))
    if views_per_subscriber is not None:
        score += 0.25 * math.log1p(max(views_per_subscriber, 0.0))

    return {
        "age_days": round(age_days, 3) if age_days is not None else None,
        "views": views,
        "likes": likes,
        "comments": comments,
        "views_per_day": round(views_per_day, 3),
        "like_rate": round(like_rate, 6),
        "comments_per_1000_views": round(comments_per_1000_views, 3),
        "channel_subscribers": subscriber_count,
        "views_per_subscriber": (
            round(views_per_subscriber, 6)
            if views_per_subscriber is not None
            else None
        ),
        "performance_score": round(score, 6),
    }


@mcp.tool()
def youtube_build_research_set(
    query: str,
    max_results_per_pool: int = 25,
    video_duration: str = "short",
    region_code: str = "US",
    relevance_language: str = "en",
    published_after: str | None = None,
) -> dict:
    """Build a deduplicated viral/control candidate set with normalized metrics."""
    if not query or not query.strip():
        raise ValueError("query must not be empty.")
    if video_duration not in {"any", "short", "medium", "long"}:
        raise ValueError("video_duration must be any, short, medium, or long.")
    if not re.fullmatch(r"[A-Za-z]{2}", region_code or ""):
        raise ValueError("region_code must be a two-letter country code such as US.")

    limit = max(5, min(int(max_results_per_pool), 50))
    youtube = _base._youtube()
    candidate_sources: dict[str, set[str]] = {}

    for pool_name, order in (("high_view", "viewCount"), ("control", "relevance")):
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query.strip(),
            "type": "video",
            "maxResults": limit,
            "order": order,
            "videoDuration": video_duration,
            "regionCode": region_code.upper(),
            "relevanceLanguage": relevance_language,
            "safeSearch": "moderate",
        }
        if published_after:
            params["publishedAfter"] = published_after

        result = youtube.search().list(**params).execute()
        for item in result.get("items", []):
            video_id = item.get("id", {}).get("videoId")
            if video_id:
                candidate_sources.setdefault(video_id, set()).add(pool_name)

    video_ids = list(candidate_sources)
    if not video_ids:
        return {
            "query": query.strip(),
            "count": 0,
            "videos": [],
            "label_method": "relative_within_returned_candidate_pool",
        }

    details: list[dict] = []
    for start in range(0, len(video_ids), 50):
        response = youtube.videos().list(
            part="snippet,contentDetails,status,statistics,liveStreamingDetails",
            id=",".join(video_ids[start:start + 50]),
        ).execute()
        details.extend(response.get("items", []))

    details = [
        video
        for video in details
        if video.get("status", {}).get("privacyStatus") == "public"
        and video.get("snippet", {}).get("liveBroadcastContent") == "none"
        and not video.get("liveStreamingDetails")
    ]

    channel_ids = sorted({
        video.get("snippet", {}).get("channelId")
        for video in details
        if video.get("snippet", {}).get("channelId")
    })
    subscribers: dict[str, int | None] = {}
    for start in range(0, len(channel_ids), 50):
        channel_result = youtube.channels().list(
            part="statistics",
            id=",".join(channel_ids[start:start + 50]),
        ).execute()
        for channel in channel_result.get("items", []):
            channel_stats = channel.get("statistics", {})
            hidden = bool(channel_stats.get("hiddenSubscriberCount"))
            subscribers[channel["id"]] = (
                None if hidden else _safe_int(channel_stats.get("subscriberCount"))
            )

    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for video in details:
        video_id = video["id"]
        snippet = video.get("snippet", {})
        duration_seconds = _base._iso8601_duration_seconds(
            video.get("contentDetails", {}).get("duration")
        )
        metrics = _research_metrics(
            video,
            subscribers.get(snippet.get("channelId")),
            now,
        )
        rows.append({
            "video_id": video_id,
            "title": snippet.get("title"),
            "channel_id": snippet.get("channelId"),
            "channel_title": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
            "duration_seconds": duration_seconds,
            "watch_url": f"https://www.youtube.com/watch?v={video_id}",
            "candidate_pools": sorted(candidate_sources.get(video_id, set())),
            **metrics,
        })

    scores = [float(row["performance_score"]) for row in rows]
    for row in rows:
        percentile = _percentile_rank(scores, float(row["performance_score"]))
        if percentile >= 0.70:
            label = "viral_candidate"
        elif percentile <= 0.30:
            label = "nonviral_control"
        else:
            label = "middle_control"
        row["performance_percentile"] = round(percentile, 4)
        row["research_label"] = label

    rows.sort(
        key=lambda row: (
            row["research_label"] != "viral_candidate",
            -float(row["performance_score"]),
        )
    )
    label_counts: dict[str, int] = {}
    for row in rows:
        label_counts[row["research_label"]] = label_counts.get(row["research_label"], 0) + 1

    return {
        "query": query.strip(),
        "count": len(rows),
        "label_counts": label_counts,
        "label_method": (
            "Relative ranking within this returned candidate pool using "
            "views/day, engagement, and views/subscriber when public. "
            "It is a research heuristic, not an absolute YouTube virality claim."
        ),
        "download_tool": "video_download_public_video",
        "analysis_tools": [
            "video_extract_frames",
            "video_extract_audio",
            "video_detect_scenes",
            "video_editing_metrics",
        ],
        "videos": rows,
    }


def _probe_duration_seconds(path: Path) -> float | None:
    result = subprocess.run(
        [_base.FFMPEG_EXE, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    text = result.stderr or ""
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _scene_timestamps(path: Path, threshold: float, max_scenes: int) -> list[float]:
    threshold = max(0.01, min(float(threshold), 0.99))
    max_scenes = max(1, min(int(max_scenes), 500))
    filter_expr = f"select='gt(scene,{threshold})',showinfo"
    result = subprocess.run(
        [
            _base.FFMPEG_EXE,
            "-hide_banner",
            "-i",
            str(path),
            "-vf",
            filter_expr,
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError("ffmpeg scene detection failed:\n" + (result.stderr or "")[-5000:])

    timestamps: list[float] = []
    for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr or ""):
        value = float(match.group(1))
        if not timestamps or abs(value - timestamps[-1]) > 0.02:
            timestamps.append(value)
        if len(timestamps) >= max_scenes:
            break
    return timestamps


@mcp.tool()
def video_detect_scenes(
    media_id: str,
    threshold: float = 0.30,
    max_scenes: int = 120,
) -> dict:
    """Detect likely visual scene changes and return their timestamps."""
    path = _base._resolve_media(media_id)
    timestamps = _scene_timestamps(path, threshold, max_scenes)
    duration = _probe_duration_seconds(path)
    return {
        "media_id": media_id,
        "filename": path.name,
        "threshold": max(0.01, min(float(threshold), 0.99)),
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "scene_change_count": len(timestamps),
        "scene_change_timestamps": [round(value, 3) for value in timestamps],
    }


@mcp.tool()
def video_editing_metrics(
    media_id: str,
    scene_threshold: float = 0.30,
    max_scenes: int = 240,
) -> dict:
    """Measure editing pace from scene-change timing for dataset comparison."""
    path = _base._resolve_media(media_id)
    duration = _probe_duration_seconds(path)
    timestamps = _scene_timestamps(path, scene_threshold, max_scenes)

    boundaries = [0.0, *timestamps]
    if duration is not None and duration > 0:
        boundaries.append(duration)

    shot_lengths = [
        max(0.0, boundaries[index + 1] - boundaries[index])
        for index in range(len(boundaries) - 1)
        if boundaries[index + 1] > boundaries[index]
    ]

    cuts_per_minute = None
    if duration and duration > 0:
        cuts_per_minute = len(timestamps) * 60.0 / duration

    return {
        "media_id": media_id,
        "filename": path.name,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "scene_threshold": max(0.01, min(float(scene_threshold), 0.99)),
        "scene_change_count": len(timestamps),
        "cuts_per_minute": round(cuts_per_minute, 3) if cuts_per_minute is not None else None,
        "mean_shot_length_seconds": round(statistics.fmean(shot_lengths), 3) if shot_lengths else None,
        "median_shot_length_seconds": round(statistics.median(shot_lengths), 3) if shot_lengths else None,
        "shortest_shot_seconds": round(min(shot_lengths), 3) if shot_lengths else None,
        "longest_shot_seconds": round(max(shot_lengths), 3) if shot_lengths else None,
        "scene_change_timestamps": [round(value, 3) for value in timestamps],
        "note": (
            "Scene detection is a visual-change heuristic; camera motion, flashes, "
            "and animation can create detections that are not editorial cuts."
        ),
    }


@mcp.tool()
def dataset_export_jsonl(
    name: str,
    rows: list[dict[str, Any]],
) -> dict:
    """Export analyzed research rows as temporary JSONL for reuse or Library storage."""
    if not rows:
        raise ValueError("rows must contain at least one dataset record.")
    if len(rows) > 500:
        raise ValueError("rows is limited to 500 records per export.")

    safe_name = _base._safe_filename(name or "youtube_research_dataset")
    if not safe_name.endswith(".jsonl"):
        safe_name += ".jsonl"

    output = _base.MEDIA_DIR / f"dataset_{uuid.uuid4().hex}_{safe_name}"
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    published = _base._publish_media(output)
    published.update({
        "row_count": len(rows),
        "format": "jsonl",
        "purpose": "youtube_viral_vs_control_research",
    })
    return published


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
    }

    proxy = _base._youtube_proxy_url()
    if proxy:
        options["proxy"] = proxy

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

    attempts: list[tuple[str, bool, bool]] = []
    if embeddable:
        attempts.append(("web_embedded", False, False))
    attempts.append(("tv", False, False))
    if not made_for_kids:
        attempts.append(("android_vr", False, False))
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
            published.update({
                "video_id": video_id,
                "title": snippet.get("title"),
                "source_watch_url": watch_url,
                "duration_seconds": info.get("duration") or duration,
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "max_height": max_height,
                "download_profile": player_client,
                "cookies_used": False,
                "public_analysis_download": True,
            })
            return published
        except Exception as exc:
            failures.append({
                "player_client": player_client,
                "skip_webpage": skip_webpage,
                "impersonate": impersonate,
                "error": _base._redact_proxy_secrets(str(exc))[-1200:],
            })
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


# server_base already registered the v10 public downloader. Replace only that tool,
# keeping the exact same public MCP name and arguments.
try:
    mcp.remove_tool("video_download_public_video")
except Exception as exc:
    raise RuntimeError("Could not replace inherited video_download_public_video tool.") from exc


@mcp.tool(name="video_download_public_video")
def video_download_public_video(
    video_id_or_url: str,
    max_height: int = 720,
    max_duration_seconds: int = 1800,
) -> dict:
    """Download an unrestricted public YouTube video for permitted analysis."""
    return _download_public_video_impl_v12(
        video_id_or_url,
        max_height=max_height,
        max_duration_seconds=max_duration_seconds,
    )


@mcp.tool()
def research_tooling_status() -> dict:
    """Describe the research extension currently loaded by MCP8."""
    return {
        "server_build": SERVER_BUILD,
        "tools": [
            "youtube_build_research_set",
            "video_download_public_video",
            "video_extract_frames",
            "video_extract_audio",
            "video_detect_scenes",
            "video_editing_metrics",
            "dataset_export_jsonl",
        ],
        "dataset_strategy": (
            "Search -> normalized candidate ranking -> public-video import -> "
            "dense visual/audio inspection -> scene/edit metrics -> JSONL export."
        ),
    }


if __name__ == "__main__":
    import uvicorn

    if not _base.MCP_API_TOKEN:
        print("WARNING: MCP_API_TOKEN is not set. The MCP endpoint is running without authentication.")
    port = int(_base.os.environ.get("PORT", "10000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)
