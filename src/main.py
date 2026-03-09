
#!/usr/bin/env python3

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date, time
from pathlib import Path
import re
import unicodedata

from dotenv import load_dotenv
from loguru import logger
from modules import GoogleCalendarClient, ScreeningPlanner, UGCScrapper

# Charger les variables d'environnement depuis .env
load_dotenv()


def configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )


def json_default_serializer(value):
    from datetime import datetime

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat(timespec="minutes")
    raise TypeError(f"Type non sérialisable: {type(value)!r}")


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def write_json_output(payload, output_path: str | None) -> None:
    """Écrit la sortie JSON sur disque si un chemin est fourni."""
    if not output_path:
        logger.debug("Aucun fichier de sortie JSON configuré")
        return

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=json_default_serializer,
        ) + "\n",
        encoding="utf-8",
    )
    logger.success("Sortie JSON écrite dans {}", target)


def main(argv=None):
    configure_logging()
    p = argparse.ArgumentParser(description="Scrape UGC Strasbourg horaires")
    p.add_argument(
        "--url",
        default=os.getenv(
            "UGC_URL", "https://www.ugc.fr/cinema-ugc-cine-cite-strasbourg.html"),
        help="URL du cinéma UGC à scraper (par défaut: depuis .env ou Strasbourg)")
    p.add_argument(
        "--watchlist",
        action="store_true",
        help="Récupérer la watchlist (nécessite authentification)")
    p.add_argument(
        "--test-email",
        action="store_true",
        help="Envoie un email de test avec un fichier ICS sans lancer tout le pipeline")
    p.add_argument(
        "--email-to",
        help="Destinataire à utiliser avec --test-email (sinon EMAIL_TO)")
    p.add_argument(
        "--seances",
        action="store_true",
        help="Affiche uniquement les séances à venir (sans watchlist ni planification)")
    p.add_argument(
        "--output-json",
        default=os.getenv("UGC_OUTPUT_JSON"),
        help="Chemin du fichier JSON de sortie (sinon UGC_OUTPUT_JSON)")
    args = p.parse_args(argv)

    try:
        if args.test_email:
            logger.info("Mode test email activé")
            calendar_client = GoogleCalendarClient()
            test_ics = calendar_client.create_test_ics_file()
            logger.info("Fichier ICS de test généré: {}", test_ics)
            try:
                result = calendar_client.send_ics_via_email(
                    test_ics,
                    recipient=args.email_to,
                    subject="Test email UGC",
                    body="Bonjour,\n\nCeci est un email de test pour valider l'envoi du fichier ICS.\n",
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": str(exc),
                    "attachment": str(test_ics),
                    "recipient": args.email_to or os.getenv("EMAIL_TO"),
                }
            if result.get("ok"):
                logger.success("Email de test envoyé avec succès")
            else:
                logger.error(
                    "Échec de l'envoi de l'email de test: {}", result.get("error"))
            write_json_output(result, args.output_json)
            return 0 if result.get("ok", True) else 1

        # Récupérer les identifiants depuis .env si disponibles
        email = os.getenv("UGC_EMAIL")
        password = os.getenv("UGC_PASSWORD")

        scrapper = UGCScrapper(email=email, password=password)

        if args.seances:
            logger.info("Mode séances uniquement")
            screenings = scrapper.scrape_url(args.url)
            logger.success(
                "Séances récupérées pour {} film(s)", len(screenings))
            payload = {
                titre: [asdict(seance) for seance in seances]
                for titre, seances in screenings.items()
            }
            write_json_output(payload, args.output_json)
        elif args.watchlist:
            logger.info("Mode watchlist activé")
            watchlist = scrapper.scrape_watchlist()
            logger.success(
                "{} film(s) récupéré(s) dans la watchlist", len(watchlist))
            write_json_output([asdict(film)
                              for film in watchlist], args.output_json)
        else:
            logger.info("Étape 1/4 - Récupération de la watchlist")
            watchlist = scrapper.scrape_watchlist()
            logger.info("Watchlist brute: {} film(s)", len(watchlist))

            logger.info("Étape 2/4 - Récupération des séances UGC")
            screenings = scrapper.scrape_url(args.url)
            logger.info("Séances récupérées pour {} film(s)", len(screenings))

            logger.info(
                "Étape 3/4 - Récupération des événements Google Calendar")
            calendar_client = GoogleCalendarClient()
            calendar_events = calendar_client.list_events_for_ugc_date_range()
            logger.info("{} événement(s) agenda récupéré(s)",
                        len(calendar_events))

            calendar_titles = {
                normalize_title(event.summary)
                for event in calendar_events
                if event.summary
            }
            filtered_watchlist = [
                film for film in watchlist
                if normalize_title(film.title) not in calendar_titles
            ]

            removed_count = len(watchlist) - len(filtered_watchlist)
            if removed_count:
                logger.info(
                    "{} film(s) retiré(s) de la watchlist car déjà présents dans l'agenda",
                    removed_count,
                )

            watchlist_by_normalized_title = {
                normalize_title(film.title): film
                for film in filtered_watchlist
            }
            screenings_by_normalized_title = {
                normalize_title(title): seances
                for title, seances in screenings.items()
            }
            common_titles = set(watchlist_by_normalized_title) & set(
                screenings_by_normalized_title)

            intersected_watchlist = [
                watchlist_by_normalized_title[title]
                for title in watchlist_by_normalized_title
                if title in common_titles
            ]
            intersected_screenings = {
                watchlist_by_normalized_title[title].title: screenings_by_normalized_title[title]
                for title in watchlist_by_normalized_title
                if title in common_titles
            }

            logger.info(
                "{} film(s) conservé(s) après intersection watchlist/séances",
                len(intersected_watchlist),
            )

            logger.info("Étape 4/4 - Planification des séances dans l'agenda")
            planner = ScreeningPlanner()
            ranked_watchlist = planner.rank_watchlist(
                watchlist=intersected_watchlist,
                screenings=intersected_screenings,
            )
            planning = planner.plan_screenings(
                watchlist=intersected_watchlist,
                screenings=intersected_screenings,
                calendar_events=calendar_events,
            )

            calendar_export = None
            calendar_add_links = []
            calendar_email = None
            if planning.scheduled:
                export_path = calendar_client.export_planned_screenings_to_ics(
                    planning.scheduled
                )
                logger.success(
                    "{} séance(s) planifiée(s) exportée(s) dans {}",
                    len(planning.scheduled),
                    export_path,
                )
                calendar_export = {
                    "ics_file": str(export_path),
                }
                calendar_add_links = calendar_client.build_planned_screening_add_links(
                    planning.scheduled
                )
                logger.info(
                    "{} lien(s) Google Calendar généré(s)",
                    len(calendar_add_links),
                )
                if calendar_client.is_email_delivery_configured():
                    try:
                        calendar_email = calendar_client.send_ics_via_email(
                            export_path)
                        logger.success(
                            "Email ICS envoyé à {}",
                            calendar_email.get("to") or os.getenv("EMAIL_TO"),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Envoi email ignoré: {}",
                            exc,
                        )
                        calendar_email = {
                            "error": str(exc),
                            "attachment": str(export_path),
                        }
            else:
                logger.warning(
                    "Aucune séance planifiée, aucun export ICS généré")

            payload = {
                "watchlist": [asdict(film) for film in ranked_watchlist],
                "screenings": {
                    titre: [asdict(seance) for seance in seances]
                    for titre, seances in intersected_screenings.items()
                },
                "calendar_events": [asdict(event) for event in calendar_events],
                "planning": {
                    "scheduled": [asdict(item) for item in planning.scheduled],
                    "unscheduled_titles": planning.unscheduled_titles,
                },
                "calendar_export": calendar_export,
                "calendar_add_links": calendar_add_links,
                "calendar_email": calendar_email,
            }

            logger.success(
                "Pipeline terminé: {} film(s) classé(s), {} séance(s) planifiée(s), {} film(s) non planifié(s)",
                len(ranked_watchlist),
                len(planning.scheduled),
                len(planning.unscheduled_titles),
            )
            write_json_output(payload, args.output_json)
    except Exception as exc:
        logger.error("Erreur lors de la récupération de la page: {}", exc)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
