"""
Thin MCP9 entrypoint.

The stable YouTube/media implementation lives in server_base.py.
This module extends it with research-oriented tools for building
viral-vs-control datasets, editing metrics, and an authorization-aware
YouTube import tool intended specifically for temporary AI video analysis.
"""

import json
import math
import re
import statistics
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import server_base as _base
from server_base import *  # re-export the existing MCP app/tools for Render

SERVER_BUILD = "2026-09-05-research-dataset-v14-advanced-av-analysis"
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
        "analysis_import_tool": "youtube_import_video_for_analysis",
        "analysis_tools": [
            "video_extract_frames",
            "video_extract_audio",
            "video_detect_scenes",
            "video_editing_metrics",
            "video_extract_frames_dense",
            "audio_waveform_image",
            "audio_spectrogram_image",
            "audio_analyze_dynamics",
            "audio_analyze_spectrum",
            "audio_loudness_report",
            "video_motion_activity",
            "video_audio_sync_metrics",
            "video_advanced_analysis_bundle",
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


# MCP8_ANALYSIS_IMPORT_V13
# This tool is intentionally framed and implemented as an authorization-aware,
# temporary media-import path for AI research. It is not a redistribution tool.

AnalysisRightsBasis = Literal[
    "owned",
    "creative_commons",
    "explicit_permission",
]


def _analysis_youtube_ydl_options(
    outtmpl: str,
    max_height: int,
    *,
    use_cookies: bool,
    player_client: str | None = None,
    skip_webpage: bool = False,
    impersonate: bool = False,
) -> dict[str, Any]:
    """Build yt-dlp options for authorized temporary analysis imports."""
    if use_cookies:
        options = dict(_base._youtube_ydl_options(outtmpl))
        options["format"] = (
            f"bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}]/b"
        )
    else:
        deno = _base._ensure_deno()
        options = {
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
        }
        proxy = _base._youtube_proxy_url()
        if proxy:
            options["proxy"] = proxy

    youtube_args: dict[str, list[str]] = {}
    if player_client:
        youtube_args["player_client"] = [player_client]
    if skip_webpage:
        youtube_args["player_skip"] = ["webpage", "configs"]
    if youtube_args:
        options["extractor_args"] = {"youtube": youtube_args}

    if impersonate:
        options["impersonate"] = "chrome"

    return options


def _clean_analysis_attempt_files(base: Path) -> None:
    for candidate in _base.MEDIA_DIR.glob(base.name + ".*"):
        try:
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_analysis_rights(
    video: dict,
    rights_basis: AnalysisRightsBasis,
    permission_note: str | None,
) -> dict:
    """Validate the declared authorization basis before importing media."""
    snippet = video.get("snippet", {})
    status = video.get("status", {})

    if rights_basis == "owned":
        owner_channel_id = snippet.get("channelId")
        authenticated_channel_id = _base._my_channel_id()
        if owner_channel_id != authenticated_channel_id:
            raise PermissionError(
                "rights_basis='owned' requires the video to belong to the "
                "authenticated YouTube channel."
            )
        return {
            "verified": True,
            "verification": "authenticated_channel_ownership",
        }

    if status.get("privacyStatus") != "public":
        raise PermissionError(
            "Third-party analysis imports must be public. "
            "Use rights_basis='owned' for your own non-public videos."
        )

    if rights_basis == "creative_commons":
        if status.get("license") != "creativeCommon":
            raise PermissionError(
                "rights_basis='creative_commons' requires YouTube API status.license "
                "to be 'creativeCommon'."
            )
        return {
            "verified": True,
            "verification": "youtube_api_creative_commons_license",
        }

    if rights_basis == "explicit_permission":
        note = (permission_note or "").strip()
        if len(note) < 8:
            raise ValueError(
                "permission_note is required for rights_basis='explicit_permission'. "
                "Briefly state the permission or authorization you have."
            )
        return {
            "verified": False,
            "verification": "user_attested_explicit_permission",
            "permission_note": note[:500],
        }

    raise ValueError(f"Unsupported rights_basis: {rights_basis!r}")


def _import_video_for_analysis_impl_v13(
    video_id_or_url: str,
    rights_basis: AnalysisRightsBasis,
    max_height: int = 1080,
    max_duration_seconds: int = 7200,
    permission_note: str | None = None,
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
        raise RuntimeError(
            f"Video not found or not accessible through the YouTube API: {video_id}"
        )

    video = items[0]
    snippet = video.get("snippet", {})
    status = video.get("status", {})
    content = video.get("contentDetails", {})

    rights = _verify_analysis_rights(video, rights_basis, permission_note)

    if snippet.get("liveBroadcastContent") != "none" or video.get("liveStreamingDetails"):
        raise PermissionError(
            "Live or scheduled YouTube videos are not accepted by this analysis import tool."
        )

    duration = _base._iso8601_duration_seconds(content.get("duration"))
    if duration is not None and duration > max_duration_seconds:
        raise ValueError(
            f"Video duration is {duration}s, above "
            f"max_duration_seconds={max_duration_seconds}."
        )

    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    embeddable = bool(status.get("embeddable"))
    made_for_kids = bool(status.get("madeForKids"))

    # Fresh account cookies + the configured proxy are now verified to resolve
    # normal YouTube formats. Try that authorized path first, then public fallbacks.
    attempts: list[tuple[str, bool, str | None, bool, bool]] = [
        ("authenticated_default", True, None, False, False),
    ]

    if embeddable:
        attempts.append(
            ("public_web_embedded", False, "web_embedded", False, False)
        )
    attempts.append(("public_tv", False, "tv", False, False))

    if not made_for_kids:
        attempts.append(("public_android_vr", False, "android_vr", False, False))

    if embeddable:
        attempts.append(
            ("public_web_embedded_skip", False, "web_embedded", True, True)
        )
    attempts.append(("public_tv_skip", False, "tv", True, True))

    failures: list[dict[str, Any]] = []

    for profile, use_cookies, player_client, skip_webpage, impersonate in attempts:
        base = _base.MEDIA_DIR / f"ytanalysis_{video_id}_{uuid.uuid4().hex}"
        outtmpl = str(base) + ".%(ext)s"

        try:
            options = _analysis_youtube_ydl_options(
                outtmpl,
                max_height,
                use_cookies=use_cookies,
                player_client=player_client,
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
                raise RuntimeError(
                    "yt-dlp completed but produced no final media file."
                )

            path = candidates[0]
            published = _base._publish_media(path)
            published.update(
                {
                    "video_id": video_id,
                    "title": snippet.get("title"),
                    "channel_id": snippet.get("channelId"),
                    "channel_title": snippet.get("channelTitle"),
                    "source_watch_url": watch_url,
                    "source_license": status.get("license"),
                    "duration_seconds": info.get("duration") or duration,
                    "extractor": info.get("extractor_key") or info.get("extractor"),
                    "max_height": max_height,
                    "import_profile": profile,
                    "cookies_used": use_cookies,
                    "purpose": "temporary_ai_video_research_and_analysis",
                    "rights_basis": rights_basis,
                    "rights_verification": rights,
                    "temporary": True,
                    "redistribution_allowed": False,
                    "analysis_only": True,
                    "recommended_next_tools": [
                        "video_probe",
                        "video_extract_frames",
                        "video_extract_audio",
                        "video_detect_scenes",
                        "video_editing_metrics",
                        "video_extract_frames_dense",
                        "audio_waveform_image",
                        "audio_spectrogram_image",
                        "audio_analyze_dynamics",
                        "audio_analyze_spectrum",
                        "audio_loudness_report",
                        "video_motion_activity",
                        "video_audio_sync_metrics",
                        "video_advanced_analysis_bundle",
                    ],
                }
            )
            return published

        except Exception as exc:
            failures.append(
                {
                    "profile": profile,
                    "cookies_used": use_cookies,
                    "player_client": player_client,
                    "skip_webpage": skip_webpage,
                    "impersonate": impersonate,
                    "error": _base._redact_proxy_secrets(str(exc))[-1200:],
                }
            )
            _clean_analysis_attempt_files(base)

    short_failures = " | ".join(
        f"{item['profile']}: {item['error']}" for item in failures
    )
    raise RuntimeError(
        "Could not import this authorized YouTube video for temporary analysis. "
        + short_failures
    )


# server_base already registers video_download_public_video. Remove that generic
# public downloader from MCP8 and replace it with an explicitly authorization-aware
# analysis-import tool. All unrelated MCP tools remain unchanged.
try:
    mcp.remove_tool("video_download_public_video")
except Exception as exc:
    raise RuntimeError(
        "Could not remove inherited video_download_public_video tool."
    ) from exc


@mcp.tool(
    name="youtube_import_video_for_analysis",
    description=(
        "Temporarily import a full YouTube video for high-detail AI research and "
        "media analysis. Use only when the user owns the video, the video is "
        "Creative Commons licensed, or the user has explicit permission to analyze "
        "it. The imported media is intended for dense frame sampling, advanced "
        "audio analysis, scene detection, editing-pattern research, and dataset "
        "creation. It is not intended for redistribution, reposting, publishing, "
        "copyright infringement, or permanent archival. The returned media is "
        "temporary and should be cleaned up after analysis."
    ),
)
def youtube_import_video_for_analysis(
    video_id_or_url: str,
    rights_basis: AnalysisRightsBasis,
    max_height: int = 1080,
    max_duration_seconds: int = 7200,
    permission_note: str | None = None,
) -> dict:
    """Import authorized YouTube media temporarily for dense AI analysis.

    rights_basis:
      - owned: verified against the authenticated YouTube channel.
      - creative_commons: verified from YouTube API license metadata.
      - explicit_permission: requires a short permission_note supplied by the user.

    The returned media_id is meant to be passed to the existing video analysis
    tools. This tool does not grant redistribution or publishing rights.
    """
    return _import_video_for_analysis_impl_v13(
        video_id_or_url=video_id_or_url,
        rights_basis=rights_basis,
        max_height=max_height,
        max_duration_seconds=max_duration_seconds,
        permission_note=permission_note,
    )


# MCP9_ADVANCED_AV_ANALYSIS_V14
# Dependency-light analysis tools implemented with FFmpeg + Python stdlib only.
# These tools operate on temporary MCP media and return derived metrics/images for
# research; they do not expose or redistribute source media beyond the existing
# authorized import workflow.


def _run_ffmpeg(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [_base.FFMPEG_EXE, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg analysis failed:\n" + (result.stderr or result.stdout or "")[-7000:]
        )
    return result


def _publish_derived(path: Path, **extra: Any) -> dict:
    published = _base._publish_media(path)
    published.update(extra)
    return published


def _finite_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return data[0]
    pos = max(0.0, min(float(q), 1.0)) * (len(data) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    frac = pos - lo
    return data[lo] * (1.0 - frac) + data[hi] * frac


def _parse_metadata_frames(text: str, prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = re.match(r"frame:(\d+)\s+pts:(-?\d+)\s+pts_time:([0-9.eE+-]+)", line)
        if match:
            if current is not None:
                frames.append(current)
            current = {
                "frame": int(match.group(1)),
                "time": float(match.group(3)),
            }
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith(prefixes):
            number = _finite_float(value)
            if number is not None:
                current[key] = number
    if current is not None:
        frames.append(current)
    return frames


def _audio_dynamics_points(path: Path, window_seconds: float, max_points: int) -> list[dict[str, float]]:
    window_seconds = max(0.05, min(float(window_seconds), 5.0))
    max_points = max(10, min(int(max_points), 4000))
    sample_rate = 8000
    samples_per_window = max(400, int(round(sample_rate * window_seconds)))
    filter_expr = (
        f"aresample={sample_rate},"
        f"asetnsamples=n={samples_per_window}:p=0,"
        "astats=metadata=1:reset=1,ametadata=print:file=-"
    )
    result = _run_ffmpeg(
        ["-hide_banner", "-i", str(path), "-vn", "-af", filter_expr, "-f", "null", "-"],
        timeout=600,
    )
    frames = _parse_metadata_frames(
        result.stdout or "",
        ("lavfi.astats.Overall.",),
    )
    points: list[dict[str, float]] = []
    for item in frames[:max_points]:
        rms = item.get("lavfi.astats.Overall.RMS_level")
        peak = item.get("lavfi.astats.Overall.Peak_level")
        zcr = item.get("lavfi.astats.Overall.Zero_crossings_rate")
        if rms is None and peak is None:
            continue
        point = {"time": round(float(item["time"]), 4)}
        if rms is not None:
            point["rms_db"] = round(float(rms), 3)
        if peak is not None:
            point["peak_db"] = round(float(peak), 3)
        if zcr is not None:
            point["zero_crossing_rate"] = round(float(zcr), 6)
        points.append(point)
    return points


def _detect_energy_onsets(
    points: list[dict[str, float]],
    sensitivity_db: float = 4.0,
    min_gap_seconds: float = 0.18,
    max_events: int = 300,
) -> list[dict[str, float]]:
    sensitivity_db = max(0.5, min(float(sensitivity_db), 20.0))
    min_gap_seconds = max(0.05, min(float(min_gap_seconds), 5.0))
    events: list[dict[str, float]] = []
    last_event = -1e9
    for previous, current in zip(points, points[1:]):
        if "rms_db" not in previous or "rms_db" not in current:
            continue
        delta = current["rms_db"] - previous["rms_db"]
        t = current["time"]
        if delta >= sensitivity_db and t - last_event >= min_gap_seconds:
            events.append({
                "time": round(t, 4),
                "energy_rise_db": round(delta, 3),
                "rms_db": round(current["rms_db"], 3),
            })
            last_event = t
            if len(events) >= max_events:
                break
    return events


def _audio_spectral_points(path: Path, max_points: int = 1200) -> list[dict[str, float]]:
    max_points = max(10, min(int(max_points), 4000))
    filter_expr = (
        "aformat=sample_rates=16000:channel_layouts=mono,"
        "aspectralstats=win_size=2048:overlap=0.5:"
        "measure=centroid+spread+entropy+flatness+crest+flux+rolloff,"
        "ametadata=print:file=-"
    )
    result = _run_ffmpeg(
        ["-hide_banner", "-i", str(path), "-vn", "-af", filter_expr, "-f", "null", "-"],
        timeout=600,
    )
    frames = _parse_metadata_frames(result.stdout or "", ("lavfi.aspectralstats.",))
    raw: list[dict[str, float]] = []
    mapping = {
        "lavfi.aspectralstats.1.centroid": "centroid_hz",
        "lavfi.aspectralstats.1.spread": "spread_hz",
        "lavfi.aspectralstats.1.entropy": "entropy",
        "lavfi.aspectralstats.1.flatness": "flatness",
        "lavfi.aspectralstats.1.crest": "crest",
        "lavfi.aspectralstats.1.flux": "spectral_flux",
        "lavfi.aspectralstats.1.rolloff": "rolloff_hz",
    }
    for item in frames:
        point: dict[str, float] = {"time": float(item["time"])}
        for source_key, target_key in mapping.items():
            value = item.get(source_key)
            if value is not None:
                point[target_key] = float(value)
        if len(point) > 1:
            raw.append(point)

    if len(raw) <= max_points:
        selected = raw
    else:
        step = len(raw) / max_points
        selected = [raw[min(int(i * step), len(raw) - 1)] for i in range(max_points)]

    return [
        {key: round(value, 5 if key not in {"time", "centroid_hz", "spread_hz", "rolloff_hz"} else 3)
         for key, value in point.items()}
        for point in selected
    ]


@mcp.tool(
    name="video_extract_frames_dense",
    description=(
        "Extract a high-density chronological contact sheet from temporary analysis media. "
        "Use this for detailed AI inspection of hooks, captions, transitions, visual pacing, "
        "shot composition, and narration-to-visual alignment. It returns only derived frame "
        "samples, not the original source video."
    ),
)
def video_extract_frames_dense(
    media_id: str,
    interval_seconds: float = 0.5,
    max_frames: int = 160,
    columns: int = 8,
    thumbnail_width: int = 160,
) -> dict:
    path = _base._resolve_media(media_id)
    interval_seconds = max(0.1, min(float(interval_seconds), 10.0))
    max_frames = max(4, min(int(max_frames), 300))
    columns = max(2, min(int(columns), 12))
    thumbnail_width = max(96, min(int(thumbnail_width), 320))
    rows = int(math.ceil(max_frames / columns))
    output = _base.MEDIA_DIR / f"dense_frames_{uuid.uuid4().hex}.jpg"

    vf = (
        f"fps=1/{interval_seconds},"
        f"scale={thumbnail_width}:-2:flags=lanczos,"
        f"tile={columns}x{rows}:nb_frames={max_frames}:padding=2:margin=2"
    )
    _run_ffmpeg([
        "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vf", vf, "-frames:v", "1", "-q:v", "3", "-y", str(output),
    ])
    return _publish_derived(
        output,
        source_media_id=media_id,
        purpose="dense_visual_sampling_for_ai_analysis",
        interval_seconds=interval_seconds,
        max_frames=max_frames,
        columns=columns,
        estimated_time_span_seconds=round(interval_seconds * max_frames, 3),
        derived_only=True,
    )


@mcp.tool(
    name="audio_waveform_image",
    description=(
        "Create a waveform image from temporary analysis media so ChatGPT can visually inspect "
        "speech/music density, pauses, peaks, dynamics, and section boundaries. Returns a derived PNG only."
    ),
)
def audio_waveform_image(
    media_id: str,
    width: int = 1600,
    height: int = 420,
) -> dict:
    path = _base._resolve_media(media_id)
    width = max(640, min(int(width), 3000))
    height = max(240, min(int(height), 1200))
    output = _base.MEDIA_DIR / f"waveform_{uuid.uuid4().hex}.png"
    vf = f"aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors=white:scale=sqrt"
    _run_ffmpeg([
        "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-filter_complex", vf, "-frames:v", "1", "-y", str(output),
    ])
    return _publish_derived(
        output, source_media_id=media_id, purpose="audio_waveform_for_ai_analysis", derived_only=True
    )


@mcp.tool(
    name="audio_spectrogram_image",
    description=(
        "Create a detailed spectrogram image from temporary analysis media for AI inspection of "
        "music, speech frequency structure, impacts, transitions, and sound-design density. "
        "Returns a derived PNG only."
    ),
)
def audio_spectrogram_image(
    media_id: str,
    width: int = 1600,
    height: int = 700,
) -> dict:
    path = _base._resolve_media(media_id)
    width = max(640, min(int(width), 3000))
    height = max(320, min(int(height), 1600))
    output = _base.MEDIA_DIR / f"spectrogram_{uuid.uuid4().hex}.png"
    vf = (
        f"showspectrumpic=s={width}x{height}:legend=1:scale=log:"
        "fscale=log:color=fiery:gain=4"
    )
    _run_ffmpeg([
        "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-lavfi", vf, "-frames:v", "1", "-y", str(output),
    ])
    return _publish_derived(
        output, source_media_id=media_id, purpose="audio_spectrogram_for_ai_analysis", derived_only=True
    )


@mcp.tool(
    name="audio_analyze_dynamics",
    description=(
        "Measure audio energy at high temporal resolution for research. Returns RMS/peak level "
        "curves, dynamic-range summaries, likely energy onsets, silence/quiet proportions, and "
        "high-energy moments for comparing editing and sound design."
    ),
)
def audio_analyze_dynamics(
    media_id: str,
    window_seconds: float = 0.25,
    onset_sensitivity_db: float = 4.0,
    max_points: int = 1500,
) -> dict:
    path = _base._resolve_media(media_id)
    points = _audio_dynamics_points(path, window_seconds, max_points)
    rms_values = [p["rms_db"] for p in points if "rms_db" in p]
    peak_values = [p["peak_db"] for p in points if "peak_db" in p]
    onsets = _detect_energy_onsets(points, onset_sensitivity_db)

    high_energy: list[dict[str, float]] = []
    if rms_values:
        threshold = _quantile(rms_values, 0.90)
        if threshold is not None:
            high_energy = [p for p in points if p.get("rms_db", -999.0) >= threshold][:100]

    quiet_threshold_db = -42.0
    quiet_count = sum(1 for value in rms_values if value <= quiet_threshold_db)
    return {
        "media_id": media_id,
        "filename": path.name,
        "window_seconds": max(0.05, min(float(window_seconds), 5.0)),
        "point_count": len(points),
        "summary": {
            "mean_rms_db": round(statistics.fmean(rms_values), 3) if rms_values else None,
            "median_rms_db": round(statistics.median(rms_values), 3) if rms_values else None,
            "rms_p10_db": round(_quantile(rms_values, 0.10), 3) if rms_values else None,
            "rms_p90_db": round(_quantile(rms_values, 0.90), 3) if rms_values else None,
            "rms_dynamic_span_p90_p10_db": (
                round(_quantile(rms_values, 0.90) - _quantile(rms_values, 0.10), 3)
                if rms_values else None
            ),
            "max_peak_db": round(max(peak_values), 3) if peak_values else None,
            "quiet_fraction_below_minus42db": round(quiet_count / len(rms_values), 4) if rms_values else None,
            "energy_onset_count": len(onsets),
        },
        "energy_onsets": onsets,
        "high_energy_points": high_energy,
        "timeline": points,
        "note": (
            "Energy onsets are derived from positive RMS jumps and are useful for edit/sound-sync research; "
            "they are not a claim of exact musical beats."
        ),
    }


@mcp.tool(
    name="audio_analyze_spectrum",
    description=(
        "Measure time-varying spectral features from temporary analysis media, including centroid, "
        "spread, entropy, flatness, crest, spectral flux and rolloff. Useful for distinguishing "
        "speech-heavy, music-heavy, impact-heavy and changing sound-design sections."
    ),
)
def audio_analyze_spectrum(
    media_id: str,
    max_points: int = 1000,
) -> dict:
    path = _base._resolve_media(media_id)
    points = _audio_spectral_points(path, max_points)
    keys = [
        "centroid_hz", "spread_hz", "entropy", "flatness",
        "crest", "spectral_flux", "rolloff_hz",
    ]
    summary: dict[str, float | None] = {}
    for key in keys:
        values = [float(p[key]) for p in points if key in p]
        summary[f"mean_{key}"] = round(statistics.fmean(values), 4) if values else None
        summary[f"median_{key}"] = round(statistics.median(values), 4) if values else None
        summary[f"p90_{key}"] = round(_quantile(values, 0.90), 4) if values else None

    flux_values = [float(p["spectral_flux"]) for p in points if "spectral_flux" in p]
    flux_peaks: list[dict[str, float]] = []
    if flux_values:
        flux_threshold = _quantile(flux_values, 0.95)
        if flux_threshold is not None:
            flux_peaks = [p for p in points if p.get("spectral_flux", -1.0) >= flux_threshold][:120]

    return {
        "media_id": media_id,
        "filename": path.name,
        "point_count": len(points),
        "summary": summary,
        "spectral_change_peaks": flux_peaks,
        "timeline": points,
    }


@mcp.tool(
    name="audio_loudness_report",
    description=(
        "Measure EBU R128 integrated loudness, loudness range and true peak from temporary analysis "
        "media. Useful for comparing perceived loudness and mastering style across viral/control videos."
    ),
)
def audio_loudness_report(media_id: str) -> dict:
    path = _base._resolve_media(media_id)
    result = subprocess.run(
        [
            _base.FFMPEG_EXE, "-hide_banner", "-nostats", "-i", str(path),
            "-vn", "-af", "ebur128=peak=true", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError("ffmpeg loudness analysis failed:\n" + (result.stderr or "")[-7000:])
    text = result.stderr or ""
    tail = text[text.rfind("Summary:"):] if "Summary:" in text else text[-5000:]

    def find(pattern: str) -> float | None:
        matches = re.findall(pattern, tail, flags=re.MULTILINE)
        return _finite_float(matches[-1]) if matches else None

    return {
        "media_id": media_id,
        "filename": path.name,
        "integrated_lufs": find(r"I:\s*([-0-9.]+)\s*LUFS"),
        "loudness_range_lu": find(r"LRA:\s*([-0-9.]+)\s*LU"),
        "true_peak_dbfs": find(r"Peak:\s*([-0-9.]+)\s*dBFS"),
        "method": "ffmpeg_ebur128",
    }


@mcp.tool(
    name="video_motion_activity",
    description=(
        "Estimate time-varying visual motion/change intensity from temporary analysis video by "
        "measuring frame-to-frame pixel differences. Useful for quantifying calm vs high-motion "
        "sections and relating motion to audio/edit timing."
    ),
)
def video_motion_activity(
    media_id: str,
    sample_fps: float = 4.0,
    max_points: int = 1200,
) -> dict:
    path = _base._resolve_media(media_id)
    sample_fps = max(0.5, min(float(sample_fps), 12.0))
    max_points = max(10, min(int(max_points), 3000))
    filter_expr = (
        f"fps={sample_fps},scale=320:-2:flags=fast_bilinear,format=gray,"
        "tblend=all_mode=difference,signalstats,metadata=print:file=-"
    )
    result = _run_ffmpeg([
        "-hide_banner", "-i", str(path), "-an", "-vf", filter_expr, "-f", "null", "-"
    ], timeout=600)
    frames = _parse_metadata_frames(result.stdout or "", ("lavfi.signalstats.",))
    points: list[dict[str, float]] = []
    for item in frames[:max_points]:
        yavg = item.get("lavfi.signalstats.YAVG")
        if yavg is not None:
            points.append({"time": round(float(item["time"]), 4), "motion_score": round(float(yavg), 4)})
    values = [p["motion_score"] for p in points]
    threshold = _quantile(values, 0.90) if values else None
    peaks = [p for p in points if threshold is not None and p["motion_score"] >= threshold][:120]
    return {
        "media_id": media_id,
        "filename": path.name,
        "sample_fps": sample_fps,
        "point_count": len(points),
        "summary": {
            "mean_motion_score": round(statistics.fmean(values), 4) if values else None,
            "median_motion_score": round(statistics.median(values), 4) if values else None,
            "p90_motion_score": round(_quantile(values, 0.90), 4) if values else None,
        },
        "high_motion_points": peaks,
        "timeline": points,
        "note": "motion_score is mean luma difference after frame differencing; it is not optical flow.",
    }


@mcp.tool(
    name="video_audio_sync_metrics",
    description=(
        "Compare visual scene-change timestamps with audio energy onsets to quantify audiovisual "
        "synchronization. Useful for learning whether cuts/transitions tend to land on impacts, "
        "speech-energy changes or musical accents in viral versus control videos."
    ),
)
def video_audio_sync_metrics(
    media_id: str,
    scene_threshold: float = 0.30,
    audio_window_seconds: float = 0.10,
    onset_sensitivity_db: float = 3.0,
    tolerance_seconds: float = 0.18,
) -> dict:
    path = _base._resolve_media(media_id)
    scenes = _scene_timestamps(path, scene_threshold, 500)
    audio_points = _audio_dynamics_points(path, audio_window_seconds, 4000)
    onsets = _detect_energy_onsets(
        audio_points, onset_sensitivity_db, min_gap_seconds=max(0.08, tolerance_seconds / 2), max_events=600
    )
    onset_times = [event["time"] for event in onsets]
    tolerance_seconds = max(0.03, min(float(tolerance_seconds), 2.0))

    matches: list[dict[str, float | bool | None]] = []
    distances: list[float] = []
    for scene in scenes:
        nearest = min(onset_times, key=lambda value: abs(value - scene)) if onset_times else None
        distance = abs(nearest - scene) if nearest is not None else None
        if distance is not None:
            distances.append(distance)
        matches.append({
            "scene_time": round(scene, 4),
            "nearest_audio_onset_time": round(nearest, 4) if nearest is not None else None,
            "distance_seconds": round(distance, 4) if distance is not None else None,
            "within_tolerance": bool(distance is not None and distance <= tolerance_seconds),
        })

    synced = sum(1 for item in matches if item["within_tolerance"])
    return {
        "media_id": media_id,
        "filename": path.name,
        "scene_threshold": max(0.01, min(float(scene_threshold), 0.99)),
        "audio_window_seconds": max(0.05, min(float(audio_window_seconds), 5.0)),
        "onset_sensitivity_db": onset_sensitivity_db,
        "tolerance_seconds": tolerance_seconds,
        "scene_change_count": len(scenes),
        "audio_energy_onset_count": len(onsets),
        "scene_changes_synced_to_audio": synced,
        "scene_audio_sync_fraction": round(synced / len(scenes), 4) if scenes else None,
        "median_nearest_onset_distance_seconds": round(statistics.median(distances), 4) if distances else None,
        "matches": matches,
        "note": (
            "This measures proximity to detected energy rises, not semantic speech alignment or exact beat-grid correctness."
        ),
    }


@mcp.tool(
    name="video_advanced_analysis_bundle",
    description=(
        "Run a compact advanced audiovisual feature bundle on temporary research media: duration, "
        "scene/editing pace, high-resolution audio dynamics and onsets, spectral summaries, visual "
        "motion activity and audio-cut synchronization. Intended for building consistent viral/control datasets."
    ),
)
def video_advanced_analysis_bundle(
    media_id: str,
    scene_threshold: float = 0.30,
    audio_window_seconds: float = 0.25,
    motion_sample_fps: float = 4.0,
) -> dict:
    path = _base._resolve_media(media_id)
    duration = _probe_duration_seconds(path)
    scenes = _scene_timestamps(path, scene_threshold, 500)
    dynamics = _audio_dynamics_points(path, audio_window_seconds, 2000)
    onsets = _detect_energy_onsets(dynamics, 4.0, max_events=400)
    spectral = _audio_spectral_points(path, 600)

    rms_values = [p["rms_db"] for p in dynamics if "rms_db" in p]
    centroid_values = [p["centroid_hz"] for p in spectral if "centroid_hz" in p]
    flux_values = [p["spectral_flux"] for p in spectral if "spectral_flux" in p]

    # Motion summary without returning the large per-frame timeline in this bundle.
    filter_expr = (
        f"fps={max(0.5, min(float(motion_sample_fps), 12.0))},"
        "scale=320:-2:flags=fast_bilinear,format=gray,"
        "tblend=all_mode=difference,signalstats,metadata=print:file=-"
    )
    motion_result = _run_ffmpeg([
        "-hide_banner", "-i", str(path), "-an", "-vf", filter_expr, "-f", "null", "-"
    ], timeout=600)
    motion_frames = _parse_metadata_frames(motion_result.stdout or "", ("lavfi.signalstats.",))
    motion_values = [
        float(item["lavfi.signalstats.YAVG"])
        for item in motion_frames
        if "lavfi.signalstats.YAVG" in item
    ]

    onset_times = [event["time"] for event in onsets]
    distances = [
        min(abs(onset - scene) for onset in onset_times)
        for scene in scenes
        if onset_times
    ]
    sync_fraction = (
        sum(1 for d in distances if d <= 0.18) / len(scenes)
        if scenes and onset_times else None
    )

    cuts_per_minute = (len(scenes) * 60.0 / duration) if duration and duration > 0 else None
    return {
        "media_id": media_id,
        "filename": path.name,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "editing": {
            "scene_threshold": scene_threshold,
            "scene_change_count": len(scenes),
            "cuts_per_minute": round(cuts_per_minute, 3) if cuts_per_minute is not None else None,
            "scene_change_timestamps": [round(x, 3) for x in scenes],
        },
        "audio_dynamics": {
            "window_seconds": audio_window_seconds,
            "mean_rms_db": round(statistics.fmean(rms_values), 3) if rms_values else None,
            "median_rms_db": round(statistics.median(rms_values), 3) if rms_values else None,
            "rms_dynamic_span_p90_p10_db": (
                round(_quantile(rms_values, 0.90) - _quantile(rms_values, 0.10), 3) if rms_values else None
            ),
            "energy_onset_count": len(onsets),
            "energy_onset_timestamps": [event["time"] for event in onsets],
        },
        "audio_spectrum": {
            "mean_centroid_hz": round(statistics.fmean(centroid_values), 3) if centroid_values else None,
            "median_centroid_hz": round(statistics.median(centroid_values), 3) if centroid_values else None,
            "mean_spectral_flux": round(statistics.fmean(flux_values), 6) if flux_values else None,
            "p90_spectral_flux": round(_quantile(flux_values, 0.90), 6) if flux_values else None,
        },
        "visual_motion": {
            "sample_fps": motion_sample_fps,
            "mean_motion_score": round(statistics.fmean(motion_values), 4) if motion_values else None,
            "median_motion_score": round(statistics.median(motion_values), 4) if motion_values else None,
            "p90_motion_score": round(_quantile(motion_values, 0.90), 4) if motion_values else None,
        },
        "av_sync": {
            "scene_audio_sync_fraction_within_0_18s": round(sync_fraction, 4) if sync_fraction is not None else None,
            "median_nearest_audio_onset_distance_seconds": round(statistics.median(distances), 4) if distances else None,
        },
        "recommended_visual_tools": [
            "video_extract_frames_dense", "audio_waveform_image", "audio_spectrogram_image"
        ],
    }

@mcp.tool()
def research_tooling_status() -> dict:
    """Describe the research extension currently loaded by MCP9."""
    return {
        "server_build": SERVER_BUILD,
        "tools": [
            "youtube_build_research_set",
            "youtube_import_video_for_analysis",
            "video_extract_frames",
            "video_extract_audio",
            "video_detect_scenes",
            "video_editing_metrics",
            "video_extract_frames_dense",
            "audio_waveform_image",
            "audio_spectrogram_image",
            "audio_analyze_dynamics",
            "audio_analyze_spectrum",
            "audio_loudness_report",
            "video_motion_activity",
            "video_audio_sync_metrics",
            "video_advanced_analysis_bundle",
            "dataset_export_jsonl",
        ],
        "dataset_strategy": (
            "Search -> normalized candidate ranking -> authorization-aware full-video "
            "temporary import -> dense visual sampling + waveform/spectrogram + audio dynamics/"
            "spectrum/loudness + motion + AV-sync metrics -> multimodal interpretation -> JSONL export."
        ),
        "analysis_import": {
            "tool": "youtube_import_video_for_analysis",
            "rights_bases": [
                "owned",
                "creative_commons",
                "explicit_permission",
            ],
            "purpose": "temporary_ai_video_research_and_analysis",
            "redistribution_allowed": False,
        },
    }


if __name__ == "__main__":
    import uvicorn

    if not _base.MCP_API_TOKEN:
        print(
            "WARNING: MCP_API_TOKEN is not set. "
            "The MCP endpoint is running without authentication."
        )
    port = int(_base.os.environ.get("PORT", "10000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)
