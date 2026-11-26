"""
title: DokuWiki HTTP Scraper Tool
author: openwebui-user
version: 2.4.0
description: Outil optimisé pour chercher et lire le contenu DokuWiki (recherche tolérante et scraping HTML propre).
"""

import re
import ssl
import urllib.parse
import urllib.request
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="https://ton-dokuwiki.domaine.com/doku.php",
            description="URL complète vers doku.php",
        )
        COOKIE: str = Field(
            default="",
            description="Cookie de session (ex: DokuWiki=...; dw_token=...) si le wiki est privé.",
        )
        VERIFY_SSL: bool = Field(
            default=False,
            description="Désactiver la vérification SSL si certificat interne.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _fetch(self, url: str) -> str:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenWebUI/1.0"
        }
        if self.valves.COOKIE:
            headers["Cookie"] = self.valves.COOKIE

        if not self.valves.VERIFY_SSL:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            return response.read().decode("utf-8", errors="ignore")

    def search_wiki(self, query: str) -> str:
        """
        Recherche des pages sur DokuWiki correspondant aux termes donnés.

        :param query: Terme exact, sigle ou mot-clé (ex: 'RQL', 'processus').
        :return: Liste détaillée des pages trouvées avec leurs identifiants complets.
        """
        try:
            query_clean = query.strip()
            params = urllib.parse.urlencode({"do": "search", "q": query_clean})
            url = f"{self.valves.BASE_URL}?{params}"
            html = self._fetch(url)

            soup = BeautifulSoup(html, "html.parser")

            if soup.find("form", id="dw__login"):
                return "Erreur : Authentification requise. Configure le COOKIE dans les réglages de l'outil."

            found_pages = {}

            # 1. Capture sur les conteneurs de recherche standards (thème par défaut et variantes)
            items = soup.select(
                "div.search_result, dd.search_snippet, dt, div.search_hit, li.search_item"
            )
            for item in items:
                link = item.find("a")
                if link and link.get("href"):
                    href = link["href"]
                    title = link.get_text(strip=True)

                    match = re.search(r"[?&]id=([^&]+)", href)
                    if match:
                        page_id = urllib.parse.unquote(match.group(1))
                    elif "doku.php/" in href:
                        page_id = href.split("doku.php/")[-1].split("?")[0].strip("/")
                    else:
                        continue

                    snippet = item.get_text(separator=" ", strip=True)
                    if page_id not in found_pages:
                        found_pages[page_id] = {
                            "title": title,
                            "snippet": snippet[:150],
                        }

            # 2. Si rien n'est trouvé, capture sur tous les liens internes de la zone de contenu
            if not found_pages:
                main_zone = soup.select_one(
                    "div.page, main#dokuwiki__content, div#dokuwiki__content"
                )
                if main_zone:
                    for link in main_zone.find_all("a"):
                        href = link.get("href", "")
                        text = link.get_text(strip=True)
                        if "doku.php" in href and text and not href.startswith("#"):
                            match = re.search(r"[?&]id=([^&]+)", href)
                            pid = (
                                urllib.parse.unquote(match.group(1))
                                if match
                                else href.split("doku.php/")[-1].split("?")[0]
                            )
                            if pid and ":" in pid or query_clean.lower() in pid.lower():
                                found_pages[pid] = {"title": text, "snippet": ""}

            if not found_pages:
                # 3. Essai direct : tester si une page porte exactement ce nom
                test_url = f"{self.valves.BASE_URL}?{urllib.parse.urlencode({'id': query_clean.lower()})}"
                test_html = self._fetch(test_url)
                if (
                    "Cette page n'existe pas encore" not in test_html
                    and "Topic does not exist" not in test_html
                ):
                    return f"Aucun résultat via le moteur de recherche, mais la page directe '{query_clean.lower()}' semble exister. Tu peux la lire avec read_page(page_id='{query_clean.lower()}')."

                return f"Aucun résultat trouvé pour '{query_clean}'. Essaie avec un mot racine ou sans accent."

            output = ["Résultats trouvés :"]
            for pid, data in list(found_pages.items())[:8]:
                desc = f"- **{data['title']}** (ID: `{pid}`)"
                if data["snippet"] and data["snippet"] != data["title"]:
                    desc += f"\n  Aperçu : {data['snippet']}..."
                output.append(desc)

            return "\n".join(output)

        except Exception as e:
            return f"Erreur lors de la recherche : {str(e)}"

    def read_page(self, page_id: str) -> str:
        """
        Lit le contenu textuel complet d'une page DokuWiki.

        :param page_id: Identifiant exact de la page (ex: 'rql', 'tech:rql', 'processus:validation').
        :return: Texte lisible extrait du corps de la page.
        """
        try:
            clean_id = page_id.split("?")[0].strip().strip("/")
            params = urllib.parse.urlencode({"id": clean_id})
            url = f"{self.valves.BASE_URL}?{params}"
            html = self._fetch(url)

            soup = BeautifulSoup(html, "html.parser")

            if soup.find("form", id="dw__login"):
                return f"Erreur d'accès à '{clean_id}' : Connexion requise (cookie absent ou expiré)."

            # Recherche du conteneur éditorial
            content_div = (
                soup.find("div", class_="page")
                or soup.find("main", id="dokuwiki__content")
                or soup.find("div", id="dokuwiki__content")
            )

            if not content_div:
                return f"Impossible de localiser le contenu de la page '{clean_id}'."

            # Supprimer les bandeaux, boutons d'édition et miettes de pain
            for tag in content_div.select(
                "div.pageId, div.secedit, div.breadcrumbs, form, div.docInfo"
            ):
                tag.decompose()

            text_content = content_div.get_text(separator="\n", strip=True)

            if not text_content or "Cette page n'existe pas encore" in text_content:
                return f"La page '{clean_id}' n'existe pas ou ne contient aucun texte."

            # Tronque à 5 000 caractères pour ne pas saturer le contexte
            return f"=== Contenu de la page '{clean_id}' ===\n\n{text_content[:5000]}"

        except Exception as e:
            return f"Erreur lors de la lecture de '{page_id}' : {str(e)}"
