#!/usr/bin/env python3

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from loguru import logger

from .google_calendar import CalendarEvent
from .ugc_scraper import Seance, WatchlistFilm


@dataclass
class AvailabilityWindow:
    """Fenêtre de disponibilité pour une journée donnée."""
    start: time
    end: time
    weight: float = 1.0


@dataclass
class PlannedScreening:
    """Séance retenue pour un film dans le planning final."""
    title: str
    release_date: Optional[date]
    screening_date: date
    start_datetime: datetime
    end_datetime: datetime
    version: str
    salle: str
    day_weight: float
    availability_weight: float
    film_weight: float
    score: float
    pub_min: Optional[int] = None


@dataclass
class PlanningResult:
    """Résultat de planification des séances."""
    scheduled: List[PlannedScreening]
    unscheduled_titles: List[str]


@dataclass
class WeightedWatchlistFilm:
    """Film restant avec son poids de priorité pour la planification."""
    title: str
    release_date: Optional[date]
    weight: float


class ScreeningPlanner:
    """Planifie des séances UGC dans les disponibilités utilisateur en évitant les événements agenda."""

    DEFAULT_CONFIG_PATH = "planner_config.json"
    WEEKDAY_NAMES = {
        0: "lundi",
        1: "mardi",
        2: "mercredi",
        3: "jeudi",
        4: "vendredi",
        5: "samedi",
        6: "dimanche",
    }

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path or os.getenv(
            "UGC_PLANNER_CONFIG", self.DEFAULT_CONFIG_PATH))
        self.config = self._load_config()
        # Les séances UGC sont en heure locale: les événements agenda y sont ramenés
        self.timezone = ZoneInfo(
            os.getenv("GOOGLE_CALENDAR_TIMEZONE") or "Europe/Paris")

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            logger.warning(
                "Fichier de planning absent ({}), utilisation d'une configuration vide",
                self.config_path,
            )
            return {
                "weekly_availability": {},
                "default_availability": [
                    {"start": "00:00", "end": "23:59", "weight": 1.0}
                ],
                "default_day_weight": 1.0,
                "weekday_weights": {},
                "buffer_minutes": 0,
            }

        with self.config_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _weekday_key(self, day: date) -> str:
        """Retourne le nom du jour en français pour une date donnée."""
        return self.WEEKDAY_NAMES[day.weekday()]

    def _parse_time(self, value: str) -> time:
        return datetime.strptime(value, "%H:%M").time()

    def _get_windows_for_day(self, day: date) -> List[AvailabilityWindow]:
        # Priorité: exception par date > jour de semaine > défaut
        raw_windows = self.config.get(
            "daily_availability", {}).get(day.isoformat())

        if raw_windows is None:
            raw_windows = self.config.get("weekly_availability", {}).get(
                self._weekday_key(day),
                self.config.get("default_availability", []),
            )

        return [
            AvailabilityWindow(
                start=self._parse_time(window["start"]),
                end=self._parse_time(window["end"]),
                weight=float(window.get("weight", 1.0)),
            )
            for window in raw_windows
        ]

    def _get_day_weight(self, day: date) -> float:
        # Priorité: exception par date > jour de semaine > défaut
        return float(
            self.config.get("day_weights", {}).get(
                day.isoformat(),
                self.config.get("weekday_weights", {}).get(
                    self._weekday_key(day),
                    self.config.get("default_day_weight", 1.0),
                ),
            )
        )

    def _buffer(self) -> timedelta:
        configured_minutes = int(self.config.get("buffer_minutes", 0))
        return timedelta(minutes=max(30, configured_minutes))

    def _film_weight(self, film: WatchlistFilm, reference_day: date) -> float:
        if not film.release_date:
            return 1.0

        days_since_release = max(0, (reference_day - film.release_date).days)
        weeks_since_release = days_since_release // 7
        return 1.0 + float(weeks_since_release)

    def rank_watchlist(
        self,
        watchlist: List[WatchlistFilm],
        screenings: Dict[str, List[Seance]],
    ) -> List[WeightedWatchlistFilm]:
        """Calcule un poids de priorité pour les films restants de la watchlist."""
        if not watchlist or not screenings:
            return []

        screening_dates = [
            screening.date
            for seances in screenings.values()
            for screening in seances
        ]
        if not screening_dates:
            return [
                WeightedWatchlistFilm(
                    title=film.title,
                    release_date=film.release_date,
                    weight=1.0,
                )
                for film in watchlist
            ]

        reference_day = min(screening_dates)

        ranked_films = [
            WeightedWatchlistFilm(
                title=film.title,
                release_date=film.release_date,
                weight=self._film_weight(film, reference_day),
            )
            for film in watchlist
        ]

        return sorted(
            ranked_films,
            key=lambda film: (-film.weight,
                              film.release_date or date.max, film.title),
        )

    def _screening_bounds(self, screening: Seance) -> Tuple[datetime, datetime]:
        start_dt = datetime.combine(screening.date, screening.heure_debut)
        if screening.heure_fin is None:
            end_dt = start_dt
        else:
            end_dt = datetime.combine(screening.date, screening.heure_fin)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
        return start_dt, end_dt

    def _to_local_naive(self, value: datetime) -> datetime:
        """Ramène une date/heure agenda en heure locale sans fuseau, comme les séances UGC."""
        if value.tzinfo is None:
            return value
        return value.astimezone(self.timezone).replace(tzinfo=None)

    def _event_bounds(self, event: CalendarEvent) -> Optional[Tuple[datetime, datetime]]:
        if event.start_datetime is None:
            return None

        start_dt = self._to_local_naive(event.start_datetime)
        if event.end_datetime is None:
            end_dt = start_dt
        else:
            end_dt = self._to_local_naive(event.end_datetime)
        return start_dt, end_dt

    def _fits_window(self, screening: Seance, start_dt: datetime, end_dt: datetime) -> Optional[AvailabilityWindow]:
        for window in self._get_windows_for_day(screening.date):
            window_start = datetime.combine(screening.date, window.start)
            window_end = datetime.combine(screening.date, window.end)
            if window_end <= window_start:
                window_end += timedelta(days=1)
            if start_dt >= window_start and end_dt <= window_end:
                return window
        return None

    def _overlaps(self, first: Tuple[datetime, datetime], second: Tuple[datetime, datetime]) -> bool:
        return first[0] < second[1] and second[0] < first[1]

    def _buffered(self, start_dt: datetime, end_dt: datetime) -> Tuple[datetime, datetime]:
        buffer = self._buffer()
        return start_dt - buffer, end_dt + buffer

    def _is_free_from_calendar(
        self,
        start_dt: datetime,
        end_dt: datetime,
        busy_intervals: List[Tuple[datetime, datetime]],
    ) -> bool:
        buffered = self._buffered(start_dt, end_dt)
        return not any(self._overlaps(buffered, busy) for busy in busy_intervals)

    def _build_candidate(
        self,
        film: WatchlistFilm,
        screening: Seance,
        busy_intervals: List[Tuple[datetime, datetime]],
        reference_day: date,
    ) -> Optional[PlannedScreening]:
        start_dt, end_dt = self._screening_bounds(screening)
        window = self._fits_window(screening, start_dt, end_dt)
        if window is None:
            return None
        if not self._is_free_from_calendar(start_dt, end_dt, busy_intervals):
            return None

        day_weight = self._get_day_weight(screening.date)
        film_weight = self._film_weight(film, reference_day)
        score = day_weight * window.weight * film_weight

        return PlannedScreening(
            title=film.title,
            release_date=film.release_date,
            screening_date=screening.date,
            start_datetime=start_dt,
            end_datetime=end_dt,
            version=screening.version,
            salle=screening.salle,
            day_weight=day_weight,
            availability_weight=window.weight,
            film_weight=film_weight,
            score=score,
            pub_min=screening.pub_min,
        )

    def _select_best_plan(
        self,
        candidates_by_title: Dict[str, List[PlannedScreening]],
    ) -> PlanningResult:
        titles = sorted(candidates_by_title, key=lambda title: (
            len(candidates_by_title[title]), title))

        # Une seule séance par jour: pour un film et un jour donnés, seule la
        # meilleure séance (score le plus haut, puis la plus tôt) peut être retenue.
        best_by_day: Dict[str, Dict[date, PlannedScreening]] = {}
        for title in titles:
            best_by_day[title] = {}
            for candidate in sorted(
                candidates_by_title[title],
                key=lambda candidate: (-candidate.score,
                                       candidate.start_datetime),
            ):
                best_by_day[title].setdefault(
                    candidate.screening_date, candidate)

        day_bits = {
            day: 1 << index
            for index, day in enumerate(sorted({
                day for days in best_by_day.values() for day in days
            }))
        }

        # Programmation dynamique sur les jours occupés: pour chaque ensemble de
        # jours pris, on garde le meilleur planning (nombre de films, puis score).
        states: Dict[int, Tuple[int, float, Tuple[PlannedScreening, ...]]] = {
            0: (0, 0.0, ())}

        for title in titles:
            next_states = dict(states)
            for mask, (count, total_score, selected) in states.items():
                for day, candidate in best_by_day[title].items():
                    if mask & day_bits[day]:
                        continue

                    # Garde-fou: une séance finissant après minuit ne doit pas
                    # chevaucher celle du lendemain
                    buffered = self._buffered(
                        candidate.start_datetime, candidate.end_datetime)
                    if any(
                        self._overlaps(buffered, self._buffered(
                            existing.start_datetime, existing.end_datetime))
                        for existing in selected
                    ):
                        continue

                    new_mask = mask | day_bits[day]
                    new_state = (
                        count + 1,
                        total_score + candidate.score,
                        selected + (candidate,),
                    )
                    current = next_states.get(new_mask)
                    if current is None or new_state[:2] > current[:2]:
                        next_states[new_mask] = new_state
            states = next_states

        best_selection = max(states.values(), key=lambda state: state[:2])[2]
        scheduled_titles = {item.title for item in best_selection}
        unscheduled_titles = [
            title for title in titles if title not in scheduled_titles]
        return PlanningResult(
            scheduled=sorted(
                best_selection, key=lambda item: item.start_datetime),
            unscheduled_titles=unscheduled_titles,
        )

    def plan_screenings(
        self,
        watchlist: List[WatchlistFilm],
        screenings: Dict[str, List[Seance]],
        calendar_events: List[CalendarEvent],
    ) -> PlanningResult:
        """Planifie une séance par film restant, en respectant agenda et disponibilités."""
        if not watchlist or not screenings:
            return PlanningResult(scheduled=[], unscheduled_titles=[])

        reference_day = min(
            (
                screening.date
                for seances in screenings.values()
                for screening in seances
            ),
            default=date.today(),
        )

        busy_intervals = [
            bounds
            for event in calendar_events
            if (bounds := self._event_bounds(event))
        ]

        watchlist_by_title = {film.title: film for film in watchlist}
        candidates_by_title: Dict[str, List[PlannedScreening]] = {}

        for title, seances in screenings.items():
            film = watchlist_by_title.get(title)
            if film is None:
                continue

            candidates = [
                candidate
                for screening in seances
                if (candidate := self._build_candidate(film, screening, busy_intervals, reference_day))
            ]
            candidates_by_title[title] = candidates

        return self._select_best_plan(candidates_by_title)
