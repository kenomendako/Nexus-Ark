# discord_manager.py
import discord
import asyncio
import threading
import logging
import os
import re
import json
import datetime
import time
import httpx
import traceback
import wave
import struct
import shutil
from typing import Optional, List, Dict, Any

import config_manager
import gemini_api
from discord import app_commands
from langchain_core.messages import AIMessage
import room_manager
import utils
import constants

try:
    import discord.ext.voice_recv as voice_recv
except Exception:
    voice_recv = None

logger = logging.getLogger(__name__)


def _install_safe_voice_recv_decoder_patch():
    """voice_recvの受信ループをDiscord音声入力向けに安定化する。"""
    if voice_recv is None:
        return
    try:
        from discord.ext.voice_recv import opus as voice_recv_opus
        from discord.ext.voice_recv import utils as voice_recv_utils
    except Exception:
        return
    multi_data_event = getattr(voice_recv_utils, "MultiDataEvent", None)
    if multi_data_event is not None and not getattr(multi_data_event, "_nexus_register_patch", False):
        original_register = multi_data_event.register

        def register_once(self, item):
            if item in self._items:
                return
            original_register(self, item)

        multi_data_event.register = register_once
        multi_data_event._nexus_register_patch = True

    packet_decoder = getattr(voice_recv_opus, "PacketDecoder", None)
    if packet_decoder is None or getattr(packet_decoder, "_nexus_safe_decode_patch", False):
        return

    original_decode_packet = packet_decoder._decode_packet
    original_get_next_packet = packet_decoder._get_next_packet

    def safe_decode_packet(self, packet):
        try:
            return original_decode_packet(self, packet)
        except discord.opus.OpusError as e:
            logger.debug(f"Discord voice opus decode skipped in voice_recv decoder: {e}")
            silence = b"\x00" * (
                discord.opus.Decoder.SAMPLES_PER_FRAME
                * discord.opus.Decoder.SAMPLE_SIZE
            )
            try:
                self._decoder = discord.opus.Decoder()
            except Exception:
                self._decoder = None
            return packet, silence

    def get_next_packet_without_eager_flush(self, timeout):
        packet = self._buffer.pop(timeout=timeout)
        if packet is None:
            return None
        if not packet:
            packet = self._make_fakepacket()
        return packet

    packet_decoder._decode_packet = safe_decode_packet
    packet_decoder._get_next_packet = get_next_packet_without_eager_flush
    packet_decoder._nexus_original_get_next_packet = original_get_next_packet
    packet_decoder._nexus_safe_decode_patch = True


_install_safe_voice_recv_decoder_patch()

_bot_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_unmentioned_help_claims: Dict[str, float] = {}
_unmentioned_help_claims_lock = threading.Lock()
_group_command_claims: Dict[str, float] = {}
_group_command_claims_lock = threading.Lock()

# 後方互換用。旧コードやUIが参照しても落ちないように残す。
_bot_thread: Optional[threading.Thread] = None
_bot_client: Optional["NexusDiscordClient"] = None
_loop: Optional[asyncio.AbstractEventLoop] = None

DISCORD_METADATA_DIR = os.path.join(constants.METADATA_DIR, "discord")
DISCORD_READ_STATE_FILE = os.path.join(DISCORD_METADATA_DIR, "user_read_state.json")
DISCORD_GROUP_SESSIONS_FILE = os.path.join(DISCORD_METADATA_DIR, "group_sessions.json")
DISCORD_GROUP_LOGS_DIR = os.path.join(DISCORD_METADATA_DIR, "group_logs")
DISCORD_GROUP_MAX_ROUNDS = 3
DISCORD_GROUP_MAX_PARTICIPANTS = 5
DISCORD_GROUP_CONTINUE_COOLDOWN_SECONDS = 60
DISCORD_VOICE_SAMPLE_RATE = 48000
DISCORD_VOICE_CHANNELS = 2
DISCORD_VOICE_SAMPLE_WIDTH = 2
DISCORD_VOICE_STT_SAMPLE_RATE = 16000
DISCORD_VOICE_STT_CHANNELS = 1


def _parse_id_list(value) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _is_openai_voice_stt_model(model_name: str) -> bool:
    value = (model_name or "").strip().lower()
    return value == "whisper-1" or value.startswith("openai:") or value.startswith("openai/")


def _normalize_openai_voice_stt_model(model_name: str) -> str:
    value = (model_name or "").strip()
    lowered = value.lower()
    if lowered.startswith("openai:"):
        value = value.split(":", 1)[1].strip()
    elif lowered.startswith("openai/"):
        value = value.split("/", 1)[1].strip()
    return value or "whisper-1"


def _session_key(scope: str, room_name: Optional[str], token: str) -> str:
    if scope == "room" and room_name:
        return f"room:{room_name}"
    return f"global:{hash(token)}"


