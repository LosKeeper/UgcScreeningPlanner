#!/usr/bin/env python3

import threading
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from .config_store import WEEKDAYS, get_schedule, load_config
from .runner import PipelineRunner


def next_run(schedule: dict, now: datetime) -> Optional[datetime]:
    """Retourne la prochaine date de lancement automatique, ou None si désactivé."""
    if not schedule["enabled"] or not schedule["days"]:
        return None

    run_time = datetime.strptime(schedule["time"], "%H:%M").time()
    for offset in range(8):
        candidate = datetime.combine(
            now.date() + timedelta(days=offset), run_time)
        if candidate > now and WEEKDAYS[candidate.weekday()] in schedule["days"]:
            return candidate
    return None


class Scheduler(threading.Thread):
    """Déclenche le pipeline selon la clé `schedule` de la configuration du planner."""

    TICK_SECONDS = 20

    def __init__(self, runner: PipelineRunner):
        super().__init__(daemon=True)
        self.runner = runner
        self._stop_event = threading.Event()
        self._last_fired: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    def _tick(self) -> None:
        # La configuration est relue à chaque tick: pas de redémarrage après modification
        schedule = get_schedule(load_config())
        now = datetime.now()
        minute_key = now.strftime("%Y-%m-%d %H:%M")

        if (
            not schedule["enabled"]
            or WEEKDAYS[now.weekday()] not in schedule["days"]
            or now.strftime("%H:%M") != schedule["time"]
            or minute_key == self._last_fired
        ):
            return

        self._last_fired = minute_key
        if not self.runner.start("scheduled"):
            logger.warning(
                "Lancement automatique ignoré: un pipeline est déjà en cours")

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                logger.error("Erreur du planificateur: {}", exc)
            self._stop_event.wait(self.TICK_SECONDS)
