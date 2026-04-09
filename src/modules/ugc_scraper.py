#!/usr/bin/env python3

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page


@dataclass
class Seance:
    """Représente une séance de cinéma."""
    date: date
    heure_debut: time
    heure_fin: Optional[time]
    version: str
    salle: str


@dataclass
class WatchlistFilm:
    """Représente un film de la watchlist."""
    title: str
    release_date: Optional[date]


class UGCScrapper:
    """Classe pour scraper les séances de cinéma UGC."""

    FRENCH_MONTHS = {
        "janvier": 1,
        "février": 2,
        "fevrier": 2,
        "mars": 3,
        "avril": 4,
        "mai": 5,
        "juin": 6,
        "juillet": 7,
        "août": 8,
        "aout": 8,
        "septembre": 9,
        "octobre": 10,
        "novembre": 11,
        "décembre": 12,
        "decembre": 12,
    }

    def __init__(self, email: Optional[str] = None, password: Optional[str] = None):
        """
        Initialise le scraper.

        Args:
            email: Email pour l'authentification (optionnel)
            password: Mot de passe pour l'authentification (optionnel)
        """
        self.email = email
        self.password = password
        self.max_login_attempts = int(
            os.getenv("UGC_MAX_LOGIN_ATTEMPTS", "20"))
        self.extra_cookies = self._parse_extra_cookies(
            os.getenv("UGC_EXTRA_COOKIES", "")
        )
        self.cookie_url = os.getenv("UGC_COOKIE_URL", "https://www.ugc.fr/")
        self.session_file = Path(os.getenv(
            "UGC_SESSION_FILE",
            str(Path(__file__).resolve().parent.parent / ".ugc_session.json")
        ))

    def _parse_extra_cookies(self, cookie_string: str) -> List[Dict[str, str]]:
        """Parse une chaîne de cookies de type 'a=b; c=d'."""
        normalized = cookie_string.strip()
        if not normalized:
            return []

        if normalized.lower().startswith("cookie:"):
            normalized = normalized.split(":", 1)[1].strip()

        cookies: List[Dict[str, str]] = []
        for raw_cookie in normalized.split(";"):
            chunk = raw_cookie.strip()
            if not chunk or "=" not in chunk:
                continue

            name, value = chunk.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue

            cookies.append({"name": name, "value": value})

        return cookies

    def _apply_extra_cookies(self, context: BrowserContext) -> None:
        """Injecte les cookies UGC personnalisés dans le contexte."""
        if not self.extra_cookies:
            return

        try:
            context.add_cookies([
                {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "url": self.cookie_url,
                }
                for cookie in self.extra_cookies
            ])
            logger.info("{} cookie(s) personnalisé(s) injecté(s)",
                        len(self.extra_cookies))
        except Exception as e:
            logger.warning(
                "Impossible d'injecter les cookies personnalisés: {}", e)

    def _create_context(self, browser: Browser, use_persisted_session: bool = False) -> BrowserContext:
        """Crée un contexte Playwright, avec session persistée si demandée."""
        if use_persisted_session and self.session_file.exists():
            context = browser.new_context(storage_state=str(self.session_file))
            self._apply_extra_cookies(context)
            return context

        context = browser.new_context()
        self._apply_extra_cookies(context)
        return context

    def _save_session(self, page: Page) -> None:
        """Sauvegarde l'état de session Playwright sur disque."""
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            page.context.storage_state(path=str(self.session_file))
            logger.success("Session persistée sauvegardée ({})",
                           self.session_file.name)
        except Exception as e:
            logger.warning("Impossible de sauvegarder la session: {}", e)

    def _enable_remember_me(self, page: Page) -> None:
        """Active l'option 'Se souvenir de moi' si elle est disponible."""
        try:
            page.wait_for_selector(
                '#remember-me', state='attached', timeout=5000)

            remember_me = page.locator('#remember-me')
            if remember_me.is_checked():
                logger.debug("Option 'Se souvenir de moi' déjà cochée")
                return

            try:
                page.locator('label[for="remember-me"]').click(timeout=5000)
            except Exception:
                page.evaluate("""
                    () => {
                        const checkbox = document.querySelector('#remember-me');
                        if (!checkbox || checkbox.checked) return;

                        checkbox.checked = true;
                        checkbox.dispatchEvent(new Event('input', { bubbles: true }));
                        checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                """)

            if remember_me.is_checked():
                logger.debug("Option 'Se souvenir de moi' cochée")
            else:
                logger.warning(
                    "Impossible de confirmer la case 'Se souvenir de moi'")
        except Exception as e:
            logger.warning("Impossible d'activer 'Se souvenir de moi': {}", e)

    def _try_reuse_persisted_session(self, browser: Browser) -> Optional[Page]:
        """Tente de réutiliser une session sauvegardée déjà authentifiée."""
        if not self.session_file.exists():
            return None

        logger.info("Réutilisation de la session persistée")
        context = self._create_context(browser, use_persisted_session=True)
        page = context.new_page()

        try:
            page.goto("https://www.ugc.fr/profil.html",
                      wait_until="domcontentloaded")
            self._accept_cookies(page)

            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            if self._is_still_on_login_page(page) or "connexion" in page.url.lower():
                logger.warning(
                    "Session persistée expirée, nouvelle connexion nécessaire")
                page.close()
                context.close()
                return None

            logger.success("Session persistée réutilisée")
            return page
        except Exception as e:
            logger.warning("Réutilisation de session impossible: {}", e)
            page.close()
            context.close()
            return None

    def _accept_cookies(self, page: Page) -> None:
        """
        Accepte automatiquement les popups (cookies et publicité) si présents.

        Args:
            page: La page Playwright
        """
        try:
            # Attendre un peu que les popups apparaissent
            page.wait_for_timeout(1000)

            try:
                accepted = page.evaluate("""
                    () => {
                        const button = document.querySelector(
                            '#hagreed button.hagreed__buttons__btn.accept, '
                            + '#hagreed button.hagreed-validate, '
                            + '#hagreed .hagreed__buttons__btn.accept'
                        );

                        if (button) {
                            button.click();
                            return true;
                        }

                        return false;
                    }
                """)
                if accepted:
                    logger.debug("Cookies acceptés")
                    page.wait_for_timeout(500)
            except Exception:
                pass

            # 1. Chercher et accepter le popup de cookies
            cookie_selectors = [
                'button.hagreed__buttons__btn.accept.hagreed-validate',
                'button:has-text("ACCEPTER")',
                'button:has-text("Accepter")',
                '.accept-cookies',
                '#acceptCookies'
            ]

            for selector in cookie_selectors:
                try:
                    if page.query_selector(selector):
                        page.click(selector, timeout=2000)
                        logger.debug("Cookies acceptés")
                        page.wait_for_timeout(500)
                        break
                except:
                    continue

            try:
                page.evaluate("""
                    () => {
                        const overlay = document.querySelector('#hagreed');
                        if (!overlay) return;

                        const style = window.getComputedStyle(overlay);
                        if (style.display !== 'none' && style.visibility !== 'hidden') {
                            overlay.style.pointerEvents = 'none';
                            overlay.style.opacity = '0';
                        }
                    }
                """)
            except Exception:
                pass

            # 2. Chercher et fermer le popup de publicité/newsletter
            ad_close_selectors = [
                '#modal-nl-advertising-rgpd-close',
                'button.close[data-dismiss="modal"]',
                '.modal button.close',
                '#modal-nl-advertising-rgpd button.close',
                'button[aria-label="Close"]'
            ]

            for selector in ad_close_selectors:
                try:
                    if page.query_selector(selector):
                        page.click(selector, timeout=2000)
                        logger.debug("Popup publicité fermé")
                        page.wait_for_timeout(500)
                        break
                except:
                    continue

            pass
        except Exception as e:
            logger.debug("Gestion des popups ignorée: {}", e)
            pass

    def _login_interactive(self, browser: Browser) -> Page:
        """
        Authentification interactive : ouvre le navigateur visible et attend que l'utilisateur se connecte.

        Returns:
            Page avec la session authentifiée
        """
        raise RuntimeError(
            "Mode interactif désactivé: fournissez une session persistée ou des cookies UGC valides."
        )

    def _is_still_on_login_page(self, page: Page) -> bool:
        """Indique si la page courante correspond encore au formulaire de connexion."""
        current_url = page.url.lower()
        return "login" in current_url or page.locator('#j_spring_security_check_form').count() > 0

    def _is_target_closed_error(self, error: Exception) -> bool:
        """Indique si l'erreur Playwright correspond à une page/contexte fermé."""
        return "Target page, context or browser has been closed" in str(error)

    def _get_active_page(self, page: Page) -> Page:
        """Retourne la page active du contexte si la page initiale a été fermée/remplacée."""
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass

        try:
            for candidate in reversed(page.context.pages):
                if not candidate.is_closed():
                    return candidate
        except Exception:
            pass

        return page

    def _get_or_create_page(self, page: Page) -> Page:
        """Retourne une page exploitable du contexte, en en rouvrant une si besoin."""
        active_page = self._get_active_page(page)

        try:
            if not active_page.is_closed():
                return active_page
        except Exception:
            pass

        return page.context.new_page()

    def _wait_for_login_result(self, page: Page, timeout_ms: int = 15000) -> bool:
        """Attend le résultat de la tentative de connexion."""
        remaining = timeout_ms

        while remaining > 0:
            page = self._get_active_page(page)

            try:
                if page.is_closed():
                    return True
            except Exception:
                return True

            if not self._is_still_on_login_page(page):
                return True

            try:
                page.wait_for_load_state(
                    "networkidle", timeout=min(2000, remaining))
            except Exception as e:
                if self._is_target_closed_error(e):
                    return True

            step = min(500, remaining)
            try:
                page.wait_for_timeout(step)
            except Exception as e:
                if self._is_target_closed_error(e):
                    return True
                raise
            remaining -= step

        page = self._get_active_page(page)
        try:
            if page.is_closed():
                return True
        except Exception:
            return True

        return not self._is_still_on_login_page(page)

    def _get_login_feedback(self, page: Page) -> Dict[str, Optional[str]]:
        """Récupère les informations utiles après une tentative de soumission."""
        return page.evaluate("""
            () => {
                const selectors = [
                    '[role="alert"]',
                    '.alert',
                    '.alert-danger',
                    '.alert-warning',
                    '.invalid-feedback',
                    '.form-error',
                    '.message-erreur',
                    '.message-error',
                    '.notification',
                    '.toast',
                    '.help-block'
                ];

                const messages = selectors
                    .flatMap(selector => Array.from(document.querySelectorAll(selector)))
                    .map(el => (el.textContent || '').trim())
                    .filter(Boolean);

                const widget = document.querySelector('#j_spring_security_check_form div[name="frc-captcha"]')?.frcWidget;
                const button = document.querySelector('#connectLink');

                return {
                    feedback: messages.join(' | ') || null,
                    buttonDisabled: button ? String(Boolean(button.disabled)) : null,
                    captchaState: widget?.state ?? null
                };
            }
        """)

    def _wait_for_captcha_completion(self, page: Page, timeout_ms: int = 90000) -> bool:
        """Attend la fin du FriendlyCaptcha après le clic sur connexion."""
        remaining = timeout_ms

        while remaining > 0:
            page = self._get_active_page(page)

            try:
                if page.is_closed():
                    return True
            except Exception:
                return True

            if not self._is_still_on_login_page(page):
                return True

            state = self._get_login_feedback(page).get('captchaState')

            if state in (None, 'completed'):
                return True

            if state not in ('solving', 'ready', 'verifying'):
                return False

            step = min(1000, remaining)
            try:
                page.wait_for_timeout(step)
            except Exception as e:
                if self._is_target_closed_error(e):
                    return True
                raise
            remaining -= step

        return False

    def _is_truthy_flag(self, value: Optional[str]) -> bool:
        """Convertit une valeur texte/bool JS en booléen Python."""
        return str(value).strip().lower() == 'true'

    def _submit_login_form(self, page: Page) -> Page:
        """Soumet le formulaire de connexion en respectant le flux front UGC."""
        self._accept_cookies(page)

        button = page.locator('#connectLink')
        button.scroll_into_view_if_needed()

        logger.info("Soumission du formulaire de connexion")
        try:
            button.click(timeout=5000)
        except Exception as e:
            if 'intercepts pointer events' not in str(e):
                raise

            logger.warning(
                "Le bandeau cookies bloque le clic, tentative renforcée")
            self._accept_cookies(page)
            button.click(timeout=5000, force=True)

        if self._wait_for_login_result(page, timeout_ms=5000):
            return self._get_active_page(page)

        page = self._get_active_page(page)

        state = self._get_login_feedback(page)
        if state.get('feedback'):
            logger.warning("Message après soumission: {}", state['feedback'])

        logger.debug(
            "État captcha: {} | bouton désactivé: {}",
            state.get('captchaState'),
            state.get('buttonDisabled'),
        )

        if state.get('captchaState') in ('solving', 'ready', 'verifying'):
            logger.info("Captcha en cours de résolution, attente prolongée")
            if self._wait_for_captcha_completion(page, timeout_ms=30000):
                logger.info("Captcha terminé, attente de la redirection")
                if self._wait_for_login_result(page, timeout_ms=15000):
                    return self._get_active_page(page)

            page = self._get_active_page(page)
            state = self._get_login_feedback(page)
            if state.get('feedback'):
                logger.warning("Message après captcha: {}", state['feedback'])
            logger.debug(
                "État captcha après attente: {} | bouton désactivé: {}",
                state.get('captchaState'),
                state.get('buttonDisabled'),
            )

        if state.get('feedback') and 'déjà été envoyée' in state['feedback'].lower():
            logger.warning(
                "Une requête semble déjà partie, pas de nouvelle soumission automatique")
            return self._get_active_page(page)

        if self._is_truthy_flag(state.get('buttonDisabled')):
            logger.warning(
                "Le formulaire est déjà en cours de traitement, pas de nouvelle soumission")
            return self._get_active_page(page)

        if state.get('captchaState') in ('solving', 'ready', 'verifying'):
            logger.warning(
                "Captcha encore actif après attente, tentative unique de secours via clic DOM")
        else:
            logger.warning(
                "Le clic n'a pas semblé partir, tentative de secours via clic DOM")

        page.locator('#connectLink').evaluate("button => button.click()")
        self._wait_for_login_result(page, timeout_ms=15000)
        return self._get_active_page(page)

    def _login_automatic(self, browser: Browser) -> Page:
        """
        Authentification automatique avec email et mot de passe.

        Returns:
            Page avec la session authentifiée
        """
        logger.info("Authentification automatique UGC")
        logger.info("Connexion avec {}", self.email)
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_login_attempts + 1):
            context = self._create_context(browser)
            page = context.new_page()

            try:
                logger.info("Tentative {}/{}", attempt,
                            self.max_login_attempts)

                page.goto("https://www.ugc.fr/login.html",
                          wait_until="domcontentloaded")

                # Accepter les cookies
                self._accept_cookies(page)

                page.wait_for_selector('#mail', state='visible', timeout=10000)
                page.wait_for_selector(
                    '#password', state='visible', timeout=10000)

                # Utiliser les identifiants explicites du formulaire UGC
                page.locator('#mail').fill(self.email)
                page.locator('#password').fill(self.password)
                self._enable_remember_me(page)

                # Déclencher explicitement les événements attendus par la validation front
                for selector in ('#mail', '#password', '#remember-me'):
                    page.dispatch_event(selector, 'input')
                    page.dispatch_event(selector, 'change')
                    page.dispatch_event(selector, 'blur')

                page.wait_for_timeout(1000)

                values = page.evaluate("""
                    () => ({
                        email: document.querySelector('#mail')?.value ?? '',
                        password: document.querySelector('#password')?.value ?? '',
                        buttonDisabled: document.querySelector('#connectLink')?.disabled ?? null
                    })
                """)
                logger.debug("Email rempli: {}", values['email'])
                logger.debug("Mot de passe rempli: {} caractères",
                             len(values['password']))
                logger.debug("Bouton désactivé: {}", values['buttonDisabled'])

                logger.debug("Attente des validations asynchrones")
                page.wait_for_timeout(2000)

                try:
                    if page.query_selector('#modal-nl-advertising-rgpd-close'):
                        page.click(
                            '#modal-nl-advertising-rgpd-close', timeout=2000)
                        logger.debug("Popup fermé avant soumission")
                        page.wait_for_timeout(500)
                except Exception:
                    pass

                page.wait_for_selector(
                    '#connectLink', state='visible', timeout=5000)

                page = self._submit_login_form(page)
                page = self._get_or_create_page(page)
                page.goto("https://www.ugc.fr/profil.html",
                          wait_until="domcontentloaded")

                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                self._accept_cookies(page)

                logger.debug("URL après connexion: {}", page.url)

                if self._is_still_on_login_page(page) or "connexion" in page.url.lower():
                    logger.warning(
                        "La connexion automatique a échoué - toujours sur la page de login")
                    last_error = Exception("Toujours sur la page de login")
                else:
                    logger.success("Connexion réussie")
                    self._save_session(page)
                    return page

            except Exception as e:
                last_error = e
                logger.warning(
                    "Erreur lors de la connexion automatique: {}", e)

            try:
                page.close()
            except Exception:
                pass

            try:
                context.close()
            except Exception:
                pass

            if attempt < self.max_login_attempts:
                logger.info("Nouvelle tentative de connexion automatique")

        raise Exception(
            f"Connexion automatique impossible après {self.max_login_attempts} tentatives"
        ) from last_error

    def _get_authenticated_context(self, browser: Browser) -> Page:
        """
        Récupère une page authentifiée.

        Stratégie :
        1. Si email/password fournis → connexion automatique
        2. Sinon → connexion interactive

        Returns:
            Page authentifiée
        """
        persisted_page = self._try_reuse_persisted_session(browser)
        if persisted_page:
            return persisted_page

        # Stratégie 1: Connexion automatique
        if self.email and self.password:
            return self._login_automatic(browser)

        raise RuntimeError(
            "Aucun moyen d'authentification disponible: fournissez une session persistée, des cookies UGC valides ou des identifiants."
        )

    def get_dates_until_next_tuesday(self) -> List[str]:
        """Génère une liste de dates du jour jusqu'au mardi prochain inclus."""
        today = datetime.now().date()

        # Le jour de la semaine (0=lundi, 1=mardi, ..., 6=dimanche)
        current_weekday = today.weekday()

        # Calculer le nombre de jours jusqu'au prochain mardi
        # Si on est mardi (1), on veut le mardi suivant (7 jours)
        # Sinon, on calcule les jours restants jusqu'au prochain mardi
        if current_weekday < 1:  # lundi (0)
            days_until_tuesday = 1 - current_weekday
        elif current_weekday == 1:  # mardi
            days_until_tuesday = 7  # prochain mardi
        else:  # mercredi à dimanche (2-6)
            days_until_tuesday = (7 - current_weekday) + 1

        next_tuesday = today + timedelta(days=days_until_tuesday)

        # Générer toutes les dates entre aujourd'hui et mardi prochain inclus
        dates = []
        current = today
        while current <= next_tuesday:
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)

        return dates

    def _extract_watchlist_title(self, link) -> str:
        """Extrait le titre d'un film depuis la structure interne de la watchlist."""
        title_elem = link.select_one("div.info-wrapper div.block--title")
        if title_elem:
            return title_elem.get_text(strip=True)

        title_attr = link.get("title", "").strip()
        if title_attr:
            return title_attr

        return link.get_text(strip=True)

    def _parse_french_release_date(self, raw_date: str) -> Optional[date]:
        """Convertit une date française UGC en objet `date`."""
        normalized = re.sub(r"\s+", " ", raw_date.strip().lower())
        match = re.match(
            r"^(\d{1,2})\s+([a-zéûîôàèùç]+)\s+(\d{4})$", normalized)
        if not match:
            return None

        day_str, month_str, year_str = match.groups()
        month = self.FRENCH_MONTHS.get(month_str)
        if month is None:
            return None

        try:
            return date(int(year_str), month, int(day_str))
        except ValueError:
            return None

    def _parse_screening_date(self, raw_date: str) -> date:
        """Convertit une date de séance UGC en objet `date`."""
        normalized = raw_date.strip()

        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(normalized, fmt).date()
            except ValueError:
                continue

        raise ValueError(f"Format de date de séance inconnu: {raw_date}")

    def _parse_screening_time(self, raw_time: str) -> Optional[time]:
        """Convertit une heure de séance UGC en objet `time`."""
        normalized = raw_time.strip()
        if not normalized or normalized.lower() == "fin inconnue":
            return None

        return datetime.strptime(normalized, "%H:%M").time()

    def _extract_hhmm(self, raw_value: str) -> Optional[str]:
        """Extrait un horaire HH:MM depuis un texte libre."""
        match = re.search(r"(\d{1,2}:\d{2})", raw_value)
        return match.group(1) if match else None

    def _extract_release_date(self, html_content: str) -> Optional[date]:
        """Extrait la date de sortie depuis la page d'un film UGC."""
        soup = BeautifulSoup(html_content, "html.parser")

        info_candidates = soup.select(
            "main .block--infos, main .film-info, main .info-wrapper, main .movie-infos"
        )

        for candidate in info_candidates:
            text = candidate.get_text(" ", strip=True)
            match = re.search(
                r"Sortie le\s+([0-9]{1,2}\s+[^0-9]+\s+[0-9]{4})", text, re.IGNORECASE)
            if match:
                return self._parse_french_release_date(match.group(1).strip())

        main_text = soup.select_one("main")
        if main_text:
            text = main_text.get_text(" ", strip=True)
            match = re.search(
                r"Sortie le\s+([0-9]{1,2}\s+[^0-9]+\s+[0-9]{4})", text, re.IGNORECASE)
            if match:
                return self._parse_french_release_date(match.group(1).strip())

        return None

    def _fetch_watchlist_release_date(self, page: Page, film_url: str) -> Optional[date]:
        """Ouvre la page d'un film et récupère sa date de sortie."""
        film_page = page.context.new_page()

        try:
            film_page.goto(film_url, wait_until="networkidle")
            self._accept_cookies(film_page)
            release_date = self._extract_release_date(film_page.content())
            logger.debug("Date de sortie extraite pour {}: {}",
                         film_url, release_date)
            return release_date
        finally:
            film_page.close()

    def _filter_screenings(self, screenings: List[Seance]) -> List[Seance]:
        """Nettoie les séances: supprime les salles inconnues et privilégie la VO si VO+VF existent."""
        screenings_with_room = [
            screening for screening in screenings
            if screening.salle and screening.salle.strip().lower() != "inconnue"
        ]

        has_vo = any(screening.version.upper().startswith("VO")
                     for screening in screenings_with_room)
        has_vf = any(screening.version.upper() ==
                     "VF" for screening in screenings_with_room)

        if has_vo and has_vf:
            return [
                screening for screening in screenings_with_room
                if screening.version.upper().startswith("VO")
            ]

        return screenings_with_room

    def scrape_watchlist(self) -> List[WatchlistFilm]:
        """
        Scrape la watchlist de l'utilisateur (nécessite authentification).

        Returns:
            Liste des films de la watchlist avec date de sortie
        """
        logger.info("Récupération de la watchlist")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = self._get_authenticated_context(browser)

            try:
                page.goto("https://www.ugc.fr/profil.html",
                          wait_until="networkidle")

                # Accepter les cookies si le popup apparaît
                self._accept_cookies(page)

                # Vérifier si on est bien authentifié
                if self._is_still_on_login_page(page) or "connexion" in page.url.lower():
                    raise Exception(
                        "Non authentifié. La session a peut-être expiré.")

                html_content = page.content()
                soup = BeautifulSoup(html_content, "html.parser")

                # Parser la watchlist
                watchlist: List[WatchlistFilm] = []

                # Sélectionner tous les éléments <a> avec un id du style goToFilm_[0-9]*
                film_links = soup.find_all("a", id=re.compile(r"goToFilm_\d+"))

                for link in film_links:
                    title = self._extract_watchlist_title(link)
                    href = link.get("href")
                    if title:  # Ne garder que les titres non vides
                        film_url = urljoin(
                            "https://www.ugc.fr/", href) if href else None
                        release_date = self._fetch_watchlist_release_date(
                            page, film_url) if film_url else None
                        watchlist.append(
                            WatchlistFilm(
                                title=title,
                                release_date=release_date,
                            )
                        )

                logger.success(
                    "Watchlist récupérée avec succès ({} films)", len(watchlist))
                return watchlist

            except Exception as e:
                logger.error(
                    "Erreur lors de la récupération de la watchlist: {}", e)
                raise
            finally:
                page.close()
                browser.close()

    def scrape_url(self, url: str) -> Dict[str, List[Seance]]:
        """Scrape l'URL donnée et retourne un mapping titre -> séances."""
        dates_to_scrape = self.get_dates_until_next_tuesday()
        logger.info("Récupération des séances pour les dates: {}",
                    ', '.join(dates_to_scrape))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Certaines pages UGC gardent des requêtes en arrière-plan actives.
            # On tente un networkidle court sans bloquer le scraping si ça n'aboutit pas.
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                logger.debug(
                    "networkidle non atteint sur la page cinéma, on continue")

            # Accepter les cookies si le popup apparaît
            self._accept_cookies(page)

            result: Dict[str, List[Seance]] = {}

            for date_str in dates_to_scrape:
                logger.debug("Récupération de la date {}", date_str)

                # Cliquer sur le sélecteur de date
                date_clicked = page.evaluate(f"""
                () => {{
                const el = document.querySelector('#nav_date_{date_str}');
                if (!el) return false;

                el.dispatchEvent(new PointerEvent('pointerdown', {{ bubbles: true }}));
                el.dispatchEvent(new PointerEvent('pointerup', {{ bubbles: true }}));
                el.dispatchEvent(new MouseEvent('click', {{ bubbles: true }}));
                return true;
                }}
                """)

                if not date_clicked:
                    logger.warning(
                        "Sélecteur de date introuvable pour {}", date_str)
                    continue

                page.wait_for_timeout(1500)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    logger.debug(
                        "networkidle non atteint après clic date {}", date_str)

                try:
                    page.wait_for_selector(
                        "div[id^='bloc-showing-film-']", timeout=10000)
                except Exception:
                    logger.warning(
                        "Aucun bloc film détecté pour la date {} (timeout)", date_str
                    )
                    continue

                html_content = page.content()

                soup = BeautifulSoup(html_content, "html.parser")

                film_divs = soup.find_all(
                    "div", id=re.compile(r"bloc-showing-film-\d+"))

                for film_div in film_divs:
                    title_elem = film_div.find(
                        "a", id=re.compile(r"goToFilm_\d+_info_title"))
                    if not title_elem:
                        continue

                    title = title_elem.get_text(strip=True)

                    screening_list = film_div.find(
                        "ul", class_="component--screening-cards no-bullets d-flex flex-wrap p-0"
                    )

                    if screening_list:
                        for li in screening_list.find_all("li"):
                            button = li.find("button")
                            if button:
                                screening_date = button.get(
                                    "data-seancedate", "")
                                version = button.get("data-version", "")
                                hourstart_elem = button.find(
                                    "div", class_="screening-start") or button.find(
                                        "div", class_="screening-time-start")
                                hourend_elem = button.find(
                                    "div", class_="screening-end") or button.find(
                                        "div", class_="screening-time-end")
                                salle_elem = button.find(
                                    "div", class_="color--white text-capitalize screening-detail") or button.find(
                                        "div", class_="color--white text-capitalize screening-room")

                                if not version:
                                    version_elem = button.find(
                                        "span", class_="screening-lang")
                                    version = version_elem.get_text(
                                        strip=True) if version_elem else "VF"

                                if hourstart_elem:
                                    hourstart_raw = hourstart_elem.get_text(
                                        strip=True)
                                    hourstart = self._extract_hhmm(
                                        hourstart_raw) or hourstart_raw

                                    hourend = "Fin inconnue"
                                    if hourend_elem:
                                        hourend_raw = hourend_elem.get_text(
                                            strip=True)
                                        hourend = self._extract_hhmm(
                                            hourend_raw) or "Fin inconnue"

                                    salle = "inconnue"
                                    if salle_elem:
                                        salle_text = salle_elem.get_text(
                                            strip=True)
                                        salle = salle_text.split(
                                            " ", 1)[1] if " " in salle_text else salle_text

                                    heure_debut = self._parse_screening_time(
                                        hourstart)
                                    if heure_debut is None:
                                        logger.debug(
                                            "Horaire de début illisible ignoré: {}", hourstart
                                        )
                                        continue

                                    seance = Seance(
                                        date=self._parse_screening_date(
                                            screening_date),
                                        heure_debut=heure_debut,
                                        heure_fin=self._parse_screening_time(
                                            hourend),
                                        version=version,
                                        salle=salle
                                    )

                                    # Ajouter la séance au film
                                    if title not in result:
                                        result[title] = []
                                    result[title].append(seance)

            browser.close()

        # Filtrer les films sans séances, supprimer les séances sans salle,
        # et conserver uniquement la VO lorsqu'un film propose VO + VF.
        result = {
            titre: filtered_seances
            for titre, seances in result.items()
            if (filtered_seances := self._filter_screenings(seances))
        }

        return result
