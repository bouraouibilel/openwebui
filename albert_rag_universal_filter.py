"""
title: Albert RAG Universal Filter with Smart Multi-Collection Router
author: Open WebUI Community
description: Filtre RAG Albert multi-collections avec routage intelligent par mot-clé ou nom de document, mémoire contextuelle maîtrisée et reranking dynamique.
version: 2.6.0
"""

import json
import os
import re
import requests
from typing import Any, Dict, List, Tuple
from pydantic import BaseModel, Field


class Filter:

    class Valves(BaseModel):
        DEBUG_LOGS: bool = Field(
            default=True,
            description="Activer les logs détaillés dans la console (docker logs)",
        )
        ALBERT_API_BASE_URL: str = Field(
            default="https://albert.api.etalab.gouv.fr/v1",
            description="URL de base de l'API Albert",
        )
        ALBERT_API_KEY: str = Field(
            default="",
            description="Clé API Albert (sk-...)",
        )
        COLLECTION_ROUTING_JSON: str = Field(
            default=json.dumps(
                {
                    "pasrau": "266591",
                    "pasaru": "266591",
                    "psig": "266592",
                    "rh": "266593",
                },
                ensure_ascii=False,
                indent=2,
            ),
            description="Mapping des mots-clés vers les IDs de collections",
        )
        DEFAULT_FALLBACK_COLLECTION: str = Field(
            default="",
            description="Projet par défaut si rien n'est détecté (laisser vide '' pour interroger toutes les collections ou aucune selon AUTO_SEARCH_ALL_ON_UNKNOWN)",
        )
        AUTO_SEARCH_ALL_ON_UNKNOWN: bool = Field(
            default=True,
            description="Si True et aucun projet détecté (et fallback vide), interroge toutes les collections configurées au lieu de bloquer sur PASRAU",
        )
        ENABLE_RERANK: bool = Field(
            default=True,
            description="Activer le reranking explicite via POST /v1/rerank",
        )
        RERANK_MODEL: str = Field(
            default="BAAI/bge-reranker-v2-m3",
            description="Modèle Albert pour le reranking",
        )
        RETRIEVAL_LIMIT: int = Field(
            default=30,
            description="Nombre de chunks bruts collectés via /search",
        )
        TOP_K: int = Field(
            default=12,
            description="Nombre final d'extraits injectés",
        )
        RERANK_SCORE_THRESHOLD: float = Field(
            default=-10.0,
            description="Score minimal de pertinence du rerank",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._doc_name_cache: Dict[str, str] = {}
        self._collection_docs_cache: Dict[str, List[Dict[str, Any]]] = {}

    def _log(self, message: str) -> None:
        if getattr(self.valves, "DEBUG_LOGS", False):
            print(f"[Albert Filter DEBUG] {message}", flush=True)

    def _extract_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            ).strip()
        return str(content or "")

    def _get_routes(self) -> Dict[str, str]:
        try:
            raw = json.loads(self.valves.COLLECTION_ROUTING_JSON)
            return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if str(v).strip()}
        except Exception as e:
            self._log(f"Erreur parsing JSON routes : {e}")
            return {}

    def _find_matching_tool_name(self, tools: list, patterns: list[str]) -> str:
        if not tools or not isinstance(tools, list):
            return ""
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function", {}) if isinstance(t.get("function"), dict) else {}
            fn_name = str(fn.get("name", ""))
            fn_desc = str(fn.get("description", ""))
            for p in patterns:
                if re.search(p, fn_name, flags=re.IGNORECASE) or re.search(
                        p, fn_desc, flags=re.IGNORECASE
                ):
                    return fn_name
        return ""

    def _get_collection_documents(self, collection_id: str) -> List[Dict[str, Any]]:
        """Récupère la liste des documents d'une collection avec mise en cache."""
        if collection_id in self._collection_docs_cache:
            return self._collection_docs_cache[collection_id]

        url = f"{self.valves.ALBERT_API_BASE_URL.rstrip('/')}/collections/{collection_id}/documents"
        headers = {
            "Authorization": f"Bearer {self.valves.ALBERT_API_KEY}",
            "Accept": "application/json",
        }
        try:
            res = requests.get(url, headers=headers, timeout=6)
            if res.status_code == 200:
                data = res.json()
                docs = data.get("data") or data.get("results") or []
                self._collection_docs_cache[collection_id] = docs
                return docs
        except Exception as e:
            self._log(
                f"Erreur récupération docs collection {collection_id} : {e}"
            )
        return []

    def _detect_collections_by_document_name(
            self, query: str, routes: Dict[str, str]
    ) -> Tuple[List[str], List[str]]:
        """Scanne les documents de toutes les collections pour voir si un fichier précis est cité."""
        q_clean = re.sub(r"[_\-\s\.]", "", query.lower())
        if len(q_clean) < 5:
            return [], []

        unique_cids = list(set(routes.values()))
        cid_to_label = {}
        for k, v in routes.items():
            if v not in cid_to_label:
                cid_to_label[v] = k.upper()

        matched_cids = []
        matched_labels = []

        for cid in unique_cids:
            docs = self._get_collection_documents(cid)
            for d in docs:
                raw_name = (
                        d.get("name")
                        or d.get("title")
                        or d.get("filename")
                        or d.get("original_filename")
                        or ""
                )
                if not raw_name:
                    continue
                name_no_ext = os.path.splitext(str(raw_name))[0]
                name_clean = re.sub(r"[_\-\s\.]", "", name_no_ext.lower())

                if len(name_clean) > 4 and (
                        name_clean in q_clean
                        or (len(name_clean) > 10 and name_clean[:12] in q_clean)
                ):
                    if cid not in matched_cids:
                        matched_cids.append(cid)
                        label = cid_to_label.get(cid, cid)
                        matched_labels.append(f"{label} (via doc '{raw_name}')")
                        self._log(
                            f"Routage par nom de document : '{raw_name}' trouvé dans {label} ({cid})"
                        )

        return matched_cids, matched_labels

    def _match_keywords_in_text(
            self, text: str, routes: Dict[str, str]
    ) -> Tuple[List[str], List[str]]:
        """Recherche les mots-clés de projets dans un texte donné."""
        text_lower = text.lower()
        target_ids = []
        matched_projects = []

        for keyword, collection_id in routes.items():
            # Détection souple : mot entier ou collé avec tiret/underscore/slash
            pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
            if re.search(pattern, text_lower):
                if collection_id not in target_ids:
                    target_ids.append(collection_id)
                clean_label = keyword.upper()
                if clean_label not in matched_projects:
                    matched_projects.append(clean_label)

        return target_ids, matched_projects

    def _resolve_collections(self, messages: list) -> Tuple[List[str], List[str]]:
        """Résout dynamiquement la ou les collections cibles sans rester bloqué sur PASRAU."""
        routes = self._get_routes()
        if not routes:
            self._log("Aucune route configurée dans COLLECTION_ROUTING_JSON.")
            return [], []

        user_messages = [
            self._extract_text(m.get("content"))
            for m in reversed(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        user_messages = [u for u in user_messages if u]

        if not user_messages:
            return [], []

        latest_query = user_messages[0]
        latest_lower = latest_query.lower()

        # 1. Tags explicites prioritaires
        if "#all" in latest_lower or "#tous" in latest_lower:
            all_ids = list(set(routes.values()))
            self._log(f"Routage #all -> {all_ids}")
            return all_ids, ["Toutes les collections"]

        # 2. Recherche de mots-clés dans le DERNIER message en priorité absolue
        ids, labels = self._match_keywords_in_text(latest_query, routes)
        if ids:
            self._log(f"Routage direct sur message courant : {labels} -> {ids}")
            return ids, labels

        # 3. Recherche par nom de document dans toutes les collections
        doc_ids, doc_labels = self._detect_collections_by_document_name(
            latest_query, routes
        )
        if doc_ids:
            return doc_ids, doc_labels

        # 4. Héritage de l'historique UNIQUEMENT si la question actuelle est une relance courte
        # (évite de rester collé à PASRAU quand on change de sujet)
        is_followup = len(latest_query.split()) <= 10 or bool(
            re.match(
                r"^(et|mais|donc|pourquoi|comment|peux-tu|donne|détaille|explique|quel|quelle|quels|quelles)\b",
                latest_lower,
            )
        )
        if is_followup and len(user_messages) > 1:
            for prev_query in user_messages[1:4]:  # On regarde max 3 tours en arrière
                hist_ids, hist_labels = self._match_keywords_in_text(prev_query, routes)
                if hist_ids:
                    self._log(
                        f"Routage hérité de la question précédente (relance) : {hist_labels} -> {hist_ids}"
                    )
                    return hist_ids, hist_labels

        # 5. Repli configurable (si DEFAULT_FALLBACK_COLLECTION est renseigné)
        fallback_key = self.valves.DEFAULT_FALLBACK_COLLECTION.strip().lower()
        if fallback_key and fallback_key in routes:
            cid = routes[fallback_key]
            self._log(f"Routage fallback configuré : {fallback_key.upper()} ({cid})")
            return [cid], [fallback_key.upper()]

        # 6. Recherche multi-collections globale si aucun projet spécifique n'est détecté
        if self.valves.AUTO_SEARCH_ALL_ON_UNKNOWN:
            all_ids = list(set(routes.values()))
            self._log(
                f"Aucun projet spécifique détecté -> recherche transversale sur toutes les collections : {all_ids}"
            )
            return all_ids, ["Multi-collections (Auto)"]

        self._log("Aucune collection sélectionnée.")
        return [], []

    def _find_targeted_document_ids(
            self, query: str, collection_ids: List[str]
    ) -> Tuple[List[str], List[str]]:
        """Vérifie si la requête fait référence à un document précis dans les collections ciblées."""
        matched_doc_ids = []
        matched_doc_names = []
        q_clean = re.sub(r"[_\-\s\.]", "", query.lower())

        for cid in collection_ids:
            docs = self._get_collection_documents(cid)
            for d in docs:
                doc_id = str(d.get("id"))
                raw_name = (
                        d.get("name")
                        or d.get("title")
                        or d.get("filename")
                        or d.get("original_filename")
                        or ""
                )
                if not raw_name:
                    continue

                name_no_ext = os.path.splitext(str(raw_name))[0]
                name_clean = re.sub(r"[_\-\s\.]", "", name_no_ext.lower())

                if len(name_clean) > 4 and (
                        name_clean in q_clean
                        or (len(name_clean) > 8 and name_clean[:12] in q_clean)
                ):
                    if doc_id not in matched_doc_ids:
                        matched_doc_ids.append(doc_id)
                        matched_doc_names.append(raw_name)
                        self._log(
                            f"-> Document ciblé isolé : '{raw_name}' (ID: {doc_id})"
                        )

        return matched_doc_ids, matched_doc_names

    def _get_document_title_from_api(self, doc_id: str) -> str:
        if not doc_id:
            return "Document sans nom"
        if doc_id in self._doc_name_cache:
            return self._doc_name_cache[doc_id]

        headers = {
            "Authorization": f"Bearer {self.valves.ALBERT_API_KEY}",
            "Accept": "application/json",
        }
        url = f"{self.valves.ALBERT_API_BASE_URL.rstrip('/')}/documents/{doc_id}"
        try:
            res = requests.get(url, headers=headers, timeout=4)
            if res.status_code == 200:
                doc_data = res.json()
                title = (
                        doc_data.get("name")
                        or doc_data.get("title")
                        or doc_data.get("filename")
                        or (
                            doc_data.get("data", {}).get("name")
                            if isinstance(doc_data.get("data"), dict)
                            else None
                        )
                )
                if title:
                    clean = os.path.basename(str(title).strip())
                    self._doc_name_cache[doc_id] = clean
                    return clean
        except Exception:
            pass

        fallback = f"Document #{doc_id}"
        self._doc_name_cache[doc_id] = fallback
        return fallback

    def _rerank_chunks(
            self, query: str, candidate_chunks: List[Dict[str, Any]], top_n: int
    ) -> List[Dict[str, Any]]:
        """Appel POST /v1/rerank."""
        if not candidate_chunks:
            return []

        rerank_url = f"{self.valves.ALBERT_API_BASE_URL.rstrip('/')}/rerank"
        headers = {
            "Authorization": f"Bearer {self.valves.ALBERT_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        doc_texts = [c["text"] for c in candidate_chunks]
        payload = {
            "model": self.valves.RERANK_MODEL,
            "query": query,
            "documents": doc_texts,
            "top_n": min(top_n, len(candidate_chunks)),
        }

        try:
            res = requests.post(rerank_url, headers=headers, json=payload, timeout=8)
            if res.status_code != 200:
                self._log(
                    f"Erreur /rerank {res.status_code}: {res.text}. Repli sur l'ordre initial."
                )
                return candidate_chunks[:top_n]

            results = res.json().get("results", [])
            reranked = []
            for item in results:
                idx = item.get("index")
                score = item.get("relevance_score", 0.0)
                if idx is not None and idx < len(candidate_chunks):
                    if score >= self.valves.RERANK_SCORE_THRESHOLD:
                        chunk = candidate_chunks[idx].copy()
                        chunk["rerank_score"] = score
                        reranked.append(chunk)

            return reranked
        except Exception as e:
            self._log(f"Exception /rerank: {e}")
            return candidate_chunks[:top_n]

    def _fetch_albert_context(
            self, query: str, collection_ids: list[str]
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Recherche sémantique multi-collections avec isolation documentaire et reranking."""
        search_url = f"{self.valves.ALBERT_API_BASE_URL.rstrip('/')}/search"
        headers = {
            "Authorization": f"Bearer {self.valves.ALBERT_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        targeted_doc_ids, targeted_doc_names = self._find_targeted_document_ids(
            query, collection_ids
        )

        limit = 50 if targeted_doc_ids else self.valves.RETRIEVAL_LIMIT
        top_k_final = 20 if targeted_doc_ids else self.valves.TOP_K

        payload = {
            "query": query,
            "collection_ids": collection_ids,
            "limit": limit,
            "method": "semantic",
        }

        self._log(
            f"POST /search : query='{query[:50]}...' | collections={collection_ids} | limit={limit}"
        )

        try:
            res = requests.post(search_url, headers=headers, json=payload, timeout=12)
            if res.status_code != 200:
                self._log(f"Erreur /search HTTP {res.status_code}: {res.text}")
                return [], []

            data = res.json()
            raw_results = (
                    data.get("data") or data.get("results") or data.get("documents") or []
            )

            candidate_chunks = []
            for item in raw_results:
                if not isinstance(item, dict):
                    continue

                chunk_obj = (
                    item.get("chunk") if isinstance(item.get("chunk"), dict) else {}
                )
                text = (
                        chunk_obj.get("content")
                        or chunk_obj.get("text")
                        or item.get("content")
                        or item.get("text")
                        or ""
                )
                if not (isinstance(text, str) and text.strip()):
                    continue

                doc_id = str(
                    chunk_obj.get("document_id") or item.get("document_id") or ""
                )
                doc_title = (
                    self._get_document_title_from_api(doc_id)
                    if doc_id
                    else "Document inconnu"
                )

                candidate_chunks.append(
                    {
                        "text": text.strip(),
                        "doc_id": doc_id,
                        "doc_title": doc_title,
                        "initial_score": item.get("score", 0.0),
                    }
                )

            if not candidate_chunks:
                return [], []

            if targeted_doc_ids:
                filtered = [
                    c for c in candidate_chunks if c["doc_id"] in targeted_doc_ids
                ]
                if filtered:
                    self._log(
                        f"Isolation réussie : {len(filtered)} chunks pour {targeted_doc_names}"
                    )
                    candidate_chunks = filtered

            if self.valves.ENABLE_RERANK:
                final_chunks = self._rerank_chunks(
                    query, candidate_chunks, top_n=top_k_final
                )
            else:
                final_chunks = candidate_chunks[:top_k_final]

            chunks_text = []
            citations = []
            for c in final_chunks:
                chunks_text.append(c["text"])
                citations.append(
                    {
                        "source": {"name": c["doc_title"]},
                        "document": [c["text"]],
                        "metadata": [
                            {
                                "source": c["doc_title"],
                                "doc_id": c["doc_id"],
                                "score": c.get("rerank_score", c.get("initial_score")),
                            }
                        ],
                    }
                )

            return chunks_text, citations

        except Exception as e:
            self._log(f"Exception _fetch_albert_context : {e}")
            return [], []

    async def inlet(
            self, body: dict, __user__: dict = None, __event_emitter__=None
    ) -> dict:
        self._log("=== [INLET] Début de traitement ===")
        messages = body.get("messages", [])
        if not messages:
            return body

        last_user_msg = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_msg = self._extract_text(m.get("content"))
                if last_user_msg:
                    break

        if not last_user_msg:
            return body

        clean_lower = last_user_msg.lower()
        available_tools = body.get("tools", [])
        is_tool_resolution_turn = any(
            isinstance(m, dict) and m.get("role") == "tool" for m in messages
        )

        # 1. Purge de l'ancien contexte RAG dans le System Prompt
        system_idx = next(
            (
                i
                for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "system"
            ),
            None,
        )
        if system_idx is not None:
            orig_sys = self._extract_text(messages[system_idx].get("content"))
            clean_content = re.sub(
                r"--- EXTRAITS DOCUMENTS ALBERT RAG[\s\S]*?--- FIN DES EXTRAITS DOCUMENTS ---\s*(DOCUMENTS RAG DISPONIBLES :[^\n]*(\n- 📄 [^\n]*)*)?",
                "",
                orig_sys,
                flags=re.MULTILINE,
            ).strip()
            messages[system_idx]["content"] = clean_content

        instructions_header = [
            "Tu es un assistant technique expert. Tu réponds TOUJOURS en français.",
            "RÈGLE D'INDÉPENDANCE DES SOURCES :",
            "- Les données issues des outils (Mantis, GitLab, Wiki) et les extraits documentaires RAG sont deux canaux complètement indépendants.",
            "- Ne conclus jamais qu'un ticket ou une information est inexistante sous prétexte qu'elle ne figure pas dans les extraits RAG.",
            "- Si un retour d'outil te fournit les détails d'un ticket, exploite-les directement pour répondre.",
        ]

        # Résolution dynamique des outils
        gitlab_fn = self._find_matching_tool_name(
            available_tools, [r"gitlab", r"code", r"repo", r"git"]
        )
        wiki_fn = self._find_matching_tool_name(
            available_tools, [r"wiki", r"search", r"confluence", r"doc"]
        )
        gda_get_fn = self._find_matching_tool_name(
            available_tools, [r"get_ticket", r"read_ticket", r"view_issue"]
        )
        gda_search_fn = self._find_matching_tool_name(
            available_tools, [r"search_issues", r"mantis_search", r"gda_search"]
        )

        tool_rules = []
        id_match = re.search(
            r"(?:gda|ticket|mantis|fiche|anomalie|issue)\D*(\d{4,7})\b|#(\d{4,7})\b",
            clean_lower,
        )

        if id_match and not is_tool_resolution_turn:
            ticket_id = int(id_match.group(1) or id_match.group(2))
            target_fn = gda_get_fn or "get_ticket"
            tool_rules.append(
                f"- Pour récupérer les détails du ticket #{ticket_id}, appelle immédiatement `{target_fn}(issue_id={ticket_id})`.\n"
                f"- Dès que les informations sont obtenues, rédige directement la synthèse sans faire d'autres appels."
            )
        elif (
                re.search(r"\b(gda|mantis|tickets?)\b", clean_lower)
                and not is_tool_resolution_turn
        ):
            target_fn = gda_search_fn or "search_issues"
            tool_rules.append(
                f"- Pour rechercher des tickets correspondants, appelle la fonction `{target_fn}`."
            )

        if (
                re.search(r"\b(gitlab|code|repo|dépôt|branche|commit|tag)\b", clean_lower)
                and not is_tool_resolution_turn
        ):
            target_fn = gitlab_fn or "l'outil GitLab"
            tool_rules.append(
                f"- Pour inspecter le dépôt, les branches ou les tags, appelle la fonction `{target_fn}`."
            )

        if (
                re.search(r"\b(wiki|documentation|confluence|page\s+wiki)\b", clean_lower)
                and not is_tool_resolution_turn
        ):
            target_fn = wiki_fn or "l'outil Wiki"
            tool_rules.append(
                f"- Pour consulter la documentation Wiki, appelle la fonction `{target_fn}`."
            )

        if tool_rules:
            instructions_header.append("CONSIGNES OUTILS :\n" + "\n".join(tool_rules))

        # 2. Résolution dynamique des collections & Recherche RAG
        collection_ids, project_labels = self._resolve_collections(messages)

        if collection_ids and not is_tool_resolution_turn:
            projets_str = ", ".join(project_labels)
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Recherche & reranking Albert ({projets_str})...",
                            "done": False,
                        },
                    }
                )

            chunks, citations = self._fetch_albert_context(
                last_user_msg, collection_ids
            )

            if chunks:
                context_block = "\n\n---\n\n".join(chunks)
                unique_doc_names = list({c["source"]["name"] for c in citations})
                sources_list = "\n".join([f"- 📄 {d}" for d in unique_doc_names])

                rag_block = (
                    f"--- EXTRAITS DOCUMENTS ALBERT RAG ({projets_str}) ---\n"
                    f"{context_block}\n"
                    f"--- FIN DES EXTRAITS DOCUMENTS ---\n\n"
                    f"DOCUMENTS RAG DISPONIBLES :\n{sources_list}\n"
                )
                instructions_header.append(rag_block)

                if __event_emitter__:
                    for citation in citations:
                        await __event_emitter__({"type": "citation", "data": citation})

                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"{len(chunks)} extraits injectés ({len(unique_doc_names)} doc) depuis {projets_str}.",
                                "done": True,
                            },
                        }
                    )
            else:
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"Aucun extrait pertinent trouvé ({projets_str}).",
                                "done": True,
                            },
                        }
                    )

        # 3. Mise à jour du prompt système
        full_instructions = "\n\n".join(instructions_header)
        if system_idx is not None:
            existing = self._extract_text(messages[system_idx].get("content"))
            messages[system_idx]["content"] = f"{full_instructions}\n\n{existing}".strip()
        else:
            messages.insert(0, {"role": "system", "content": full_instructions})

        self._log("=== [INLET] Fin d'injection ===")
        body["messages"] = messages
        return body

    async def outlet(
            self, body: dict, __user__: dict = None, __event_emitter__=None
    ) -> dict:
        return body