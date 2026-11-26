"""
title: MantisBT Advanced Search & Management
author: Assistant
version: 2.1
license: MIT
description: Consultation, recherche avancée multi-critères (mots-clés multiples, versions, projets, custom fields) et ajout de notes sur MantisBT.
"""

import urllib3
import requests
from pydantic import BaseModel, Field
from typing import Optional, Union

# Désactiver les avertissements SSL en cas d'utilisation avec verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Tools:

    class Valves(BaseModel):
        MANTIS_REST_URL: str = Field(
            default="https://gda.entrep.com/gda/api/rest",
            description="URL de base de l'API REST Mantis",
        )
        MANTIS_API_TOKEN: str = Field(
            default="",
            description="Clé API Mantis générée dans Mon Compte > Jetons d'API",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _get_headers(self) -> dict:
        token = self.valves.MANTIS_API_TOKEN.strip()
        return {
            "Authorization": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    def search_issues(
        self,
        query: Optional[str] = None,
        project: Optional[Union[int, str]] = None,
        target_version: Optional[str] = None,
        status: Optional[str] = None,
        page_size: int = 50,
        max_pages: int = 1,
    ) -> str:
        """Recherche et filtre des tickets Mantis avec recherche plein texte multi-mots.
        ATTENTION : À utiliser UNIQUEMENT si vous n'avez pas l'identifiant numérique du ticket.
        Si l'ID du ticket est connu, utilisez obligatoirement get_ticket(issue_id).

        Scanne simultanément le résumé, la description, les notes, les versions cibles et les champs personnalisés.

        :param query: Termes ou mots-clés séparés par des espaces (ex: 'PASRAU 2024.1', 'arrondi calcul').
        :param project: Nom partiel ou ID numérique du projet pour restreindre la recherche.
        :param target_version: Version cible exacte ou partielle (ex: '2024.1', 'V24').
        :param status: Filtre par statut (ex: 'assigned', 'resolved', 'closed', 'feedback', 'nouveau').
        :param page_size: Nombre de tickets récupérés par page (défaut: 50, max: 100).
        :param max_pages: Nombre maximal de pages à scanner (défaut: 1, augmenter à 2 ou 3 si besoin d'élargir).
        :return: Tableau Markdown des tickets correspondants.
        """
        token = self.valves.MANTIS_API_TOKEN.strip()
        if not token:
            return "❌ `MANTIS_API_TOKEN` non configuré dans les Valves."

        base_url = self.valves.MANTIS_REST_URL.rstrip("/")
        url = f"{base_url}/issues"

        params = {"page_size": min(page_size, 100)}
        if isinstance(project, int) or (isinstance(project, str) and project.isdigit()):
            params["project_id"] = int(project)

        matched_issues = []
        total_scanned = 0

        # Mots-clés en liste pour faire un ET logique
        keywords = query.lower().strip().split() if query else []
        tv_clean = target_version.lower().strip() if target_version else None
        st_clean = status.lower().strip() if status else None
        proj_clean = (
            str(project).lower().strip()
            if project and not str(project).isdigit()
            else None
        )

        try:
            for current_page in range(1, max_pages + 1):
                params["page"] = current_page
                res = requests.get(
                    url,
                    headers=self._get_headers(),
                    params=params,
                    timeout=25,
                    verify=False,
                )

                if res.status_code != 200:
                    return f"⚠️ Erreur HTTP {res.status_code} lors de la recherche : {res.text}"

                data = res.json()
                issues = data.get("issues", [])
                if not issues:
                    break

                total_scanned += len(issues)

                for issue in issues:
                    issue_id = issue.get("id")
                    summary = issue.get("summary", "")
                    description = issue.get("description", "")
                    issue_status = issue.get("status", {}).get("name", "")
                    project_name = issue.get("project", {}).get("name", "Inconnu")

                    # Récupération version cible
                    t_ver_data = issue.get("target_version")
                    issue_target_version = (
                        t_ver_data.get("name", "")
                        if isinstance(t_ver_data, dict)
                        else str(t_ver_data or "")
                    )

                    # Agrégation des notes
                    notes_text = " ".join(
                        [n.get("text", "") for n in issue.get("notes", [])]
                    )

                    # Agrégation des champs personnalisés
                    custom_fields = issue.get("custom_fields", [])
                    custom_text = " ".join(
                        [
                            f"{cf.get('field', {}).get('name', '')} {cf.get('value', '')}"
                            for cf in custom_fields
                        ]
                    )

                    # 1. Filtre Projet par nom (si non géré par ID)
                    if proj_clean and proj_clean not in project_name.lower():
                        continue

                    # 2. Filtre Version cible
                    if (
                        tv_clean
                        and tv_clean not in issue_target_version.lower()
                        and tv_clean not in custom_text.lower()
                    ):
                        continue

                    # 3. Filtre Statut
                    if st_clean and st_clean not in issue_status.lower():
                        continue

                    # 4. Filtre Plein Texte multi-mots (Tous les mots demandés doivent être présents)
                    if keywords:
                        corpus = f"{summary} {description} {notes_text} {issue_target_version} {custom_text}".lower()
                        if not all(kw in corpus for kw in keywords):
                            continue

                    matched_issues.append(
                        {
                            "id": issue_id,
                            "summary": summary,
                            "status": issue_status,
                            "project": project_name,
                            "target_version": issue_target_version or "-",
                            "handler": issue.get("handler", {}).get(
                                "name", "Non assigné"
                            ),
                        }
                    )

            if not matched_issues:
                critères = []
                if query:
                    critères.append(f"mots-clés='{query}'")
                if project:
                    critères.append(f"projet='{project}'")
                if target_version:
                    critères.append(f"version='{target_version}'")
                if status:
                    critères.append(f"statut='{status}'")
                return f"Aucun ticket ne correspond aux critères ({', '.join(critères)}) sur {total_scanned} tickets scannés."

            lines = [
                f"### {len(matched_issues)} ticket(s) trouvé(s) (sur {total_scanned} analysés)\n",
                "| ID | Résumé | Projet | Statut | Version cible | Assigné à |",
                "|---|---|---|---|---|---|",
            ]
            for m in matched_issues:
                clean_summary = m["summary"].replace("|", "-")
                lines.append(
                    f"| **#{m['id']}** | {clean_summary} | {m['project']} | {m['status']} | {m['target_version']} | {m['handler']} |"
                )

            return "\n".join(lines)

        except Exception as e:
            return f"❌ Erreur lors de la recherche : {str(e)}"

    def list_projects(self) -> str:
        """Liste tous les projets accessibles avec leurs identifiants et versions disponibles."""
        token = self.valves.MANTIS_API_TOKEN.strip()
        if not token:
            return "❌ `MANTIS_API_TOKEN` non configuré dans les Valves."

        url = f"{self.valves.MANTIS_REST_URL.rstrip('/')}/projects"

        try:
            res = requests.get(
                url, headers=self._get_headers(), timeout=15, verify=False
            )
            if res.status_code == 200:
                projects = res.json().get("projects", [])
                if not projects:
                    return "Aucun projet accessible."

                lines = [
                    "### Projets accessibles\n",
                    "| ID | Nom du projet | Versions définies |",
                    "|---|---|---|",
                ]
                for p in projects:
                    versions = [v.get("name", "") for v in p.get("versions", [])]
                    v_str = ", ".join(versions) if versions else "-"
                    lines.append(f"| {p.get('id')} | **{p.get('name')}** | {v_str} |")
                return "\n".join(lines)

            return f"⚠️ Erreur HTTP {res.status_code} : {res.text}"
        except Exception as e:
            return f"❌ Erreur réseau : {str(e)}"

    def get_ticket(self, issue_id: int) -> str:
        """Récupère les détails complets et notes d'un ticket Mantis par son ID.

        :param issue_id: L'identifiant numérique du ticket (ex: 45380).
        """
        token = self.valves.MANTIS_API_TOKEN.strip()
        if not token:
            return "❌ `MANTIS_API_TOKEN` non configuré dans les Valves."

        url = f"{self.valves.MANTIS_REST_URL.rstrip('/')}/issues/{issue_id}"

        try:
            res = requests.get(
                url, headers=self._get_headers(), timeout=15, verify=False
            )
            if res.status_code == 200:
                data = res.json()
                issues = data.get("issues", [])
                if not issues:
                    return f"Ticket #{issue_id} introuvable."

                issue = issues[0]
                summary = issue.get("summary", "Sans résumé")
                description = issue.get("description", "Pas de description")
                status = issue.get("status", {}).get("name", "Inconnu")
                project = issue.get("project", {}).get("name", "Inconnu")
                reporter = issue.get("reporter", {}).get("name", "Inconnu")
                handler = issue.get("handler", {}).get("name", "Non assigné")
                t_ver = (
                    issue.get("target_version", {}).get("name", "-")
                    if isinstance(issue.get("target_version"), dict)
                    else str(issue.get("target_version") or "-")
                )

                # Extraction des champs personnalisés
                custom_fields = [
                    f"- **{cf.get('field', {}).get('name')}:** {cf.get('value')}"
                    for cf in issue.get("custom_fields", [])
                    if cf.get("value")
                ]

                # Extraction des notes
                notes = []
                for n in issue.get("notes", []):
                    author = n.get("reporter", {}).get("name", "Anonyme")
                    text = n.get("text", "")
                    created_at = n.get("created_at", "")
                    date_info = f" ({created_at[:10]})" if created_at else ""
                    notes.append(f"- **{author}**{date_info} : {text}")

                cf_section = (
                    ("\n**Champs personnalisés :**\n" + "\n".join(custom_fields) + "\n")
                    if custom_fields
                    else ""
                )

                return (
                    f"### Ticket #{issue_id} : {summary}\n"
                    f"- **Projet :** {project} | **Version cible :** {t_ver}\n"
                    f"- **Statut :** {status}\n"
                    f"- **Rapporteur :** {reporter} | **Assigné à :** {handler}\n"
                    f"{cf_section}\n"
                    f"**Description :**\n{description}\n\n"
                    f"**Commentaires / Notes :**\n"
                    + ("\n".join(notes) if notes else "Aucun commentaire.")
                )

            return f"⚠️ Erreur HTTP {res.status_code} : {res.text}"

        except Exception as e:
            return f"❌ Erreur : {str(e)}"

    def add_comment(self, issue_id: int, comment: str) -> str:
        """Ajoute une note ou un commentaire sur un ticket Mantis.

        :param issue_id: L'identifiant numérique du ticket (ex: 45380).
        :param comment: Texte du commentaire à publier.
        """
        url = f"{self.valves.MANTIS_REST_URL.rstrip('/')}/issues/{issue_id}/notes"
        payload = {"text": comment, "view_state": {"name": "public"}}

        try:
            res = requests.post(
                url,
                headers=self._get_headers(),
                json=payload,
                timeout=15,
                verify=False,
            )
            if res.status_code in (200, 201):
                return f"✅ Commentaire ajouté avec succès sur le ticket #{issue_id}."
            return f"⚠️ Erreur HTTP {res.status_code} : {res.text}"

        except Exception as e:
            return f"❌ Erreur : {str(e)}"
