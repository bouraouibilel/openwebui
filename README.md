# Open WebUI — Albert RAG & Outils Équipe (GDA, Wiki, GitLab)

Ce dépôt regroupe la configuration Docker, les fonctions (Pipe & Filtre) et les outils asynchrones Open WebUI permettant à l'équipe d'interroger (avec **identifiants personnels par utilisateur** via `UserValves` et **confirmation interactive** avant toute écriture) :

- **Albert API** (modèles LLM avec relais SSE complet / *function calling* natif + RAG multi-collections avec routage intelligent et mémoire du document actif)
- **GDA / MantisBT** (consultation, recherche multi-critères paginée sans accents et ajout de commentaires avec confirmation)
- **Wiki / DokuWiki** (recherche et lecture via `?do=export_raw` ou API JSON-RPC, pagination `offset`)
- **GitLab** (récupération de code multi-modules, recherche plein texte `search_code` et requêtes API v4 avec confirmation sur les écritures)

## Documentation

👉 **[Guide de prise en main pour l'équipe (`docs/GUIDE_EQUIPE.md`)](docs/GUIDE_EQUIPE.md)**

Ce guide prêt à partager couvre :
- la prise en main rapide côté utilisateur (clé API personnelle, activation des outils) ;
- l'utilisation du RAG Albert (`#reset`, `#all`, mémoire du document actif, exemples de prompts) ;
- l'utilisation détaillée des outils **GDA**, **Wiki** et **GitLab** (y compris les requêtes combinées) ;
- le guide d'installation et la référence complète des **Valves** pour les administrateurs ;
- le dépannage des erreurs courantes.

## Structure du dépôt

```text
.
├── docker-compose.yml                      # Déploiement Open WebUI
├── docs/
│   └── GUIDE_EQUIPE.md                     # Documentation de prise en main à partager à l'équipe
├── pipes/
│   └── pipe_albert.py                      # Pipe (Manifold) des modèles text-generation Albert API (v2.0.0)
├── filtres/
│   └── albert_rag_universal_filter.py      # Filtre RAG universel Albert (v2.9.1)
└── tools/
    ├── gda_tool.py                         # Outil MantisBT / GDA (v3.0.1)
    ├── wiki_search_tool.py                 # Outil DokuWiki (v3.0.0)
    └── gitlab_code_fetcher.py              # Outil GitLab Code & API v4 (v3.0.0)
```

## Notes clés (v2.9 / v3.0)

- **Clés & jetons par utilisateur (`UserValves`)** : la clé Albert saisie une seule fois dans la Pipe (`API_KEY`) est automatiquement réutilisée par le filtre RAG. Chaque outil (GDA, GitLab, Wiki) utilise le jeton personnel de l'utilisateur (`ALLOW_ADMIN_*_FALLBACK=False` par défaut).
- **Certificats internes (SSL)** : `VERIFY_SSL=False` par défaut sur les outils internes (GDA, GitLab, Wiki), et `VERIFY_SSL=True` sur Albert API (modifiable dans les Valves). Si vous montez une CA interne (`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`), vous pouvez repasser `VERIFY_SSL=True`.
