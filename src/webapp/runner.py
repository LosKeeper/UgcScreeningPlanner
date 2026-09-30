#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from loguru import logger

from .config_store import PROJECT_ROOT, resolve_path


def output_json_path() -> Path:
    return resolve_path(os.getenv("UGC_OUTPUT_JSON", "output/ugc_output.json"))


class PipelineRunner:
    """Lance le pipeline complet en sous-processus et conserve ses logs."""

    def __init__(self, command: Optional[List[str]] = None):
        self.command = command or [
            sys.executable,
            str(PROJECT_ROOT / "src" / "main.py"),
            "--output-json",
            str(output_json_path()),
        ]
        self.log_path = output_json_path().parent / "last_run.log"
        self.meta_path = output_json_path().parent / "last_run.json"
        self._lock = threading.Lock()
        self._lines: List[str] = []
        self._meta = {
            "status": "idle",
            "trigger": None,
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
        }
        self._restore_last_run()

    def _restore_last_run(self) -> None:
        """Recharge le dernier lancement terminé, pour l'afficher après un redémarrage."""
        try:
            self._meta.update(json.loads(
                self.meta_path.read_text(encoding="utf-8")))
            self._lines = self.log_path.read_text(
                encoding="utf-8").splitlines()
        except (OSError, ValueError):
            pass

    def start(self, trigger: str) -> bool:
        """Démarre le pipeline. Retourne False si un lancement est déjà en cours."""
        with self._lock:
            if self._meta["status"] == "running":
                return False
            self._lines = []
            self._meta = {
                "status": "running",
                "trigger": trigger,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "finished_at": None,
                "exit_code": None,
            }

        logger.info("Lancement du pipeline ({})", trigger)
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        exit_code = -1
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    self.command,
                    cwd=PROJECT_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
                for line in process.stdout:
                    line = line.rstrip("\n")
                    log_file.write(line + "\n")
                    log_file.flush()
                    with self._lock:
                        self._lines.append(line)
                exit_code = process.wait()
        except Exception as exc:
            logger.error("Échec du lancement du pipeline: {}", exc)
            with self._lock:
                self._lines.append(f"Échec du lancement du pipeline: {exc}")

        with self._lock:
            self._meta["status"] = "success" if exit_code == 0 else "failed"
            self._meta["finished_at"] = datetime.now().isoformat(
                timespec="seconds")
            self._meta["exit_code"] = exit_code
            meta = dict(self._meta)

        logger.info("Pipeline terminé (code {})", exit_code)
        try:
            self.meta_path.write_text(json.dumps(meta), encoding="utf-8")
        except OSError as exc:
            logger.warning("Impossible d'écrire {}: {}", self.meta_path, exc)

    def snapshot(self, since: int = 0) -> dict:
        """Retourne l'état du lancement courant et les lignes de logs à partir de `since`."""
        with self._lock:
            return {
                **self._meta,
                "lines": self._lines[max(0, since):],
                "total_lines": len(self._lines),
            }
