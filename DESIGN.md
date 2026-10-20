# sabislabi-x-bot v2 — design technique

Assistant X pour **@sabislabi** (compte international en anglais : utiliser l'IA, actu IA décryptée, business, un peu de dev perso / histoires vraies).

## Principes non négociables (sources officielles, oct. 2026)

1. **API officielle uniquement.** Aucun navigateur sur x.com (docs.x.com/developer-guidelines : « Non-API automation (scraping, browser automation) results in permanent suspension »). Le dossier `auth/` et `storage_state.json` disparaissent.
2. **Un humain valide chaque contenu écrit par Claude** avant publication (mode `APPROVAL_MODE=telegram`, par défaut). Raisons : les réponses générées par IA exigent l'accord préalable de X ; un compte entièrement automatique doit porter l'étiquette « Automated » ; Original Content Rewards exclut le contenu « created or posted using automated means » ; X a supprimé 42 000 comptes de réponses par chatbot en juillet 2026.
3. **Aucune réponse automatique sous les posts des autres, aucun like automatique.** L'API refuse d'ailleurs les réponses non « summoned » depuis le 23/02/2026 et a retiré likes/follows/citations du self-serve le 20/04/2026. Le **radar** propose une réponse rédigée, l'utilisateur la publie lui-même via un lien `x.com/intent/post?in_reply_to=…` qui ouvre l'app X.
4. **Pas de lien dans les posts publiés par l'API** (0,20 $ au lieu de 0,015 $ ; l'algorithme ne pénalise plus les liens, c'est une question de coût). Les noms de domaine (`claude.ai`) comptent comme des liens.
5. **Budget API X plafonné** (jour / mois), chaque appel est compté (`bot/costs.py`).

## Modules

| Fichier | Rôle |
|---|---|
| `bot/utils.py` | chemins, logs, watchdog, heure locale (`now_local`, `set_clock` pour les tests), JSON atomique |
| `bot/config.py` | `Settings` (env + `config/settings.json` + `config/content.json`), `Slot`, `load_settings()`, `validate()` |
| `bot/state.py` | `StateStore` : items, créneaux, posts publiés, compteurs, notes, etc. |
| `bot/costs.py` | `Budget` : enregistrement des coûts API X, plafonds essentiel/optionnel |
| `bot/llm.py` | `LLM.json(label, system, user, schema, effort)` → dict ou None (sorties structurées, cache 1 h, bascule refus) ; helpers `schema()`, `STR`, `BOOL`, `INT`, `STR_LIST`, `INT_LIST` |
| `bot/x_api.py` | `XClient` (OAuth1) : `post`, `post_thread`, `upload_image`, `mentions`, `my_recent_posts`, `user_posts`, `user_ids`, `user_id` ; `XError(kind)` ; `intent_reply_url`, `intent_post_url`, `tweet_url`, `contains_url` |
| `bot/telegram.py` | `Telegram` : `send`, `send_photo`, `edit`, `answer_callback`, `updates` ; `button()`, `esc()` |
| `bot/filters.py` | garde-fous éditoriaux : `publish_problem`, `off_limits_term`, `hard_blocked`, `is_noise`, `is_too_similar`, `clean_output`, `weighted_length`… |
| `bot/models.py` | dataclasses partagées `NewsItem`, `Visual`, `Draft` |
| `bot/sources.py` | `NewsHub.collect()` : flux RSS/Atom + Hacker News + posts des comptes IA officiels (API) → `list[NewsItem]` triée |
| `bot/writer.py` | `Writer` : toutes les consignes Claude et la génération (posts, threads, réactif, réponses, radar, vérifications) |
| `bot/approvals.py` | `Approvals` : cartes Telegram avec boutons, édition, régénération, commandes (/statut, /plan, /pause, /reprendre, /note, /budget, /aide) |
| `bot/publisher.py` | `Publisher` : publie les items validés à l'heure, reprises, erreurs |
| `bot/comments.py` | `CommentDesk.check()` : nouvelles réponses sous mes posts → brouillon de réponse à valider |
| `bot/radar.py` | `Radar.check()` : posts frais des gros comptes → suggestion de réponse à publier soi-même |
| `bot/analytics.py` | `Analytics` : statistiques de mes posts (owned reads), notes de performance pour Claude, récap du matin, bilan hebdo |
| `bot/visuals.py` | captures de vraies pages (Playwright, sites autorisés, jamais x.com) + photo IA optionnelle (OpenAI) |
| `bot/scheduler.py` | `Bot` : assemble tout, `tick()` (un tour de boucle), `run_forever()` |
| `bot/main.py` | point d'entrée : `run`, `--check`, `--once JOB`, `--busy-check` |

## Modèles partagés (`bot/models.py`)

```python
@dataclass
class NewsItem:
    id: str                 # hash stable (url ou id du post)
    title: str
    summary: str            # texte nettoyé, ≤ 600 caractères
    url: str
    source: str             # "OpenAI", "TechCrunch AI", "@claudeai"…
    published: datetime | None
    official: bool = False
    kind: str = "rss"       # rss | hn | x
    engagement: int = 0     # points HN ou likes X
    score: float = 0.0      # calculé par NewsHub (fraîcheur × poids × engagement)

@dataclass
class Visual:
    kind: str               # "screenshot" | "ai"
    target: str             # URL (screenshot) ou description de photo (ai)

@dataclass
class Draft:
    kind: str               # gm | tip | news | thread | reactive
    texts: list[str]        # 1 texte, ou les tweets d'un thread
    visual: Visual | None = None
    poll: dict | None = None        # {"options": [...], "duration_minutes": 1440}
    sources: list[NewsItem] = field(default_factory=list)
    reason: str = ""        # pourquoi ce sujet (affiché sur Telegram)
    angle: str = ""         # genre de thread, angle de réponse…
    from_fallback: bool = False
```

## Item (dans `state.data["items"]`)

Champs créés par `StateStore.new_item(kind, texts, **fields)` : `id, kind, status, texts, image_path, image_kind, poll, publish_at, expires_at, created_at, approved_at, published_ids, tg_message_id, regen_count, attempts, last_error, slot_key, slot_date, meta`.

- `kind` : `gm | tip | news | thread | reactive | comment_reply | radar`
- `status` : `awaiting` (attend ta validation) → `approved` (programmé) → `publishing` → `published` ; ou `rejected | expired | failed` ; pour le radar : `suggested` → `dismissed | expired`.
- `meta` : `reason`, `angle`, `sources` (liste de {title,url,source}), `in_reply_to` (comment_reply), `comment_text`, `commenter`, `post_url`, `intent_url` (radar), `partial_ids` (thread interrompu), `retry_at`.

## Cycle de vie d'un post planifié

1. `Bot.plan_slots()` : pour chaque occurrence de créneau (aujourd'hui et demain) dont l'heure de rédaction (`occurrence - lead`) est passée et qui n'a pas encore d'item (`state.slot_item_id(date, slot.key)`), récupère l'actu (`NewsHub.collect`), les notes perso, les notes de performance, appelle `Writer.draft_post(kind, …)` / `draft_thread(…)` (2 essais, sinon `Writer.fallback_post`), prépare le visuel (`visuals.prepare`), crée l'item (`publish_at = occurrence + jitter aléatoire 0..PUBLISH_JITTER_MINUTES`, `expires_at = occurrence + APPROVAL_GRACE_MINUTES`), marque le créneau et appelle `Approvals.propose(item)`.
2. Telegram : carte avec boutons `✅ Valider` · `✏️ Modifier` · `🔄 Autre version` · `❌ Annuler` (+ `📲 Publier moi-même` = lien intent pour un post simple sans image).
3. `✅` : si `now < publish_at` → `approved` (« programmé à 13h32 ») ; sinon publication immédiate (`publish_at = now`).
4. `Publisher.publish_due()` : items `approved` avec `publish_at <= now` → upload image éventuel → `post` / `post_thread` → `published`, `state.add_published(...)`, carte mise à jour avec le lien.
5. Items `awaiting` dont `expires_at` est passé → `expired` (carte : « ⌛ expiré, rien publié »).
   Un item `approved` qui n'a pas pu partir à l'heure (bot arrêté) n'est plus publié au-delà de 3 h de retard (2 h pour un réactif, 12 h pour une réponse à un commentaire ; jamais pour un thread déjà en partie en ligne) : `expired` + message. `/reprendre` décale les posts validés en retard à `now + 3 min` (le temps d'annuler) et expire ceux qui ont trop attendu.
6. `APPROVAL_MODE=auto` (déconseillé, bloqué sans `AUTOMATED_LABEL_ACK=yes`) : l'item naît `approved`, la carte montre `❌ Annuler` / `✏️ Modifier` jusqu'à l'heure.

## Réponses à mes commentaires (`comments.py`)

Toutes les `COMMENTS_CHECK_EVERY_MINUTES` dans `COMMENTS_WINDOW` : `XClient.mentions(since_id)` (owned read). Garder les réponses à **mes** posts (`in_reply_to_user_id == mon id` ou conversation d'un de mes posts publiés), pas de moi-même, pas déjà traitées, pas blacklistées, pas hors ligne éditoriale. `Writer.draft_comment_reply(...)` → item `comment_reply` (`expires_at` +24 h) → carte `✅ Envoyer` · `✏️ Modifier` · `❌ Ignorer` · `🔗 Voir`. `✅` → `XClient.post(text, reply_to=comment_id)` immédiatement (réponse « summoned », 0,01 $). Si l'API refuse (`not_summoned`) → la carte propose `📲 Répondre moi-même` (lien intent). Plafond `COMMENTS_MAX_DRAFTS_PER_DAY`.

## Radar (`radar.py`)

Toutes les `RADAR_CHECK_EVERY_MINUTES` dans `RADAR_WINDOW`, si le budget optionnel le permet : résout les ids des comptes (`user_ids`, mis en cache dans `state.radar.user_ids`), lit `RADAR_ACCOUNTS_PER_CHECK` comptes en rotation (`user_posts(since_id)`), garde les posts de moins de `RADAR_MAX_AGE_MINUTES` qui montent (`likes/min ≥ RADAR_MIN_LIKES_PER_MIN`), hors bruit / sujets bloqués, un seul par compte et par jour ; `Writer.draft_radar_reply(...)` → item `radar` (`status="suggested"`, `expires_at` +90 min) → carte `📲 Répondre dans X` (lien intent) · `🔄 Autre réponse` · `❌ Passer` · `🔗 Voir le post`. **Jamais de publication par l'API.** Plafond `RADAR_MAX_PER_DAY`.

## Posts réactifs

Toutes les `REACTIVE_CHECK_EVERY_MINUTES` dans `REACTIVE_WINDOW`, hors ±`REACTIVE_MIN_GAP_MINUTES` autour d'un créneau, sous `REACTIVE_MAX_PER_DAY` : `Writer.draft_reactive(news)` (porte stricte, la plupart du temps « non »). Si oui : item `reactive`, `publish_at = now`, `expires_at = now + 180 min`.

## Analytique (`analytics.py`)

- `refresh_metrics()` toutes les 3 h : `my_recent_posts()` → `public_metrics` (impressions, likes, réponses, reposts, signets) rangées dans `state.published[*].metrics` ; agrégats par type et par créneau dans `state.data["performance"]`.
- `performance_notes()` → 3-6 lignes pour Claude (« les tutos avec un prompt à copier font 2,1× plus de signets que l'actu… »).
- `daily_report()` (à `HEALTH_REPORT_AT`) et `weekly_report()` (à `WEEKLY_REPORT_AT`) → texte Telegram en français.

## Coûts estimés

API X : ~4 posts/jour + 2 threads/semaine ≈ 3 $/mois ; réponses aux commentaires validées 0,01 $ pièce ; lectures radar/actu (optionnelles) plafonnées à 75 % de `X_BUDGET_MONTHLY_USD`, étalées jour par jour (au plus `min(80 % du plafond du jour, 75 % du mois / nb de jours)`) : le dernier quart du mois reste toujours disponible pour publier. Claude : ~30-60 appels/jour, consignes en cache.

## Tests

`pytest` sans réseau : faux `LLM`, faux `XClient`, faux `Telegram`, horloge figée (`utils.set_clock`). `deploy/deploy.sh` lance la suite sur une copie isolée avant toute installation.

## Installation (`deploy/`)

- `push_from_mac.sh` (sur le Mac) : envoie le code (tar via ssh, jamais `config/.env`) dans `~/xbv2_stage`, propose `set_secrets.sh` (`ssh -t`, saisie masquée, écriture atomique en 600), puis lance `deploy.sh`.
- `deploy.sh` (sur le serveur) : verrou ; refus si publication en cours (`--busy-check`, ou horaires fixes de la v1) ; `pip install` + `pytest` sur la nouvelle version ; arrêt du service ; sauvegarde datée (`backup_deploy/<date>/` + manifeste `.xbot_backup`, 5 gardées) ; installation de `bot/ deploy/ tests/` et des JSON ; `migrate_env_v2.sh` (ajoute les réglages v2 absents, n'écrase rien) ; `--check` réel ; redémarrage ; tout échec après l'arrêt → `restore_backup.sh` + relance de l'ancienne version.
- `restore_backup.sh` : remet une sauvegarde (code + JSON ; l'état seulement s'il change de format ; `.env` seulement pour annuler un déploiement raté).
- `cleanup_v1.sh` : une fois la v2 en service, supprime `auth/` (session X du navigateur), les copies de l'ancien `.env`, les restes v1 et les clés SSH privées égarées dans le dossier.
- Points d'entrée de test : `XBOT_APP_DIR`, `XBOT_SERVICE=""`, `XBOT_SUDO`, `XBOT_SKIP_PIP|TESTS|CHECK`, `XBOT_CLOCK`, `XBOT_SSH`, `XBOT_HOST`, `XBOT_ASK_SECRETS`.
