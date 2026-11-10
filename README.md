# Assistant X de @sabislabi — version 2

Claude rédige, **tu valides d'un geste sur Telegram** (ou le bot publie seul, en mode automatique), et tout
part à l'heure via l'**API officielle de X**.
Plus de navigateur qui se fait passer pour toi sur x.com, plus de réponses ni de likes automatiques.

**La ligne du compte (novembre 2026) : « l'IA simplement, pour la vie de tous les jours ».** On parle à
M. et Mme Tout-le-monde, pas aux passionnés de tech : une situation que tout le monde connaît, la phrase
exacte à taper, zéro jargon — et **chaque post part avec une belle image** à enregistrer.

> Guide écrit pour être suivi sans connaissances techniques. Les commandes sont à copier-coller telles quelles.

---

## 1. Pourquoi une v2 ?

Les règles de X ont changé en 2026. La v1 (un navigateur piloté par un robot) mettait le compte en danger.

| Règle | Depuis |
|---|---|
| Automatiser x.com avec un navigateur = **suspension définitive** (règles développeurs X) ; les nouvelles conditions interdisent aussi le « scraping » | conditions du 9 oct. 2026 |
| Par l'API, un bot ne peut répondre qu'aux personnes qui **t'ont mentionné ou répondu** | 23 févr. 2026 |
| Likes, abonnements et citations retirés de l'API | 20 avr. 2026 |
| Réponses écrites par une IA : accord préalable de X ; compte 100 % automatique : étiquette « Automated » obligatoire | 2026 |
| **42 000 comptes** de réponses par chatbot supprimés | 24 juil. 2026 |
| La rémunération des créateurs (Original Content Rewards) exclut le contenu « créé ou publié par des moyens automatisés » | 8 sept. 2026 |

En septembre, le compte a publié **850 posts, presque tous des réponses automatiques**. Or les réponses
ne sont pas montrées aux personnes qui ne te suivent pas (fil « Pour toi ») et ne rapportent rien.
C'est exactement le profil que X a supprimé en juillet. D'où la v2 : moins de posts, mais de vrais posts,
validés par toi.

## 2. Ce que l'algorithme de X récompense vraiment

D'après le code de classement publié par xAI (mis à jour le 13 oct. 2026) :

- **Ce qui compte** : réponse (×5), citation (×5), partage en message privé (×5), **copie du lien (×20)**,
  abonnement (×4). Un like ne vaut que ×0,66.
- **Ce qui coûte très cher** : « pas intéressé » (−52), masquer (−59), signaler (−234).
  Un post médiocre fait plus de mal que pas de post du tout.
- **Pas plus de 3 posts par jour, bien espacés** : ton 2e post dans le fil d'une même personne ne vaut que ×0,63,
  le 3e ×0,44.
- **Les 2 premières heures comptent** : un post récent (moins de 2 h, moins de 200 vues) est montré à des
  non-abonnés, pour les comptes de moins de 50 000 abonnés.
- **Dans un thread, seul le 1er tweet atteint les non-abonnés** : il doit se suffire à lui-même.
- Les liens ne sont plus pénalisés… mais via l'API un post avec lien coûte **0,20 $ au lieu de 0,015 $**.
  Le bot n'en met donc pas.
- Grok traduit automatiquement les posts partout dans le monde : **un seul compte en anglais suffit**.
- **Ce qui fait grandir un petit compte** : répondre vite, et bien, sous les posts des gros comptes de ta
  niche. C'est le rôle du **radar** (plus bas), mais c'est **toi** qui publies ces réponses.

## 3. Ta journée avec le bot

Tout se passe dans Telegram. Chaque contenu arrive sous forme de **carte** avec des boutons :

