# Assistant X de @sabislabi — version 2

Claude rédige, **tu valides d'un geste sur Telegram** (ou le bot publie seul, en mode automatique), et tout
part à l'heure via l'**API officielle de X**.
Plus de navigateur qui se fait passer pour toi sur x.com, plus de réponses ni de likes automatiques.

**La ligne du compte (novembre 2026) : « le futur arrive plus vite qu'on ne le croit ».** On parle à
M. et Mme Tout-le-monde, pas aux passionnés de tech : chaque post raconte, avec des mots simples, une
nouveauté qui fait dire « attends, quoi ? » (un robot à la maison, une IA qui fait ce que seuls les humains
faisaient, un geste de tous les jours qui va disparaître) — incroyable, parfois un peu inquiétant, toujours
vrai. **Le compte donne la nouvelle lui-même**, comme un compte d'alertes info : jamais « d'après tel
site », jamais de lien. Chaque post part, quand elle existe, avec **un extrait de la vidéo officielle** de
l'actu, sinon **sa photo officielle**, prises à la source (sur la page de l'entreprise ou du labo),
jamais chez un média.

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
| **Le futur arrive** 🔮 : un geste de tous les jours qui va changer (« Bientôt, plus besoin de manette pour jouer »), raconté à partir d'une vraie nouveauté des derniers jours | 14 h 30 | 2 h 30 avant (45 min en mode automatique) |
| **L'histoire du jour** 📰 : la nouveauté du jour qui intrigue tout le monde (« Amazon vient de dévoiler une caméra volante qui patrouille seule chez vous ») | 19 h, et une 2e histoire à 23 h | 2 h avant (45 min en auto) |
| Thread 🧵 : une histoire racontée en grand le mardi et le jeudi, **« cette semaine, le futur s'est rapproché »** le dimanche | mardi et jeudi 14 h 30, dimanche 16 h | 4 h avant (1 h 30 en auto) |
| **Ça vient de tomber** ⚡ : seulement pour une histoire dont tout le monde va parler (grande annonce d'un nom connu, première mondiale) | 3 par jour au plus, entre 12 h et 23 h 30 | dès qu'elle tombe |
| **Vidéo explicative** 🎬 : une petite vidéo animée de 30 à 60 s qui explique une nouveauté, comme une émission de science pour toute la famille (texte en grand, étapes qui apparaissent, la photo de l'actu en fond) | lundi, mercredi et vendredi 14 h 30 sur ton serveur, à la place du « futur arrive » | 1 h 30 avant (en auto) |

Trois posts par jour, aux heures où le public anglophone est en ligne (14 h 30 = matin aux États-Unis,
19 h = leur pause de midi et la soirée en Europe, 23 h = leur après-midi). Rythme choisi par le
propriétaire le 18 novembre 2026 (avant : 2 posts, 2 threads, 1 « ça vient de tomber »). Chaque post
reste soigné : s'il n'y a rien d'assez fort, le créneau est sauté.

**La qualité avant le nombre** (choix du propriétaire, 19 novembre 2026) : les crédits Claude servent à
mieux faire, pas à publier plus.

- **Trois versions pour chaque post.** Claude écrit le post trois fois, séparément, en cherchant à
  chaque fois une autre accroche. Un second avis les compare et garde la meilleure ; c'est elle dont
  les faits sont vérifiés. Sur la carte Telegram : « meilleure de 3 versions ».
- **Plus de temps de réflexion** pour écrire chaque post, comme pour les threads.
- **L'actu chaude est regardée toutes les 15 minutes** (30 avant), et Claude peut être interrogé
  30 fois par jour (6 avant : le bot ne voyait plus rien le soir, à l'heure des annonces américaines).
  Le nombre de posts « ça vient de tomber » ne change pas : 3 par jour au plus.
- **Un « ça vient de tomber » peut partir près d'un post prévu** : 10 minutes d'écart au lieu d'une
  heure. Avant, le bot ne regardait pas l'actu chaude de 18 h à 20 h, et une annonce américaine de 19 h
  attendait 20 h. Entre deux posts « ça vient de tomber », l'écart reste d'une heure.

**Insister sur ce qui marche** (ta consigne du 23 novembre 2026 : « insiste seulement sur les posts qui
marchent, dans le même style, tant que ça marche, et regarde les tendances à chaque fois ») :

- **Le bot apprend de ses propres chiffres.** Chaque post est classé par sujet (jeux vidéo, IA qui étonne,
  vie quotidienne, gadgets…). Le bot calcule ce qui fait plus de vues et de réactions que d'habitude, en
  donnant plus de poids aux derniers jours : si un sujet ne marche plus, il est vite oublié. Le 23/11, ce
  qui marchait le mieux : l'IA qui fait quelque chose d'étonnant (1,7 fois plus de portée), les jeux vidéo
  (1,6x), la vie quotidienne (1,4x) ; moins bien : téléphones, lunettes et gadgets (0,8x).
- **Le rédacteur en chef le sait** quand il choisit l'histoire, et **le rédacteur voit nos trois meilleurs
  posts récents en entier**, pour écrire dans le même style.
- **Un post prévu sur quatre essaie autre chose** : sinon le compte ne découvrirait jamais ce qui pourrait
  marcher encore mieux.
- **Jamais les sujets pour initiés**, même quand ils marchent (1,3x le 23/11, sans doute l'ancien public
  tech du compte) : la ligne reste le grand public, sans jargon.
- **La veille du marché** (ton accord du 23/11 : tes crédits API couvrent la dépense) : une fois par
  jour, le bot lit les derniers posts de 5 gros comptes qui racontent la tech et l'IA à tout le monde, et
  montre au rédacteur en chef ceux qui font le plus réagir, pour s'inspirer du genre d'histoire, jamais
  pour les copier. Environ 0,25 $ par jour chez X, compensés : les statistiques de nos posts sont
  maintenant relues toutes les 6 heures au lieu de 3 (c'était le plus gros poste de dépense chez X).
- **Les tendances du jour, avant chaque choix** (gratuites, relues toutes les heures) : ce que le public
  cherche sur Google aux États-Unis et au Royaume-Uni, les pages Wikipédia les plus lues la veille, ce dont
  parle le public tech, et les annonces les plus aimées sur X. Une histoire liée à une tendance passe
  devant, à force égale. Exemple : le 22/11, GTA VI et l'hypothèse de Riemann étaient parmi les dix pages
  Wikipédia les plus lues… nos deux meilleurs posts du mois.

**À qui on parle.** Une infirmière, un chauffeur de taxi, un étudiant qui joue aux jeux vidéo, un parent,
un retraité : des gens qui ne suivent pas la tech et qui font défiler leur fil très vite. Ils ne s'arrêtent
que pour une chose : ce qui leur fait dire **« attends, quoi ? »**. Donc :

- la première ligne dit **ce que la chose FAIT**, ou ce qu'elle change dans la vie de tous les jours —
  pas le nom de l'entreprise ni un numéro de version ;
- on montre le futur comme **une scène du quotidien** (« Bientôt, plus besoin de manette pour jouer ») ;
- quand c'est à la fois incroyable et un peu inquiétant, on le dit ;
- **zéro jargon** : les mots comme « LLM », « API », « benchmark », « open-source » sont refusés par le
  bot, et une seconde lecture de Claude vérifie qu'un lecteur qui n'a jamais ouvert ChatGPT comprend tout ;
- **intriguer, jamais inventer** : chaque fait vient de l'article cité. Une démonstration reste « une
  démonstration », un prototype « un prototype » ; pas de date, de prix ni de catastrophe que la source
  n'annonce pas ;
- **c'est nous qui donnons la nouvelle** : le post dit le fait directement, sans jamais nommer le site, le
  journal ou le blog où le bot l'a lue (« TechCrunch reports », « according to The Verge » sont refusés
  d'office). Celui qui a fait l'annonce ou qui l'a dit, lui, est nommé : « OpenAI says ».

**Comment une histoire est choisie.** Le bot lit une quarantaine de sources (robots, jeux vidéo, réalité
virtuelle, gadgets, IA, voitures, espace…). Trois étapes à chaque post :

1. **Le rédacteur en chef** (Claude) lit une centaine de titres et garde les 2 ou 3 histoires qui
   intrigueront le plus quelqu'un qui ne suit pas la tech. Le lancement d'une IA que tout le monde connaît
   (ChatGPT, Claude, Gemini…) en fait toujours partie. Il écarte l'argent des entreprises, les outils
   pour programmeurs, les bons plans, les avis de produits.
2. Le bot **lit les articles en entier** (plus seulement les trois lignes du flux).
3. Claude **écrit** le post à partir de ces articles, puis une **seconde lecture** vérifie chaque fait,
   phrase de l'article à l'appui. Un chiffre qui n'est pas dans l'article est refusé d'office.

Le bot note aussi la **forme d'accroche** de chaque post (« ça peut faire quoi ? », « le chiffre », « bientôt,
plus besoin de… »…). Au fil des chiffres, il privilégie celles qui marchent sur ton compte, et il ne
recommence jamais deux posts de suite par les mêmes mots.

S'il n'y a rien d'assez fort, le créneau est sauté : mieux vaut pas de post qu'un post tiède.

**L'image : comme un compte d'alertes info.** Plus de capture d'écran d'article (depuis le 14 novembre
2026). Comme AlertesInfos, le bot poste l'image de l'actu **comme la sienne** (choix du propriétaire, 15
novembre 2026) :

- **La vidéo d'abord** : si la page officielle de l'actu (le blog de l'entreprise, le communiqué du labo)
  ou l'article publie un fichier vidéo, le bot en prend **les 30 premières secondes** et les met
  directement dans le post. Pas YouTube : YouTube interdit de télécharger ses vidéos, et un lien dans le
  post est exclu.
- **Sinon la photo** de l'actu : d'abord celle de la page officielle, sinon celle de l'article (la
  personne, le produit, la scène).
- **Souvent une 2e image à côté**, qui dit tout de suite de qui ou d'où on parle : **le logo de
  l'entreprise** (OpenAI, NVIDIA, Meta…) ou **le drapeau du pays** (un robot japonais → le drapeau du
  Japon). Claude décide si elle sert, et vérifie que c'est le bon logo ou le bon drapeau, actuel. Ces
  logos et drapeaux viennent de Wikimedia (fichiers libres). Jamais de 2e image avec une vidéo : X ne
  mélange pas vidéo et photos.
- Avant de servir, **Claude regarde** chaque image : elle montre ce dont parle le post, sans le nom, le
  logo, le bandeau ou le filigrane d'un autre média ou d'une agence, sans être une capture de page.
- Rien de sûr : la 2e image seule (le logo), sinon le post part **en texte seul**.
- Formats : vidéo MP4 (1280 px au plus, avec le son), photo JPEG (2048 px au plus), sans les
  informations cachées du fichier (appareil, lieu).
- Un sondage n'a pas d'image (X ne le permet pas).
- **Le risque, à connaître** : reprendre la photo d'un article sans autorisation n'est pas légal à
  proprement parler (droit d'auteur), mais c'est toléré et très répandu. Le risque concret n'est pas un
  procès : c'est qu'un photographe ou une agence demande à X de retirer un post, et qu'à force de
  retraits X suspende le compte. Si ça arrivait, `PRESS_MEDIA_ENABLED=false` (section 10) revient aux
  seules images officielles, et il suffit de supprimer le post concerné.

- **Les jours de thread**, le thread prend la place du post de 14 h 30 : jamais plus de 3 contenus
  prévus dans la journée (plus, au plus, 3 « ça vient de tomber »).
- Si Claude ne trouve rien de solide, le créneau est **sauté** (tu es prévenu) plutôt que rempli avec un
  texte creux. En mode automatique, aucun texte « de secours » n'est jamais publié tout seul.
- Chaque texte est **vérifié** par une seconde lecture de Claude, article en main : faits, chiffres,
  aucune exagération, aucune « expérience perso » inventée, et des mots que tout le monde comprend.
- Tu as jusqu'à **2 h après l'heure prévue** pour valider. Passé ce délai, la carte expire : **rien ne part**,
  même si tu appuies sur ✅ plus tard.
- Si le bot a été arrêté et qu'un post validé a plus de 3 h de retard, il ne part pas non plus
  (l'actu du matin à 16 h n'a plus de sens). Tu es prévenu.
- Si X ne confirme pas une publication (coupure réseau), le bot vérifie sur ton profil avant de réessayer :
  **jamais de doublon**.
- La nuit (23 h 30 - 7 h 30), les messages arrivent **sans sonnerie**.

**Les commentaires sous tes posts.** Quand quelqu'un répond à l'un de tes posts, le bot écrit une réponse
et **l'envoie tout seul 3 minutes plus tard** (X l'autorise, puisque cette personne t'a répondu). La carte
arrive sur Telegram avec un bouton ❌ : touche-le dans ces 3 minutes pour l'arrêter. Depuis le 18 novembre
2026 (`COMMENTS_AUTO_REPLY=true`). Avec `COMMENTS_AUTO_REPLY=false`, rien ne part sans ton ✅. Les mêmes
garde-fous que pour les posts s'appliquent : les commentaires hors ligne éditoriale, le bruit (emoji seul,
lien seul) et la liste noire sont ignorés, chaque réponse est relue par les filtres, et une seule réponse
par personne et par post dans la journée.

**Le radar 🎯 (coupé depuis le 18 novembre 2026, `RADAR_ENABLED=false`).** Aucune de ses 62 suggestions
n'avait servi, et c'est lui qui lisait le plus chez X (payé par ta carte). Quand il est allumé : quand un gros compte de ta niche vient de poster et que ça décolle, une carte arrive avec
une réponse suggérée. Le bouton **« Répondre dans X »** ouvre l'application X avec le texte déjà rempli :
tu relis, tu ajustes, **tu publies toi-même**. Le bot ne publie jamais ces réponses (c'est interdit et
c'est ce qui a fait supprimer 42 000 comptes). La suggestion reste valable 90 minutes : une réponse en
retard ne sert à rien.

**Les bilans.** Un récap chaque matin à 8 h 30, un bilan de la semaine le dimanche à 20 h 30.

**Voir ce que le bot écrirait, sans rien publier.** Depuis le Terminal :

```
ssh botuser@178.128.194.190 'cd ~/x-degen-bot && .venv/bin/python -m bot.main --preview'
```

Tu reçois sur Telegram les deux posts du jour rédigés à l'instant, **avec leur vidéo ou leur photo** (s'il y en a une), marqués
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

**API X** (paiement à l'usage, **payé par ta carte chez X** : les crédits Claude ne peuvent pas payer X) :
un post coûte 0,015 $, une réponse 0,01 $, la lecture d'un post 0,005 $.

- 3 posts par jour : environ 1,50 $ par mois ; 3 threads par semaine : environ 1,50 $ par mois ; les
  réponses automatiques aux commentaires : 1 centime chacune.
- Le reste sert à lire (comptes d'actu, statistiques, commentaires). Mi-novembre 2026, le bot dépensait
  environ 24 $ par mois chez X, surtout en lectures ; le radar, gros lecteur, est coupé depuis le 18/11.
- **Plafond du bot : 30 $ par mois.** Les lectures (radar…) ne peuvent jamais dépasser 75 % de ce plafond,
  réparties sur le mois : le dernier quart reste toujours disponible pour publier.
- Crédits offerts par X : 20 $ à l'ajout d'une carte, puis jusqu'à 50 $ (égaux à ta première recharge
  automatique).

**Claude** : avec le modèle par défaut (Opus 5.5), de l'ordre de **30 à 35 $ par mois** pour 2 posts par
jour écrits en une seule version. Chaque post demande quatre ou cinq appels : choisir l'histoire, écrire,
relire, trouver la source, regarder l'image. Avec le rythme et les réglages de qualité de ton serveur
(3 posts par jour, trois versions par post, plus de réflexion, actu chaude regardée plus souvent), compte
plutôt **2,60 à 3 $ par jour, soit environ 85 $ par mois** : mesuré du 19 au 21/11/2026 (un post coûte
0,25 $ quand la première version passe la relecture, jusqu'à 0,40 $ quand il faut le réécrire).
`/budget` affiche où tu en es.

- **Qui paie** : depuis le 18 novembre 2026, ces appels sont pris sur les **200 $ de crédits compris chaque
  mois dans ton forfait Claude Ultra** (associé au compte du bot sur la console Claude). Rien à recharger
  tant que le forfait Ultra est actif. Les crédits non utilisés ne se reportent pas d'un mois sur l'autre.
- **Jamais ta carte pour Claude** (consigne du propriétaire, 18/11/2026) : la recharge automatique est
  coupée sur la console Claude, et elle doit le rester. Quand les crédits gratuits sont épuisés, Claude
  s'arrête simplement d'écrire (le bot te prévient sur Telegram) ; rien n'est prélevé. Ne jamais acheter
  de crédits Claude pour le bot.
- **Le plafond : 195 $ par mois** (`CLAUDE_BUDGET_MONTHLY_USD`, section 10 ; 35 $ jusqu'au 18/11/2026).
  Le bot compte tout ce que Claude lui coûte, aperçus et vérifications compris. Si le mois devient juste,
  il met d'abord en pause le radar, les posts « ça vient de tomber » et les réponses aux commentaires, pour
  garder de quoi écrire les posts prévus. À 80 % il te prévient sur Telegram ; au plafond il n'écrit plus
  rien jusqu'au 1er.
- Les 200 $ de crédits sont partagés par tout ton compte API : avec ce plafond, le bot peut presque tout
  prendre. Ton forfait Ultra lui-même (Claude, Claude Code) n'est limité par rien de tout ça.
- **Deuxième sécurité, côté console Claude** : la clé du bot (« bot-x-serveur ») vit dans un espace à
  part, « Bot X », lui aussi limité à 195 $ par mois. Même si cette clé fuyait, elle ne pourrait pas
  dépenser plus. L'espace « Default » n'a aucune limite et le compte entier garde la sienne (500 $).
- `/budget` affiche où tu en es. Pour diviser la consommation environ par deux, mets
  `LLM_MODEL=claude-sonnet-5-5` dans les réglages (section 10).

## 5. Installation (une seule fois)

Tu n'as **qu'une chose à faire** : coller une ligne. L'assistant télécharge lui-même la dernière version,
fait le reste et te guide quand il a besoin de toi.

Ouvre l'app **Terminal** (Cmd + Espace, tape « Terminal »), colle cette ligne et appuie sur Entrée :

```
curl -fsSL https://raw.githubusercontent.com/7rg2r4rm29-dotcom/sabislabi-x-bot/main/deploy/install_from_github.sh | bash
```

(Ancienne méthode, toujours valable : bouton vert **Code → Download ZIP** sur GitHub, puis
`bash ~/Downloads/sabislabi-x-bot-main/deploy/push_from_mac.sh`.)

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

Quand une nouvelle version est prête : recolle la même ligne (section 5). Tes clés, l'historique du bot et
tes réglages sont conservés. Si tu as modifié `config/settings.json` ou `config/content.json`, tes
modifications sont gardées et la nouvelle version par défaut est posée à côté (`….nouveau`) ; seule la liste
des sites d'actualité (`news_feeds`), qui va avec la ligne éditoriale, est toujours mise à jour.

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
| Les posts partent **sans image** | Souvent normal : beaucoup d'actus n'ont pas de page officielle trouvable, ou pas d'image officielle utilisable (photo d'agence, logo…). Si c'est **tous** les posts pendant des jours, quelque chose bloque. Lance la vérification : `ssh botuser@178.128.194.190 'cd ~/x-degen-bot && .venv/bin/python -m bot.main --check'` et lis la ligne « Images » |
| « 💳 Claude ne peut plus écrire » | Le message dit pourquoi. **Plafond du mois** atteint (35 $ par défaut) : tout reprend le 1er, ou augmente `CLAUDE_BUDGET_MONTHLY_USD` (section 10). **Plus de crédit** sur la console Claude (platform.claude.com → Facturation) : vérifie que ton forfait Ultra est toujours actif, sinon achète des crédits. **Clé refusée** : elle a sans doute expiré (une clé peut avoir une date de fin, visible sur platform.claude.com → Clés API). Crée une nouvelle clé dans l'espace « Bot X » (case « Portée »), avec « Expire : Jamais », puis colle-la avec `ssh -t botuser@178.128.194.190 'bash ~/x-degen-bot/deploy/set_secrets.sh ANTHROPIC_API_KEY'` (rien ne s'affiche quand tu colles, c'est normal ; ne l'écris JAMAIS ailleurs), et relance le bot (section 7). Dans les deux premiers cas le bot reprend tout seul |
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
| `POST_SLOTS` | `tip@14:30,news@19:00` | posts du jour (type@heure : `tip` = « le futur arrive », `news` = « l'histoire du jour » ; `gm` existe encore si tu y tiens). Sur ton serveur depuis le 18/11/2026 : `tip@14:30,news@19:00,news@23:00` |
| `THREAD_SLOTS` | `tue@14:30,sun@16:00` | threads (jour@heure). Sur ton serveur depuis le 18/11/2026 : `tue@14:30,thu@14:30,sun@16:00` |
| `EXPLAINER_SLOTS` | vide | vidéos explicatives animées (jour@heure), à la place du post prévu le plus proche. Sur ton serveur depuis le 23/11/2026 : `mon@14:30,wed@14:30,fri@14:30` |
| `TRENDS_ENABLED` | `true` | regarder les tendances du jour (Google, Wikipédia, Hacker News, annonces X déjà lues) avant chaque choix d'histoire |
| `MARKET_WATCH_ENABLED` | `false` | veille du marché : une fois par jour, ce qui fait réagir chez 5 gros comptes du même sujet (environ 0,25 $ par jour chez X ; comptes dans `market_accounts` de `config/settings.json`). `true` sur ton serveur depuis le 23/11/2026 |
| `DRAFT_LEAD_MINUTES` | `gm:660,tip:150,news:120,thread:240,explainer:240` | combien de minutes avant l'heure la carte arrive |
| `APPROVAL_GRACE_MINUTES` | `120` | délai de validation après l'heure prévue |
| `QUIET_HOURS` | `23:30-07:30` | messages sans sonnerie |
| `RADAR_ENABLED` / `RADAR_MAX_PER_DAY` | `true` / `8` | suggestions de réponses aux gros comptes (`false` sur ton serveur depuis le 18/11/2026) |
| `COMMENTS_ENABLED` | `true` | réponses aux commentaires sous tes posts |
| `COMMENTS_AUTO_REPLY` | `false` | `true` = la réponse part seule 3 min après la carte (❌ pour l'arrêter) ; `true` sur ton serveur depuis le 18/11/2026 |
| `COMMENTS_MAX_DRAFTS_PER_DAY` | `40` | réponses aux commentaires par jour, au plus |
| `REACTIVE_ENABLED` / `REACTIVE_MAX_PER_DAY` | `true` / `1` | « ça vient de tomber » : seulement pour une histoire dont tout le monde va parler (`3` sur ton serveur depuis le 18/11/2026) |
| `REACTIVE_WINDOW` | `12:00-23:30` | heures où une actu à chaud peut partir |
| `REACTIVE_SLOT_GAP_MINUTES` | vide | minutes d'écart, au moins, entre un « ça vient de tomber » et un post prévu ; vide = une heure, comme entre deux « ça vient de tomber » (`10` sur ton serveur depuis le 19/11/2026) |
| `REACTIVE_CHECK_EVERY_MINUTES` | `30` | toutes les combien de minutes le bot regarde l'actu chaude (`15` sur ton serveur depuis le 19/11/2026) |
| `REACTIVE_MAX_CHECKS_PER_DAY` | `6` | fois par jour, au plus, où Claude est interrogé sur l'actu chaude (`30` sur ton serveur depuis le 19/11/2026 : à 6, le bot ne voyait plus rien le soir) |
| `POST_VERSIONS` | `1` | versions écrites pour chaque post ; un second avis garde la meilleure (`3` sur ton serveur depuis le 19/11/2026 ; 5 au plus) |
| `LLM_EFFORT_POST` | vide | temps de réflexion de Claude pour écrire un post : `low`, `medium`, `high`, `xhigh` ou `max` ; vide = le réglage général (`high` sur ton serveur depuis le 19/11/2026) |
| `VIDEOS_ENABLED` | `true` | `false` = jamais d'extrait vidéo : seulement des photos |
| `PRESS_MEDIA_ENABLED` | `true` | reprendre la photo ou la vidéo de l'article, comme un compte d'alertes info ; `false` = seulement les images des pages officielles (le mode prudent) |
| `SECOND_IMAGE_ENABLED` | `true` | la 2e image à côté de la photo : logo de l'entreprise ou drapeau du pays |
| `PHOTOS_ENABLED` | `true` | `false` = aucune photo (l'ancien nom `SCREENSHOTS_ENABLED` marche encore) ; avec `VIDEOS_ENABLED=false`, tous les posts partent en texte seul |
| `X_BUDGET_MONTHLY_USD` / `X_BUDGET_DAILY_USD` | `30` / `1.50` | plafonds API X |
| `CLAUDE_BUDGET_MONTHLY_USD` | `35` | plafond Claude du mois, en dollars (`0` = pas de plafond ; `195` sur ton serveur depuis le 18/11/2026) |
| `LLM_MODEL` | `claude-opus-5-5` | modèle Claude (`claude-sonnet-5-5` = moins cher) |
| `IMAGE_AI_ENABLED` | `false` | photos générées par IA (clé OpenAI nécessaire) |
| `DRY_RUN` | `false` | `true` = simulation, rien n'est publié |
| `APPROVAL_MODE` | `telegram` | `auto` publie sans toi (étiquette « Automated » obligatoire sur le profil, pas de rémunération des créateurs) |

La liste des comptes du radar et des sources d'actu est dans `config/settings.json` (presse grand public,
science et tech ; `"filter": "tech"` ne garde d'un flux généraliste que ce qui touche à la technologie,
`"filter": "ai"` que ce qui parle d'IA). Les thèmes de threads sont dans `config/content.json`.

---

### Pour les développeurs

- Architecture et choix techniques : [`DESIGN.md`](DESIGN.md).
- Tests : `python -m pytest` (tous les appels à Claude, X et Telegram sont simulés ; une journée complète
  est rejouée dans `tests/test_integration.py`).
- Vérification réelle de la configuration : `python -m bot.main --check` (met aussi une photo d'essai au
  propre et tire un extrait d'une vidéo d'essai).
- Aperçu sans publier : `python -m bot.main --preview [tip,news,thread,reactive]` ; chiffres :
  `python -m bot.main --report`.
- Image de l'actu : `bot/visuals.py` (page officielle trouvée via les liens de l'article, puis
  l'article lui-même ; vidéo en fichier → extrait de 30 s par ffmpeg (paquet imageio-ffmpeg), sinon
  photo ; 2e image logo / drapeau via Wikidata et Commons ; téléchargements bornés, regard de Claude).
  Garde-fou « on donne la nouvelle » : `filters.news_outlet_cited`.
- Rédaction en deux temps : `bot/writer.py` (`_pick` : le choix de l'histoire sur les titres ;
  `_read_articles` + `sources.fetch_article_text` : le texte des articles ; puis rédaction et relecture).
- Scripts d'installation : `deploy/` (testés dans `tests/test_deploy_scripts.py`).
