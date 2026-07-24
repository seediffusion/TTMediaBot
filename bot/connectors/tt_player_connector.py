from __future__ import annotations
import logging
from threading import Thread
import time
from typing import TYPE_CHECKING

from bot.player import State
from bot import app_vars

if TYPE_CHECKING:
    from bot import Bot


class TTPlayerConnector(Thread):
    def __init__(self, bot: Bot):
        super().__init__(daemon=True)
        self.name = "TTPlayerConnector"
        self.player = bot.player
        self.ttclients = bot.ttclients
        self.translator = bot.translator

    def run(self):
        last_player_state = State.Stopped
        # Matches the Stopped state's status text, so a fresh, idle bot does not
        # issue a redundant status update before anything plays.
        last_status_text = ""
        self._close = False
        while not self._close:
            try:
                state = self.player.state
                # Voice transmission only flips on a real state change.
                if state != last_player_state:
                    last_player_state = state
                    if state == State.Playing:
                        for ttclient in self.ttclients:
                            ttclient.enable_voice_transmission()
                    else:
                        for ttclient in self.ttclients:
                            ttclient.disable_voice_transmission()

                # The status text is recomputed every tick, not just on a state
                # change, so it follows track changes while playback stays in the
                # Playing state (e.g. advancing through a playlist).
                status_text = self._status_text(state)
                if status_text != last_status_text:
                    last_status_text = status_text
                    for ttclient in self.ttclients:
                        ttclient.change_status_text(status_text)
            except Exception:
                logging.error("", exc_info=True)
            time.sleep(app_vars.loop_timeout)

    def _status_text(self, state: State) -> str:
        """The TeamTalk status to show for the current player state.

        While playing, show the current media's title (its URL if untitled, as
        the "t" command does), falling back to a generic label if neither is
        known yet. An empty string resets the client to its default status.
        """
        if state == State.Playing:
            track = self.player.track
            return (
                track.name
                or track.url
                or self.translator.translate("Streaming media")
            )
        if state == State.Paused:
            return self.translator.translate("Paused")
        return ""

    def close(self):
        self._close = True
