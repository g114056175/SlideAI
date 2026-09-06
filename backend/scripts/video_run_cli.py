#!/usr/bin/env python3
"""CLI client for SlideAI persistent video-run records.

Examples:
  python backend/scripts/video_run_cli.py create --pdf /path/to/slides.pdf --subtitle-source none
  python backend/scripts/video_run_cli.py list
  python backend/scripts/video_run_cli.py show --run-id 20260524-120000-abcd1234
  python backend/scripts/video_run_cli.py render-run --run-id ... --reference-audio backend/app/static/ref_voices/YunJhe_中文-男.mp3 --variants 3
  python backend/scripts/video_run_cli.py merge-run --run-id ... --output output.mp4
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

DEFAULT_API = "http://127.0.0.1:8002"
DEFAULT_SPLIT_MIN = 18
DEFAULT_SPLIT_MAX = 24


def api_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def request_json(base: str, path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(api_url(base, path), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"API connection failed: {exc}") from exc


def _load_scripts(args: argparse.Namespace) -> list[str]:
    if args.scripts_json:
        data = json.loads(Path(args.scripts_json).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("scripts", [])
        if not isinstance(data, list):
            raise SystemExit("--scripts-json must contain a JSON list or {'scripts': [...]}")
        return [str(x or "") for x in data]
    if args.scripts_text:
        raw = Path(args.scripts_text).read_text(encoding="utf-8")
        return [part.strip() for part in raw.split(args.page_delimiter)]
    return []


def cmd_create(args: argparse.Namespace) -> None:
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.is_file():
        raise SystemExit(f"PDF not found: {pdf}")
    payload = {
        "pdf_path": str(pdf),
        "subtitle_source": args.subtitle_source,
        "run_label": args.label or pdf.stem,
        "scripts": _load_scripts(args),
        "settings": {
            "tts": {"voice": args.voice, "speed": args.speed},
            "subtitle": {
                "fontSize": args.font_size,
                "marginV": args.margin_v,
                "enableBackground": True,
                "bgColor": "#000000",
                "bgOpacity": args.bg_opacity,
            },
        },
    }
    result = request_json(args.api_base, "/api/video-runs/local-pdf", payload, method="POST")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_list(args: argparse.Namespace) -> None:
    result = request_json(args.api_base, f"/api/video-runs?limit={args.limit}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_show(args: argparse.Namespace) -> None:
    result = request_json(args.api_base, f"/api/video-runs/{args.run_id}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _parse_pages(spec: str, total: int) -> list[int]:
    raw = (spec or "all").strip().lower()
    if raw in {"", "all", "*"}:
        return list(range(total))
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = max(1, int(a))
            end = min(total, int(b))
            out.extend(range(start - 1, end))
        else:
            idx = int(part) - 1
            if 0 <= idx < total:
                out.append(idx)
    return sorted(set(out))


def _raise_for_response(resp) -> None:
    if resp.ok:
        return
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text
    raise RuntimeError(f"HTTP {resp.status_code}: {detail}")


def cmd_render_run(args: argparse.Namespace) -> None:
    if requests is None:
        raise SystemExit("The 'requests' package is required for render-run")
    ref_audio = Path(args.reference_audio).expanduser().resolve()
    if not ref_audio.is_file():
        raise SystemExit(f"reference audio not found: {ref_audio}")

    manifest = request_json(args.api_base, f"/api/video-runs/{args.run_id}")
    pages = manifest.get("pages") or []
    selected = _parse_pages(args.pages, len(pages))
    if not selected:
        raise SystemExit("No pages selected")

    session = requests.Session()
    for variant_no in range(1, args.variants + 1):
        for idx in selected:
            page = pages[idx]
            script = str(page.get("script") or "").strip()
            if not script:
                print(f"[SKIP] page {idx + 1}: empty script")
                continue

            print(f"[TTS] variant {variant_no}/{args.variants} page {idx + 1}/{len(pages)}")
            with ref_audio.open("rb") as fp:
                tts_resp = session.post(
                    api_url(args.api_base, f"/api/video-runs/{args.run_id}/pages/{idx}/tts"),
                    data={
                        "response_mode": "json",
                        "text": script,
                        "voice": args.voice,
                        "speed": str(args.speed),
                        "reference_text": args.reference_text or "",
                    },
                    files={"reference_audio": (ref_audio.name, fp, "application/octet-stream")},
                    timeout=args.timeout,
                )
            _raise_for_response(tts_resp)
            variant_id = tts_resp.json()["variant_id"]

            print(f"[ALIGN] page {idx + 1}")
            align_resp = session.post(
                api_url(args.api_base, f"/api/video-runs/{args.run_id}/pages/{idx}/align"),
                data={
                    "text": script,
                    "variant_id": variant_id,
                    "split_min_chars": str(args.split_min),
                    "split_max_chars": str(args.split_max),
                },
                timeout=args.timeout,
            )
            _raise_for_response(align_resp)
            aligned = align_resp.json()
            segments = aligned.get("segments") or []
            if not segments:
                raise RuntimeError(f"page {idx + 1}: empty alignment segments")

            print(f"[RENDER] page {idx + 1}")
            render_resp = session.post(
                api_url(args.api_base, "/api/video-abstract/render-subtitle-ass-video"),
                data={
                    "segments_json": json.dumps(segments, ensure_ascii=False),
                    "subtitle_style": "bg-dark",
                    "enable_highlight": str(args.enable_highlight).lower(),
                    "font_size": str(args.font_size),
                    "enable_background": "true",
                    "bg_color": args.bg_color,
                    "bg_opacity": str(args.bg_opacity),
                    "margin_v": str(args.margin_v),
                    "align_backend": str(aligned.get("backend") or ""),
                    "run_id": args.run_id,
                    "page_index": str(idx),
                    "variant_id": variant_id,
                    "variant_label": f"cli-v{variant_no}-page-{idx + 1}",
                },
                timeout=args.timeout,
            )
            _raise_for_response(render_resp)
            variant_id = render_resp.headers.get("X-Variant-Id", "")
            print(f"[OK] page {idx + 1} variant={variant_id or '(recorded)'} bytes={len(render_resp.content)}")
            time.sleep(args.pause)

    updated = request_json(args.api_base, f"/api/video-runs/{args.run_id}")
    print(json.dumps({
        "run_id": updated.get("run_id"),
        "status": updated.get("status"),
        "root": (updated.get("paths") or {}).get("root"),
        "pages": [
            {"page_number": p.get("page_number"), "variants": len(p.get("variants") or [])}
            for p in updated.get("pages", [])
        ],
    }, ensure_ascii=False, indent=2))


def cmd_merge_run(args: argparse.Namespace) -> None:
    if requests is None:
        raise SystemExit("The 'requests' package is required for merge-run")
    manifest = request_json(args.api_base, f"/api/video-runs/{args.run_id}")
    pages = manifest.get("pages") or []
    selected_pages = []
    for idx, page in enumerate(pages):
        variant_id = page.get("selected_variant_id")
        if not variant_id:
            variants = page.get("variants") or []
            variant_id = variants[-1].get("variant_id") if variants else ""
        if variant_id:
            selected_pages.append((idx, variant_id))

    if not selected_pages:
        raise SystemExit("No selected/rendered page videos to merge")

    with requests.Session() as session:
        response = session.post(
            api_url(args.api_base, f"/api/video-runs/{args.run_id}/exports/merge-selected"),
            data={"page_indexes_json": json.dumps([idx for idx, _ in selected_pages]),
                  "variant_ids_json": json.dumps(dict(selected_pages)), "response_mode": "video"},
            timeout=args.timeout, stream=True,
        )
        _raise_for_response(response)
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as handle:
            for chunk in response.iter_content(1024 * 1024):
                handle.write(chunk)
        response.close()
        print(f"[OK] saved {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SlideAI video-run API CLI")
    parser.add_argument("--api-base", default=DEFAULT_API, help="Backend API base, default http://127.0.0.1:8002")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create a persistent run record from a local PDF path")
    p_create.add_argument("--pdf", required=True)
    p_create.add_argument("--subtitle-source", choices=["none", "zh", "en", "user_input"], default="none")
    p_create.add_argument("--scripts-json", default="", help="JSON list or {'scripts': [...]} file")
    p_create.add_argument("--scripts-text", default="", help="Plain text split by --page-delimiter")
    p_create.add_argument("--page-delimiter", default="\n---PAGE---\n")
    p_create.add_argument("--label", default="")
    p_create.add_argument("--voice", default="qwen3_local")
    p_create.add_argument("--speed", type=float, default=1.0)
    p_create.add_argument("--font-size", type=int, default=52)
    p_create.add_argument("--margin-v", type=int, default=90)
    p_create.add_argument("--bg-opacity", type=int, default=64)
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="List persistent runs")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show one run manifest")
    p_show.add_argument("--run-id", required=True)
    p_show.set_defaults(func=cmd_show)

    p_render = sub.add_parser("render-run", help="Render selected pages and persist page variants")
    p_render.add_argument("--run-id", required=True)
    p_render.add_argument("--reference-audio", required=True)
    p_render.add_argument("--reference-text", default="")
    p_render.add_argument("--pages", default="all", help="all, 1,3, 2-5")
    p_render.add_argument("--variants", type=int, default=1)
    p_render.add_argument("--voice", default="qwen3_local")
    p_render.add_argument("--speed", type=float, default=1.0)
    p_render.add_argument("--font-size", type=int, default=52)
    p_render.add_argument("--margin-v", type=int, default=90)
    p_render.add_argument("--bg-color", default="#000000")
    p_render.add_argument("--bg-opacity", type=int, default=64)
    p_render.add_argument("--split-min", type=int, default=DEFAULT_SPLIT_MIN)
    p_render.add_argument("--split-max", type=int, default=DEFAULT_SPLIT_MAX)
    p_render.add_argument("--enable-highlight", action=argparse.BooleanOptionalAction, default=False)
    p_render.add_argument("--timeout", type=int, default=900)
    p_render.add_argument("--pause", type=float, default=0.0)
    p_render.set_defaults(func=cmd_render_run)

    p_merge = sub.add_parser("merge-run", help="Merge selected page variants from one run and save a local MP4")
    p_merge.add_argument("--run-id", required=True)
    p_merge.add_argument("--output", required=True)
    p_merge.add_argument("--timeout", type=int, default=900)
    p_merge.set_defaults(func=cmd_merge_run)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
