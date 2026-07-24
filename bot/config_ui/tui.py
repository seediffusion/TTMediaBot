"""Terminal (line-based) configuration wizard for Cider.

Deliberately a simple, sequential *print question -> read ``input()``* flow --
no curses, no full-screen redraw, no ANSI cursor tricks.  That makes it robust
over a plain SSH session and reliably announced by screen readers, which is the
priority for Cider's largely blind / visually-impaired user base.

Only the Python standard library is used here, so it always works on a bare
server.  All the real work (validation, config assembly, writing) is delegated
to :mod:`bot.config_ui.wizard`.
"""

from __future__ import annotations

import os
from typing import List, Optional

from pydantic import ValidationError

from bot.config.models import TeamTalkModel
from bot.config_ui import wizard
from bot.config_ui.wizard import (
    SERVER_FIELDS,
    SERVICES,
    Field,
    WizardState,
    plain_label,
)


# ---------------------------------------------------------------------------
# Small, screen-reader friendly prompt helpers.
# ---------------------------------------------------------------------------
def _print_heading(text: str) -> None:
    print()
    print(f"== {text} ==")


def ask_text(label: str, default: str = "", help_text: str = "", secret: bool = False) -> str:
    if help_text:
        print(help_text)
    shown_default = "" if secret and default else default
    suffix = f" [{shown_default}]" if shown_default != "" else " [blank]" if default == "" else ""
    while True:
        answer = input(f"{label}{suffix}: ").strip()
        if answer == "":
            return default
        return answer


def ask_int(label: str, default: int, help_text: str = "") -> int:
    if help_text:
        print(help_text)
    while True:
        answer = input(f"{label} [{default}]: ").strip()
        if answer == "":
            return default
        try:
            return int(answer)
        except ValueError:
            print("Please enter a whole number.")


def ask_bool(label: str, default: bool, help_text: str = "") -> bool:
    if help_text:
        print(help_text)
    default_hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{label}? [{default_hint}]: ").strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer yes or no.")


def ask_choice(label: str, options: List[str], default_index: int = 0, help_text: str = "") -> int:
    """Print a numbered menu and return the chosen zero-based index."""
    if help_text:
        print(help_text)
    print(f"{label}:")
    for i, option in enumerate(options):
        marker = " (default)" if i == default_index else ""
        print(f"  {i + 1}. {option}{marker}")
    while True:
        answer = input(f"Enter a number 1-{len(options)} [{default_index + 1}]: ").strip()
        if answer == "":
            return default_index
        try:
            index = int(answer) - 1
        except ValueError:
            print("Please enter a number from the list.")
            continue
        if 0 <= index < len(options):
            return index
        print("That number is not in the list.")


def _ask_field(fld: Field, current: object) -> object:
    # Labels carry wx mnemonics for the GUI; strip them before printing.
    label = plain_label(fld.label)
    if fld.kind == "int":
        return ask_int(label, int(current), fld.help)
    if fld.kind == "bool":
        return ask_bool(label, bool(current), fld.help)
    if fld.kind == "list":
        default_str = ", ".join(current) if isinstance(current, list) else str(current)
        raw = ask_text(label, default_str, fld.help)
        return wizard.parse_admins(raw)
    if fld.kind == "args":
        default_str = wizard.format_args(current) if isinstance(current, list) else str(current)
        raw = ask_text(label, default_str, fld.help)
        return wizard.parse_args(raw)
    if fld.kind == "secret":
        return ask_text(label, str(current), fld.help, secret=True)
    return ask_text(label, str(current), fld.help)


# ---------------------------------------------------------------------------
# Wizard sections.
# ---------------------------------------------------------------------------
def _configure_server(base: Optional[TeamTalkModel]) -> TeamTalkModel:
    """Prompt for one server's fields until it validates."""
    values = wizard.server_to_values(base) if base else {
        f.key: f.default for f in SERVER_FIELDS
    }
    while True:
        collected = {}
        for fld in SERVER_FIELDS:
            collected[fld.key] = _ask_field(fld, values.get(fld.key, fld.default))
        try:
            return wizard.build_server(collected, base=base)
        except ValidationError as exc:
            print("\nThat server configuration is not valid:")
            for err in exc.errors():
                loc = ".".join(str(part) for part in err["loc"])
                print(f"  - {loc}: {err['msg']}")
            print("Let's try again.\n")
            values = collected


