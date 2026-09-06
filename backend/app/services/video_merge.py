"""FFmpeg helpers for lossless concatenation and optional page transitions."""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
from pathlib import Path


# Keep the automatic set restrained: no black/white flashes, cube rotations, or
# aggressive zooms.  Directional effects make page changes visible without
# overwhelming the presentation content.
TRANSITION_EFFECTS = (
    "fade",
    "dissolve",
    "smoothleft",
    "smoothright",
    "coverleft",
    "coverright",
    "revealleft",
    "revealright",
)
# ``dissolve`` is a pixel-noise effect.  It is acceptable in an overlay on
# video, but noticeably dirty when blending two detailed slide images, so the
# fast image-bridge path intentionally leaves it out.
INSERTED_TRANSITION_EFFECTS = (
    "fade",
    "smoothleft",
    "smoothright",
    "coverleft",
    "coverright",
    "revealleft",
    "revealright",
)
DEFAULT_TRANSITION_SECONDS = 0.42
INSERTED_TRANSITION_STRATEGY = "inserted-v1"


async def _run(command: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    return proc.returncode or 0, stdout or b"", stderr or b""


async def _probe_media(path: str) -> dict:
    code, stdout, stderr = await _run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", path,
    ])
    if code != 0:
        raise RuntimeError(f"無法讀取影片資訊：{stderr.decode(errors='ignore')[-300:]}")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("ffprobe 回傳格式錯誤") from exc
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError(f"影片沒有視訊軌：{path}")
    try:
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise RuntimeError(f"影片長度無效：{path}")
    return {
        "duration": duration,
        "width": int(video.get("width") or 1920),
        "height": int(video.get("height") or 1080),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def _even(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


async def _concat_video_files(input_paths: list[str], temp_dir: str) -> tuple[str, dict]:
    list_file = os.path.join(temp_dir, "concat.txt")
    with open(list_file, "w", encoding="utf-8") as handle:
        for path in input_paths:
            safe_path = str(path).replace("'", "'\\''")
            handle.write(f"file '{safe_path}'\n")

    out_path = os.path.join(temp_dir, "merged.mp4")
    code, _stdout, stderr = await _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", "-movflags", "+faststart", out_path,
    ])
    if code != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        out_path = os.path.join(temp_dir, "merged_reencode.mp4")
        code2, _stdout2, stderr2 = await _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out_path,
        ])
        if code2 != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            message = (stderr + b"\n" + stderr2).decode(errors="ignore").strip()
            raise RuntimeError(f"影片合併失敗：{message[-500:]}")
    return out_path, {"enabled": False, "seed": None, "effects": [], "duration_seconds": 0.0}


async def _create_inserted_transition_clip(
    *,
    before_image: str,
    after_image: str,
    output_path: str,
    width: int,
    height: int,
    effect: str,
    duration: float,
) -> None:
    """Render one short, silent bridge from two original slide images.

    The page videos themselves remain untouched.  The output deliberately
    matches SlideAI's page-video streams (H.264/AAC, 25 fps, 48 kHz mono), so
    the final concat demuxer can copy all page and bridge streams without
    another full-video encode.
    """
    frame_rate = 25
    common = (
        f"fps={frame_rate},scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    )
    command = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", str(frame_rate), "-t", f"{duration:.6f}", "-i", before_image,
        "-loop", "1", "-framerate", str(frame_rate), "-t", f"{duration:.6f}", "-i", after_image,
        "-f", "lavfi", "-t", f"{duration:.6f}", "-i", "anullsrc=r=48000:cl=mono",
        "-filter_complex",
        f"[0:v]{common}[va];[1:v]{common}[vb];"
        f"[va][vb]xfade=transition={effect}:duration={duration:.6f}:offset=0[vout]",
        "-map", "[vout]", "-map", "2:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(frame_rate),
        "-video_track_timescale", "12800",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "1",
        "-movflags", "+faststart", "-shortest", output_path,
    ]
    code, _stdout, stderr = await _run(command)
    if code != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"插入式轉場產生失敗：{stderr.decode(errors='ignore')[-700:]}")