def _load_read_state() -> Dict[str, Any]:
    try:
        if os.path.exists(DISCORD_READ_STATE_FILE):
            with open(DISCORD_READ_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Discord read state load failed: {e}")
    return {}


def _save_read_state(data: Dict[str, Any]):
    try:
        os.makedirs(DISCORD_METADATA_DIR, exist_ok=True)
        with open(DISCORD_READ_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Discord read state save failed: {e}")


def _extract_log_timestamp(content: str) -> Optional[str]:
    match = re.search(
        r"(\d{4}-\d{2}-\d{2}\s*\([^)]+\)\s*\d{2}:\d{2}:\d{2})(?:\s*\|.*)?\s*$",
        content or "",
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _parse_log_timestamp(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        cleaned = re.sub(r"\s*\([^)]+\)\s*", " ", value).strip()
        return datetime.datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _chunk_text(text: str, limit: int = 1900) -> List[str]:
    if not text:
        return []
    return [text[i:i + limit] for i in range(0, len(text), limit)]


def _select_log_messages(messages: List[Dict[str, Any]], mode: str, limit: int) -> List[Dict[str, Any]]:
    if mode == "since_me":
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "USER":
                return messages[idx:][-limit:]
        return []
    if mode == "today":
        now = datetime.datetime.now()
        since_dt = datetime.datetime(now.year, now.month, now.day)
        selected = []
        for msg in messages:
            ts = _parse_log_timestamp(_extract_log_timestamp(msg.get("content", "")))
            if ts and ts >= since_dt:
                selected.append(msg)
        return selected[-limit:]
    if mode == "latest":
        return messages[-limit:]
    return []


def _claim_unmentioned_help_response(message: discord.Message, window_seconds: float = 2.0) -> bool:
    now = time.monotonic()
    message_id = getattr(message, "id", None)
    if message_id is not None:
        key = f"message:{message_id}"
    else:
        key = f"channel:{getattr(message.channel, 'id', '')}:help"

    with _unmentioned_help_claims_lock:
        expired = [
            claim_key
            for claim_key, claimed_at in _unmentioned_help_claims.items()
            if now - claimed_at > window_seconds
        ]
        for claim_key in expired:
            _unmentioned_help_claims.pop(claim_key, None)

        if key in _unmentioned_help_claims:
            return False
        _unmentioned_help_claims[key] = now
        return True


def _claim_group_command_response(message: discord.Message, window_seconds: float = 2.0) -> bool:
    now = time.monotonic()
    key = f"message:{getattr(message, 'id', '')}"
    with _group_command_claims_lock:
        expired = [
            claim_key
            for claim_key, claimed_at in _group_command_claims.items()
            if now - claimed_at > window_seconds
        ]
        for claim_key in expired:
            _group_command_claims.pop(claim_key, None)
        if key in _group_command_claims:
            return False
        _group_command_claims[key] = now
        return True


def _discord_group_session_key(guild_id: Any, channel_id: Any) -> str:
    return f"{guild_id}:{channel_id}"


def _load_group_sessions() -> Dict[str, Any]:
    try:
        if os.path.exists(DISCORD_GROUP_SESSIONS_FILE):
            with open(DISCORD_GROUP_SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"Discord group sessions load failed: {e}")
    return {}


def _save_group_sessions(data: Dict[str, Any]):
    try:
        os.makedirs(DISCORD_METADATA_DIR, exist_ok=True)
        with open(DISCORD_GROUP_SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Discord group sessions save failed: {e}")


def _safe_discord_group_log_part(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "unknown")).strip("_") or "unknown"


def _create_group_log_path(guild_id: Any, channel_id: Any, created_at: str) -> str:
    created_key = _safe_discord_group_log_part(created_at.replace(":", "").replace("+", "_"))
    filename = f"{_safe_discord_group_log_part(guild_id)}_{_safe_discord_group_log_part(channel_id)}_{created_key}.txt"
    return os.path.join(DISCORD_GROUP_LOGS_DIR, filename)


def _ensure_group_log_path(session: Dict[str, Any]) -> str:
    group_log_path = str(session.get("group_log_path") or "").strip()
    if group_log_path:
        return group_log_path
    group_log_path = _create_group_log_path(
        session.get("guild_id"),
        session.get("channel_id"),
        session.get("created_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    session["group_log_path"] = group_log_path
    return group_log_path


def _append_group_log(group_log_path: str, header: str, text_content: str):
    if not group_log_path or not header or not text_content or not text_content.strip():
        return
    os.makedirs(os.path.dirname(group_log_path), exist_ok=True)
    content_to_append = f"{header.strip()}\n{text_content.strip()}\n\n"
    if not os.path.exists(group_log_path) or os.path.getsize(group_log_path) == 0:
        content_to_append = content_to_append.lstrip()
    with open(group_log_path, "a", encoding="utf-8") as f:
        f.write(content_to_append)
    utils.invalidate_chat_log_cache(group_log_path)


def _append_group_entry_to_logs(group_log_path: str, participants: List[str], header: str, text_content: str):
    _append_group_log(group_log_path, header, text_content)
    for room_name in participants:
        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        if log_file:
            utils.save_message_to_log(log_file, header, text_content)


def _get_group_session_for_message(message: discord.Message) -> Optional[Dict[str, Any]]:
    if not getattr(message, "guild", None):
        return None
    sessions = _load_group_sessions()
    session = sessions.get(_discord_group_session_key(message.guild.id, message.channel.id))
    if isinstance(session, dict) and session.get("active"):
        return session
    return None


def _set_group_session(session: Dict[str, Any]):
    sessions = _load_group_sessions()
    key = _discord_group_session_key(session.get("guild_id"), session.get("channel_id"))
    sessions[key] = session
    _save_group_sessions(sessions)


def _end_group_session_for_message(message: discord.Message) -> Optional[Dict[str, Any]]:
    if not getattr(message, "guild", None):
        return None
    sessions = _load_group_sessions()
    key = _discord_group_session_key(message.guild.id, message.channel.id)
    session = sessions.get(key)
    if isinstance(session, dict):
        session["active"] = False
        session["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        sessions[key] = session
        _save_group_sessions(sessions)
        return session
    return None


def _room_display_name(room_name: str) -> str:
    try:
        config = room_manager.get_room_config(room_name) or {}
        return config.get("agent_display_name") or config.get("room_name") or room_name
    except Exception:
        return room_name


def _find_room_for_bot_user_id(bot_user_id: Any) -> Optional[str]:
    bot_user_id = str(bot_user_id)
    with _sessions_lock:
        for session in _bot_sessions.values():
            if session.get("scope") != "room":
                continue
            client = session.get("client")
            user = getattr(client, "user", None)
            if user and str(getattr(user, "id", "")) == bot_user_id:
                return session.get("room_name")
    return None


def _get_room_bot_session(room_name: str) -> Optional[Dict[str, Any]]:
    with _sessions_lock:
        for session in _bot_sessions.values():
            if session.get("scope") == "room" and session.get("room_name") == room_name and session.get("client") and session.get("loop"):
                return session
    return None


def _parse_group_rounds(command_content: str) -> int:
    match = re.search(r"(?:rounds|巡)\s*[:=]\s*(\d+)", command_content or "", re.IGNORECASE)
    if not match:
        return 1
    try:
        return max(1, min(int(match.group(1)), DISCORD_GROUP_MAX_ROUNDS))
    except Exception:
        return 1


def _parse_group_continue_rounds(command_content: str) -> int:
    return max(1, min(_parse_group_rounds(command_content), 1))


def _seconds_until_group_continue_available(session: Dict[str, Any], now: Optional[float] = None) -> int:
    last_continue_at = session.get("last_continue_at")
    if not last_continue_at:
        return 0
    try:
        elapsed = (now if now is not None else time.time()) - float(last_continue_at)
    except Exception:
        return 0
    remaining = DISCORD_GROUP_CONTINUE_COOLDOWN_SECONDS - elapsed
    return max(0, int(remaining + 0.999))


def _format_group_participants(participants: List[str]) -> str:
    return "、".join(_room_display_name(room) for room in participants)


def _replace_discord_mentions_with_room_names(content: str, mentioned_users: List[Any]) -> str:
    normalized = content or ""
    for mentioned_user in mentioned_users or []:
        user_id = getattr(mentioned_user, "id", "")
        room_name = _find_room_for_bot_user_id(user_id)
        if not room_name:
            continue
        label = _room_display_name(room_name)
        normalized = re.sub(rf"<@!?{re.escape(str(user_id))}>", label, normalized)
    return normalized.strip()


def _format_group_turn_context(turn_entries: List[Dict[str, str]]) -> str:
    lines = []
    for entry in turn_entries:
        speaker = str(entry.get("speaker") or "").strip()
        text = utils.clean_persona_text(utils.remove_ai_timestamp(str(entry.get("text") or ""))).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        if speaker and text:
            lines.append(f"{speaker}: {text}")
    if not lines:
        return ""
    return "【今回のDiscordグループ会話の共有ログ】\n" + "\n\n".join(lines)


def _is_user_authorized_for_room(user_id: Any, room_name: str) -> bool:
    settings = config_manager.get_room_discord_bot_settings(room_name)
    authorized_ids = _parse_id_list(settings.get("authorized_user_ids"))
    return str(user_id) in [str(auth_id) for auth_id in authorized_ids]


def _build_group_agent_args(
    room_name: str,
    controller_room: str,
    active_participants: List[str],
    user_content: str,
    attachments_paths: List[str],
    shared_history_log_path: Optional[str] = None,
    force_user_prompt_parts: bool = False,
) -> Dict[str, Any]:
    responding_log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
    effective_settings = config_manager.get_effective_settings(room_name)
    return {
        "room_to_respond": room_name,
        "api_key_name": effective_settings.get("api_key_name") or config_manager.initial_api_key_name_global,
        "api_history_limit": effective_settings.get("api_history_limit_option", constants.DEFAULT_API_HISTORY_LIMIT_OPTION),
        "debug_mode": False,
        # Group turns are written into each participant's own log.
        # Passing the controller log here would make gemini_api append it as
        # a snapshot and mix the controller's memories into the responder.
        "history_log_path": responding_log_file,
        "shared_history_log_path": shared_history_log_path,
        "suppress_history_image_memory": True,
        "user_prompt_parts": [user_content] if user_content else [],
        "force_user_prompt_parts": force_user_prompt_parts,
        "soul_vessel_room": controller_room,
        "active_participants": active_participants,
        "active_attachments": attachments_paths,
        "shared_location_name": utils.get_current_location(controller_room),
        "shared_scenery_text": None,
        "season_en": None,
        "time_of_day_en": None,
        "global_model_from_ui": config_manager.CONFIG_GLOBAL.get("last_model"),
        "skip_tool_execution": False,
        "enable_supervisor": False,
    }


class _InteractionMessageAdapter:
    def __init__(self, interaction: discord.Interaction, content: str = "", mentions: Optional[List[Any]] = None):
        self.interaction = interaction
        self.content = content
        self.mentions = mentions or []
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.author = interaction.user
        self.id = interaction.id
        self.attachments = []

    async def reply(self, content: str, **kwargs):
        if self.interaction.response.is_done():
            await self.interaction.followup.send(content, **kwargs)
        else:
            await self.interaction.response.send_message(content, **kwargs)


class _ChannelReplyAdapter:
    def __init__(self, channel, author, content: str = ""):
        self.content = content
        self.mentions = []
        self.guild = getattr(channel, "guild", None)
        self.channel = channel
        self.author = author
        self.id = None
        self.attachments = []

    async def reply(self, content: str = "", **kwargs):
        await self.channel.send(content, **kwargs)


if voice_recv is not None:
    class _DiscordVoiceInputSink(voice_recv.AudioSink):
        def __init__(self, client: "NexusDiscordClient", session: Dict[str, Any]):
            self.nexus_client = client
            self.session = session
            self.buffers: Dict[str, Dict[str, Any]] = {}
            self.lock = threading.Lock()
            super().__init__()

        def wants_opus(self):
            return False

        def write(self, user, data):
            if not user or getattr(user, "bot", False):
                return
            allowed_user_id = str(self.session.get("user_id") or "")
            user_id = str(getattr(user, "id", ""))
            if allowed_user_id and user_id != allowed_user_id:
                return
            pcm = getattr(data, "pcm", None) or b""
            if not pcm:
                return

            with self.lock:
                state = self.buffers.get(user_id)
                now = time.time()
                if not state:
                    state = {
                        "buffer": bytearray(),
                        "started_at": now,
                        "last_at": now,
                        "timer": None,
                        "user": user,
                    }
                    self.buffers[user_id] = state
                state["buffer"].extend(pcm)
                state["last_at"] = now
                state["user"] = user

                timer = state.get("timer")
                if timer:
                    timer.cancel()
                silence_seconds = float(self.session.get("silence_seconds") or 1.1)
                state["timer"] = threading.Timer(silence_seconds, self._finalize_user, args=(user_id,))
                state["timer"].daemon = True
                state["timer"].start()

                max_seconds = float(self.session.get("max_seconds") or 12.0)
                byte_limit = int(DISCORD_VOICE_SAMPLE_RATE * DISCORD_VOICE_CHANNELS * DISCORD_VOICE_SAMPLE_WIDTH * max_seconds)
                if len(state["buffer"]) >= byte_limit:
                    state["timer"].cancel()
                    self._finalize_user_locked(user_id)

        def cleanup(self):
            with self.lock:
                user_ids = list(self.buffers.keys())
                for user_id in user_ids:
                    timer = self.buffers[user_id].get("timer")
                    if timer:
                        timer.cancel()
                    self._finalize_user_locked(user_id)

        def _finalize_user(self, user_id: str):
            with self.lock:
                self._finalize_user_locked(user_id)

        def _finalize_user_locked(self, user_id: str):
            state = self.buffers.pop(user_id, None)
            if not state:
                return
            pcm_bytes = bytes(state.get("buffer") or b"")
            duration = len(pcm_bytes) / float(DISCORD_VOICE_SAMPLE_RATE * DISCORD_VOICE_CHANNELS * DISCORD_VOICE_SAMPLE_WIDTH)
            min_seconds = float(self.session.get("min_seconds") or 0.6)
            if duration < min_seconds:
                return

            room_name = str(self.session.get("room_name") or "unknown")
            voice_dir = self._get_voice_input_dir(room_name)
            os.makedirs(voice_dir, exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            audio_path = os.path.join(voice_dir, f"voice_{user_id}_{timestamp}.wav")
            try:
                wav_pcm = self._prepare_pcm_for_stt(pcm_bytes)
                with wave.open(audio_path, "wb") as wf:
                    wf.setnchannels(DISCORD_VOICE_STT_CHANNELS)
                    wf.setsampwidth(DISCORD_VOICE_SAMPLE_WIDTH)
                    wf.setframerate(DISCORD_VOICE_STT_SAMPLE_RATE)
                    wf.writeframes(wav_pcm)
                self._cleanup_voice_dir(voice_dir)
            except Exception as e:
                logger.error(f"Discord voice segment save failed: {e}", exc_info=True)
                return

            loop = self.session.get("loop")
            if loop:
                asyncio.run_coroutine_threadsafe(
                    self.nexus_client._handle_voice_audio_segment(self.session, audio_path, state.get("user"), duration),
                    loop,
                )

        def _prepare_pcm_for_stt(self, pcm_bytes: bytes) -> bytes:
            """Discordの48kHz stereo PCMをSTT向けの16kHz mono PCMへ落とす。"""
            if not pcm_bytes:
                return b""
            frame_size = DISCORD_VOICE_CHANNELS * DISCORD_VOICE_SAMPLE_WIDTH
            sample_count = len(pcm_bytes) // frame_size
            if sample_count <= 0:
                return b""
            output = bytearray()
            step = max(1, DISCORD_VOICE_SAMPLE_RATE // DISCORD_VOICE_STT_SAMPLE_RATE)
            for frame_index in range(0, sample_count, step):
                accum = 0
                used = 0
                for offset in range(step):
                    idx = frame_index + offset
                    if idx >= sample_count:
                        break
                    base = idx * frame_size
                    left, right = struct.unpack_from("<hh", pcm_bytes, base)
                    accum += int((left + right) / 2)
                    used += 1
                if used:
                    sample = max(-32768, min(32767, int(accum / used)))
                    output.extend(struct.pack("<h", sample))
            return bytes(output)

        def _cleanup_voice_dir(self, voice_dir: str):
            try:
                keep_count = int(config_manager.CONFIG_GLOBAL.get("voice_input_audio_rotation_count", 10) or 10)
                keep_count = max(1, min(keep_count, 100))
                wav_files = []
                for filename in os.listdir(voice_dir):
                    if filename.lower().endswith(".wav"):
                        path = os.path.join(voice_dir, filename)
                        if os.path.isfile(path):
                            wav_files.append(path)
                wav_files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
                for old_path in wav_files[keep_count:]:
                    try:
                        os.remove(old_path)
                    except OSError as e:
                        logger.debug(f"Discord voice old wav cleanup skipped: {old_path}: {e}")
            except Exception as e:
                logger.debug(f"Discord voice wav cleanup failed: {e}")

        def _get_voice_input_dir(self, room_name: str) -> str:
            voice_dir = os.path.join(constants.ROOMS_DIR, room_name, "audio_cache", "voice_input", "discord")
            legacy_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.LOGS_DIR_NAME, "discord_voice_input")
            self._migrate_legacy_voice_dir(legacy_dir, voice_dir)
            return voice_dir

        def _migrate_legacy_voice_dir(self, legacy_dir: str, voice_dir: str):
            if not os.path.isdir(legacy_dir):
                return
            try:
                os.makedirs(voice_dir, exist_ok=True)
                for filename in os.listdir(legacy_dir):
                    if not filename.lower().endswith(".wav"):
                        continue
                    src = os.path.join(legacy_dir, filename)
                    dst = os.path.join(voice_dir, filename)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        shutil.move(src, dst)
                try:
                    os.rmdir(legacy_dir)
                except OSError:
                    pass
            except Exception as e:
                logger.debug(f"Discord voice legacy dir migration skipped: {e}")
else:
    _DiscordVoiceInputSink = None


class NexusDiscordClient(discord.Client):
    def __init__(self, room_name: Optional[str], settings: dict, scope: str = "global", *args, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents, *args, **kwargs)
        self.room_name = room_name
        self.settings = config_manager._normalize_discord_bot_settings(settings)
        self.scope = scope
        self.message_lock = asyncio.Lock()
        self.voice_input_session: Optional[Dict[str, Any]] = None
        self.tree = app_commands.CommandTree(self)
        self._register_application_commands()

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            label = self.room_name if self.scope == "room" else "global"
            print(f"--- [Discord Bot] Slash commands synced ({label}): {len(synced)} ---")
        except Exception as e:
            label = self.room_name if self.scope == "room" else "global"
            logger.warning(f"Discord slash command sync failed ({label}): {e}")

    async def on_ready(self):
        label = self.room_name if self.scope == "room" else "global"
        print(f"--- [Discord Bot] '{self.user}' としてログインしました ({label}) ---")
        logger.info(f"Discord Bot logged in as {self.user} ({label})")

    def _authorized_user_ids(self) -> List[str]:
        if self.scope == "room":
            return _parse_id_list(self.settings.get("authorized_user_ids"))
        return _parse_id_list(config_manager.DISCORD_AUTHORIZED_USER_IDS)

    def _allowed_channel_ids(self) -> List[str]:
        return _parse_id_list(self.settings.get("allowed_channel_ids"))

    def _get_room_name(self) -> Optional[str]:
        if self.scope == "room":
            return self.room_name
        return config_manager.DISCORD_BOT_LINKED_ROOM or config_manager.initial_room_global

    def _is_authorized(self, message: discord.Message) -> bool:
        auth_ids = self._authorized_user_ids()
        return str(message.author.id) in [str(aid) for aid in auth_ids]

    def _is_channel_allowed(self, message: discord.Message) -> bool:
        allowed = self._allowed_channel_ids()
        return not allowed or str(message.channel.id) in allowed

    def _is_interaction_authorized(self, interaction: discord.Interaction) -> bool:
        auth_ids = self._authorized_user_ids()
        return str(interaction.user.id) in [str(aid) for aid in auth_ids]

    def _is_interaction_channel_allowed(self, interaction: discord.Interaction) -> bool:
        allowed = self._allowed_channel_ids()
        channel_id = getattr(interaction.channel, "id", "")
        return not allowed or str(channel_id) in allowed

    async def _run_application_command(
        self,
        interaction: discord.Interaction,
        content: str,
        handler,
        mentions: Optional[List[Any]] = None,
        defer: bool = False,
    ):
        if not self._authorized_user_ids() or not self._is_interaction_authorized(interaction):
            await interaction.response.send_message("⚠️ このBotの許可ユーザーIDにあなたが含まれていません。", ephemeral=True)
            return
        if not self._is_interaction_channel_allowed(interaction):
            await interaction.response.send_message("⚠️ このチャンネルではこのBotの操作が許可されていません。", ephemeral=True)
            return
        if defer and not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
        message = _InteractionMessageAdapter(interaction, content=content, mentions=mentions)
        await handler(message)

    async def _twitter_draft_id_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            from twitter_manager import twitter_manager
            twitter_manager.reload()
            room_name = self._get_room_name()
            choices = []
            for draft in twitter_manager.get_pending_list():
                if room_name and draft.get("room_name") not in (None, room_name):
                    continue
                draft_id = str(draft.get("id", ""))
                content = str(draft.get("filtered_content") or draft.get("content") or "")
                if current and current.lower() not in draft_id.lower() and current.lower() not in content.lower():
                    continue
                label = f"{draft_id}: {content[:70]}".strip()
                choices.append(app_commands.Choice(name=label[:100], value=draft_id))
                if len(choices) >= 25:
                    break
            return choices
        except Exception:
            return []

    def _register_application_commands(self):
        @app_commands.command(name="help", description="Nexus ArkのDiscordコマンド一覧を表示します")
        async def slash_help(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/help", self.handle_help_command)

        @app_commands.command(name="commands", description="Nexus ArkのDiscordコマンド一覧を表示します")
        async def slash_commands(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/commands", self.handle_help_command)

        @app_commands.command(name="retry", description="直前のAI応答を削除して再生成します")
        async def slash_retry(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/retry", self.handle_retry_command, defer=True)

        @app_commands.command(name="room", description="旧共通Botで対話対象ルームを切り替えます")
        @app_commands.describe(room_name="切り替え先のルーム名")
        async def slash_room(interaction: discord.Interaction, room_name: str):
            await self._run_application_command(interaction, f"/room {room_name}", lambda msg: self.handle_room_command(msg, f"/room {room_name}"))

        @app_commands.command(name="log", description="チャットログを表示します")
        @app_commands.describe(mode="取得範囲", limit="最大件数（1-50）")
        @app_commands.choices(mode=[
            app_commands.Choice(name="最新のユーザー発言以降", value="since_me"),
            app_commands.Choice(name="最新ログ", value="latest"),
            app_commands.Choice(name="今日", value="today"),
        ])
        async def slash_log(interaction: discord.Interaction, mode: app_commands.Choice[str] = None, limit: int = 20):
            selected_mode = mode.value if mode else "since_me"
            selected_limit = max(1, min(int(limit or 20), 50))
            content = f"/log {selected_mode} {selected_limit}"
            await self._run_application_command(interaction, content, lambda msg: self.handle_log_command(msg, content), defer=True)

        tw_group = app_commands.Group(name="tw", description="Twitter下書き承認コマンド")

        @tw_group.command(name="drafts", description="Twitter承認待ち下書きを一覧表示します")
        async def slash_tw_drafts(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/tw drafts", lambda msg: self.handle_twitter_command(msg, "/tw drafts"), defer=True)

        @tw_group.command(name="show", description="Twitter下書きの詳細を表示します")
        @app_commands.describe(draft_id="下書きID")
        @app_commands.autocomplete(draft_id=self._twitter_draft_id_autocomplete)
        async def slash_tw_show(interaction: discord.Interaction, draft_id: str):
            content = f"/tw show {draft_id}"
            await self._run_application_command(interaction, content, lambda msg: self.handle_twitter_command(msg, content), defer=True)

        @tw_group.command(name="approve", description="Twitter下書きを承認して投稿します")
        @app_commands.describe(draft_id="下書きID")
        @app_commands.autocomplete(draft_id=self._twitter_draft_id_autocomplete)
        async def slash_tw_approve(interaction: discord.Interaction, draft_id: str):
            content = f"/tw approve {draft_id}"
            await self._run_application_command(interaction, content, lambda msg: self.handle_twitter_command(msg, content), defer=True)

        @tw_group.command(name="reject", description="Twitter下書きを却下します")
        @app_commands.describe(draft_id="下書きID")
        @app_commands.autocomplete(draft_id=self._twitter_draft_id_autocomplete)
        async def slash_tw_reject(interaction: discord.Interaction, draft_id: str):
            content = f"/tw reject {draft_id}"
            await self._run_application_command(interaction, content, lambda msg: self.handle_twitter_command(msg, content), defer=True)

        @app_commands.command(name="drafts", description="Twitter承認待ち下書きを一覧表示します")
        async def slash_drafts(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/drafts", lambda msg: self.handle_twitter_command(msg, "/drafts"), defer=True)

        group = app_commands.Group(name="group", description="Discordグループ会話コマンド")

        @group.command(name="start", description="Discordグループ会話を開始します")
        @app_commands.describe(bot_a="参加Bot 1", bot_b="参加Bot 2", bot_c="参加Bot 3（任意）", bot_d="参加Bot 4（任意）", bot_e="参加Bot 5（任意）", rounds="最大自動継続巡数（1-3）")
        async def slash_group_start(
            interaction: discord.Interaction,
            bot_a: discord.Member,
            bot_b: discord.Member,
            bot_c: Optional[discord.Member] = None,
            bot_d: Optional[discord.Member] = None,
            bot_e: Optional[discord.Member] = None,
            rounds: int = 1,
        ):
            mentions = [bot for bot in [bot_a, bot_b, bot_c, bot_d, bot_e] if bot is not None]
            content = f"/group start rounds:{max(1, min(int(rounds or 1), DISCORD_GROUP_MAX_ROUNDS))}"
            await self._run_application_command(interaction, content, lambda msg: self.handle_group_command(msg, content), mentions=mentions, defer=True)

        @group.command(name="status", description="Discordグループ会話の状態を表示します")
        async def slash_group_status(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/group status", lambda msg: self.handle_group_command(msg, "/group status"))

        @group.command(name="members", description="Discordグループ会話の参加者を表示します")
        async def slash_group_members(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/group members", lambda msg: self.handle_group_command(msg, "/group members"))

        @group.command(name="add", description="Discordグループ会話に参加者を追加します")
        @app_commands.describe(bot="追加するBot")
        async def slash_group_add(interaction: discord.Interaction, bot: discord.Member):
            await self._run_application_command(interaction, "/group add", lambda msg: self.handle_group_command(msg, "/group add"), mentions=[bot])

        @group.command(name="remove", description="Discordグループ会話から参加者を外します")
        @app_commands.describe(bot="外すBot")
        async def slash_group_remove(interaction: discord.Interaction, bot: discord.Member):
            await self._run_application_command(interaction, "/group remove", lambda msg: self.handle_group_command(msg, "/group remove"), mentions=[bot])

        @group.command(name="continue", description="ユーザー発言なしで直前の流れからもう1巡だけ続けます")
        async def slash_group_continue(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/group continue", lambda msg: self.handle_group_command(msg, "/group continue"), defer=True)

        @group.command(name="end", description="Discordグループ会話を終了します")
        async def slash_group_end(interaction: discord.Interaction):
            await self._run_application_command(interaction, "/group end", lambda msg: self.handle_group_command(msg, "/group end"))

        for command in [slash_help, slash_commands, slash_retry, slash_room, slash_log, slash_drafts, tw_group, group]:
            self.tree.add_command(command)

    def _own_mention_pattern(self) -> Optional[re.Pattern]:
        if not self.user:
            return None
        return re.compile(rf"<@!?{self.user.id}>")

    def _is_own_mention_in_content(self, content: str) -> bool:
        pattern = self._own_mention_pattern()
        return bool(pattern and pattern.search(content or ""))

    def _content_without_own_mention(self, content: str) -> str:
        pattern = self._own_mention_pattern()
        if not pattern:
            return (content or "").strip()
        return pattern.sub("", content or "").strip()

    def _command_content_without_mentions(self, content: str) -> str:
        return re.sub(r"<@!?\d+>", "", content or "").strip()

    def _is_mentioned(self, message: discord.Message) -> bool:
        if self.user and self.user.mentioned_in(message):
            return True
        return self._is_own_mention_in_content(message.content or "")

    def _channel_response_mode(self, message: discord.Message) -> str:
        channel_id = str(message.channel.id)
        modes = self.settings.get("channel_response_modes", {})
        if isinstance(modes, dict):
            mode = modes.get(channel_id)
            if mode in {"always", "mention", "ignore"}:
                return mode
        return "mention" if self.settings.get("mention_only", False) else "always"

    def _is_targeted_message(self, message: discord.Message) -> bool:
        if self.scope != "room":
            return True
        content = (message.content or "").strip()
        mode = self._channel_response_mode(message)
        if mode == "ignore":
            return False
        if self._is_mentioned(message):
            return True
        if content.startswith("/"):
            return mode == "always"
        return mode == "always"

    def _should_handle_unmentioned_help(self, message: discord.Message, command_content: str) -> bool:
        if self.scope != "room":
            return False
        if command_content not in ("/help", "/commands", "/?"):
            return False
        if self._channel_response_mode(message) != "mention":
            return False
        if self._is_mentioned(message):
            return False
        return _claim_unmentioned_help_response(message)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not self._authorized_user_ids() or not self._is_authorized(message):
            return
        if not self._is_channel_allowed(message):
            return
        command_content = self._command_content_without_mentions(message.content or "")
        if command_content.startswith("/group"):
            if _claim_group_command_response(message):
                await self.handle_group_command(message, command_content)
            return

        group_session = _get_group_session_for_message(message)
        if group_session and not command_content.startswith("/"):
            if self.scope != "room" or self.room_name != group_session.get("controller_room"):
                return
            async with self.message_lock:
                await self.handle_group_chat(message, group_session)
            return

        if not self._is_targeted_message(message):
            if self._should_handle_unmentioned_help(message, command_content):
                await self.handle_help_command(message)
            return

        if command_content in ("/help", "/commands", "/?"):
            await self.handle_help_command(message)
            return
        if command_content.startswith("/room "):
            await self.handle_room_command(message, command_content)
            return
        if command_content == "/retry":
            async with self.message_lock:
                await self.handle_retry_command(message)
            return
        if command_content.startswith("/tw ") or command_content == "/drafts":
            await self.handle_twitter_command(message, command_content)
            return
        if command_content.startswith("/log"):
            await self.handle_log_command(message, command_content)
            return

        async with self.message_lock:
            await self.handle_chat(message)

    async def handle_room_command(self, message: discord.Message, content: Optional[str] = None):
        if self.scope == "room":
            await message.reply("このBotはペルソナ専用Botのため、`/room` による切り替えは無効です。")
            return
        try:
            command_content = content if content is not None else message.content
            requested_room = command_content[6:].strip()
            rooms = room_manager.get_room_list_for_ui()
            folder_names = [r[1] for r in rooms]
            display_names = [r[0] for r in rooms]

            target_folder = None
            display_name = requested_room
            if requested_room in folder_names:
                target_folder = requested_room
                display_name = next(r[0] for r in rooms if r[1] == requested_room)
            elif requested_room in display_names:
                target_folder = next(r[1] for r in rooms if r[0] == requested_room)

            if target_folder:
                config_manager.save_discord_bot_settings(linked_room=target_folder)
                await message.reply(f"✅ 対話対象をルーム「{display_name}」に切り替えました。")
                print(f"--- [Discord Bot] ルーム切り替え: {target_folder} ---")
            else:
                await message.reply(f"❌ ルーム「{requested_room}」が見つかりませんでした。")
        except Exception as e:
            logger.error(f"Room command error: {e}")
            await message.reply(f"⚠️ エラーが発生しました: {e}")

    async def handle_help_command(self, message: discord.Message):
        room_note = (
            "- `/room <ルーム名>`: 旧共通Botで対話対象ルームを切り替えます。"
            if self.scope != "room"
            else "- `/room <ルーム名>`: ペルソナ専用Botでは無効です。"
        )
        lines = [
            "**Nexus Ark Discordコマンド**",
            "- `/help` または `/commands`: このコマンド一覧を表示します。",
            "- `/retry`: 直前のAI応答を削除して再生成します。",
            "- `/log since_me`: 最新のユーザー発言以降のチャットログを表示します。",
            "- `/log latest 20`: 最新ログを最大20件表示します。件数は1〜50で指定できます。",
            "- `/log today`: 今日のチャットログを表示します。",
            "- `/tw drafts`: Twitter承認待ち下書きを一覧表示します。",
            "- `/tw show <ID>`: Twitter下書きの詳細を表示します。",
            "- `/tw approve <ID>`: Twitter下書きを承認して投稿します。",
            "- `/tw reject <ID>`: Twitter下書きを却下します。",
            "- `/group start @BotA @BotB [rounds:1]`: このチャンネルでDiscordグループ会話を開始します。",
            "- `/group status`: Discordグループ会話の状態を表示します。",
            "- `/group members`: Discordグループ会話の参加者を表示します。",
            "- `/group add @BotC`: 実行中のDiscordグループ会話に参加者を追加します。",
            "- `/group remove @BotC`: 実行中のDiscordグループ会話から参加者を外します。",
            "- `/group continue`: ユーザー発言を挟まず、直前の流れからもう1巡だけ続けます。",
            "- `/group end`: このチャンネルのDiscordグループ会話を終了します。",
            room_note,
            "",
            "複数ペルソナがいるチャンネルでは、`@Bot名 /retry` や `/log since_me @Bot名` のように対象Botをメンションしてください。`/help` はメンションなしでも使えます。",
        ]
        await message.reply("\n".join(lines))

    async def handle_group_command(self, message: discord.Message, content: Optional[str] = None):
        command_content = (content if content is not None else self._command_content_without_mentions(message.content or "")).strip()
        args = command_content.split()
        subcommand = args[1].lower() if len(args) > 1 else "status"
        if not getattr(message, "guild", None):
            await message.reply("⚠️ Discordグループ会話はサーバーチャンネルでのみ利用できます。")
            return

        if subcommand == "start":
            await self.handle_group_start_command(message, command_content)
            return
        if subcommand == "end":
            await self.handle_group_end_command(message)
            return
        if subcommand in {"status", "members"}:
            await self.handle_group_status_command(message)
            return
        if subcommand == "add":
            await self.handle_group_add_command(message)
            return
        if subcommand == "remove":
            await self.handle_group_remove_command(message)
            return
        if subcommand == "continue":
            async with self.message_lock:
                await self.handle_group_continue_command(message, command_content)
            return
        await message.reply("使い方: `/group start @BotA @BotB [rounds:1]`, `/group status`, `/group members`, `/group add @BotC`, `/group remove @BotC`, `/group continue`, `/group end`")

    async def handle_group_start_command(self, message: discord.Message, command_content: str):
        existing = _get_group_session_for_message(message)
        if existing:
            await message.reply("⚠️ このチャンネルでは既にDiscordグループ会話が開始されています。`/group status` または `/group end` を使ってください。")
            return

        participants = []
        for mentioned_user in getattr(message, "mentions", []) or []:
            room_name = _find_room_for_bot_user_id(getattr(mentioned_user, "id", ""))
            if room_name and room_name not in participants:
                participants.append(room_name)

        if len(participants) < 2:
            await message.reply("⚠️ 参加するペルソナBotを2体以上メンションしてください。例: `/group start @BotA @BotB`")
            return
        if len(participants) > DISCORD_GROUP_MAX_PARTICIPANTS:
            await message.reply(f"⚠️ 初期実装では参加者は最大{DISCORD_GROUP_MAX_PARTICIPANTS}体までです。")
            return
        unauthorized = [room for room in participants if not _is_user_authorized_for_room(message.author.id, room)]
        if unauthorized:
            await message.reply(f"⚠️ 次のペルソナBotの許可ユーザーIDにあなたが含まれていません: {_format_group_participants(unauthorized)}")
            return

        controller_room = self.room_name if self.scope == "room" and self.room_name in participants else participants[0]
        rounds = _parse_group_rounds(command_content)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        group_log_path = _create_group_log_path(message.guild.id, message.channel.id, now)
        session = {
            "guild_id": str(message.guild.id),
            "channel_id": str(message.channel.id),
            "active": True,
            "owner_user_id": str(message.author.id),
            "controller_room": controller_room,
            "participants": participants,
            "rounds": rounds,
            "mode": "director",
            "created_at": now,
            "updated_at": now,
            "group_log_path": group_log_path,
        }
        _set_group_session(session)

        session_start_message = f"（システム通知：Discordチャンネル「{getattr(message.channel, 'name', message.channel.id)}」でグループ会話が開始されました。参加者: {_format_group_participants(participants)}）"
        _append_group_entry_to_logs(group_log_path, participants, "## SYSTEM:(Discordグループ会話)", session_start_message)

        await message.reply(
            "✅ Discordグループ会話を開始しました。\n"
            f"参加者: {_format_group_participants(participants)}\n"
            f"司会/進行: {_room_display_name(controller_room)}\n"
            f"最大自動継続: {rounds}巡"
        )

    async def handle_group_end_command(self, message: discord.Message):
        session = _get_group_session_for_message(message)
        if not session:
            await message.reply("現在、このチャンネルでDiscordグループ会話は開始されていません。")
            return
        ended = _end_group_session_for_message(message)
        had_group_log_path = bool(ended and ended.get("group_log_path"))
        group_log_path = _ensure_group_log_path(ended) if ended else ""
        if ended and not had_group_log_path:
            _set_group_session(ended)
        session_end_message = "（システム通知：Discordグループ会話が終了しました。）"
        _append_group_entry_to_logs(group_log_path, session.get("participants", []), "## SYSTEM:(Discordグループ会話)", session_end_message)
        await message.reply("✅ Discordグループ会話を終了しました。")

    async def handle_group_status_command(self, message: discord.Message):
        session = _get_group_session_for_message(message)
        if not session:
            await message.reply("現在、このチャンネルでDiscordグループ会話は開始されていません。")
            return
        participants = session.get("participants", [])
        await message.reply(
            "**Discordグループ会話: 実行中**\n"
            f"参加者: {_format_group_participants(participants)}\n"
            f"司会/進行: {_room_display_name(session.get('controller_room'))}\n"
            f"最大自動継続: {session.get('rounds', 1)}巡\n"
            f"モード: {session.get('mode', 'director')}"
        )

    def _mentioned_group_rooms(self, message: discord.Message) -> List[str]:
        rooms = []
        for mentioned_user in getattr(message, "mentions", []) or []:
            room_name = _find_room_for_bot_user_id(getattr(mentioned_user, "id", ""))
            if room_name and room_name not in rooms:
                rooms.append(room_name)
        return rooms

    async def handle_group_add_command(self, message: discord.Message):
        session = _get_group_session_for_message(message)
        if not session:
            await message.reply("現在、このチャンネルでDiscordグループ会話は開始されていません。")
            return

        participants = [room for room in session.get("participants", []) if room]
        unauthorized_existing = [room for room in participants if not _is_user_authorized_for_room(message.author.id, room)]
        if unauthorized_existing:
            await message.reply(f"⚠️ 次のペルソナBotの許可ユーザーIDにあなたが含まれていません: {_format_group_participants(unauthorized_existing)}")
            return

        additions = [room for room in self._mentioned_group_rooms(message) if room not in participants]
        if not additions:
            await message.reply("追加するペルソナBotをメンションしてください。例: `/group add @BotC`")
            return
        if len(participants) + len(additions) > DISCORD_GROUP_MAX_PARTICIPANTS:
            await message.reply(f"⚠️ Discordグループ会話の参加者は最大{DISCORD_GROUP_MAX_PARTICIPANTS}体までです。")
            return

        unauthorized = [room for room in additions if not _is_user_authorized_for_room(message.author.id, room)]
        if unauthorized:
            await message.reply(f"⚠️ 次のペルソナBotの許可ユーザーIDにあなたが含まれていません: {_format_group_participants(unauthorized)}")
            return

        participants.extend(additions)
        session["participants"] = participants
        session["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        group_log_path = _ensure_group_log_path(session)
        _set_group_session(session)

        system_message = f"（システム通知：Discordグループ会話に参加者が追加されました。追加: {_format_group_participants(additions)} / 現在の参加者: {_format_group_participants(participants)}）"
        _append_group_entry_to_logs(group_log_path, participants, "## SYSTEM:(Discordグループ会話)", system_message)
        await message.reply(f"✅ 参加者を追加しました: {_format_group_participants(additions)}")

    async def handle_group_remove_command(self, message: discord.Message):
        session = _get_group_session_for_message(message)
        if not session:
            await message.reply("現在、このチャンネルでDiscordグループ会話は開始されていません。")
            return

        participants = [room for room in session.get("participants", []) if room]
        unauthorized = [room for room in participants if not _is_user_authorized_for_room(message.author.id, room)]
        if unauthorized:
            await message.reply(f"⚠️ 次のペルソナBotの許可ユーザーIDにあなたが含まれていません: {_format_group_participants(unauthorized)}")
            return

        removals = [room for room in self._mentioned_group_rooms(message) if room in participants]
        if not removals:
            await message.reply("外すペルソナBotをメンションしてください。例: `/group remove @BotC`")
            return

        remaining = [room for room in participants if room not in removals]
        if len(remaining) < 2:
            await message.reply("⚠️ Discordグループ会話は参加者が2体以上必要です。終了する場合は `/group end` を使ってください。")
            return

        old_participants = list(participants)
        session["participants"] = remaining
        if session.get("controller_room") not in remaining:
            session["controller_room"] = remaining[0]
        session["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        group_log_path = _ensure_group_log_path(session)
        _set_group_session(session)

        system_message = f"（システム通知：Discordグループ会話から参加者が外れました。除外: {_format_group_participants(removals)} / 現在の参加者: {_format_group_participants(remaining)}）"
        _append_group_entry_to_logs(group_log_path, old_participants, "## SYSTEM:(Discordグループ会話)", system_message)
        await message.reply(f"✅ 参加者を外しました: {_format_group_participants(removals)}")

    async def handle_group_continue_command(self, message: discord.Message, command_content: str):
        session = _get_group_session_for_message(message)
        if not session:
            await message.reply("現在、このチャンネルでDiscordグループ会話は開始されていません。")
            return

        participants = [room for room in session.get("participants", []) if room]
        unauthorized = [room for room in participants if not _is_user_authorized_for_room(message.author.id, room)]
        if unauthorized:
            await message.reply(f"⚠️ 次のペルソナBotの許可ユーザーIDにあなたが含まれていません: {_format_group_participants(unauthorized)}")
            return

        wait_seconds = _seconds_until_group_continue_available(session)
        if wait_seconds > 0:
            await message.reply(f"⏳ `/group continue` は連続実行できません。あと{wait_seconds}秒待ってください。")
            return

        group_log_path = _ensure_group_log_path(session)
        timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
        system_content = f"ユーザーが `/group continue` で、ユーザー発言を挟まずに直前の流れから会話を続けるよう指示しました。\n\n{timestamp_str} | Discord Group"
        _append_group_entry_to_logs(group_log_path, participants, "## SYSTEM:(Discordグループ会話)", system_content)

        await message.reply("✅ 直前の流れからDiscordグループ会話をもう1巡続けます。")
        turn_entries = [{"speaker": "SYSTEM", "text": "ユーザーが会話継続を指示しました。直前の流れを受けて、自然に会話を続けてください。"}]
        await self._run_group_turns(
            message=message,
            session=session,
            turn_entries=turn_entries,
            attachments_paths=[],
            rounds=_parse_group_continue_rounds(command_content),
        )
        session["last_continue_at"] = time.time()
        session["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _set_group_session(session)

    async def handle_voice_command(self, message: discord.Message, content: Optional[str] = None):
        command_content = (content if content is not None else self._command_content_without_mentions(message.content or "")).strip()
        args = command_content.split()
        subcommand = args[1].lower() if len(args) > 1 else "status"
        if subcommand in {"join", "start", "on"}:
            await self.handle_voice_join_command(message)
            return
        if subcommand in {"stop", "end", "off", "leave"}:
            await self.handle_voice_stop_command(message)
            return
        if subcommand == "status":
            await self.handle_voice_status_command(message)
            return
        await message.reply("使い方: `/voice join`, `/voice status`, `/voice stop`")

    async def handle_voice_join_command(self, message: discord.Message):
        room_name = self._get_room_name()
        if not room_name:
            await message.reply("⚠️ ルームが設定されていません。")
            return
        if self.scope != "room":
            await message.reply("⚠️ Discord音声入力はペルソナ専用Botでのみ利用できます。")
            return
        settings = config_manager.get_room_discord_bot_settings(room_name)
        if not settings.get("voice_input_enabled"):
            await message.reply("⚠️ このペルソナのDiscord音声入力が無効です。Nexus ArkのDiscord設定で有効化してください。")
            return
        if voice_recv is None or _DiscordVoiceInputSink is None:
            await message.reply("⚠️ 音声受信用ライブラリが見つかりません。依存関係を同期してからアプリを再起動してください。")
            return
        if not getattr(message, "guild", None):
            await message.reply("⚠️ Discord音声入力はサーバーのボイスチャンネルでのみ利用できます。")
            return

        voice_member = message.author
        try:
            member = message.guild.get_member(message.author.id)
            if member is None:
                member = await message.guild.fetch_member(message.author.id)
            if member is not None:
                voice_member = member
        except Exception as e:
            logger.debug(f"Discord voice member lookup failed: {e}")

        voice_state = getattr(voice_member, "voice", None)
        voice_channel = getattr(voice_state, "channel", None)
        if voice_channel is None:
            await message.reply("⚠️ 先にDiscordのボイスチャンネルへ参加してから `/voice join` を実行してください。")
            return

        if self.voice_input_session and self.voice_input_session.get("active"):
            await message.reply("⚠️ 既にDiscord音声入力が開始されています。停止する場合は `/voice stop` を使ってください。")
            return

        try:
            voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as e:
            logger.error(f"Discord voice connect failed: {e}", exc_info=True)
            await message.reply(f"⚠️ ボイスチャンネルへの参加に失敗しました: {e}")
            return

        timeout_minutes = int(settings.get("voice_input_timeout_minutes", 10) or 10)
        session = {
            "active": True,
            "room_name": room_name,
            "guild_id": str(message.guild.id),
            "voice_channel_id": str(voice_channel.id),
            "text_channel_id": str(message.channel.id),
            "user_id": str(message.author.id),
            "started_at": time.time(),
            "timeout_minutes": timeout_minutes,
            "confirm_transcript": bool(settings.get("voice_input_confirm_transcript", True)),
            "silence_seconds": max(1.8, float(settings.get("voice_input_silence_seconds", 1.8) or 1.8)),
            "min_seconds": float(settings.get("voice_input_min_seconds", 0.6) or 0.6),
            "max_seconds": float(settings.get("voice_input_max_seconds", 12.0) or 12.0),
            "stt_model": str(settings.get("voice_input_stt_model") or constants.DISCORD_VOICE_STT_MODEL),
            "voice_client": voice_client,
            "text_channel": message.channel,
            "author": message.author,
            "loop": asyncio.get_running_loop(),
        }
        try:
            sink = _DiscordVoiceInputSink(self, session)
        except Exception as e:
            logger.error(f"Discord voice sink init failed: {e}", exc_info=True)
            await voice_client.disconnect(force=True)
            await message.reply(f"⚠️ 音声入力の初期化に失敗しました: {e}")
            return
        session["sink"] = sink
        self.voice_input_session = session

        try:
            voice_client.listen(sink)
        except Exception as e:
            logger.error(f"Discord voice listen failed: {e}", exc_info=True)
            await voice_client.disconnect(force=True)
            self.voice_input_session = None
            await message.reply(f"⚠️ 音声入力の開始に失敗しました: {e}")
            return

        session["timeout_task"] = asyncio.create_task(self._voice_input_timeout(session))
        await message.reply(
            "✅ Discord音声入力を開始しました。\n"
            f"対象: {message.author.mention}\n"
            f"ボイスチャンネル: {getattr(voice_channel, 'name', voice_channel.id)}\n"
            f"自動停止: {timeout_minutes}分後\n"
            "ペルソナの返信はDiscordテキストで返します。"
        )

    async def handle_voice_stop_command(self, message: discord.Message):
        if not self.voice_input_session or not self.voice_input_session.get("active"):
            await message.reply("現在、Discord音声入力は開始されていません。")
            return
        session_user_id = str(self.voice_input_session.get("user_id") or "")
        if session_user_id and str(message.author.id) != session_user_id:
            await message.reply("⚠️ 音声入力を開始したユーザーだけが停止できます。")
            return
        await self._stop_voice_input_session("ユーザー操作で停止しました。")
        await message.reply("✅ Discord音声入力を停止しました。")

    async def handle_voice_status_command(self, message: discord.Message):
        session = self.voice_input_session
        if not session or not session.get("active"):
            await message.reply("Discord音声入力: 停止中")
            return
        elapsed = time.time() - float(session.get("started_at") or time.time())
        timeout_seconds = int(session.get("timeout_minutes", 10) or 10) * 60
        remaining = max(0, int(timeout_seconds - elapsed))
        await message.reply(
            "**Discord音声入力: 実行中**\n"
            f"対象ルーム: {_room_display_name(session.get('room_name'))}\n"
            f"対象ユーザー: <@{session.get('user_id')}>\n"
            f"残り時間: 約{remaining // 60}分{remaining % 60}秒\n"
            f"STTモデル: `{session.get('stt_model')}`"
        )

    async def _voice_input_timeout(self, session: Dict[str, Any]):
        try:
            timeout_seconds = int(session.get("timeout_minutes", 10) or 10) * 60
            await asyncio.sleep(timeout_seconds)
            if self.voice_input_session is session and session.get("active"):
                text_channel = session.get("text_channel")
                await self._stop_voice_input_session("タイムアウトで停止しました。")
                if text_channel:
                    await text_channel.send("⏱️ Discord音声入力を自動停止しました。")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Discord voice timeout task failed: {e}", exc_info=True)

    async def _stop_voice_input_session(self, reason: str = ""):
        session = self.voice_input_session
        if not session:
            return
        session["active"] = False
        timeout_task = session.get("timeout_task")
        if timeout_task:
            timeout_task.cancel()
        sink = session.get("sink")
        try:
            if sink and hasattr(sink, "cleanup"):
                sink.cleanup()
        except Exception:
            pass
        voice_client = session.get("voice_client")
        try:
            if voice_client and getattr(voice_client, "is_connected", lambda: False)():
                if hasattr(voice_client, "stop_listening"):
                    voice_client.stop_listening()
                await voice_client.disconnect(force=True)
        except Exception as e:
            logger.warning(f"Discord voice disconnect failed: {e}")
        self.voice_input_session = None
        if reason:
            logger.info(f"Discord voice input stopped: {reason}")

    async def handle_chat(self, message: discord.Message):
        room_name = self._get_room_name()
        if not room_name:
            await message.reply("⚠️ ルームが設定されていません。Web UIでルームを選択するか、共通Botで `/room` コマンドを使用してください。")
            return

        print(f"--- [Discord Bot] メッセージ受信 (Room: {room_name}): {message.content[:50]}... ---")

        user_content = message.content
        if self.user:
            user_content = re.sub(rf"<@!?{self.user.id}>", "", user_content).strip()
        attachments_paths = []
        if message.attachments:
            room_log_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.LOGS_DIR_NAME)
            images_dir = os.path.join(room_log_dir, "images")
            os.makedirs(images_dir, exist_ok=True)

            for i, attachment in enumerate(message.attachments):
                if any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_filename = f"discord_{timestamp}_{i}_{attachment.filename}"
                    file_path = os.path.join(images_dir, safe_filename)
                    try:
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(attachment.url)
                            if resp.status_code == 200:
                                with open(file_path, "wb") as f:
                                    f.write(resp.content)
                                try:
                                    resized_img = utils.resize_image_for_api(file_path, max_size=512, return_image=True)
                                    if resized_img is not None:
                                        resized_img.save(file_path)
                                        resized_img.close()
                                except Exception as img_err:
                                    logger.warning(f"Image resize failed for {attachment.filename}, using original: {img_err}")
                                attachments_paths.append(file_path)
                                user_content += f"\n[VIEW_IMAGE: {file_path}]"
                    except Exception as e:
                        logger.error(f"Attachment download error: {attachment.filename}, {e}")

        await self._submit_discord_user_content(room_name, user_content, attachments_paths, message, source_label="Discord")

    async def _submit_discord_user_content(
        self,
        room_name: str,
        user_content: str,
        attachments_paths: List[str],
        reply_target,
        source_label: str = "Discord",
    ):
        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
        full_user_log_entry = f"{user_content}\n\n{timestamp_str} | {source_label}"
        try:
            utils.save_message_to_log(log_file, "## USER:user", full_user_log_entry)
        except Exception as e:
            logger.error(f"Logging error: {e}")

        await self._execute_ai_interaction(room_name, user_content, attachments_paths, reply_target)

    async def _handle_voice_audio_segment(self, session: Dict[str, Any], audio_path: str, user, duration: float):
        if self.voice_input_session is not session or not session.get("active"):
            return
        room_name = session.get("room_name")
        text_channel = session.get("text_channel")
        if not room_name or not text_channel:
            return

        stt_model = str(session.get("stt_model") or constants.DISCORD_VOICE_STT_MODEL).strip()

        try:
            import stt_manager

            loop = asyncio.get_running_loop()
            if _is_openai_voice_stt_model(stt_model):
                openai_model = _normalize_openai_voice_stt_model(stt_model)
                openai_setting = (
                    config_manager.get_openai_setting_by_name("OpenAI")
                    or config_manager.get_openai_setting_by_name("OpenAI Official")
                )
                openai_api_key = (openai_setting or {}).get("api_key", "")
                if not openai_api_key:
                    await text_channel.send("⚠️ Discord音声入力: OpenAI Whisper用のAPIキーが見つかりません。APIキー/Webhook管理の OpenAI Official に設定してください。")
                    return
                stt_result = await loop.run_in_executor(
                    None,
                    lambda: stt_manager.transcribe_audio_file_openai_detailed(
                        audio_path,
                        openai_api_key,
                        model_name=openai_model,
                        base_url=(openai_setting or {}).get("base_url") or "https://api.openai.com/v1",
                    ),
                )
            else:
                effective_settings = config_manager.get_effective_settings(room_name)
                api_key_name = effective_settings.get("api_key_name") or config_manager.initial_api_key_name_global
                api_key = config_manager.GEMINI_API_KEYS.get(api_key_name)
                if not api_key:
                    await text_channel.send("⚠️ Discord音声入力: STT用のGemini APIキーが見つかりません。")
                    return
                stt_result = await loop.run_in_executor(
                    None,
                    lambda: stt_manager.transcribe_audio_file_detailed(
                        audio_path,
                        api_key,
                        model_name=stt_model or constants.DISCORD_VOICE_STT_MODEL,
                    ),
                )
        except Exception as e:
            logger.error(f"Discord voice STT failed: {e}", exc_info=True)
            error_text = str(e)
            if "一時的に混み合っています" in error_text or "503" in error_text or "UNAVAILABLE" in error_text:
                await text_channel.send("⚠️ Discord音声入力: STT APIが一時的に混み合っています。少し待ってからもう一度試してください。")
            else:
                await text_channel.send(f"⚠️ Discord音声入力: 文字起こしに失敗しました ({error_text[:160]})")
            return

        transcript = (getattr(stt_result, "text", "") or "").strip()
        if not transcript:
            if session.get("confirm_transcript", True):
                await text_channel.send("📝 音声入力: 聞き取れませんでした。もう少し長めにはっきり話してみてください。")
            return
        if getattr(stt_result, "uncertain", False):
            if session.get("confirm_transcript", True):
                await text_channel.send(f"📝 音声入力候補（低信頼）: {transcript}\n誤認識の可能性があるため、ペルソナには送信しませんでした。")
            return
        if duration < 2.0 and len(transcript) <= 2:
            if session.get("confirm_transcript", True):
                await text_channel.send(f"📝 音声入力: 短い断片として認識されたため送信しませんでした（認識: {transcript}）")
            return

        if session.get("confirm_transcript", True):
            display_name = getattr(user, "display_name", None) or getattr(user, "name", "user")
            await text_channel.send(f"📝 {display_name}: {transcript}")

        reply_target = _ChannelReplyAdapter(text_channel, session.get("author") or user, content=transcript)
        async with self.message_lock:
            await self._submit_discord_user_content(
                room_name=room_name,
                user_content=transcript,
                attachments_paths=[],
                reply_target=reply_target,
                source_label="Discord Voice",
            )

    async def handle_group_chat(self, message: discord.Message, session: Dict[str, Any]):
        participants = [room for room in session.get("participants", []) if room]
        controller_room = session.get("controller_room")
        if not controller_room or controller_room not in participants:
            await message.reply("⚠️ Discordグループ会話セッションが不正です。`/group end` 後に開始し直してください。")
            return
        group_log_path = _ensure_group_log_path(session)

        user_content = _replace_discord_mentions_with_room_names(message.content.strip(), getattr(message, "mentions", []))
        attachments_paths = await self._download_message_attachments(message, controller_room, user_content)
        if isinstance(attachments_paths, tuple):
            attachments_paths, user_content = attachments_paths

        timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
        full_user_log_entry = f"{user_content}\n\n{timestamp_str} | Discord Group"
        _append_group_entry_to_logs(group_log_path, participants, "## USER:user", full_user_log_entry)
        turn_entries = [{"speaker": "USER", "text": user_content}]

        await self._run_group_turns(
            message=message,
            session=session,
            turn_entries=turn_entries,
            attachments_paths=attachments_paths,
            rounds=max(1, min(int(session.get("rounds", 1) or 1), DISCORD_GROUP_MAX_ROUNDS)),
        )

    async def _run_group_turns(
        self,
        message: discord.Message,
        session: Dict[str, Any],
        turn_entries: List[Dict[str, str]],
        attachments_paths: Optional[List[str]] = None,
        rounds: int = 1,
    ):
        participants = [room for room in session.get("participants", []) if room]
        controller_room = session.get("controller_room")
        if not controller_room or controller_room not in participants:
            await message.reply("⚠️ Discordグループ会話セッションが不正です。`/group end` 後に開始し直してください。")
            return
        group_log_path = _ensure_group_log_path(session)
        rounds = max(1, min(int(rounds or 1), DISCORD_GROUP_MAX_ROUNDS))
        max_turns = len(participants) * rounds
        speaker_counts = {room: 0 for room in participants}
        last_speaker = None
        active_participants = [room for room in participants if room != controller_room]
        attachments_paths = attachments_paths or []

        try:
            async with message.channel.typing():
                await asyncio.sleep(0)
        except Exception:
            pass
        for turn_index in range(max_turns):
            try:
                import ui_handlers
                current_room = ui_handlers._select_group_speaker_with_director(
                    soul_vessel_room=controller_room,
                    candidates=participants,
                    speaker_counts=speaker_counts,
                    last_speaker=last_speaker,
                    max_rounds=rounds,
                    turn_index=turn_index,
                    debug_mode=False,
                )
            except Exception as e:
                logger.error(f"Discord group speaker selection failed: {e}", exc_info=True)
                current_room = self._choose_group_fallback_speaker(participants, speaker_counts, last_speaker, rounds)

            if not current_room:
                break

            speaker_counts[current_room] = speaker_counts.get(current_room, 0) + 1
            last_speaker = current_room
            try:
                response_text, image_paths = await self._generate_group_ai_response(
                    room_name=current_room,
                    controller_room=controller_room,
                    participants=participants,
                    active_participants=active_participants,
                    user_content=_format_group_turn_context(turn_entries),
                    attachments_paths=attachments_paths if turn_index == 0 else [],
                    shared_history_log_path=None,
                    group_log_path=group_log_path,
                    force_user_prompt_parts=True,
                )
                if response_text or image_paths:
                    await self._send_group_response_as_room(current_room, str(message.channel.id), response_text, image_paths, message.channel)
                    if response_text:
                        turn_entries.append({"speaker": _room_display_name(current_room), "text": response_text})
                else:
                    await message.channel.send(f"({_room_display_name(current_room)} の応答が空でした)")
            except Exception as e:
                logger.error(f"Discord group response failed for {current_room}: {e}", exc_info=True)
                await message.channel.send(f"⚠️ {_room_display_name(current_room)} の応答生成中にエラーが発生しました: {e}")
                break

        session["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _set_group_session(session)

    async def _download_message_attachments(self, message: discord.Message, room_name: str, user_content: str):
        attachments_paths = []
        if not message.attachments:
            return attachments_paths, user_content
        room_log_dir = os.path.join(constants.ROOMS_DIR, room_name, constants.LOGS_DIR_NAME)
        images_dir = os.path.join(room_log_dir, "images")
        os.makedirs(images_dir, exist_ok=True)

        for i, attachment in enumerate(message.attachments):
            if any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp"]):
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                safe_filename = f"discord_group_{timestamp}_{i}_{attachment.filename}"
                file_path = os.path.join(images_dir, safe_filename)
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(attachment.url)
                        if resp.status_code == 200:
                            with open(file_path, "wb") as f:
                                f.write(resp.content)
                            try:
                                resized_img = utils.resize_image_for_api(file_path, max_size=512, return_image=True)
                                if resized_img is not None:
                                    resized_img.save(file_path)
                                    resized_img.close()
                            except Exception as img_err:
                                logger.warning(f"Image resize failed for {attachment.filename}, using original: {img_err}")
                            attachments_paths.append(file_path)
                            user_content += f"\n[VIEW_IMAGE: {file_path}]"
                except Exception as e:
                    logger.error(f"Attachment download error: {attachment.filename}, {e}")
        return attachments_paths, user_content

    async def handle_retry_command(self, message: discord.Message):
        room_name = self._get_room_name()
        if not room_name:
            await message.reply("⚠️ ルームが設定されていません。")
            return

        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        all_messages = utils.load_chat_log(log_file)
        if not all_messages:
            await message.reply("⚠️ ログが空のため、再生成できません。")
            return

        target_msg = None
        for msg in reversed(all_messages):
            if msg.get("role") in ("AGENT", "SYSTEM"):
                target_msg = msg
                break
        if not target_msg:
            await message.reply("⚠️ 再生成対象（AIの応答）が見つかりませんでした。")
            return

        restored_input, deleted_timestamp = utils.delete_and_get_previous_user_input(log_file, target_msg)
        if restored_input is None:
            await message.reply("⚠️ ログの巻き戻しに失敗しました。")
            return

        attachment_pattern = re.compile(r"\[(?:VIEW_IMAGE|GENERATED_IMAGE|ファイル添付):\s*(.*?)\]")
        found_attachments = attachment_pattern.findall(restored_input)
        clean_user_content = attachment_pattern.sub("", restored_input).strip()

        timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d (%a) %H:%M:%S")
        full_user_log_entry = f"{restored_input}\n\n{timestamp_str} | Discord (Retry)"
        utils.save_message_to_log(log_file, "## USER:user", full_user_log_entry)

        await message.reply(f"🔄 直前の応答を破棄し、再生成を開始します... (Room: {room_name})")
        await self._execute_ai_interaction(room_name, clean_user_content, found_attachments, message)

    async def handle_twitter_command(self, message: discord.Message, content: Optional[str] = None):
        room_name = self._get_room_name()
        if not room_name:
            await message.reply("⚠️ ルームが設定されていません。")
            return
        approval_ids = _parse_id_list(self.settings.get("approval_command_allowlist"))
        if approval_ids and str(message.author.id) not in approval_ids:
            await message.reply("⚠️ このユーザーにはTwitter下書き承認コマンドの権限がありません。")
            return

        command_content = (content if content is not None else message.content).strip()
        if command_content == "/drafts":
            args = ["drafts"]
        else:
            args = command_content.split()[1:]
        if not args:
            await message.reply("使い方: `/tw drafts`, `/tw show <ID>`, `/tw approve <ID>`, `/tw reject <ID>`")
            return

        try:
            from twitter_manager import twitter_manager
            twitter_manager.reload()
            pending = [d for d in twitter_manager.get_pending_list() if d.get("room_name") == room_name]
            command = args[0].lower()
            if command == "drafts":
                if not pending:
                    await message.reply("承認待ちのTwitter下書きはありません。")
                    return
                lines = ["承認待ちTwitter下書き:"]
                for draft in pending[:10]:
                    preview = (draft.get("filtered_content") or "").replace("\n", " ")
                    if len(preview) > 80:
                        preview = preview[:79] + "..."
                    lines.append(f"- `{draft.get('id')}`: {preview}")
                await message.reply("\n".join(lines))
                return

            if len(args) < 2:
                await message.reply("下書きIDを指定してください。")
                return
            draft_id = args[1].strip()
            draft = next((d for d in pending if d.get("id") == draft_id), None)
            if not draft:
                await message.reply("指定された下書きが見つからないか、このペルソナの下書きではありません。")
                return

            if command == "show":
                text = draft.get("filtered_content") or ""
                warnings = draft.get("warnings") or []
                detail = f"ID: `{draft_id}`\n{text}"
                if draft.get("reply_to_url"):
                    detail += f"\n返信先: {draft.get('reply_to_url')}"
                if draft.get("media_paths"):
                    detail += f"\n画像: {len(draft.get('media_paths'))}枚"
                if warnings:
                    detail += "\n警告: " + " / ".join(str(w) for w in warnings)
                for part in _chunk_text(detail):
                    await message.reply(part)
                return

            if command == "reject":
                twitter_manager.reject_tweet(draft_id)
                await message.reply(f"🗑️ Twitter下書き `{draft_id}` を却下しました。")
                return

            if command == "approve":
                ok = twitter_manager.approve_tweet(draft_id)
                if not ok:
                    await message.reply("承認処理に失敗しました。")
                    return
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: twitter_manager.execute_post(draft_id, room_name))
                if result.get("success"):
                    await message.reply(f"✅ Twitter投稿に成功しました: {result.get('url', '')}")
                else:
                    twitter_manager.move_back_to_drafts(draft_id)
                    await message.reply(f"❌ Twitter投稿に失敗しました。下書きに戻しました: {result.get('error', '不明なエラー')}")
                return

            await message.reply("使い方: `/tw drafts`, `/tw show <ID>`, `/tw approve <ID>`, `/tw reject <ID>`")
        except Exception as e:
            logger.error(f"Twitter command error: {e}", exc_info=True)
            await message.reply(f"⚠️ Twitterコマンド処理中にエラーが発生しました: {e}")

    async def handle_log_command(self, message: discord.Message, content: Optional[str] = None):
        room_name = self._get_room_name()
        if not room_name:
            await message.reply("⚠️ ルームが設定されていません。")
            return
        command_content = (content if content is not None else message.content).strip()
        args = command_content.split()
        mode = args[1].lower() if len(args) > 1 else "since_me"
        limit = 20
        if len(args) > 2 and args[2].isdigit():
            limit = max(1, min(int(args[2]), 50))

        try:
            log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
            messages = utils.load_chat_log(log_file)
            if mode not in ("since_me", "today", "latest"):
                await message.reply("使い方: `/log since_me`, `/log latest 20`, `/log today`")
                return

            selected = _select_log_messages(messages, mode, limit)

            if not selected:
                await message.reply("該当するログはありません。")
                return
            lines = []
            for msg in selected:
                role = msg.get("role", "")
                name = msg.get("responder") or role
                body = utils.clean_persona_text(utils.remove_ai_timestamp(msg.get("content", ""))).strip()
                body = re.sub(r"\n{3,}", "\n\n", body)
                if mode != "since_me" and len(body) > 300:
                    body = body[:299] + "..."
                lines.append(f"[{role}:{name}] {body}")
            text = "\n\n".join(lines)
            for part in _chunk_text(text):
                await message.reply(part)
        except Exception as e:
            logger.error(f"Log command error: {e}", exc_info=True)
            await message.reply(f"⚠️ ログ取得中にエラーが発生しました: {e}")

    def _choose_group_fallback_speaker(
        self,
        participants: List[str],
        speaker_counts: Dict[str, int],
        last_speaker: Optional[str],
        rounds: int,
    ) -> Optional[str]:
        eligible = [room for room in participants if speaker_counts.get(room, 0) < rounds]
        if not eligible:
            return None
        non_repeat = [room for room in eligible if room != last_speaker] or eligible
        non_repeat.sort(key=lambda room: (speaker_counts.get(room, 0), participants.index(room)))
        return non_repeat[0]

    async def _generate_group_ai_response(
        self,
        room_name: str,
        controller_room: str,
        participants: List[str],
        active_participants: List[str],
        user_content: str,
        attachments_paths: List[str],
        shared_history_log_path: Optional[str] = None,
        group_log_path: Optional[str] = None,
        force_user_prompt_parts: bool = False,
    ) -> tuple[str, List[str]]:
        agent_args = _build_group_agent_args(
            room_name=room_name,
            controller_room=controller_room,
            active_participants=active_participants,
            user_content=user_content,
            attachments_paths=attachments_paths,
            shared_history_log_path=shared_history_log_path,
            force_user_prompt_parts=force_user_prompt_parts,
        )

        def run_agent():
            return list(gemini_api.invoke_nexus_agent_stream(agent_args))

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, run_agent)

        full_response = ""
        captured_model_name = None
        for mode, payload in results:
            if mode == "values" and "messages" in payload:
                last_msg = payload["messages"][-1]
                if isinstance(last_msg, AIMessage):
                    full_response = last_msg.content
            if mode == "values" and payload.get("model_name"):
                captured_model_name = payload.get("model_name")

        content_str = utils.remove_ai_timestamp(full_response)
        if content_str:
            actual_model_name = captured_model_name or config_manager.CONFIG_GLOBAL.get("last_model") or "Gemini"
            timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)} | Discord Group"
            _append_group_entry_to_logs(group_log_path or shared_history_log_path, participants, f"## AGENT:{room_name}", content_str + timestamp)

        final_text = utils.clean_persona_text(full_response)
        img_matches = re.findall(r"\[(?:VIEW_IMAGE|GENERATED_IMAGE):\s*(.*?)\]", full_response)
        image_paths = [img_path.strip() for img_path in img_matches if os.path.exists(img_path.strip())]
        return final_text, image_paths

    async def _send_group_response_as_room(
        self,
        room_name: str,
        channel_id: str,
        response_text: str,
        image_paths: List[str],
        fallback_channel,
    ):
        session = _get_room_bot_session(room_name)

        async def _send_with_client(client):
            channel = client.get_channel(int(channel_id))
            if channel is None:
                channel = await client.fetch_channel(int(channel_id))
            parts = _chunk_text(response_text) if response_text else [""]
            for idx, part in enumerate(parts):
                files = [discord.File(path) for path in image_paths if os.path.exists(path)] if idx == 0 else None
                if part:
                    await channel.send(part, files=files if files else None)
                elif files:
                    await channel.send(files=files)

        if session:
            target_loop = session.get("loop")
            client = session.get("client")
            try:
                running_loop = asyncio.get_running_loop()
                if target_loop is running_loop:
                    await _send_with_client(client)
                else:
                    future = asyncio.run_coroutine_threadsafe(_send_with_client(client), target_loop)
                    await running_loop.run_in_executor(None, future.result, 30)
                return
            except Exception as e:
                logger.error(f"Discord group send as {room_name} failed: {e}", exc_info=True)

        prefix = f"**{_room_display_name(room_name)}**\n"
        parts = _chunk_text(prefix + (response_text or ""))
        for idx, part in enumerate(parts):
            files = [discord.File(path) for path in image_paths if os.path.exists(path)] if idx == 0 else None
            await fallback_channel.send(part, files=files if files else None)

    async def _execute_ai_interaction(self, room_name: str, user_content: str, attachments_paths: List[str], reply_target: discord.Message):
        log_file, _, _, _, _, _, _ = room_manager.get_room_files_paths(room_name)
        internal_state_before = None
        try:
            from motivation_manager import MotivationManager
            mm = MotivationManager(room_name)
            internal_state_before = mm.get_state_snapshot()
        except Exception as e:
            logger.error(f"  - [Arousal] スナップショット取得失敗: {e}")

        effective_settings = config_manager.get_effective_settings(room_name)
        display_thoughts = effective_settings.get("display_thoughts", True)
        agent_args = {
            "room_to_respond": room_name,
            "api_key_name": effective_settings.get("api_key_name") or config_manager.initial_api_key_name_global,
            "api_history_limit": effective_settings.get("api_history_limit_option", constants.DEFAULT_API_HISTORY_LIMIT_OPTION),
            "debug_mode": False,
            "history_log_path": log_file,
            "user_prompt_parts": [user_content],
            "soul_vessel_room": room_name,
            "active_participants": [],
            "active_attachments": attachments_paths,
            "shared_location_name": None,
            "shared_scenery_text": None,
            "season_en": None,
            "time_of_day_en": None,
            "global_model_from_ui": config_manager.CONFIG_GLOBAL.get("last_model"),
            "skip_tool_execution": False,
            "enable_supervisor": False,
        }

        async with reply_target.channel.typing():
            try:
                full_response = ""
                generated_images = []

                def run_agent():
                    return list(gemini_api.invoke_nexus_agent_stream(agent_args))

                loop = asyncio.get_event_loop()
                results = await loop.run_in_executor(None, run_agent)

                captured_model_name = None
                for mode, payload in results:
                    if mode == "values" and "messages" in payload:
                        last_msg = payload["messages"][-1]
                        if isinstance(last_msg, AIMessage):
                            full_response = last_msg.content
                    if mode == "values" and payload.get("model_name"):
                        captured_model_name = payload.get("model_name")

                if full_response:
                    content_str = utils.remove_ai_timestamp(full_response)
                    persona_emotion_pattern = r"<persona_emotion\s+category=[\"'](\w+)[\"']\s+intensity=[\"']([0-9.]+)[\"']\s*/>"
                    emotion_match = re.search(persona_emotion_pattern, content_str, re.IGNORECASE)
                    if emotion_match:
                        try:
                            detected_category = emotion_match.group(1).lower()
                            detected_intensity = float(emotion_match.group(2))
                            from motivation_manager import MotivationManager
                            mm = MotivationManager(room_name)
                            mm.set_persona_emotion(detected_category, detected_intensity)
                            mm._save_state()
                        except Exception as e:
                            logger.error(f"  - [Emotion] 感情反映エラー: {e}")

                    memory_trace_pattern = r"<memory_trace\s+id=[\"']([^\"']+)[\"']\s+resonance=[\"']([0-9.]+)[\"']\s*/>"
                    trace_matches = re.findall(memory_trace_pattern, content_str, re.IGNORECASE)
                    if trace_matches:
                        try:
                            from episodic_memory_manager import EpisodicMemoryManager
                            emm = EpisodicMemoryManager(room_name)
                            for episode_id, resonance_str in trace_matches:
                                emm.update_arousal(episode_id, float(resonance_str))
                        except Exception as e:
                            logger.error(f"  - [MemoryTrace] 共鳴処理エラー: {e}")

                    actual_model_name = captured_model_name or config_manager.CONFIG_GLOBAL.get("last_model") or "Gemini"
                    timestamp = f"\n\n{datetime.datetime.now().strftime('%Y-%m-%d (%a) %H:%M:%S')} | {utils.sanitize_model_name(actual_model_name)}"
                    try:
                        utils.save_message_to_log(log_file, f"## AGENT:{room_name}", content_str + timestamp)
                    except Exception as e:
                        logger.error(f"Failed to save AI response to log: {e}")

                final_text = utils.clean_persona_text(full_response)
                if display_thoughts:
                    thoughts = utils.extract_thoughts_from_text(full_response)
                    if thoughts:
                        quoted_thoughts = "\n".join([f"> {line}" for line in thoughts.split("\n") if line.strip()])
                        if quoted_thoughts:
                            final_text = f"{quoted_thoughts}\n\n{final_text}"

                img_matches = re.findall(r"\[(?:VIEW_IMAGE|GENERATED_IMAGE):\s*(.*?)\]", full_response)
                for img_path in img_matches:
                    if os.path.exists(img_path.strip()):
                        generated_images.append(img_path.strip())

                if final_text:
                    parts = _chunk_text(final_text)
                    for idx, part in enumerate(parts):
                        files = [discord.File(path) for path in generated_images if os.path.exists(path)] if idx == 0 else None
                        await reply_target.reply(part, files=files if files else None)
                elif generated_images:
                    files = [discord.File(path) for path in generated_images if os.path.exists(path)]
                    await reply_target.reply(files=files)
                else:
                    await reply_target.reply("（AIからの応答が空でした）")

                try:
                    from motivation_manager import MotivationManager
                    mm = MotivationManager(room_name)
                    mm.update_last_interaction()
                    if internal_state_before:
                        from arousal_calculator import calculate_arousal
                        internal_state_after = mm.get_state_snapshot()
                        arousal_score = calculate_arousal(internal_state_before, internal_state_after)
                        if full_response:
                            import session_arousal_manager
                            session_arousal_manager.add_arousal_score(room_name, arousal_score, time_str=datetime.datetime.now().strftime("%H:%M:%S"))
                except Exception as e:
                    logger.error(f"Post-interaction processing error: {e}")
            except Exception as e:
                logger.error(f"AI invocation error: {e}", exc_info=True)
                await reply_target.reply(f"⚠️ AIの呼び出し中にエラーが発生しました: {e}")


def _start_single_bot(room_name: Optional[str], settings: dict, scope: str):
    global _bot_thread, _bot_client, _loop
    token = settings.get("token")
    if not token:
        return
    key = _session_key(scope, room_name, token)
    with _sessions_lock:
        session = _bot_sessions.get(key)
        if session and session.get("thread") and session["thread"].is_alive():
            print(f"--- [Discord Bot] 既に実行中です ({key}) ---")
            return

    def run_event_loop():
        global _bot_thread, _bot_client, _loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = NexusDiscordClient(room_name=room_name, settings=settings, scope=scope)
        with _sessions_lock:
            _bot_sessions[key] = {"client": client, "loop": loop, "thread": threading.current_thread(), "room_name": room_name, "settings": settings, "scope": scope}
            if scope == "global":
                _bot_client = client
                _loop = loop
        try:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    loop.run_until_complete(client.start(token))
                    break
                except discord.LoginFailure:
                    print("--- [Discord Bot] ログイン失敗: トークンが無効です ---")
                    break
                except Exception as e:
                    error_msg = str(e)
                    if "sequence" in error_msg and "NoneType" in error_msg:
                        print(f"--- [Discord Bot] ネットワーク接続エラーを検知。再試行します... ({attempt + 1}/{max_retries}) ---")
                    else:
                        print(f"--- [Discord Bot] 起動時エラー: {e}。再試行します... ({attempt + 1}/{max_retries}) ---")
                    if attempt < max_retries - 1:
                        import time
                        time.sleep(5)
                    else:
                        print("--- [Discord Bot] 最大再試行回数に達したため、起動を一時放棄しました ---")
                        traceback.print_exc()
        finally:
            with _sessions_lock:
                _bot_sessions.pop(key, None)
            loop.close()

    thread = threading.Thread(target=run_event_loop, daemon=True)
    with _sessions_lock:
        _bot_sessions[key] = {"client": None, "loop": None, "thread": thread, "room_name": room_name, "settings": settings, "scope": scope}
        if scope == "global":
            _bot_thread = thread
    thread.start()
    print(f"--- [Discord Bot] 起動スレッドを開始しました ({key}) ---")


def start_bot(room_name: Optional[str] = None):
    configs = []
    if room_name:
        settings = config_manager.get_room_discord_bot_settings(room_name)
        if settings.get("enabled") and settings.get("token"):
            configs.append({"room_name": room_name, "settings": settings, "scope": "room"})
    else:
        configs = config_manager.get_enabled_discord_bot_configs(include_global=True)
    if not configs:
        print("--- [Discord Bot] 有効なBot設定がないため起動しません ---")
        return

    seen_tokens = set()
    for cfg in configs:
        token = cfg.get("settings", {}).get("token")
        if not token:
            continue
        if token in seen_tokens:
            print("--- [Discord Bot] 同一Botトークンの重複設定を検知したため、後続設定をスキップしました ---")
            continue
        seen_tokens.add(token)
        _start_single_bot(cfg.get("room_name"), cfg.get("settings", {}), cfg.get("scope", "global"))


def stop_bot(room_name: Optional[str] = None):
    targets = []
    with _sessions_lock:
        for key, session in list(_bot_sessions.items()):
            if room_name and session.get("room_name") != room_name:
                continue
            targets.append((key, session))
    for key, session in targets:
        client = session.get("client")
        loop = session.get("loop")
        if client and loop:
            future = asyncio.run_coroutine_threadsafe(client.close(), loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
        print(f"--- [Discord Bot] 停止しました ({key}) ---")


def stop_global_bot():
    targets = []
    with _sessions_lock:
        for key, session in list(_bot_sessions.items()):
            if session.get("scope") == "global":
                targets.append((key, session))
    for key, session in targets:
        client = session.get("client")
        loop = session.get("loop")
        if client and loop:
            future = asyncio.run_coroutine_threadsafe(client.close(), loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
        print(f"--- [Discord Bot] 停止しました ({key}) ---")


def send_message_to_room(room_name: str, message_text: str, channel_id: Optional[str] = None, image_paths: Optional[List[str]] = None) -> Dict[str, Any]:
    if not room_name:
        return {"success": False, "error": "room_name is required"}
    settings = config_manager.get_room_discord_bot_settings(room_name)
    if not settings.get("allow_autonomous_send", False):
        return {"success": False, "error": "このペルソナではDiscord自律送信が許可されていません。"}
    target_channel_id = str(channel_id or settings.get("default_channel_id") or "").strip()
    if not target_channel_id:
        return {"success": False, "error": "送信先Discordチャンネルが設定されていません。"}
    allowed = _parse_id_list(settings.get("allowed_channel_ids"))
    if allowed and target_channel_id not in allowed:
        return {"success": False, "error": "許可されていないDiscordチャンネルです。"}

    session = None
    with _sessions_lock:
        for candidate in _bot_sessions.values():
            if candidate.get("scope") == "room" and candidate.get("room_name") == room_name and candidate.get("client") and candidate.get("loop"):
                session = candidate
                break
    if not session:
        return {"success": False, "error": "対象ペルソナのDiscord Botが起動していません。"}

    async def _send():
        client = session["client"]
        channel = client.get_channel(int(target_channel_id))
        if channel is None:
            channel = await client.fetch_channel(int(target_channel_id))
        files = []
        for path in image_paths or []:
            if path and os.path.exists(path):
                files.append(discord.File(path))
        await channel.send(utils.clean_persona_text(message_text or ""), files=files if files else None)

    future = asyncio.run_coroutine_threadsafe(_send(), session["loop"])
    try:
        future.result(timeout=20)
        return {"success": True, "message": "Discordへ送信しました。"}
    except Exception as e:
        logger.error(f"Discord autonomous send failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
