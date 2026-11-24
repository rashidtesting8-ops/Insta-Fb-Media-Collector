"""Disposable worker fixture. Never mount a real jobs directory into this process."""

from functools import partial
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import compare_digest
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit
import json
import os
import requests

from fastapi import FastAPI, Header, HTTPException
from PIL import Image, ImageDraw
import uvicorn


SCENARIOS = ("success", "partial", "no-eligible", "upload-failure", "oauth-expiry", "ambiguous", "oauth-rejected")
ORIGIN = "http://127.0.0.1:8790"
FIXTURE_SHA = "e5c5dd3e2d67e9e532e9de2fb5fa9a8f33a444df5ac285cc9fea2bc2bae38162"


def fixture_page(names, published):
    media = [{"@type": "ImageObject", "contentUrl": name, "datePublished": published} for name in names]
    images = "".join(f'<img src="{name}" alt="Synthetic pet grooming study">' for name in names)
    return f'<html><title>Offline Fixture Salon</title><script type="application/ld+json">{json.dumps(media)}</script><body>{images}</body></html>'


def make_site(root, published):
    (root / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    for scenario in SCENARIOS:
        folder = root / scenario
        folder.mkdir()
        names = ["review.jpg"] if scenario == "no-eligible" else ["approved.jpg"]
        if scenario == "partial":
            names.append("review.jpg")
        for name in names:
            approved = name == "approved.jpg"
            image = Image.new("RGB", (320, 256), "#ebc168" if approved else "#135e86")
            draw = ImageDraw.Draw(image)
            for index in range(12):
                x = 18 + index * 24
                if approved:
                    draw.rectangle((x, 12 + index * 9, x + 12, 180), fill="#572734")
                else:
                    draw.ellipse((x, 135 - index * 7, x + 71, 229 - index * 6), fill="#e3faaa")
            image.save(folder / name, format="JPEG", quality=90)
        (folder / "index.html").write_text(fixture_page(names, published))


def main():
    with TemporaryDirectory(prefix="pgm-qa-worker-") as work:
        root = Path(work)
        web = root / "website"
        web.mkdir()
        guarded = os.getenv("PGM_QA_LOOPBACK") == "1"
        published = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        if guarded:
            fixture = Path("/repo/storage/media-delivery-preflight-20261123/synthetic-pup-bath.jpg").read_bytes()
            if sha256(fixture).hexdigest() != FIXTURE_SHA:
                raise ValueError("The locally generated QA JPEG changed")
            (web / "robots.txt").write_text("User-agent: *\nAllow: /\n")
            image_path = web / "guarded"
            image_path.mkdir()
            (image_path / "synthetic-pup-bath.jpg").write_bytes(fixture)
            (image_path / "index.html").write_text(fixture_page(["synthetic-pup-bath.jpg"], published))
        else:
            make_site(web, published)
        admin = os.environ["PGM_QA_WORKER_TOKEN"] if guarded else "fixture-admin"
        dashboard = os.environ["PGM_QA_DASHBOARD_TOKEN"] if guarded else "fixture-dashboard"
        os.environ.update(PROJECTS_DIR=str(root / "jobs"), AI_PROVIDER="mock", AI_API_KEY="",
                          MEDIA_WORKER_TOKEN=admin, MEDIA_WORKER_DASHBOARD_TOKEN=dashboard)
        from app import collector, delivery, service
        from app.ai import pipeline
        from app.ai.provider import AIProviderConfig
        from app.manifest import write_manifest

        crawl = collector.run_website_extraction
        curate = pipeline.run_ai_curation
        build_plan = delivery.build_delivery_plan

        def crawl_fixture(job_id, job_dir, website_url, *args, **kwargs):
            scenarios = ("guarded",) if guarded else SCENARIOS
            if website_url not in {f"{ORIGIN}/{scenario}/" for scenario in scenarios}:
                raise ValueError("Unexpected QA fixture URL")
            return crawl(job_id, job_dir, website_url, *args, allow_private=True, **kwargs)

        def curate_fixture(job_dir, job_id, **kwargs):
            manifest = json.loads((Path(job_dir) / "manifest.json").read_text(encoding="utf-8"))
            for asset in manifest["assets"]:
                source = urlsplit(asset.get("source_url") or "")
                if (source.netloc == "127.0.0.1:8790" and
                        (source.path.endswith("/approved.jpg") or
                         (guarded and source.path == "/guarded/synthetic-pup-bath.jpg" and asset.get("sha256") == FIXTURE_SHA))):
                    asset["rights_status"] = "verified"
                    asset["rights_evidence"] = "mock-only synthetic fixture approval; not valid for real delivery"
            write_manifest(job_dir, manifest)
            if guarded:
                kwargs["config"] = AIProviderConfig(provider="mock", max_retries=0, max_requests_per_job=2,
                                                    max_cost_usd_per_job=0.25, mode_a_max_output_tokens=1200,
                                                    mode_b_max_output_tokens=900, batch_size=1)
            return curate(job_dir, job_id, **kwargs)

        collector.run_website_extraction = crawl_fixture
        pipeline.run_ai_curation = curate_fixture
        # This fixture process alone can send mock-selected bytes to the fake Drive.
        delivery.build_delivery_plan = partial(build_plan, allow_fixture=True)
        app = service.app
        if guarded:
            adapter_token = os.environ["PGM_QA_ADAPTER_TOKEN"]
            drive_token = os.environ["PGM_QA_DRIVE_TOKEN"]
            if min(map(len, (admin, dashboard, adapter_token, drive_token))) < 32:
                raise ValueError("Isolated fixture credentials are required")
            from app.qa_live import GuardedJobRequest, bounded_config

            config = bounded_config("fixture-token-with-no-groq-access")
            assert (config.mode_a_max_output_tokens, config.mode_b_max_output_tokens,
                    config.max_cost_usd_per_job, config.max_requests_per_job, config.max_retries) == (1200, 900, 0.25, 2, 0)

            app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=service.app.router.lifespan_context)

            @app.post("/guarded-job")
            def guarded_fixture(body: GuardedJobRequest, x_qa_adapter_token: str = Header(default="")):
                if not compare_digest(x_qa_adapter_token.encode(), adapter_token.encode()):
                    raise HTTPException(401, "Fixture adapter authorization required")
                job = service.fetch(body.job_id)
                if not job or job.get("website_url") != ORIGIN + "/guarded/" or job.get("status") != "extraction_complete":
                    raise HTTPException(403, "Job outside fixture scope")
                directory = service.DATA_DIR / body.job_id
                manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                items = manifest.get("assets", [])
                if len(items) != 1 or items[0].get("sha256") != FIXTURE_SHA or manifest.get("job_id") != body.job_id:
                    raise HTTPException(409, "Fixture job and asset hash mismatch")
                try:
                    with (directory / "fixture-adapter-intent").open("x"):
                        pass
                except FileExistsError:
                    raise HTTPException(409, "Fixture job already attempted") from None
                # The worker and published mock artifacts stay explicitly marked mock.
                analysis = curate_fixture(directory, body.job_id)
                outcome = analysis.as_dict()
                if outcome.get("mock") is not True or outcome.get("status") != "completed":
                    raise HTTPException(409, "Fixture curation did not complete as mock")
                service.store({**job, "run_ai_curation": True, "ai_curation": outcome})
                plan = delivery.build_delivery_plan(directory, body.job_id)
                eligible = [item for item in plan["pending_uploads"]
                            if item["rights_status"] == "verified" and item["rights_review_required"] is False]
                if len(eligible) != 1 or eligible[0]["sha256"] != FIXTURE_SHA:
                    raise HTTPException(409, "Fixture asset is not the approved simulation candidate")
                response = requests.post("http://127.0.0.1:8899/deliver",
                                         json={"job_id": body.job_id, "upload_keys": [eligible[0]["upload_key"]]},
                                         headers={"x-qa-drive-token": drive_token}, timeout=15, allow_redirects=False)
                response.raise_for_status()
                receipt = response.json()
                if receipt.get("job_id") != body.job_id:
                    raise HTTPException(409, "Fixture receipt belongs to another job")
                return {**receipt, "mode": "fixture", "status": "verified"}

            app.mount("/", service.app)
        server = ThreadingHTTPServer(("127.0.0.1", 8790), partial(SimpleHTTPRequestHandler, directory=str(web)))
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            uvicorn.run(app, host="127.0.0.1" if guarded else "0.0.0.0",
                        port=8877 if guarded else 8787, access_log=False)
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
