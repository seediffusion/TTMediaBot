"""Shared, UI-agnostic backend for the Cider configuration wizard.

This module is the single source of truth for the setup flow.  Both the
wxPython GUI (``wx_ui``) and the terminal wizard (``tui``) drive the exact
same logic that lives here:

* it describes *what* the wizard asks (the field metadata below),
* it enumerates the real sound devices (reusing the same primitives the bot
  uses at runtime), and
* it builds, validates and writes a :class:`~bot.config.models.ConfigModel`.

The pydantic ``ConfigModel`` is the source of truth for the schema, so this
module never hand-rolls validation -- it constructs the model and lets pydantic
reject invalid input with clear messages.

Only the stdlib and pydantic (already a dependency) are imported at module
level.  The heavy, platform specific bits -- ``mpv`` for output devices,
``TeamTalkPy`` for input devices -- are imported lazily inside the enumeration
helpers and guarded, so importing this module on a bare Linux server (or during
a syntax check) never requires them.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from bot import app_vars
from bot.config.models import (
    ConfigModel,
    TeamTalkModel,
    VkModel,
    YamModel,
    YtModel,
)

# Default output path, matching Cider.py's ``--config`` default.
DEFAULT_CONFIG_PATH = os.path.join(app_vars.directory, "config.json")


# ---------------------------------------------------------------------------
# Declarative description of the questions the wizard asks.
#
# Both front ends iterate these descriptors so the labels, defaults and help
# text are defined exactly once.
# ---------------------------------------------------------------------------
def plain_label(label: str) -> str:
    """Strip mnemonic markers and a trailing colon from a label.

    Labels carry wx mnemonics (``&``) so the GUI and the terminal wizard can
    share one set of strings.  ``&&`` is wx's escape for a literal ampersand and
    collapses to one; a lone ``&`` is the marker and is dropped.  The terminal
    wizard runs every label through this before printing, and the GUI uses it for
    accessible names.
    """
    out = []
    i = 0
    while i < len(label):
        if label[i] == "&":
            # "&&" is a literal ampersand; a lone "&" marks the mnemonic.
            if i + 1 < len(label) and label[i + 1] == "&":
                out.append("&")
                i += 2
                continue
            i += 1
            continue
        out.append(label[i])
        i += 1
    return "".join(out).rstrip(":").strip()


@dataclass
class Field:
    """A single question shown by both front ends.

    ``label`` may contain a wx mnemonic marker (``&``); the terminal wizard
    strips it with :func:`plain_label`.
    """

    key: str
    label: str
    kind: str  # "text" | "secret" | "int" | "bool" | "list" | "args"
    default: Any
    help: str = ""


# Pull the defaults straight from the pydantic model so they never drift.
_TT_DEFAULTS = TeamTalkModel()

SERVER_FIELDS: List[Field] = [
    Field("hostname", "Server &address", "text", _TT_DEFAULTS.hostname,
          "The hostname or IP address of the TeamTalk server, "
          "e.g. tt.example.com."),
    Field("tcp_port", "&TCP port", "int", _TT_DEFAULTS.tcp_port,
          "Usually 10333."),
    Field("udp_port", "&UDP port", "int", _TT_DEFAULTS.udp_port,
          "Usually the same as the TCP port."),
    Field("encrypted", "Use an &encrypted connection", "bool", _TT_DEFAULTS.encrypted,
          "Enable only if the server requires encryption."),
    Field("nickname", "Bot &nickname", "text", _TT_DEFAULTS.nickname,
          "The display name the bot shows in the channel."),
    Field("username", "Lo&gin username", "text", _TT_DEFAULTS.username,
          "The account the bot logs in with. Leave blank for a guest login."),
    Field("password", "Login &password", "secret", _TT_DEFAULTS.password,
          "The password for the login account. Leave blank if none."),
    Field("channel", "Channel to &join (path or ID)", "text", str(_TT_DEFAULTS.channel),
          'A channel path such as "/" (root) or "/Music", or a numeric channel '
          "ID. If the path does not exist the bot joins the root channel."),
    Field("channel_password", "Channel pass&word", "secret", _TT_DEFAULTS.channel_password,
          "Leave blank if the channel has no password."),
    Field("admins", "Ad&min usernames (comma separated)", "list",
          list(_TT_DEFAULTS.users.admins),
          "Comma separated TeamTalk account usernames (not nicknames) allowed to "
          "run admin commands. TeamTalk server administrators always have "
          "access, whether or not they are listed here."),
]


@dataclass
class ServiceSpec:
    key: str
    label: str
    fields: List[Field] = field(default_factory=list)


_VK_DEFAULTS = VkModel()
_YAM_DEFAULTS = YamModel()
_YT_DEFAULTS = YtModel()

# ``ServiceSpec.label`` must stay free of mnemonic markers: it is also used
# verbatim as the items of the "Default service" wx.Choice, which would render a
# literal "&".  The GUI adds mnemonics to the per-service "Enable" checkboxes.
SERVICES: List[ServiceSpec] = [
    ServiceSpec("vk", "VK (VKontakte)", [
        Field("token", "V&K API token", "secret", _VK_DEFAULTS.token,
              "Paste your VK API token. Leave blank to configure it later."),
    ]),
    ServiceSpec("yam", "Yandex Music", [
        Field("token", "Yandex &Music token", "secret", _YAM_DEFAULTS.token,
              "Paste your Yandex Music token. Leave blank to configure it later."),
    ]),
    ServiceSpec("yt", "YouTube", [
        Field("yt_dlp_path", "Path to the yt-&dlp program", "text", _YT_DEFAULTS.yt_dlp_path,
              'Path to the yt-dlp binary, e.g. "/home/user/cider/yt-dlp", or just '
              '"yt-dlp" if it is on the system PATH.'),
        Field("cookiefile_path", "Path to a c&ookies.txt file", "text",
              _YT_DEFAULTS.cookiefile_path,
              "Optional. A YouTube cookies file helps with age or bot gated videos. "
              "Leave blank for none. If the file does not exist it is ignored "
              "without warning."),
        Field("extra_args", "Extra yt-dlp ar&guments", "args",
              list(_YT_DEFAULTS.extra_args),
              "Advanced, optional. Added to every yt-dlp call, including "
              "downloads. Quote any value containing spaces, "
              "e.g.  --js-runtimes deno:/home/user/.deno/bin/deno"),
    ]),
]


def get_available_languages() -> List[str]:
    """Return the language codes Cider ships translations for.

    Reuses the same discovery the runtime :class:`~bot.translator.Translator`
    uses (``["en"] + os.listdir(locale)``) but without loading gettext.
    """
    locale_dir = os.path.join(app_vars.directory, "locale")
    languages = ["en"]
    try:
        languages += sorted(
            name
            for name in os.listdir(locale_dir)
            if os.path.isdir(os.path.join(locale_dir, name))
        )
    except OSError:
        pass
    return languages


# ---------------------------------------------------------------------------
# Sound device enumeration.
#
# The config stores ``sound_devices.output_device`` / ``input_device`` as an
# *index* into the device lists the bot builds at runtime.  We enumerate the
# devices here in the exact same order the runtime does so the chosen index is
# consistent, without needing a live server connection.
# ---------------------------------------------------------------------------
def enumerate_output_devices() -> List[str]:
    """Return player (mpv) output device names, index == config value.

    Mirrors :meth:`bot.player.Player.get_output_devices` (which iterates
    ``mpv.MPV().audio_device_list``).
    """
    import mpv  # lazy: requires libmpv, only present in a real install

    player = mpv.MPV(video=False, ytdl=False)
    try:
        names: List[str] = []
        for device in player.audio_device_list:
            names.append(device.get("description") or device.get("name") or "")
        return names
    finally:
        player.terminate()


def enumerate_input_devices() -> List[str]:
    """Return TeamTalk input device names, index == config value.

    Mirrors :meth:`bot.TeamTalk.TeamTalk.get_input_devices` (which filters
    ``TeamTalkPy.TeamTalk().getSoundDevices()``).  Importing ``bot.TeamTalk``
    performs the Windows DLL directory setup for us.
    """
    import bot.TeamTalk as tt_module  # lazy: requires TeamTalkPy / native libs

    TeamTalkPy = tt_module.TeamTalkPy
    tt = TeamTalkPy.TeamTalk()
    names: List[str] = []
    for device in tt.getSoundDevices():
        if sys.platform == "win32":
            if (
                device.nSoundSystem == TeamTalkPy.SoundSystem.SOUNDSYSTEM_WASAPI
                and device.nMaxOutputChannels == 0
            ):
                names.append(tt_module._str(device.szDeviceName))
        else:
            names.append(tt_module._str(device.szDeviceName))
    return names


def safe_enumerate_output_devices() -> Tuple[List[str], Optional[str]]:
    """Enumerate output devices, never raising.

    Returns ``(names, error)`` where ``error`` is a human readable message when
    enumeration failed (e.g. on a headless box without audio libraries).
    """
    try:
        return enumerate_output_devices(), None
    except Exception as exc:  # noqa: BLE001 - report anything to the user
        return [], f"{type(exc).__name__}: {exc}"


def safe_enumerate_input_devices() -> Tuple[List[str], Optional[str]]:
    """Enumerate input devices, never raising. See ``safe_enumerate_output_devices``."""
    try:
        return enumerate_input_devices(), None
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Collected wizard answers and config assembly.
# ---------------------------------------------------------------------------
@dataclass
class WizardState:
    """Everything the front ends collect, assembled into a config by ``build_config``."""

    language: str = "en"
    servers: List[TeamTalkModel] = field(default_factory=list)
    output_device_index: int = 0
    input_device_index: int = 0
    service_enabled: Dict[str, bool] = field(
        default_factory=lambda: {s.key: True for s in SERVICES}
    )
    service_config: Dict[str, Dict[str, Any]] = field(
        default_factory=lambda: {
            s.key: {f.key: f.default for f in s.fields} for s in SERVICES
        }
    )
    default_service: str = "vk"
    # When editing an existing file, keep every field the wizard does not touch.
    base_config: Optional[ConfigModel] = None

    @classmethod
    def from_config(cls, config: ConfigModel) -> "WizardState":
        """Pre-fill a state from an existing config so it can be used as defaults."""
        return cls(
            language=config.general.language,
            servers=[s.model_copy(deep=True) for s in config.teamtalk],
            output_device_index=config.sound_devices.output_device,
            input_device_index=config.sound_devices.input_device,
            service_enabled={
                "vk": config.services.vk.enabled,
                "yam": config.services.yam.enabled,
                "yt": config.services.yt.enabled,
            },
            service_config={
                spec.key: {
                    fld.key: getattr(getattr(config.services, spec.key), fld.key)
                    for fld in spec.fields
                }
                for spec in SERVICES
            },
            default_service=config.services.default_service,
            base_config=config,
        )


def build_server(values: Dict[str, Any], base: Optional[TeamTalkModel] = None) -> TeamTalkModel:
    """Build and validate a single ``TeamTalkModel`` from flat wizard answers.

    ``values`` keys match :data:`SERVER_FIELDS`.  Any field not covered by the
    wizard (status, gender, license, reconnection, banned users, event
    handling...) is preserved from ``base``.  pydantic performs the validation
    and type coercion, so an invalid port raises ``ValidationError``.
    """
    data = (base or TeamTalkModel()).model_dump()
    data["hostname"] = values["hostname"]
    data["tcp_port"] = values["tcp_port"]
    data["udp_port"] = values["udp_port"]
    data["encrypted"] = values["encrypted"]
    data["nickname"] = values["nickname"]
    data["username"] = values["username"]
    data["password"] = values["password"]
    data["channel"] = parse_channel(values["channel"])
    data["channel_password"] = values["channel_password"]
    data.setdefault("users", {})
    data["users"]["admins"] = list(values["admins"])
    return TeamTalkModel(**data)


def server_to_values(server: TeamTalkModel) -> Dict[str, Any]:
    """Inverse of :func:`build_server`: flat values for the editor UIs."""
    return {
        "hostname": server.hostname,
        "tcp_port": server.tcp_port,
        "udp_port": server.udp_port,
        "encrypted": server.encrypted,
        "nickname": server.nickname,
        "username": server.username,
        "password": server.password,
        "channel": str(server.channel),
        "channel_password": server.channel_password,
        "admins": list(server.users.admins),
    }


def server_summary(server: TeamTalkModel) -> str:
    """A short, screen-reader friendly one-line description of a server."""
    who = server.username or "guest"
    return f"{server.hostname}:{server.tcp_port} (nickname {server.nickname}, login {who})"


def build_config(state: WizardState) -> ConfigModel:
    """Assemble and validate a full ``ConfigModel`` from the collected answers.

    Raises ``pydantic.ValidationError`` on invalid input (e.g. no servers).
    """
    base = state.base_config or ConfigModel()
    data = base.model_dump()

    data["general"]["language"] = state.language
    data["sound_devices"]["output_device"] = state.output_device_index
    data["sound_devices"]["input_device"] = state.input_device_index
    data["teamtalk"] = [server.model_dump() for server in state.servers]

    services = data["services"]
    services["default_service"] = state.default_service
    for spec in SERVICES:
        service = services[spec.key]
        service["enabled"] = state.service_enabled.get(spec.key, True)
        values = state.service_config.get(spec.key, {})
        for fld in spec.fields:
            if fld.key in values:
                service[fld.key] = values[fld.key]

    return ConfigModel(**data)


def write_config(config: ConfigModel, path: str) -> None:
    """Serialize a validated config to ``path``.

    Uses the same serialization as ``ConfigManager._dump``
    (``indent=4``, ``ensure_ascii=False``, UTF-8) so the output is identical to
    what the bot itself writes.
    """
    with open(path, "w", encoding="UTF-8") as f:
        json.dump(config.model_dump(), f, indent=4, ensure_ascii=False)


def load_config(path: str) -> ConfigModel:
    """Load and validate an existing config file (for use as defaults)."""
    with open(path, "r", encoding="UTF-8") as f:
        data = json.load(f)
    return ConfigModel(**data)


def parse_channel(raw: Any) -> Union[int, str]:
    """Coerce a channel answer back to the ``Union[int, str]`` the model stores.

    ``TeamTalk.join`` treats an ``int`` as a channel ID and anything else as a
    path to look up (``bot/TeamTalk/__init__.py``).  The editors show the value
    as text, so a numeric ID would come back as ``"5"`` and be looked up as a
    path -- which fails, silently landing the bot in the root channel.  Channel
    paths always start with "/", so an all-digit answer is unambiguously an ID.
    """
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    return int(text) if text.isdigit() else text


def parse_admins(raw: str) -> List[str]:
    """Parse a comma separated admin list from a text field."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_args(raw: str) -> List[str]:
    """Parse extra command-line arguments (shell-like, space separated)."""
    return shlex.split(raw)


def format_args(args: List[str]) -> str:
    """Inverse of :func:`parse_args`, for displaying a list of args in a field."""
    return shlex.join(args)
