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
  images officielles. Aucune maquette, aucune carte dessinée. Sans image sûre, texte seul.
- **Mode** : `APPROVAL_MODE=auto`, donc les posts partent sans validation humaine. Telegram sert aux
  rapports et aux commandes.
- **Historique** :
  - v1 : navigateur automatisé, abandonnée ;
  - v2 : API officielle X + Telegram ;
  - v3 : cartes dessinées (`0d04a7e`) ;
  - v4 (novembre 2026) : captures façon téléphone, ligne grand public, garde-fous renforcés
    (`7bb6b1b`, `e153686`, `7d5e462`) ;
  - v5 (14/11/2026) : image prise à la source (extrait vidéo, sinon photo officielle) au lieu des
    captures, et le compte donne la nouvelle lui-même (aucun média cité, jamais de lien).

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
  - relecture par affirmation (`_factcheck`) ;
  - consignes : `AUDIENCE`, `INTRIGUE`, `PLAYBOOK`, `NEWS_VOICE`, `PICKS`, `TRUTH`, `PHOTO_RULES`,
    `EDITOR_PROMPT`, `FACTCHECK_PROMPT`, `PHOTO_CHECK_PROMPT`.
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
- Les autres modules :
  - `bot/scheduler.py` : créneaux, posts « à chaud », 🔄 nouvelle version ;
  - `bot/approvals.py` : Telegram ;
  - `bot/publisher.py` : API X ;
  - `bot/analytics.py` : statistiques, rapports, notes de performance données à Claude ;
  - `bot/main.py` : ligne de commande ;
  - `bot/state.py`, `bot/config.py`, `bot/llm.py`.
- **Rythme par défaut** (modifiable dans `config/.env`) :
  - `POST_SLOTS=tip@14:30,news@19:00` (« le futur arrive », « l'histoire du jour ») ;
  - `THREAD_SLOTS=tue@14:30,sun@16:00` ;
  - post « ça vient de tomber » : au plus 1 par jour, entre 12 h et 23 h 30 (6 vérifications par jour
    au plus).
  - Sans histoire assez forte, un créneau est sauté exprès. Si Claude tombe pendant la rédaction, le
    créneau est rédigé à son retour.
- Modèle : `LLM_MODEL` (par défaut `claude-opus-5-5`). Coût Claude mesuré : environ 1,10 $ par jour (30
  à 35 $ par mois). Budget X : `X_BUDGET_DAILY_USD` / `X_BUDGET_MONTHLY_USD`.
- **Budget Claude : 35 $ par mois au plus** (consigne du propriétaire, 18/11/2026 ;
  `CLAUDE_BUDGET_MONTHLY_USD`, codé dans `bot/llm.py`) : tout est compté, essais à la main compris
  (`LLM(…, manual=True)` → `state/llm_manual_usage.json`) ; l'optionnel (radar, posts à chaud, réponses
  aux commentaires) cède le premier ; alerte Telegram à 80 % ; arrêt au plafond jusqu'au 1er.
  **Donc : peu d'aperçus et d'essais payants** (un `--preview` coûte environ 0,50 $).
- **Qui paie Claude** : la clé du bot est dans l'organisation individuelle du propriétaire sur la console
  Claude (platform.claude.com). Depuis le 18/11/2026 elle reçoit chaque mois les 200 $ de crédits API
  compris dans son forfait Claude Ultra (gratuits, expirent à chaque cycle : le premier lot le 13/12/2026).
  Plus de recharge à faire tant que le forfait Ultra est actif.
  - Ces crédits sont partagés par TOUT le compte API (toutes les clés, tous les projets) : le bot en
    prend 35 $ au plus, le reste est pour les autres projets du propriétaire.
  - La limite de dépenses de l'organisation, côté console, est restée à 500 $ **exprès** : elle
    s'applique à tout le compte, et le propriétaire veut que les 35 $ ne concernent que le bot (18/11).
    Pour une limite côté console qui ne touche que le bot : lui donner son propre espace de travail
    (workspace) avec une limite de 35 $, et y créer sa clé. Ne jamais limiter l'espace « Default ».
  - **La clé du bot (« sabislabi-bot », créée le 27/10/2026) expire le 26/11/2026** : sans nouvelle
    clé avant cette date, plus aucun post (« clé ANTHROPIC_API_KEY refusée »). L'expiration ne se
    modifie pas dans la console. Une ancienne clé « sabislabi-bot-octobre » (21/10) traîne, inutilisée.
    Remplacer seulement la clé Claude : le propriétaire crée la clé dans la console (sans date
    d'expiration), puis la colle lui-même dans son Terminal avec
    `ssh -t botuser@178.128.194.190 'bash ~/x-degen-bot/deploy/set_secrets.sh ANTHROPIC_API_KEY'`
    (saisie masquée) ; ensuite `sudo -n systemctl restart x-degen-bot` et `--check`.

## 5. Commandes utiles

- **Tests** : `python -m pytest -q -p no:cacheprovider`. Ils ne touchent jamais le réseau ; Claude, X et
  Telegram sont simulés (pages, photos et vidéos aussi ; les tests de découpage lancent le vrai ffmpeg sur
  une vidéo fabriquée sur place ; Wikimedia aussi) ; 1330 tests au 18/11/2026.
- **Sur le serveur** (`cd ~/x-degen-bot`, puis `.venv/bin/python -m bot.main …`) :
  - `--check` : clés, Telegram, API X, Claude, photo d'essai mise au propre, extrait d'une vidéo d'essai ;
  - `--preview tip,news` (ou `thread`, `reactive`) : brouillons, rien n'est publié ;
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

## 8. État au 18/11/2026, limites connues, pistes

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
- **Urgent, avant le 26/11/2026** : remplacer la clé Claude du bot, qui expire (voir section 4,
  « Qui paie Claude »). Proposé au propriétaire le 18/11, avec un espace réservé au bot limité à 35 $.
- **En attente de la décision du propriétaire** : lancer `cleanup_v1.sh` (le dossier `auth/` et deux
  fichiers de clé SSH égarés traînent encore sur le serveur ; le script n'efface pas encore
  `config/.env.avant_pause` ni `config/.env.avant_rotation`, deux vieilles copies de clés) ; couper ou non
  le radar.
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
  - X Premium, pour la portée : une dépense, donc la décision du propriétaire.
