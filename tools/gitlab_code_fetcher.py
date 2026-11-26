"""
title: GitLab Code Fetcher (Multi-modules, Full-Text Code Search & Safe API)
author: Assistant
version: 3.0.0
description: Recherche asynchrone dans le code et les fichiers GitLab v4, détection auto de la branche par défaut, jeton personnel par utilisateur et confirmation obligatoire pour les écritures.
"""

import json
import os
import urllib.parse
from typing import Any, Dict, Optional

import httpx
from pydantic import BaseModel, Field


EXT_TO_LANG = {
    ".java": "java",
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".sql": "sql",
    ".xml": "xml",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sh": "bash",
    ".bash": "bash",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".properties": "properties",
    ".groovy": "groovy",
    ".kt": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".dockerfile": "dockerfile",
}


class Tools:

    class Valves(BaseModel):
        GITLAB_URL: str = Field(
            default="https://gitlab.riva",
            description="URL de l'instance GitLab (ex: https://gitlab.riva)",
        )
        GITLAB_TOKEN: str = Field(
            default="",
            description="Jeton GitLab admin (utilisé uniquement si ALLOW_ADMIN_TOKEN_FALLBACK=True)",
        )
        ALLOW_ADMIN_TOKEN_FALLBACK: bool = Field(
            default=False,
            description="Autoriser le repli sur le jeton admin si l'utilisateur n'a pas saisi son PAT personnel (False recommandé)",
        )
        DEFAULT_PROJECT_ID: str = Field(
            default="75",
            description="ID numérique ou chemin 'groupe/projet' par défaut",
        )
        ALLOW_WRITE: bool = Field(
            default=True,
            description="Autoriser les requêtes POST/PUT/PATCH/DELETE via gitlab_api_request (toujours soumises à confirmation dans le chat)",
        )
        VERIFY_SSL: bool = Field(
            default=False,
            description="Vérifier le certificat SSL (False par défaut pour les instances internes sans CA montée)",
        )
        MAX_OUTPUT_CHARS: int = Field(
            default=8000,
            description="Limite maximale de caractères retournés au modèle",
        )

    class UserValves(BaseModel):
        GITLAB_TOKEN: str = Field(
            default="",
            description="Votre Personal Access Token GitLab personnel (scopes read_api et read_repository)",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._default_branch_cache: Dict[str, str] = {}

    def _resolve_token(self, __user__: Optional[dict]) -> str:
        if isinstance(__user__, dict):
            uv = __user__.get("valves")
            if uv is not None:
                tok = (
                    uv.get("GITLAB_TOKEN", "")
                    if isinstance(uv, dict)
                    else getattr(uv, "GITLAB_TOKEN", "")
                )
                tok = str(tok or "").strip()
                if tok:
                    return tok
        if self.valves.ALLOW_ADMIN_TOKEN_FALLBACK and self.valves.GITLAB_TOKEN.strip():
            return self.valves.GITLAB_TOKEN.strip()
        return ""

    def _missing_token_msg(self) -> str:
        return (
            "❌ Jeton GitLab personnel manquant : renseignez `GITLAB_TOKEN` "
            "dans vos réglages personnels de l'outil GitLab (Paramètres -> Outils)."
        )

    def _make_client(self, timeout: float = 20.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=8.0),
            verify=self.valves.VERIFY_SSL,
        )

    async def _emit_status(self, emitter, description: str, done: bool) -> None:
        if not emitter:
            return
        try:
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception:
            pass

    @staticmethod
    def _encode_project(project_id: str) -> str:
        raw = str(project_id).strip()
        return raw if raw.isdigit() else urllib.parse.quote(raw, safe="")

    @staticmethod
    def _detect_lang(path: str) -> str:
        base = os.path.basename(path).lower()
        if base == "dockerfile":
            return "dockerfile"
        _, ext = os.path.splitext(base)
        return EXT_TO_LANG.get(ext, "")

    async def _resolve_branch(
        self,
        client: httpx.AsyncClient,
        base_api: str,
        headers: Dict[str, str],
        project_key: str,
        branch: Optional[str],
    ) -> str:
        if branch and branch.strip():
            return branch.strip()
        if project_key in self._default_branch_cache:
            return self._default_branch_cache[project_key]
        try:
            res = await client.get(base_api, headers=headers)
            if res.status_code == 200:
                default_br = str(res.json().get("default_branch") or "").strip()
                if default_br:
                    self._default_branch_cache[project_key] = default_br
                    return default_br
        except Exception:
            pass
        return "main"

    def _format_file_output(
        self,
        path: str,
        branch: str,
        content: str,
        start_line: Optional[int],
        end_line: Optional[int],
    ) -> str:
        lines = content.splitlines()
        total_lines = len(lines)
        s = max(1, int(start_line)) if start_line else 1
        e = min(total_lines, int(end_line)) if end_line else total_lines

        selected = lines[s - 1 : e]
        numbered = [f"{i:>4} | {line}" for i, line in enumerate(selected, start=s)]
        body = "\n".join(numbered)

        limit = max(1000, self.valves.MAX_OUTPUT_CHARS)
        note = ""
        if len(body) > limit:
            body = body[:limit]
            last_shown = s + body.count("\n")
            note = (
                f"\n\n… [fichier tronqué à la ligne ~{last_shown}/{total_lines} — "
                f"appelle `get_file_content(target_path='{path}', branch='{branch}', "
                f"start_line={last_shown + 1}, end_line={min(total_lines, last_shown + 200)})` pour la suite]"
            )
        elif e < total_lines:
            note = f"\n\n*(Lignes {s}-{e} sur {total_lines})*"

        lang = self._detect_lang(path)
        return f"### Fichier : `{path}` (ref `{branch}`, {total_lines} lignes)\n\n```{lang}\n{body}\n```{note}"

    async def get_file_content(
        self,
        target_path: str,
        project_id: Optional[str] = None,
        branch: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        __user__: dict = None,
        __event_emitter__=None,
    ) -> str:
        """Récupère un fichier ou cherche une classe dans le dépôt GitLab (multi-modules).

        :param target_path: Chemin complet du fichier ou simple nom de fichier/classe (ex: 'PasrauNamingStrategy').
        :param project_id: ID numérique ou 'groupe/projet' GitLab (par défaut DEFAULT_PROJECT_ID).
        :param branch: Branche, tag ou commit (si omis, utilise la branche par défaut du projet).
        :param start_line: Première ligne à afficher (1-indexé, optionnel).
        :param end_line: Dernière ligne à afficher (optionnel).
        :return: Contenu numéroté du fichier trouvé.
        """
        project = (project_id or self.valves.DEFAULT_PROJECT_ID or "").strip()
        if not project:
            return "❌ Erreur : aucun `project_id` configuré."
        token = self._resolve_token(__user__)
        if not token:
            return self._missing_token_msg()

        headers = {"PRIVATE-TOKEN": token}
        encoded_project = self._encode_project(project)
        base_api = f"{self.valves.GITLAB_URL.rstrip('/')}/api/v4/projects/{encoded_project}"
        clean_target = target_path.strip()

        await self._emit_status(
            __event_emitter__, f"🔎 Lecture GitLab : `{clean_target}`…", False
        )

        try:
            async with self._make_client(timeout=15.0) as client:
                ref = await self._resolve_branch(
                    client, base_api, headers, encoded_project, branch
                )

                # 1. Accès direct si un chemin complet est fourni
                if "/" in clean_target:
                    encoded_path = urllib.parse.quote(clean_target, safe="")
                    res = await client.get(
                        f"{base_api}/repository/files/{encoded_path}/raw",
                        headers=headers,
                        params={"ref": ref},
                    )
                    if res.status_code == 200:
                        await self._emit_status(
                            __event_emitter__, f"Fichier `{clean_target}` chargé.", True
                        )
                        return self._format_file_output(
                            clean_target, ref, res.text, start_line, end_line
                        )

                # 2. Recherche via l'API Blobs
                query_term = os.path.splitext(clean_target.split("/")[-1])[0]
                search_res = await client.get(
                    f"{base_api}/search",
                    headers=headers,
                    params={
                        "scope": "blobs",
                        "search": f"filename:{query_term}",
                        "ref": ref,
                    },
                )
                if search_res.status_code == 200 and search_res.json():
                    matched_path = search_res.json()[0]["path"]
                    encoded_path = urllib.parse.quote(matched_path, safe="")
                    content_res = await client.get(
                        f"{base_api}/repository/files/{encoded_path}/raw",
                        headers=headers,
                        params={"ref": ref},
                    )
                    if content_res.status_code == 200:
                        await self._emit_status(
                            __event_emitter__, f"Fichier `{matched_path}` chargé.", True
                        )
                        return self._format_file_output(
                            matched_path, ref, content_res.text, start_line, end_line
                        )

                # 3. Parcours paginé de l'arborescence (repli)
                page = 1
                matched_path = None
                while page <= 15:
                    tree_res = await client.get(
                        f"{base_api}/repository/tree",
                        headers=headers,
                        params={
                            "ref": ref,
                            "recursive": True,
                            "per_page": 100,
                            "page": page,
                        },
                    )
                    if tree_res.status_code != 200:
                        break
                    files = tree_res.json()
                    if not files:
                        break
                    for f in files:
                        if f.get("type") == "blob":
                            p = f.get("path", "")
                            if clean_target.lower() in p.lower():
                                matched_path = p
                                break
                    if matched_path:
                        break
                    next_page = tree_res.headers.get("X-Next-Page")
                    if not next_page:
                        break
                    page = int(next_page)

                if matched_path:
                    encoded_path = urllib.parse.quote(matched_path, safe="")
                    content_res = await client.get(
                        f"{base_api}/repository/files/{encoded_path}/raw",
                        headers=headers,
                        params={"ref": ref},
                    )
                    if content_res.status_code == 200:
                        await self._emit_status(
                            __event_emitter__, f"Fichier `{matched_path}` chargé.", True
                        )
                        return self._format_file_output(
                            matched_path, ref, content_res.text, start_line, end_line
                        )

            await self._emit_status(__event_emitter__, "Fichier introuvable.", True)
            return f"Fichier introuvable pour '{clean_target}' sur la ref '{ref}' dans le projet {project}."

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur GitLab", True)
            return f"❌ Erreur lors de la requête GitLab : {e}"

    async def search_code(
        self,
        query: str,
        project_id: Optional[str] = None,
        branch: Optional[str] = None,
        max_results: int = 10,
        __user__: dict = None,
        __event_emitter__=None,
    ) -> str:
        """Recherche plein texte dans le CONTENU des fichiers du dépôt GitLab (méthodes, constantes, messages d'erreur, clés SQL…).

        :param query: Texte ou symbole à rechercher dans le code source.
        :param project_id: ID numérique ou 'groupe/projet' GitLab (par défaut DEFAULT_PROJECT_ID).
        :param branch: Branche ou tag (si omis, utilise la branche par défaut du projet).
        :param max_results: Nombre maximal d'extraits retournés (défaut: 10, max: 20).
        :return: Liste des fichiers et extraits correspondants.
        """
        project = (project_id or self.valves.DEFAULT_PROJECT_ID or "").strip()
        if not project:
            return "❌ Erreur : aucun `project_id` configuré."
        token = self._resolve_token(__user__)
        if not token:
            return self._missing_token_msg()

        headers = {"PRIVATE-TOKEN": token}
        encoded_project = self._encode_project(project)
        base_api = f"{self.valves.GITLAB_URL.rstrip('/')}/api/v4/projects/{encoded_project}"
        limit = max(1, min(int(max_results), 20))

        await self._emit_status(
            __event_emitter__, f"🔎 Recherche dans le code GitLab : '{query}'…", False
        )

        try:
            async with self._make_client(timeout=20.0) as client:
                ref = await self._resolve_branch(
                    client, base_api, headers, encoded_project, branch
                )
                res = await client.get(
                    f"{base_api}/search",
                    headers=headers,
                    params={
                        "scope": "blobs",
                        "search": query.strip(),
                        "ref": ref,
                        "per_page": limit,
                    },
                )
            if res.status_code != 200:
                await self._emit_status(__event_emitter__, "Erreur recherche code", True)
                return f"⚠️ Erreur HTTP {res.status_code} sur `/search` : {res.text[:400]}"

            hits = res.json() or []
            await self._emit_status(
                __event_emitter__,
                f"GitLab : {len(hits)} résultat(s) pour '{query}'.",
                True,
            )
            if not hits:
                return f"Aucune occurrence de '{query}' trouvée dans le projet {project} (ref `{ref}`)."

            parts = [f"### {len(hits)} occurrence(s) de `{query}` (ref `{ref}`)\n"]
            for h in hits[:limit]:
                path = h.get("path", "?")
                start = h.get("startline", 1)
                snippet = str(h.get("data") or "").strip()
                if len(snippet) > 600:
                    snippet = snippet[:600] + "\n…"
                lang = self._detect_lang(path)
                parts.append(
                    f"**`{path}`** (ligne {start})\n```{lang}\n{snippet}\n```"
                )

            out = "\n\n".join(parts)
            max_chars = max(1000, self.valves.MAX_OUTPUT_CHARS)
            if len(out) > max_chars:
                out = out[:max_chars] + f"\n\n… [tronqué à {max_chars} caractères]"
            return out

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur recherche code", True)
            return f"❌ Erreur recherche code GitLab : {e}"

    async def gitlab_api_request(
        self,
        endpoint: str,
        method: str = "GET",
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        __user__: dict = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """Exécute un appel sur l'API REST v4 de GitLab (tags, commits, branches, issues, pipelines…).
        Les méthodes d'écriture (POST, PUT, PATCH, DELETE) requièrent une confirmation explicite dans le chat.

        :param endpoint: Chemin relatif de l'API v4 (ex: "/projects/75/repository/tags").
        :param method: Méthode HTTP ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'). Par défaut 'GET'.
        :param params: Paramètres d'URL (ex: {"order_by": "updated", "sort": "desc", "per_page": 5}).
        :param data: Payload JSON pour les requêtes POST/PUT/PATCH.
        :return: Résultat JSON formaté renvoyé par GitLab.
        """
        token = self._resolve_token(__user__)
        if not token:
            return self._missing_token_msg()

        base_url = self.valves.GITLAB_URL.rstrip("/")
        if not base_url:
            return "❌ `GITLAB_URL` non configuré dans les Valves de l'outil GitLab."

        http_method = (method or "GET").strip().upper()
        if http_method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            return f"❌ Méthode HTTP non supportée : `{http_method}`."

        clean_endpoint = endpoint.strip()
        if not clean_endpoint.startswith("/"):
            clean_endpoint = f"/{clean_endpoint}"

        if clean_endpoint.startswith("/api/v4"):
            url = f"{base_url.replace('/api/v4', '')}{clean_endpoint}"
        else:
            url = f"{base_url}/api/v4{clean_endpoint}"

        if http_method not in ("GET", "HEAD"):
            if not self.valves.ALLOW_WRITE:
                return "🚫 Les appels d'écriture GitLab sont désactivés (`ALLOW_WRITE=False`)."
            if not __event_call__:
                return (
                    "🚫 Confirmation interactive impossible dans ce contexte : "
                    f"requête `{http_method} {clean_endpoint}` annulée par sécurité."
                )
            details = json.dumps(data, ensure_ascii=False)[:400] if data else "(aucun corps)"
            approved = await __event_call__(
                {
                    "type": "confirmation",
                    "data": {
                        "title": f"Exécuter `{http_method} {clean_endpoint}` sur GitLab ?",
                        "message": f"Données envoyées : {details}",
                    },
                }
            )
            if approved is not True:
                return f"❎ Requête `{http_method} {clean_endpoint}` annulée par l'utilisateur."

        headers = {
            "PRIVATE-TOKEN": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        await self._emit_status(
            __event_emitter__, f"🔎 GitLab `{http_method} {clean_endpoint}`…", False
        )

        try:
            async with self._make_client(timeout=20.0) as client:
                res = await client.request(
                    method=http_method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=data if data is not None else None,
                )

            await self._emit_status(
                __event_emitter__, f"GitLab `{clean_endpoint}` : HTTP {res.status_code}.", True
            )

            if res.status_code in (200, 201, 204):
                if res.status_code == 204 or not res.text:
                    return f"✅ Succès (HTTP {res.status_code}) sur `{clean_endpoint}`."
                try:
                    payload = res.json()
                    if isinstance(payload, list) and len(payload) > 10:
                        payload = payload[:10]
                    formatted = json.dumps(payload, indent=2, ensure_ascii=False)
                except Exception:
                    formatted = res.text
                max_chars = max(1000, self.valves.MAX_OUTPUT_CHARS)
                if len(formatted) > max_chars:
                    formatted = formatted[:max_chars] + f"\n… [tronqué à {max_chars} caractères]"
                return formatted

            return f"⚠️ Erreur HTTP {res.status_code} sur `{clean_endpoint}` : {res.text[:400]}"

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur API GitLab", True)
            return f"❌ Erreur réseau GitLab : {e}"