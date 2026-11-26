"""
title: Albert RAG Universal Filter with Smart Multi-Collection Router
author: Open WebUI Community
description: Filtre RAG Albert multi-collections avec routage intelligent par mot-clé ou nom de document, mémoire du document actif par conversation, filtrage serveur par document_ids, budget de contexte et reranking dynamique asynchrone.
version: 2.8.0
"""

import json
import os
import re
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel, Field


class Filter:

    FILTER_BLOCK_START = "<!-- ALBERT_RAG_FILTER_START -->"
    FILTER_BLOCK_END = "<!-- ALBERT_RAG_FILTER_END -->"

    RESET_TAGS = ("#reset", "#nouveau", "#new")
    ALL_TAGS = ("#all", "#tous")

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

    # Mots ignorés lors du matching par tokens du nom de fichier
    GENERIC_NAME_TOKENS = {
        "guide", "document", "doc", "docs", "manuel", "procedure", "version",
        "final", "finale", "annexe", "fiche", "note", "notes", "rapport",
        "pdf", "docx", "doc", "xlsx", "pptx", "txt", "md", "odt",
        "de", "du", "des", "la", "le", "les", "et", "en", "pour", "sur", "the",
        "and", "of", "v1", "v2", "v3", "copie", "draft",
    }

    FOLLOWUP_REGEX = re.compile(
        r"^(et|mais|donc|alors|aussi|pourquoi|comment|peux[- ]tu|pourrais[- ]tu|"
        r"donne|détaille|detaille|explique|précise|precise|résume|resume|liste|"
        r"quel|quelle|quels|quelles|où|ou|quand|combien|qui|dans ce|ce document|"
        r"celui|celle|le même|la même|idem|ok|d'accord|merci)\b",
        flags=re.IGNORECASE,
    )

    MAX_CHAT_STATES = 500
    MAX_DOC_NAME_CACHE = 5000

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
        STICKY_CONTEXT: bool = Field(
            default=True,
            description="Mémoriser par conversation le projet/document actif et le réutiliser pour les questions suivantes (#reset pour l'oublier, #all pour tout interroger)",
        )
        STICKY_TTL_SECONDS: int = Field(
            default=4 * 3600,
            description="Durée de vie de la mémoire du document actif par conversation (en secondes)",
        )
        HISTORY_SCAN_DEPTH: int = Field(
            default=6,
            description="Nombre de questions précédentes analysées pour retrouver un document/projet si la mémoire est vide (ex : après redémarrage)",
        )
        SEARCH_METHOD: str = Field(
            default="semantic",
            description="Méthode de recherche Albert : 'semantic', 'lexical' ou 'hybrid' (hybrid recommandé pour les acronymes métier)",
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
            description="Nombre de chunks bruts collectés via /search (max 100)",
        )
        TOP_K: int = Field(
            default=12,
            description="Nombre final d'extraits injectés",
        )
        TOP_K_TARGETED_DOC: int = Field(
            default=15,
            description="Nombre final d'extraits injectés quand un document précis est ciblé",
        )
        RERANK_SCORE_THRESHOLD: float = Field(
            default=-10.0,
            description="Score minimal de pertinence du rerank",
        )
        MAX_RAG_CONTEXT_CHARS: int = Field(
            default=12000,
            description="Budget maximal (en caractères) des extraits injectés (~4 caractères = 1 token). À réduire pour les modèles locaux à petit contexte",
        )
        MAX_HISTORY_MESSAGES: int = Field(
            default=0,
            description="Si > 0, ne conserve que les N derniers messages de l'historique envoyés au LLM (évite le dépassement de contexte). 0 = désactivé",
        )
        CACHE_TTL_SECONDS: int = Field(
            default=600,
            description="Durée de vie du cache des listes de documents par collection (en secondes)",
        )

    def __init__(self):
        self.valves = self.Valves()
        self._doc_name_cache: Dict[str, str] = {}
        self._collection_docs_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        # Mémoire par conversation : chat_id -> état du projet/document actif
        self._chat_states: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # Utilitaires
    # ------------------------------------------------------------------ #

    def _log(self, message: str) -> None:
        if getattr(self.valves, "DEBUG_LOGS", False):
            print(f"[Albert Filter DEBUG] {message}", flush=True)

    def _base_url(self) -> str:
        return self.valves.ALBERT_API_BASE_URL.rstrip("/")

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.valves.ALBERT_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    @staticmethod
    def _to_api_id(value: Any) -> Any:
        """L'API Albert attend des IDs entiers ; on convertit quand c'est possible."""
        s = str(value).strip()
        return int(s) if s.isdigit() else s

    async def _emit_status(self, emitter, description: str, done: bool) -> None:
        if not emitter:
            return
        try:
            await emitter(
                {"type": "status", "data": {"description": description, "done": done}}
            )
        except Exception as e:
            self._log(f"Erreur émission status : {e}")

    def _extract_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in content
                if not (isinstance(item, dict) and item.get("type") == "image_url")
            ).strip()
        return str(content or "")

    def _normalize_for_match(self, text: str) -> str:
        return re.sub(r"[_\-\s\.\,/\\:;'\"()\[\]]+", "", text.lower())

    def _tokenize(self, text: str) -> List[str]:
        return [t for t in re.split(r"[^a-z0-9àâäéèêëîïôöùûüç]+", text.lower()) if t]

    def _strip_tags(self, text: str) -> str:
        for tag in self.RESET_TAGS + self.ALL_TAGS:
            text = re.sub(re.escape(tag), "", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def _has_tag(self, text_lower: str, tags: Tuple[str, ...]) -> bool:
        return any(
            re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text_lower) for t in tags
        )

    def _get_routes(self) -> Dict[str, str]:
        try:
            raw = json.loads(self.valves.COLLECTION_ROUTING_JSON)
            if not isinstance(raw, dict):
                self._log("COLLECTION_ROUTING_JSON doit être un objet JSON {mot_cle: id}.")
                return {}
            return {
                str(k).strip().lower(): str(v).strip()
                for k, v in raw.items()
                if str(k).strip() and str(v).strip()
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

    def _extract_doc_raw_name(self, doc: Any) -> str:
        if not isinstance(doc, dict):
            return ""
        return str(
            doc.get("name")
            or doc.get("title")
            or doc.get("filename")
            or doc.get("original_filename")
            or ""
        ).strip()

    def _cache_doc_name(self, doc_id: str, name: str) -> None:
        if len(self._doc_name_cache) > self.MAX_DOC_NAME_CACHE:
            self._doc_name_cache.clear()
        self._doc_name_cache[doc_id] = name

    # ------------------------------------------------------------------ #
    # Mémoire par conversation (corrige la perte du document actif)
    # ------------------------------------------------------------------ #

    def _get_chat_id(self, body: dict, metadata: Optional[dict]) -> Optional[str]:
        candidates = [metadata, body.get("metadata")]
        for src in candidates:
            if isinstance(src, dict) and src.get("chat_id"):
                return str(src["chat_id"])
        if body.get("chat_id"):
            return str(body["chat_id"])
        return None

    def _get_state(self, chat_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not chat_id or not self.valves.STICKY_CONTEXT:
            return None
        state = self._chat_states.get(chat_id)
        if not state:
            return None
        if time.time() - state.get("ts", 0) > self.valves.STICKY_TTL_SECONDS:
            self._chat_states.pop(chat_id, None)
            return None
        return state

    def _save_state(self, chat_id: Optional[str], target: Dict[str, Any]) -> None:
        if not chat_id or not self.valves.STICKY_CONTEXT:
            return
        if len(self._chat_states) >= self.MAX_CHAT_STATES and chat_id not in self._chat_states:
            oldest = min(self._chat_states, key=lambda k: self._chat_states[k].get("ts", 0))
            self._chat_states.pop(oldest, None)
        self._chat_states[chat_id] = {
            "collection_ids": list(target["collection_ids"]),
            "labels": list(target["labels"]),
            "doc_ids": list(target["doc_ids"]),
            "doc_names": list(target["doc_names"]),
            "ts": time.time(),
        }
        self._log(
            f"Mémoire chat {chat_id[:8]}… : collections={target['collection_ids']} docs={target['doc_names']}"
        )

    def _clear_state(self, chat_id: Optional[str]) -> None:
        if chat_id:
            self._chat_states.pop(chat_id, None)

    # ------------------------------------------------------------------ #
    # Documents des collections
    # ------------------------------------------------------------------ #

    async def _get_collection_documents(
        self, client: httpx.AsyncClient, collection_id: str
    ) -> List[Dict[str, Any]]:
        """
        Liste paginée des documents d'une collection (cache TTL).
        API Albert actuelle : GET /v1/documents?collection_id=X&limit=100&offset=N
        (repli sur l'ancien GET /v1/collections/{id}/documents).
        """
        now = time.time()
        cached = self._collection_docs_cache.get(collection_id)
        if cached and now - cached[0] < self.valves.CACHE_TTL_SECONDS:
            return cached[1]

        docs: List[Dict[str, Any]] = []
        ok = False
        page_size = 100
        try:
            for page in range(20):  # 2000 documents max par collection
                res = await client.get(
                    f"{self._base_url()}/documents",
                    headers=self._get_headers(),
                    params={
                        "collection_id": self._to_api_id(collection_id),
                        "limit": page_size,
                        "offset": page * page_size,
                    },
                    timeout=8.0,
                )
                if res.status_code != 200:
                    self._log(
                        f"HTTP {res.status_code} sur GET /documents?collection_id={collection_id} : {res.text[:200]}"
                    )
                    break
                ok = True
                data = res.json()
                batch = data.get("data") if isinstance(data, dict) else data
                batch = batch if isinstance(batch, list) else []
                docs.extend(d for d in batch if isinstance(d, dict))
                if len(batch) < page_size:
                    break

            if not ok:
                res = await client.get(
                    f"{self._base_url()}/collections/{collection_id}/documents",
                    headers=self._get_headers(),
                    timeout=8.0,
                )
                if res.status_code == 200:
                    ok = True
                    data = res.json()
                    batch = (data.get("data") or data.get("results") or []) if isinstance(data, dict) else data
                    docs = [d for d in batch if isinstance(d, dict)] if isinstance(batch, list) else []
        except Exception as e:
            self._log(f"Erreur récupération docs collection {collection_id} : {e}")

        if ok:
            # On ne garde que les documents réellement rattachés à cette collection
            docs = [
                d for d in docs
                if d.get("collection_id") is None
                or str(d.get("collection_id")) == str(collection_id)
            ]
            self._collection_docs_cache[collection_id] = (now, docs)
            for d in docs:
                if d.get("id") is not None:
                    raw_name = self._extract_doc_raw_name(d)
                    if raw_name:
                        self._cache_doc_name(str(d["id"]), os.path.basename(raw_name))
            self._log(f"Collection {collection_id} : {len(docs)} documents indexés en cache")
            return docs

        # En cas d'échec, on ne met pas en cache pour réessayer au prochain tour
        return cached[1] if cached else []

    def _is_document_mentioned_in_query(self, raw_name: str, query: str) -> bool:
        """Vérifie de façon robuste si un nom de document est mentionné dans la requête."""
        if not raw_name:
            return False
        q_clean = self._normalize_for_match(query)
        if len(q_clean) < 5:
            return False

        name_no_ext = os.path.splitext(raw_name)[0]
        name_clean = self._normalize_for_match(name_no_ext)
        if len(name_clean) <= 4:
            return False

        # 1. Nom complet normalisé présent dans la requête
        if name_clean in q_clean:
            return True

        # 2. Préfixe significatif (hors préfixes génériques)
        if len(name_clean) > 10 and name_clean[:12] in q_clean:
            prefix = name_clean[:12]
            if not any(
                prefix.startswith(gen[: min(len(gen), 8)])
                for gen in self.GENERIC_DOC_PREFIXES
            ):
                return True

        # 3. Tous les tokens significatifs du nom (>= 2) présents dans la requête
        name_tokens = [
            t for t in self._tokenize(name_no_ext)
            if len(t) >= 3 and t not in self.GENERIC_NAME_TOKENS
        ]
        if len(name_tokens) >= 2:
            q_tokens = set(self._tokenize(query))
            if all(t in q_tokens for t in name_tokens):
                return True

        return False

    async def _scan_documents(
        self,
        client: httpx.AsyncClient,
        query: str,
        collection_ids: List[str],
    ) -> Tuple[List[str], List[str], List[str]]:
        """Retourne (collections_concernées, doc_ids, doc_names) des documents cités dans la requête."""
        if len(self._normalize_for_match(query)) < 5:
            return [], [], []

        matched_cids: List[str] = []
        matched_doc_ids: List[str] = []
        matched_doc_names: List[str] = []

        for cid in collection_ids:
            docs = await self._get_collection_documents(client, cid)
            for d in docs:
                raw_name = self._extract_doc_raw_name(d)
                doc_id = str(d.get("id") or "")
                if doc_id and self._is_document_mentioned_in_query(raw_name, query):
                    if cid not in matched_cids:
                        matched_cids.append(cid)
                    if doc_id not in matched_doc_ids:
                        matched_doc_ids.append(doc_id)
                        matched_doc_names.append(os.path.basename(raw_name))
                        self._log(
                            f"Document cité détecté : '{raw_name}' (doc_id={doc_id}, collection={cid})"
                        )

        return matched_cids, matched_doc_ids, matched_doc_names

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

    # ------------------------------------------------------------------ #
    # Résolution de la cible (projet + document) pour le tour courant
    # ------------------------------------------------------------------ #

    def _is_followup(self, query: str) -> bool:
        return len(query.split()) <= 10 or bool(self.FOLLOWUP_REGEX.match(query.strip()))

    def _build_search_query(self, latest: str, user_msgs: List[str], is_followup: bool) -> str:
        latest_clean = self._strip_tags(latest) or latest
        if is_followup and len(user_msgs) > 1:
            prev = self._strip_tags(user_msgs[1])[:300]
            if prev:
                return f"{prev} — {latest_clean}"
        return latest_clean

    @staticmethod
    def _make_target(cids, labels, doc_ids, doc_names, source, search_query) -> Dict[str, Any]:
        return {
            "collection_ids": list(dict.fromkeys(cids)),
            "labels": labels,
            "doc_ids": list(dict.fromkeys(doc_ids)),
            "doc_names": doc_names,
            "source": source,
            "search_query": search_query,
        }

    async def _resolve_target(
        self, client: httpx.AsyncClient, messages: list, chat_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        routes = self._get_routes()
        if not routes:
            self._log("Aucune route configurée dans COLLECTION_ROUTING_JSON.")
            return None

        user_msgs = [
            self._extract_text(m.get("content"))
            for m in reversed(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        user_msgs = [u for u in user_msgs if u.strip()]
        if not user_msgs:
            return None

        latest = user_msgs[0]
        latest_lower = latest.lower()
        latest_clean = self._strip_tags(latest)
        all_cids = list(dict.fromkeys(routes.values()))
        cid_to_label: Dict[str, str] = {}
        for k, v in routes.items():
            cid_to_label.setdefault(v, k.upper())

        is_followup = self._is_followup(latest_clean or latest)
        search_query = self._build_search_query(latest, user_msgs, is_followup)

        # 0. Réinitialisation explicite de la mémoire
        if self._has_tag(latest_lower, self.RESET_TAGS):
            self._log("Tag #reset : mémoire du document actif effacée.")
            self._clear_state(chat_id)
            state = None
            search_query = latest_clean or latest
        else:
            state = self._get_state(chat_id)

        # 1. #all / #tous
        if self._has_tag(latest_lower, self.ALL_TAGS):
            target = self._make_target(all_cids, ["Toutes les collections"], [], [], "explicit", search_query)
            self._save_state(chat_id, target)
            return target

        # 2. Document cité explicitement dans le message courant (le plus spécifique)
        d_cids, d_ids, d_names = await self._scan_documents(client, latest, all_cids)
        if d_ids:
            labels = [cid_to_label.get(c, c) for c in d_cids]
            target = self._make_target(d_cids, labels, d_ids, d_names, "explicit", latest_clean or latest)
            self._save_state(chat_id, target)
            return target

        # 3. Mot-clé projet dans le message courant
        kw_ids, kw_labels = self._match_keywords_in_text(latest, routes)
        if kw_ids:
            keep_docs = bool(
                state
                and state.get("doc_ids")
                and is_followup
                and set(state["collection_ids"]).issubset(set(kw_ids))
            )
            target = self._make_target(
                kw_ids,
                kw_labels,
                state["doc_ids"] if keep_docs else [],
                state["doc_names"] if keep_docs else [],
                "explicit",
                search_query,
            )
            self._save_state(chat_id, target)
            return target

        # 4. Mémoire de la conversation (document/projet actif)
        if state:
            self._log(f"Réutilisation du contexte mémorisé : {state['labels']} / {state['doc_names']}")
            target = self._make_target(
                state["collection_ids"], state["labels"], state["doc_ids"],
                state["doc_names"], "sticky", search_query,
            )
            self._save_state(chat_id, target)  # rafraîchit le TTL
            return target

        # 5. Pas de mémoire (nouveau process, chat temporaire…) : on relit l'historique
        for prev in user_msgs[1 : 1 + max(0, self.valves.HISTORY_SCAN_DEPTH)]:
            p_cids, p_ids, p_names = await self._scan_documents(client, prev, all_cids)
            if p_ids:
                labels = [cid_to_label.get(c, c) for c in p_cids]
                target = self._make_target(p_cids, labels, p_ids, p_names, "history", search_query)
                self._save_state(chat_id, target)
                return target
            h_ids, h_labels = self._match_keywords_in_text(prev, routes)
            if h_ids:
                target = self._make_target(h_ids, h_labels, [], [], "history", search_query)
                self._save_state(chat_id, target)
                return target

        # 6. Repli configuré
        fallback_key = self.valves.DEFAULT_FALLBACK_COLLECTION.strip().lower()
        if fallback_key and fallback_key in routes:
            return self._make_target(
                [routes[fallback_key]], [fallback_key.upper()], [], [], "fallback", search_query
            )

        # 7. Recherche transversale
        if self.valves.AUTO_SEARCH_ALL_ON_UNKNOWN:
            return self._make_target(all_cids, ["Multi-collections (Auto)"], [], [], "auto", search_query)

        return None

    # ------------------------------------------------------------------ #
    # Recherche, titres, rerank
    # ------------------------------------------------------------------ #

    async def _get_document_title(
        self,
        client: httpx.AsyncClient,
        doc_id: str,
        item_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
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
                    self._cache_doc_name(doc_id, clean)
                return clean

        if not doc_id:
            return "Document sans nom"
        if doc_id in self._doc_name_cache:
            return self._doc_name_cache[doc_id]

        try:
            res = await client.get(
                f"{self._base_url()}/documents/{doc_id}", headers=self._get_headers(), timeout=4.0
            )
            if res.status_code == 200:
                doc_data = res.json()
                title = self._extract_doc_raw_name(doc_data) or self._extract_doc_raw_name(
                    doc_data.get("data") if isinstance(doc_data, dict) else None
                )
                if title:
                    clean = os.path.basename(title)
                    self._cache_doc_name(doc_id, clean)
                    return clean
        except Exception as e:
            self._log(f"Erreur récupération titre document {doc_id}: {e}")

        fallback = f"Document #{doc_id}"
        self._cache_doc_name(doc_id, fallback)
        return fallback

    async def _search(
        self,
        client: httpx.AsyncClient,
        query: str,
        collection_ids: List[str],
        doc_ids: List[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        method = (self.valves.SEARCH_METHOD or "semantic").strip().lower()
        if method not in ("semantic", "lexical", "hybrid"):
            method = "semantic"

        payload: Dict[str, Any] = {
            "query": query,
            "collection_ids": [self._to_api_id(c) for c in collection_ids],
            "limit": max(1, min(int(limit), 100)),
            "method": method,
        }
        if doc_ids:
            payload["document_ids"] = [self._to_api_id(d) for d in doc_ids][:100]

        self._log(
            f"POST /search : query='{query[:60]}…' | collections={collection_ids} | docs={doc_ids} | limit={payload['limit']} | method={method}"
        )
        res = await client.post(
            f"{self._base_url()}/search", headers=self._get_headers(), json=payload, timeout=15.0
        )
        if res.status_code != 200 and method != "semantic":
            self._log(f"/search {method} HTTP {res.status_code}, nouvel essai en 'semantic'.")
            payload["method"] = "semantic"
            res = await client.post(
                f"{self._base_url()}/search", headers=self._get_headers(), json=payload, timeout=15.0
            )
        if res.status_code != 200:
            self._log(f"Erreur /search HTTP {res.status_code}: {res.text[:300]}")
            return []

        data = res.json()
        if isinstance(data, list):
            return data
        return data.get("data") or data.get("results") or data.get("documents") or []

    async def _rerank_chunks(
        self,
        client: httpx.AsyncClient,
        query: str,
        candidate_chunks: List[Dict[str, Any]],
        top_n: int,
    ) -> List[Dict[str, Any]]:
        if not candidate_chunks:
            return []

        payload = {
            "model": self.valves.RERANK_MODEL,
            "query": query,
            "documents": [c["text"] for c in candidate_chunks],
            "top_n": min(top_n, len(candidate_chunks)),
        }
        try:
            res = await client.post(
                f"{self._base_url()}/rerank", headers=self._get_headers(), json=payload, timeout=12.0
            )
            if res.status_code != 200:
                self._log(f"Erreur /rerank {res.status_code}: {res.text[:300]}. Repli sur l'ordre initial.")
                return candidate_chunks[:top_n]

            data = res.json()
            results = data.get("results") or data.get("data") or []
            reranked = []
            for item in results:
                idx = item.get("index")
                score = item.get("relevance_score", item.get("score", 0.0))
                if isinstance(idx, int) and 0 <= idx < len(candidate_chunks):
                    if score is not None and score >= self.valves.RERANK_SCORE_THRESHOLD:
                        chunk = candidate_chunks[idx].copy()
                        chunk["rerank_score"] = score
                        reranked.append(chunk)

            reranked.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
            # Si le seuil a tout éliminé, on garde au moins les meilleurs résultats initiaux
            return reranked[:top_n] if reranked else candidate_chunks[: min(3, top_n)]
        except Exception as e:
            self._log(f"Exception /rerank: {e}")
            return candidate_chunks[:top_n]

    async def _parse_results(
        self, client: httpx.AsyncClient, raw_results: List[Any]
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        seen = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            chunk_obj = item.get("chunk") if isinstance(item.get("chunk"), dict) else {}
            metadata_obj = (
                chunk_obj.get("metadata") if isinstance(chunk_obj.get("metadata"), dict)
                else item.get("metadata") if isinstance(item.get("metadata"), dict)
                else {}
            )
            text = (
                chunk_obj.get("content") or chunk_obj.get("text")
                or item.get("content") or item.get("text") or ""
            )
            if not (isinstance(text, str) and text.strip()):
                continue
            clean_text = text.strip()
            if clean_text in seen:
                continue
            seen.add(clean_text)

            doc_id = str(
                chunk_obj.get("document_id") or item.get("document_id")
                or metadata_obj.get("document_id") or ""
            )
            candidates.append(
                {
                    "text": clean_text,
                    "doc_id": doc_id,
                    "doc_title": await self._get_document_title(client, doc_id, metadata_obj),
                    "initial_score": item.get("score", 0.0),
                }
            )
        return candidates

    async def _fetch_albert_context(
        self, client: httpx.AsyncClient, target: Dict[str, Any]
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        collection_ids = target["collection_ids"]
        doc_ids = target["doc_ids"]
        query = target["search_query"]

        limit = 50 if doc_ids else self.valves.RETRIEVAL_LIMIT
        top_k = self.valves.TOP_K_TARGETED_DOC if doc_ids else self.valves.TOP_K

        try:
            raw = await self._search(client, query, collection_ids, doc_ids, limit)
            candidates = await self._parse_results(client, raw)

            if doc_ids:
                # Filet de sécurité si l'API ignore document_ids
                filtered = [c for c in candidates if c["doc_id"] in doc_ids]
                if filtered:
                    candidates = filtered
                elif not candidates:
                    self._log("Aucun résultat dans le document ciblé, élargissement à la collection.")
                    raw = await self._search(client, query, collection_ids, [], self.valves.RETRIEVAL_LIMIT)
                    candidates = await self._parse_results(client, raw)

            if not candidates:
                return [], []

            if self.valves.ENABLE_RERANK:
                final_chunks = await self._rerank_chunks(client, query, candidates, top_k)
            else:
                final_chunks = candidates[:top_k]

            formatted: List[str] = []
            grouped: Dict[str, Dict[str, Any]] = {}
            budget = max(1000, self.valves.MAX_RAG_CONTEXT_CHARS)
            used = 0

            for c in final_chunks:
                block = f"[Extrait #{len(formatted) + 1} | Source : {c['doc_title']}]\n{c['text']}"
                if used + len(block) > budget:
                    if not formatted:
                        block = block[:budget]
                    else:
                        self._log(f"Budget de contexte atteint ({used} car.), extraits restants ignorés.")
                        break
                formatted.append(block)
                used += len(block)

                score = c.get("rerank_score", c.get("initial_score"))
                g = grouped.setdefault(
                    c["doc_title"],
                    {"source": {"name": c["doc_title"]}, "document": [], "metadata": []},
                )
                g["document"].append(c["text"])
                g["metadata"].append(
                    {
                        "source": c["doc_title"],
                        "doc_id": c["doc_id"],
                        "score": round(float(score), 4) if isinstance(score, (int, float)) else score,
                    }
                )

            return formatted, list(grouped.values())
        except Exception as e:
            self._log(f"Exception _fetch_albert_context : {e}")
            return [], []

    # ------------------------------------------------------------------ #
    # Pipeline Open WebUI
    # ------------------------------------------------------------------ #

    def _is_current_turn_tool_resolution(self, messages: list) -> bool:
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "tool":
                return True
            if role == "user":
                return False
        return False

    def _trim_history(self, messages: list) -> list:
        n = self.valves.MAX_HISTORY_MESSAGES
        if n <= 0:
            return messages
        system_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
        others = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
        if len(others) <= n:
            return messages
        kept = others[-n:]
        # Ne jamais commencer par une réponse d'outil / d'assistant orpheline
        while kept and isinstance(kept[0], dict) and kept[0].get("role") != "user":
            kept.pop(0)
        if not kept:
            kept = others[-1:]
        self._log(f"Historique tronqué : {len(others)} -> {len(kept)} messages")
        return system_msgs + kept

    def _purge_previous_injection(self, text: str) -> str:
        pattern = rf"{re.escape(self.FILTER_BLOCK_START)}[\s\S]*?{re.escape(self.FILTER_BLOCK_END)}"
        text = re.sub(pattern, "", text)
        text = re.sub(
            r"--- EXTRAITS DOCUMENTS ALBERT RAG[\s\S]*?--- FIN DES EXTRAITS DOCUMENTS ---\s*(DOCUMENTS RAG DISPONIBLES :[^\n]*(\n- 📄 [^\n]*)*)?",
            "",
            text,
            flags=re.MULTILINE,
        )
        return text.strip()

    def _build_tool_rules(self, clean_lower: str, available_tools: list) -> List[str]:
        gitlab_fn = self._find_matching_tool_name(available_tools, [r"gitlab", r"code", r"repo", r"git"])
        wiki_fn = self._find_matching_tool_name(available_tools, [r"wiki", r"search", r"confluence", r"doc"])
        gda_get_fn = self._find_matching_tool_name(available_tools, [r"get_ticket", r"read_ticket", r"view_issue"])
        gda_search_fn = self._find_matching_tool_name(available_tools, [r"search_issues", r"mantis_search", r"gda_search"])

        rules = []
        id_match = re.search(
            r"(?:gda|ticket|mantis|fiche|anomalie|issue)\D*(\d{4,7})\b|#(\d{4,7})\b",
            clean_lower,
        )
        if id_match:
            ticket_id = int(id_match.group(1) or id_match.group(2))
            target_fn = gda_get_fn or "get_ticket"
            rules.append(
                f"- Pour récupérer les détails du ticket #{ticket_id}, appelle immédiatement `{target_fn}(issue_id={ticket_id})`.\n"
                f"- Dès que les informations sont obtenues, rédige directement la synthèse sans faire d'autres appels."
            )
        elif re.search(r"\b(gda|mantis|tickets?)\b", clean_lower):
            rules.append(f"- Pour rechercher des tickets correspondants, appelle la fonction `{gda_search_fn or 'search_issues'}`.")

        if re.search(r"\b(gitlab|code|repo|dépôt|branche|commit|tag)\b", clean_lower):
            rules.append(f"- Pour inspecter le dépôt, les branches ou les tags, appelle la fonction `{gitlab_fn or 'l outil GitLab'}`.")

        if re.search(r"\b(wiki|documentation|confluence|page\s+wiki)\b", clean_lower):
            rules.append(f"- Pour consulter la documentation Wiki, appelle la fonction `{wiki_fn or 'l outil Wiki'}`.")
        return rules

    async def inlet(
        self,
        body: dict,
        __user__: dict = None,
        __event_emitter__=None,
        __metadata__: dict = None,
    ) -> dict:
        """Point d'entrée protégé : une erreur du filtre ne doit jamais faire planter le chat."""
        try:
            return await self._inlet_impl(body, __event_emitter__, __metadata__)
        except Exception as e:
            self._log(f"ERREUR INLET (le message part sans RAG) : {e}\n{traceback.format_exc()}")
            await self._emit_status(
                __event_emitter__,
                f"⚠️ Filtre Albert : erreur interne ({type(e).__name__}), réponse sans extraits RAG.",
                True,
            )
            return body

    async def _inlet_impl(self, body: dict, emitter, metadata: Optional[dict]) -> dict:
        self._log("=== [INLET] Début de traitement ===")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return body

        # Ignorer les tâches de fond d'Open WebUI (titre, tags, suggestions, autocomplétion…)
        for src in (metadata, body.get("metadata")):
            if isinstance(src, dict) and src.get("task"):
                self._log(f"Tâche de fond Open WebUI ignorée : {src.get('task')}")
                return body

        # Copie superficielle : body n'est modifié qu'à la toute fin
        messages = [dict(m) if isinstance(m, dict) else m for m in messages]

        last_user_msg = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user_msg = self._extract_text(m.get("content"))
                if last_user_msg:
                    break
        if not last_user_msg:
            return body

        chat_id = self._get_chat_id(body, metadata)
        clean_lower = last_user_msg.lower()
        is_tool_turn = self._is_current_turn_tool_resolution(messages)
        self._log(f"chat_id={chat_id} | tool_turn={is_tool_turn}")

        if not self.valves.ALBERT_API_KEY.strip():
            self._log("ATTENTION : ALBERT_API_KEY non renseignée dans les Valves.")

        instructions = [
            "Tu es un assistant technique expert. Tu réponds TOUJOURS en français.",
            "RÈGLE D'INDÉPENDANCE DES SOURCES :",
            "- Les données issues des outils (Mantis, GitLab, Wiki) et les extraits documentaires RAG sont deux canaux complètement indépendants.",
            "- Ne conclus jamais qu'un ticket ou une information est inexistante sous prétexte qu'elle ne figure pas dans les extraits RAG.",
            "- Si un retour d'outil te fournit les détails d'un ticket, exploite-les directement pour répondre.",
            "- Lorsque tu utilises les extraits RAG, cite explicitement le nom du document source.",
        ]

        if not is_tool_turn:
            tool_rules = self._build_tool_rules(clean_lower, body.get("tools") or [])
            if tool_rules:
                instructions.append("CONSIGNES OUTILS :\n" + "\n".join(tool_rules))

        if not is_tool_turn and self.valves.ALBERT_API_KEY.strip():
            async with self._make_client() as client:
                target = await self._resolve_target(client, messages, chat_id)
                if target and target["collection_ids"]:
                    projets_str = ", ".join(target["labels"]) or "Albert"
                    docs_str = ", ".join(target["doc_names"])
                    scope = f"{projets_str} › {docs_str}" if docs_str else projets_str
                    self._log(f"Cible [{target['source']}] : {scope}")

                    if docs_str:
                        instructions.append(
                            "CONTEXTE DE TRAVAIL ACTIF :\n"
                            f"- Projet(s) : {projets_str}\n"
                            f"- Document(s) étudié(s) : {docs_str}\n"
                            "- Sauf indication contraire de l'utilisateur, ses questions portent sur ce(s) document(s)."
                        )

                    await self._emit_status(emitter, f"🔎 Recherche Albert ({scope})…", False)
                    chunks, citations = await self._fetch_albert_context(client, target)

                    if chunks:
                        unique_docs = [c["source"]["name"] for c in citations]
                        instructions.append(
                            f"--- EXTRAITS DOCUMENTS ALBERT RAG ({projets_str}) ---\n"
                            + "\n\n---\n\n".join(chunks)
                            + "\n--- FIN DES EXTRAITS DOCUMENTS ---\n\n"
                            + "DOCUMENTS RAG DISPONIBLES :\n"
                            + "\n".join(f"- 📄 {d}" for d in unique_docs)
                        )
                        if emitter:
                            for citation in citations:
                                try:
                                    await emitter({"type": "citation", "data": citation})
                                except Exception as e:
                                    self._log(f"Erreur émission citation : {e}")
                        hint = " · 📌 mémorisé (#reset pour changer)" if target["source"] in ("sticky", "history") or docs_str else ""
                        await self._emit_status(
                            emitter,
                            f"{len(chunks)} extraits injectés ({len(unique_docs)} doc) depuis {scope}{hint}",
                            True,
                        )
                    else:
                        await self._emit_status(emitter, f"Aucun extrait pertinent trouvé ({scope}).", True)

        injected_block = (
            f"{self.FILTER_BLOCK_START}\n" + "\n\n".join(instructions) + f"\n{self.FILTER_BLOCK_END}"
        )

        system_idx = next(
            (i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "system"),
            None,
        )
        if system_idx is not None:
            existing = self._purge_previous_injection(
                self._extract_text(messages[system_idx].get("content"))
            )
            messages[system_idx]["content"] = (
                f"{injected_block}\n\n{existing}" if existing else injected_block
            )
        else:
            messages.insert(0, {"role": "system", "content": injected_block})

        body["messages"] = self._trim_history(messages)
        self._log("=== [INLET] Fin d'injection ===")
        return body

    async def outlet(
        self, body: dict, __user__: dict = None, __event_emitter__=None
    ) -> dict:
        return body