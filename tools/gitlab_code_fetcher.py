"""
title: GitLab Code Fetcher (Multi-modules & Full Search)
author: Assistant
version: 2.1
description: Recherche, extrait du code et exécute des requêtes directes sur l'API GitLab v4.
"""

import json
import requests
import urllib3
from typing import Optional
from pydantic import BaseModel, Field

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Tools:
    class Valves(BaseModel):
        GITLAB_URL: str = Field(
            default="https://gitlab.riva",
            description="URL de l'instance GitLab (ex: https://gitlab.riva)",
        )
        GITLAB_TOKEN: str = Field(
            default="",
            description="Personal Access Token avec scopes read_api et read_repository",
        )
        DEFAULT_PROJECT_ID: str = Field(
            default="75",
            description="ID numérique ou nom URL-encodé du projet GitLab",
        )
        VERIFY_SSL: bool = Field(
            default=False,
            description="Désactiver la vérification SSL (False si certificat autosigné)",
        )

    def __init__(self):
        self.valves = self.Valves()

    def get_file_content(
        self,
        target_path: str,
        project_id: Optional[str] = None,
        branch: str = "main",
    ) -> str:
        """
        Récupère un fichier ou cherche une classe dans le dépôt GitLab (supporte multi-modules).

        :param target_path: Chemin complet du fichier (ex: 'utilities-commun/src/.../PasrauNamingStrategy.java') ou simple nom de classe (ex: 'PasrauNamingStrategy').
        :param project_id: ID du projet GitLab (par défaut DEFAULT_PROJECT_ID).
        :param branch: Nom exact de la branche, tag ou commit (ex: 'main', 'develop', '20271.1.0').
        :return: Le contenu du fichier trouvé.
        """
        project = project_id or self.valves.DEFAULT_PROJECT_ID
        if not project:
            return "Erreur : Aucun project_id configuré."
        if not self.valves.GITLAB_TOKEN:
            return "Erreur : Aucun token configuré dans les Valves."

        headers = {"PRIVATE-TOKEN": self.valves.GITLAB_TOKEN}
        base_api = f"{self.valves.GITLAB_URL.rstrip('/')}/api/v4/projects/{project}"
        verify = self.valves.VERIFY_SSL
        clean_target = target_path.strip()

        try:
            # 1. Tentative d'accès direct si un chemin complet est fourni
            if "/" in clean_target:
                encoded_path = requests.utils.quote(clean_target, safe="")
                file_url = (
                    f"{base_api}/repository/files/{encoded_path}/raw?ref={branch}"
                )
                res = requests.get(file_url, headers=headers, verify=verify, timeout=15)
                if res.status_code == 200:
                    return f"### Fichier : `{clean_target}`\n\n```java\n{res.text}\n```"

            # 2. Recherche via l'API Blobs de GitLab (efficace dans les gros dépôts)
            search_url = f"{base_api}/search"
            query_term = clean_target.split("/")[-1].replace(".java", "")
            search_params = {
                "scope": "blobs",
                "search": f"filename:{query_term}",
                "ref": branch,
            }
            search_res = requests.get(
                search_url,
                headers=headers,
                params=search_params,
                verify=verify,
                timeout=15,
            )

            if search_res.status_code == 200 and search_res.json():
                matched_path = search_res.json()[0]["path"]
                encoded_path = requests.utils.quote(matched_path, safe="")
                file_url = (
                    f"{base_api}/repository/files/{encoded_path}/raw?ref={branch}"
                )
                content_res = requests.get(
                    file_url, headers=headers, verify=verify, timeout=15
                )
                if content_res.status_code == 200:
                    return f"### Fichier : `{matched_path}`\n\n```java\n{content_res.text}\n```"

            # 3. Parcours paginé de l'arborescence (fallback si pas d'index de recherche)
            page = 1
            matched_path = None
            while page <= 15:
                tree_url = f"{base_api}/repository/tree"
                tree_params = {
                    "ref": branch,
                    "recursive": True,
                    "per_page": 100,
                    "page": page,
                }
                tree_res = requests.get(
                    tree_url,
                    headers=headers,
                    params=tree_params,
                    verify=verify,
                    timeout=15,
                )

                if tree_res.status_code != 200:
                    break

                files = tree_res.json()
                if not files:
                    break

                for f in files:
                    if f.get("type") == "blob":
                        path = f.get("path", "")
                        if clean_target.lower() in path.lower():
                            matched_path = path
                            break

                if matched_path:
                    break

                next_page = tree_res.headers.get("X-Next-Page")
                if not next_page:
                    break
                page = int(next_page)

            if matched_path:
                encoded_path = requests.utils.quote(matched_path, safe="")
                file_url = (
                    f"{base_api}/repository/files/{encoded_path}/raw?ref={branch}"
                )
                content_res = requests.get(
                    file_url, headers=headers, verify=verify, timeout=15
                )
                if content_res.status_code == 200:
                    return f"### Fichier : `{matched_path}`\n\n```java\n{content_res.text}\n```"

            return f"Fichier introuvable pour '{clean_target}' sur la branche/tag '{branch}' dans le projet {project}."

        except Exception as e:
            return f"Erreur lors de la requête : {str(e)}"

    def gitlab_api_request(
            self,
            endpoint: str,
            method: str = "GET",
            params: Optional[dict] = None,
            data: Optional[dict] = None,
        ) -> str:
            """Exécute un appel direct et générique sur l'API REST v4 de GitLab.
            Permet d'interroger n'importe quel endpoint officiel (tags, commits, branches, issues, pipelines, etc.).

        :param endpoint: Chemin relatif de l'API v4 (ex: "/projects/75/repository/tags", "/projects/75/repository/commits").
            :param method: Méthode HTTP ('GET', 'POST', 'PUT', 'DELETE'). Par défaut 'GET'.
        :param params: Paramètres d'URL (ex: {"order_by": "updated", "sort": "desc", "per_page": 5}).
        :param data: Payload JSON pour les requêtes POST/PUT.
            :return: Résultat JSON brut ou formaté renvoyé par GitLab.
            """
            token = self.valves.GITLAB_TOKEN.strip()
            if not token:
                return "❌ `GITLAB_TOKEN` non configuré dans les Valves de l'outil GitLab."

            base_url = self.valves.GITLAB_URL.rstrip("/")
        if not base_url:
            return "❌ `GITLAB_URL` non configuré dans les Valves de l'outil GitLab."

            clean_endpoint = endpoint.strip()
            if not clean_endpoint.startswith("/"):
                clean_endpoint = f"/{clean_endpoint}"

        # Évite la duplication si l'endpoint inclut déjà /api/v4
            if clean_endpoint.startswith("/api/v4"):
                url = f"{base_url.replace('/api/v4', '')}{clean_endpoint}"
            else:
            url = f"{base_url}/api/v4{clean_endpoint}"

            headers = {
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }

            try:
                res = requests.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    params=params,
                    json=data if data else None,
                    timeout=20,
                verify=self.valves.VERIFY_SSL,
                )

                if res.status_code in (200, 201):
                    try:
                        payload = res.json()
                        if isinstance(payload, list) and len(payload) > 10:
                            payload = payload[:10]
                        return json.dumps(payload, indent=2, ensure_ascii=False)
                    except Exception:
                        return res.text

                return f"⚠️ Erreur HTTP {res.status_code} sur `{clean_endpoint}` : {res.text}"

            except Exception as e:
                return f"❌ Erreur réseau GitLab : {str(e)}"