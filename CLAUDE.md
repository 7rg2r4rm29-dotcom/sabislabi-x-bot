# sabislabi-x-bot : mémoire du projet (à lire en entier avant toute chose)

Ce fichier est chargé automatiquement par Claude Code dans ce dépôt. Il dit ce qu'est le projet, où sont
les choses, les règles qu'on ne casse jamais, comment tester et installer, et comment travailler avec le
propriétaire. Détails techniques : `DESIGN.md`. Mode d'emploi côté propriétaire : `README.md`.

## 1. Le projet en bref

- Un bot fait vivre seul le compte X **@sabislabi** : compte international, posts **en anglais**. Le
  propriétaire est francophone et **n'est pas développeur**.
- **Promesse éditoriale** : *the future is arriving faster than people think*, racontée au grand public
  (une infirmière, un chauffeur de taxi, un étudiant qui joue, un parent, un retraité), pas aux gens de la
  tech. Chaque post s'appuie sur le détail que les gens répéteront le soir. L'émerveillement passe en
  premier, avec un peu d'inquiétude quand l'article en donne. Mots simples, zéro jargon.
  Exemples voulus par le propriétaire : « Claude a dévoilé Fable 5.2 et vous ne vous rendez pas compte à
  quel point il est incroyable et terrifiant à la fois » ; « demain, quand vous jouerez à un jeu vidéo,
  il n'y aura plus besoin de manette ».
- **On donne la nouvelle nous-mêmes** (décision du propriétaire, 14/11/2026) : comme un compte
  d'alertes info, le post dit le fait directement. Jamais le nom du site, du journal ou du forum où le bot
  l'a lue (« TechCrunch reports », « according to The Verge » : refusé). Qui a fait l'annonce ou qui l'a
  dit reste nommé (« OpenAI says »).