| Bouton | Effet |
|---|---|
| ✅ | Valider : partira à l'heure prévue (ou tout de suite si l'heure est passée). Seul le texte affiché sur la carte est validé |
| ✏️ | Modifier : appuie, puis **réponds** au message qui s'affiche avec ton texte |
| 🔄 | Autre version, écrite par Claude (3 fois maximum) |
| ❌ | Annuler (même après validation, tant que ce n'est pas parti) |

**Le planning par défaut (heure de Paris)**

| Contenu | Publication | La carte arrive vers |
|---|---|---|
| **L'IA au quotidien** : une situation de tous les jours et la phrase exacte à taper (le frigo, une lettre incompréhensible, un mail délicat, les devoirs…) | 14 h 30 | 2 h 30 avant (45 min en mode automatique) |
| **L'actu IA en mots simples** : la nouveauté du jour qui concerne tout le monde. S'il n'y en a pas, un autre post du quotidien | 19 h | 2 h avant (45 min en auto) |
| Thread : usages du quotidien le mardi, **la semaine de l'IA en mots simples** le dimanche | mardi 14 h 30 et dimanche 16 h | 4 h avant (1 h 30 en auto) |
| **Actu à chaud** : seulement quand une annonce touche vraiment tout le monde (gratuit, sur le téléphone, une arnaque à éviter) | 1 par jour au plus, entre 12 h et 23 h 30 | dès qu'elle tombe |

Deux posts par jour, aux heures où le public anglophone est en ligne (14 h 30 = matin aux États-Unis,
19 h = leur pause de midi et la soirée en Europe). Moins de posts, mais chacun soigné : un post médiocre
coûte plus cher qu'un post en moins.

**À qui on parle.** Une infirmière, un prof, une commerçante, un parent, un retraité : des gens qui ont
entendu parler de ChatGPT et qui veulent savoir ce que ça peut faire **pour eux, ce soir**. Donc :

- on part d'un moment de la vie (« Tu ouvres le frigo et tu n'as aucune idée »), jamais de la technologie ;
- on donne **la phrase exacte à taper**, entre guillemets ;
- **zéro jargon** : les mots comme « LLM », « API », « benchmark », « open-source » sont refusés par le
  bot, et une seconde lecture de Claude vérifie qu'un lecteur qui n'a jamais ouvert ChatGPT comprend tout ;
- pour l'actu, on laisse de côté ce qui n'intéresse que les développeurs ou les investisseurs (levées de
  fonds, tests de performance, puces, procès…) ;
- les thèmes tournent (cuisine, courriers, mails, enfants, emploi, voyages, achats, arnaques…) : jamais
  deux fois le même à la suite.

**Chaque post part avec une image** (format portrait 4:5, affiché en entier dans le fil) :

| Image | Quand |
|---|---|
| **La phrase à copier**, dans une bulle de messagerie | la plupart des astuces |
| **Des étapes** numérotées, ou **une liste** | les pas-à-pas, la couverture des threads |
| **Un fait en très grand** | une actu résumée en une phrase |
| **La page de l'article, vue dans un smartphone** | une actu : la vraie page (chargée comme sur un téléphone, cadrée sur son titre), posée dans une maquette de smartphone |

Même identité à chaque fois (mêmes polices, même mise en page), et **cinq palettes qui tournent** : deux
posts de suite ne se ressemblent pas. La capture « smartphone » est **regardée par Claude** avant de
servir : si la page n'est pas impeccable (bandeau, mur d'abonnement, titre coupé), une carte dessinée la
remplace. Plus jamais de capture ratée publiée. Un sondage, lui, n'a pas d'image (X ne le permet pas).

- **Les jours de thread**, le thread prend la place du post de 14 h 30 : jamais plus de 2 ou 3 contenus
  dans la journée.
- Si Claude ne trouve rien de solide, le créneau est **sauté** (tu es prévenu) plutôt que rempli avec un
  texte creux. En mode automatique, aucun texte « de secours » n'est jamais publié tout seul.
- Chaque texte (et sa carte) est **vérifié** par une seconde lecture de Claude : faits d'actu, chiffres,
  aucune « expérience perso » inventée, et des mots que tout le monde comprend.
- Tu as jusqu'à **2 h après l'heure prévue** pour valider. Passé ce délai, la carte expire : **rien ne part**,
  même si tu appuies sur ✅ plus tard.
- Si le bot a été arrêté et qu'un post validé a plus de 3 h de retard, il ne part pas non plus
  (l'actu du matin à 16 h n'a plus de sens). Tu es prévenu.
- Si X ne confirme pas une publication (coupure réseau), le bot vérifie sur ton profil avant de réessayer :
  **jamais de doublon**.
- La nuit (23 h 30 - 7 h 30), les messages arrivent **sans sonnerie**.

**Les commentaires sous tes posts.** Quand quelqu'un répond à l'un de tes posts, une carte arrive avec une
réponse proposée. ✅ = le bot répond pour toi (X l'autorise, puisque cette personne t'a répondu).

