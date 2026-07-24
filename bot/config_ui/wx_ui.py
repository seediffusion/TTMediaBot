"""wxPython configuration wizard for Cider (Windows default front end).

Accessibility is the priority here: Cider's user base is largely blind /
visually-impaired.  Every input has an associated ``wx.StaticText`` label *and*
an explicit accessible name, controls are laid out in a logical tab order, every
action is reachable from the keyboard (mnemonics, default / escape buttons, list
+ button patterns instead of drag/drop), focus is placed sensibly on open and on
error, and validation problems are reported through announced message boxes.

``wx`` is only imported by this module, which itself is only imported when the
GUI front end has been selected, so headless installs never need wxPython.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional

import wx

from pydantic import ValidationError

from bot import app_vars
from bot.config.models import TeamTalkModel
from bot.config_ui import wizard
from bot.config_ui.wizard import (
    SERVER_FIELDS,
    SERVICES,
    Field,
    WizardState,
    plain_label,
)


# Mnemonics for the per-service "Enable" checkboxes.  They live here rather than
# in ``ServiceSpec.label`` because that label is also used verbatim as an item of
# the "Default service" choice, where a "&" would be rendered literally.
ENABLE_LABELS: Dict[str, str] = {
    "vk": "Ena&ble VK (VKontakte)",
    "yam": "Enable &Yandex Music",
    "yt": "Enable Yo&uTube",
}


def _format_errors(exc: ValidationError) -> str:
    lines = ["The configuration is not valid:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        lines.append(f"- {loc}: {err['msg']}")
    return "\n".join(lines)


def _add_labeled(parent: wx.Window, sizer: wx.Sizer, label: str,
                 make_ctrl: Callable[[wx.Window], wx.Window],
                 help_text: str = "") -> wx.Window:
    """Create a StaticText label, then the control it names, and add both.

    ``make_ctrl`` is a factory rather than a ready-made control, and that is the
    whole point: on MSW a control with no native label of its own (EDIT,
    COMBOBOX, LISTBOX) takes its accessible name from the nearest *preceding*
    Static in the real Win32 HWND z-order, and that z-order follows the order
    the windows were created in.  Building the control first -- as is the natural
    thing to write -- puts every label *after* the control it names, so each
    field ends up announced with the previous field's label and the first field
    has no name at all.  Creating the label first is what actually fixes it.

    Note that ``wxWindow.MoveAfterInTabOrder`` does *not* fix this: it reorders
    wx's own child list, which leaves the underlying HWND z-order (the thing
    MSAA reads) untouched.  ``SetName`` is likewise only a partial backstop, so
    neither is a substitute for creating the windows in the right order.
    """
    static = wx.StaticText(parent, label=label)
    sizer.Add(static, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
    ctrl = make_ctrl(parent)
    ctrl.SetName(plain_label(label))
    if help_text:
        ctrl.SetToolTip(help_text)
        ctrl.SetHelpText(help_text)
    sizer.Add(ctrl, 0, wx.EXPAND | wx.ALL, 6)
    return ctrl


# ---------------------------------------------------------------------------
# Single-server editor dialog.
# ---------------------------------------------------------------------------
class ServerDialog(wx.Dialog):
    def __init__(self, parent: wx.Window, server: Optional[TeamTalkModel]) -> None:
        title = "Edit TeamTalk server" if server else "Add TeamTalk server"
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._base = server
        self.result: Optional[TeamTalkModel] = None
        self._controls: Dict[str, wx.Window] = {}

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        values = (
            wizard.server_to_values(server)
            if server
            else {f.key: f.default for f in SERVER_FIELDS}
        )
        for fld in SERVER_FIELDS:
            self._controls[fld.key] = self._make_control(panel, sizer, fld, values[fld.key])

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(panel, wx.ID_OK, "&Save server")
        ok.SetDefault()
        cancel = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        buttons.AddButton(ok)
        buttons.AddButton(cancel)
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize((460, self.GetSize().height))
        self.SetEscapeId(wx.ID_CANCEL)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        first = self._controls[SERVER_FIELDS[0].key]
        first.SetFocus()

    def _make_control(self, panel: wx.Window, sizer: wx.Sizer, fld: Field, value) -> wx.Window:
        if fld.kind == "bool":
            ctrl = wx.CheckBox(panel, label=fld.label)
            ctrl.SetValue(bool(value))
            if fld.help:
                ctrl.SetToolTip(fld.help)
                ctrl.SetHelpText(fld.help)
            sizer.Add(ctrl, 0, wx.ALL, 6)
            return ctrl
        style = wx.TE_PASSWORD if fld.kind == "secret" else 0
        if fld.kind == "list" and isinstance(value, list):
            text = ", ".join(value)
        else:
            text = str(value)
        return _add_labeled(
            panel, sizer, f"{fld.label}:",
            lambda p: wx.TextCtrl(p, value=text, style=style),
            fld.help,
        )

    def _error(self, message: str, focus: Optional[wx.Window]) -> None:
        wx.MessageBox(message, "Invalid server settings", wx.OK | wx.ICON_ERROR, self)
        if focus is not None:
            focus.SetFocus()

    def _on_ok(self, event: wx.CommandEvent) -> None:
        collected: Dict[str, object] = {}
        for fld in SERVER_FIELDS:
            ctrl = self._controls[fld.key]
            if fld.kind == "bool":
                collected[fld.key] = ctrl.GetValue()
            elif fld.kind == "int":
                raw = ctrl.GetValue().strip()
                try:
                    collected[fld.key] = int(raw)
                except ValueError:
                    self._error(f"{plain_label(fld.label)} must be a whole number.", ctrl)
                    return
            elif fld.kind == "list":
                collected[fld.key] = wizard.parse_admins(ctrl.GetValue())
            else:
                collected[fld.key] = ctrl.GetValue()
        try:
            self.result = wizard.build_server(collected, base=self._base)
        except ValidationError as exc:
            self._error(_format_errors(exc), None)
            return
        self.EndModal(wx.ID_OK)


# ---------------------------------------------------------------------------
# Main wizard window.
# ---------------------------------------------------------------------------
class WizardFrame(wx.Frame):
    def __init__(self, config_path: str) -> None:
        super().__init__(
            None,
            title=f"{app_vars.app_name} configuration wizard",
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self.config_path = config_path
        self.exit_code = 1
        self._base_config = None
        self.servers: List[TeamTalkModel] = []

        # Offer to start from an existing config.
        if os.path.isfile(config_path):
            answer = wx.MessageBox(
                f"A configuration file already exists at:\n{config_path}\n\n"
                "Load its current values as the starting point?",
                "Existing configuration found",
                wx.YES_NO | wx.ICON_QUESTION,
                self,
            )
            if answer == wx.YES:
                try:
                    self._base_config = wizard.load_config(config_path)
                except (ValidationError, ValueError, OSError) as exc:
                    wx.MessageBox(
                        f"Could not read the existing file:\n{exc}\n\nStarting fresh.",
                        "Could not read the existing configuration",
                        wx.OK | wx.ICON_WARNING,
                        self,
                    )

        self._state = (
            WizardState.from_config(self._base_config)
            if self._base_config is not None
            else WizardState()
        )
        self.servers = list(self._state.servers)

        panel = wx.Panel(self)
        self.notebook = wx.Notebook(panel)
        # Freely navigable pages, not an ordered sequence: do not call these
        # "steps", which would imply a Next button that does not exist.
        self.notebook.SetName("Configuration sections")
        self.notebook.AddPage(self._build_general_page(), "General")
        self.notebook.AddPage(self._build_servers_page(), "Servers")
        self.notebook.AddPage(self._build_sound_page(), "Sound devices")
        self.notebook.AddPage(self._build_services_page(), "Services")

        save = wx.Button(panel, wx.ID_SAVE, "&Save configuration")
        save.SetDefault()
        cancel = wx.Button(panel, wx.ID_CANCEL, "&Cancel")
        button_row = wx.BoxSizer(wx.HORIZONTAL)
        button_row.AddStretchSpacer()
        button_row.Add(save, 0, wx.ALL, 6)
        button_row.Add(cancel, 0, wx.ALL, 6)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 6)
        outer.Add(button_row, 0, wx.EXPAND)
        panel.SetSizer(outer)

        self.Bind(wx.EVT_BUTTON, self._on_save, id=wx.ID_SAVE)
        self.Bind(wx.EVT_BUTTON, self._on_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, lambda e: self._finish(1))

        self.SetSize((560, 560))
        self.CentreOnScreen()
        self._refresh_server_list()
        self.lang_choice.SetFocus()

    # --- pages ----------------------------------------------------------
    def _build_general_page(self) -> wx.Window:
        page = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.languages = wizard.get_available_languages()
        self.lang_choice = _add_labeled(
            page, sizer, "&Interface language:",
            lambda p: wx.Choice(p, choices=self.languages),
            "The language Cider uses for its messages.",
        )
        try:
            self.lang_choice.SetSelection(self.languages.index(self._state.language))
        except ValueError:
            self.lang_choice.SetSelection(0)
        page.SetSizer(sizer)
        return page

    def _build_servers_page(self) -> wx.Window:
        page = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.server_list = _add_labeled(
            page, sizer, "Configured ser&vers:",
            lambda p: wx.ListBox(p, style=wx.LB_SINGLE),
            "TeamTalk servers the bot will connect to. At least one is required.",
        )
        self.server_list.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self._edit_server())
        # _add_labeled adds controls at a fixed height; the list should take the
        # leftover vertical space instead.
        sizer.GetItem(self.server_list).SetProportion(1)

        row = wx.BoxSizer(wx.HORIZONTAL)
        add = wx.Button(page, label="&Add server...")
        edit = wx.Button(page, label="&Edit server...")
        remove = wx.Button(page, label="&Remove server")
        add.Bind(wx.EVT_BUTTON, lambda e: self._add_server())
        edit.Bind(wx.EVT_BUTTON, lambda e: self._edit_server())
        remove.Bind(wx.EVT_BUTTON, lambda e: self._remove_server())
        row.Add(add, 0, wx.ALL, 4)
        row.Add(edit, 0, wx.ALL, 4)
        row.Add(remove, 0, wx.ALL, 4)
        sizer.Add(row, 0, wx.ALL, 4)
        page.SetSizer(sizer)
        return page

    def _build_sound_page(self) -> wx.Window:
        page = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)

        out_names, out_err = wizard.safe_enumerate_output_devices()
        self.out_choice: Optional[wx.Choice] = None
        self.out_spin: Optional[wx.SpinCtrl] = None
        if out_names:
            self.out_choice = _add_labeled(
                page, sizer, "&Player output device:",
                lambda p: wx.Choice(p, choices=out_names),
                "Where Cider sends the audio it plays.",
            )
            idx = self._state.output_device_index
            self.out_choice.SetSelection(idx if 0 <= idx < len(out_names) else 0)
        else:
            sizer.Add(
                wx.StaticText(
                    page,
                    label=f"Output devices could not be listed ({out_err}).\n"
                    "Enter the device index manually "
                    "(see: python Cider.py --devices).",
                ),
                0, wx.ALL, 6,
            )
            self.out_spin = _add_labeled(
                page, sizer, "&Player output device index:",
                lambda p: wx.SpinCtrl(p, min=0, max=999,
                                      initial=self._state.output_device_index),
                "Where Cider sends the audio it plays.",
            )

        in_names, in_err = wizard.safe_enumerate_input_devices()
        self.in_choice: Optional[wx.Choice] = None
        self.in_spin: Optional[wx.SpinCtrl] = None
        if in_names:
            self.in_choice = _add_labeled(
                page, sizer, "&TeamTalk input device:",
                lambda p: wx.Choice(p, choices=in_names),
                "The device TeamTalk transmits from "
                "(usually a virtual cable fed by the player output).",
            )
            idx = self._state.input_device_index
            self.in_choice.SetSelection(idx if 0 <= idx < len(in_names) else 0)
        else:
            sizer.Add(
                wx.StaticText(
                    page,
                    label=f"Input devices could not be listed ({in_err}).\n"
                    "Enter the device index manually "
                    "(see: python Cider.py --devices).",
                ),
                0, wx.ALL, 6,
            )
            self.in_spin = _add_labeled(
                page, sizer, "&TeamTalk input device index:",
                lambda p: wx.SpinCtrl(p, min=0, max=999,
                                      initial=self._state.input_device_index),
                "The device TeamTalk transmits from "
                "(usually a virtual cable fed by the player output).",
            )

        page.SetSizer(sizer)
        return page

    def _make_service_control(self, parent: wx.Window, box: wx.Sizer, fld: Field, value) -> wx.Window:
        if fld.kind == "list" and isinstance(value, list):
            text = ", ".join(value)
        elif fld.kind == "args" and isinstance(value, list):
            text = wizard.format_args(value)
        else:
            text = str(value)
        style = wx.TE_PASSWORD if fld.kind == "secret" else 0
        return _add_labeled(
            parent, box, f"{fld.label}:",
            lambda p: wx.TextCtrl(p, value=text, style=style),
            fld.help,
        )

    def _build_services_page(self) -> wx.Window:
        page = wx.Panel(self.notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.svc_enable: Dict[str, wx.CheckBox] = {}
        self.svc_fields: Dict[str, Dict[str, wx.Window]] = {}

        for spec in SERVICES:
            # The group is named "<service> settings" rather than just
            # "<service>" so entering it does not announce the service name
            # twice, once for the group and again for the checkbox inside it.
            box = wx.StaticBoxSizer(wx.VERTICAL, page, f"{spec.label} settings")
            parent = box.GetStaticBox()
            enable = wx.CheckBox(parent, label=ENABLE_LABELS[spec.key])
            enable.SetValue(self._state.service_enabled.get(spec.key, True))
            box.Add(enable, 0, wx.ALL, 6)
            self.svc_enable[spec.key] = enable

            values = self._state.service_config.get(spec.key, {})
            controls: Dict[str, wx.Window] = {}
            for fld in spec.fields:
                ctrl = self._make_service_control(parent, box, fld, values.get(fld.key, fld.default))
                ctrl.Enable(enable.GetValue())
                controls[fld.key] = ctrl
            self.svc_fields[spec.key] = controls
            # Toggling a service enables/disables its fields and adds or removes
            # it from the "Default service" choice.
            enable.Bind(wx.EVT_CHECKBOX, self._on_service_toggled)
            sizer.Add(box, 0, wx.EXPAND | wx.ALL, 6)

        self._default_service = self._state.default_service
        self._default_keys: List[str] = []
        self.default_choice = _add_labeled(
            page, sizer, "Service used for searc&hes:", wx.Choice,
            "Which service the play command searches at startup. URLs are always "
            "matched to a service by their address, so this does not affect them. "
            "Users can switch service at any time with the sv command.",
        )
        self._refresh_default_choice()
        page.SetSizer(sizer)
        return page

    def _on_service_toggled(self, event: wx.CommandEvent) -> None:
        for key, checkbox in self.svc_enable.items():
            for ctrl in self.svc_fields[key].values():
                ctrl.Enable(checkbox.GetValue())
        self._refresh_default_choice()
        event.Skip()

    def _refresh_default_choice(self) -> None:
        """List only enabled services, so the default can never name a disabled one."""
        self._remember_default_service()
        self._default_keys = [
            s.key for s in SERVICES if self.svc_enable[s.key].GetValue()
        ]
        labels = {s.key: s.label for s in SERVICES}
        self.default_choice.Set([labels[key] for key in self._default_keys])
        if not self._default_keys:
            return
        try:
            self.default_choice.SetSelection(self._default_keys.index(self._default_service))
        except ValueError:
            self.default_choice.SetSelection(0)

    def _remember_default_service(self) -> None:
        """Keep the last chosen service so it is restored if it is re-enabled."""
        index = self.default_choice.GetSelection()
        if index != wx.NOT_FOUND and index < len(self._default_keys):
            self._default_service = self._default_keys[index]

    # --- server list actions -------------------------------------------
    def _refresh_server_list(self) -> None:
        selection = self.server_list.GetSelection()
        self.server_list.Set([wizard.server_summary(s) for s in self.servers])
        if self.servers:
            new_sel = selection if 0 <= selection < len(self.servers) else 0
            self.server_list.SetSelection(new_sel)

    def _add_server(self) -> None:
        dialog = ServerDialog(self, None)
        if dialog.ShowModal() == wx.ID_OK and dialog.result is not None:
            self.servers.append(dialog.result)
            self._refresh_server_list()
            self.server_list.SetSelection(len(self.servers) - 1)
        dialog.Destroy()

    def _edit_server(self) -> None:
        index = self.server_list.GetSelection()
        if index == wx.NOT_FOUND:
            wx.MessageBox("Select a server to edit first.", "No server selected",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        dialog = ServerDialog(self, self.servers[index])
        if dialog.ShowModal() == wx.ID_OK and dialog.result is not None:
            self.servers[index] = dialog.result
            self._refresh_server_list()
            self.server_list.SetSelection(index)
        dialog.Destroy()

    def _remove_server(self) -> None:
        index = self.server_list.GetSelection()
        if index == wx.NOT_FOUND:
            wx.MessageBox("Select a server to remove first.", "No server selected",
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        if len(self.servers) == 1:
            wx.MessageBox("This is the only configured server, and Cider needs "
                          "at least one. Add another server before removing it.",
                          "At least one server is required",
                          wx.OK | wx.ICON_WARNING, self)
            return
        del self.servers[index]
        self._refresh_server_list()

    # --- save / cancel --------------------------------------------------
    def _collect_state(self) -> WizardState:
        state = WizardState(base_config=self._base_config)
        state.language = self.languages[self.lang_choice.GetSelection()]
        state.servers = list(self.servers)
        if self.out_choice is not None:
            state.output_device_index = self.out_choice.GetSelection()
        elif self.out_spin is not None:
            state.output_device_index = self.out_spin.GetValue()
        if self.in_choice is not None:
            state.input_device_index = self.in_choice.GetSelection()
        elif self.in_spin is not None:
            state.input_device_index = self.in_spin.GetValue()
        for spec in SERVICES:
            state.service_enabled[spec.key] = self.svc_enable[spec.key].GetValue()
            values = state.service_config.setdefault(spec.key, {})
            for fld in spec.fields:
                ctrl = self.svc_fields[spec.key][fld.key]
                if fld.kind == "list":
                    values[fld.key] = wizard.parse_admins(ctrl.GetValue())
                elif fld.kind == "args":
                    values[fld.key] = wizard.parse_args(ctrl.GetValue())
                else:
                    values[fld.key] = ctrl.GetValue()
        # Falls back to the remembered value when every service is disabled and
        # the choice is therefore empty.
        self._remember_default_service()
        state.default_service = self._default_service
        return state

    def _on_save(self, event: wx.CommandEvent) -> None:
        state = self._collect_state()
        try:
            config = wizard.build_config(state)
        except ValidationError as exc:
            wx.MessageBox(_format_errors(exc), "Configuration is not valid",
                          wx.OK | wx.ICON_ERROR, self)
            return

        if os.path.isfile(self.config_path):
            answer = wx.MessageBox(
                f"{self.config_path}\nalready exists. Overwrite it?",
                "Confirm overwrite",
                wx.YES_NO | wx.ICON_QUESTION,
                self,
            )
            if answer != wx.YES:
                return
        try:
            wizard.write_config(config, self.config_path)
        except OSError as exc:
            wx.MessageBox(f"Failed to write the file:\n{exc}",
                          "Could not save the configuration",
                          wx.OK | wx.ICON_ERROR, self)
            return
        wx.MessageBox(
            f"Configuration saved to:\n{self.config_path}\n\n"
            f"{len(state.servers)} server(s) configured. You can start the bot now.",
            "Configuration saved",
            wx.OK | wx.ICON_INFORMATION,
            self,
        )
        self._finish(0)

    def _on_cancel(self, event: wx.CommandEvent) -> None:
        self._finish(1)

    def _finish(self, code: int) -> None:
        self.exit_code = code
        self.Destroy()


def run(config_path: str) -> int:
    app = wx.App()
    frame = WizardFrame(config_path)
    frame.Show()
    app.MainLoop()
    return getattr(frame, "exit_code", 1)
