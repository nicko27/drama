# Drama Downloader (macOS)

Télécharge uniquement les ressources accessibles sans DRM. Application locale Qt, sans compte.


## Version évolutive : multi-sources et téléchargement généraliste

Cette version propose deux onglets **Analyseur de liens** et **Téléchargements**.
On peut analyser un lien de drama déjà pris en charge ou l'URL d'un fichier
HTTP/HTTPS direct (ZIP, PDF, MP4, images, archives et autres formats reconnus).
Une URL de **vidéo seule** compatible avec yt-dlp peut également être analysée
manuellement. Les playlists vidéo génériques, les flux en direct et certains
hébergeurs nécessitent encore un connecteur spécialisé.

Une URL de fichier direct crée une tâche d'un seul élément **sans fusion**.
Le moteur HTTP conserve un fichier \`.part\`, reprend seulement si le serveur
fournit un validateur exploitable (ETag ou Last-Modified), protège les fichiers
locaux existants et écrit un manifeste de vérification. Les séries continuent
à utiliser yt-dlp et FFmpeg.

L'application reconnaît certains blocages Cloudflare et propose, lorsque
possible, une **fenêtre de vérification humaine**. Un navigateur Qt intégré est
disponible **en option** avec PySide6-Addons :

\`\`\`sh
.venv/bin/python -m pip install PySide6-Addons
\`\`\`

Sans ce module, un dialogue permet d'ouvrir le navigateur système puis de
réessayer. **La vérification dans le navigateur ne garantit pas que les
requêtes urllib ou yt-dlp seront autorisées** : si le site bloque encore, le
logiciel signale l'échec. Aucun CAPTCHA ni DRM n'est contourné.

Les séries sont identifiées par leur source et leur ID. Les dossiers restent
stables après renommage ; les anciens dossiers reconnus sont préservés.
Un MP4 fusionné n'est réutilisé qu'avec un manifeste correspondant et une
empreinte SHA-256 valide. Un ancien fichier non vérifié n'est pas écrasé : un
nouveau résultat porte un suffixe \`_Corrige_N\`.

### État du développement

- **Réalisé** : stabilisation des séries, fusion sécurisée, plusieurs plateformes
  de dramas, téléchargements directs, extraction vidéo générique simple,
  reprise et interface à deux onglets.
- **À développer** : import de playlists génériques, SQLite, planificateur avancé,
  plugins externes, intégration robuste des sessions navigateur et packaging.
- **Validation** : tests automatiques hors écran sur Linux avec FFmpeg. Les
  parcours graphiques macOS et la disponibilité des sites en direct demandent
  une vérification distincte.


## Démarrage

Installer FFmpeg (`brew install ffmpeg`) et Python 3.12 / [uv](https://docs.astral.sh/uv/), puis lancer `./start.sh` (ou `sh start.sh`). Le premier lancement installe PySide6 et yt-dlp dans `.venv`.

Coller une URL `https://idrama.dramafren.org/index.php?page=watch&id=…`, cliquer **Analyser**, vérifier le titre, la plage et les options, puis **Ajouter à la file** et **Démarrer**. Le titre peut être indisponible si la page Web refuse l'accès ; dans ce cas le nom provisoire `Série_ID` est modifiable par double-clic dans la file. Le total d'épisodes provient de l'API, sans nombre fixe.

Les pages IDrama `page=detail&id=…` sont également acceptées : l’analyse commence à l’épisode 1 pour obtenir le total réel. Les liens `dramabox.dramafren.org` avec `sv=…` utilisent l’API accessible `get_video` sur `cdn-dramabox.dramafren.org`. Le titre et le nombre d’épisodes proviennent des métadonnées publiques de `dramaboxdb.com` ; **la disponibilité est vérifiée auprès de DramaFren pour chaque épisode**, sans reprendre le statut d’accès d’un catalogue tiers. Pour `42000023176`, « My Cruel Mate » compte 62 épisodes ; les épisodes 10, 20 et 62 répondaient sur DramaFren lors des tests. Si un épisode ne répond plus, il est signalé en erreur et la fusion n’a pas lieu. Aucun DRM ni contrôle d’accès n’est contourné.

Les liens `reelshort.dramafren.org` avec un identifiant hexadécimal sont reconnus. Le titre et le nombre d’épisodes peuvent être lus sur le catalogue public ReelShort ; **cela ne donne pas accès aux vidéos DramaFren**. Tant que son point d’accès vidéo n’est pas connu et vérifié, le lien reste en erreur explicite dans la liste, sans faux téléchargement.

Les liens `moboreels.dramafren.org/?api_route=detail&…` sont reconnus comme des fiches **MoboReels** (et non ReelShort). Cette route ne fournit pas une vidéo et répond actuellement avec un défi Cloudflare ici. Sans point d’accès vidéo MoboReels identifié, ils restent en erreur explicite dans la file.

