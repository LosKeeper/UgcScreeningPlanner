#!/usr/bin/env python3

import os
from base64 import b64decode
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import smtplib
import ssl
from typing import List, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

from loguru import logger
import requests
from icalendar import Calendar, Event


@dataclass
class CalendarEvent:
    """Représente un événement Google Calendar filtré (seuls les événements marqués comme occupé sont inclus)."""
    id: str
    summary: str
    start_datetime: Optional[datetime]
    end_datetime: Optional[datetime]
    all_day: bool
    status: str
    transp: str
    html_link: Optional[str]
    location: Optional[str]
    description: Optional[str]


class GoogleCalendarClient:
    """Client pour récupérer les événements d'un agenda Google Calendar partagé publiquement.

    Filtre automatiquement pour retourner uniquement les événements marqués comme occupé (TRANSP:OPAQUE).
    Les événements marqués comme libre (TRANSP:TRANSPARENT) ou autre ne sont pas inclus.
    """

    def __init__(
        self,
        shared_url: Optional[str] = None,
        calendar_id: Optional[str] = None,
    ):
        self.shared_url = shared_url or os.getenv("GOOGLE_CALENDAR_SHARED_URL")
        self.calendar_id = calendar_id or os.getenv("GOOGLE_CALENDAR_ID")

        if not self.calendar_id and self.shared_url:
            self.calendar_id = self._extract_calendar_id_from_url(
                self.shared_url)

        if not self.calendar_id:
            raise ValueError(
                "Aucun calendrier Google partagé configuré: fournissez `shared_url`, `calendar_id` ou `GOOGLE_CALENDAR_SHARED_URL`."
            )

        self.timezone_name = os.getenv(
            "GOOGLE_CALENDAR_TIMEZONE", "Europe/Paris")
        self.cinema_name = os.getenv(
            "UGC_CINEMA_NAME", "UGC Ciné Cité Strasbourg")

    def _extract_calendar_id_from_url(self, shared_url: str) -> str:
        """Extrait l'identifiant d'un calendrier depuis son URL partageable Google."""
        parsed = urlparse(shared_url)
        cid_values = parse_qs(parsed.query).get("cid")
        if not cid_values:
            raise ValueError(
                "Paramètre `cid` introuvable dans l'URL Google Calendar partagée")

        cid = cid_values[0]
        if "@" in cid:
            return cid

        padded = cid + "=" * (-len(cid) % 4)
        try:
            return b64decode(padded).decode("utf-8")
        except Exception as exc:
            raise ValueError(
                "Impossible de décoder le `cid` du calendrier partagé") from exc

    def _build_ics_urls(self) -> List[str]:
        """Construit les URLs iCal publiques possibles pour le calendrier."""
        calendar_id = quote(self.calendar_id, safe="@")
        return [
            f"https://calendar.google.com/calendar/ical/{calendar_id}/public/full.ics",
            f"https://calendar.google.com/calendar/ical/{calendar_id}/public/basic.ics",
        ]

    def _download_calendar(self) -> Calendar:
        """Télécharge le flux iCal public du calendrier."""
        last_error: Optional[Exception] = None

        for url in self._build_ics_urls():
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                logger.info("Flux Google Calendar public téléchargé")
                return Calendar.from_ical(response.text)
            except Exception as exc:
                last_error = exc

        raise RuntimeError(
            "Impossible de télécharger le flux public Google Calendar") from last_error

    def _parse_event_datetime(self, value: Optional[str]) -> Optional[datetime]:
        """Convertit une date/heure Google Calendar en `datetime`."""
        if not value:
            return None

        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)

    def _coerce_ical_datetime(self, value) -> Optional[datetime]:
        """Convertit une valeur iCal en `datetime`."""
        if value is None:
            return None

        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value

        if isinstance(value, date):
            return datetime.combine(value, time.min, tzinfo=timezone.utc)

        return self._parse_event_datetime(str(value))

    def _normalize_event(self, component) -> CalendarEvent:
        """Normalise un composant iCal `VEVENT` en dataclass."""
        start_value = component.decoded("DTSTART", None)
        end_value = component.decoded("DTEND", None)

        all_day = isinstance(start_value, date) and not isinstance(
            start_value, datetime)
        start_datetime = self._coerce_ical_datetime(start_value)
        end_datetime = self._coerce_ical_datetime(end_value)

        return CalendarEvent(
            id=str(component.get("UID", "")),
            summary=str(component.get("SUMMARY", "(Sans titre)")),
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            all_day=all_day,
            status=str(component.get("STATUS", "confirmed")).lower(),
            transp=str(component.get("TRANSP", "opaque")).lower(),
            html_link=str(component.get("URL")) if component.get(
                "URL") else None,
            location=str(component.get("LOCATION")) if component.get(
                "LOCATION") else None,
            description=str(component.get("DESCRIPTION")) if component.get(
                "DESCRIPTION") else None,
        )

    def _filter_events(
        self,
        events: List[CalendarEvent],
        time_min: Optional[datetime],
        time_max: Optional[datetime],
        max_results: int,
    ) -> List[CalendarEvent]:
        """Filtre les événements par intervalle, statut occupé et limite de résultats."""
        filtered = []

        for event in sorted(events, key=lambda item: item.start_datetime or datetime.max.replace(tzinfo=timezone.utc)):
            if event.start_datetime is None:
                continue

            # Filtre uniquement les événements marqués comme occupé (TRANSP:OPAQUE)
            if event.transp != "opaque":
                continue

            if time_min and event.end_datetime and event.end_datetime < time_min:
                continue
            if time_min and not event.end_datetime and event.start_datetime < time_min:
                continue
            if time_max and event.start_datetime >= time_max:
                continue

            filtered.append(event)
            if len(filtered) >= max_results:
                break

        return filtered

    def list_events(
        self,
        time_min: Optional[datetime] = None,
        time_max: Optional[datetime] = None,
        max_results: int = 100,
    ) -> List[CalendarEvent]:
        """Retourne les événements marqués comme occupé dans un intervalle donné."""
        calendar = self._download_calendar()
        events = [
            self._normalize_event(component)
            for component in calendar.walk()
            if component.name == "VEVENT"
        ]
        return self._filter_events(events, time_min, time_max, max_results)

    def list_upcoming_events(self, max_results: int = 10) -> List[CalendarEvent]:
        """Retourne les prochains événements marqués comme occupé à partir de maintenant."""
        now = datetime.now(timezone.utc)
        return self.list_events(time_min=now, max_results=max_results)

    def get_dates_until_next_tuesday(self) -> List[date]:
        """Génère la même plage de dates que le scraper UGC: d'aujourd'hui au mardi suivant inclus."""
        today = datetime.now().date()
        current_weekday = today.weekday()

        if current_weekday < 1:
            days_until_tuesday = 1 - current_weekday
        elif current_weekday == 1:
            days_until_tuesday = 7
        else:
            days_until_tuesday = (7 - current_weekday) + 1

        next_tuesday = today + timedelta(days=days_until_tuesday)

        dates = []
        current = today
        while current <= next_tuesday:
            dates.append(current)
            current += timedelta(days=1)

        return dates

    def list_events_for_ugc_date_range(self, max_results: int = 500) -> List[CalendarEvent]:
        """Retourne tous les événements marqués comme occupé sur la même plage de dates que les séances UGC."""
        dates = self.get_dates_until_next_tuesday()
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc

        start_of_range = datetime.combine(dates[0], time.min, tzinfo=local_tz)
        end_of_range = datetime.combine(
            dates[-1] + timedelta(days=1),
            time.min,
            tzinfo=local_tz,
        )

        logger.info(
            "Récupération des événements Google Calendar pour la plage UGC: {} -> {}",
            dates[0],
            dates[-1],
        )
        logger.info(
            "Récupération des événements Google Calendar occupés pour la plage UGC: {} -> {}",
            dates[0],
            dates[-1],
        )
        return self._filter_events(
            events,
            time_min=start_of_range,
            time_max=end_of_range,
            max_results=max_results,
        )

    def list_events_for_day(self, target_day: date) -> List[CalendarEvent]:
        """Retourne les événements marqués comme occupé d'une journée donnée."""
        start_of_day = datetime.combine(
            target_day, time.min, tzinfo=timezone.utc)
        end_of_day = start_of_day + timedelta(days=1)
        return self.list_events(time_min=start_of_day, time_max=end_of_day)

    def _build_screening_location(self, screening) -> str:
        """Construit le lieu affiché pour une séance planifiée."""
        salle = getattr(screening, "salle", None)
        if salle:
            return f"{self.cinema_name} - Salle {salle}"
        return self.cinema_name

    def _build_screening_description(self, screening) -> str:
        """Construit la description d'une séance planifiée."""
        lines = ["Séance UGC planifiée automatiquement"]

        version = getattr(screening, "version", None)
        if version:
            lines.append(f"Version: {version}")

        release_date = getattr(screening, "release_date", None)
        if release_date:
            lines.append(f"Sortie: {release_date.isoformat()}")

        score = getattr(screening, "score", None)
        if score is not None:
            lines.append(f"Score planner: {score:.2f}")

        return "\n".join(lines)

    def _build_event_uid(self, screening) -> str:
        """Construit un UID stable pour une séance planifiée."""
        start_dt = getattr(screening, "start_datetime")
        end_dt = getattr(screening, "end_datetime")
        title = getattr(screening, "title", "film")
        slug = "".join(ch.lower() if ch.isalnum()
                       else "-" for ch in title).strip("-")
        slug = "-".join(chunk for chunk in slug.split("-") if chunk) or "film"
        return (
            f"ugc-{slug}-{start_dt.strftime('%Y%m%dT%H%M%S')}-"
            f"{end_dt.strftime('%Y%m%dT%H%M%S')}@ugc-webscrap"
        )

    def export_planned_screenings_to_ics(
        self,
        planned_screenings,
        output_path: Optional[str] = None,
    ) -> Path:
        """Exporte les séances planifiées dans un fichier iCal importable."""
        destination = Path(output_path or os.getenv(
            "GOOGLE_CALENDAR_PLANNING_ICS",
            "planned_screenings.ics",
        ))

        calendar = Calendar()
        calendar.add("prodid", "-//ugc-webscrap//planned-screenings//fr")
        calendar.add("version", "2.0")

        for screening in planned_screenings:
            event = Event()
            event.add("uid", self._build_event_uid(screening))
            event.add("summary", getattr(screening, "title", "Séance UGC"))
            event.add("dtstart", getattr(screening, "start_datetime"))
            event.add("dtend", getattr(screening, "end_datetime"))
            event.add("location", self._build_screening_location(screening))
            event.add("description",
                      self._build_screening_description(screening))
            calendar.add_component(event)

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(calendar.to_ical())
        logger.success("Séances planifiées exportées dans {}", destination)
        return destination

    def create_test_ics_file(self, output_path: Optional[str] = None) -> Path:
        """Crée un petit fichier ICS de test pour valider l'envoi email."""
        destination = Path(output_path or os.getenv(
            "GOOGLE_CALENDAR_PLANNING_ICS",
            "planned_screenings.ics",
        ))

        now = datetime.now().replace(second=0, microsecond=0)
        start_dt = now + timedelta(hours=1)
        end_dt = start_dt + timedelta(hours=2)

        calendar = Calendar()
        calendar.add("prodid", "-//ugc-webscrap//test-email//fr")
        calendar.add("version", "2.0")

        event = Event()
        event.add(
            "uid", f"ugc-test-email-{start_dt.strftime('%Y%m%dT%H%M%S')}@ugc-webscrap")
        event.add("summary", "Test email UGC")
        event.add("dtstart", start_dt)
        event.add("dtend", end_dt)
        event.add("location", self.cinema_name)
        event.add(
            "description",
            "Événement de test généré automatiquement pour vérifier l'envoi du fichier ICS.",
        )
        calendar.add_component(event)

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(calendar.to_ical())
        logger.info("Fichier ICS de test généré dans {}", destination)
        return destination

    def build_planned_screening_add_links(self, planned_screenings) -> List[dict]:
        """Construit des liens Google Calendar préremplis pour les séances planifiées."""
        links = []

        for screening in planned_screenings:
            start_dt = getattr(screening, "start_datetime")
            end_dt = getattr(screening, "end_datetime")
            params = urlencode({
                "action": "TEMPLATE",
                "text": getattr(screening, "title", "Séance UGC"),
                "dates": f"{start_dt.strftime('%Y%m%dT%H%M%S')}/{end_dt.strftime('%Y%m%dT%H%M%S')}",
                "ctz": self.timezone_name,
                "location": self._build_screening_location(screening),
                "details": self._build_screening_description(screening),
            })
            links.append({
                "title": getattr(screening, "title", "Séance UGC"),
                "url": f"https://calendar.google.com/calendar/render?{params}",
            })

        return links

    def is_email_delivery_configured(self) -> bool:
        """Indique si l'envoi email du fichier ICS est configuré."""
        required_values = [
            os.getenv("EMAIL_SMTP_HOST"),
            os.getenv("EMAIL_SMTP_USERNAME"),
            os.getenv("EMAIL_SMTP_PASSWORD"),
            os.getenv("EMAIL_TO"),
        ]
        return all(required_values)

    def _build_smtp_connection(self, smtp_host: str, smtp_port: int, use_ssl: bool):
        """Crée une connexion SMTP compatible SSL implicite ou STARTTLS."""
        verify_tls = os.getenv(
            "EMAIL_VERIFY_TLS", "true").strip().lower() != "false"
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        if use_ssl:
            return smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30, context=context)

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls(context=context)
            server.ehlo()
        return server

    def send_ics_via_email(
        self,
        ics_path: str | Path,
        recipient: Optional[str] = None,
        subject: Optional[str] = None,
        body: Optional[str] = None,
    ) -> dict:
        """Envoie le fichier ICS en pièce jointe par email."""
        attachment_path = Path(ics_path)
        if not attachment_path.exists():
            raise FileNotFoundError(
                f"Fichier ICS introuvable: {attachment_path}")

        smtp_host = os.getenv("EMAIL_SMTP_HOST")
        smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        smtp_username = os.getenv("EMAIL_SMTP_USERNAME")
        smtp_password = os.getenv("EMAIL_SMTP_PASSWORD")
        sender = os.getenv("EMAIL_FROM") or smtp_username
        target_recipient = recipient or os.getenv("EMAIL_TO")

        if not all([smtp_host, smtp_username, smtp_password, sender, target_recipient]):
            raise RuntimeError(
                "Configuration email incomplète: renseignez EMAIL_SMTP_HOST, EMAIL_SMTP_USERNAME, EMAIL_SMTP_PASSWORD et EMAIL_TO."
            )

        message = EmailMessage()
        message["Subject"] = subject or os.getenv(
            "EMAIL_SUBJECT", "Séances UGC planifiées")
        message["From"] = sender
        message["To"] = target_recipient
        message.set_content(
            body
            or os.getenv(
                "EMAIL_BODY",
                "Bonjour,\n\nVoici le fichier ICS des séances UGC planifiées automatiquement.\n",
            )
        )
        message.add_attachment(
            attachment_path.read_bytes(),
            maintype="text",
            subtype="calendar",
            filename=attachment_path.name,
        )

        use_ssl = os.getenv("EMAIL_USE_SSL", "true").strip().lower() != "false"

        attempts = [(use_ssl, smtp_port)]
        if smtp_port == 587 and use_ssl:
            attempts.append((False, smtp_port))
        elif smtp_port == 465 and not use_ssl:
            attempts.append((True, smtp_port))

        last_error: Optional[Exception] = None

        for attempt_use_ssl, attempt_port in attempts:
            transport = "SSL" if attempt_use_ssl else "STARTTLS"
            try:
                logger.info(
                    "Envoi email via SMTP {} {}:{}",
                    transport,
                    smtp_host,
                    attempt_port,
                )
                with self._build_smtp_connection(smtp_host, attempt_port, attempt_use_ssl) as server:
                    server.login(smtp_username, smtp_password)
                    server.send_message(message)
                logger.success(
                    "Fichier ICS envoyé par email à {}", target_recipient)
                return {
                    "ok": True,
                    "to": target_recipient,
                    "subject": message["Subject"],
                    "attachment": str(attachment_path),
                    "transport": transport,
                    "host": smtp_host,
                    "port": attempt_port,
                }
            except ssl.SSLError as exc:
                last_error = exc
                logger.warning(
                    "Échec envoi email via {} {}:{}: {}",
                    transport,
                    smtp_host,
                    attempt_port,
                    exc,
                )
            except Exception as exc:
                last_error = exc
                if len(attempts) == 1:
                    raise
                logger.warning(
                    "Échec envoi email via {} {}:{}: {}",
                    transport,
                    smtp_host,
                    attempt_port,
                    exc,
                )

        raise RuntimeError(
            "Impossible d'envoyer le fichier ICS par email avec la configuration SMTP actuelle."
        ) from last_error
