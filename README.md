# UGC Screening Planner

Python tool that scrapes UGC cinema showtimes, cross-references them with your watchlist and Google Calendar, and automatically plans the best screenings for you.

## Structure du projet

```
ugc-webscrap/
├── src/
│   ├── main.py         # Script principal
│   └── modules/        # Modules Python du projet
│       ├── __init__.py
│       ├── google_calendar.py
│       ├── planner.py
│       └── ugc_scraper.py
├── .env                # Configuration (URL du cinéma)
├── .env.example        # Exemple de configuration
├── requirements.txt    # Dépendances Python
├── Dockerfile          # Image Docker avec Playwright + supercronic
├── docker-compose.yml  # Stack Docker (Portainer-ready)
├── .dockerignore
└── README.md
```

## Installation

1. Installer les dépendances:

```bash
python3 -m pip install -r requirements.txt
```

2. Installer les navigateurs Playwright:

```bash
python3 -m playwright install chromium
```

3. Configurer l'URL du cinéma (optionnel):

Copier `.env.example` vers `.env` et modifier si nécessaire:

```bash
cp .env.example .env
```

Fichier `.env` par défaut:

```env
# URL du cinéma
UGC_URL=https://www.ugc.fr/cinema-ugc-cine-cite-strasbourg.html

# Identifiants (optionnel, pour accéder aux pages authentifiées)
UGC_EMAIL=votre.email@example.com
UGC_PASSWORD=votre_mot_de_passe

# Cookies personnalisés injectés en plus dans le navigateur Playwright
UGC_EXTRA_COOKIES=nom1=valeur1; nom2=valeur2
UGC_COOKIE_URL=https://www.ugc.fr/

# Niveau de logs
LOG_LEVEL=INFO
```

**Note sur les identifiants** : Ils ne sont nécessaires que pour accéder aux pages authentifiées (profil, réservations, etc.). Pour le scraping des séances publiques, ils ne sont pas requis.

## Utilisation

### Scraper les séances (pas d'authentification requise)

Lancer le pipeline:

```bash
python3 src/main.py
```

Le script journalise maintenant son avancement dans le terminal via `loguru`.

Si vous voulez aussi conserver le résultat JSON, indiquez un fichier de sortie :

```bash
python3 src/main.py --output-json ugc_output.json
```

Vous pouvez aussi le configurer dans `.env` :

```env
UGC_OUTPUT_JSON=ugc_output.json
```

L'URL est automatiquement chargée depuis le fichier `.env`. Vous pouvez aussi spécifier une URL différente en ligne de commande:

```bash
python3 src/main.py --url https://www.ugc.fr/cinema-autre-ville.html
```

### Lister les séances à venir (sans watchlist ni planification)

Pour récupérer uniquement les séances à venir du cinéma :

```bash
python3 src/main.py --seances
```

Avec export JSON :

```bash
python3 src/main.py --seances --output-json seances.json
```

### Scraper la watchlist (authentification requise)

Pour accéder aux pages authentifiées comme la watchlist :

```bash
python3 src/main.py --watchlist
```

Avec export JSON optionnel :

```bash
python3 src/main.py --watchlist --output-json watchlist.json
```

**Méthodes d'authentification** :

Le script utilise les stratégies suivantes dans cet ordre :

1. **Session persistée** : si `.ugc_session.json` est valide, elle est réutilisée automatiquement

2. **Cookies personnalisés** : si `UGC_EXTRA_COOKIES` est renseigné, ils sont injectés dans le navigateur Playwright

3. **Connexion automatique** : si `UGC_EMAIL` et `UGC_PASSWORD` sont dans `.env`, le script tente une connexion automatique

Le script sauvegarde aussi une session persistée dans `.ugc_session.json` et coche l'option "Se souvenir de moi" lors de la connexion. Si la session est encore valide, elle est réutilisée automatiquement au lancement suivant.

Si besoin, vous pouvez aussi injecter vos propres cookies avec `UGC_EXTRA_COOKIES`. Format attendu : `nom1=valeur1; nom2=valeur2`. Ils sont ajoutés à chaque nouveau contexte Playwright, y compris lors de la réutilisation de la session persistée.

Les logs passent par `loguru` et s'affichent sur stderr. Vous pouvez ajuster leur verbosité avec `LOG_LEVEL`.

Le pipeline principal ne fait plus de `print()` final sur stdout. Les résultats structurés sont écrits dans un fichier JSON uniquement si `--output-json` ou `UGC_OUTPUT_JSON` est défini.

## Fonctionnement

### Google Calendar
Une classe dédiée est disponible dans [src/modules/google_calendar.py](src/modules/google_calendar.py) : `GoogleCalendarClient`.

Configuration minimale dans `.env` :

