"""
title: Albert API Pipe (Per-User API Key + Native Tool Calling)
author: Open WebUI Community
version: 2.0.0
description: Connecteur asynchrone vers l'API Albert imposant une clé API personnelle par utilisateur, avec cache des modèles et relais SSE complet (compatible function calling natif et raisonnement).
"""

import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple, Union

import httpx
from pydantic import BaseModel, Field


class Pipe:

    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="https://albert.api.etalab.gouv.fr/v1",
            description="URL de base de l'API Albert (sans slash final)",
        )
        DISCOVERY_API_KEY: str = Field(
            default="",
            description="Clé API admin optionnelle utilisée UNIQUEMENT pour lister les modèles (/v1/models) si l'endpoint est protégé",
        )
        NAME_PREFIX: str = Field(
            default="Albert / ",
            description="Préfixe affiché devant chaque modèle dans le menu déroulant",
        )
        MODELS_CACHE_TTL_SECONDS: int = Field(
            default=300,
            description="Durée de mise en cache de la liste des modèles (en secondes)",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=300,
            description="Délai maximal de lecture d'une réponse Albert (en secondes)",
        )
        VERIFY_SSL: bool = Field(
            default=True,
            description="Vérifier le certificat SSL (laisser True pour l'API Albert publique)",
        )

    class UserValves(BaseModel):
        API_KEY: str = Field(
            default="",
            description="Votre clé API personnelle Albert (obligatoire pour discuter et pour le RAG)",
        )

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()
        self._models_cache: Optional[Tuple[float, List[Dict[str, str]]]] = None

    def _make_client(self, read_timeout: Optional[float] = None) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=10.0,
            read=read_timeout or float(self.valves.REQUEST_TIMEOUT_SECONDS),
            write=30.0,
            pool=10.0,
        )
        return httpx.AsyncClient(timeout=timeout, verify=self.valves.VERIFY_SSL)

    async def pipes(self) -> List[Dict[str, str]]:
        now = time.time()
        if self._models_cache:
            ts, cached_models = self._models_cache
            if now - ts < self.valves.MODELS_CACHE_TTL_SECONDS:
                return cached_models

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.valves.DISCOVERY_API_KEY.strip():
            headers["Authorization"] = f"Bearer {self.valves.DISCOVERY_API_KEY.strip()}"

        try:
            async with self._make_client(read_timeout=15.0) as client:
                r = await client.get(
                    f"{self.valves.BASE_URL.rstrip('/')}/models",
                    headers=headers,
                )
                r.raise_for_status()
                data = r.json().get("data", [])

            models = [
                {
                    "id": model["id"],
                    "name": f"{self.valves.NAME_PREFIX}{model['id']}",
                }
                for model in data
                if isinstance(model, dict)
                and model.get("type") == "text-generation"
                and model.get("id")
            ]

            if models:
                self._models_cache = (now, models)
                return models

            if self._models_cache:
                return self._models_cache[1]
            return [
                {
                    "id": "aucun-modele",
                    "name": "Aucun modèle text-generation trouvé",
                }
            ]

        except Exception as e:
            if self._models_cache:
                return self._models_cache[1]
            return [{"id": "erreur", "name": f"Erreur découverte API : {e}"}]

    def _extract_user_api_key(self, __user__: Optional[dict]) -> str:
        if not isinstance(__user__, dict):
            return ""
        user_valves = __user__.get("valves")
        if user_valves is None:
            return ""
        if isinstance(user_valves, dict):
            return str(user_valves.get("API_KEY", "") or "").strip()
        return str(getattr(user_valves, "API_KEY", "") or "").strip()

    async def pipe(
        self, body: dict, __user__: Optional[dict] = None
    ) -> Union[str, dict, AsyncGenerator[str, None]]:
        api_key = self._extract_user_api_key(__user__)
        if not api_key:
            return (
                "Accès refusé : veuillez entrer votre clé API Albert personnelle dans "
                "Paramètres -> Fonctions (ou via l'icône des contrôles en haut à droite du chat)."
            )

        raw_model_id = str(body.get("model", ""))
        model_id = (
            raw_model_id.split(".", 1)[-1] if "." in raw_model_id else raw_model_id
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload: Dict[str, Any] = {**body, "model": model_id}
        url = f"{self.valves.BASE_URL.rstrip('/')}/chat/completions"

        if body.get("stream", False):

            async def stream_generator() -> AsyncGenerator[str, None]:
                try:
                    async with self._make_client() as client:
                        async with client.stream(
                            "POST", url, json=payload, headers=headers
                        ) as r:
                            if r.status_code != 200:
                                err_text = (await r.aread()).decode(
                                    "utf-8", errors="ignore"
                                )
                                yield f"Erreur API Albert ({r.status_code}) : {err_text[:500]}"
                                return

                            async for line in r.aiter_lines():
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    data_part = line[5:].strip()
                                    if data_part == "[DONE]":
                                        break
                                    # Relais SSE brut : préserve delta.content, tool_calls, reasoning_content et usage
                                    yield line
                except httpx.TimeoutException:
                    yield "Erreur : délai d'attente dépassé lors de la réponse de l'API Albert."
                except Exception as e:
                    yield f"Erreur de connexion à l'API Albert : {e}"

            return stream_generator()

        try:
            async with self._make_client() as client:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code != 200:
                    return f"Erreur API Albert ({r.status_code}) : {r.text[:500]}"
                # Retourne la réponse OpenAI complète (préserve tool_calls et usage)
                return r.json()
        except httpx.TimeoutException:
            return "Erreur : délai d'attente dépassé lors de la réponse de l'API Albert."
        except Exception as e:
            return f"Erreur de connexion à l'API Albert : {e}"
