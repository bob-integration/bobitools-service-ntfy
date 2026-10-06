# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Service Notifications (ntfy) — serveur push auto-hébergé pour le compte des outils.

Jumeau « push » du service Mail : au lieu d'un e-mail, un outil (ou le code serveur) pousse
une notification vers les téléphones/navigateurs abonnés. On HÉBERGE le serveur ntfy
(https://ntfy.sh, Apache-2.0/GPLv2) sous forme de conteneur Docker local ; on ne fait que le
LANCER et publier via HTTP — aucune combinaison de code (simple agrégation, cf. ATTRIBUTION.md).

Architecture :
  · le service GÈRE le conteneur ntfy (pull/run/stop) via le pilote Docker de l'app. Le serveur
    est publié sur un port fixe du LAN (0.0.0.0:<ntfy_port>) pour que les téléphones l'atteignent
    directement (l'app ne le proxifie PAS : ntfy a besoin d'un flux SSE/WebSocket direct).
  · AUTH activée : `auth-default-access=deny-all`, un utilisateur admin provisionné (identifiants
    des réglages), et un TOKEN d'accès (créé côté ntfy) que les outils utilisent pour publier.

Deux chemins d'accès pour les outils (comme le service Mail) :
  · in-process → `ctx.notify(message, topic=..., title=..., priority=..., tags=...)`
  · HTTP       → `POST /api/ntfy/publish` (session OU header X-BT-Ntfy-Token) pour les outils Docker.

Publication ASYNCHRONE best-effort (file + worker) : une alerte ratée ne casse jamais le flux
appelant. Le bouton « test » de l'UI est synchrone pour un retour pass/échec immédiat.
"""
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.request

from app import settings
from app import docker_driver as dk
from app.database import audit_log

log = logging.getLogger(__name__)

NTFY_ACTOR = "Service ntfy"
CONTAINER = "bobi-ntfy"                 # nom du conteneur géré
VOLUME = "bobi-ntfy-data"              # volume nommé (persistance : auth.db, cache, pièces jointes)
INTERNAL_PORT = 80                     # port ntfy DANS le conteneur (défaut)
_TOKEN_RE = re.compile(r"tk_[A-Za-z0-9]+")

# Priorités ntfy : nom → niveau (l'en-tête Priority accepte 1..5 ou les noms).
_PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}

# ── File d'attente + worker (jumeau du service Mail) ─────────────
_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()
_STOP = object()

_state_lock = threading.Lock()
_last = {"ok": None, "detail": "", "at": None}

# Sérialise les opérations Docker (ensure/apply/provision) : elles ne doivent pas se chevaucher.
_docker_lock = threading.RLock()


def _set_last(ok, detail):
    from datetime import datetime
    with _state_lock:
        _last["ok"] = ok
        _last["detail"] = detail[:500]
        _last["at"] = datetime.now().isoformat(timespec="seconds")


# ═════════════════════════════════════════════════════════════════════
# Cycle de vie du conteneur ntfy
# ═════════════════════════════════════════════════════════════════════

def _cfg():
    """Configuration courante du conteneur, depuis les réglages."""
    port = int(settings.get("ntfy_port") or 2586)
    bind = (settings.get("ntfy_bind") or "0.0.0.0").strip() or "0.0.0.0"
    access = (settings.get("ntfy_default_access") or "deny-all").strip()
    if access not in ("deny-all", "read-only", "read-write"):
        access = "deny-all"
    return {
        "image": (settings.get("ntfy_image") or "binwiederhier/ntfy:latest").strip(),
        "port": port,
        "bind": bind,
        "access": access,
        "base_url": (settings.get("ntfy_base_url") or f"http://{bind}:{port}").strip(),
    }


def _signature(cfg):
    """Empreinte de la config qui, si elle change, impose de recréer le conteneur."""
    return f"{cfg['image']}|{cfg['bind']}:{cfg['port']}|{cfg['access']}|{cfg['base_url']}"


def _run_container(cfg):
    """(Re)crée et lance le conteneur ntfy, publié sur le LAN. Lève dk.DockerError sur échec."""
    dk.rm_container(CONTAINER, force=True, ignore=True)
    env = {
        "NTFY_BASE_URL": cfg["base_url"],
        "NTFY_LISTEN_HTTP": ":80",
        "NTFY_CACHE_FILE": "/var/lib/ntfy/cache.db",
        "NTFY_AUTH_FILE": "/var/lib/ntfy/auth.db",
        "NTFY_AUTH_DEFAULT_ACCESS": cfg["access"],
        "NTFY_ATTACHMENT_CACHE_DIR": "/var/lib/ntfy/attachments",
    }
    args = ["run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
            "-p", f"{cfg['bind']}:{cfg['port']}:{INTERNAL_PORT}",
            "-v", f"{VOLUME}:/var/lib/ntfy",
            "--label", f"bobi.ntfy.sig={_signature(cfg)}"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [cfg["image"], "serve"]
    dk._docker(*args, timeout=180)
    # attendre que le serveur réponde
    for _ in range(20):
        if _container_running():
            return
        time.sleep(0.3)


def _container_running():
    return dk.status(CONTAINER, INTERNAL_PORT).get("state") == "running"


def _current_signature():
    """Signature avec laquelle le conteneur tourne actuellement (label), ou None."""
    try:
        out = dk._docker("inspect", CONTAINER, timeout=20, check=False)
        if not out.strip():
            return None
        labels = (json.loads(out)[0].get("Config") or {}).get("Labels") or {}
        return labels.get("bobi.ntfy.sig")
    except Exception:
        return None


def ensure_container(force_provision=False):
    """Idempotent : garantit que le conteneur tourne avec la config courante, puis provisionne
    l'auth. Recrée uniquement si l'image nécessaire est absente ou si la config a changé.
    Retourne un dict statut. Ne lève pas (renvoie {"error": ...})."""
    with _docker_lock:
        cfg = _cfg()
        try:
            if not dk.available():
                return {"error": "Docker indisponible sur l'hôte"}
            if not dk.image_exists(cfg["image"]):
                dk._docker("pull", cfg["image"], timeout=600)
            need_run = (not _container_running()) or (_current_signature() != _signature(cfg))
            if need_run:
                _run_container(cfg)
                force_provision = True
            if not _container_running():
                return {"error": "le conteneur ntfy ne démarre pas (voir logs)"}
            if force_provision:
                _provision()
            return {"ok": True}
        except dk.DockerError as e:
            log.error("ntfy: ensure_container échoué : %s", e)
            return {"error": str(e)}
        except Exception as e:  # pragma: no cover
            log.exception("ntfy: ensure_container")
            return {"error": str(e)}


def _provision():
    """Crée/actualise l'utilisateur admin et garantit un token de publication.

    - `ntfy user add --role=admin <user>` (mot de passe via NTFY_PASSWORD, non-interactif) ; si
      l'utilisateur existe déjà, on met à jour le mot de passe (`change-pass`).
    - si aucun token de publication n'est mémorisé, en crée un (`token add <user>`) et le stocke.
    Ne lève pas : le conteneur peut tourner sans provisioning (on remonte via l'audit)."""
    user = (settings.get("ntfy_admin_user") or "bobi").strip()
    pwd = settings.get("ntfy_admin_pass") or ""
    if not user or not pwd:
        return  # rien à provisionner tant que l'admin n'est pas renseigné
    try:
        out = dk._docker("exec", "-e", f"NTFY_PASSWORD={pwd}", CONTAINER,
                         "ntfy", "user", "add", "--role=admin", user, timeout=30, check=False)
        if "already exists" in (out or "").lower():
            dk._docker("exec", "-e", f"NTFY_PASSWORD={pwd}", CONTAINER,
                       "ntfy", "user", "change-pass", user, timeout=30, check=False)
        if not (settings.get("ntfy_publish_token") or ""):
            tok = create_token()
            if tok:
                settings.set("ntfy_publish_token", tok)
    except dk.DockerError as e:
        log.warning("ntfy: provisioning échoué : %s", e)


def create_token():
    """Crée un token d'accès ntfy pour l'utilisateur admin et le renvoie (tk_…), ou None."""
    user = (settings.get("ntfy_admin_user") or "bobi").strip()
    try:
        out = dk._docker("exec", CONTAINER, "ntfy", "token", "add", user, timeout=30, check=False)
        m = _TOKEN_RE.search(out or "")
        return m.group(0) if m else None
    except dk.DockerError as e:
        log.warning("ntfy: création de token échouée : %s", e)
        return None


def _stop_container():
    with _docker_lock:
        dk.stop_container(CONTAINER, ignore=True)


# ═════════════════════════════════════════════════════════════════════
# Publication
# ═════════════════════════════════════════════════════════════════════

def _server_url():
    """URL du serveur ntfy JOIGNABLE DEPUIS L'APP (l'hôte) : le port publié en local."""
    return f"http://127.0.0.1:{int(settings.get('ntfy_port') or 2586)}"


def _norm_priority(p):
    if p is None:
        return None
    if isinstance(p, str):
        return str(_PRIORITIES.get(p.strip().lower(), p.strip()))
    return str(int(p))


def _http_publish(topic, message, *, title=None, priority=None, tags=None, click=None):
    """POST la notification au serveur ntfy (Bearer token). Lève en cas d'échec."""
    token = settings.get("ntfy_publish_token") or ""
    if not token:
        raise ValueError("aucun token de publication (provisioning ntfy incomplet)")
    url = f"{_server_url()}/{topic}"
    body = (message or "").encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    if title:
        req.add_header("Title", str(title))
    pr = _norm_priority(priority)
    if pr:
        req.add_header("Priority", pr)
    if tags:
        req.add_header("Tags", tags if isinstance(tags, str) else ",".join(map(str, tags)))
    if click:
        req.add_header("Click", str(click))
    timeout = int(settings.get("ntfy_timeout") or 10)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def _deliver(item):
    topic, message, kw, actor = item
    try:
        _http_publish(topic, message, **kw)
        detail = f"topic={topic} · {(kw.get('title') or message or '')}"[:500]
        _set_last(True, detail)
        audit_log("ntfy", "sent", detail, user_id=None, username=actor)
    except Exception as e:
        detail = f"topic={topic} · échec : {e}"[:500]
        _set_last(False, detail)
        audit_log("ntfy", "fail", detail, user_id=None, username=actor)
        log.warning("ntfy: publication échouée : %s", e)


def _worker_loop():
    while True:
        item = _queue.get()
        try:
            if item is _STOP:
                return
            _deliver(item)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="ntfy-worker", daemon=True)
        _worker.start()


# ── API publique du service ──────────────────────────────────

def publish(message, *, topic=None, title=None, priority=None, tags=None, click=None,
            actor=NTFY_ACTOR):
    """Met une notification en file (asynchrone, best-effort). Retourne {"queued": bool, ...}.
    Ne lève jamais : une alerte ratée ne doit pas casser le flux appelant."""
    try:
        if not settings.get("ntfy_enabled"):
            return {"queued": False, "error": "service ntfy désactivé"}
        topic = (topic or settings.get("ntfy_default_topic") or "bobitools").strip()
        _ensure_worker()
        _queue.put((topic, message,
                    {"title": title, "priority": priority, "tags": tags, "click": click},
                    actor or NTFY_ACTOR))
        return {"queued": True, "topic": topic}
    except Exception as e:
        log.warning("ntfy: publish échoué : %s", e)
        return {"queued": False, "error": str(e)}


def send_now(message, *, topic=None, title=None, priority=None, tags=None, click=None,
             actor=NTFY_ACTOR):
    """Publication SYNCHRONE (bouton « test »). Retourne {"ok": bool, "error"?}.
    Contourne `ntfy_enabled` (un test doit valider la config avant activation)."""
    topic = (topic or settings.get("ntfy_default_topic") or "bobitools").strip()
    try:
        _http_publish(topic, message, title=title, priority=priority, tags=tags, click=click)
        _set_last(True, f"topic={topic} · test")
        audit_log("ntfy", "test", f"topic={topic}", user_id=None, username=actor)
        return {"ok": True, "topic": topic}
    except Exception as e:
        _set_last(False, f"topic={topic} · test · échec : {e}")
        audit_log("ntfy", "test_fail", f"topic={topic} · {e}"[:500], user_id=None, username=actor)
        return {"ok": False, "error": str(e)}


def status_dict():
    """État du service pour l'UI ; secrets exposés en booléens *_set seulement."""
    with _state_lock:
        last = dict(_last)
    cfg = _cfg()
    st = dk.status(CONTAINER, INTERNAL_PORT)
    return {
        "enabled": bool(settings.get("ntfy_enabled")),
        "docker_available": dk.available(),
        "container_state": st.get("state"),
        "container_port": st.get("port"),
        "image": cfg["image"],
        "port": cfg["port"],
        "bind": cfg["bind"],
        "base_url": cfg["base_url"],
        "default_topic": settings.get("ntfy_default_topic") or "bobitools",
        "default_access": cfg["access"],
        "admin_user": settings.get("ntfy_admin_user") or "",
        "timeout": int(settings.get("ntfy_timeout") or 10),
        "admin_pass_set": bool(settings.get("ntfy_admin_pass")),
        "publish_token_set": bool(settings.get("ntfy_publish_token")),
        "token_set": bool(settings.get("ntfy_token")),
        "queue_depth": _queue.qsize(),
        "last": last,
    }


def boot():
    """Démarrage au lancement de l'app : si activé, garantit le conteneur + le worker."""
    try:
        if settings.get("ntfy_enabled"):
            _ensure_worker()
            # ensure_container fait des appels Docker (potentiellement lents) → thread détaché,
            # pour ne pas retarder le boot de l'app.
            threading.Thread(target=ensure_container, name="ntfy-boot", daemon=True).start()
    except Exception as e:
        log.error("ntfy: boot échoué : %s", e)


def stop():
    """Arrêt propre du worker (le conteneur ntfy, lui, reste up : restart=unless-stopped)."""
    global _worker
    with _worker_lock:
        w = _worker
        _worker = None
    if w and w.is_alive():
        _queue.put(_STOP)
        w.join(timeout=2)


# ═════════════════════════════════════════════════════════════════════
# Routes API du service (montées par core_plugins.register_all_routes)
# ═════════════════════════════════════════════════════════════════════

def register_routes(bp):
    from flask import request, jsonify
    from app.auth import require_login, require_perm, current_user

    _MASK = "••••••••"

    @bp.route("/api/ntfy/status", methods=["GET"])
    @require_login
    def ntfy_status():
        return jsonify(status_dict())

    @bp.route("/api/ntfy/logs", methods=["GET"])
    @require_perm("settings.edit")
    def ntfy_logs():
        return jsonify({"logs": dk.logs(CONTAINER, tail=200)})

    @bp.route("/api/ntfy/apply", methods=["POST"])
    @require_perm("settings.edit")
    def ntfy_apply():
        data = request.json or {}
        enabled = bool(data.get("enabled"))
        try:
            port = int(data.get("port") or 2586)
            timeout = int(data.get("timeout") or 10)
        except (TypeError, ValueError):
            return jsonify({"error": "port/délai invalide"}), 400
        if not (1 <= port <= 65535):
            return jsonify({"error": "port invalide"}), 400
        access = (data.get("default_access") or "deny-all").strip()
        if access not in ("deny-all", "read-only", "read-write"):
            return jsonify({"error": "accès par défaut invalide"}), 400

        settings.set("ntfy_enabled", enabled)
        settings.set("ntfy_image", (data.get("image") or "binwiederhier/ntfy:latest").strip())
        settings.set("ntfy_port", port)
        settings.set("ntfy_bind", (data.get("bind") or "0.0.0.0").strip() or "0.0.0.0")
        settings.set("ntfy_base_url", (data.get("base_url") or "").strip())
        settings.set("ntfy_default_topic", (data.get("default_topic") or "bobitools").strip())
        settings.set("ntfy_default_access", access)
        settings.set("ntfy_admin_user", (data.get("admin_user") or "bobi").strip())
        settings.set("ntfy_timeout", timeout)
        pwd = data.get("admin_pass")
        if pwd is not None and pwd != _MASK:
            settings.set("ntfy_admin_pass", pwd)
        tok = data.get("token")
        if tok is not None and tok != _MASK:
            settings.set("ntfy_token", tok)

        u = current_user() or {}
        audit_log("ntfy", "apply",
                  f"enabled={enabled} port={port} bind={settings.get('ntfy_bind')} access={access}",
                  user_id=u.get("id"), username=u.get("username") or "système")

        res = {}
        if enabled:
            _ensure_worker()
            res = ensure_container()          # (re)lance/provisionne selon la nouvelle config
        else:
            _stop_container()
        out = status_dict()
        if res.get("error"):
            out["apply_error"] = res["error"]
        return jsonify(out)

    @bp.route("/api/ntfy/token", methods=["POST"])
    @require_perm("settings.edit")
    def ntfy_token_new():
        """Régénère le token de publication (ex. compromission)."""
        if not _container_running():
            return jsonify({"error": "conteneur ntfy non démarré"}), 409
        tok = create_token()
        if not tok:
            return jsonify({"error": "création de token échouée"}), 502
        settings.set("ntfy_publish_token", tok)
        u = current_user() or {}
        audit_log("ntfy", "token", "nouveau token de publication",
                  user_id=u.get("id"), username=u.get("username") or "système")
        return jsonify(status_dict())

    @bp.route("/api/ntfy/test", methods=["POST"])
    @require_perm("settings.edit")
    def ntfy_test():
        data = request.json or {}
        u = current_user() or {}
        res = send_now(data.get("message") or "Notification de test depuis Bobi.Tools.",
                       topic=data.get("topic"),
                       title=data.get("title") or "Bobi.Tools — test",
                       priority=data.get("priority"), tags=data.get("tags") or "white_check_mark",
                       actor=u.get("username") or NTFY_ACTOR)
        return (jsonify(res), 200) if res.get("ok") else (jsonify(res), 502)

    @bp.route("/api/ntfy/publish", methods=["POST"])
    def ntfy_publish():
        # Auth : session connectée OU token partagé (outils Docker).
        token = settings.get("ntfy_token") or ""
        header = request.headers.get("X-BT-Ntfy-Token") or ""
        authed = bool(current_user())
        if not authed:
            if not token or header != token:
                return jsonify({"error": "non autorisé"}), 401
        data = request.json or {}
        u = current_user() or {}
        actor = (u.get("username") if authed else None) or "outil (token)"
        res = publish(data.get("message") or "", topic=data.get("topic"),
                      title=data.get("title"), priority=data.get("priority"),
                      tags=data.get("tags"), click=data.get("click"), actor=actor)
        return (jsonify(res), 200) if res.get("queued") else (jsonify(res), 400)
