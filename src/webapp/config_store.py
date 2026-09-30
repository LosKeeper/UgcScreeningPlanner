#!/usr/bin/env python3

import json
import os
from datetime import date, datetime
from pathlib import Path

from modules.planner import ScreeningPlanner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "planner_config.example.json"
WEEKDAYS = list(ScreeningPlanner.WEEKDAY_NAMES.values())
DEFAULT_SCHEDULE = {"enabled": True, "days": ["mardi"], "time": "11:00"}


class ConfigError(ValueError):
    """Configuration de planning invalide."""


def resolve_path(value: str) -> Path:
    """Résout un chemin relatif par rapport à la racine du projet."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def config_path() -> Path:
    return resolve_path(os.getenv(
        "UGC_PLANNER_CONFIG", ScreeningPlanner.DEFAULT_CONFIG_PATH))


def load_config() -> dict:
    """Charge la configuration du planner, ou l'exemple si elle n'existe pas encore."""
    path = config_path()
    if not path.is_file():
        path = EXAMPLE_CONFIG_PATH
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_schedule(config: dict) -> dict:
    return {**DEFAULT_SCHEDULE, **(config.get("schedule") or {})}


def _validate_time(value, label: str) -> str:
    try:
        return datetime.strptime(str(value), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ConfigError(f"{label}: heure invalide ({value!r}), format attendu HH:MM")


def _validate_weight(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{label}: le poids doit être un nombre supérieur à 0")
    return float(value)


def _validate_weekday(value, label: str) -> str:
    if value not in WEEKDAYS:
        raise ConfigError(f"{label}: jour inconnu ({value!r})")
    return value


def _validate_date(value, label: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ConfigError(f"{label}: date invalide ({value!r}), format attendu AAAA-MM-JJ")


def _validate_windows(value, label: str) -> list:
    if not isinstance(value, list):
        raise ConfigError(f"{label}: une liste de créneaux est attendue")

    windows = []
    for index, window in enumerate(value, start=1):
        window_label = f"{label}, créneau {index}"
        if not isinstance(window, dict):
            raise ConfigError(f"{window_label}: format invalide")
        windows.append({
            "start": _validate_time(window.get("start"), window_label),
            "end": _validate_time(window.get("end"), window_label),
            "weight": _validate_weight(window.get("weight", 1.0), window_label),
        })
    return windows


def _validate_mapping(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{label}: format invalide")
    return value


def validate_config(raw) -> dict:
    """Valide et normalise une configuration de planning. Les clés inconnues sont conservées."""
    config = dict(_validate_mapping(raw, "Configuration"))

    config["default_availability"] = _validate_windows(
        config.get("default_availability", []), "Disponibilité par défaut")
    config["weekly_availability"] = {
        _validate_weekday(day, "Disponibilités hebdomadaires"): _validate_windows(windows, day.capitalize())
        for day, windows in _validate_mapping(
            config.get("weekly_availability", {}), "Disponibilités hebdomadaires").items()
    }
    config["weekday_weights"] = {
        _validate_weekday(day, "Poids des jours"): _validate_weight(weight, day.capitalize())
        for day, weight in _validate_mapping(
            config.get("weekday_weights", {}), "Poids des jours").items()
    }
    config["daily_availability"] = {
        _validate_date(day, "Exceptions par date"): _validate_windows(windows, f"Exception du {day}")
        for day, windows in _validate_mapping(
            config.get("daily_availability", {}), "Exceptions par date").items()
    }
    config["day_weights"] = {
        _validate_date(day, "Exceptions par date"): _validate_weight(weight, f"Exception du {day}")
        for day, weight in _validate_mapping(
            config.get("day_weights", {}), "Exceptions par date").items()
    }
    config["default_day_weight"] = _validate_weight(
        config.get("default_day_weight", 1.0), "Poids par défaut")

    buffer_minutes = config.get("buffer_minutes", 0)
    if isinstance(buffer_minutes, bool) or not isinstance(buffer_minutes, int) or buffer_minutes < 0:
        raise ConfigError("Buffer: un nombre entier de minutes positif est attendu")

    schedule = _validate_mapping(
        config.get("schedule") or DEFAULT_SCHEDULE, "Lancement automatique")
    days = schedule.get("days", [])
    if not isinstance(days, list):
        raise ConfigError("Lancement automatique: une liste de jours est attendue")
    config["schedule"] = {
        "enabled": bool(schedule.get("enabled", True)),
        "days": [day for day in WEEKDAYS
                 if day in {_validate_weekday(item, "Lancement automatique") for item in days}],
        "time": _validate_time(schedule.get("time", DEFAULT_SCHEDULE["time"]), "Lancement automatique"),
    }

    return config


def save_config(raw) -> dict:
    """Valide puis écrit la configuration du planner."""
    config = validate_config(raw)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Écriture en place: le fichier peut être un bind-mount Docker, qu'un rename casserait
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    return config
