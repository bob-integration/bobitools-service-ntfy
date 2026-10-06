# Attribution — serveur ntfy

Ce service **héberge** le serveur **ntfy** (https://ntfy.sh — dépôt
https://github.com/binwiederhier/ntfy) en le lançant comme **conteneur Docker séparé**
(image officielle `binwiederhier/ntfy`). Bobi.Tools ne fait que **démarrer** ce serveur et
**publier** des notifications vers lui **en HTTP** : il n'y a **aucune combinaison ni liaison de
code** — c'est de la **simple agrégation** de deux programmes indépendants.

- **ntfy** est distribué sous **double licence Apache-2.0 / GPLv2** par Philipp Heckel
  (binwiederhier) et ses contributeurs. L'auto-hébergement est explicitement prévu par le projet.
- **Bobi.Tools** (dont ce service) est sous **GPL-3.0-or-later**.

Comme on ne lie pas le code de ntfy (GPLv2, non compatible GPLv3) dans le nôtre — on ne fait que
l'exécuter comme processus séparé et lui parler en HTTP — il n'y a pas de conflit de licences.
L'image Docker officielle est **tirée au déploiement** (non redistribuée dans le zip de
distribution de Bobi.Tools).

Merci à l'auteur et aux contributeurs de ntfy pour ce serveur de notifications ouvert.
