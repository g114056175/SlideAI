import json
import asyncio
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from PIL import Image
from fastapi import UploadFile
from pypdf import PdfWriter

from backend.app.api import video
from backend.app.services.artifact_store import VideoRunStore
from backend.app.services.tts_chunks import combine_tts_chunks


class ArtifactPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pdf = self.root / "source.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        self.store = VideoRunStore(self.root / "runs")
        self.run = self.store.create_run(pdf_path=self.pdf, scripts=["第一頁。"])
        self.run_id = self.run["run_id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_concurrent_manifest_updates_do_not_lose_fields(self):
        errors = []

        def update(index):
            try:
                self.store.update_settings(self.run_id, {f"field_{index}": index})
            except Exception as exc:  # pragma: no cover - diagnostic path
                errors.append(exc)

        threads = [threading.Thread(target=update, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        current = self.store.load_manifest(self.run_id)["settings"]["current"]
        for index in range(16):
            self.assertEqual(current[f"field_{index}"], index)
        self.assertEqual(list(self.store.run_dir(self.run_id).glob(".manifest.json.*.tmp")), [])

    def test_job_state_is_persistent_and_merge_updates_pages(self):
        job = self.store.create_job(run_id=self.run_id, payload={"page_indexes": [0]})
        updated = self.store.update_job(
            run_id=self.run_id,
            job_id=job["job_id"],
            updates={"status": "running", "pages": {"0": {"status": "tts_ready"}}},
        )
        self.assertEqual(updated["status"], "running")
        self.assertEqual(self.store.load_job(run_id=self.run_id, job_id=job["job_id"])["pages"]["0"]["status"], "tts_ready")

    def test_chunk_combination_emits_reusable_timeline(self):
        rate = 24000
        first = self.root / "first.wav"
        second = self.root / "second.wav"
        sf.write(first, np.zeros(rate, dtype=np.float32), rate)
        sf.write(second, np.zeros(rate // 2, dtype=np.float32), rate)
        output = self.root / "combined.wav"

        timeline = combine_tts_chunks(
            [("第一段。", first), ("Second chunk.", second)], output, silence_ms=120,
        )
        self.assertTrue(output.is_file())
        self.assertAlmostEqual(timeline[0]["duration"], 1.0, places=3)
        self.assertAlmostEqual(timeline[1]["start"], 1.12, places=3)
        payload = json.loads(output.with_suffix(".chunks").joinpath("chunks.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["chunks"]), 2)
        variant = self.store.record_page_variant_tts(
            run_id=self.run_id,
            page_index=0,
            audio_source_path=output,
            metadata={"text": "第一段。Second chunk."},
        )
        self.assertEqual(len(variant["tts"]["chunks"]), 2)
        self.assertTrue(Path(variant["tts"]["chunks"][0]["path"]).is_file())

    def test_background_thumbnail_does_not_recreate_deleted_run(self):
        self.store.delete_run(self.run_id)
        image = Image.new("RGB", (320, 180), "white")
        with (
            patch.object(video, "get_video_run_store", return_value=self.store),
        ):
            video._pregenerate_run_thumbnails_safe(self.run_id, str(self.pdf))
            self.assertFalse(self.store.run_dir(self.run_id).exists())

    def test_agent_submission_requires_one_script_per_page_and_queues_auto_merge(self):
        pdf_buffer = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=1280, height=720)
        writer.add_blank_page(width=1280, height=720)
        writer.write(pdf_buffer)
        pdf_buffer.seek(0)
        queued = []
        config = {
            "scripts": ["第一頁講稿。", "第二頁講稿。"],
            "reference_text": "這是參考音檔的逐字稿。",
            "subtitle_mode": "none",
            "tts_speed": 1.0,
            "transitions_enabled": True,
        }
        with (
            patch.object(video, "get_video_run_store", return_value=self.store),
            patch.object(video, "_pregenerate_run_thumbnails_safe", return_value=None),
            patch.object(video, "_start_batch_job_task", side_effect=lambda run_id, job_id: queued.append((run_id, job_id))),
        ):
            response = asyncio.run(video.create_agent_video_job(
                pdf=UploadFile(filename="agent.pdf", file=pdf_buffer),
                reference_audio=UploadFile(filename="voice.wav", file=io.BytesIO(b"RIFF-reference")),
                config_json=json.dumps(config, ensure_ascii=False),
            ))
        body = json.loads(bytes(response.body).decode("utf-8"))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(queued), 1)
        job = self.store.load_job(run_id=body["run_id"], job_id=body["job_id"])
        self.assertTrue(job["payload"]["auto_merge"])
        self.assertTrue(job["payload"]["transitions_enabled"])
        self.assertEqual(job["payload"]["page_indexes"], [0, 1])
        manifest = self.store.load_manifest(body["run_id"])
        self.assertEqual([page["script"] for page in manifest["pages"]], config["scripts"])
        self.assertTrue(Path(manifest["settings"]["current"]["reference_audio"]["path"]).is_file())

    def test_persistent_page_tts_resolves_builtin_voice_without_uploaded_file(self):
        generated = self.root / "generated.wav"
        sf.write(generated, np.zeros(2400, dtype=np.float32), 24000)
        with (
            patch.object(video, "get_video_run_store", return_value=self.store),
            patch.object(video, "synthesize_tts_preview", return_value=(True, str(generated), "ok")) as synth,
            patch.object(video, "_apply_audio_speed", return_value=str(generated)),
        ):
            response = asyncio.run(video.video_run_page_tts_endpoint(
                run_id=self.run_id,
                page_index=0,
                text="這是批次渲染測試。",
                voice="",
                speed=1.0,
                reference_audio=None,
                reference_text="",
                selected_voice_key="YunJhe_中文-男",
                response_mode="json",
            ))
        body = json.loads(bytes(response.body).decode("utf-8"))
        self.assertTrue(body["ok"])
        self.assertTrue(body["variant_id"])
        self.assertGreater(len(synth.call_args.kwargs["reference_audio_bytes"]), 100)
        self.assertTrue(synth.call_args.kwargs["reference_text"])

    def test_chunk_regeneration_resolves_preset_voice_without_uploaded_reference(self):
        """Preset voices must work for local retries just as normal page TTS does."""
        first = self.root / "first.wav"
        second = self.root / "second.wav"
        replacement = self.root / "replacement.wav"
        for path in (first, second, replacement):
            sf.write(path, np.zeros(2400, dtype=np.float32), 24000)
        source = self.root / "source.wav"
        combine_tts_chunks([("第一段。", first), ("第二段。", second)], source, silence_ms=120)
        variant = self.store.record_page_variant_tts(
            run_id=self.run_id,
            page_index=0,
            audio_source_path=source,
            metadata={"selected_voice_key": "YunJhe_中文-男"},
        )
        self.store.update_settings(self.run_id, {"selected_voice_key": "YunJhe_中文-男"})

        with (
            patch.object(video, "get_video_run_store", return_value=self.store),
            patch.object(video, "synthesize_tts_preview", return_value=(True, str(replacement), "ok")) as synth,
        ):
            response = asyncio.run(video.regenerate_video_run_tts_chunk(
                run_id=self.run_id,
                page_index=0,
                variant_id=variant["variant_id"],
                chunk_index=1,
                text="修改後第二段。",
            ))
        body = json.loads(bytes(response.body).decode("utf-8"))
        self.assertTrue(body["ok"])
        self.assertIn("修改後第二段。", body["page_text"])
        self.assertGreater(len(synth.call_args.kwargs["reference_audio_bytes"]), 100)

    def test_restart_waits_for_confirmation_then_resumes_in_requested_order(self):
        jobs = []
        run_ids = [self.run_id]
        for index in range(4):
            extra = self.store.create_run(
                pdf_path=self.pdf,
                scripts=[f"第 {index + 2} 個任務。"],
            )
            run_ids.append(extra["run_id"])
        for run_id in run_ids:
            jobs.append(self.store.create_job(run_id=run_id, payload={"page_indexes": [0]}))

        recovered = []
        with (
            patch.object(video, "get_video_run_store", return_value=self.store),
            patch.object(video, "_start_batch_job_task", side_effect=lambda run_id, job_id: recovered.append((run_id, job_id))),
        ):
            self.assertEqual(video.recover_persistent_batch_jobs(), 5)
        self.assertEqual(recovered, [])
        for job in jobs:
            self.assertEqual(self.store.load_job(run_id=job["run_id"], job_id=job["job_id"])["status"], "interrupted")
        with (
            patch.object(video, "get_video_run_store", return_value=self.store),
            patch.object(video, "_start_batch_job_task", side_effect=lambda run_id, job_id: recovered.append((run_id, job_id))),
        ):
            for job in jobs:
                asyncio.run(video.resume_batch_render_job(job["run_id"], job["job_id"]))
        self.assertEqual(recovered, [(job["run_id"], job["job_id"]) for job in jobs])

        old_active = video._BATCH_ACTIVE_JOB
        old_waiting = list(video._BATCH_WAITING_ORDER)
        try:
            video._BATCH_ACTIVE_JOB = recovered[0]
            video._BATCH_WAITING_ORDER[:] = recovered[1:]
            with patch.object(video, "get_video_run_store", return_value=self.store):
                second = video._batch_queue_metadata(*recovered[1])
                fifth = video._batch_queue_metadata(*recovered[4])
            self.assertEqual(second["jobs_ahead"], 1)
            self.assertEqual(second["queue_position"], 1)
            self.assertEqual(fifth["jobs_ahead"], 4)
            self.assertEqual(fifth["queue_position"], 4)
            self.assertEqual(fifth["active"]["stage"], "queued")
        finally:
            video._BATCH_ACTIVE_JOB = old_active
            video._BATCH_WAITING_ORDER[:] = old_waiting


if __name__ == "__main__":
    unittest.main()
