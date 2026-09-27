"""Friend galgame plugin: play a bound QQ private chat as a visual novel scene.

Assets live in two shared libraries: characters (each with several sprites, and
every sprite tagged with emotion labels) and backgrounds. A bound friend points
at one character plus one background, so the same character can be reused.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import mimetypes
import re
from pathlib import Path
from time import time

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    json_response,
    request,
    stream_response,
)
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .store import GalgameStore

PLUGIN_NAME = "astrbot_plugin_galgame_friend"
PAGE_NAME = "galgame"
# Art that ships inside the plugin. Treated as read only: the plugin directory is
# replaced wholesale on every update, so nothing user created may live here.
BUNDLED_ASSETS_DIR = Path(__file__).resolve().parent / "pages" / PAGE_NAME / "assets"
# Uploaded art lives with the rest of the plugin's persistent data.
PLUGIN_DATA_DIR = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
DATA_ASSETS_DIR = PLUGIN_DATA_DIR / "assets"
THUMBNAIL_DIR = PLUGIN_DATA_DIR / "thumbnails"
ALLOWED_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
# Display limits for the images handed to the page. A plugin Page runs in a
# sandboxed iframe that cannot reach the dashboard's own static routes, so the
# only way it can show an uploaded file is for the backend to inline the bytes.
STAGE_MAX_SIDE = 1536
STAGE_QUALITY = 85
THUMBNAIL_MAX_SIDE = 320
THUMBNAIL_QUALITY = 72
# Example art shipped inside the plugin, so a fresh install has something to show
# before the user uploads their own. File names stay ASCII because the plugin is
# distributed as a zip archive, while the emotion labels are Chinese on purpose:
# they are what the page displays and what the model must answer with.
DEFAULT_SPRITES = (
    ("default_neutral.webp", ["中性"]),
    ("default_happy.webp", ["兴奋"]),
    ("default_expect.webp", ["期待"]),
    ("default_shy.webp", ["害羞"]),
    ("default_sad.webp", ["委屈"]),
    ("default_disgust.webp", ["嫌弃"]),
)
DEFAULT_BACKGROUND = ("示例背景", "default_bg.webp")
DEFAULT_CHARACTER_NAME = "示例角色"
DEFAULTS_IMPORTED_KEY = "defaults_imported"
AFFECTION_MIN = 0
AFFECTION_MAX = 100
AFFECTION_MIN_STEP = -3
AFFECTION_MAX_STEP = 3
SELF_PERSONA_KEY = "self_persona"
DEFAULT_PERSONA = (
    "你是一位恋爱对话军师，替用户本人斟酌下一句话。"
    "台词必须是用户本人可以直接发送给对方的口语，不要代替对方说话，不要写旁白或动作描写。"
    "只输出 JSON，不要输出解释。"
)
JSON_RETRY_NOTE = (
    "\n\n注意：上一次的输出不是合法 JSON，无法解析。请重新输出，"
    "并且**字符串内部不要出现英文双引号**（需要引号时用中文引号「」），"
    "不要输出代码块标记或任何解释文字。"
)
# Human-readable placeholders for non-text components, so the model knows the
# friend sent a sticker or a voice clip instead of seeing an empty message.
COMPONENT_LABELS = (
    ("Image", "[图片]"),
    ("Record", "[语音]"),
    ("Video", "[视频]"),
    ("Face", "[表情]"),
    ("AtAll", "[@全体成员]"),
    ("At", "[@某人]"),
    ("Reply", "[引用了一条消息]"),
    ("Poke", "[戳一戳]"),
    ("Nodes", "[合并转发]"),
    ("Forward", "[合并转发]"),
    ("File", "[文件]"),
    ("Json", "[卡片消息]"),
    ("Share", "[分享]"),
    ("Music", "[音乐]"),
    ("Location", "[位置]"),
    ("Contact", "[名片]"),
    ("RPS", "[猜拳]"),
    ("Dice", "[骰子]"),
    ("Shake", "[窗口抖动]"),
    ("Unknown", "[其他消息]"),
)


def _describe_message(event) -> tuple[str, str, list[str], list[str]]:
    """Turn an incoming event into scene text plus the media it carries.

    Args:
        event: Incoming message event.

    Returns:
        Tuple of (text stored in the scene, message kind, image references,
        audio references that can be transcribed).
    """
    text = event.get_message_str().strip()
    labels: list[str] = []
    images: list[str] = []
    audios: list[str] = []
    for component in event.get_messages():
        name = type(component).__name__
        if name == "Image":
            ref = (
                getattr(component, "path", "")
                or getattr(component, "file", "")
                or getattr(component, "url", "")
            )
            if ref:
                images.append(str(ref))
        if name == "Record":
            ref = (
                getattr(component, "path", "")
                or getattr(component, "file", "")
                or getattr(component, "url", "")
            )
            if ref:
                audios.append(str(ref))
        if name == "File":
            # Keep the file name: it costs nothing and lets the model react
            # sensibly without any AI step.
            file_name = str(getattr(component, "name", "") or "").strip()
            label = f"[文件：{file_name}]" if file_name else "[文件]"
            if label not in labels:
                labels.append(label)
            continue
        for class_name, label in COMPONENT_LABELS:
            if name == class_name:
                if label not in labels:
                    labels.append(label)
                break
    if labels:
        suffix = " ".join(labels)
        text = f"{text} {suffix}".strip() if text else suffix
    kind = labels[0].strip("[]") if labels else "text"
    return (text or "[空消息]"), kind, images, audios


LINE_FORMAT_NOTE = (
    "\n\n重要：请**不要使用 JSON**，改用下面这种极简格式输出，"
    "避免任何转义问题。每行一条候选，用竖线分隔三个字段：\n"
    "角度|好感度|台词\n"
    "好感度是 -3 到 +3 的整数。最后另起一行写表情标签，格式 tag|标签。\n"
    "示例：\n"
    "温柔关心|2|在吗，我一直都在\n"
    "幽默调侃|-1|你这么晚才想起我啊\n"
    "tag|中性\n"
    "除这些行以外不要输出任何其他内容。"
)


def _repair_json_text(raw: str) -> str:
    """Best-effort repair of JSON produced by an LLM.

    Models routinely embed unescaped ASCII double quotes inside a string value
    (for example ``"reason":"用玩笑化解"被盗号"的调侃"``), which makes the whole
    document invalid. Quotes that are not acting as JSON delimiters get escaped.

    Args:
        raw: Raw model output, possibly wrapped in a code fence.

    Returns:
        Repaired JSON text.
    """
    text = re.sub(r"```[a-zA-Z]*", "", raw).strip()
    openers = set("{[,:")
    closers = set(",}]:")
    out: list[str] = []
    for index, char in enumerate(text):
        if char != '"':
            out.append(char)
            continue
        prev = next((item for item in reversed(text[:index]) if not item.isspace()), "")
        nxt = next((item for item in text[index + 1 :] if not item.isspace()), "")
        if prev in openers or nxt in closers:
            out.append(char)
        else:
            out.append('\\"')
    repaired = "".join(out)
    # Drop trailing commas before a closing bracket.
    return re.sub(r",\s*([}\]])", r"\1", repaired)


def _load_json_lenient(raw: str) -> dict:
    """Parse the JSON object contained in a model reply.

    Args:
        raw: Raw model output.

    Returns:
        Parsed object, or an empty dict when nothing could be recovered.
    """
    matched = re.search(r"\{.*\}", raw, re.S)
    if not matched:
        return {}
    text = matched.group()
    for candidate in (text, _repair_json_text(text)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _coerce_item(candidate: object) -> dict | None:
    """Normalize one option entry into a text/angle/affection dict.

    Args:
        candidate: Either a plain string or a dict produced by the model.

    Returns:
        Normalized dict, or ``None`` when the entry has no usable text.
    """
    if isinstance(candidate, dict):
        text = str(candidate.get("text") or "").strip()
        angle = str(candidate.get("angle") or "").strip()
        value = candidate.get("affection")
    else:
        text = str(candidate).strip()
        angle = ""
        value = 0
    if not text:
        return None
    delta = (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0
    )
    return {
        "text": text,
        "angle": angle,
        "affection": max(AFFECTION_MIN_STEP, min(AFFECTION_MAX_STEP, delta)),
    }


def _extract_items(data: dict) -> list[dict]:
    """Pull normalized candidates out of a parsed JSON object.

    Args:
        data: Parsed model output.

    Returns:
        List of candidate dicts, possibly empty.
    """
    raw_options = data.get("options")
    if not isinstance(raw_options, list):
        return []
    items: list[dict] = []
    for candidate in raw_options:
        item = _coerce_item(candidate)
        if item is not None:
            items.append(item)
    return items


def _regex_items(raw: str) -> list[dict]:
    """Recover candidates from JSON that still fails to parse.

    Args:
        raw: Raw model output.

    Returns:
        List of candidate dicts, possibly empty.
    """
    repaired = _repair_json_text(raw)
    pattern = re.compile(
        r'\{\s*"text"\s*:\s*"(?P<text>.*?)"\s*'
        r'(?:,\s*"angle"\s*:\s*"(?P<angle>.*?)"\s*)?'
        r'(?:,\s*"affection"\s*:\s*(?P<affection>-?\d+)\s*)?\}',
        re.S,
    )
    items: list[dict] = []
    for matched in pattern.finditer(repaired):
        item = _coerce_item(
            {
                "text": matched.group("text"),
                "angle": matched.group("angle") or "",
                "affection": matched.group("affection") or 0,
            }
        )
        if item is not None:
            items.append(item)
    return items


def _pipe_items(raw: str) -> tuple[list[dict], str]:
    """Parse the delimiter format used as the last-resort fallback.

    The format is one candidate per line as ``angle|affection|text`` plus an
    optional ``tag|label`` line. It carries no quoting rules, so it cannot break
    the way JSON does. Lines without a separator are ignored: a prose answer must
    never be shown to the user as a candidate.

    Args:
        raw: Raw model output.

    Returns:
        Tuple of (candidates, portrait tag).
    """
    items: list[dict] = []
    tag = ""
    for line in raw.splitlines():
        text = line.strip().strip("`").strip()
        if not text or text.startswith("{") or text.startswith("["):
            continue
        parts = [part.strip() for part in text.split("|")]
        if parts[0].lower() in {"tag", "标签", "表情"} and len(parts) >= 2:
            tag = "|".join(parts[1:]).strip()
            continue
        if len(parts) >= 3:
            digits = parts[1].lstrip("+-")
            item = _coerce_item(
                {
                    "text": "|".join(parts[2:]),
                    "angle": parts[0],
                    "affection": int(parts[1]) if digits.isdigit() else 0,
                }
            )
        elif len(parts) == 2:
            item = _coerce_item({"text": parts[1], "angle": parts[0], "affection": 0})
        else:
            continue
        if item is not None:
            items.append(item)
    return items, tag


# Cached answer of _resolve_block_polarity(); resolved once per process.
_BLOCK_POLARITY: bool | None = None


def _resolve_block_polarity() -> bool:
    """Work out which argument suppresses AstrBot's default reply.

    In current AstrBot the ``call_llm`` attribute defaults to False and the
    pipeline gates its default LLM call on ``not event.call_llm``, so the
    argument of ``should_call_llm`` effectively means "forbid". The naming is
    confusing enough that upstream may flip it in a later release, so the gate
    is inspected once and the detected behaviour is used instead of assumed.

    Returns:
        The argument that blocks the default reply (True on current versions).
    """
    global _BLOCK_POLARITY
    if _BLOCK_POLARITY is not None:
        return _BLOCK_POLARITY
    detected = True
    try:
        import inspect

        from astrbot.core.pipeline.process_stage import stage as pipeline_stage

        source = inspect.getsource(pipeline_stage)
        detected = "not event.call_llm" in source
    except Exception:
        # Unknown pipeline shape: keep the behaviour verified against 4.27.x.
        detected = True
    _BLOCK_POLARITY = detected
    return detected


class FriendGalgame(Star):
    """Render bound private chats as galgame scenes and send the chosen lines."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """Register the page APIs and prepare in-memory scene state.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration managed by AstrBot.
        """
        super().__init__(context, config)
        self.config = config or {}
        self.store = GalgameStore(
            Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "galgame.sqlite3"
        )
        self.subscribers: set[asyncio.Queue] = set()
        self.pending: dict[str, asyncio.Task] = {}
        # Image references attached to the most recent message of each session,
        # consumed by the next generation so a vision model can see them.
        self.recent_images: dict[str, list[str]] = {}
        for route, handler, methods, desc in (
            (f"/{PLUGIN_NAME}/library", self.api_library, ["GET"], "Shared libraries"),
            (
                f"/{PLUGIN_NAME}/character/create",
                self.api_character_create,
                ["POST"],
                "Create a character",
            ),
            (
                f"/{PLUGIN_NAME}/character/rename",
                self.api_character_rename,
                ["POST"],
                "Rename a character",
            ),
            (
                f"/{PLUGIN_NAME}/character/delete",
                self.api_character_delete,
                ["POST"],
                "Delete a character",
            ),
            (
                f"/{PLUGIN_NAME}/sprite/add",
                self.api_sprite_add,
                ["POST"],
                "Attach a tagged sprite",
            ),
            (
                f"/{PLUGIN_NAME}/sprite/update",
                self.api_sprite_update,
                ["POST"],
                "Edit sprite tags or default",
            ),
            (
                f"/{PLUGIN_NAME}/sprite/delete",
                self.api_sprite_delete,
                ["POST"],
                "Delete a sprite",
            ),
            (
                f"/{PLUGIN_NAME}/background/add",
                self.api_background_add,
                ["POST"],
                "Add a background",
            ),
            (
                f"/{PLUGIN_NAME}/background/delete",
                self.api_background_delete,
                ["POST"],
                "Delete a background",
            ),
            (
                f"/{PLUGIN_NAME}/friends",
                self.api_friends,
                ["GET"],
                "Bound friends and recently seen sessions",
            ),
            (f"/{PLUGIN_NAME}/bind", self.api_bind, ["POST"], "Bind a session"),
            (
                f"/{PLUGIN_NAME}/relation",
                self.api_relation,
                ["POST"],
                "Set the relationship with one friend",
            ),
            (
                f"/{PLUGIN_NAME}/settings",
                self.api_settings,
                ["GET"],
                "Global persona settings",
            ),
            (
                f"/{PLUGIN_NAME}/settings/save",
                self.api_settings_save,
                ["POST"],
                "Save global persona settings",
            ),
            (f"/{PLUGIN_NAME}/unbind", self.api_unbind, ["POST"], "Remove a binding"),
            (
                f"/{PLUGIN_NAME}/scene",
                self.api_scene,
                ["GET"],
                "Scene log, candidates, and state",
            ),
            (
                f"/{PLUGIN_NAME}/log",
                self.api_log,
                ["GET"],
                "Full chat history",
            ),
            (
                f"/{PLUGIN_NAME}/regenerate",
                self.api_regenerate,
                ["POST"],
                "Regenerate reply candidates",
            ),
            (f"/{PLUGIN_NAME}/send", self.api_send, ["POST"], "Send a chosen line"),
            (
                f"/{PLUGIN_NAME}/assets",
                self.api_assets,
                ["GET"],
                "List uploaded files",
            ),
            (
                f"/{PLUGIN_NAME}/upload",
                self.api_upload,
                ["POST"],
                "Upload an image",
            ),
            (
                f"/{PLUGIN_NAME}/image",
                self.api_image,
                ["GET"],
                "Read one asset as a data URL",
            ),
            (
                f"/{PLUGIN_NAME}/defaults",
                self.api_load_defaults,
                ["POST"],
                "Load the bundled example assets",
            ),
            (
                f"/{PLUGIN_NAME}/events",
                self.api_events,
                ["GET"],
                "Scene event stream",
            ),
        ):
            context.register_web_api(route, handler, methods, desc)

    async def initialize(self) -> None:
        """Prepare the database, the data directories, and the example library."""
        await self.store.initialize()
        DATA_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
        # Some platforms (notably Windows, which asks the registry) have no
        # mapping for .webp. Without this a .webp asset is described as
        # application/octet-stream, and ``X-Content-Type-Options: nosniff`` then
        # makes the browser refuse to render it.
        mimetypes.add_type("image/webp", ".webp", strict=True)
        try:
            await self.migrate_plugin_dir_assets()
            await self.import_default_assets(initial=True)
        except Exception as exc:  # noqa: BLE001
            # Both steps only restore convenience content. A failure here (for
            # example an unwritable directory) must never keep the plugin from
            # loading; the page can retry the example import on demand.
            self.logger.warning("Skipped asset preparation: %s", exc)
        # Logged so a support request can tell whether the reply-blocking call
        # matched this AstrBot version.
        self.logger.info(
            "Friend galgame ready. Reply blocking uses should_call_llm(%s).",
            _resolve_block_polarity(),
        )

    async def terminate(self) -> None:
        """Cancel pending generations and close every open page stream."""
        for task in self.pending.values():
            task.cancel()
        self.pending.clear()
        for queue in list(self.subscribers):
            queue.put_nowait(None)
        self.subscribers.clear()

    def list_assets(self) -> list[str]:
        """List every image available to the libraries.

        Returns:
            Asset file names, sorted, with duplicates removed: the bundled
            example art plus everything the user uploaded. ``.gitkeep`` and any
            other non-image file is left out.
        """
        names: set[str] = set()
        for directory in (BUNDLED_ASSETS_DIR, DATA_ASSETS_DIR):
            if not directory.is_dir():
                continue
            names.update(
                item.name
                for item in directory.iterdir()
                if item.is_file() and item.suffix.lower() in ALLOWED_ASSET_SUFFIXES
            )
        return sorted(names)

    def find_asset(self, name: str) -> Path | None:
        """Resolve an asset name to a file, preferring the user's own uploads.

        Args:
            name: Bare file name as stored in the library.

        Returns:
            Absolute path of the file, or ``None`` when the name is unsafe, is
            not an image, or does not exist in either asset directory.
        """
        if not name or name != Path(name).name:
            # Rejects subdirectories, ``..`` and absolute paths.
            return None
        if Path(name).suffix.lower() not in ALLOWED_ASSET_SUFFIXES:
            return None
        for directory in (DATA_ASSETS_DIR, BUNDLED_ASSETS_DIR):
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None

    async def migrate_plugin_dir_assets(self) -> int:
        """Move uploads that older versions stored inside the plugin directory.

        The plugin directory is replaced on every update, so pictures kept there
        were silently lost. Moving them next to the database both rescues them
        and keeps the plugin directory read only. Library entries reference files
        by bare name, so nothing else has to change.

        Returns:
            Number of files moved.
        """
        if not BUNDLED_ASSETS_DIR.is_dir():
            return 0
        moved = 0
        for item in list(BUNDLED_ASSETS_DIR.iterdir()):
            if not item.is_file() or item.name.startswith("default_"):
                continue
            if item.suffix.lower() not in ALLOWED_ASSET_SUFFIXES:
                continue
            target = DATA_ASSETS_DIR / item.name
            if target.exists():
                # Already migrated; the plugin directory copy is a leftover.
                target = DATA_ASSETS_DIR / f"{item.stem}_{int(time())}{item.suffix}"
            try:
                item.replace(target)
            except OSError as exc:
                self.logger.warning("Could not move %s: %s", item.name, exc)
                continue
            moved += 1
        if moved:
            self.logger.info("Moved %s uploaded asset(s) out of the plugin dir.", moved)
        return moved

    def encode_asset(self, path: Path, thumbnail: bool) -> str | None:
        """Inline one image as a data URL the sandboxed page can display.

        The page runs in an iframe without ``allow-same-origin``, so it cannot
        authenticate a plain ``<img src>`` against the plugin Web API and every
        image has to travel through the bridge as text. Thumbnails also keep the
        library list small when the user uploads multi-megabyte pictures.

        Args:
            path: Image file to read.
            thumbnail: Downscale the image for a list thumbnail instead of
                returning it at display size.

        Returns:
            A ``data:`` URL, or ``None`` when the image cannot be decoded.
        """
        max_side = THUMBNAIL_MAX_SIDE if thumbnail else STAGE_MAX_SIDE
        quality = THUMBNAIL_QUALITY if thumbnail else STAGE_QUALITY
        cache = THUMBNAIL_DIR / (
            hashlib.sha1(
                f"{path}:{path.stat().st_mtime_ns}:{max_side}".encode()
            ).hexdigest()
            + ".webp"
        )
        try:
            if cache.is_file():
                payload = cache.read_bytes()
            else:
                from PIL import Image

                with Image.open(path) as source:
                    image = source.convert("RGBA")
                    if max(image.size) > max_side:
                        image.thumbnail((max_side, max_side), Image.LANCZOS)
                    buffer = io.BytesIO()
                    image.save(buffer, format="WEBP", quality=quality, method=4)
                payload = buffer.getvalue()
                cache.write_bytes(payload)
            encoded = base64.b64encode(payload).decode("ascii")
            return f"data:image/webp;base64,{encoded}"
        except Exception as exc:  # noqa: BLE001
            # Also covers a missing Pillow, which only affects resizing.
            self.logger.warning("Could not encode %s: %s", path.name, exc)
            return None

    async def import_default_assets(self, initial: bool = False) -> dict:
        """Register the bundled example art in the shared libraries.

        Safe to call repeatedly: only the entries still missing are added, so the
        page can offer a "load example assets" action after the user deleted
        them. Deleting a library entry never removes a file, which is what makes
        this recovery possible.

        Args:
            initial: When ``True`` this is the first start after installation.
                An installation that already owns characters or backgrounds is
                left untouched, so upgrading never pollutes an existing library.

        Returns:
            Counts of the sprites and backgrounds actually added.
        """
        characters = await self.store.list_characters()
        backgrounds = await self.store.list_backgrounds()
        if initial:
            if await self.store.get_setting(DEFAULTS_IMPORTED_KEY):
                return {"sprites": 0, "backgrounds": 0}
            await self.store.set_setting(DEFAULTS_IMPORTED_KEY, "1")
            if characters or backgrounds:
                return {"sprites": 0, "backgrounds": 0}
        added_sprites = 0
        added_backgrounds = 0
        wanted = [
            (file, tags)
            for file, tags in DEFAULT_SPRITES
            if (BUNDLED_ASSETS_DIR / file).is_file()
        ]
        if wanted:
            character = next(
                (item for item in characters if item["name"] == DEFAULT_CHARACTER_NAME),
                None,
            )
            if character is None:
                character_id = await self.store.create_character(DEFAULT_CHARACTER_NAME)
                existing_files: set[str] = set()
            else:
                character_id = character["id"]
                existing_files = {sprite["file"] for sprite in character["sprites"]}
            for file, tags in wanted:
                if file in existing_files:
                    continue
                await self.store.add_sprite(character_id, file, tags)
                added_sprites += 1
        name, file = DEFAULT_BACKGROUND
        if (BUNDLED_ASSETS_DIR / file).is_file() and file not in {
            item["file"] for item in backgrounds
        }:
            await self.store.add_background(name, file)
            added_backgrounds += 1
        if added_sprites or added_backgrounds:
            self.logger.info(
                "Imported example assets: %s sprites, %s backgrounds.",
                added_sprites,
                added_backgrounds,
            )
            self.broadcast({"type": "library_changed"})
        return {"sprites": added_sprites, "backgrounds": added_backgrounds}

    def resolve_sprite(self, character: dict | None, tag: str) -> str:
        """Pick the sprite file matching an emotion tag.

        Args:
            character: Character dict with a ``sprites`` list.
            tag: Emotion tag chosen by the LLM, may be empty.

        Returns:
            Asset file name, empty when the character has no sprite at all.
        """
        sprites = (character or {}).get("sprites") or []
        if not sprites:
            return ""
        wanted = tag.strip().casefold()
        if wanted:
            for sprite in sprites:
                if any(wanted == item.casefold() for item in sprite["tags"]):
                    return sprite["file"]
        for sprite in sprites:
            if sprite["is_default"]:
                return sprite["file"]
        return sprites[0]["file"]

    def character_tags(self, character: dict | None) -> list[str]:
        """Collect every emotion tag the character can actually show.

        Args:
            character: Character dict with a ``sprites`` list.

        Returns:
            Sorted, de-duplicated tag list.
        """
        tags: list[str] = []
        for sprite in (character or {}).get("sprites") or []:
            for tag in sprite["tags"]:
                if tag not in tags:
                    tags.append(tag)
        return sorted(tags)

    def block_default_llm(self, event) -> None:
        """Ask AstrBot to skip its own reply for one incoming message.

        The dashboard's own chat page produces the same private-message event
        type, so it is skipped: blocking it would silently break the built-in
        WebUI chat.

        Args:
            event: Incoming message event.
        """
        if event.get_platform_name() == "webchat":
            return
        event.should_call_llm(_resolve_block_polarity())

    async def transcribe(self, umo: str, audios: list[str]) -> str:
        """Turn a voice message into text with AstrBot's STT provider.

        Args:
            umo: Unified message origin of the private session.
            audios: Audio references attached to the message.

        Returns:
            Transcript, empty when no STT provider is configured or it failed.
        """
        if not audios:
            return ""
        try:
            stt = await self.context.get_using_stt_provider_async(umo)
        except Exception:
            self.logger.exception("获取语音识别模型失败")
            return ""
        if stt is None:
            return ""
        try:
            return (await stt.get_text(audios[0]) or "").strip()
        except Exception:
            self.logger.exception("语音转文字失败")
            return ""

    async def caption_images(self, images: list[str]) -> str:
        """Describe images with AstrBot's configured image-caption provider.

        This is the fallback used when the chat model cannot look at the picture
        itself, so a text-only model still understands what the friend sent.

        Args:
            images: Image references (URLs or local paths).

        Returns:
            Description, empty when no caption provider is configured.
        """
        if not images:
            return ""
        try:
            settings = self.context.get_config().get("provider_settings", {})
        except Exception:
            settings = {}
        settings = settings or {}
        provider_id = str(
            settings.get("default_image_caption_provider_id") or ""
        ).strip()
        if not provider_id:
            return ""
        provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            return ""
        prompt = str(
            settings.get("image_caption_prompt")
            or "请用中文描述这张图片的内容、画面主体的表情和情绪。"
        )
        try:
            response = await provider.text_chat(prompt=prompt, image_urls=images)
        except Exception:
            self.logger.exception("图片转述失败")
            return ""
        return (response.completion_text or "").strip()

    def broadcast(self, payload: dict) -> None:
        """Push one scene event to every open page stream.

        Args:
            payload: JSON-serializable scene event.
        """
        event = {"ts": int(time()), **payload}
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def schedule_options(self, umo: str, delay: float | None = None) -> None:
        """Start candidate generation, replacing an in-flight run.

        Args:
            umo: Unified message origin of the private session.
            delay: Seconds to wait before calling the model. ``None`` uses the
                configured debounce so a friend typing several messages in a row
                only triggers one request; pass 0 for an explicit user action.
        """
        running = self.pending.get(umo)
        if running and not running.done():
            running.cancel()
        if delay is None:
            delay = max(0.0, float(self.config.get("debounce_seconds", 2.5)))
        task = asyncio.create_task(self._generate_after_pause(umo, delay))
        self.pending[umo] = task

        def _forget(finished: asyncio.Task) -> None:
            if self.pending.get(umo) is finished:
                self.pending.pop(umo, None)

        task.add_done_callback(_forget)

    async def _generate_after_pause(self, umo: str, delay: float) -> None:
        """Wait for the friend to stop typing, then generate candidates.

        Args:
            umo: Unified message origin of the private session.
            delay: Seconds to wait, 0 to generate immediately.
        """
        if delay:
            # A new message cancels this task and starts a fresh timer, so the
            # request only happens once the friend stops sending.
            await asyncio.sleep(delay)
        await self.generate_options(umo)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        """Stage an incoming private message as the friend's next line.

        Args:
            event: Incoming private message event.
        """
        umo = event.unified_msg_origin
        if event.get_sender_id() == event.get_self_id():
            return
        text, kind, images, audios = _describe_message(event)
        if audios:
            # Voice: reuse the speech-to-text provider the user configured in
            # AstrBot, so a voice message arrives as an actual sentence.
            transcript = await self.transcribe(umo, audios)
            if transcript:
                text = text.replace("[语音]", f"[语音：{transcript}]")
        if images:
            # Kept in memory only until the candidates for this exchange are
            # generated, so a vision model can look at the sticker the friend
            # just sent without storing image files on disk.
            self.recent_images[umo] = images

        await self.store.record_session(
            umo,
            event.get_sender_id(),
            event.get_sender_name() or event.get_sender_id(),
            text,
        )
        binding = await self.store.get_binding(umo)
        staged = binding is not None and bool(binding["enabled"])

        # A bound friend is played by hand, so AstrBot's own reply is taken out of
        # the way. Unbound sessions keep their normal behaviour unless the user
        # explicitly asked to take over every private chat.
        if not bool(self.config.get("keep_auto_reply", False)) and (
            staged or bool(self.config.get("block_unbound_private", False))
        ):
            self.block_default_llm(event)

        if not staged:
            return
        await self.store.add_message(umo, "friend", text, kind)
        self.broadcast({"type": "friend_message", "umo": umo, "text": text})
        self.broadcast({"type": "thinking", "umo": umo})
        self.schedule_options(umo)

    async def generate_options(self, umo: str) -> None:
        """Ask the LLM for the lines the user could send next plus a portrait tag.

        Args:
            umo: Unified message origin of the private session.
        """
        binding = await self.store.get_binding(umo)
        if binding is None:
            return
        provider = await self.context.get_using_provider_async(umo)
        if provider is None:
            self.broadcast(
                {
                    "type": "error",
                    "umo": umo,
                    "message": "没有可用的对话模型，请先在 AstrBot 中配置 LLM Provider。",
                }
            )
            return
        character = await self.store.get_character(binding["character_id"])
        tags = self.character_tags(character)
        count = max(1, min(6, int(self.config.get("option_count", 3))))
        limit = max(2, int(self.config.get("history_limit", 100)))
        history = await self.store.history(umo, limit)
        transcript = "\n".join(
            f"{binding['friend_name'] if row['role'] == 'friend' else '我'}：{row['text']}"
            for row in history
        )
        # Who the user is, and what these two are to each other. Without this the
        # model falls back to a generic helpful-assistant voice.
        self_persona = (await self.store.get_setting(SELF_PERSONA_KEY)).strip()
        relation = str(binding["relation"] or "").strip()
        identity_block = ""
        if self_persona:
            identity_block += f"【我（用户本人）的人设】\n{self_persona}\n"
        if relation:
            identity_block += (
                f"【我和「{binding['friend_name']}」的关系】\n{relation}\n"
            )
        if identity_block:
            identity_block += "\n"
        tag_rule = ""
        if tags:
            tag_rule = (
                "4. 另外，请**只看上面最后一条来自「"
                f"{binding['friend_name']}"
                "」的消息**，判断他发这条消息时的情绪（也就是他此刻的表情和心情），"
                "然后从下面这些情绪标签中**严格挑选一个**"
                "（必须原样照抄其中一个，不要自创）：\n"
                f"   {'、'.join(tags)}\n"
                "   注意：这是**他现在的状态**，不是猜测他对我还没发出的回复会有什么反应。\n"
            )
        tag_field = '"tag": "从上面标签里选一个"' if tags else '"tag": ""'
        prompt = (
            f"{identity_block}"
            f"你和「{binding['friend_name']}」正在 QQ 私聊。"
            f"当前好感度：{binding['affection']}/100（范围 0-100，越高越亲近）。\n"
            f"最近的对话（从上到下，越靠下越新）：\n{transcript}\n\n"
            f"请以「我」的身份（即上面人设里的那个人），给出 {count} 条可以直接发送给对方的回复候选。要求：\n"
            "1. 每条候选的**策略角度必须明显不同**，并在 angle 字段用 3-6 个字概括，"
            "例如：温柔关心 / 幽默调侃 / 直球表达 / 反客为主 / 认真回应 / 转移话题。\n"
            "2. 每条候选给出 affection 字段：如果发送这一条，对方的好感度会变化多少，"
            "必须是 **-3 到 +3 之间的整数**（0 表示基本没变化）。"
            "三条候选的数值应当有差异，不要都给同一个数。\n"
            "3. **语气必须符合我的人设和我们的关系**：要像真人随手打出来的字——"
            "可以有语气词、口语、省略、不完整句，甚至懒得打标点；"
            "句长贴近我们平时聊天的习惯，别写成小作文。\n"
            "   **严禁客服/助理腔**：不要出现「有什么可以帮您」「很高兴为您服务」"
            "「希望能帮到你」这类句式；不要总结或复述对方说的话；不要解释自己为什么这么说；"
            "不要用书面排比和分点。\n"
            f"{tag_rule}\n"
            "只输出 JSON，不要输出解释或代码块标记，格式：\n"
            f'{{"options": [{{"text": "候选1", "angle": "温柔关心", "affection": 2}}, '
            f'{{"text": "候选2", "angle": "幽默调侃", "affection": -1}}], '
            f"{tag_field}}}"
            "\n重要：台词字符串内部**不要出现英文双引号**（需要引号时用中文引号「」），"
            "否则 JSON 会解析失败。"
        )
        persona = str(self.config.get("persona_prompt", "")).strip() or DEFAULT_PERSONA

        # The model occasionally emits JSON with unescaped quotes inside a string.
        # Try to repair it first; if nothing usable comes back, retry once with a
        # stricter instruction instead of showing the user a raw JSON blob.
        items: list[dict] = []
        data: dict = {}
        raw = ""
        raw_tag = ""
        # Attach the friend's sticker to the first call so a vision-capable model
        # can actually look at it. If that call fails (model without vision,
        # unreadable file), it is retried as plain text.
        pending_images = self.recent_images.pop(umo, [])
        if pending_images and not bool(self.config.get("vision_for_images", True)):
            pending_images = []
        if pending_images:
            prompt = (
                "（对方最后那条消息附带了图片，已和本条消息一起提供，"
                "请结合图片内容理解他想表达什么。）\n" + prompt
            )
        # Attempt 1: normal JSON. Attempt 2: JSON again with a stricter note
        # (most failures are a one-off). Attempt 3: drop JSON entirely for a
        # delimiter format that has no escaping rules to get wrong.
        attempts = (
            (prompt, False),
            (prompt + JSON_RETRY_NOTE, False),
            (prompt + LINE_FORMAT_NOTE, True),
        )
        for index, (prompt_use, pipe_mode) in enumerate(attempts):
            try:
                response = await provider.text_chat(
                    prompt=prompt_use,
                    system_prompt=persona,
                    image_urls=pending_images or None,
                )
            except Exception as exc:
                if pending_images:
                    # The chat model cannot take the picture. Fall back to
                    # AstrBot's image-caption provider so a text-only model still
                    # knows what the friend sent, then retry as plain text.
                    self.logger.warning(
                        "Vision call failed for %s, falling back to caption: %s",
                        umo,
                        exc,
                    )
                    caption = await self.caption_images(pending_images)
                    pending_images = []
                    if caption:
                        prompt_use = prompt_use.replace("[图片]", f"[图片：{caption}]")
                    try:
                        response = await provider.text_chat(
                            prompt=prompt_use, system_prompt=persona
                        )
                    except Exception as retry_exc:
                        self.logger.exception(
                            "Candidate generation failed for %s.", umo
                        )
                        self.broadcast(
                            {
                                "type": "error",
                                "umo": umo,
                                "message": f"生成候选失败：{retry_exc}",
                            }
                        )
                        return
                else:
                    self.logger.exception("Candidate generation failed for %s.", umo)
                    self.broadcast(
                        {"type": "error", "umo": umo, "message": f"生成候选失败：{exc}"}
                    )
                    return
            raw = (response.completion_text or "").strip()
            if pipe_mode:
                items, raw_tag = _pipe_items(raw)
            else:
                data = _load_json_lenient(raw)
                items = _extract_items(data) or _regex_items(raw)
                raw_tag = str(data.get("tag") or "")
            if items:
                if index:
                    self.logger.info(
                        "Recovered candidates for %s on attempt %d.", umo, index + 1
                    )
                break
            self.logger.warning(
                "Unparsable model output for %s (attempt %d): %.300s",
                umo,
                index + 1,
                raw,
            )
        if not items:
            self.broadcast(
                {
                    "type": "error",
                    "umo": umo,
                    "message": "模型连续三次返回的格式都无法解析，请点「换一批候选」重试，或换一个模型。",
                }
            )
            return
        items = items[:count]
        batch = int(time() * 1000)
        option_ids = await self.store.add_options(umo, batch, items)

        tag = raw_tag if raw_tag in tags else str(binding["tag"] or "")
        await self.store.update_tag(umo, tag)
        sprite = self.resolve_sprite(character, tag)
        self.broadcast(
            {
                "type": "options",
                "umo": umo,
                "batch": batch,
                "options": [
                    {
                        "id": option_ids[index],
                        "text": item["text"],
                        "angle": item["angle"],
                        "affection": item["affection"],
                        "used": False,
                    }
                    for index, item in enumerate(items)
                ],
                "tag": tag,
                "sprite": sprite,
                "affection": binding["affection"],
            }
        )

    # ------------------------------------------------------------- libraries

    async def api_library(self):
        """Return both shared libraries plus the uploaded file list."""
        return json_response(
            {
                "characters": await self.store.list_characters(),
                "backgrounds": await self.store.list_backgrounds(),
                "assets": self.list_assets(),
            }
        )

    async def api_character_create(self):
        """Create an empty character in the shared library."""
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("missing name", status_code=400)
        character_id = await self.store.create_character(name)
        self.broadcast({"type": "library_changed"})
        return json_response({"id": character_id, "name": name})

    async def api_character_rename(self):
        """Rename a character."""
        payload = await request.json(default={})
        character_id = payload.get("id")
        name = str(payload.get("name") or "").strip()
        if not isinstance(character_id, int) or not name:
            return error_response("missing id or name", status_code=400)
        await self.store.rename_character(character_id, name)
        self.broadcast({"type": "library_changed"})
        return json_response({"renamed": True})

    async def api_character_delete(self):
        """Delete a character, its sprites, and unlink bound friends."""
        payload = await request.json(default={})
        character_id = payload.get("id")
        if not isinstance(character_id, int):
            return error_response("missing id", status_code=400)
        await self.store.delete_character(character_id)
        self.broadcast({"type": "library_changed"})
        self.broadcast({"type": "bindings_changed"})
        return json_response({"deleted": True})

    async def api_sprite_add(self):
        """Attach an uploaded image to a character with emotion tags."""
        payload = await request.json(default={})
        character_id = payload.get("character_id")
        file = str(payload.get("file") or "").strip()
        tags = payload.get("tags")
        if not isinstance(character_id, int) or not file:
            return error_response("missing character_id or file", status_code=400)
        if file not in self.list_assets():
            return error_response("unknown asset file", status_code=400)
        tag_list = [str(item) for item in tags] if isinstance(tags, list) else []
        sprite_id = await self.store.add_sprite(character_id, file, tag_list)
        self.broadcast({"type": "library_changed"})
        return json_response({"id": sprite_id})

    async def api_sprite_update(self):
        """Edit a sprite's tags or mark it as the character default."""
        payload = await request.json(default={})
        sprite_id = payload.get("id")
        if not isinstance(sprite_id, int):
            return error_response("missing id", status_code=400)
        tags = payload.get("tags")
        tag_list = [str(item) for item in tags] if isinstance(tags, list) else None
        is_default = payload.get("is_default")
        await self.store.update_sprite(
            sprite_id,
            tags=tag_list,
            is_default=bool(is_default) if is_default is not None else None,
        )
        self.broadcast({"type": "library_changed"})
        return json_response({"updated": True})

    async def api_sprite_delete(self):
        """Delete one sprite."""
        payload = await request.json(default={})
        sprite_id = payload.get("id")
        if not isinstance(sprite_id, int):
            return error_response("missing id", status_code=400)
        await self.store.delete_sprite(sprite_id)
        self.broadcast({"type": "library_changed"})
        return json_response({"deleted": True})

    async def api_background_add(self):
        """Add an uploaded image to the background library."""
        payload = await request.json(default={})
        file = str(payload.get("file") or "").strip()
        name = str(payload.get("name") or "").strip() or Path(file).stem
        if not file:
            return error_response("missing file", status_code=400)
        if file not in self.list_assets():
            return error_response("unknown asset file", status_code=400)
        background_id = await self.store.add_background(name, file)
        self.broadcast({"type": "library_changed"})
        return json_response({"id": background_id, "name": name})

    async def api_background_delete(self):
        """Delete a background from the library."""
        payload = await request.json(default={})
        background_id = payload.get("id")
        if not isinstance(background_id, int):
            return error_response("missing id", status_code=400)
        await self.store.delete_background(background_id)
        self.broadcast({"type": "library_changed"})
        self.broadcast({"type": "bindings_changed"})
        return json_response({"deleted": True})

    # -------------------------------------------------------------- bindings

    async def api_friends(self):
        """Return bound friends and sessions still waiting to be bound."""
        bindings = await self.store.list_bindings()
        bound = {row["umo"] for row in bindings}
        sessions = await self.store.list_sessions(50)
        characters = await self.store.list_characters()
        by_id = {item["id"]: item["name"] for item in characters}
        backgrounds = await self.store.list_backgrounds()
        background_names = {item["id"]: item["name"] for item in backgrounds}
        return json_response(
            {
                "bindings": [
                    {
                        **dict(row),
                        "character_name": by_id.get(row["character_id"], ""),
                        "background_name": background_names.get(
                            row["background_id"], ""
                        ),
                    }
                    for row in bindings
                ],
                "sessions": [dict(row) for row in sessions if row["umo"] not in bound],
                "characters": characters,
                "backgrounds": backgrounds,
                "assets": self.list_assets(),
            }
        )

    async def api_bind(self):
        """Bind a session to a character and a background."""
        payload = await request.json(default={})
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            return error_response("missing umo", status_code=400)
        character_id = payload.get("character_id")
        background_id = payload.get("background_id")
        await self.store.upsert_binding(
            umo,
            str(payload.get("friend_name") or "").strip() or umo,
            character_id if isinstance(character_id, int) else 0,
            background_id if isinstance(background_id, int) else 0,
        )
        self.broadcast({"type": "bindings_changed"})
        return json_response({"bound": True, "umo": umo})

    async def api_unbind(self):
        """Remove a binding while keeping the scene log."""
        payload = await request.json(default={})
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            return error_response("missing umo", status_code=400)
        await self.store.delete_binding(umo)
        self.broadcast({"type": "bindings_changed"})
        return json_response({"unbound": True, "umo": umo})

    async def api_relation(self):
        """Store how the user relates to one friend."""
        payload = await request.json(default={})
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            return error_response("missing umo", status_code=400)
        if await self.store.get_binding(umo) is None:
            return error_response("该会话还没有绑定角色", status_code=404)
        await self.store.update_relation(umo, str(payload.get("relation") or ""))
        self.broadcast({"type": "bindings_changed"})
        return json_response({"saved": True})

    async def api_settings(self):
        """Return the global persona settings."""
        return json_response(
            {
                "self_persona": await self.store.get_setting(SELF_PERSONA_KEY),
            }
        )

    async def api_settings_save(self):
        """Save the global persona settings."""
        payload = await request.json(default={})
        if "self_persona" in payload:
            await self.store.set_setting(
                SELF_PERSONA_KEY, str(payload.get("self_persona") or "")
            )
        self.broadcast({"type": "settings_changed"})
        return json_response({"saved": True})

    async def api_scene(self):
        """Return one scene: binding, libraries, log, and candidates."""
        umo = str(request.query.get("umo") or "").strip()
        if not umo:
            return error_response("missing umo", status_code=400)
        limit = request.query.get("limit", 60, type=int)
        binding = await self.store.get_binding(umo)
        messages = await self.store.history(umo, max(1, limit))
        character = None
        background = None
        if binding is not None:
            character = await self.store.get_character(binding["character_id"])
            background = await self.store.get_background(binding["background_id"])
        options = await self.store.latest_options(umo) if binding else []
        return json_response(
            {
                "binding": dict(binding) if binding else None,
                "character": character,
                "background": background,
                "tag": str(binding["tag"]) if binding else "",
                "sprite": self.resolve_sprite(
                    character, str(binding["tag"]) if binding else ""
                ),
                "messages": [dict(row) for row in messages],
                "options": [dict(row) for row in options],
                "assets": self.list_assets(),
            }
        )

    async def api_regenerate(self):
        """Regenerate reply candidates for a bound session."""
        payload = await request.json(default={})
        umo = str(payload.get("umo") or "").strip()
        if not umo:
            return error_response("missing umo", status_code=400)
        if await self.store.get_binding(umo) is None:
            return error_response("该会话还没有绑定角色", status_code=404)
        self.broadcast({"type": "thinking", "umo": umo})
        # Manual action: generate right away, no debounce.
        self.schedule_options(umo, delay=0)
        return json_response({"started": True})

    async def api_send(self):
        """Send a chosen or hand-written line to the bound friend."""
        payload = await request.json(default={})
        umo = str(payload.get("umo") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not umo or not text:
            return error_response("missing umo or text", status_code=400)
        if len(text) > 2000:
            return error_response("text too long", status_code=400)
        if await self.store.get_binding(umo) is None:
            return error_response("该会话还没有绑定角色", status_code=404)
        try:
            sent = await self.context.send_message(
                session=umo,
                message_chain=MessageChain([Plain(text)]),
            )
        except ValueError as exc:
            return error_response(f"会话标识不合法：{exc}", status_code=400)
        except Exception as exc:
            self.logger.exception("Failed to send to %s.", umo)
            return error_response(f"发送失败：{exc}", status_code=502)
        if not sent:
            return error_response("没有匹配的平台，消息未发送", status_code=502)
        await self.store.add_message(umo, "me", text, "text")
        option_id = payload.get("option_id")
        if isinstance(option_id, int) and not isinstance(option_id, bool):
            await self.store.mark_option_used(umo, option_id)
        self.broadcast({"type": "my_message", "umo": umo, "text": text})

        # Only a chosen candidate moves the affection meter; a hand-written line
        # leaves it untouched, exactly as requested.
        if isinstance(option_id, int) and not isinstance(option_id, bool):
            option = await self.store.get_option(umo, option_id)
            binding = await self.store.get_binding(umo)
            if option is not None and binding is not None:
                delta = int(option["affection"] or 0)
                if delta:
                    value = max(
                        AFFECTION_MIN,
                        min(AFFECTION_MAX, binding["affection"] + delta),
                    )
                    await self.store.update_affection(umo, value)
                    self.broadcast(
                        {
                            "type": "affection",
                            "umo": umo,
                            "delta": delta,
                            "value": value,
                            "angle": str(option["angle"] or ""),
                        }
                    )
        return json_response({"sent": True})

    async def api_log(self):
        """Return the full chat history for the floating log window."""
        umo = str(request.query.get("umo") or "").strip()
        if not umo:
            return error_response("missing umo", status_code=400)
        limit = request.query.get("limit", 400, type=int)
        rows = await self.store.history(umo, max(1, min(2000, limit)))
        return json_response({"messages": [dict(row) for row in rows]})

    async def api_assets(self):
        """List files available to the libraries."""
        return json_response({"assets": self.list_assets()})

    async def api_load_defaults(self):
        """Re-register every bundled example asset missing from the libraries."""
        added = await self.import_default_assets()
        return json_response(
            {
                "added": added,
                "characters": await self.store.list_characters(),
                "backgrounds": await self.store.list_backgrounds(),
                "assets": self.list_assets(),
            }
        )

    async def api_image(self):
        """Return one asset as a data URL, optionally downscaled.

        Query:
            name: Bare file name from the library.
            thumb: ``1`` for a list thumbnail instead of the display size.
        """
        name = str(request.query.get("name") or "").strip()
        thumbnail = str(request.query.get("thumb") or "").strip() in {"1", "true"}
        path = self.find_asset(name)
        if path is None:
            return error_response("unknown asset file", status_code=404)
        # Decoding a multi-megabyte picture would otherwise stall every other
        # task, including the message pipeline.
        data_url = await asyncio.to_thread(self.encode_asset, path, thumbnail)
        if data_url is None:
            return error_response("该图片无法解析，请换一张", status_code=500)
        return json_response({"data_url": data_url})

    async def api_upload(self):
        """Store an uploaded image next to the plugin's other persistent data."""
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("missing file", status_code=400)
        filename = Path(upload.filename or "").name
        suffix = Path(filename).suffix.lower()
        if not filename or suffix not in ALLOWED_ASSET_SUFFIXES:
            return error_response(
                "只支持 png / jpg / jpeg / webp / gif 图片", status_code=400
            )
        # AI-generated file names can be longer than the Windows path limit, and
        # they are unreadable in the UI anyway, so keep only a short stem.
        stem = Path(filename).stem[:40] or "asset"
        if stem.startswith("default_"):
            # ``default_*`` marks art that ships inside the plugin and is served
            # as a plain URL by the page. Never hand that prefix to an upload.
            stem = f"upload_{stem}"
        DATA_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        target = DATA_ASSETS_DIR / f"{stem}{suffix}"
        if target.exists():
            target = DATA_ASSETS_DIR / f"{stem}_{int(time())}{suffix}"
        await upload.save(target)
        return json_response({"filename": target.name, "assets": self.list_assets()})

    async def api_events(self):
        """Stream scene events to the page until the client disconnects."""
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(queue)

        async def stream():
            try:
                yield 'data: {"type": "connected"}\n\n'
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if event is None:
                        break
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            finally:
                self.subscribers.discard(queue)

        return stream_response(stream())
