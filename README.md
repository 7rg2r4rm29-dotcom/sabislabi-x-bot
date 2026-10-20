# Assistant X de @sabislabi — version 2

Claude rédige, **tu valides d'un geste sur Telegram**, le bot publie à l'heure via l'**API officielle de X**.
Plus de navigateur qui se fait passer pour toi sur x.com, plus de réponses ni de likes automatiques.

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
| ✅ | Valider : partira à l'heure prévue (ou tout de suite si l'heure est passée) |
| ✏️ | Modifier : appuie, puis **réponds** au message qui s'affiche avec ton texte |
| 🔄 | Autre version, écrite par Claude (3 fois maximum) |
| ❌ | Annuler (même après validation, tant que ce n'est pas parti) |

**Le planning par défaut (heure de Paris)**

| Contenu | Publication | La carte arrive vers |
|---|---|---|
| GM (petit post du matin) | 8 h 30 | 21 h 30 la veille |
| Astuce IA | 14 h | 11 h 30 |
| Actu IA décryptée | 18 h 30 | 16 h 30 |
| Thread | mardi 15 h et dimanche 17 h | 4 h avant |
| Post réactif (actu brûlante) | au plus 1 par jour, seulement si ça vaut le coup | quand ça arrive |

- Tu as jusqu'à **2 h après l'heure prévue** pour valider. Passé ce délai, la carte expire : **rien ne part**.
- Si le bot a été arrêté et qu'un post validé a plus de 3 h de retard, il ne part pas non plus
  (un GM à 16 h n'a pas de sens). Tu es prévenu.
- La nuit (23 h 30 - 7 h 30), les messages arrivent **sans sonnerie**.

**Les commentaires sous tes posts.** Quand quelqu'un répond à l'un de tes posts, une carte arrive avec une
réponse proposée. ✅ = le bot répond pour toi (X l'autorise, puisque cette personne t'a répondu).

**Le radar 🎯.** Quand un gros compte de ta niche vient de poster et que ça décolle, une carte arrive avec
une réponse suggérée. Le bouton **« Répondre dans X »** ouvre l'application X avec le texte déjà rempli :
tu relis, tu ajustes, **tu publies toi-même**. Le bot ne publie jamais ces réponses (c'est interdit et
c'est ce qui a fait supprimer 42 000 comptes). La suggestion reste valable 90 minutes : une réponse en
retard ne sert à rien.

**Les bilans.** Un récap chaque matin à 8 h 30, un bilan de la semaine le dimanche à 20 h 30.

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

- 3 posts par jour : environ 1,40 $ par mois ; 2 threads par semaine : environ 1 $ par mois.
- Le reste sert à lire (radar, comptes d'actu, statistiques).
- **Plafond du bot : 30 $ par mois.** Les lectures (radar…) ne peuvent jamais dépasser 75 % de ce plafond,
  réparties sur le mois : le dernier quart reste toujours disponible pour publier.
- Crédits offerts par X : 20 $ à l'ajout d'une carte, puis jusqu'à 50 $ (égaux à ta première recharge
  automatique).

**Claude** : à toi de surveiller sur console.anthropic.com ; `/budget` affiche une estimation. Le bot utilise
le meilleur modèle (Opus 5.5). Pour diviser la facture environ par deux, mets `LLM_MODEL=claude-sonnet-5-5`
dans les réglages (section 9).

## 5. Installation (une seule fois)

### Étape 1 : changer TOUTES les clés (obligatoire)

Tes clés ont pu être exposées lors du vol du 9 octobre, et elles traînent en clair dans le dossier du Mac
synchronisé avec iCloud. On en fait de nouvelles, et on supprime les anciennes.

**Ne colle jamais ces clés dans une conversation** (Claude, Cowork, mail, Telegram…). Garde-les juste
le temps de l'installation, par exemple dans l'app Notes, puis efface-les.

1. **X** : sur **console.x.com**, ouvre ton app.
   - *Settings → User authentication settings* : mets **App permissions = Read and write**, enregistre.
   - *Keys and tokens* : **API Key and Secret → Regenerate** (2 valeurs : API Key, API Key Secret).
   - *Keys and tokens* : **Access Token and Secret → Regenerate** (2 valeurs). À faire **après** le passage
     en « Read and write », sinon le jeton ne pourra pas publier. Vérifie que tu es connecté en tant que
     **@sabislabi**.
   - *Billing* : ajoute une carte bancaire, et un plafond mensuel si la console le propose.
2. **Claude** : sur **console.anthropic.com → API Keys**, crée une nouvelle clé, puis **supprime l'ancienne**.
3. **Telegram** : dans Telegram, écris à **@BotFather** : `/revoke`, choisis ton bot, copie le nouveau jeton.
4. **OpenAI** (seulement si tu veux des photos générées par IA) : platform.openai.com → API keys.
5. **Ton compte X** : change ton mot de passe, puis *Paramètres → Sécurité et accès au compte → Applications
   et sessions → Sessions* : **déconnecte toutes les autres sessions** (dont celle de l'ancien robot).

### Étape 2 : télécharger le code

Sur **github.com/7rg2r4rm29-dotcom/sabislabi-x-bot** : bouton vert **Code → Download ZIP**.
Safari le décompresse tout seul dans *Téléchargements* (dossier `sabislabi-x-bot-main`). Avec Chrome,
double-clique sur le fichier zip.

### Étape 3 : une seule commande

Ouvre l'app **Terminal** (Cmd + Espace, tape « Terminal »), colle cette ligne et appuie sur Entrée :

```
bash ~/Downloads/sabislabi-x-bot-main/deploy/push_from_mac.sh
```

1. Le code part sur le serveur (`ENVOI_OK`).
2. Question « As-tu de nouvelles clés à enregistrer ? » : tape **o**. Colle chaque clé quand on te la
   demande, puis Entrée. **Rien ne s'affiche quand tu colles, c'est normal.** Entrée sans rien coller =
   garder la clé actuelle. Les clés vont directement sur le serveur, nulle part ailleurs.
3. L'installation se déroule en 5 étapes (environ 5 minutes). Elle se termine par :

```
V2_OK : installation terminée, le bot tourne.
```

Sur Telegram, tu reçois « ✅ Test v2 OK ». Envoie `/statut` pour voir le planning.

**Si ça ne se termine pas par V2_OK :**

- `PAS MAINTENANT` : une publication est en cours. Relance la même commande 20 minutes plus tard
  (réponds **N** à la question des clés). Rien n'a été modifié.
- `ÉCHEC : ancienne version remise et relancée` : rien n'est perdu. Lis les lignes ❌ juste au-dessus, elles
  disent quoi corriger (le plus souvent une clé mal copiée ou des crédits X manquants), puis relance.

### Étape 4 : le grand ménage (2 ou 3 jours plus tard, quand tout va bien)

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
*Téléchargements*, retélécharge le ZIP (étape 2), relance la même commande (étape 3) et réponds **N** à la
question des clés. Tes réglages, tes clés et l'historique du bot sont conservés.

## 7. Revenir en arrière, mettre en pause, arrêter

- **Pause** (le bot continue de tourner mais ne publie plus) : `/pause` sur Telegram, puis `/reprendre`.
- **Revenir à la version précédente** (5 sauvegardes sont gardées) :

  ```
  ssh botuser@178.128.194.190 'bash ~/x-degen-bot/deploy/restore_backup.sh'
  ```

- **Arrêter complètement / relancer** :

  ```
  ssh botuser@178.128.194.190 'sudo systemctl stop x-degen-bot'
  ssh botuser@178.128.194.190 'sudo systemctl start x-degen-bot'
  ```

## 8. En cas de problème

| Ce que tu vois | Quoi faire |
|---|---|
| Plus aucune carte | Envoie `/statut`. Pas de réponse : relance le bot (section 7) |
| « Clés API X refusées » | Régénère l'Access Token après le passage en « Read and write », puis relance l'étape 3 en répondant **o** |
| « Plus de crédits API X » | Recharge sur console.x.com (*Billing*) |
| « X refuse que le bot réponde » | Normal si la personne ne t'a pas répondu : utilise le bouton 📲 et réponds toi-même |
| Telegram ne reçoit rien | As-tu écrit `/start` à ton bot ? Le jeton a-t-il été changé sans relancer l'étape 3 ? |

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

Ils sont dans `config/.env` sur le serveur. Pour modifier :
`ssh botuser@178.128.194.190`, puis `nano ~/x-degen-bot/config/.env` (Ctrl+O pour enregistrer, Ctrl+X pour
quitter), puis `sudo systemctl restart x-degen-bot`.

| Réglage | Par défaut | Rôle |
|---|---|---|
| `POST_SLOTS` | `gm@08:30,tip@14:00,news@18:30` | posts du jour (type@heure) |
| `THREAD_SLOTS` | `tue@15:00,sun@17:00` | threads (jour@heure) |
| `DRAFT_LEAD_MINUTES` | `gm:660,tip:150,news:120,thread:240` | combien de minutes avant l'heure la carte arrive |
| `APPROVAL_GRACE_MINUTES` | `120` | délai de validation après l'heure prévue |
| `QUIET_HOURS` | `23:30-07:30` | messages sans sonnerie |
| `RADAR_ENABLED` / `RADAR_MAX_PER_DAY` | `true` / `8` | suggestions de réponses aux gros comptes |
| `COMMENTS_ENABLED` | `true` | réponses proposées aux commentaires |
| `REACTIVE_ENABLED` / `REACTIVE_MAX_PER_DAY` | `true` / `1` | post sur une actu brûlante |
| `X_BUDGET_MONTHLY_USD` / `X_BUDGET_DAILY_USD` | `30` / `1.50` | plafonds API X |
| `LLM_MODEL` | `claude-opus-5-5` | modèle Claude (`claude-sonnet-5-5` = moins cher) |
| `IMAGE_AI_ENABLED` | `false` | photos générées par IA (clé OpenAI nécessaire) |
| `DRY_RUN` | `false` | `true` = simulation, rien n'est publié |
| `APPROVAL_MODE` | `telegram` | `auto` publie sans toi : **déconseillé** (étiquette « Automated » obligatoire, pas de rémunération) |

La liste des comptes du radar et des sources d'actu est dans `config/settings.json`.

---

### Pour les développeurs

- Architecture et choix techniques : [`DESIGN.md`](DESIGN.md).
- Tests : `python -m pytest` (tous les appels à Claude, X et Telegram sont simulés ; une journée complète
  est rejouée dans `tests/test_integration.py`).
- Vérification réelle de la configuration : `python -m bot.main --check`.
- Scripts d'installation : `deploy/` (testés dans `tests/test_deploy_scripts.py`).
