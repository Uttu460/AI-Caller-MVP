import os
import certifi
import ssl

# SSL setup
_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl
os.environ['SSL_CERT_FILE'] = certifi.where()

import logging
import json
import asyncio
from dotenv import load_dotenv

from livekit import agents, api, rtc
from livekit.agents import Agent, RoomInputOptions, AgentSession
from livekit.plugins import noise_cancellation
from livekit.plugins.google.realtime import RealtimeModel
import livekit.plugins.silero as silero

# Detect if RoomOptions is available or deprecated
_HAS_ROOM_OPTIONS = False
try:
    from livekit.agents import RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    pass

from google.genai import types as _gt

from db import (
    init_db, log_error, get_setting, get_enabled_tools, get_agent_profile
)
from prompts import build_prompt, DEFAULT_SYSTEM_PROMPT
from tools import AppointmentTools

load_dotenv(".env", override=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")


async def _log(level: str, msg: str, detail: str = "") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load settings from DB and put them into OS environment variables."""
    # We do a quick loop through settings if DB is up
    try:
        loop = asyncio.get_event_loop()
        async def _load():
            from db import get_all_settings
            s = await get_all_settings()
            for k, val_info in s.items():
                v = val_info.get("value")
                if v:
                    os.environ[k] = str(v)
        if loop.is_running():
            asyncio.create_task(_load())
        else:
            asyncio.run(_load())
    except Exception as exc:
        print(f"⚠️  Could not pre-load settings: {exc}")


class OutboundAssistant(Agent):
    """Voice assistant wrapped to work with LiveKit's Session."""
    def __init__(self, instructions: str) -> None:
        super().__init__(
            instructions=instructions,
            tools=[],  # Tools are passed directly to AgentSession, keep Agent tools empty
        )


def _build_session(tools: list, system_prompt: str) -> AgentSession:
    """Build a RealtimeModel AgentSession with all 3 silence-prevention configurations."""
    api_key = os.getenv("GOOGLE_API_KEY", "")
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")

    # 1. Transparent session resumption
    session_resumption = _gt.SessionResumptionConfig(transparent=True)

    # 2. Context window compression
    context_window_compression = _gt.ContextWindowCompressionConfig(
        trigger_tokens=25600,
        sliding_window=_gt.SlidingWindow(target_tokens=12800),
    )

    # 3. VAD tuning — 2 second silence threshold, low sensitivity
    realtime_input_config = _gt.RealtimeInputConfig(
        automatic_activity_detection=_gt.AutomaticActivityDetection(
            end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
            silence_duration_ms=2000,
            prefix_padding_ms=200,
        ),
    )

    # Create the RealtimeModel
    realtime_model = RealtimeModel(
        api_key=api_key,
        model=model,
        voice=voice,
        instructions=system_prompt,
        session_resumption=session_resumption,
        context_window_compression=context_window_compression,
        realtime_input_config=realtime_input_config,
    )
    
    vad = silero.VAD.load()
    return AgentSession(
        llm=realtime_model,
        vad=vad,
        tools=tools,
    )


async def entrypoint(ctx: agents.JobContext) -> None:
    """LiveKit agent worker entrypoint — dials via SIP and connects voice AI."""
    await _log("info", f"Agent worker assigned to room: {ctx.room.name}")

    # Parse metadata
    phone_number = None
    lead_name = "there"
    business_name = "our company"
    service_type = "our service"
    custom_prompt = None
    enabled_tools = []

    # Get from job metadata
    try:
        if ctx.job.metadata:
            meta = json.loads(ctx.job.metadata)
            phone_number = meta.get("phone_number") or meta.get("phone")
            lead_name = meta.get("lead_name", lead_name)
            business_name = meta.get("business_name", business_name)
            service_type = meta.get("service_type", service_type)
            custom_prompt = meta.get("system_prompt")
            # Overrides from metadata
            if meta.get("voice_override"):
                os.environ["GEMINI_TTS_VOICE"] = meta["voice_override"]
            if meta.get("model_override"):
                os.environ["GEMINI_MODEL"] = meta["model_override"]
            if meta.get("tools_override"):
                try:
                    enabled_tools = json.loads(meta["tools_override"])
                except Exception:
                    pass
    except Exception as exc:
        await _log("warning", f"Failed to parse job metadata: {exc}")

    # Build prompt
    system_prompt = build_prompt(
        lead_name=lead_name,
        business_name=business_name,
        service_type=service_type,
        custom_prompt=custom_prompt
    )

    # Tool context
    tool_ctx = AppointmentTools(ctx=ctx, phone_number=phone_number, lead_name=lead_name)

    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    # ── Dial — MUST come before session.start() ──────────────────────────────
    if phone_number:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot place outbound call")
            ctx.shutdown()
            return
        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
        except Exception as exc:
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            ctx.shutdown()
            return
        await _log("info", f"Call ANSWERED — {phone_number} picked up, starting AI session now")

    # ── Build and start Gemini Live ──────────────────────────────────────────
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    await _log("info", f"Building AI session — model={gemini_model}")
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    await _log("info", f"Tools loaded: {[t.__name__ for t in active_tools]}")
    session = _build_session(tools=active_tools, system_prompt=system_prompt)

    # Use RoomOptions if available (non-deprecated), else fall back
    # NEVER use close_on_disconnect=True with SIP — drops on any audio blip
    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_options=_RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())),
        )
    else:
        _session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        )

    await session.start(**_session_kwargs)
    await _log("info", "Agent session started — AI ready, generating greeting")

    # ── Optional S3 recording ────────────────────────────────────────────────
    if phone_number:
        _aws_key    = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
        _aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        _aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
        _s3_endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
        _s3_region  = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
        if _aws_key and _aws_secret and _aws_bucket:
            try:
                _recording_path = f"recordings/{ctx.room.name}.ogg"
                _egress_req = api.RoomCompositeEgressRequest(
                    room_name=ctx.room.name, audio_only=True,
                    file_outputs=[api.EncodedFileOutput(
                        file_type=api.EncodedFileType.OGG, filepath=_recording_path,
                        s3=api.S3Upload(access_key=_aws_key, secret=_aws_secret,
                                        bucket=_aws_bucket, region=_s3_region, endpoint=_s3_endpoint),
                    )],
                )
                _egress = await ctx.api.egress.start_room_composite_egress(_egress_req)
                _s3_ep = _s3_endpoint.rstrip("/")
                tool_ctx.recording_url = (f"{_s3_ep}/{_aws_bucket}/{_recording_path}"
                                           if _s3_ep else f"s3://{_aws_bucket}/{_recording_path}")
                await _log("info", f"Recording started: egress={_egress.egress_id}")
            except Exception as _exc:
                await _log("warning", f"Recording start failed (non-fatal): {_exc}")

    # ── Greeting ─────────────────────────────────────────────────────────────
    # gemini-3.1 and gemini-2.5 native-audio speak autonomously from system prompt.
    # generate_reply() is blocked by the plugin for these models — skip it entirely.
    _active_model = os.getenv("GEMINI_MODEL", "")
    if "3.1" in _active_model or "2.5" in _active_model:
        await _log("info", "Gemini native-audio: model will greet autonomously from system prompt")
    else:
        greeting = (
            f"The call just connected. Greet the lead and ask if you're speaking with {lead_name}."
            if phone_number else "Greet the caller warmly."
        )
        try:
            await session.generate_reply(instructions=greeting)
        except Exception as _gr_exc:
            await _log("warning", f"generate_reply failed: {_gr_exc}")

    # ── Keep session alive until SIP participant actually leaves ─────────────
    # Without this block, the entrypoint returns and the process spins down.
    # We watch participant_disconnected for the specific SIP identity.
    if phone_number:
        _sip_identity = f"sip_{phone_number}"
        _disconnect_event = asyncio.Event()

        def _on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity == _sip_identity:
                _disconnect_event.set()
        def _on_disconnected():
            _disconnect_event.set()

        ctx.room.on("participant_disconnected", _on_participant_disconnected)
        ctx.room.on("disconnected", _on_disconnected)

        try:
            await asyncio.wait_for(_disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call reached 1-hour safety timeout — shutting down")

        await _log("info", f"SIP participant disconnected — ending session for {phone_number}")
        await session.aclose()
    else:
        _done = asyncio.Event()
        ctx.room.on("disconnected", lambda: _done.set())
        try:
            await asyncio.wait_for(_done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    init_db()
    load_db_settings_to_env()
    agents.cli.run_app(
        agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller")
    )