async def _merge_with_inserted_transitions(
    input_paths: list[str],
    transition_images: list[str],
    temp_dir: str,
    transition_seed: int | None,
) -> tuple[str, dict]:
    """Insert short visual bridge clips and stream-copy the final sequence.

    Unlike xfade over all source videos, this only encodes ``n - 1`` short
    image-based clips.  It intentionally adds the bridge duration to the
    output timeline; callers can use ``inserted_duration_seconds`` to shift
    later subtitle/chapters offsets accurately.
    """
    media = [await _probe_media(path) for path in input_paths]
    target_width = _even(media[0]["width"])
    target_height = _even(media[0]["height"])
    minimum_duration = min(item["duration"] for item in media)
    duration = min(DEFAULT_TRANSITION_SECONDS, max(0.18, minimum_duration * 0.2))

    seed = int(transition_seed) if transition_seed is not None else secrets.randbits(63)
    rng = random.Random(seed)
    effects: list[str] = []
    sequence: list[str] = []
    previous = ""
    for index, page_video in enumerate(input_paths):
        sequence.append(page_video)
        if index >= len(input_paths) - 1:
            continue
        choices = [effect for effect in INSERTED_TRANSITION_EFFECTS if effect != previous]
        effect = rng.choice(choices)
        effects.append(effect)
        previous = effect
        bridge_path = os.path.join(temp_dir, f"transition_{index + 1:03d}.mp4")
        await _create_inserted_transition_clip(
            before_image=transition_images[index],
            after_image=transition_images[index + 1],
            output_path=bridge_path,
            width=target_width,
            height=target_height,
            effect=effect,
            duration=duration,
        )
        sequence.append(bridge_path)

    merged_path, _unused = await _concat_video_files(sequence, temp_dir)
    return merged_path, {
        "enabled": True,
        "strategy": INSERTED_TRANSITION_STRATEGY,
        "seed": seed,
        "effects": effects,
        "duration_seconds": round(duration, 3),
        "inserted_duration_seconds": round(duration * len(effects), 3),
        "output_size": [target_width, target_height],
        "input_durations": [item["duration"] for item in media],
    }


async def _merge_with_transitions(
    input_paths: list[str],
    temp_dir: str,
    transition_seed: int | None,
) -> tuple[str, dict]:
    media = [await _probe_media(path) for path in input_paths]
    target_width = _even(media[0]["width"])
    target_height = _even(media[0]["height"])
    minimum_duration = min(item["duration"] for item in media)
    transition_seconds = min(DEFAULT_TRANSITION_SECONDS, max(0.18, minimum_duration * 0.2))

    seed = int(transition_seed) if transition_seed is not None else secrets.randbits(63)
    rng = random.Random(seed)
    effects: list[str] = []
    previous = ""
    for _ in range(len(input_paths) - 1):
        choices = [effect for effect in TRANSITION_EFFECTS if effect != previous]
        effect = rng.choice(choices)
        effects.append(effect)
        previous = effect

    command = ["ffmpeg", "-y"]
    for path in input_paths:
        command.extend(["-i", path])

    filters: list[str] = []
    for index, item in enumerate(media):
        video_chain = (
            f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,"
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
        )
        if index < len(media) - 1:
            video_chain += f",tpad=stop_mode=clone:stop_duration={transition_seconds:.6f}"
        filters.append(f"{video_chain}[v{index}]")

        if item["has_audio"]:
            filters.append(
                f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={item['duration']:.6f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )

    previous_label = "v0"
    elapsed = media[0]["duration"]
    for index, effect in enumerate(effects, start=1):
        output_label = f"vx{index}"
        filters.append(
            f"[{previous_label}][v{index}]xfade=transition={effect}:"
            f"duration={transition_seconds:.6f}:offset={elapsed:.6f}[{output_label}]"
        )
        previous_label = output_label
        elapsed += media[index]["duration"]

    audio_inputs = "".join(f"[a{index}]" for index in range(len(media)))
    filters.append(f"{audio_inputs}concat=n={len(media)}:v=0:a=1[aout]")

    out_path = os.path.join(temp_dir, "merged_transitions.mp4")
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{previous_label}]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-shortest", out_path,
    ])
    code, _stdout, stderr = await _run(command)
    if code != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"轉場影片合併失敗：{stderr.decode(errors='ignore')[-700:]}")

    return out_path, {
        "enabled": True,
        "strategy": "full-reencode-v1",
        "seed": seed,
        "effects": effects,
        "duration_seconds": round(transition_seconds, 3),
        "output_size": [target_width, target_height],
        "input_durations": [item["duration"] for item in media],
    }


async def merge_video_files(
    input_paths: list[str],
    temp_dir: str,
    *,
    transitions_enabled: bool = False,
    transition_seed: int | None = None,
    transition_images: list[str] | None = None,
) -> tuple[str, dict]:
    """Merge videos and return ``(output_path, transition_metadata)``."""
    paths = [str(Path(path).resolve()) for path in input_paths]
    if not paths:
        raise ValueError("沒有可合併的片段")
    for path in paths:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ValueError(f"找不到有效影片片段：{path}")
    os.makedirs(temp_dir, exist_ok=True)
    image_paths = [str(Path(path).resolve()) for path in (transition_images or [])]
    if (
        transitions_enabled
        and len(paths) > 1
        and len(image_paths) == len(paths)
        and all(os.path.isfile(path) and os.path.getsize(path) > 0 for path in image_paths)
    ):
        return await _merge_with_inserted_transitions(paths, image_paths, temp_dir, transition_seed)
    if transitions_enabled and len(paths) > 1:
        return await _merge_with_transitions(paths, temp_dir, transition_seed)
    return await _concat_video_files(paths, temp_dir)
