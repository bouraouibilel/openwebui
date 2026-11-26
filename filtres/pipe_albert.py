import json
from pydantic import BaseModel, Field
import requests


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

    class UserValves(BaseModel):
        API_KEY: str = Field(
            default="",
            description="Votre clé API personnelle Albert (obligatoire pour discuter)",
        )

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()

    def pipes(self):
        headers = {"Content-Type": "application/json"}
        if self.valves.DISCOVERY_API_KEY.strip():
            headers["Authorization"] = f"Bearer {self.valves.DISCOVERY_API_KEY.strip()}"

        try:
            r = requests.get(
                f"{self.valves.BASE_URL.rstrip('/')}/models",
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json().get("data", [])

            # Filtrage strict sur "type": "text-generation"
            models = [
                {
                    "id": model["id"],
                    "name": f"{self.valves.NAME_PREFIX}{model['id']}",
                }
                for model in data
                if model.get("type") == "text-generation" and "id" in model
            ]

            if models:
                return models
            return [
                {
                    "id": "aucun-modele",
                    "name": "Aucun modèle text-generation trouvé",
                }
            ]

        except Exception as e:
            return [{"id": "erreur", "name": f"Erreur découverte API : {e}"}]

    def pipe(self, body: dict, __user__: dict):
        user_valves = __user__.get("valves")

        # Bloque l'appel si l'utilisateur n'a pas saisi sa propre clé
        if not user_valves or not user_valves.API_KEY.strip():
            return "Accès refusé : veuillez entrer votre clé API Albert personnelle dans vos Paramètres -> Fonctions (ou via l'icône des contrôles en haut à droite du chat)."

        # Open WebUI préfixe l'ID du modèle avec l'ID de la fonction (ex: "albert.gpt-oss-120b")
        # On extrait uniquement le vrai nom du modèle après le premier point
        raw_model_id = body.get("model", "")
        model_id = (
            raw_model_id.split(".", 1)[-1] if "." in raw_model_id else raw_model_id
        )

        headers = {
            "Authorization": f"Bearer {user_valves.API_KEY.strip()}",
            "Content-Type": "application/json",
        }

        payload = {**body, "model": model_id}

        try:
            r = requests.post(
                f"{self.valves.BASE_URL.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
                stream=body.get("stream", False),
                timeout=60,
            )

            if r.status_code != 200:
                return f"Erreur API Albert ({r.status_code}) : {r.text}"

            if body.get("stream", False):

                def generate():
                    for line in r.iter_lines():
                        if line:
                            decoded = line.decode("utf-8")
                            if decoded.startswith("data: "):
                                data_str = decoded[6:].strip()
                                if data_str == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data_str)
                                    choices = chunk.get("choices", [])
                                    if choices:
                                        content = (
                                            choices[0]
                                            .get("delta", {})
                                            .get("content", "")
                                        )
                                        if content:
                                            yield content
                                except Exception:
                                    pass

                return generate()
            else:
                return r.json()["choices"][0]["message"]["content"]

        except Exception as e:
            return f"Erreur de connexion à l'API Albert : {str(e)}"
