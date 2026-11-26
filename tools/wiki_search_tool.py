"""
title: DokuWiki Search & Reader Tool (export_raw + JSON-RPC)
author: openwebui-user
version: 3.0.0
description: Outil asynchrone DokuWiki avec identifiants par utilisateur, lecture propre via ?do=export_raw (tableaux préservés), pagination par offset et support optionnel de l'API JSON-RPC.
"""

import re
import urllib.parse
from typing import Any, Dict, Optional, Tuple

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field


class Tools:

    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="https://ton-dokuwiki.domaine.com/doku.php",
            description="URL complète vers doku.php",
        )
        ACCESS_MODE: str = Field(
            default="auto",
            description="Mode d'accès : 'auto' (JSON-RPC si identifiants/jeton configurés, sinon HTML/export_raw), 'html' ou 'jsonrpc'",
        )
        JSONRPC_URL: str = Field(
            default="",
            description="URL optionnelle vers lib/exe/jsonrpc.php (dérivée automatiquement de BASE_URL si vide)",
        )
        COOKIE: str = Field(
            default="",
            description="Cookie admin (utilisé uniquement si ALLOW_ADMIN_AUTH_FALLBACK=True)",
        )
        ALLOW_ADMIN_AUTH_FALLBACK: bool = Field(
            default=False,
            description="Autoriser le repli sur le cookie admin si l'utilisateur n'a pas saisi ses identifiants personnels (False recommandé)",
        )
        VERIFY_SSL: bool = Field(
            default=True,
            description="Vérifier le certificat SSL (monter la CA interne dans le conteneur plutôt que mettre False)",
        )
        MAX_OUTPUT_CHARS: int = Field(
            default=6000,
            description="Nombre maximal de caractères retournés par page (pagination possible via `offset`)",
        )

    class UserValves(BaseModel):
        WIKI_API_TOKEN: str = Field(
            default="",
            description="Jeton Bearer DokuWiki (API JSON-RPC, si activé sur votre instance)",
        )
        WIKI_USERNAME: str = Field(
            default="",
            description="Identifiant personnel DokuWiki (pour JSON-RPC / Basic Auth)",
        )
        WIKI_PASSWORD: str = Field(
            default="",
            description="Mot de passe personnel DokuWiki (pour JSON-RPC / Basic Auth)",
        )
        COOKIE: str = Field(
            default="",
            description="Cookie de session personnel (ex: DokuWiki=...; dw_token=...) si accès HTML authentifié",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _resolve_auth(self, __user__: Optional[dict]) -> Dict[str, str]:
        auth = {"token": "", "user": "", "password": "", "cookie": ""}
        if isinstance(__user__, dict):
            uv = __user__.get("valves")
            if uv is not None:
                getter = (
                    (lambda k: uv.get(k, ""))
                    if isinstance(uv, dict)
                    else (lambda k: getattr(uv, k, ""))
                )
                auth["token"] = str(getter("WIKI_API_TOKEN") or "").strip()
                auth["user"] = str(getter("WIKI_USERNAME") or "").strip()
                auth["password"] = str(getter("WIKI_PASSWORD") or "").strip()
                auth["cookie"] = str(getter("COOKIE") or "").strip()

        if (
            not any(auth.values())
            and self.valves.ALLOW_ADMIN_AUTH_FALLBACK
            and self.valves.COOKIE.strip()
        ):
            auth["cookie"] = self.valves.COOKIE.strip()
        return auth

    def _jsonrpc_url(self) -> str:
        if self.valves.JSONRPC_URL.strip():
            return self.valves.JSONRPC_URL.strip()
        base = self.valves.BASE_URL.strip()
        if base.endswith("doku.php"):
            return base[: -len("doku.php")] + "lib/exe/jsonrpc.php"
        return base.rstrip("/") + "/lib/exe/jsonrpc.php"

    def _use_jsonrpc(self, auth: Dict[str, str]) -> bool:
        mode = (self.valves.ACCESS_MODE or "auto").strip().lower()
        if mode == "jsonrpc":
            return True
        if mode == "html":
            return False
        return bool(auth["token"] or (auth["user"] and auth["password"]))

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            verify=self.valves.VERIFY_SSL,
            follow_redirects=True,
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

    async def _jsonrpc_call(
        self,
        client: httpx.AsyncClient,
        auth: Dict[str, str],
        method: str,
        params: Optional[dict] = None,
    ) -> Tuple[bool, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        basic_auth = None
        if auth["token"]:
            headers["Authorization"] = f"Bearer {auth['token']}"
        elif auth["user"] and auth["password"]:
            basic_auth = httpx.BasicAuth(auth["user"], auth["password"])
        elif auth["cookie"]:
            headers["Cookie"] = auth["cookie"]

        url = f"{self._jsonrpc_url().rstrip('/')}/{method}"
        res = await client.post(
            url, headers=headers, json=params or {}, auth=basic_auth
        )
        if res.status_code != 200:
            return False, f"HTTP {res.status_code}"
        data = res.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("code", 0) != 0:
            return False, err.get("message") or str(err)
        return True, data.get("result") if isinstance(data, dict) else data

    async def _fetch_html(
        self, client: httpx.AsyncClient, auth: Dict[str, str], url: str
    ) -> Tuple[int, str]:
        headers = {"User-Agent": "OpenWebUI-DokuWikiTool/3.0"}
        basic_auth = None
        if auth["cookie"]:
            headers["Cookie"] = auth["cookie"]
        if auth["user"] and auth["password"]:
            basic_auth = httpx.BasicAuth(auth["user"], auth["password"])
        res = await client.get(url, headers=headers, auth=basic_auth)
        return res.status_code, res.text

    def _paginate_page(self, page_id: str, text: str, offset: int) -> str:
        total = len(text)
        start = max(0, int(offset))
        limit = max(1000, self.valves.MAX_OUTPUT_CHARS)
        chunk = text[start : start + limit]
        end = start + len(chunk)

        header = f"=== Contenu de la page '{page_id}' (caractères {start}-{end} / {total}) ===\n\n"
        if end < total:
            footer = (
                f"\n\n… [page tronquée à {end}/{total} caractères — "
                f"appelle `read_page(page_id='{page_id}', offset={end})` pour lire la suite]"
            )
            return header + chunk + footer
        return header + chunk

    async def search_wiki(
        self,
        query: str,
        __user__: dict = None,
        __event_emitter__=None,
    ) -> str:
        """Recherche des pages sur DokuWiki correspondant aux termes donnés.

        :param query: Terme exact, sigle ou mot-clé (ex: 'RQL', 'processus').
        :return: Liste des pages trouvées avec leurs identifiants complets.
        """
        query_clean = (query or "").strip()
        if not query_clean:
            return "❌ Requête vide."

        auth = self._resolve_auth(__user__)
        await self._emit_status(
            __event_emitter__, f"🔎 Recherche DokuWiki : '{query_clean}'…", False
        )

        try:
            async with self._make_client() as client:
                # 1. Mode JSON-RPC si configuré
                if self._use_jsonrpc(auth):
                    ok, result = await self._jsonrpc_call(
                        client, auth, "core.searchPages", {"query": query_clean}
                    )
                    if ok and isinstance(result, list):
                        await self._emit_status(
                            __event_emitter__,
                            f"Wiki : {len(result)} page(s) trouvée(s).",
                            True,
                        )
                        if not result:
                            return f"Aucun résultat trouvé pour '{query_clean}'."
                        lines = ["Résultats trouvés (JSON-RPC) :"]
                        for item in result[:10]:
                            pid = item.get("id", "")
                            title = item.get("title") or pid
                            snippet = re.sub(
                                r"<[^>]+>", "", str(item.get("snippet") or "")
                            ).strip()
                            entry = f"- **{title}** (ID: `{pid}`)"
                            if snippet:
                                entry += f"\n  Aperçu : {snippet[:180]}…"
                            lines.append(entry)
                        return "\n".join(lines)

                # 2. Mode HTML
                params = urllib.parse.urlencode({"do": "search", "q": query_clean})
                url = f"{self.valves.BASE_URL}?{params}"
                status_code, html = await self._fetch_html(client, auth, url)
                if status_code >= 400:
                    await self._emit_status(__event_emitter__, "Erreur Wiki", True)
                    return f"⚠️ Erreur HTTP {status_code} lors de la recherche Wiki."

                soup = BeautifulSoup(html, "html.parser")
                if soup.find("form", id="dw__login"):
                    await self._emit_status(
                        __event_emitter__, "Wiki : authentification requise.", True
                    )
                    return (
                        "❌ Authentification Wiki requise : renseignez votre `COOKIE` ou vos "
                        "identifiants personnels dans les réglages de l'outil Wiki."
                    )

                found_pages: Dict[str, Dict[str, str]] = {}

                # Conteneurs de résultats de recherche DokuWiki
                items = soup.select(
                    "div.search_result, dd.search_snippet, dt.search_hit, div.search_hit, li.search_item, div.search_quickresult li"
                )
                for item in items:
                    link = item.find("a", href=True)
                    if not link:
                        continue
                    href = link["href"]
                    title = link.get_text(strip=True)
                    match = re.search(r"[?&]id=([^&]+)", href)
                    if match:
                        page_id = urllib.parse.unquote(match.group(1))
                    elif "doku.php/" in href:
                        page_id = href.split("doku.php/")[-1].split("?")[0].strip("/")
                    else:
                        continue
                    if not page_id:
                        continue
                    snippet = item.get_text(separator=" ", strip=True)
                    if page_id not in found_pages:
                        found_pages[page_id] = {
                            "title": title or page_id,
                            "snippet": snippet[:180],
                        }

                # Repli strict sur la zone de résultats uniquement (hors menus/barres latérales)
                if not found_pages:
                    main_zone = soup.select_one(
                        "div.search_results, div.page, main#dokuwiki__content, div#dokuwiki__content"
                    )
                    if main_zone:
                        for nav in main_zone.select(
                            "nav, aside, div.breadcrumbs, div.pageId, div.docInfo, div#dokuwiki__pagetools"
                        ):
                            nav.decompose()
                        q_low = query_clean.lower()
                        for link in main_zone.find_all("a", href=True):
                            href = link["href"]
                            text = link.get_text(strip=True)
                            if (
                                "doku.php" in href
                                and "do=" not in href
                                and text
                                and not href.startswith("#")
                            ):
                                match = re.search(r"[?&]id=([^&]+)", href)
                                pid = (
                                    urllib.parse.unquote(match.group(1))
                                    if match
                                    else href.split("doku.php/")[-1].split("?")[0].strip("/")
                                )
                                if pid and (q_low in pid.lower() or q_low in text.lower()):
                                    found_pages.setdefault(
                                        pid, {"title": text, "snippet": ""}
                                    )

                if not found_pages:
                    test_url = f"{self.valves.BASE_URL}?{urllib.parse.urlencode({'id': query_clean.lower(), 'do': 'export_raw'})}"
                    code, raw_txt = await self._fetch_html(client, auth, test_url)
                    if code == 200 and raw_txt.strip() and "<form" not in raw_txt[:400]:
                        await self._emit_status(
                            __event_emitter__, "Wiki : page directe trouvée.", True
                        )
                        return (
                            f"Aucun résultat via la recherche, mais la page directe `{query_clean.lower()}` existe. "
                            f"Tu peux la lire avec `read_page(page_id='{query_clean.lower()}')`."
                        )

                    await self._emit_status(__event_emitter__, "Wiki : 0 résultat.", True)
                    return f"Aucun résultat trouvé pour '{query_clean}'. Essaie avec un mot racine ou sans accent."

                await self._emit_status(
                    __event_emitter__,
                    f"Wiki : {len(found_pages)} page(s) trouvée(s).",
                    True,
                )
                output = ["Résultats trouvés :"]
                for pid, data in list(found_pages.items())[:10]:
                    desc = f"- **{data['title']}** (ID: `{pid}`)"
                    if data["snippet"] and data["snippet"] != data["title"]:
                        desc += f"\n  Aperçu : {data['snippet']}…"
                    output.append(desc)
                return "\n".join(output)

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur recherche Wiki", True)
            return f"❌ Erreur lors de la recherche Wiki : {e}"

    async def read_page(
        self,
        page_id: str,
        offset: int = 0,
        __user__: dict = None,
        __event_emitter__=None,
    ) -> str:
        """Lit le contenu d'une page DokuWiki (source brute propre, avec pagination via `offset`).

        :param page_id: Identifiant exact de la page (ex: 'rql', 'tech:rql', 'processus:validation').
        :param offset: Position de départ en caractères pour lire la suite d'une longue page (défaut: 0).
        :return: Texte extrait de la page.
        """
        clean_id = (page_id or "").split("?")[0].strip().strip("/")
        if not clean_id:
            return "❌ Identifiant de page vide."

        auth = self._resolve_auth(__user__)
        await self._emit_status(
            __event_emitter__, f"🔎 Lecture page Wiki `{clean_id}`…", False
        )

        try:
            async with self._make_client() as client:
                # 1. Mode JSON-RPC si configuré
                if self._use_jsonrpc(auth):
                    ok, result = await self._jsonrpc_call(
                        client, auth, "core.getPage", {"page": clean_id}
                    )
                    if ok and isinstance(result, str) and result.strip():
                        await self._emit_status(
                            __event_emitter__, f"Page `{clean_id}` lue (JSON-RPC).", True
                        )
                        return self._paginate_page(clean_id, result.strip(), offset)

                # 2. Export brut DokuWiki (?do=export_raw) : compact, tableaux & titres préservés
                raw_url = f"{self.valves.BASE_URL}?{urllib.parse.urlencode({'id': clean_id, 'do': 'export_raw'})}"
                code, raw_text = await self._fetch_html(client, auth, raw_url)
                if (
                    code == 200
                    and raw_text.strip()
                    and "<!DOCTYPE" not in raw_text[:200].upper()
                    and "<html" not in raw_text[:200].lower()
                ):
                    await self._emit_status(
                        __event_emitter__, f"Page `{clean_id}` lue (export_raw).", True
                    )
                    return self._paginate_page(clean_id, raw_text.strip(), offset)

                # 3. Repli HTML nettoyé si export_raw est désactivé
                html_url = f"{self.valves.BASE_URL}?{urllib.parse.urlencode({'id': clean_id})}"
                code, html = await self._fetch_html(client, auth, html_url)
                if code >= 400:
                    await self._emit_status(__event_emitter__, "Erreur lecture Wiki", True)
                    return f"⚠️ Erreur HTTP {code} lors de la lecture de '{clean_id}'."

                soup = BeautifulSoup(html, "html.parser")
                if soup.find("form", id="dw__login"):
                    await self._emit_status(
                        __event_emitter__, "Wiki : authentification requise.", True
                    )
                    return (
                        f"❌ Erreur d'accès à '{clean_id}' : connexion requise "
                        "(configurez votre cookie ou vos identifiants personnels)."
                    )

                content_div = (
                    soup.find("div", class_="page")
                    or soup.find("main", id="dokuwiki__content")
                    or soup.find("div", id="dokuwiki__content")
                )
                if not content_div:
                    return f"Impossible de localiser le contenu de la page '{clean_id}'."

                for tag in content_div.select(
                    "div.pageId, div.secedit, div.breadcrumbs, form, div.docInfo, nav, aside"
                ):
                    tag.decompose()

                text_content = content_div.get_text(separator="\n", strip=True)
                if not text_content or "Cette page n'existe pas encore" in text_content:
                    await self._emit_status(
                        __event_emitter__, f"Page `{clean_id}` introuvable.", True
                    )
                    return f"La page '{clean_id}' n'existe pas ou ne contient aucun texte."

                await self._emit_status(
                    __event_emitter__, f"Page `{clean_id}` lue.", True
                )
                return self._paginate_page(clean_id, text_content, offset)

        except Exception as e:
            await self._emit_status(__event_emitter__, "Erreur lecture Wiki", True)
            return f"❌ Erreur lors de la lecture de '{page_id}' : {e}"