Les liens `reelfren.net/watch/anyreel/…` utilisent `/api/detail` pour le titre et le nombre réel d'épisodes, puis `/api/video` pour renouveler le flux HLS de chaque épisode. L’API `/api/likes` et son `userId` ne sont pas utilisés. Si un épisode est signalé comme restreint ou sans vidéo, il reste en erreur et aucune fusion incomplète n'est lancée.

Les liens `kiss18.vip/watch/<série>/<épisode>` lisent le catalogue public pour déterminer les épisodes réellement listés, puis chargent le JSON de lecture de l'épisode demandé. Pour « Forbidden Guest House », 27 épisodes sont listés et jouables ; l'épisode 1 a été téléchargé intégralement et fusionné en MP4 (59,2 s). L'application ne contourne pas les accès restreints et signale les épisodes sans lien HLS direct ; elle ne déduit pas leur disponibilité future du seul catalogue.

Pour la nouvelle série AnyReel « Chained to the Dragon: The King's Forbidden Pet », `/api/detail` indique 59 épisodes ; l'analyse de l'épisode 1 et un extrait réel de 3 s ont été validés, **pas** le téléchargement des 59 épisodes. Pour la page ReelShort `6a31012f0a4db3ff73065e0f`, la playlist M3U8 transmise répond pour un épisode, mais ne permet pas de retrouver les autres : l'analyse de la série reste indisponible. Pour HDrama « Behind the Open Curtains », la page répond HTTP 403 et le proxy M3U8 fourni HTTP 451 : aucun contournement n'est tenté.

Si un lien DramaBox ajouté avant cette correction apparaît déjà **en erreur** dans la file, sélectionnez sa ligne et cliquez sur **Réessayer l’analyse** : l’application utilisera alors `get_video`.

Pour une plage limitée (par exemple 1–9 sur 62), la fusion contient uniquement les épisodes sélectionnés et porte le suffixe `_Episodes_01-09.mp4`, **pas** `_Complet.mp4`. Elle ne démarre pas si un épisode de la plage sélectionnée manque.

Pour une série DramaBox ajoutée avant la suppression de l’ancienne limite à 9, sélectionnez-la, réglez **à 62**, puis cliquez sur **Appliquer à la série**. Les épisodes déjà téléchargés et vérifiés seront conservés et ignorés à la reprise.

Pendant que l’application est ouverte, même en arrière-plan, **Détecter les liens copiés** (activé par défaut) ajoute chaque nouvelle série copiée à la liste, puis l’analyse en arrière-plan. Les doublons d’identifiant sont ignorés. Le champ URL est prérempli s’il est vide ; sinon **Utiliser le lien copié** propose le nouveau lien sans effacer le brouillon. La détection lit uniquement le presse-papiers : elle n’en retire rien et n’interfère pas avec JDownloader. Elle peut être désactivée en décochant la case. L’icône est fournie dans `assets/`.

**Démarrer** lance les séries prêtes dans l’ordre de la file, même si d’autres liens sont encore en cours d’analyse (une série après l’autre, jusqu’à trois épisodes simultanés selon le réglage). Un lien impossible à analyser reste visible en **erreur** : sélectionnez-le puis **Réessayer l’analyse** ou **Retirer la sélection**. Sélectionnez une série pour modifier sa plage d’épisodes et ses options, puis cliquez sur **Appliquer à la série**. Les liens en attente sont aussi enregistrés et récupérés après fermeture. Aucun téléchargement ne démarre à la copie.

Pour accélérer, cliquez sur **Rapide (2)** : deux épisodes sont alors téléchargés en parallèle (réglage modifiable de 1 à 3, **1** par défaut). Lors d’un essai réel sur deux épisodes accessibles, cela a pris 15,6 s contre 29,4 s séquentiellement ; le gain dépend du réseau et du serveur. Augmenter les fragments HLS simultanés était plus lent lors de cet essai et n’est donc pas activé. La fusion conserve ses vérifications d’intégrité.

La file est enregistrée dans `~/Library/Application Support/DramaFren Downloader/queue.json`. Les fichiers restent dans le dossier de destination : les nouveaux packages utilisent un nom stable `source_ID/` ; les anciens dossiers `Titre_ID/` sont conservés lorsqu'ils sont identifiés. Une relance vérifie les vidéos avec ffprobe et reprend les fragments yt-dlp. **Pause** attend la fin des transferts en cours ; **Arrêter** interrompt les processus et préserve les fragments. L’option de suppression des épisodes n’agit qu’après validation de la fusion. En cas d’échec, lire le journal et cliquer à nouveau sur **Démarrer**.

Tests : `.venv/bin/python -m unittest discover -s tests -v`.
# drama
