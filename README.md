# Thermal Heatmap — SoaringSpot IGC Analyzer

Cartographie des thermiques détectés à partir de fichiers IGC issus de compétitions SoaringSpot.  
Produit deux cartes Folium interactives avec fond topographique et espaces aériens OpenAIP.

---

## Pipeline

```
SoaringSpot (URLs) → scraping journées → téléchargement IGC → détection thermiques → agrégation → cartes HTML
```

1. **Scraping** — récupère les URLs des journées de résultats pour chaque championnat
2. **Téléchargement** — télécharge les fichiers `.igc` via `opensoar`
3. **Détection** — `PySoarThermalDetector` identifie les phases thermiques (durée ≥ 40 s)
4. **Agrégation** — moyenne du vario par cellule de 0.01° (~1 km)
5. **Rendu** — deux cartes HTML sauvegardées localement

---

## Sorties

| Fichier | Description |
|---|---|
| `thermals_heatmap_map.html` | Heatmap densité/intensité des thermiques |
| `thermals_circles_map.html` | Cercles proportionnels au nombre de thermiques, colorés par vario moyen |
| `dataframe_23204_therlique.csv` | DataFrame brut des thermiques détectés (optionnel) |

---

## Configuration (`heat_map_vf_soaring_spot.py`)

| Paramètre | Valeur par défaut | Description |
|---|---|---|
| `CHAMP_RESULTS_URLS` | liste | URLs des pages `/results` SoaringSpot |
| `SAVE_FOLDER` | `downloaded_igc_files` | Dossier de stockage des IGC |
| `OPENAIP_KEY` | — | Clé API OpenAIP |
| `V_MIN` | `0.5` m/s | Seuil bas du vario (filtrage + couleur) |
| `V_MAX` | `3.0` m/s | Seuil haut du vario (couleur) |

---

## Installation

```bash
pip install pandas requests beautifulsoup4 folium branca geopy aerofiles opensoar
```

Fichier auxiliaire requis : `help_function.py` (fonctions `datetime_difference`, `vitesse`).

---

## Structure des fichiers IGC téléchargés

```
downloaded_igc_files/
└── <Compétition>/
    └── <Classe>/
        └── <DD-MM-YYYY>/
            └── <competition_id>.igc
```

---

## Légende couleurs

Échelle linéaire `blue → yellow → red` entre `V_MIN` et `V_MAX`.

| Couleur | Vario |
|---|---|
| Bleu | ≤ 0.5 m/s |
| Jaune | ~1.75 m/s |
| Rouge | ≥ 3.0 m/s |

---

## Auteur

Aurélien Doriat
