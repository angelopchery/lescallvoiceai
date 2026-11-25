"""Entrypoint — run the voice pipeline."""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import os
import sys
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent / "logs"


def _setup_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(LOGS_DIR / "latest.log", mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream_handler, file_handler])


def _validate_env() -> None:
    """Fail fast if required API keys are missing. Logs a masked key on success."""
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if not key:
        print(
            "FATAL: SARVAM_API_KEY is not set. "
            "Add it to calling-agent/.env and restart.",
            file=sys.stderr,
        )
        sys.exit(1)
    masked = key[:6] + "…" + key[-4:]
    print(f"[startup] SARVAM_API_KEY loaded: {masked}", flush=True)


def main() -> None:
    _setup_logging()
    _validate_env()

    if "--test" in sys.argv:
        from app.voice.main_loop import _run_test
        asyncio.run(_run_test())
    elif "--legacy" in sys.argv:
        from app.voice.main_loop import _run
        asyncio.run(_run())
    else:
        import time
        from app.voice.pipeline import VoicePipeline
        from app.voice.conversation import ConversationRouter
        from app.voice.stt.router import STTRouter
        from app.voice.tracking_stt import TrackingSarvamSTT, TrackingWhisperSTT
        from app.voice.tracking_tts import TrackingSarvamTTS
        from app.voice.credit_tracker import SarvamCreditTracker
        from app.agent_definition.schema import AgentDefinition

        # Resolve which agent to load: --agent <name> or --agent-file <path>
        agent_def = None
        _agents_dir = Path(__file__).resolve().parent.parent / "agents"
        if "--agent-file" in sys.argv:
            _idx = sys.argv.index("--agent-file")
            _path = sys.argv[_idx + 1]
            agent_def = AgentDefinition.from_yaml(_path)
            logging.getLogger(__name__).info("[startup] Agent loaded from file: %s", _path)
        elif "--agent" in sys.argv:
            _idx = sys.argv.index("--agent")
            _name = sys.argv[_idx + 1]
            _yaml = _agents_dir / _name / "agent.yaml"
            if not _yaml.exists():
                print(f"FATAL: No agent found at {_yaml}", file=sys.stderr)
                sys.exit(1)
            agent_def = AgentDefinition.from_yaml(_yaml)
            logging.getLogger(__name__).info("[startup] Agent loaded: %s (%s)", _name, agent_def.agent_id)

        if agent_def is None:
            # CLI default agent, passed EXPLICITLY: ConversationRouter no
            # longer falls back to Pramukh on its own (P0 safety substrate).
            from app.agent_definition import get_pramukh_agent
            agent_def = get_pramukh_agent()
            logging.getLogger(__name__).info(
                "[startup] No --agent given — using default Pramukh agent")

        web_enabled = "--web" in sys.argv
        call_id = f"local-{int(time.time())}"
        tracker = SarvamCreditTracker.get()

        from app.agent_definition.registry import agent_registry
        agent_registry.start_watcher(_agents_dir)

        async def run() -> None:
            hub = None
            if web_enabled:
                from app.voice.event_hub import EventHub
                hub = EventHub()

            call_id_provider = lambda: call_id
            _corrections = agent_def.knowledge_base.stt_corrections if agent_def else None
            stt = STTRouter(
                primary=TrackingSarvamSTT(call_id_provider=call_id_provider),
                fallback=TrackingWhisperSTT(call_id_provider=call_id_provider),
                stt_corrections=_corrections,
            )
            # Pick TTS voice from agent definition (default language's voice config)
            _tts_model, _tts_speaker, _tts_pace = "bulbul:v3", "shubh", 1.0
            if agent_def:
                _dlang = agent_def.languages.default
                _vcfg = agent_def.languages.tts_voices.get(_dlang)
                if _vcfg:
                    _tts_model, _tts_speaker = _vcfg.model, _vcfg.speaker
                    _tts_pace = getattr(_vcfg, "pace", 1.0) or 1.0
            tts = TrackingSarvamTTS(
                call_id_provider=call_id_provider,
                model=_tts_model,
                speaker=_tts_speaker,
                pace=_tts_pace,
            )

            pipeline = VoicePipeline(
                conversation=ConversationRouter(
                    agent_def=agent_def,
                    default_language=(
                        (agent_def.languages.default or "hi")
                        if agent_def else "hi"),
                ),
                event_hub=hub,
                stt=stt,
                tts=tts,
                mic_enabled=not web_enabled,  # web UI gates mic; CLI mode keeps it on
            )

            bridge = None
            if web_enabled:
                from app.voice.dashboard_bridge import DashboardBridge
                bridge = DashboardBridge(hub, pipeline.conversation, call_id)

            tracker.start_call(call_id)
            try:
                if not web_enabled:
                    await pipeline.run_until_shutdown()
                    return

                import uvicorn
                from app.web.server import make_app

                # Allow --port N to override the default 8000 (e.g. 8001 when
                # running alongside serve.py which already occupies 8000).
                _web_port = 8000
                if "--port" in sys.argv:
                    _port_idx = sys.argv.index("--port")
                    try:
                        _web_port = int(sys.argv[_port_idx + 1])
                    except (IndexError, ValueError):
                        pass

                app = make_app(hub, pipeline)
                config = uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=_web_port,
                    log_level="warning",
                    access_log=False,
                )
                server = uvicorn.Server(config)

                logging.getLogger(__name__).info(
                    "[WEB] Console at http://127.0.0.1:8000 — mic starts OFF, tap to enable"
                )

                bridge.start()

                pipeline_task = asyncio.create_task(
                    pipeline.run_until_shutdown(), name="pipeline"
                )
                server_task = asyncio.create_task(server.serve(), name="web")

                done, pending = await asyncio.wait(
                    [pipeline_task, server_task], return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                if bridge is not None:
                    await bridge.stop()
                tracker.end_call(call_id)

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            pass
        finally:
            agent_registry.stop_watcher()


if __name__ == "__main__":
    main()
