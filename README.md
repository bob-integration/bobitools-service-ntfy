# Notifications (ntfy) — service Bobi.Tools

Notifications **push** auto-hébergées pour [Bobi.Tools](https://github.com/bob-integration/bobitools) :
le service lance un serveur [ntfy](https://ntfy.sh) local et les outils y poussent leurs alertes,
reçues sur les téléphones et navigateurs abonnés du LAN ou du VPN.

Un service n'apparaît pas au lanceur : il se règle dans **Réglages → Système → Notifications**
(si l'onglet n'y figure pas, version plus ancienne du cœur : adresse directe `/settings#ntfy`).

## Ce que fait le service

- **Gère le conteneur ntfy** : téléchargement de l'image (`binwiederhier/ntfy:latest` par
  défaut), démarrage, arrêt, données dans un volume Docker.
- **Le publie sur le port 2586** (réglable, sur toutes les interfaces par défaut) : les
  téléphones s'y connectent directement, sans passer par Bobi.Tools.
- **Active l'authentification** : accès par défaut `deny-all`, compte administrateur créé à
  partir des réglages, jeton de publication généré côté ntfy.
- **Sert les outils** par `ctx.notify(message, topic=…, title=…, priority=…, tags=…)`
  (in-process) ou `POST /api/ntfy/publish` avec une session ou l'en-tête `X-BT-Ntfy-Token`
  (outils en conteneur).
- **N'attend pas** : la publication passe par une file d'attente et est journalisée au nom de
  « Service ntfy ». Un bouton « Envoyer un test » vérifie la chaîne tout de suite.

## À savoir

- En `deny-all`, chaque abonné utilise le compte administrateur (ou un compte créé à la main
  dans le conteneur) : pas encore de comptes par personne.
- Prévu pour le LAN ou un VPN : pas de TLS ni de reverse-proxy intégrés.

## Prérequis

- Bobi.Tools avec **Docker** : le serveur ntfy tourne en conteneur.
- Accès au registre Docker pour tirer l'image au premier démarrage.

## Installation

Dans Bobi.Tools : **Réglages → Outils → Catalogue**, bouton « Installer ». Ou, sur une machine
neuve, en une ligne :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bob-integration/bobitools/main/get.sh) --outils ntfy
```

Un service se charge au démarrage : redémarrer Bobi.Tools après l'installation. Il est
désactivé par défaut. Renseigner le mot de passe administrateur avant d'activer : sans lui,
ni le compte ni le jeton de publication ne sont créés.

## Sécurité

- Le port ntfy est **publié sur le réseau**, pas seulement en local : le limiter au LAN ou au
  VPN.
- Le mot de passe administrateur et les jetons sont masqués dans l'interface.

## Licence et attribution

Ce service est sous GPL-3.0-or-later — © 2026 BOBI SAS. Voir [LICENSE](LICENSE).

Le serveur **ntfy** (Apache-2.0 / GPLv2, Philipp Heckel et contributeurs) n'est ni inclus ni lié :
son image officielle est tirée au déploiement et lancée comme programme séparé, à qui l'on parle
en HTTP. Le détail est dans [`ATTRIBUTION.md`](ATTRIBUTION.md).

## In English

Self-hosted **push notifications** for Bobi.Tools. The service runs a local ntfy server in
Docker (port 2586 by default, `deny-all` access, admin account and publish token provisioned
automatically); tools publish through `ctx.notify(...)` or `POST /api/ntfy/publish` with a
shared token, and phones or browsers on the LAN/VPN subscribe directly. ntfy itself is
Apache-2.0/GPLv2 and only run as a separate container — see `ATTRIBUTION.md`.