**Le radar 🎯.** Quand un gros compte de ta niche vient de poster et que ça décolle, une carte arrive avec
une réponse suggérée. Le bouton **« Répondre dans X »** ouvre l'application X avec le texte déjà rempli :
tu relis, tu ajustes, **tu publies toi-même**. Le bot ne publie jamais ces réponses (c'est interdit et
c'est ce qui a fait supprimer 42 000 comptes). La suggestion reste valable 90 minutes : une réponse en
retard ne sert à rien.

**Les bilans.** Un récap chaque matin à 8 h 30, un bilan de la semaine le dimanche à 20 h 30.

**Voir ce que le bot écrirait, sans rien publier.** Depuis le Terminal :

```
ssh botuser@178.128.194.190 'cd ~/x-degen-bot && .venv/bin/python -m bot.main --preview'
```

Tu reçois sur Telegram une astuce et une actu rédigées à l'instant, **avec leur image**, marquées
« APERÇU ». Rien n'est publié et le planning du bot n'est pas touché (`--preview tip,news,thread` pour
voir aussi un thread).

**Les chiffres.** Pour savoir ce que chaque post a donné (vues, j'aime, réponses, signets) et comment
évolue le nombre d'abonnés :

```
ssh botuser@178.128.194.190 'cd ~/x-degen-bot && .venv/bin/python -m bot.main --report'
```

### Les commandes Telegram

| Commande | Effet |
|---|---|
| `/statut` | ce qui attend ta validation, ce qui est programmé |
| `/plan` | les prochains créneaux |
| `/budget` | dépenses API X et Claude |
| `/pause` | plus rien n'est publié jusqu'à `/reprendre` |
| `/reprendre` | relance ; les posts validés en retard partent 3 min plus tard (le temps d'annuler) |
| `/note …` | un fait réel que tu as vécu (voir ci-dessous) |
| `/aide` | la liste des commandes |

### `/note` : ton meilleur levier

Claude n'invente jamais d'expérience personnelle. Pour qu'il puisse raconter du vécu (ce qui marche le
mieux sur X), donne-lui des faits :

```
/note j'ai testé Claude pour trier 200 mails clients, 1 h gagnée
/note j'ai construit ma landing page en une soirée avec l'IA
```

Chaque note peut être utilisée 2 fois, pendant 14 jours, sans rien ajouter au-delà de ce que tu as écrit.

## 4. Combien ça coûte

**API X** (paiement à l'usage) : un post coûte 0,015 $, une réponse 0,01 $, la lecture d'un post 0,005 $.

- 2 posts par jour : environ 1 $ par mois ; 2 threads par semaine : environ 1 $ par mois.
- Le reste sert à lire (radar, comptes d'actu, statistiques).
- **Plafond du bot : 30 $ par mois.** Les lectures (radar…) ne peuvent jamais dépasser 75 % de ce plafond,
  réparties sur le mois : le dernier quart reste toujours disponible pour publier.
- Crédits offerts par X : 20 $ à l'ajout d'une carte, puis jusqu'à 50 $ (égaux à ta première recharge
  automatique).

**Claude** : à toi de surveiller sur console.anthropic.com ; `/budget` affiche une estimation. Le bot utilise
le meilleur modèle (Opus 5.5). Pour diviser la facture environ par deux, mets `LLM_MODEL=claude-sonnet-5-5`
dans les réglages (section 9).

## 5. Installation (une seule fois)

Tu n'as que **deux choses à faire**. L'assistant fait le reste et te guide quand il a besoin de toi.

### 1. Télécharger le code (un clic)

Sur **github.com/7rg2r4rm29-dotcom/sabislabi-x-bot** : bouton vert **Code → Download ZIP**.
Safari le décompresse tout seul dans *Téléchargements* (dossier `sabislabi-x-bot-main`). Avec Chrome,
double-clique sur le fichier zip.

### 2. Coller une commande

Ouvre l'app **Terminal** (Cmd + Espace, tape « Terminal »), colle cette ligne et appuie sur Entrée :

```
bash ~/Downloads/sabislabi-x-bot-main/deploy/push_from_mac.sh
```

Ensuite, laisse-toi guider :

1. Le code part sur ton serveur.
2. L'assistant **essaie d'abord les clés déjà installées** (Telegram, API X, Claude). Il ne te demande que
   ce qui ne marche pas : il **ouvre lui-même la bonne page** et te dit où cliquer. Quand il te demande de
   coller une clé, **rien ne s'affiche, c'est normal**. Les clés vont directement sur le serveur.
   Ne les colle jamais ailleurs (Claude, Cowork, mail…).
3. Il installe la nouvelle version (5 à 10 minutes) : tests, sauvegarde, vérification, puis redémarrage.
   Si ta connexion se coupe, **l'installation continue toute seule** sur le serveur.
4. Une fois l'installation réussie, il te propose de **remplacer aussi les clés qui marchent** : c'est
   recommandé à cause du vol du 9 octobre (environ 5 minutes, il te guide, puis relance le bot avec les
   nouvelles clés). Tu peux répondre **N** et le faire plus tard en relançant la même commande.

C'est réussi quand tu lis **`✅ C'est fait`**, et que Telegram affiche « ✅ Test v2 OK » puis
« 🤖 Bot v2 démarré ». Envoie `/statut` pour voir le planning.

**Si ça ne se termine pas ainsi :**

- `Une publication était en cours` : relance la même commande 20 minutes plus tard. Rien n'a été modifié.
- `❌ L'installation n'a pas abouti` : rien n'est perdu, l'ancienne version tourne comme avant et tu reçois
  un message sur Telegram. Les lignes ❌ au-dessus disent ce qui bloque (souvent une clé mal copiée ou des
  crédits manquants). Relance la commande.

**Ce que seul toi peux faire, si l'assistant te le demande :** ajouter une carte bancaire sur la console X
(paiement à l'usage, X offre 20 $ de crédit) ou du crédit sur la console Claude.

**À faire aussi (2 minutes) :** sur X, change ton mot de passe, puis *Paramètres → Sécurité et accès au
compte → Applications et sessions → Sessions* : **déconnecte toutes les autres sessions** (dont celle de
l'ancien robot).

### 3. Le grand ménage (2 ou 3 jours plus tard, quand tout va bien)

Dans le Terminal :

```
ssh botuser@178.128.194.190 'bash ~/x-degen-bot/deploy/cleanup_v1.sh'
```

Il doit afficher `NETTOYAGE_OK`. Il supprime sur le serveur la session de connexion à X de l'ancien robot
(l'équivalent de ton mot de passe), les copies de tes anciennes clés et les restes de la v1.

Puis sur ton Mac : mets le dossier **x-degen-bot** du Bureau à la corbeille (il contient tes anciennes
clés en clair et la session X, et il est synchronisé avec iCloud) et **vide la corbeille**. Sur
icloud.com, *Drive → Récemment supprimés* : supprime-le aussi. Le code vit désormais sur GitHub et sur le
serveur.

## 6. Mettre à jour plus tard

Quand une nouvelle version est prête : supprime l'ancien dossier `sabislabi-x-bot-main` de
*Téléchargements*, retélécharge le ZIP et relance la même commande. Tes clés, l'historique du bot et tes
réglages sont conservés. Si tu as modifié `config/settings.json` ou `config/content.json`, tes modifications
sont gardées et la nouvelle version par défaut est posée à côté (`….nouveau`).

## 7. Revenir en arrière, mettre en pause, arrêter

- **Pause** (le bot continue de tourner mais ne publie plus) : `/pause` sur Telegram, puis `/reprendre`.
- **Revenir à la version précédente** (5 sauvegardes sont gardées) :

  ```
  ssh -t botuser@178.128.194.190 'bash ~/x-degen-bot/deploy/restore_backup.sh'
  ```

  Revenir à l'**ancienne version navigateur** demande de taper `OUI` : elle met le compte en danger.
  Pour souffler, préfère `/pause`.

- **Relancer le bot** (s'il ne répond plus) :

  ```
  ssh botuser@178.128.194.190 'sudo systemctl restart x-degen-bot'
  ```

- **Arrêter complètement** : `ssh botuser@178.128.194.190 'sudo systemctl stop x-degen-bot'`
  (si le serveur demande un mot de passe, utilise plutôt `/pause`).

## 8. En cas de problème

| Ce que tu vois | Quoi faire |
|---|---|
| Plus aucune carte | Envoie `/statut`. Pas de réponse : relance le bot (section 7) |
| Les posts partent **sans image** | Le serveur n'arrive plus à dessiner les cartes. Lance la vérification : `ssh botuser@178.128.194.190 'cd ~/x-degen-bot && .venv/bin/python -m bot.main --check'` et lis la ligne « Images » |
| « Clés API X refusées », « plus de crédits » | Relance la commande de la section 5 : l'assistant détecte le problème et te guide |
| « ❌ Le bot ne peut pas démarrer » sur Telegram | Un réglage de `config/.env` est faux : le message dit lequel |
| « ⚠️ Réglage ignoré » au démarrage | Un horaire mal écrit : le bot utilise la valeur par défaut en attendant |
| « X refuse que le bot réponde » | Normal si la personne ne t'a pas répondu : utilise le bouton 📲 et réponds toi-même |
| « X n'a pas confirmé la publication » | Regarde ton profil X : le post y est peut-être déjà. Ne republie qu'après avoir vérifié |
| Telegram ne reçoit rien | Relance la commande de la section 5 : l'assistant vérifie Telegram et te guide |

Voir les dernières lignes du journal du bot :

```
ssh botuser@178.128.194.190 'tail -n 50 ~/x-degen-bot/logs/bot.log'
```

## 9. Chaque mois : la santé du compte

Ouvre **x.com/i/jf/under_the_hood** et regarde le rapport du mois : s'il affiche des **étiquettes**
(labels) sur le compte ou sur des posts, c'est que X te limite. Celui d'octobre, généré début novembre,
est le premier à surveiller de près. En cas d'étiquette, arrête tout ce qui ressemble à de l'automatique
et demande de l'aide.

## 10. Les réglages

Ils sont dans `config/.env` sur le serveur. Un horaire mal écrit n'arrête pas le bot : il reprend la valeur
par défaut et te prévient sur Telegram. Les jours s'écrivent `mon`…`sun`, `lun`…`dim` ou en toutes lettres
(`mardi`), les heures `14:00` ou `14h`. Pour modifier :
`ssh botuser@178.128.194.190`, puis `nano ~/x-degen-bot/config/.env` (Ctrl+O pour enregistrer, Ctrl+X pour
quitter), puis `sudo systemctl restart x-degen-bot`.

| Réglage | Par défaut | Rôle |
|---|---|---|
| `POST_SLOTS` | `tip@14:30,news@19:00` | posts du jour (type@heure : `tip` = l'IA au quotidien, `news` = l'actu en mots simples ; `gm` existe encore si tu y tiens) |
| `THREAD_SLOTS` | `tue@14:30,sun@16:00` | threads (jour@heure) |
| `DRAFT_LEAD_MINUTES` | `gm:660,tip:150,news:120,thread:240` | combien de minutes avant l'heure la carte arrive |
| `APPROVAL_GRACE_MINUTES` | `120` | délai de validation après l'heure prévue |
| `QUIET_HOURS` | `23:30-07:30` | messages sans sonnerie |
| `RADAR_ENABLED` / `RADAR_MAX_PER_DAY` | `true` / `8` | suggestions de réponses aux gros comptes |
| `COMMENTS_ENABLED` | `true` | réponses proposées aux commentaires |
| `REACTIVE_ENABLED` / `REACTIVE_MAX_PER_DAY` | `true` / `1` | actu à chaud, seulement si elle touche tout le monde |
| `REACTIVE_WINDOW` | `12:00-23:30` | heures où une actu à chaud peut partir |
| `SCREENSHOTS_ENABLED` | `true` | `false` = jamais de page « dans un smartphone », seulement des cartes dessinées |
| `X_BUDGET_MONTHLY_USD` / `X_BUDGET_DAILY_USD` | `30` / `1.50` | plafonds API X |
| `LLM_MODEL` | `claude-opus-5-5` | modèle Claude (`claude-sonnet-5-5` = moins cher) |
| `IMAGE_AI_ENABLED` | `false` | photos générées par IA (clé OpenAI nécessaire) |
| `DRY_RUN` | `false` | `true` = simulation, rien n'est publié |
| `APPROVAL_MODE` | `telegram` | `auto` publie sans toi (étiquette « Automated » obligatoire sur le profil, pas de rémunération des créateurs) |

La liste des comptes du radar et des sources d'actu est dans `config/settings.json` (presse et blogs
qui parlent des produits que tout le monde utilise ; `"filter": "ai"` ne garde que les articles sur l'IA
d'un flux généraliste). Les sujets de threads sont dans `config/content.json`.

Les couleurs des images (la « direction artistique ») sont dans `bot/cards.py`, bloc `THEMES` : pour suivre
une tendance, on y change une palette, rien d'autre.

---

### Pour les développeurs

- Architecture et choix techniques : [`DESIGN.md`](DESIGN.md).
- Tests : `python -m pytest` (tous les appels à Claude, X et Telegram sont simulés ; une journée complète
  est rejouée dans `tests/test_integration.py`).
- Vérification réelle de la configuration : `python -m bot.main --check` (dessine aussi une carte d'essai).
- Aperçu sans publier : `python -m bot.main --preview [tip,news,thread,reactive]` ; chiffres :
  `python -m bot.main --report`.
- Images : `bot/cards.py` (HTML/CSS rendu par Chromium, polices embarquées sous licence OFL dans
  `bot/assets/fonts/`). Tests de rendu réel : `XBOT_RENDER_TESTS=1 python -m pytest tests/test_cards.py`.
- Scripts d'installation : `deploy/` (testés dans `tests/test_deploy_scripts.py`).