def _configure_servers(state: WizardState) -> None:
    _print_heading("TeamTalk servers")
    print("You must configure at least one server. You can add more afterwards.")
    servers: List[TeamTalkModel] = []
    existing = list(state.servers)
    # Offer to reuse / edit any existing servers first.
    for index, server in enumerate(existing, start=1):
        print(f"\nExisting server {index}: {wizard.server_summary(server)}")
        if ask_bool("Keep this server", True):
            if ask_bool("Edit its settings", False):
                servers.append(_configure_server(server))
            else:
                servers.append(server)
    if not servers:
        print("\nLet's set up your first server.")
        servers.append(_configure_server(None))
    while ask_bool("\nAdd another server", False):
        servers.append(_configure_server(None))
    state.servers = servers


def _configure_general(state: WizardState) -> None:
    _print_heading("General")
    languages = wizard.get_available_languages()
    try:
        default_index = languages.index(state.language)
    except ValueError:
        default_index = 0
    choice = ask_choice(
        "Interface language",
        languages,
        default_index,
        "The language Cider uses for its messages.",
    )
    state.language = languages[choice]


def _configure_sound(state: WizardState) -> None:
    _print_heading("Sound devices")
    print(
        "Cider plays audio to an output device and TeamTalk transmits from an "
        "input device (usually a virtual cable that links the two)."
    )

    out_names, out_err = wizard.safe_enumerate_output_devices()
    if out_names:
        default = state.output_device_index if 0 <= state.output_device_index < len(out_names) else 0
        state.output_device_index = ask_choice(
            "Player output device", out_names, default
        )
    else:
        print(f"\nCould not list output devices ({out_err}).")
        print("You can find indices later with:  python Cider.py --devices")
        state.output_device_index = ask_int(
            "Output device index", state.output_device_index
        )

    in_names, in_err = wizard.safe_enumerate_input_devices()
    if in_names:
        default = state.input_device_index if 0 <= state.input_device_index < len(in_names) else 0
        state.input_device_index = ask_choice(
            "TeamTalk input device", in_names, default
        )
    else:
        print(f"\nCould not list input devices ({in_err}).")
        print("You can find indices later with:  python Cider.py --devices")
        state.input_device_index = ask_int(
            "Input device index", state.input_device_index
        )


def _configure_services(state: WizardState) -> None:
    _print_heading("Music services")
    print("Enable the services you want the bot to be able to play from.")
    for spec in SERVICES:
        enabled = ask_bool(
            f"Enable {spec.label}", state.service_enabled.get(spec.key, True)
        )
        state.service_enabled[spec.key] = enabled
        if enabled and spec.fields:
            values = state.service_config.setdefault(spec.key, {})
            for fld in spec.fields:
                values[fld.key] = _ask_field(fld, values.get(fld.key, fld.default))

    enabled_services = [s.key for s in SERVICES if state.service_enabled.get(s.key)]
    if enabled_services:
        try:
            default_index = enabled_services.index(state.default_service)
        except ValueError:
            default_index = 0
        labels = {s.key: s.label for s in SERVICES}
        choice = ask_choice(
            "Service used for searches",
            [labels[k] for k in enabled_services],
            default_index,
            "Which service the play command searches at startup. URLs are always "
            "matched to a service by their address, so this does not affect them. "
            "Users can switch service at any time with the sv command.",
        )
        state.default_service = enabled_services[choice]


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------
def run(config_path: str) -> int:
    print("Cider configuration wizard")
    print("Press Enter to accept the value shown in [brackets].")

    state = WizardState()
    if os.path.isfile(config_path):
        print(f"\nA configuration file already exists at:\n  {config_path}")
        if ask_bool("Load its current values as the starting point", True):
            try:
                state = WizardState.from_config(wizard.load_config(config_path))
            except (ValidationError, ValueError, OSError) as exc:
                print(f"Could not read the existing file ({exc}); starting fresh.")

    _configure_general(state)
    _configure_servers(state)
    _configure_sound(state)
    _configure_services(state)

    # Build & validate before writing anything.
    try:
        config = wizard.build_config(state)
    except ValidationError as exc:
        print("\nThe configuration could not be validated:")
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            print(f"  - {loc}: {err['msg']}")
        print("Nothing was written.")
        return 1

    _print_heading("Save")
    print(f"Configuration will be written to:\n  {config_path}")
    if os.path.isfile(config_path):
        if not ask_bool("This file already exists. Overwrite it", False):
            print("Cancelled; nothing was written.")
            return 1
    try:
        wizard.write_config(config, config_path)
    except OSError as exc:
        print(f"Failed to write the file: {exc}")
        return 1

    print(f"\nDone. Saved {len(state.servers)} server(s) to {config_path}.")
    print("You can start the bot now.")
    return 0
