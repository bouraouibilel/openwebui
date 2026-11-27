"""
title: MantisBT Advanced Search & Management
author: Assistant
version: 3.0.1
license: MIT
description: Consultation asynchrone, recherche avancée multi-critères (sans accents, scoring, pagination) et ajout de notes avec confirmation utilisateur et jeton personnel sur MantisBT.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
from pydantic import BaseModel, Field


class Tools:

    class Valves(BaseModel):
        MANTIS_REST_URL: str = Field(
            default="https://gda.entrep.com/gda/api/rest",
            description="URL de base de l'API REST Mantis",
        )
        MANTIS_API_TOKEN: str = Field(
            default="",
            description="Jeton API Mantis admin (utilisé uniquement si ALLOW_ADMIN_TOKEN_FALLBACK=True)",
        )
        ALLOW_ADMIN_TOKEN_FALLBACK: bool = Field(
            default=False,
            description="Autoriser le repli sur le jeton admin si l'utilisateur n'a pas saisi son jeton personnel (False recommandé)",
        )
        ALLOW_WRITE: bool = Field(
            default=True,
            description="Autoriser l'ajout de commentaires (toujours soumis à confirmation dans le chat)",
        )
        VERIFY_SSL: bool = Field(
            default=False,
            description="Vérifier le certificat SSL (False par défaut pour les instances internes sans CA montée)",
        )
        MAX_OUTPUT_CHARS: int = Field(
            default=8000,
            description="Limite maximale de caractères retournés au modèle",
        )
        MAX_TICKET_NOTES: int = Field(
            default=15,
            description="Nombre maximal de notes récentes retournées par get_ticket",
        )
        CUSTOM_USER_AGENT: str = Field(
            default="",
            description="User-Agent personnalisé (laisser vide sauf si requis par un WAF interne)",
        )

    class UserValves(BaseModel):
        MANTIS_API_TOKEN: str = Field(
            default="",
            description="Votre jeton API personnel Mantis (Mon Compte > Jetons d'API)",
        )

    def __init__(self):
        self.valves = self.Valves()

    @staticmethod
    def _fold(text: str) -> str:
        """Normalise en minuscules sans accents pour des recherches tolérantes."""
        nfkd = unicodedata.normalize("NFKD", str(text or ""))
        return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).lower()

    def _resolve_token(self, __user__: Optional[dict]) -> str:
        if isinstance(__user__, dict):
            uv = __user__.get("valves")
            if uv is not None:
                tok = (
                    uv.get("MANTIS_API_TOKEN", "")
                    if isinstance(uv, dict)
                    else getattr(uv, "MANTIS_API_TOKEN", "")
                )
                tok = str(tok or "").strip()
                if tok:
                    return tok
        if self.valves.ALLOW_ADMIN_TOKEN_FALLBACK and self.valves.MANTIS_API_TOKEN.strip():
            return self.valves.MANTIS_API_TOKEN.strip()
        return ""

    @staticmethod
    def _parse_issue_id(issue_id: Any) -> Optional[int]:
        """Accepte 45380, "45380", "#45380", "GDA-45380"… et renvoie l'entier, ou None."""
        digits = re.sub(r"\D", "", str(issue_id or ""))
        return int(digits) if digits else None

    def _missing_token_msg(self) -> str:
        return (
            "❌ Jeton API Mantis personnel manquant : renseignez `MANTIS_API_TOKEN` "
            "dans vos réglages personnels de l'outil GDA (Paramètres -> Outils)."
        )

    def _get_headers(self, token: str) -> Dict[str, str]:
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.valves.CUSTOM_USER_AGENT.strip():
            headers["User-Agent"] = self.valves.CUSTOM_USER_AGENT.strip()
        return headers

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

    def _truncate(self, text: str) -> str:
        limit = max(1000, self.valves.MAX_OUTPUT_CHARS)
        if len(text) <= limit:
            return text
        return (
            text[:limit]
            + f"\n\n… [sortie tronquée à {limit}/{len(text)} caractères]"
        )

    async def search_issues(
        self,
        query: Optional[str] = None,
        project: Optional[Union[int, str]] = None,
        target_version: Optional[str] = None,
        status: Optional[str] = None,
        page_size: int = 100,
        max_pages: int = 4,
        __user__: dict = None,
        __event_emitter__=None,
    ) -> str:
        """Recherche et filtre des tickets Mantis avec recherche plein texte tolérante (sans accents, classée par pertinence).
        ATTENTION : À utiliser UNIQUEMENT si vous n'avez pas l'identifiant numérique du ticket.
        Si l'ID du ticket est connu, utilisez obligatoirement get_ticket(issue_id).

        :param query: Termes ou mots-clés séparés par des espaces (ex: 'PASRAU 2024.1', 'arrondi calcul').
        :param project: Nom partiel ou ID numérique du projet pour restreindre la recherche.
        :param target_version: Version cible exacte ou partielle (ex: '2024.1', 'V24').
        :param status: Filtre par statut (ex: 'assigned', 'resolved', 'closed', 'feedback', 'nouveau').
        :param page_size: Nombre de tickets récupérés par page (défaut: 100, max: 100).
        :param max_pages: Nombre maximal de pages à scanner (défaut: 4, max: 10).
        :return: Tableau Markdown des tickets correspondants classés par pertinence.
        """
        token = self._resolve_token(__user__)
        if not token:
            return self._missing_token_msg()

        base_url = self.valves.MANTIS_REST_URL.rstrip("/")
        url = f"{base_url}/issues"
        effective_page_size = max(1, min(int(page_size), 100))
        effective_max_pages = max(1, min(int(max_pages), 10))

        params: Dict[str, Any] = {
            "page_size": effective_page_size,
            "select": "id,summary,description,status,project,target_version,handler,notes,custom_fields",
        }
        if isinstance(project, int) or (isinstance(project, str) and project.isdigit()):
            params["project_id"] = int(project)

        keywords = [self._fold(w) for w in (query or "").strip().split() if w.strip()]
        phrase = self._fold(query.strip()) if query and query.strip() else ""
        tv_clean = self._fold(target_version.strip()) if target_version else None
        st_clean = self._fold(status.strip()) if status else None
        proj_clean = (
            self._fold(str(project).strip())
            if project and not str(project).isdigit()
            else None
        )

        scored_matches: List[Tuple[int, Dict[str, Any]]] = []
        total_scanned = 0

        try:
            async with self._make_client(timeout=25.0) as client:
                for current_page in range(1, effective_max_pages + 1):
                    await self._emit_status(
                        __event_emitter__,
                        f"🔎 Recherche Mantis (page {current_page}/{effective_max_pages})…",
                        False,
                    )
                    params["page"] = current_page
                    res = await client.get(
                        url, headers=self._get_headers(token), params=params
                    )
                    if res.status_code != 200:
                        await self._emit_status(
                            __event_emitter__, "Erreur recherche Mantis", True
                        )
                        return f"⚠️ Erreur HTTP {res.status_code} lors de la recherche : {res.text[:400]}"

                    issues = res.json().get("issues", [])
                    if not issues:
                        break
                    total_scanned += len(issues)

                    for issue in issues:
                        issue_id = issue.get("id")
                        summary = issue.get("summary", "")
                        description = issue.get("description", "")
                        issue_status = (issue.get("status") or {}).get("name", "")
                        project_name = (issue.get("project") or {}).get("name", "Inconnu")

                        t_ver_data = issue.get("target_version")
                        issue_target_version = (
                            t_ver_data.get("name", "")
                            if isinstance(t_ver_data, dict)
                            else str(t_ver_data or "")
                        )

                        notes_text = " ".join(
                            n.get("text", "") for n in issue.get("notes", []) if isinstance(n, dict)
                        )
                        custom_text = " ".join(
                            f"{(cf.get('field') or {}).get('name', '')} {cf.get('value', '')}"
                            for cf in issue.get("custom_fields", [])
                            if isinstance(cf, dict)
                        )

                        if proj_clean and proj_clean not in self._fold(project_name):
                            continue
                        if (
                            tv_clean
                            and tv_clean not in self._fold(issue_target_version)
                            and tv_clean not in self._fold(custom_text)
                        ):
                            continue
                        if st_clean and st_clean not in self._fold(issue_status):
                            continue

                        score = 1
                        if keywords:
                            summary_f = self._fold(summary)
                            corpus_f = self._fold(
                                f"{summary} {description} {notes_text} {issue_target_version} {custom_text}"
                            )
                            matched_kw = sum(1 for kw in keywords if kw in corpus_f)
                            if matched_kw == 0:
                                continue
                            # Bonus si tous les mots matchent ou si le résumé contient la phrase
                            score = matched_kw * 10
                            if matched_kw == len(keywords):
                                score += 50
                            if phrase and phrase in summary_f:
                                score += 100
                            score += sum(5 for kw in keywords if kw in summary_f)

                        scored_matches.append(
                            (
                                score,
                                {
                                    "id": issue_id,
                                    "summary": summary,
                                    "status": issue_status,
                                    "project": project_name,
                                    "target_version": issue_target_version or "-",
                                    "handler": (issue.get("handler") or {}).get(
                                        "name", "Non assigné"
                                    ),
                                },
                            )
                        )

                    if len(issues) < effective_page_size:
                        break

            # Si on a des correspondances complètes (tous les mots-clés), on les privilégie
            if keywords and scored_matches:
                full_threshold = len(keywords) * 10 + 50
                full_matches = [m for m in scored_matches if m[0] >= full_threshold]
                display_list = full_matches if full_matches else scored_matches
            else:
                display_list = scored_matches

            display_list.sort(key=lambda x: x[0], reverse=True)
            matched_issues = [m[1] for m in display_list[:40]]

            await self._emit_status(
                __event_emitter__,
                f"Mantis : {len(matched_issues)} ticket(s) trouvé(s) sur {total_scanned} analysés.",
                True,
            )

            if not matched_issues:
                criteres = []
                if query:
                    criteres.append(f"mots-clés='{query}'")
                if project:
                    criteres.append(f"projet='{project}'")
                if target_version:
                    criteres.append(f"version='{target_version}'")
                if status:
                    criteres.append(f"statut='{status}'")
                return (
                    f"Aucun ticket ne correspond aux critères ({', '.join(criteres)}) "
                    f"sur {total_scanned} tickets scannés (augmentez `max_pages` si besoin)."
                )

            lines = [
                f"### {len(matched_issues)} ticket(s) trouvé(s) (sur {total_scanned} analysés)\n",
                "| ID | Résumé | Projet | Statut | Version cible | Assigné à |",
                "|---|---|---|---|---|---|",
            ]
            for m in matched_issues:
                clean_summary = str(m["summary"]).replace("|", "-").replace("\n", " ")
                lines.append(
                    f"| **#{m['id']}** | {clean_summary} | {m['project']} | {m['status']} | {m['target_version']} | {m['handler']} |"
                )

            return self._truncate("\n".join(lines))

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur recherche Mantis", True)
            return f"❌ Erreur lors de la recherche : {e}"

    async def list_projects(
        self, __user__: dict = None, __event_emitter__=None
    ) -> str:
        """Liste tous les projets accessibles avec leurs identifiants et versions disponibles."""
        token = self._resolve_token(__user__)
        if not token:
            return self._missing_token_msg()

        url = f"{self.valves.MANTIS_REST_URL.rstrip('/')}/projects"
        await self._emit_status(__event_emitter__, "🔎 Liste des projets Mantis…", False)

        try:
            async with self._make_client(timeout=15.0) as client:
                res = await client.get(url, headers=self._get_headers(token))
            await self._emit_status(__event_emitter__, "Projets Mantis récupérés.", True)

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
                    v_str = ", ".join(versions[:15]) if versions else "-"
                    if len(versions) > 15:
                        v_str += f" (+{len(versions) - 15})"
                    lines.append(f"| {p.get('id')} | **{p.get('name')}** | {v_str} |")
                return self._truncate("\n".join(lines))

            return f"⚠️ Erreur HTTP {res.status_code} : {res.text[:400]}"
        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur projets Mantis", True)
            return f"❌ Erreur réseau : {e}"

    async def get_ticket(
        self, issue_id: int, __user__: dict = None, __event_emitter__=None
    ) -> str:
        """Récupère les détails complets et les dernières notes d'un ticket Mantis par son ID.

        :param issue_id: L'identifiant numérique du ticket (ex: 45380).
        """
        parsed_id = self._parse_issue_id(issue_id)
        if parsed_id is None:
            return f"❌ Identifiant de ticket invalide : {issue_id!r} (attendu : un numéro, ex. 45380)."
        issue_id = parsed_id

        token = self._resolve_token(__user__)
        if not token:
            return self._missing_token_msg()

        url = f"{self.valves.MANTIS_REST_URL.rstrip('/')}/issues/{issue_id}"
        await self._emit_status(
            __event_emitter__, f"🔎 Lecture du ticket Mantis #{issue_id}…", False
        )

        try:
            async with self._make_client(timeout=15.0) as client:
                res = await client.get(url, headers=self._get_headers(token))
            await self._emit_status(
                __event_emitter__, f"Ticket #{issue_id} récupéré.", True
            )

            if res.status_code == 200:
                issues = res.json().get("issues", [])
                if not issues:
                    return f"Ticket #{issue_id} introuvable."

                issue = issues[0]
                summary = issue.get("summary", "Sans résumé")
                description = str(issue.get("description") or "Pas de description")
                if len(description) > 3500:
                    description = (
                        description[:3500]
                        + f"\n… [description tronquée : {len(description)} caractères au total]"
                    )

                status = (issue.get("status") or {}).get("name", "Inconnu")
                project = (issue.get("project") or {}).get("name", "Inconnu")
                reporter = (issue.get("reporter") or {}).get("name", "Inconnu")
                handler = (issue.get("handler") or {}).get("name", "Non assigné")
                t_ver = (
                    issue["target_version"].get("name", "-")
                    if isinstance(issue.get("target_version"), dict)
                    else str(issue.get("target_version") or "-")
                )

                custom_fields = [
                    f"- **{(cf.get('field') or {}).get('name')}:** {cf.get('value')}"
                    for cf in issue.get("custom_fields", [])
                    if isinstance(cf, dict) and cf.get("value")
                ]

                raw_notes = [
                    n for n in issue.get("notes", []) if isinstance(n, dict)
                ]
                max_notes = max(1, self.valves.MAX_TICKET_NOTES)
                omitted = max(0, len(raw_notes) - max_notes)
                kept_notes = raw_notes[-max_notes:]

                notes_lines = []
                if omitted:
                    notes_lines.append(
                        f"*({omitted} note(s) plus ancienne(s) omise(s), affichage des {len(kept_notes)} dernières)*"
                    )
                for n in kept_notes:
                    author = (n.get("reporter") or {}).get("name", "Anonyme")
                    text = str(n.get("text") or "")
                    if len(text) > 1000:
                        text = text[:1000] + "…"
                    created_at = str(n.get("created_at") or "")
                    date_info = f" ({created_at[:10]})" if created_at else ""
                    notes_lines.append(f"- **{author}**{date_info} : {text}")

                cf_section = (
                    "\n**Champs personnalisés :**\n" + "\n".join(custom_fields) + "\n"
                    if custom_fields
                    else ""
                )

                out = (
                    f"### Ticket #{issue_id} : {summary}\n"
                    f"- **Projet :** {project} | **Version cible :** {t_ver}\n"
                    f"- **Statut :** {status}\n"
                    f"- **Rapporteur :** {reporter} | **Assigné à :** {handler}\n"
                    f"{cf_section}\n"
                    f"**Description :**\n{description}\n\n"
                    f"**Commentaires / Notes :**\n"
                    + ("\n".join(notes_lines) if notes_lines else "Aucun commentaire.")
                )
                return self._truncate(out)

            return f"⚠️ Erreur HTTP {res.status_code} : {res.text[:400]}"

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur lecture ticket", True)
            return f"❌ Erreur : {e}"

    async def add_comment(
        self,
        issue_id: int,
        comment: str,
        view_state: str = "public",
        __user__: dict = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """Ajoute une note sur un ticket Mantis (soumis à confirmation explicite dans le chat).

        :param issue_id: L'identifiant numérique du ticket (ex: 45380).
        :param comment: Texte du commentaire à publier.
        :param view_state: Visibilité de la note : 'public' ou 'private' (défaut: 'public').
        """
        if not self.valves.ALLOW_WRITE:
            return "🚫 L'écriture sur Mantis est désactivée par l'administrateur (`ALLOW_WRITE=False`)."

        parsed_id = self._parse_issue_id(issue_id)
        if parsed_id is None:
            return f"❌ Identifiant de ticket invalide : {issue_id!r} (attendu : un numéro, ex. 45380)."
        issue_id = parsed_id

        token = self._resolve_token(__user__)
        if not token:
            return self._missing_token_msg()

        vs = "private" if str(view_state).strip().lower() == "private" else "public"
        preview = comment.strip()
        if not preview:
            return "❌ Le commentaire est vide."

        # Confirmation interactive obligatoire avant toute écriture
        if not __event_call__:
            return (
                "🚫 Confirmation interactive impossible dans ce contexte : "
                f"ajout du commentaire sur le ticket #{issue_id} annulé par sécurité."
            )

        approved = await __event_call__(
            {
                "type": "confirmation",
                "data": {
                    "title": f"Publier une note ({vs}) sur le ticket Mantis #{issue_id} ?",
                    "message": preview[:500] + ("…" if len(preview) > 500 else ""),
                },
            }
        )
        if approved is not True:
            return f"❎ Publication annulée par l'utilisateur pour le ticket #{issue_id}."

        url = f"{self.valves.MANTIS_REST_URL.rstrip('/')}/issues/{issue_id}/notes"
        payload = {"text": preview, "view_state": {"name": vs}}
        await self._emit_status(
            __event_emitter__, f"✍️ Publication sur Mantis #{issue_id}…", False
        )

        try:
            async with self._make_client(timeout=15.0) as client:
                res = await client.post(
                    url, headers=self._get_headers(token), json=payload
                )
            await self._emit_status(
                __event_emitter__, f"Commentaire Mantis #{issue_id} traité.", True
            )
            if res.status_code in (200, 201):
                return f"✅ Commentaire ({vs}) ajouté avec succès sur le ticket #{issue_id}."
            return f"⚠️ Erreur HTTP {res.status_code} : {res.text[:400]}"

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur écriture Mantis", True)
            return f"❌ Erreur : {e}"