```env
GOOGLE_CALENDAR_SHARED_URL=https://calendar.google.com/calendar/u/0?cid=...
GOOGLE_CALENDAR_ID=
```

Le client se base sur l'URL partageable Google Calendar et télécharge le flux iCal public correspondant. Aucune authentification Google n'est nécessaire.

Le script peut aussi exporter les séances retenues dans [planned_screenings.ics](planned_screenings.ics), puis l'envoyer automatiquement par email en pièce jointe si une configuration SMTP est présente.

Méthodes principales :
- `list_upcoming_events()`
- `list_events()`
- `list_events_for_day()`

### Envoi du fichier ICS par email

Configuration optionnelle dans `.env` :

```env
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=465
EMAIL_USE_SSL=true
EMAIL_SMTP_USERNAME=votre.adresse@gmail.com
EMAIL_SMTP_PASSWORD=votre_mot_de_passe_application
EMAIL_FROM=votre.adresse@gmail.com
EMAIL_TO=votre.adresse@gmail.com
EMAIL_SUBJECT=Séances UGC planifiées
```

Pour Gmail, utilisez un mot de passe d'application, pas votre mot de passe principal.

Si ces variables sont définies, le pipeline principal :
- génère [planned_screenings.ics](planned_screenings.ics)
- envoie ce fichier en pièce jointe à votre adresse

Test d'envoi rapide :

```bash
python3 src/main.py --test-email
```

Avec fichier de résultat JSON :

```bash
python3 src/main.py --test-email --output-json email_test.json
```

### Planification des séances
Une classe dédiée est disponible dans [src/modules/planner.py](src/modules/planner.py) : `ScreeningPlanner`.

Configuration facultative dans `.env` :

```env
UGC_PLANNER_CONFIG=planner_config.json
```

Copier le fichier d'exemple et l'adapter :

```bash
cp planner_config.example.json planner_config.json
```

Le fichier JSON de planning permet de définir des disponibilités par date, un poids par jour et un buffer autour des événements existants.

Le format recommandé est générique par jour de semaine avec des clés françaises dans `weekly_availability` et `weekday_weights` :
- `lundi`
- `mardi`
- `mercredi`
- `jeudi`
- `vendredi`
- `samedi`
- `dimanche`

### Scraping des séances
Le scraper récupère les séances de cinéma du jour jusqu'au mardi suivant inclus. Les données sont retournées au format JSON avec pour chaque film:
- Date de la séance
- Heure de début et de fin
- Version (VF, VO, etc.)
- Numéro de salle

### Scraping de la watchlist
Nécessite une authentification. Le scraper se connecte à chaque exécution :
1. En réutilisant la session persistée si elle existe
2. En injectant les cookies supplémentaires éventuels
3. En tentant un login automatique si nécessaire
4. Puis en récupérant les informations demandées (watchlist, etc.)

La classe principale du scraper UGC est disponible dans [src/modules/ugc_scraper.py](src/modules/ugc_scraper.py) : `UGCScrapper`.

Le navigateur Chromium utilisé pour la watchlist s'exécute en mode headless par défaut, donc sans fenêtre visible.

## Docker

Le projet est dockerisé et prêt à être déployé via **Portainer** ou tout autre orchestrateur Docker.

### Build & lancement rapide

```bash
docker compose up --build -d
```

Le container tourne en continu et exécute automatiquement le pipeline **tous les mardis à 11h** (heure de Paris) grâce à [supercronic](https://github.com/aptible/supercronic).

### Déploiement via Portainer

1. Aller dans **Stacks** → **Add stack**
2. Coller le contenu de `docker-compose.yml` (ou pointer vers le repo Git)
3. Déployer

Le container redémarre automatiquement (`restart: unless-stopped`).

### Volumes montés

| Fichier hôte | Montage conteneur | Description |
|---|---|---|
| `.env` | via `env_file` | Variables d'environnement |
| `google_credentials.json` | `/app/google_credentials.json` | Credentials OAuth Google (lecture seule) |
| `.google_calendar_token.json` | `/app/.google_calendar_token.json` | Token Google Calendar |
| `.ugc_session.json` | `/app/.ugc_session.json` | Session Playwright persistée |
| `planner_config.json` | `/app/planner_config.json` | Configuration du planner (lecture seule) |
| `output/` | `/app/output/` | Sortie ICS + JSON |

### Lancement manuel dans le container

Pour exécuter le script à la demande (hors du cron) :

```bash
docker exec ugc-scraper python src/main.py
```

Ou avec des options :

```bash
docker exec ugc-scraper python src/main.py --seances --output-json /app/output/seances.json
```

## License

This project is licensed under the Apache License 2.0 — see the [LICENSE](LICENSE) file for details.
