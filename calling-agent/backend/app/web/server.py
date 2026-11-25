"""
FastAPI app exposing a localhost chat UI for the voice pipeline.

Read-only view: shows transcripts (user) and responses (agent) emitted by
the pipeline via EventHub. The only thing the UI sends back is mic toggle
state.

Latency contract: this server NEVER blocks the pipeline.
- Pipeline calls hub.emit_nowait() (microseconds, in-memory).
- Each websocket has its own send-loop task that drains its own queue.
- Slow / disconnected clients drop messages on their own queue, never
  affecting the pipeline.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import subprocess
import sys
import time as _time_module
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from ..voice.event_hub import EventHub
from ..agent_definition.schema import AgentDefinition
from ..agent_definition.registry import agent_registry

logger = logging.getLogger(__name__)

_INDEX_PATH = Path(__file__).parent / "index.html"
_AGENTS_DIR = Path(__file__).resolve().parents[3] / "agents"  # calling-agent/agents/


def make_app(hub: EventHub, pipeline=None, standalone: bool = False) -> FastAPI:
    app = FastAPI()
    _start_time = _time_module.time()

    # Subprocess state — only used when standalone=True
    _proc: list[subprocess.Popen | None] = [None]
    _proc_agent: list[str] = [""]

    _BACKEND_DIR = Path(__file__).resolve().parents[3]  # calling-agent/

    # ── Static UI ──────────────────────────────────────────────────────────
    @app.get("/")
    async def root() -> HTMLResponse:
        return HTMLResponse(
            _INDEX_PATH.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    # ── Agent Builder API ──────────────────────────────────────────────────
    @app.get("/api/agents")
    async def list_agents() -> dict:
        """List all agent definitions available in the agents/ directory."""
        result = []
        for yaml_path in sorted(_AGENTS_DIR.glob("*/agent.yaml")):
            name = yaml_path.parent.name
            try:
                agent = AgentDefinition.from_yaml(yaml_path)
                result.append({
                    "name": name,
                    "agent_id": agent.agent_id,
                    "version": agent.version,
                    "agent_name": agent.identity.agent_name,
                    "company": agent.identity.company_name,
                    "languages": agent.languages.supported,
                    "locations": agent.knowledge_base.locations,
                    "stages": [s.id for s in agent.call_flow.stages],
                })
            except Exception as exc:
                result.append({"name": name, "error": str(exc)})
        return {"agents": result}

    @app.get("/api/agents/{name}")
    async def get_agent(name: str) -> dict:
        """Return the full configuration of a named agent."""
        yaml_path = _AGENTS_DIR / name / "agent.yaml"
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        try:
            agent = AgentDefinition.from_yaml(yaml_path)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return dataclasses.asdict(agent)

    # ── Agent Builder Write API ────────────────────────────────────────
    @app.post("/api/agents")
    async def create_agent(request: Request) -> dict:
        """Create a new agent. Body: {"name": "<slug>", ...agent_definition...}."""
        body = await request.json()
        name = body.pop("name", None)
        if not name:
            raise HTTPException(status_code=400, detail="'name' field is required")
        if not name.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(status_code=400, detail="'name' must be alphanumeric with _ or -")
        target_dir = _AGENTS_DIR / name
        if target_dir.exists():
            raise HTTPException(status_code=409, detail=f"Agent '{name}' already exists — use PUT to update")
        try:
            agent = AgentDefinition.from_dict(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Schema validation failed: {exc}")
        target_dir.mkdir(parents=True, exist_ok=True)
        agent.to_yaml(target_dir / "agent.yaml")
        agent_registry.put(name, agent)
        logger.info("[api] created agent '%s'", name)
        return {"status": "created", "name": name, "agent_id": agent.agent_id}

    @app.put("/api/agents/{name}")
    async def update_agent(name: str, request: Request) -> dict:
        """Replace an existing agent's YAML. Body: full agent definition (no 'name' key needed)."""
        yaml_path = _AGENTS_DIR / name / "agent.yaml"
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found — use POST /api/agents to create")
        body = await request.json()
        body.pop("name", None)  # ignore any stray name field
        try:
            agent = AgentDefinition.from_dict(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Schema validation failed: {exc}")
        agent.to_yaml(yaml_path)
        agent_registry.invalidate(name)
        logger.info("[api] updated agent '%s'", name)
        return {"status": "updated", "name": name, "agent_id": agent.agent_id}

    @app.delete("/api/agents/{name}")
    async def archive_agent(name: str) -> dict:
        """Archive an agent by moving its directory to agents/_archived/. Not a hard delete."""
        agent_dir = _AGENTS_DIR / name
        if not agent_dir.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        archived_base = _AGENTS_DIR / "_archived"
        archived_base.mkdir(exist_ok=True)
        dest = archived_base / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(agent_dir), str(dest))
        agent_registry.invalidate(name)
        logger.info("[api] archived agent '%s' to %s", name, dest)
        return {"status": "archived", "name": name, "archived_to": str(dest)}

    @app.post("/api/agents/{name}/validate")
    async def validate_agent(name: str, request: Request) -> dict:
        """Dry-run schema validation. Accepts raw YAML (text/plain) or JSON body."""
        content_type = request.headers.get("content-type", "")
        try:
            if "text/" in content_type or "yaml" in content_type:
                raw = (await request.body()).decode("utf-8")
                body = yaml.safe_load(raw)
            else:
                body = await request.json()
                body.pop("name", None)
            agent = AgentDefinition.from_dict(body)
            return {
                "valid": True,
                "agent_id": agent.agent_id,
                "version": agent.version,
                "stages": [s.id for s in agent.call_flow.stages],
                "languages": agent.languages.supported,
            }
        except Exception as exc:
            return {"valid": False, "error": str(exc)}

    @app.get("/api/agents/active/info")
    async def active_agent_info() -> dict:
        """Return a summary of the currently running agent."""
        # Standalone: get info from registry if pipeline is running
        if standalone:
            if _proc[0] and _proc[0].poll() is None and _proc_agent[0]:
                try:
                    a = agent_registry.get(_proc_agent[0], _AGENTS_DIR)
                    return {
                        "agent_id": a.agent_id,
                        "version": a.version,
                        "agent_name": a.identity.agent_name,
                        "company": a.identity.company_name,
                        "languages": a.languages.supported,
                        "locations": a.knowledge_base.locations,
                        "stages": [s.id for s in a.call_flow.stages],
                    }
                except Exception:
                    pass
            # No pipeline running — return empty 200 to avoid browser console noise
            return {"agent_id": None, "agent_name": None, "company": None,
                    "languages": [], "locations": [], "stages": []}
        # Embedded mode
        try:
            agent = pipeline.conversation._def
            return {
                "agent_id": agent.agent_id,
                "version": agent.version,
                "agent_name": agent.identity.agent_name,
                "company": agent.identity.company_name,
                "languages": agent.languages.supported,
                "locations": agent.knowledge_base.locations,
                "stages": [s.id for s in agent.call_flow.stages],
            }
        except AttributeError:
            raise HTTPException(status_code=503, detail="No active agent")

    # ── Raw YAML edit API ─────────────────────────────────────────────
    @app.get("/api/agents/{name}/raw")
    async def get_agent_raw(name: str) -> PlainTextResponse:
        """Return the raw YAML file content for an agent."""
        yaml_path = _AGENTS_DIR / name / "agent.yaml"
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        return PlainTextResponse(yaml_path.read_text(encoding="utf-8"))

    @app.put("/api/agents/{name}/raw")
    async def update_agent_raw(name: str, request: Request) -> dict:
        """Write raw YAML text to an agent's file after validating the schema."""
        yaml_path = _AGENTS_DIR / name / "agent.yaml"
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found — use POST /api/agents to create")
        text = (await request.body()).decode("utf-8")
        try:
            data = yaml.safe_load(text)
            agent = AgentDefinition.from_dict(data)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"YAML parse/validation failed: {exc}")
        yaml_path.write_text(text, encoding="utf-8")
        agent_registry.invalidate(name)
        logger.info("[api] raw-updated agent '%s'", name)
        return {"status": "updated", "name": name, "agent_id": agent.agent_id}

    # ── Routing config API ────────────────────────────────────────────
    @app.get("/api/agents/{name}/routing")
    async def get_routing(name: str) -> dict:
        """Return the routing.yaml for an agent (empty defaults if missing)."""
        routing_path = _AGENTS_DIR / name / "routing.yaml"
        if not routing_path.exists():
            return {"exists": False, "did": "", "caller_id_prefixes": [], "time_windows": [], "fallback_agent": ""}
        data = yaml.safe_load(routing_path.read_text(encoding="utf-8")) or {}
        return {"exists": True, **data}

    @app.put("/api/agents/{name}/routing")
    async def update_routing(name: str, request: Request) -> dict:
        """Write routing configuration for an agent."""
        routing_path = _AGENTS_DIR / name / "routing.yaml"
        routing_path.parent.mkdir(parents=True, exist_ok=True)
        body = await request.json()
        with open(routing_path, "w", encoding="utf-8") as f:
            yaml.dump(body, f, allow_unicode=True, default_flow_style=False)
        logger.info("[api] updated routing for agent '%s'", name)
        return {"status": "updated"}

    # ── Documents API ─────────────────────────────────────────────────

    @app.post("/api/agents/{name}/documents")
    async def upload_document(
        name: str,
        file: UploadFile = File(...),
        org_name: str = Form(""),
        doc_type: str = Form("General"),
        description: str = Form(""),
    ) -> dict:
        """Upload a document for an agent. Extracts text, chunks it, embeds it."""
        agent_dir = _AGENTS_DIR / name
        if not agent_dir.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        content = await file.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        try:
            from ..documents.extractor import extract_text
            extracted = extract_text(content, file.filename or "upload.txt")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Text extraction failed: {exc}")

        if not extracted.strip():
            raise HTTPException(status_code=422, detail="No text could be extracted from this file")

        from ..documents.store import save_document
        meta = save_document(
            agent_folder=name,
            filename=file.filename or "document.txt",
            content=content,
            extracted_text=extracted,
            org_name=org_name,
            doc_type=doc_type,
            description=description,
        )

        # Embed in the background (non-blocking — returns immediately if OpenAI slow)
        try:
            from ..documents.embeddings import embed_document
            embed_document(name, meta["doc_id"])
        except Exception as exc:
            logger.warning("[api/docs] Embedding failed for %s/%s: %s", name, meta["doc_id"], exc)

        return {"status": "uploaded", "doc_id": meta["doc_id"], "meta": meta}

    @app.get("/api/agents/{name}/documents")
    async def list_agent_documents(name: str) -> dict:
        """List all uploaded documents for an agent."""
        import json as _json
        index_path = _AGENTS_DIR / name / "documents" / "index.json"
        if not index_path.exists():
            return {"documents": []}
        try:
            return {"documents": _json.loads(index_path.read_text(encoding="utf-8"))}
        except Exception as exc:
            logger.warning("[api/docs] Could not read index for '%s': %s", name, exc)
            return {"documents": []}

    @app.delete("/api/agents/{name}/documents/{doc_id}")
    async def delete_agent_document(name: str, doc_id: str) -> dict:
        """Permanently delete a document and its embeddings."""
        from ..documents.store import delete_document
        deleted = delete_document(name, doc_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Document '{doc_id}' not found")
        return {"status": "deleted", "doc_id": doc_id}

    @app.post("/api/agents/{name}/documents/{doc_id}/extract-yaml")
    async def extract_yaml_from_document(name: str, doc_id: str) -> dict:
        """Run LLM extraction on a document and return a diff preview (does NOT apply changes)."""
        from ..documents.store import get_document_text
        text = get_document_text(name, doc_id)
        if not text:
            raise HTTPException(status_code=404, detail="Document text not found")
        from ..documents.yaml_filler import extract_fields_from_text
        result = extract_fields_from_text(text)
        result["agent_folder"] = name
        result["doc_id"] = doc_id
        return result

    @app.post("/api/agents/{name}/documents/{doc_id}/apply-yaml")
    async def apply_yaml_from_document(name: str, doc_id: str, request: Request) -> dict:
        """Apply extracted fields (from the diff preview) to the agent's YAML file."""
        body = await request.json()
        fields = body.get("fields", {})
        if not fields:
            raise HTTPException(status_code=400, detail="No fields provided to apply")
        from ..documents.yaml_filler import apply_fields
        result = apply_fields(name, fields)
        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("error", "Apply failed"))
        return result

    # ── TTS voice preview ─────────────────────────────────────────────
    @app.get("/api/tts/preview")
    async def tts_voice_preview(speaker: str, model: str = "bulbul:v3", language: str = "en-IN",
                                pace: float = 1.0):
        """Stream a short TTS sample and return it as audio/wav for in-browser playback."""
        import struct, os
        import httpx as _httpx
        api_key = os.environ.get("SARVAM_API_KEY", "")
        if not api_key:
            raise HTTPException(status_code=503, detail="SARVAM_API_KEY not configured")
        PREVIEW_TEXT = {
            "en-IN": "Hi there! I'm your AI assistant. How can I help you today?",
            "hi-IN": "नमस्ते! मैं आपकी AI सहायक हूँ। आज मैं आपकी कैसे मदद कर सकती हूँ?",
            "gu-IN": "નમસ્તે! હું તમારી AI સહાયક છું. આજે હું તમને કેવી રીતે મદદ કરી શકું?",
        }
        text = PREVIEW_TEXT.get(language, PREVIEW_TEXT["en-IN"])
        payload = {
            "text": text,
            "target_language_code": language,
            "speaker": speaker,
            "model": model,
            "pace": min(max(pace, 0.5), 2.0),
            "speech_sample_rate": 16000,
            "output_audio_codec": "linear16",
        }
        try:
            async with _httpx.AsyncClient(timeout=15) as client:
                async with client.stream(
                    "POST",
                    "https://api.sarvam.ai/text-to-speech/stream",
                    headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise HTTPException(status_code=resp.status_code, detail=body.decode(errors="replace"))
                    pcm = b"".join([chunk async for chunk in resp.aiter_bytes(4096)])
            sr, ch, bps = 16000, 1, 16
            wav = struct.pack('<4sI4s', b'RIFF', 36 + len(pcm), b'WAVE')
            wav += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, ch, sr, sr * ch * bps // 8, ch * bps // 8, bps)
            wav += struct.pack('<4sI', b'data', len(pcm)) + pcm
            return Response(content=wav, media_type="audio/wav",
                            headers={"Cache-Control": "no-store"})
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Intent preview API ────────────────────────────────────────────
    @app.post("/api/preview/intent")
    async def preview_intent(request: Request) -> dict:
        """Pattern-match caller text against the active agent's intent rules."""
        body = await request.json()
        text = body.get("text", "").strip()
        language = body.get("language", "auto")

        try:
            if standalone:
                # Prefer: body-specified agent → running agent → first available agent
                req_agent = body.get("agent", "").strip()
                agent_name = req_agent or _proc_agent[0]
                if not agent_name:
                    yamls = sorted(_AGENTS_DIR.glob("*/agent.yaml"))
                    if not yamls:
                        raise HTTPException(status_code=503, detail="No agents found — create one first")
                    agent_name = yamls[0].parent.name
                agent_def = agent_registry.get(agent_name, _AGENTS_DIR)
            else:
                agent_def = pipeline.conversation._def
        except HTTPException:
            raise
        except AttributeError:
            raise HTTPException(status_code=503, detail="No active agent loaded")

        kb = agent_def.knowledge_base
        text_lower = text.lower()
        matches: list[dict] = []

        for pattern in kb.objection_patterns:
            if pattern.lower() in text_lower:
                matches.append({"intent": "objection", "pattern": pattern})

        for pattern in kb.time_patterns:
            if pattern.lower() in text_lower:
                matches.append({"intent": "time_request", "pattern": pattern})

        for alias in kb.location_aliases:
            if alias.lower() in text_lower:
                matches.append({"intent": "location_selection", "pattern": alias})

        qa_hits: list[dict] = []
        for field_name, signals in kb.qa_field_signals.items():
            for sig in signals:
                if sig.lower() in text_lower:
                    qa_hits.append({"field": field_name, "signal": sig})

        top_intent = matches[0]["intent"] if matches else ("qa_signal" if qa_hits else "none")

        return {
            "text": text,
            "language": language,
            "intent": top_intent,
            "matches": matches,
            "qa_signals": qa_hits,
            "agent_id": agent_def.agent_id,
            "patterns_checked": {
                "objection": len(kb.objection_patterns),
                "time": len(kb.time_patterns),
                "location": len(kb.location_aliases),
                "qa_fields": len(kb.qa_field_signals),
            },
        }

    # ── Runtime control ───────────────────────────────────────────────
    @app.get("/api/runtime/status")
    async def runtime_status() -> dict:
        """Live pipeline health — works in both embedded and standalone modes."""
        uptime = int(_time_module.time() - _start_time)

        if standalone:
            proc = _proc[0]
            running = proc is not None and proc.poll() is None
            exit_code = proc.returncode if proc and proc.poll() is not None else None

            # Try to get agent details from registry if a name is known
            agent_name, company, languages, stages, agent_id, version = "", "", [], [], "", ""
            if _proc_agent[0]:
                try:
                    a = agent_registry.get(_proc_agent[0], _AGENTS_DIR)
                    agent_name = a.identity.agent_name
                    company     = a.identity.company_name
                    languages   = a.languages.supported
                    stages      = [s.id for s in a.call_flow.stages]
                    agent_id    = a.agent_id
                    version     = a.version
                except Exception:
                    agent_name = _proc_agent[0]

            # Find exit reason from log when process crashed
            exit_reason = ""
            if exit_code is not None and exit_code != 0:
                log_path = _BACKEND_DIR / "backend" / "logs" / "pipeline_latest.log"
                if log_path.exists():
                    try:
                        log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        for line in reversed(log_lines[-30:]):
                            if any(kw in line for kw in ("FATAL", "Error", "error", "Exception", "not found")):
                                exit_reason = line.strip()
                                break
                    except Exception:
                        pass

            return {
                "online": running,
                "standalone": True,
                "mic_enabled": running,
                "pipeline_ws_url": "ws://127.0.0.1:8001/ws" if running else None,
                "agent_name": agent_name,
                "agent_folder": _proc_agent[0],
                "company": company,
                "languages": languages,
                "stages": stages,
                "agent_id": agent_id,
                "version": version,
                "pid": proc.pid if running else None,
                "exit_code": exit_code,
                "exit_reason": exit_reason,
                "uptime_seconds": uptime,
            }

        # Embedded mode (run.py --web)
        try:
            agent = pipeline.conversation._def
            return {
                "online": True,
                "standalone": False,
                "mic_enabled": pipeline.mic_enabled,
                "agent_id": agent.agent_id,
                "agent_name": agent.identity.agent_name,
                "company": agent.identity.company_name,
                "languages": agent.languages.supported,
                "stages": [s.id for s in agent.call_flow.stages],
                "uptime_seconds": uptime,
            }
        except AttributeError:
            return {"online": False, "standalone": False, "mic_enabled": False, "uptime_seconds": uptime}

    @app.post("/api/runtime/pause")
    async def runtime_pause() -> dict:
        """Embedded mode: disable mic. Standalone mode: stop the pipeline process."""
        if standalone:
            proc = _proc[0]
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            _proc[0] = None
            logger.info("[platform] pipeline stopped via pause")
            return {"status": "stopped", "mic_enabled": False}
        pipeline.set_mic_enabled(False)
        logger.info("[api] runtime paused (mic disabled)")
        return {"status": "paused", "mic_enabled": False}

    @app.post("/api/runtime/resume")
    async def runtime_resume() -> dict:
        """Embedded mode: enable mic. Standalone mode: restart the pipeline process."""
        if standalone:
            proc = _proc[0]
            if proc and proc.poll() is None:
                return {"status": "already_running", "mic_enabled": True}
            agent = _proc_agent[0] or "pramukh"
            run_script = _BACKEND_DIR / "backend" / "run.py"
            _log_dir = _BACKEND_DIR / "backend" / "logs"
            _log_dir.mkdir(exist_ok=True)
            _log_f = open(_log_dir / "pipeline_latest.log", "w", buffering=1, encoding="utf-8")
            new_proc = subprocess.Popen(
                [sys.executable, str(run_script), "--agent", agent, "--web", "--port", "8001"],
                cwd=str(_BACKEND_DIR / "backend"),
                stdout=_log_f, stderr=subprocess.STDOUT,
            )
            _proc[0] = new_proc
            logger.info("[platform] pipeline restarted for agent '%s' (pid %d)", agent, new_proc.pid)
            return {"status": "running", "mic_enabled": True, "pid": new_proc.pid}
        pipeline.set_mic_enabled(True)
        logger.info("[api] runtime resumed (mic enabled)")
        return {"status": "running", "mic_enabled": True}

    @app.post("/api/runtime/start")
    async def runtime_start(agent: str = "pramukh") -> dict:
        """Start the pipeline for a given agent (standalone mode only)."""
        if not standalone:
            raise HTTPException(status_code=400, detail="Use /api/agents/{name}/activate in embedded mode")
        proc = _proc[0]
        if proc and proc.poll() is None:
            return {"status": "already_running", "agent": _proc_agent[0], "pid": proc.pid}
        # Validate the agent exists before spawning the subprocess
        agent_yaml = _AGENTS_DIR / agent / "agent.yaml"
        if not agent_yaml.exists():
            raise HTTPException(status_code=422, detail=f"Agent '{agent}' not found — create it first via the wizard.")
        run_script = _BACKEND_DIR / "backend" / "run.py"
        if not run_script.exists():
            raise HTTPException(status_code=500, detail=f"run.py not found at {run_script}")
        _log_dir = _BACKEND_DIR / "backend" / "logs"
        _log_dir.mkdir(exist_ok=True)
        _log_f = open(_log_dir / "pipeline_latest.log", "w", buffering=1, encoding="utf-8")
        new_proc = subprocess.Popen(
            [sys.executable, str(run_script), "--agent", agent, "--web", "--port", "8001"],
            cwd=str(_BACKEND_DIR / "backend"),
            stdout=_log_f, stderr=subprocess.STDOUT,
        )
        _proc[0] = new_proc
        _proc_agent[0] = agent
        logger.info("[platform] started pipeline for agent '%s' (pid %d)", agent, new_proc.pid)
        return {"status": "started", "agent": agent, "pid": new_proc.pid}

    @app.post("/api/runtime/stop")
    async def runtime_stop() -> dict:
        """Stop the running pipeline process (standalone mode only)."""
        if not standalone:
            raise HTTPException(status_code=400, detail="Use /api/runtime/pause in embedded mode")
        proc = _proc[0]
        if not proc or proc.poll() is not None:
            return {"status": "not_running"}
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _proc[0] = None
        logger.info("[platform] pipeline stopped")
        return {"status": "stopped"}

    @app.post("/api/runtime/restart")
    async def runtime_restart(agent: str = "") -> dict:
        """Stop then start the pipeline, optionally switching agent (standalone only)."""
        if not standalone:
            raise HTTPException(status_code=400, detail="Use /api/agents/{name}/activate in embedded mode")
        proc = _proc[0]
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        target = agent or _proc_agent[0] or "pramukh"
        agent_yaml = _AGENTS_DIR / target / "agent.yaml"
        if not agent_yaml.exists():
            raise HTTPException(status_code=422, detail=f"Agent '{target}' not found — create it first via the wizard.")
        run_script = _BACKEND_DIR / "backend" / "run.py"
        _log_dir = _BACKEND_DIR / "backend" / "logs"
        _log_dir.mkdir(exist_ok=True)
        _log_f = open(_log_dir / "pipeline_latest.log", "w", buffering=1, encoding="utf-8")
        new_proc = subprocess.Popen(
            [sys.executable, str(run_script), "--agent", target, "--web", "--port", "8001"],
            cwd=str(_BACKEND_DIR / "backend"),
            stdout=_log_f, stderr=subprocess.STDOUT,
        )
        _proc[0] = new_proc
        _proc_agent[0] = target
        logger.info("[platform] restarted pipeline for agent '%s' (pid %d)", target, new_proc.pid)
        return {"status": "restarted", "agent": target, "pid": new_proc.pid}

    @app.post("/api/runtime/end-call")
    async def runtime_end_call() -> dict:
        """Stop the running pipeline (end the active call). Standalone only."""
        proc = _proc[0]
        if not proc or proc.poll() is not None:
            return {"status": "not_running"}
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _proc[0] = None
        logger.info("[platform] call ended — pipeline stopped")
        return {"status": "ended"}

    @app.get("/api/runtime/logs")
    async def runtime_logs(lines: int = 60) -> dict:
        """Return the last N lines of the pipeline subprocess log."""
        log_path = _BACKEND_DIR / "backend" / "logs" / "pipeline_latest.log"
        if not log_path.exists():
            return {"lines": [], "available": False}
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            all_lines = text.splitlines()
            return {"lines": all_lines[-lines:], "available": True, "total": len(all_lines)}
        except Exception as exc:
            return {"lines": [str(exc)], "available": True, "total": 1}

    # ── API Key Manager ───────────────────────────────────────────────
    from ..voice import key_rotator as _kr

    @app.get("/api/keys/status")
    async def keys_status() -> dict:
        key = _kr.get_active_key()
        masked = (key[:8] + "…" + key[-4:]) if len(key) > 12 else ("set" if key else "")
        pool = _kr._load_pool()
        return {
            "active_key_masked": masked,
            "active_key_set": bool(key),
            "pool_size": len(pool),
            "pool_preview": [(k[:8] + "…" + k[-4:]) for k in pool],
        }

    @app.post("/api/keys/set-active")
    async def keys_set_active(request: Request) -> dict:
        import os as _os
        body = await request.json()
        key = body.get("key", "").strip()
        if not key:
            raise HTTPException(status_code=400, detail="Key cannot be empty")
        _os.environ["SARVAM_API_KEY"] = key
        _kr._update_env(key)
        masked = (key[:8] + "…" + key[-4:]) if len(key) > 12 else "set"
        logger.info("[api/keys] active key updated to %s", masked)
        return {"status": "updated", "masked": masked}

    @app.post("/api/keys/add")
    async def keys_add(request: Request) -> dict:
        body = await request.json()
        new_keys = [k.strip() for k in body.get("keys", []) if k.strip()]
        if not new_keys:
            raise HTTPException(status_code=400, detail="No valid keys provided")
        pool = _kr._load_pool()
        added = 0
        for k in new_keys:
            if k not in pool:
                pool.append(k)
                added += 1
        _kr._save_pool(pool)
        return {"status": "added", "added": added, "pool_size": len(pool)}

    @app.post("/api/keys/rotate")
    async def keys_rotate() -> dict:
        new_key = _kr.rotate()
        if not new_key:
            raise HTTPException(status_code=409, detail="Pool is empty — add backup keys first")
        masked = (new_key[:8] + "…" + new_key[-4:]) if len(new_key) > 12 else "set"
        return {"status": "rotated", "new_key_masked": masked, "pool_size": _kr.pool_size()}

    @app.delete("/api/keys/pool")
    async def keys_clear_pool() -> dict:
        _kr._save_pool([])
        logger.info("[api/keys] backup pool cleared")
        return {"status": "cleared", "pool_size": 0}

    # ── Agent switcher ────────────────────────────────────────────────
    @app.post("/api/agents/{name}/activate")
    async def activate_agent(name: str) -> dict:
        """Hot-swap the active agent in the running pipeline (takes effect next turn)."""
        yaml_path = _AGENTS_DIR / name / "agent.yaml"
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        try:
            agent = AgentDefinition.from_yaml(yaml_path)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Failed to load agent: {exc}")
        if standalone:
            # In standalone mode, just record the selection — do NOT start the subprocess.
            # The pipeline only starts when the user explicitly clicks "Start Call" or
            # "Start Pipeline" in Runtime Control. This prevents auto-starting on Set Active.
            _proc_agent[0] = name
            agent_registry.put(name, agent)
            logger.info("[platform] selected agent '%s' (not started yet)", name)
            return {"status": "selected", "name": name, "agent_id": agent.agent_id,
                    "agent_name": agent.identity.agent_name}
        try:
            pipeline.conversation._def = agent
            agent_registry.put(name, agent)
            logger.info("[api] activated agent '%s' (%s)", name, agent.agent_id)
            return {"status": "activated", "name": name, "agent_id": agent.agent_id,
                    "agent_name": agent.identity.agent_name}
        except AttributeError:
            raise HTTPException(status_code=503, detail="Pipeline not running")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        queue = hub.subscribe()

        try:
            mic_state = pipeline.mic_enabled if pipeline is not None else False
            await ws.send_json({"type": "mic_state", "enabled": mic_state})
        except Exception:
            hub.unsubscribe(queue)
            return

        async def send_loop() -> None:
            try:
                while True:
                    msg = await queue.get()
                    await ws.send_json(msg)
            except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                pass

        async def recv_loop() -> None:
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "mic" and pipeline is not None:
                        pipeline.set_mic_enabled(bool(data.get("enabled")))
            except (WebSocketDisconnect, asyncio.CancelledError):
                pass

        send_task = asyncio.create_task(send_loop())
        recv_task = asyncio.create_task(recv_loop())
        try:
            done, pending = await asyncio.wait(
                [send_task, recv_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            hub.unsubscribe(queue)

    return app
