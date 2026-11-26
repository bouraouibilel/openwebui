# Open WebUI — Albert RAG & Outils Équipe (GDA, Wiki, GitLab)

Ce dépôt regroupe la configuration Docker, les fonctions (Pipe & Filtre) et les outils Open WebUI permettant à l'équipe d'interroger :

- **Albert API** (modèles LLM + RAG multi-collections avec routage intelligent et mémoire de document)
- **GDA / MantisBT** (consultation, recherche multi-critères et ajout de commentaires sur tickets)
- **Wiki / DokuWiki** (recherche et lecture de pages internes)
- **GitLab** (récupération de code multi-modules et requêtes API v4)

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
├── filtres/
│   ├── pipe_albert.py                      # Pipe (Manifold) des modèles text-generation Albert API
│   └── albert_rag_universal_filter.py      # Filtre RAG universel Albert (v2.8.0)
└── tools/
    ├── gda_tool.py                         # Outil MantisBT / GDA (v2.1)
    ├── wiki_search_tool.py                 # Outil DokuWiki (v2.4.0)
    └── gitlab_code_fetcher.py              # Outil GitLab Code & API v4 (v2.1)
```