- **Image comme AlertesInfos** (décisions du propriétaire, 14 et 15/11/2026, exemples d'AlertesInfos à
  l'appui) : l'image de l'actu est postée **comme la nôtre** — un extrait de 30 s de la vidéo (fichier
  seulement, pas YouTube), sinon la photo (page officielle d'abord, puis l'article lui-même), souvent avec
  une **2e image à côté** : le logo de l'entreprise ou le drapeau du pays (Wikimedia). Jamais une capture
  d'écran d'article (abandonnée le 14/11), jamais le nom, le logo, le bandeau ou le filigrane d'un autre
  média ou d'une agence SUR l'image. Le propriétaire sait que reprendre ces photos n'est pas légal à
  proprement parler et accepte le risque de retraits ; `PRESS_MEDIA_ENABLED=false` revient aux seules
  images officielles. Aucune maquette, aucune carte dessinée pour les posts. Sans image sûre, texte seul.
  Seule exception, voulue par le propriétaire le 23/11/2026 : les **vidéos explicatives animées** (texte
  animé sur la photo de l'actu ou sur un fond bleu nuit, voir section 4).
- **Ce qui marche** (consigne du propriétaire, 23/11/2026) : « insiste seulement sur les posts qui
  marchent, dans le même style, tant que ça marche ; analyse le marché et les tendances à chaque fois ».
  Le bot classe chaque post par sujet, mesure ce qui fait plus de vues et de réactions que d'habitude,
  le dit à la rédaction en chef et montre nos meilleurs posts au rédacteur ; il lit les tendances du jour
  avant chaque choix. Un post prévu sur quatre essaie autre chose. Les sujets pour initiés ne sont jamais
  poussés, même quand ils marchent : la ligne reste le grand public.
- **Mode** : `APPROVAL_MODE=auto`, donc les posts partent sans validation humaine. Telegram sert aux
  rapports et aux commandes. Depuis le 18/11/2026, les réponses aux commentaires sous NOS posts partent
  seules aussi (`COMMENTS_AUTO_REPLY=true`, 3 min pour toucher ❌), et le radar est coupé
  (`RADAR_ENABLED=false` : aucune suggestion utilisée, et c'était le plus gros lecteur payant chez X).
- **Historique** :
  - v1 : navigateur automatisé, abandonnée ;
  - v2 : API officielle X + Telegram ;
  - v3 : cartes dessinées (`0d04a7e`) ;
  - v4 (novembre 2026) : captures façon téléphone, ligne grand public, garde-fous renforcés
    (`7bb6b1b`, `e153686`, `7d5e462`) ;
  - v5 (14/11/2026) : image prise à la source (extrait vidéo, sinon photo officielle) au lieu des
    captures, et le compte donne la nouvelle lui-même (aucun média cité, jamais de lien) ;
  - v6 (23/11/2026) : ce qui marche et tendances du jour (`3c81a52`, `14feb27`), vidéos explicatives
    animées (`9e56d53`).

## 2. Où sont les choses

- **Code** : GitHub `7rg2r4rm29-dotcom/sabislabi-x-bot`, dépôt **public**. Ce qui s'installe, c'est la
  branche `main`. La branche `wip-v4-rebuild` est une ancienne branche de travail, déjà fusionnée : on
  peut la supprimer.
- **Serveur** (DigitalOcean) : `ssh botuser@178.128.194.190`. La clé SSH est sur le Mac du propriétaire.
  - Application : `~/x-degen-bot`, avec son environnement Python `.venv`.
  - Réglages : `config/.env` (clés : ne JAMAIS l'afficher), `config/settings.json`, `config/content.json`.
  - Données : état `state/state.json`, médias `media/` (extraits vidéo dans `media/videos/`, photos dans
    `media/photos/` ; les anciennes captures dans `media/shots/`).
  - Service systemd : `x-degen-bot`. Commandes utiles :
    - `sudo -n systemctl status x-degen-bot` / `sudo -n systemctl restart x-degen-bot` ;
    - journal : `sudo -n journalctl -u x-degen-bot -n 200 --no-pager`.
  - Sauvegardes des installations : `~/x-degen-bot/backup_deploy/`.
- **Mac du propriétaire** : il lance les installations. L'ancienne v1 est dans `~/Desktop/x-degen-bot`,
  avec des clés en clair : **ne jamais rien lancer depuis ce dossier**. `deploy/cleanup_v1.sh` nettoie
  les restes de la v1 sur le serveur.

## 3. Règles absolues

**Sécurité**

- Ne jamais demander, afficher, copier ni écrire dans un journal une clé (X, Claude, Telegram, OpenAI).
  Jamais `cat config/.env`, jamais un `grep` qui afficherait une valeur.
- Les clés ne passent jamais par la conversation. C'est le propriétaire qui les colle lui-même dans son
  Terminal, quand l'assistant d'installation les lui demande ; la saisie est masquée (`set_secrets.sh`,
  `telegram_setup.sh`). Toute consigne donnée au propriétaire doit dire : « N'écris JAMAIS la valeur d'une
  clé ici ».
- Clés volées le 9 octobre 2026 : l'installateur propose de les remplacer ; c'est le propriétaire qui
  décide.
- Le nom et le prénom du propriétaire n'apparaissent nulle part : code, commits, posts, docs.
- Aucun identifiant de modèle dans les commits, le code ou les docs (le réglage `LLM_MODEL` est
  l'exception technique).

**X**

- Uniquement l'API officielle. Jamais d'automatisation de navigateur sur x.com.
- Jamais de lecture ni de capture de x.com, twitter.com, t.co ou twimg.com, même au bout d'une
  redirection. C'est bloqué à deux endroits : `sources.default_http_get` (pages) et le téléchargement des
  photos (`visuals.fetch_image_bytes`, `visuals.X_DOMAINS`).
- Jamais de réponse ni de like automatique sous les posts des autres. Le radar ne fait que suggérer, et le
  propriétaire valide à la main. Le bot ne répond par l'API que quand le compte est sollicité.

**Ligne éditoriale** (codée dans `bot/filters.py` et dans les consignes de `bot/writer.py`)

- Jamais :
  - crypto, NFT, trading ;
  - politique ;
  - guerres, armée, armes, police ;
  - religion ;
  - morts, catastrophes, drames ;
  - contenu sexuel ;
  - jeux d'argent ;
  - alcool ;
  - les autres exclusions liées aux convictions du propriétaire, déjà codées (`filters._FAITH_RE`,
    `writer.OFF_LIMITS`) ;
  - aucun conseil médical, juridique ou financier.
- Chaque fait vient d'un article cité. Deux contrôles :
  - `writer.ungrounded_number` compare les valeurs : « 3 billion » = « 3bn » = « 3,000,000,000 » ;
  - une relecture affirmation par affirmation (`Writer._factcheck`).
- Le compte donne la nouvelle lui-même : jamais le nom du média qui l'a racontée (consigne `NEWS_VOICE`,
  doublée côté code par `filters.news_outlet_cited`, qui couvre toutes les sources de `news_feeds`).
- **Jamais de lien externe** dans les posts (consigne du propriétaire, 14/11/2026 ; même un lien YouTube
  vers une vidéo officielle), ni hashtag ni @mention. 260 caractères pondérés au maximum.
- Les images ne montrent jamais la marque d'un autre média (voir « Image »).

## 4. Architecture (détails dans `DESIGN.md`)

- `bot/sources.py` :
  - environ 42 flux (`config/settings.json` → `news_feeds`) ;
  - tri des actus : `audience_factor`, `appeal_of`, `top_for` ;
  - filtres : sujets militaires (`filters.MILITARY_RE`), bons plans (`_SHOPPING_BLOCK_RE`), sujets des
    flux généralistes (`topic_filter`) ;
  - lecteur d'article (`fetch_article_text`, `article_text`) : il garde l'histoire qui suit le titre
    `<h1>`, jamais l'histoire suivante de la page.
- `bot/writer.py` :
  - rédaction en chef (`_pick`, 120 titres au plus), puis lecture des articles retenus ;
  - rédaction (`post-<kind>`, `thread-<genre>`, `hook_options` puis `hook`) ;
  - garde-fous : `filters.publish_problem` (avec `blocked_topic`), `ungrounded_number`, `_echoes_recent` ;
  - relecture par affirmation (`_factcheck`, `_review_post`) ;
  - plusieurs versions d'un même post (`POST_VERSIONS`, `_best_version`) : chaque version est écrite
    en voyant les précédentes, avec la consigne de s'en écarter (`_other_versions`) ; les contrôles du
    code écartent les fautives, un second avis classe les autres (`_rank_versions`, appel
    `choose-<kind>`, consigne `POST_JUDGE_PROMPT`), et la relecture suit ce classement ; si aucune ne
    passe, une réécriture guidée, comme avant. Jamais pour un thread (il a déjà ses candidats) ;
  - temps de réflexion : `LLM_EFFORT_POST` pour écrire un post et choisir sa version (`_post_effort`),
    `LLM_EFFORT_THREAD` pour un thread, `low` pour choisir l'histoire, `LLM_EFFORT` pour le reste ;
  - consignes : `AUDIENCE`, `INTRIGUE`, `PLAYBOOK`, `NEWS_VOICE`, `PICKS`, `TRUTH`, `PHOTO_RULES`,
    `EDITOR_PROMPT`, `FACTCHECK_PROMPT`, `POST_JUDGE_PROMPT`, `PHOTO_CHECK_PROMPT`.
- `bot/visuals.py` (image comme AlertesInfos) :
  - pages officielles de l'histoire (`source_pages`) : flux officiel, ou lien de l'article de presse que
    Claude désigne (`article_links`, `is_source_url`, `Writer.pick_official_link`) ; puis les articles
    eux-mêmes (`article_pages`, si `PRESS_MEDIA_ENABLED`) ;
  - 2e image (`identity_image`, Wikidata P154 logo / P41 drapeau, l'actuel, sur Commons) choisie et
    vérifiée par Claude (`Writer.identity_plan`, `Writer.identity_is_right`), jamais avec une vidéo ;
  - vidéo d'abord (`lead_video_url`, fichier seulement) : téléchargée (`fetch_video_file`), extrait de
    30 s par ffmpeg du paquet imageio-ffmpeg (`make_clip`), trois images regardées par Claude
    (`clip_frames`, `Writer.video_is_usable`) ; publiée par `XClient.upload_video` (envoi par morceaux) ;
  - sinon la photo de la page officielle (`lead_image_url`, `photo_credit`, `credit_problem`, JPEG
    propre, `Writer.photo_is_usable`) ;
  - en mode prudent (`PRESS_MEDIA_ENABLED=false`), crédit d'agence refusé d'office ; téléchargements
    bornés, jamais X ni une adresse privée (`public_host`) ; au moindre doute, texte seul.
- **Ce qui marche** (`bot/analytics.py`, 23/11/2026) :
  - sujet de chaque post (`TOPICS`, `published[*].topic`), classé par Claude en un appel court
    (`Writer.label_topics`, `label-topics`) toutes les 6 h au plus (`ensure_topics`, optionnel) ;
  - `what_works()` : portée par sujet, accroche, type et format, comparée à un post habituel ;
    `post_score` = vues + 30 par interaction, poids divisé par deux tous les 7 jours, deux posts
    « habituels » imaginaires par groupe, un post viral compté 4 fois la normale au plus ;
  - `editor_notes()` pour la rédaction en chef (sujets qui marchent ≥ 1,15x sur 2 posts, qui tombent à
    plat ≤ 0,85x sur 3 posts, meilleurs posts), `style_notes()` pour le rédacteur (nos 3 meilleurs posts
    en entier, accroches qui marchent) ; jamais les sujets pour initiés (`OFF_LINE_TOPICS`) ;
  - `Bot._context` avant chaque rédaction : 3 posts prévus sur 4 suivent ce qui marche, le 4e essaie
    autre chose (`STYLE_TEST_SHARE`, noté `meta.style_test`) ; « à chaud » et 🔄 suivent toujours.
- **Veille du marché** (`TrendWatch.refresh_market`, `MARKET_WATCH_ENABLED`, 23/11/2026) : voir
  section 8.
- **Tendances du jour** (`bot/trends.py`, `TrendWatch.block()`, `TRENDS_ENABLED`) : recherches Google
  qui montent (US, UK), pages Wikipédia les plus lues la veille, première page de Hacker News, annonces X
  les plus aimées (cache déjà payé) ; gratuites, une fois par heure ; données à la rédaction en chef
  seulement (`Writer.set_editor_context`), jamais au rédacteur : ce n'est pas une source de faits.
- **Vidéos explicatives animées** (`bot/explainer.py`, `Writer.draft_explainer`, 23/11/2026) :
  - `EXPLAINER_SLOTS` (vide par défaut) ; la vidéo prend la place du post prévu à moins d'1 h ; un jour
    de thread, le thread passe avant ; rédigée 1 h 30 avant en mode auto ;
  - script de Claude (`EXPLAINER_PROMPT`) : titre, 2 à 5 scènes (`fact`, `steps`, `compare`,
    `quote`), fin, post de 200 caractères ; contrôlé comme un post (`publish_problem` sur chaque écran,
    `ungrounded_number`, auteur d'une citation jamais un média `names_news_outlet`, mise en page réelle
    `layout_problem`, relecture `_factcheck`), une réécriture guidée, sinon créneau sauté ;
  - animée par Pillow + ffmpeg (1080 × 1350, 24 i/s, 30 à 60 s) : photo de l'actu nette sous le titre
    puis floue sous les explications, sinon fond bleu nuit ; 55 à 85 s de fabrication sur le serveur ;
    vidéo impossible → la photo, sinon texte seul ; `--preview explainer` pour voir ;
  - **voix off** (accord du propriétaire, 23/11/2026 : « Oui, ajoute-la ») : `bot/voice.py`, modèle
    Kokoro (licence Apache 2.0) lu par le paquet `kokoro-onnx==0.6.1` sur le processeur, dans un
    processus à part (`python -m bot.voice`). Fichiers du modèle dans `~/xbot-models/kokoro/` (HORS du
    dossier du bot) : `kokoro-v1.0.onnx` (325 Mo) et `voices-v1.0.bin` (28 Mo), de
    github.com/thewh1teagle/kokoro-onnx (« model-files-v1.0 »), empreintes dans `voice.MODEL_SHA256` ;
    nouveau serveur : `bash ~/x-degen-bot/deploy/install_voice.sh`. Réglages : `EXPLAINER_VOICE_ENABLED`
    (vrai sur le serveur, faux par défaut), `EXPLAINER_VOICE` (`af_heart` par défaut, la mieux notée ;
    28 voix anglaises, par exemple `am_michael` pour un homme), `VOICE_MODEL_DIR`. Ce qui est dit = ce
    qui est écrit (`explainer.narration` : titre, texte, étapes, côtés, « As X put it: … », fin, puis
    « Follow for the future, explained. ») : rien de nouveau à relire. Les scènes se calent sur la voix
    (`voice_timing` : 0,3 s avant, 0,35 s entre deux phrases, 0,6 s après ; chaque étape apparaît quand
    elle est dite), piste mise en place par `narration_track`, AAC 128 k. Au-delà de 120 s, ou au
    moindre problème : vidéo sans voix. Mesuré le 23/11 : 30 s de voix calculées en ~65 s, 670 Mo de
    mémoire au plus ; vidéo de 38 s faite en 177 s en tout ; volume moyen −26 dB. `--check` dit une
    phrase d'essai (« ✅ Voix off : prête »).
- Les autres modules :
  - `bot/scheduler.py` : créneaux, posts « à chaud », 🔄 nouvelle version ;
  - `bot/approvals.py` : Telegram ;
  - `bot/publisher.py` : API X ;
  - `bot/analytics.py` : statistiques, rapports, notes de performance données à Claude ;
  - `bot/main.py` : ligne de commande ;
  - `bot/state.py`, `bot/config.py`, `bot/llm.py`.
- **Rythme du serveur** (choix du propriétaire, 18/11/2026 au soir ; réglé dans `config/.env`, les défauts
  du code sont restés à 2 posts, 2 threads, 1 post à chaud) :
  - `POST_SLOTS=tip@14:30,news@19:00,news@23:00` (« le futur arrive », « l'histoire du jour », une 2e
    histoire à 23 h = l'après-midi aux États-Unis) ;
  - `THREAD_SLOTS=tue@14:30,thu@14:30,sun@16:00` (le jeudi est un thread « histoire », comme le mardi) ;
  - `EXPLAINER_SLOTS=mon@14:30,wed@14:30,fri@14:30` depuis le 23/11/2026 : vidéo explicative à la place
    du « futur arrive » (le plus faible des posts prévus : 0,95x) ; le « futur arrive » ne reste que le
    samedi ;
  - post « ça vient de tomber » : `REACTIVE_MAX_PER_DAY=3`, entre 12 h et 23 h 30. Depuis le
    19/11/2026, l'actu chaude est regardée toutes les 15 min (`REACTIVE_CHECK_EVERY_MINUTES=15`, 30
    avant) et Claude peut être interrogé 30 fois par jour (`REACTIVE_MAX_CHECKS_PER_DAY=30` ; à 6, le
    plafond tombait vers 20 h 30 et le bot ne voyait plus les annonces du soir). La mémoire des flux
    suit l'intervalle (`NewsHub._cache_ttl`) ; les comptes X restent lus toutes les 2 h au plus.
    Un post à chaud peut partir près d'un post prévu : 10 min d'écart (`REACTIVE_SLOT_GAP_MINUTES=10`,
    accord du propriétaire le 19/11/2026 ; `Bot._slot_gap`). Avec les 60 min d'avant, l'actu chaude
    n'était regardée ni de 13 h 30 à 15 h 30, ni de 18 h à 20 h, ni après 22 h : une annonce américaine
    de 19 h attendait 20 h. Entre deux posts à chaud, l'écart reste de 60 min
    (`REACTIVE_MIN_GAP_MINUTES`).
  - Sans histoire assez forte, un créneau est sauté exprès. Si Claude tombe pendant la rédaction, le
    créneau est rédigé à son retour.
- **La qualité avant le nombre** (choix du propriétaire, 19/11/2026) : il a demandé s'il fallait
  dépenser tous les crédits Claude avant qu'ils expirent ; réponse retenue : pas en publiant plus (il
  faudrait une vingtaine de posts par jour, donc des posts faibles), mais en faisant mieux. Sur le
  serveur : `POST_VERSIONS=3` (chaque post écrit en trois versions, un second avis garde la meilleure),
  `LLM_EFFORT_POST=high` (plus de réflexion pour l'écrire), et l'actu chaude regardée plus souvent
  (ci-dessus). Réglages posés par `deploy/migrate_env_v2.sh` ; les défauts du code restent une
  version, pas de réglage de réflexion, 6 vérifications, 30 min. Le nombre de posts ne change pas.
- Modèle : `LLM_MODEL` (par défaut `claude-opus-5-5`). Coût Claude mesuré : environ 1,10 $ par jour
  jusqu'au 18/11 (2 posts, une version) ; 1,37 $ le 18/11 (4 publications). Avec la qualité du 19/11,
  un post coûte 0,25 $ quand la première version passe la relecture, 0,38 $ mesuré dans le pire cas
  (trois versions refusées puis une réécriture). Mesuré ensuite : 2,59 $ le 19/11 (réglages posés à
  16 h 13), 2,99 $ le 20/11, 2,69 $ le 21/11, soit environ 85 $ par mois. Budget X :
  `X_BUDGET_DAILY_USD` / `X_BUDGET_MONTHLY_USD`.
- **Budget Claude : 195 $ par mois au plus** (consigne du propriétaire, 18/11/2026 au soir ; 35 $ le
  matin même ; `CLAUDE_BUDGET_MONTHLY_USD=195` dans le `.env` du serveur, défaut du code 35, codé dans
  `bot/llm.py`) : tout est compté, essais à la main compris
  (`LLM(…, manual=True)` → `state/llm_manual_usage.json`) ; l'optionnel (radar, posts à chaud, réponses
  aux commentaires) cède le premier ; alerte Telegram à 80 % ; arrêt au plafond jusqu'au 1er.
  Un `--preview news` coûte 0,25 à 0,40 $ (`tip,news` : le double) : compté dans le plafond, donc à
  garder pour vérifier un vrai changement de la rédaction.
- **Qui paie Claude** : la clé du bot est dans l'organisation individuelle du propriétaire sur la console
  Claude (platform.claude.com). Depuis le 18/11/2026 elle reçoit chaque mois les 200 $ de crédits API
  compris dans son forfait Claude Ultra (gratuits, expirent à chaque cycle : le premier lot le 13/12/2026).
  Plus de recharge à faire tant que le forfait Ultra est actif.
  - **Jamais la carte du propriétaire pour Claude** (sa consigne, 18/11/2026) : la recharge automatique
    de la console est coupée et doit le rester ; ne jamais acheter de crédits Claude pour le bot. À crédit
    épuisé, l'API refuse, le bot prévient sur Telegram et attend. (L'API X, elle, est payée par sa carte
    chez X : d'où le radar coupé, pour que la dépense X n'augmente pas avec le nouveau rythme.)
  - Ces crédits sont partagés par TOUT le compte API (toutes les clés, tous les projets) : le bot peut en
    prendre 195 $, presque tout.
  - La limite de dépenses de l'organisation, côté console, est restée à 500 $ **exprès** : elle
    s'applique à tout le compte, et le propriétaire veut que les 35 $ ne concernent que le bot (18/11).
    Ne jamais limiter l'espace « Default » : il sert à ses autres projets.
  - **Le bot a son propre espace de travail, « Bot X », limité à 195 $ par mois côté console** (35 $
    jusqu'au 18/11/2026 au soir)
    (Dépenses et utilisation → Limites de dépenses → Limites de l'espace de travail ; créé le
    18/11/2026). C'est une deuxième barrière : le compteur du bot (un peu pessimiste) s'arrête
    normalement le premier. La console ne compte dans « Bot X » que depuis le 18/11 : les 18 $ du début
    de novembre sont passés par l'ancienne clé, dans « Default ».
  - **Clé du bot : « bot-x-serveur »**, créée le 18/11/2026 dans l'espace « Bot X », **sans date
    d'expiration**. Vérifié le 18/11 : `--check` au vert, le service a écrit avec elle (appel du
    radar à 10 h 32), et la console compte bien sa dépense dans « Bot X ».
    C'est la seule clé du compte : les deux anciennes (« sabislabi-bot » du 27/10, qui expirait le
    26/11, et « sabislabi-bot-octobre » du 21/10, toutes deux dans « Default ») ont été supprimées
    le 18/11, avec l'accord du propriétaire.
  - Remplacer seulement la clé Claude (ce qui a marché le 18/11) : sur la console, Clés API → « Créer
    une clé » → « Continuer avec une clé API » ; nom, Expire « Jamais », Portée « Bot X ». L'assistant
    peut remplir le formulaire, mais c'est le propriétaire qui clique sur « Créer une clé » et qui copie
    la clé (l'assistant ne regarde plus la page tant qu'elle peut afficher la clé). Il la colle lui-même
    dans son Terminal avec
    `ssh -t botuser@178.128.194.190 'bash ~/x-degen-bot/deploy/set_secrets.sh ANTHROPIC_API_KEY'`
    (saisie masquée). Ensuite : un petit appel d'essai, `sudo -n systemctl restart x-degen-bot`,
    désactiver l'ancienne clé, puis `--check`.

## 5. Commandes utiles

- **Tests** : `python -m pytest -q -p no:cacheprovider`. Ils ne touchent jamais le réseau ; Claude, X et
  Telegram sont simulés (pages, photos et vidéos aussi ; les tests de découpage lancent le vrai ffmpeg sur
  une vidéo fabriquée sur place ; Wikimedia aussi ; les vidéos explicatives sont fabriquées en petit avec
  le vrai ffmpeg) ; 1493 tests sur le Mac au 23/11/2026 (sans ceux de l'installateur). Sur le Mac, les tests
  de l'installateur (`tests/test_deploy_scripts.py`) sont sautés, faute de `flock` : pour les voir avant
  d'installer, envoyer une copie du code dans un dossier temporaire du serveur et y lancer pytest avec
  le `.venv` du bot (4 min 30), ou lancer ceux de `migrate` sur le Mac avec un faux `flock` dans le PATH.
- **Sur le serveur** (`cd ~/x-degen-bot`, puis `.venv/bin/python -m bot.main …`) :
  - `--check` : clés, Telegram, API X, Claude, photo d'essai mise au propre, extrait d'une vidéo d'essai ;
  - `--preview tip,news` (ou `thread`, `reactive`, `explainer`) : brouillons, rien n'est publié
    (`explainer` fabrique la vidéo pour de vrai et l'envoie sur Telegram) ;
  - `--report` : les chiffres ;
  - `--once <plan|publish|comments|radar|reactive|metrics|daily|weekly|poll>` : une seule étape.
- **Telegram** : `/statut`, `/plan`, `/budget`, `/pause`, `/reprendre`, `/note <un fait réel>`, `/aide`.

## 6. Installer une mise à jour (la procédure qui marche)

1. Modifier, lancer TOUTE la suite de tests (verte), puis commit et push sur `main`.
2. Sur le Mac, une seule ligne :
   `curl -fsSL https://raw.githubusercontent.com/7rg2r4rm29-dotcom/sabislabi-x-bot/main/deploy/install_from_github.sh | bash`
   (ou, depuis un clone : `bash deploy/push_from_mac.sh`). L'assistant :
   - envoie le code ;
   - teste les clés déjà installées (`precheck.sh`) ;
   - installe (`deploy.sh`) : bibliothèques, tests sur le serveur, sauvegarde, `--check` réel,
     redémarrage, et retour arrière automatique en cas d'échec.
   - La question finale (« remplacer aussi tes clés ? ») se règle avec Entrée, qui vaut non.
3. Depuis une session Claude Code sur le Mac, ces questions n'attendent pas de clavier :
   `XBOT_NO_TTY=1 bash deploy/push_from_mac.sh < /dev/null` répond « non » partout. Si une clé ne marche
   pas, s'arrêter, et laisser le propriétaire relancer lui-même la commande dans son Terminal pour la
   coller.
4. `deploy.sh` lance toute la suite de tests sur le serveur. Un test qui dépend d'un outil absent du
   serveur fait donc échouer l'installation : le protéger avec `pytest.mark.skipif`.
5. Réglages modifiés à la main sur le serveur : ils sont gardés, et la nouvelle version est posée à côté
   (`….nouveau`). Exception : `news_feeds`, toujours mis à jour (la liste va avec la ligne éditoriale).
6. Après l'installation, vérifier `--check`, le journal du service et le premier post publié (texte et
   image).

## 7. Travailler avec le propriétaire

- Écrire en **français simple, sans jargon**.
- **Faire le travail soi-même, de bout en bout** : code, tests, push, installation quand l'accès existe,
  vérification. Ne pas lui demander de faire à la main ce que les outils permettent de faire.
- Le **consulter seulement** pour :
  - une dépense (X Premium, budget publicitaire…) ;
  - une action irréversible (suppression, résiliation) ;
  - un choix qui lui appartient vraiment (prix, offre, nom).
  Pour tout le reste : décider, faire, vérifier, puis dire ce qui a été fait.
- Quand un accès manque : chercher d'abord comment l'obtenir. Ensuite seulement, expliquer simplement ce
  qui manque et demander l'autorisation précise.
- Vérifier avant d'annoncer « c'est fait » ; dire franchement ce qui n'a pas marché.
- **Git** :
  - messages de commit en anglais, courts ;
  - pas de pull request sauf demande ;
  - pousser souvent (un travail non poussé peut être perdu si la session est réinitialisée).
  - Sur le Mac, le clone est `~/Documents/sabislabi-x-bot`. Il pousse avec une clé de déploiement propre à
    ce dépôt (`~/.ssh/sabislabi_x_bot_github`, « Mac - push to main » dans les réglages GitHub, écriture
    permise), déjà branchée dans la config git du clone (`core.sshCommand`, `pushurl` en SSH). Auteur des
    commits : `Claude <noreply@anthropic.com>` (config locale du clone). Un clone neuf doit reprendre ces
    trois réglages.

## 8. État au 23/11/2026, limites connues, pistes

- **Portée, bilan du 23/11/2026** (104 posts du 1er au 23/11, chiffres de l'API) : 20 à 60 vues par
  post, médiane environ 40 ; en tout 37 likes, 30 réponses, 1 signet et **aucun partage**. Abonnés :
  2 940 (4 511 fin octobre ; −1 440 le 31/10 et −101 le 18/11, deux ménages de X), hérités de la période
  NFT : peu voient nos posts, presque personne ne réagit, donc X ne les pousse pas plus loin. Les mieux :
  GTA VI (208 vues), la triche à l'examen en Corée (thread, 134), l'IA et le problème de maths de 1859
  (109). Par sujet (calcul `what_works` du 23/11) : IA qui étonne 1,7x, jeux vidéo 1,6x, vie quotidienne
  1,4x, sujets pour initiés 1,3x (jamais poussés : ligne grand public), gadgets 0,8x. Par type : threads
  1,65x, « à chaud » 1,4x, « le futur arrive » 0,95x. Texte seul et photo : pas d'écart net.
- **v6 installée le 23/11/2026** : ce qui marche + tendances (`3c81a52`, 1464 tests verts sur le
  serveur, puis `14feb27`), vidéos explicatives (`9e56d53`, `0dc69d3`, `29f67f3` ; 1539 tests verts sur
  le serveur, 1508 sur le Mac). Premier classement par sujet fait par le bot à 21 h 15 (120 posts).
  Première vidéo prévue le mercredi 25/11 à 14 h 30.
- **Essais réels des vidéos, 23/11 au soir** (`--preview explainer`, 0,22 à 0,40 $ chacun) :
  1. post refusé deux fois « trop proche d'une source » : il était comparé à l'article ENTIER (presque
     tous ses mots y sont) → comparé au titre et au résumé, comme un post (`0dc69d3`) ;
  2. script jugé « un peu technique » (« soft robot » pas expliqué, « 15 ft/s »), puis post qui
     recopiait le titre → consignes plus nettes (ses propres mots, unités en toutes lettres), motifs de
     refus traduits en consignes anglaises (`_FEEDBACK_EN`, `_NESTED_FEEDBACK`), 3 essais (`29f67f3`) ;
  3. réussi : les poubelles-robots de la gare de Shinjuku. La relecture a refusé le premier script
     (il disait que les robots roulent encore, alors que le test de 10 semaines était fini), le second
     est passé ; photo de l'article (la page officielle, un PDF de JR East, répond 403) ; vidéo de
     7 scènes, 57 s, 2 Mo, fabriquée en 118 s ; envoyée sur Telegram. Coût : 0,32 $.
  4. avec la voix off (23 h 15) : l'écran des Tesla « façon GTA » dessiné en direct par une IA ;
     42 s de voix, vidéo de 50 s (7 scènes), voix ~85 s + vidéo 98 s ; envoyée sur Telegram. La
     relecture avait accepté « GTA » (sigle non expliqué) au 2e essai : depuis `067d228`, « un peu
     technique » n'est toléré qu'au 3e et dernier essai d'une vidéo.
- Le flux The Guardian répond aussi 403 depuis le 23/11 (avec IEEE Spectrum 403 et The Debrief 404).
- **@sabislabi est DÉJÀ abonné à X Premium** (le propriétaire l'a rappelé le 23/11/2026 ; vérifié par
  l'API : `verified_type` « blue », `subscription_type` « Premium »). Ne plus le lui proposer ; vérifier
  ce genre de chose par l'API AVANT de suggérer une dépense. La portée faible ne vient donc pas de là.
- **Dépenses chez X : « les crédits API que j'ai couvrent le reste des dépenses »** (le propriétaire,
  23/11/2026, en réponse à la proposition chiffrée de la veille du marché, environ 0,25 $ par jour) :
  accord pour la veille. Le plafond du bot reste `X_BUDGET_MONTHLY_USD=30`.
- **Veille du marché, installée le 23/11/2026** (`e5a8962`, 1544 tests verts sur le serveur ;
  `MARKET_WATCH_ENABLED=true` sur le serveur, faux par défaut dans le code) : une fois par jour, à la
  première rédaction, les 10 derniers posts de 5 gros comptes du même sujet (`market_accounts` de
  `settings.json`, sinon `DEFAULT_MARKET_ACCOUNTS` : minchoi, rowancheung, kimmonismus, Rainmaker1973,
  TheHumanoidHub), classés par réactions (likes + 2 × partages + réponses), ligne éditoriale appliquée,
  montrés à la rédaction en chef seulement (« learn what kind of story and what angle works, never copy
  them »). Lectures optionnelles : elles cèdent au budget X ; un compte introuvable est noté et n'est
  plus cherché. Essai réel à petite dose le 23/11 (minchoi, 5 posts) : comptes et chiffres bien lus.
  Payée par une économie : les statistiques de nos 50 derniers posts sont relues toutes les ~6 h au
  lieu de 3 (`analytics.REFRESH_EVERY_MIN` 350 ; ces « owned reads » coûtaient 0,40 à 0,45 $ par jour,
  le plus gros poste chez X ; les chiffres se stabilisent en 24 h). Dépense X avant : 0,50 à 0,92 $ par
  jour du 17 au 23/11.

- **Installé** : `main` `7d5e462` le 13/11 à 23 h 23, la correction de `--report` (heure de publication
  au lieu de l'heure de rédaction), puis le 14/11 la v5 : aucun média cité dans les posts, et l'image
  prise à la source (extrait vidéo, sinon photo officielle). Envoi vidéo à X vérifié pour de vrai le
  14/11 (vidéo d'essai envoyée sans être publiée : initialize → append → finalize → STATUS). Le 15/11 :
  images comme AlertesInfos (photo ou vidéo de l'article reprise comme la nôtre, 2e image logo /
  drapeau). Essai réel le 15/11 sur ChatGPT Wallet : openai.com bloque les robots, la photo de
  TechCrunch montrait l'assistant d'Amazon (écartée par Claude), le logo d'OpenAI a été retenu.
- **Panne du 17/11** : crédit Claude prépayé épuisé le 17/11 à 13 h 02 (la recharge de 24 $ du 27/10 a
  tenu trois semaines ; environ 2 $ sont partis dans les essais de la session du 13 au 15/11). Le thread
  du mardi et le post de 19 h ont été perdus ; le bot avait prévenu sur Telegram (« 💳 Claude ne peut plus
  écrire »), sans que le propriétaire le voie. Réglé le 18/11 à 8 h : crédits du forfait Ultra réclamés
  (avec son accord), puis plafond de 35 $ par mois ajouté au bot.
- **Bilan du 13/11** : `--check` au vert, aucune erreur depuis le 28/10, aucun créneau sauté du 10 au 13/11.
  Aperçu réel de la v4 : deux posts corrects, captures TechCrunch propres (abandonnées depuis).
  Abonnés : −1 440 d'un coup le 31/10 (ménage de X, pas le bot), puis environ −2 par jour. Radar et
  réponses aux commentaires : aucune suggestion utilisée (62 et 10 expirées). Le flux IEEE Spectrum
  répond 403 (refus des robots) : le bot le saute.
- **Clé Claude remplacée le 18/11/2026** (elle expirait le 26/11) : nouvelle clé sans date
  d'expiration, dans un espace réservé au bot et limité à 35 $ par mois (voir section 4, « Qui paie
  Claude »). Bot relancé à 9 h 47, `--check` au vert ; les deux anciennes clés ont été supprimées.
- **Qualité avant le nombre, installée le 19/11/2026 à 16 h 13** (`c65ad27`, 1394 tests verts sur le
  serveur) : trois versions par post et second avis, réflexion longue pour les écrire, actu chaude
  regardée toutes les 15 min avec 30 appels par jour au plus (détail section 4).
  - Avant (12 jours de journal, une seule version) : 27 posts écrits, 13 acceptés au premier essai,
    13 au second, 1 abandonné ; refus au premier essai : relecture des faits 5, trop technique 4,
    trop long 3, trop proche d'une source 2. Une version sur deux est donc refusée : avec trois
    versions, il arrive encore qu'aucune ne passe (une fois sur huit environ, par le calcul).
  - Essai réel du 19/11 (`--preview news`, sortie de Gemini 4 Neon) : trois versions écrites en 20 s
    chacune (0,05 $ pièce), second avis 0,02 $, puis le relecteur a refusé les trois (« paid » et
    « only » absents de la source ; « long » ; mots trop techniques) ; la réécriture guidée de la
    mieux classée est passée. 2 minutes, 0,38 $ en tout, post en texte seul (pas d'image à la source).
  - Vérifié dans le journal après l'installation : l'actu relevée à 16 h 31 puis à 16 h 47 (flux
    relus à chaque fois), avec un appel `pick-reactive` de 0,026 $ à chaque relevé.
  - **Bilan du 22/11 (19/11 16 h → 21/11)** : 16 posts publiés, aucun créneau perdu. Rédaction :
    11 posts acceptés au premier essai (version classée 1re par le second avis 9 fois, 2e 2 fois),
    3 après réécriture, 4 brouillons abandonnés (le post de 19 h le 19/11 au premier passage, refait
    au passage suivant ; trois essais « à chaud » sur la partie de GTA VI jouée par la pensée, dont la
    seule source était un post X de six mots : refusés par la relecture, puis publiés le 21/11 à
    12 h 18 quand un vrai article est sorti). Vérifications à chaud : 12, 25 et 19 par jour (plafond
    30). Posts à chaud : 3, 2 et 2 par jour ; deux sont partis juste après le post de 19 h (19 h 12 le
    19/11, 19 h 28 le 20/11 : 28 et 26 vues, contre 23 et 22 pour le post de 19 h). La même histoire
    deux fois (l'hypothèse de Riemann, 20/11 « pas encore vérifiée » puis 21/11 « vérifiée par deux
    groupes ») : une suite voulue par la rédaction en chef, raison écrite dans le journal. Dépense X :
    0,72, 0,63 puis 0,53 $ par jour (le relevé toutes les 15 min ne lit pas X plus souvent). Portée :
    16 à 208 vues par post, la plupart entre 20 et 45. Seuls avertissements : Telegram injoignable
    quelques minutes le 20/11 vers 0 h 50.
  - À regarder dans le journal : `Post … : version retenue parmi 3 (… rang N du second avis)`,
    `version n/3 écartée`, `version classée n refusée à la relecture`, appels `choose-<kind>`.
  - Le flux The Debrief répond 404 depuis le 19/11 au moins (adresse à corriger dans `news_feeds`).
- **Écart autour des posts prévus ramené de 60 à 10 min le 19/11** (accord du propriétaire : « Oui,
  autorise », en sachant que deux posts proches se gênent un peu sur X). À surveiller : un post à chaud
  et un post prévu à quelques minutes l'un de l'autre, et leur portée respective.
- **En attente de la décision du propriétaire** : lancer `cleanup_v1.sh` (le dossier `auth/` et deux
  fichiers de clé SSH égarés traînent encore sur le serveur ; le script n'efface pas encore
  `config/.env.avant_pause` ni `config/.env.avant_rotation`, deux vieilles copies de clés). Le radar,
  lui, est coupé depuis le 18/11/2026.
- **Mac, façon de faire du 19/11** : clone neuf dans le dossier temporaire de la session (depuis
  GitHub, avec son `venv`), travail et tests là, les trois réglages git recopiés (auteur,
  `core.sshCommand`, `pushurl`), push et `push_from_mac.sh` depuis ce clone, puis `git pull` dans
  `~/Documents/sabislabi-x-bot` (git y répond, c'est Python qui bloque).
- **Mac du propriétaire : Documents est dans iCloud, avec des fichiers « dans le nuage seulement »**
  (le 18/11/2026, `.venv` et quelques objets `.git` du clone ne s'ouvraient plus : Python restait
  bloqué). Pour tester, travailler dans un clone neuf hors de Documents (avec son propre `venv`), puis
  recopier les fichiers modifiés dans `~/Documents/sabislabi-x-bot`.
- **À surveiller les premiers jours** :
  - la qualité des posts et des images (et la part des posts en texte seul) ;
  - les créneaux sautés (normal s'il n'y a pas d'histoire assez forte, à creuser si c'est fréquent) ;
  - le coût de Claude (`/budget`).
- **Limites connues** :
  - l'audience est héritée de l'ancienne période NFT du compte ;
  - pas encore de vraies statistiques sur la nouvelle ligne ;
  - beaucoup d'actus n'ont pas d'image à la source : l'article ne renvoie pas vers la page officielle
    (annonce faite sur X seulement), la source bloque les robots (openai.com, EurekAlert : jamais forcés),
    ou la vidéo est sur YouTube. Le post part alors en texte seul ;
  - la limite de 40 s par téléchargement est approximative, mais le relevé des flux est coupé au bout de
    120 s.
- **Pistes** :
  - après une ou deux semaines, lire `--report` et les notes de performance, puis ajuster les sources
    (`news_feeds`) et les consignes ;
  - regarder la portée des premières vidéos explicatives (type `explainer` dans `what_works`) contre les
    autres posts, et celle des posts « d'essai » (`fmt.test`) contre ceux qui suivent ce qui marche ;
  - la voix off est installée (voir section 4) : l'assistant ne peut pas l'écouter lui-même ; demander
    au propriétaire si la voix lui plaît (sinon `EXPLAINER_VOICE`, par exemple `am_michael`).
