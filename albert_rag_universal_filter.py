"""
title: Albert RAG Universal Filter with Smart Multi-Collection Router
author: Open WebUI Community
description: Filtre RAG Albert multi-collections avec routage intelligent par mot-clé ou nom de document, mémoire contextuelle maîtrisée, enrichissement des relances et reranking dynamique asynchrone.
version: 2.7.0
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx
from pydantic import BaseModel, Field


class Filter:

    FILTER_BLOCK_START = "<!-- ALBERT_RAG_FILTER_START -->"
    FILTER_BLOCK_END = "<!-- ALBERT_RAG_FILTER_END -->"

    # Préfixes génériques ignorés lors du matching par préfixe de fichier
    GENERIC_DOC_PREFIXES = (
        "document",
        "documentation",
        "guideutilisateur",
        "guide",
        "manuel",
        "procedure",
        "specification",
        "specifications",
        "annexe",
        "fiche",
        "note",
        "rapport",
        "presentation",
    )

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
            description="Si True et aucun projet détecté (et fallback vide), interroge toutes les collections configurées",
        )
        SEARCH_METHOD: str = Field(
            default="semantic",
            description="Méthode de recherche Albert : 'semantic', 'lexical' ou 'hybrid'",
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
        CACHE_TTL_SECONDS: int = Field(
            default=600,
            description="Durée de vie du cache des listes de documents par collection (en secondes)",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._doc_name_cache: Dict[str, str] = {}
        self._collection_docs_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    def _log(self, message: str) -> None:
        if getattr(self.valves, "DEBUG_LOGS", False):
            print(f"[Albert Filter DEBUG] {message}", flush=True)

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.valves.ALBERT_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _extract_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
            ).strip()
        return str(content or "")

    def _normalize_for_match(self, text: str) -> str:
        return re.sub(r"[_\-\s\.\,/\\:;'\"]+", "", text.lower())

    def _get_routes(self) -> Dict[str, str]:
        try:
            raw = json.loads(self.valves.COLLECTION_ROUTING_JSON)
            return {
                str(k).strip().lower(): str(v).strip()
                for k, v in raw.items()
                if str(v).strip()
            }
        except Exception as e:
            self._log(f"Erreur parsing JSON routes : {e}")
            return {}

    def _find_matching_tool_name(self, tools: list, patterns: List[str]) -> str:
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

    def _extract_doc_raw_name(self, doc: Dict[str, Any]) -> str:
        return str(
            doc.get("name")
            or doc.get("title")
            or doc.get("filename")
            or doc.get("original_filename")
            or ""
        ).strip()

    async def _get_collection_documents(
            self, client: httpx.AsyncClient, collection_id: str
    ) -> List[Dict[str, Any]]:
        """Récupère la liste des documents d'une collection avec cache TTL et pré-remplit le cache des titres."""
        now = time.time()
        if collection_id in self._collection_docs_cache:
            ts, cached_docs = self._collection_docs_cache[collection_id]
            if now - ts < self.valves.CACHE_TTL_SECONDS:
                return cached_docs

        base_url = self.valves.ALBERT_API_BASE_URL.rstrip("/")
        url = f"{base_url}/collections/{collection_id}/documents"
        try:
            res = await client.get(url, headers=self._get_headers(), timeout=6.0)
            if res.status_code == 200:
                data = res.json()
                docs = data.get("data") or data.get("results") or []
                if isinstance(docs, list):
                    self._collection_docs_cache[collection_id] = (now, docs)
                    # Pré-remplissage du cache doc_id -> titre pour éviter les requêtes N+1
                    for d in docs:
                        if isinstance(d, dict) and d.get("id") is not None:
                            doc_id = str(d.get("id"))
                            raw_name = self._extract_doc_raw_name(d)
                            if raw_name:
                                self._doc_name_cache[doc_id] = os.path.basename(raw_name)
                    return docs
            else:
                self._log(
                    f"HTTP {res.status_code} sur GET /collections/{collection_id}/documents"
                )
        except Exception as e:
            self._log(f"Erreur récupération docs collection {collection_id} : {e}")
        return []

    def _is_document_mentioned_in_query(self, raw_name: str, q_clean: str) -> bool:
        """Vérifie de façon robuste si un nom de document est mentionné dans la requête."""
        if not raw_name or len(q_clean) < 5:
            return False

        name_no_ext = os.path.splitext(raw_name)[0]
        name_clean = self._normalize_for_match(name_no_ext)
        if len(name_clean) <= 4:
            return False

        # 1. Correspondance exacte du nom complet normalisé
        if name_clean in q_clean:
            return True

        # 2. Correspondance par préfixe significatif (en excluant les préfixes génériques)
        if len(name_clean) > 10 and name_clean[:12] in q_clean:
            prefix = name_clean[:12]
            if not any(
                    prefix.startswith(gen[: min(len(gen), 8)])
                    for gen in self.GENERIC_DOC_PREFIXES
            ):
                return True

        return False

    async def _scan_documents_across_collections(
            self,
            client: httpx.AsyncClient,
            query: str,
            collection_ids: List[str],
            cid_to_label: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[str], List[str], List[str], List[str]]:
        """
        Fonction unifiée qui scanne les documents d'une liste de collections.
        Retourne : (matched_cids, matched_cid_labels, matched_doc_ids, matched_doc_names)
        """
        q_clean = self._normalize_for_match(query)
        if len(q_clean) < 5:
            return [], [], [], []

        matched_cids: List[str] = []
        matched_cid_labels: List[str] = []
        matched_doc_ids: List[str] = []
        matched_doc_names: List[str] = []

        for cid in collection_ids:
            docs = await self._get_collection_documents(client, cid)
            for d in docs:
                raw_name = self._extract_doc_raw_name(d)
                doc_id = str(d.get("id") or "")
                if self._is_document_mentioned_in_query(raw_name, q_clean):
                    if cid not in matched_cids:
                        matched_cids.append(cid)
                        label = (cid_to_label or {}).get(cid, cid)
                        matched_cid_labels.append(f"{label} (via doc '{raw_name}')")
                    if doc_id and doc_id not in matched_doc_ids:
                        matched_doc_ids.append(doc_id)
                        matched_doc_names.append(raw_name)
                        self._log(
                            f"Document ciblé détecté : '{raw_name}' (doc_id={doc_id}, collection={cid})"
                        )

        return matched_cids, matched_cid_labels, matched_doc_ids, matched_doc_names

    def _match_keywords_in_text(
            self, text: str, routes: Dict[str, str]
    ) -> Tuple[List[str], List[str]]:
        """Recherche les mots-clés de projets dans un texte donné."""
        text_lower = text.lower()
        target_ids: List[str] = []
        matched_projects: List[str] = []

        for keyword, collection_id in routes.items():
            pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
            if re.search(pattern, text_lower):
                if collection_id not in target_ids:
                    target_ids.append(collection_id)
                clean_label = keyword.upper()
                if clean_label not in matched_projects:
                    matched_projects.append(clean_label)

        return target_ids, matched_projects

    async def _resolve_collections_and_query(
            self, client: httpx.AsyncClient, messages: list
    ) -> Tuple[List[str], List[str], str]:
        """
        Résout dynamiquement :
        - les collections cibles
        - leurs libellés
        - la requête de recherche effective (enrichie si c'est une relance courte)
        """
        routes = self._get_routes()
        if not routes:
            self._log("Aucune route configurée dans COLLECTION_ROUTING_JSON.")
            return [], [], ""

        user_messages = [
            self._extract_text(m.get("content"))
            for m in reversed(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        user_messages = [u for u in user_messages if u]

        if not user_messages:
            return [], [], ""

        latest_query = user_messages[0]
        latest_lower = latest_query.lower()
        effective_query = latest_query

        # Construction d'une liste dédupliquée de collections en préservant l'ordre
        all_unique_cids = list(dict.fromkeys(routes.values()))
        cid_to_label: Dict[str, str] = {}
        for k, v in routes.items():
            if v not in cid_to_label:
                cid_to_label[v] = k.upper()

        # Détection de relance courte pour enrichir la requête vectorielle si nécessaire
        is_followup = len(latest_query.split()) <= 10 or bool(
            re.match(
                r"^(et|mais|donc|pourquoi|comment|peux-tu|donne|détaille|explique|quel|quelle|quels|quelles|où|quand)\b",
                latest_lower,
            )
        )
        if is_followup and len(user_messages) > 1:
            effective_query = f"{user_messages[1]} — {latest_query}"

        # 1. Tags explicites prioritaires (#all / #tous)
        if "#all" in latest_lower or "#tous" in latest_lower:
            self._log(f"Routage #all -> {all_unique_cids}")
            return all_unique_cids, ["Toutes les collections"], effective_query

        # 2. Recherche de mots-clés dans le DERNIER message en priorité absolue
        ids, labels = self._match_keywords_in_text(latest_query, routes)
        if ids:
            self._log(f"Routage direct sur message courant : {labels} -> {ids}")
            return ids, labels, effective_query

        # 3. Recherche par nom de document dans toutes les collections
        doc_cids, doc_labels, _, _ = await self._scan_documents_across_collections(
            client, latest_query, all_unique_cids, cid_to_label
        )
        if doc_cids:
            return doc_cids, doc_labels, effective_query

        # 4. Héritage de l'historique UNIQUEMENT si la question actuelle est une relance courte
        if is_followup and len(user_messages) > 1:
            for prev_query in user_messages[1:4]:
                hist_ids, hist_labels = self._match_keywords_in_text(prev_query, routes)
                if hist_ids:
                    self._log(
                        f"Routage hérité de la question précédente (relance) : {hist_labels} -> {hist_ids}"
                    )
                    return hist_ids, hist_labels, effective_query

        # 5. Repli configurable (si DEFAULT_FALLBACK_COLLECTION est renseigné)
        fallback_key = self.valves.DEFAULT_FALLBACK_COLLECTION.strip().lower()
        if fallback_key and fallback_key in routes:
            cid = routes[fallback_key]
            self._log(f"Routage fallback configuré : {fallback_key.upper()} ({cid})")
            return [cid], [fallback_key.upper()], effective_query

        # 6. Recherche multi-collections globale si aucun projet spécifique n'est détecté
        if self.valves.AUTO_SEARCH_ALL_ON_UNKNOWN:
            self._log(
                f"Aucun projet spécifique détecté -> recherche transversale sur toutes les collections : {all_unique_cids}"
            )
            return all_unique_cids, ["Multi-collections (Auto)"], effective_query

        self._log("Aucune collection sélectionnée.")
        return [], [], effective_query

    async def _get_document_title(
            self,
            client: httpx.AsyncClient,
            doc_id: str,
            item_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Récupère le nom du document depuis les métadonnées du chunk, le cache, ou l'API en dernier recours."""
        # 1. Vérifier si le titre est déjà présent dans les métadonnées retournées par /search
        if isinstance(item_metadata, dict):
            meta_name = (
                    item_metadata.get("document_name")
                    or item_metadata.get("name")
                    or item_metadata.get("title")
                    or item_metadata.get("filename")
                    or item_metadata.get("source")
            )
            if meta_name and str(meta_name).strip():
                clean = os.path.basename(str(meta_name).strip())
                if doc_id:
                    self._doc_name_cache[doc_id] = clean
                return clean

        if not doc_id:
            return "Document sans nom"

        # 2. Vérifier le cache (déjà pré-rempli par _get_collection_documents)
        if doc_id in self._doc_name_cache:
            return self._doc_name_cache[doc_id]

        # 3. Appel API unitaire en dernier recours
        url = f"{self.valves.ALBERT_API_BASE_URL.rstrip('/')}/documents/{doc_id}"
        try:
            res = await client.get(url, headers=self._get_headers(), timeout=4.0)
            if res.status_code == 200:
                doc_data = res.json()
                title = self._extract_doc_raw_name(doc_data) or (
                    self._extract_doc_raw_name(doc_data.get("data", {}))
                    if isinstance(doc_data.get("data"), dict)
                    else ""
                )
                if title:
                    clean = os.path.basename(title)
                    self._doc_name_cache[doc_id] = clean
                    return clean
        except Exception as e:
            self._log(f"Erreur récupération titre document {doc_id}: {e}")

        fallback = f"Document #{doc_id}"
        self._doc_name_cache[doc_id] = fallback
        return fallback

    async def _rerank_chunks(
            self,
            client: httpx.AsyncClient,
            query: str,
            candidate_chunks: List[Dict[str, Any]],
            top_n: int,
    ) -> List[Dict[str, Any]]:
        """Appel asynchrone POST /v1/rerank."""
        if not candidate_chunks:
            return []

        rerank_url = f"{self.valves.ALBERT_API_BASE_URL.rstrip('/')}/rerank"
        doc_texts = [c["text"] for c in candidate_chunks]
        payload = {
            "model": self.valves.RERANK_MODEL,
            "query": query,
            "documents": doc_texts,
            "top_n": min(top_n, len(candidate_chunks)),
        }

        try:
            res = await client.post(
                rerank_url, headers=self._get_headers(), json=payload, timeout=10.0
            )
            if res.status_code != 200:
                self._log(
                    f"Erreur /rerank {res.status_code}: {res.text}. Repli sur l'ordre initial."
                )
                return candidate_chunks[:top_n]

            data = res.json()
            results = data.get("results") or data.get("data") or []
            reranked = []
            for item in results:
                idx = item.get("index")
                score = item.get("relevance_score", item.get("score", 0.0))
                if idx is not None and 0 <= idx < len(candidate_chunks):
                    if score >= self.valves.RERANK_SCORE_THRESHOLD:
                        chunk = candidate_chunks[idx].copy()
                        chunk["rerank_score"] = score
                        reranked.append(chunk)

            # Tri explicite par score décroissant au cas où l'API ne garantit pas l'ordre
            reranked.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
            return reranked[:top_n]
        except Exception as e:
            self._log(f"Exception /rerank: {e}")
            return candidate_chunks[:top_n]

    async def _fetch_albert_context(
            self,
            client: httpx.AsyncClient,
            raw_query: str,
            search_query: str,
            collection_ids: List[str],
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Recherche sémantique/hybride multi-collections avec isolation documentaire et reranking."""
        search_url = f"{self.valves.ALBERT_API_BASE_URL.rstrip('/')}/search"

        _, _, targeted_doc_ids, targeted_doc_names = (
            await self._scan_documents_across_collections(
                client, raw_query, collection_ids
            )
        )

        limit = 50 if targeted_doc_ids else self.valves.RETRIEVAL_LIMIT
        top_k_final = 20 if targeted_doc_ids else self.valves.TOP_K

        payload = {
            "query": search_query,
            "collections": collection_ids,
            "collection_ids": collection_ids,
            "limit": limit,
            "method": self.valves.SEARCH_METHOD,
        }

        self._log(
            f"POST /search : query='{search_query[:60]}...' | collections={collection_ids} | limit={limit} | method={self.valves.SEARCH_METHOD}"
        )

        try:
            res = await client.post(
                search_url, headers=self._get_headers(), json=payload, timeout=12.0
            )
            if res.status_code != 200:
                self._log(f"Erreur /search HTTP {res.status_code}: {res.text}")
                return [], []

            data = res.json()
            raw_results = (
                    data.get("data") or data.get("results") or data.get("documents") or []
            )

            candidate_chunks: List[Dict[str, Any]] = []
            seen_texts = set()

            for item in raw_results:
                if not isinstance(item, dict):
                    continue

                chunk_obj = (
                    item.get("chunk") if isinstance(item.get("chunk"), dict) else {}
                )
                metadata_obj = (
                    chunk_obj.get("metadata")
                    if isinstance(chunk_obj.get("metadata"), dict)
                    else (
                        item.get("metadata")
                        if isinstance(item.get("metadata"), dict)
                        else {}
                    )
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

                clean_text = text.strip()
                # Déduplication des chunks identiques
                text_hash = hash(clean_text)
                if text_hash in seen_texts:
                    continue
                seen_texts.add(text_hash)

                doc_id = str(
                    chunk_obj.get("document_id")
                    or item.get("document_id")
                    or metadata_obj.get("document_id")
                    or ""
                )
                doc_title = await self._get_document_title(client, doc_id, metadata_obj)

                candidate_chunks.append(
                    {
                        "text": clean_text,
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
                final_chunks = await self._rerank_chunks(
                    client, search_query, candidate_chunks, top_n=top_k_final
                )
            else:
                final_chunks = candidate_chunks[:top_k_final]

            # Formatage des extraits avec mention explicite de la source et regroupement des citations par document
            formatted_chunks: List[str] = []
            grouped_citations: Dict[str, Dict[str, Any]] = {}

            for idx, c in enumerate(final_chunks, start=1):
                doc_title = c["doc_title"]
                score = c.get("rerank_score", c.get("initial_score"))
                formatted_chunks.append(
                    f"[Extrait #{idx} | Source : {doc_title}]\n{c['text']}"
                )

                if doc_title not in grouped_citations:
                    grouped_citations[doc_title] = {
                        "source": {"name": doc_title},
                        "document": [],
                        "metadata": [],
                    }
                grouped_citations[doc_title]["document"].append(c["text"])
                grouped_citations[doc_title]["metadata"].append(
                    {
                        "source": doc_title,
                        "doc_id": c["doc_id"],
                        "score": round(float(score), 4) if isinstance(score, (int, float)) else score,
                    }
                )

            return formatted_chunks, list(grouped_citations.values())

        except Exception as e:
            self._log(f"Exception _fetch_albert_context : {e}")
            return [], []

    def _is_current_turn_tool_resolution(self, messages: list) -> bool:
        """
        Vérifie si le tour actuel fait suite à un retour d'outil (après le dernier message 'user'),
        sans être pollué par les appels d'outils des tours précédents.
        """
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "tool":
                return True
            if role == "user":
                return False
        return False

    async def inlet(
            self, body: dict, __user__: dict = None, __event_emitter__=None
    ) -> dict:
        self._log("=== [INLET] Début de traitement ===")
        messages = body.get("messages", [])
        if not messages:
            return body

        if not self.valves.ALBERT_API_KEY.strip():
            self._log("ATTENTION : ALBERT_API_KEY non renseignée dans les Valves.")

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
        is_tool_resolution_turn = self._is_current_turn_tool_resolution(messages)

        # 1. Purge complète du bloc injecté par ce filtre lors des tours précédents
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
            # Purge du bloc balisé v2.7+
            pattern_v27 = rf"{re.escape(self.FILTER_BLOCK_START)}[\s\S]*?{re.escape(self.FILTER_BLOCK_END)}"
            clean_content = re.sub(pattern_v27, "", orig_sys).strip()
            # Rétro-compatibilité : purge aussi l'ancien format v2.6 s'il était présent dans la session
            clean_content = re.sub(
                r"--- EXTRAITS DOCUMENTS ALBERT RAG[\s\S]*?--- FIN DES EXTRAITS DOCUMENTS ---\s*(DOCUMENTS RAG DISPONIBLES :[^\n]*(\n- 📄 [^\n]*)*)?",
                "",
                clean_content,
                flags=re.MULTILINE,
            ).strip()
            messages[system_idx]["content"] = clean_content

        instructions_header = [
            "Tu es un assistant technique expert. Tu réponds TOUJOURS en français.",
            "RÈGLE D'INDÉPENDANCE DES SOURCES :",
            "- Les données issues des outils (Mantis, GitLab, Wiki) et les extraits documentaires RAG sont deux canaux complètement indépendants.",
            "- Ne conclus jamais qu'un ticket ou une information est inexistante sous prétexte qu'elle ne figure pas dans les extraits RAG.",
            "- Si un retour d'outil te fournit les détails d'un ticket, exploite-les directement pour répondre.",
            "- Lorsque tu utilises les extraits RAG, cite explicitement le nom du document source.",
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

        # 2. Résolution dynamique des collections & Recherche RAG asynchrone
        if not is_tool_resolution_turn and self.valves.ALBERT_API_KEY.strip():
            async with httpx.AsyncClient() as client:
                collection_ids, project_labels, effective_query = (
                    await self._resolve_collections_and_query(client, messages)
                )

                if collection_ids:
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

                    chunks, citations = await self._fetch_albert_context(
                        client, last_user_msg, effective_query, collection_ids
                    )

                    if chunks:
                        context_block = "\n\n---\n\n".join(chunks)
                        unique_doc_names = [c["source"]["name"] for c in citations]
                        sources_list = "\n".join(
                            [f"- 📄 {d}" for d in unique_doc_names]
                        )

                        rag_block = (
                            f"--- EXTRAITS DOCUMENTS ALBERT RAG ({projets_str}) ---\n"
                            f"{context_block}\n"
                            f"--- FIN DES EXTRAITS DOCUMENTS ---\n\n"
                            f"DOCUMENTS RAG DISPONIBLES :\n{sources_list}"
                        )
                        instructions_header.append(rag_block)

                        if __event_emitter__:
                            for citation in citations:
                                await __event_emitter__(
                                    {"type": "citation", "data": citation}
                                )

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

        # 3. Mise à jour propre et idempotente du prompt système
        injected_block = (
                f"{self.FILTER_BLOCK_START}\n"
                + "\n\n".join(instructions_header)
                + f"\n{self.FILTER_BLOCK_END}"
        )

        if system_idx is not None:
            existing = self._extract_text(messages[system_idx].get("content"))
            messages[system_idx]["content"] = (
                f"{injected_block}\n\n{existing}".strip()
                if existing
                else injected_block
            )
        else:
            messages.insert(0, {"role": "system", "content": injected_block})

        self._log("=== [INLET] Fin d'injection ===")
        body["messages"] = messages
        return body

    async def outlet(
            self, body: dict, __user__: dict = None, __event_emitter__=None
    ) -> dict:
        return body