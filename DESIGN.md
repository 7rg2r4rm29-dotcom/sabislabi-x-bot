# sabislabi-x-bot v2 — design technique

Assistant X pour **@sabislabi** (compte international en anglais). Ligne éditoriale depuis novembre 2026 : **« le futur arrive plus vite qu'on ne le croit »**, raconté avec des mots simples au grand public — et chaque post part avec une capture d'écran de l'article, comme prise au téléphone.

## Ligne éditoriale et images (novembre 2026)

- **Public** : une infirmière, un chauffeur de taxi, un étudiant qui joue, un parent, un retraité. Ils ne suivent pas la tech ; ils s'arrêtent pour ce qui fait dire « attends, quoi ? » : de la science-fiction devenue réelle, un geste de tous les jours qui va changer, quelque chose d'un peu inquiétant. Consignes : `AUDIENCE`, `INTRIGUE`, `PLAYBOOK` (formes d'accroche), `TONE` (`bot/writer.py`).
- **Intriguer sans inventer** (`TRUTH`, `GROUNDING`) : le cadrage (« incroyable », « terrifiant ») est une opinion permise ; les faits viennent tous des articles cités. Une démo reste une démo, le futur se dit « bientôt / pourrait », pas de date, de prix ni de catastrophe absents de la source. La relecture (`FACTCHECK_PROMPT`) recale l'exagération et la certitude, pas l'émotion.
- **Zéro jargon, trois barrières** : les consignes (`PLAIN_WORDS`) ; un filtre côté code (`filters.find_tech_jargon` : LLM, API, benchmark, open-source… → motif « trop technique (mot) » et nouvel essai) ; la relecture par Claude, qui rend deux verdicts dans le même appel (`ok`/`issues` pour les faits, `plain`/`hard_words` pour la lisibilité). Au second essai, un texte dont les faits sont justes mais encore jugé « un peu technique » part quand même.
- **Rédaction en deux temps** :
  1. *La rédaction en chef* — `Writer._pick(brief, pool, n)` : Claude lit jusqu'à `MAX_ITEMS_PICK` (60) titres (`_headlines_block` : source · âge — titre — début du résumé), les consignes `EDITOR_PROMPT` + `PICKS` et le brief du post (`PICK_BRIEFS` : `news`, `tip`, `reactive`, `thread-timely|future|weekly`), et rend `picks` (numéros, le meilleur d'abord) ; liste vide = rien ne mérite un post, le créneau est sauté. Effort `low`.
  2. *La lecture* — `Writer._read_articles` : le texte des articles retenus (`sources.fetch_article_text`, ≤ 8 000 caractères, en parallèle, 45 s au plus en tout, jamais x.com) est rangé dans `NewsItem.body`. Un article illisible garde son résumé de flux.
  3. *L'écriture* — `Writer._write_post` / `_thread_candidate` : Claude écrit d'abord cinq premières lignes possibles (`hook_options`), garde la meilleure, écrit à partir de ces seules histoires (article montré jusqu'à `ARTICLE_SHOWN`), cite ses sources (`source_ids`, obligatoires), désigne la page à capturer (`visual_url` = `"[n]"`) et nomme sa forme d'accroche (`hook`, normalisée par `hook_shape` parmi `HOOK_SHAPES`, rangée dans `Draft.hook` puis dans les statistiques).
  4. *Les garde-fous du code* — `filters.publish_problem` (liens, jargon, formules toutes faites `find_hype`, longueur), `ungrounded_number` (un chiffre absent des histoires citées est refusé), `_echoes_recent` (pas les mêmes trois premiers mots ni la même dernière ligne qu'un post récent), `is_too_similar`.
  5. *La relecture* — `Writer._factcheck` : Claude liste chaque affirmation avec la phrase de l'article qui la soutient (`claims`) ; une seule non soutenue suffit à refuser. Un refus déclenche un second essai avec le motif (« reconstruire le texte, ne pas coller la correction en dernière ligne »), puis l'abandon.
- **Tri de l'actu avant le choix** : `sources.audience_factor` multiplie le score par 1,6 pour ce qui peut intriguer tout le monde (robots, jeux vidéo, premières mondiales, noms connus…), par 0,45 pour les sujets d'initiés (API, levées de fonds, puces…) et par 0,3 pour le commerce. Les flux généralistes sont filtrés (`"filter": "tech"` ou `"ai"`). Sujets militaires et titres de bons plans / codes promo / tests de produits ne sont jamais montrés. `top_for` plafonne chaque source.
- **La capture** (`bot/visuals.py`) : la page de l'actu citée est chargée comme sur un iPhone (390 × 844 px CSS, ×3 → 1170 px de large, agent mobile), nettoyée (`_PREPARE_JS` : éléments fixes ou collants, bandeaux de consentement, publicités, menus), stabilisée (`_SETTLE_JS` : images et polices chargées), cadrée sur le titre (`_ALIGN_JS` : ligne de rubrique au-dessus du titre, grande image d'ouverture gardée si elle précède le titre), coupée en bas entre deux lignes de texte (`_FRAME_JS`, 4:5 au plus, jamais un liseré d'image), la bande unie du bas retirée (`trim_flat_bottom`), enregistrée en JPEG. `title_on_page` vérifie que la page est bien celle de l'actu citée, puis Claude la REGARDE (`Writer.screenshot_is_clean(path, headline, post)` : propre, bon article, rien qui contredise le post). Jusqu'à `MAX_PAGES_TRIED` pages citées sont essayées ; sinon le post part en texte seul. Aucune maquette, aucune carte, rien d'ajouté. Jamais de capture de X.
- **Rythme** : `POST_SLOTS=tip@14:30,news@19:00` (`tip` = « le futur arrive », `news` = « l'histoire du jour »), `THREAD_SLOTS=tue@14:30,sun@16:00` (genres `timely | future | weekly`), « ça vient de tomber » 1/jour au plus entre 12 h et 23 h 30. Sans histoire assez forte, un créneau est sauté.

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
| `bot/filters.py` | garde-fous éditoriaux : `publish_problem`, `off_limits_term`, `find_tech_jargon`, `hard_blocked`, `is_noise`, `is_too_similar`, `clean_output`, `weighted_length`… |
| `bot/models.py` | dataclasses partagées `NewsItem`, `Visual`, `Draft` |
| `bot/sources.py` | `NewsHub.collect()` : flux RSS/Atom (+ Hacker News si configuré) + posts des comptes officiels (API) → `list[NewsItem]` triée ; `audience_factor` (grand public / initiés / commerce), `topic_filter`, `top_for` ; lecteur d'articles `article_text` / `fetch_article_text` |
| `bot/writer.py` | `Writer` : toutes les consignes Claude et la génération en deux temps (choix de l'histoire, lecture, rédaction, relecture) pour les posts, threads et actus à chaud ; réponses, radar, contrôle visuel des captures |
| `bot/approvals.py` | `Approvals` : cartes Telegram avec boutons, édition, régénération, commandes (/statut, /plan, /pause, /reprendre, /note, /budget, /aide) |
| `bot/publisher.py` | `Publisher` : publie les items validés à l'heure, reprises, erreurs |
| `bot/comments.py` | `CommentDesk.check()` : nouvelles réponses sous mes posts → brouillon de réponse à valider |
| `bot/radar.py` | `Radar.check()` : posts frais des gros comptes → suggestion de réponse à publier soi-même |
| `bot/analytics.py` | `Analytics` : statistiques de mes posts (owned reads), notes de performance pour Claude, récap du matin, bilan hebdo |
| `bot/visuals.py` | `prepare(settings, draft, writer, tag)` → `(chemin, "shot")` : capture, façon téléphone, de la page de l'actu citée (jamais x.com), ou `(None, None)` ; `self_test()` pour `--check` ; photo IA optionnelle (OpenAI) |
| `bot/scheduler.py` | `Bot` : assemble tout, `tick()` (un tour de boucle), `run_forever()` |
| `bot/main.py` | point d'entrée : `run`, `--check` (prend aussi une capture d'essai), `--preview [TYPES]` (vrais brouillons + images envoyés sur Telegram, rien n'est publié, état copié), `--report` (posts, chiffres, abonnés, coûts, erreurs), `--once JOB`, `--busy-check` |

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
    score: float = 0.0      # calculé par NewsHub (fraîcheur × poids × engagement × public)
    body: str = ""          # texte de l'article, lu seulement pour les histoires retenues

@dataclass
class Visual:
    kind: str               # "shot" (capture de la page de l'actu, façon téléphone) | "ai"
    target: str             # URL de la page (shot) ou description de photo (ai)

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
    image_path: str | None = None   # renseignés par le planificateur (🔄 : l'image suit le texte)
    image_kind: str | None = None   # "shot" | "ai"
```

## Item (dans `state.data["items"]`)

Champs créés par `StateStore.new_item(kind, texts, **fields)` : `id, kind, status, texts, image_path, image_kind, poll, publish_at, expires_at, created_at, approved_at, published_ids, tg_message_id, regen_count, attempts, last_error, slot_key, slot_date, meta`.

- `kind` : `gm | tip | news | thread | reactive | comment_reply | radar`
- `status` : `awaiting` (attend ta validation) → `approved` (programmé) → `publishing` → `published` ; ou `rejected | expired | failed` ; pour le radar : `suggested` → `dismissed | expired`.
- `meta` : `reason`, `angle`, `sources` (liste de {title,url,source}), `in_reply_to` (comment_reply), `comment_text`, `commenter`, `post_url`, `intent_url` (radar), `partial_ids` (thread interrompu), `retry_at`.
- `image_kind` : `shot` (capture d'écran de la page de l'actu) | `ai`.

## Cycle de vie d'un post planifié

1. `Bot.plan_slots()` : pour chaque occurrence de créneau (aujourd'hui et demain) dont l'heure de rédaction (`occurrence - lead`) est passée et qui n'a pas encore d'item (`state.slot_item_id(date, slot.key)`), récupère l'actu (`NewsHub.collect`), les notes perso, les notes de performance, appelle `Writer.draft_post(kind, …)` / `draft_thread(…)` (choix de l'histoire, lecture, 2 essais de rédaction ; sans histoire assez forte le créneau est sauté — seul l'ancien type `gm` a encore un texte de secours, en mode Telegram), prépare le visuel (`visuals.prepare`), crée l'item (`publish_at = occurrence + jitter aléatoire 0..PUBLISH_JITTER_MINUTES`, `expires_at = occurrence + APPROVAL_GRACE_MINUTES`), marque le créneau et appelle `Approvals.propose(item)`.
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

Toutes les `REACTIVE_CHECK_EVERY_MINUTES` dans `REACTIVE_WINDOW`, hors ±`REACTIVE_MIN_GAP_MINUTES` autour d'un créneau, sous `REACTIVE_MAX_PER_DAY` (1 par défaut), et seulement s'il y a une actu fraîche (< 12 h) que la rédaction n'a pas encore examinée et qui peut valoir un post immédiat — officielle, qui décolle, ou de nature à intriguer tout le monde (`Scheduler._appeal`), jamais réservée aux initiés (`REACTIVE_MAX_CHECKS_PER_DAY` appels par jour au plus) : `Writer.draft_reactive(news)` = la rédaction en chef (`_pick("reactive", …, 1)`, « la plupart du temps, la bonne réponse est une liste vide »), puis le même chemin d'écriture qu'un post. Si oui : item `reactive`, `publish_at = now`, `expires_at = now + 180 min`.

## Analytique (`analytics.py`)

- `refresh_metrics()` toutes les 3 h : `my_recent_posts()` → `public_metrics` (impressions, likes, réponses, reposts, signets) rangées dans `state.published[*].metrics` ; agrégats par type et par créneau dans `state.data["performance"]`.
- `performance_notes()` → 3-6 lignes pour Claude (meilleurs types et créneaux, posts avec ou sans capture d'écran…).
- `daily_report()` (à `HEALTH_REPORT_AT`) et `weekly_report()` (à `WEEKLY_REPORT_AT`) → texte Telegram en français.

## Coûts estimés

API X : 2 posts/jour + 2 threads/semaine ≈ 2 $/mois ; réponses aux commentaires validées 0,01 $ pièce ; lectures radar/actu (optionnelles) plafonnées à 75 % de `X_BUDGET_MONTHLY_USD`, étalées jour par jour (au plus `min(80 % du plafond du jour, 75 % du mois / nb de jours)`) : le dernier quart du mois reste toujours disponible pour publier. Claude : ~15-40 appels/jour (chaque post = un choix d'histoire, une rédaction, une relecture, un regard sur la capture ; les vérifications « à chaud » ne coûtent que le choix), soit de l'ordre de 25 $/mois avec `claude-opus-5-5` (4 $ / 20 $ par million de tokens), environ moitié moins avec `claude-sonnet-5-5`.

## Tests

`pytest` sans réseau (`tests/conftest.py` fait échouer tout accès à `sources.default_http_get`) : faux `LLM`, faux `XClient`, faux `Telegram`, faux lecteur d'articles (`Writer(read_article=…)`), horloge figée (`utils.set_clock`). `deploy/deploy.sh` lance la suite sur une copie isolée avant toute installation. Les tests qui lancent un vrai navigateur (sur une page locale) ne tournent que sur demande : `XBOT_REAL_BROWSER=1` ; sur le serveur, c'est `--check` qui prend une capture d'essai, sans jamais bloquer une installation. `XBOT_BROWSER_PATH` : navigateur préinstallé ; `XBOT_BROWSER_PROXY` : proxy du navigateur, pour les environnements de test.

## Installation (`deploy/`)

- `push_from_mac.sh` (sur le Mac, bash 3.2) : assistant guidé. Envoie le code (tar via ssh, jamais `config/.env`) dans un dossier unique `~/xbv2_stage_<date>` ; lance `precheck.sh` et ne demande que les clés qui ne marchent pas (ouvre la bonne page, `set_secrets.sh CLE…` via `ssh -t`, `telegram_setup.sh` pour le jeton et le chat privé) ; propose la rotation des clés ; lance `deploy.sh` détaché (`nohup setsid`, journal `~/xbv2_deploy_<date>.log`) et le suit avec `follow.sh` : une coupure ssh n'interrompt pas l'installation.
- `precheck.sh` (serveur) : instantané `pip freeze`, mise à jour des bibliothèques, puis `bot.main --check` de la NOUVELLE version avec le `.env` installé (`XBOT_ENV_FILE`) ; dernière ligne `PARTS telegram=… x=… claude=… images=…` (ok | missing | bad | group | auth | credits | forbidden | wrong_account | error ; `images=bad` n'est qu'un avertissement : le bot publierait sans image).
- `deploy.sh` (serveur) : verrou ; refus si publication en cours (`--busy-check`, ou horaires fixes de la v1) ; bibliothèques + `pytest` sur la nouvelle version ; arrêt du service ; sauvegarde datée (`backup_deploy/<date>/` + manifeste `.xbot_backup`, 5 gardées) ; installation de `bot/ deploy/ tests/` ; `settings.json`/`content.json` remplacés seulement s'ils n'ont pas été modifiés (copie livrée dans `config/.shipped/`, sinon `….nouveau`) ; `migrate_env_v2.sh` ; `--check` réel ; redémarrage. Tout échec ou interruption (trap INT/TERM/HUP) après l'arrêt → `restore_backup.sh`, bibliothèques remises (`pip freeze`), relance, message Telegram ; une restauration incomplète arrête le bot et le dit. Dernière ligne `FIN_DEPLOIEMENT <code>`.
- `restore_backup.sh` : remet une sauvegarde (code + JSON ; l'état seulement s'il change de format ; `.env` seulement pour annuler un déploiement raté) ; continue malgré une erreur et la signale ; remettre la v1 (navigateur) à la main exige de taper `OUI`.
- `cleanup_v1.sh` : une fois la v2 en service, supprime `auth/` (session X du navigateur), les copies de l'ancien `.env`, les restes v1 et les clés SSH privées égarées dans le dossier.
- Points d'entrée de test : `XBOT_APP_DIR`, `XBOT_SERVICE=""`, `XBOT_SUDO`, `XBOT_SKIP_PIP|TESTS|CHECK`, `XBOT_CLOCK`, `XBOT_SSH`, `XBOT_HOST`, `XBOT_OPEN`, `XBOT_NO_TTY`, `XBOT_NO_TELEGRAM`, `XBOT_TELEGRAM_API`.
