<div align="center">
  <img src="https://img3.pixhost.to/images/6977/800059020_image.png" alt="Auto-Ontology Lab Banner" width="100%" />

  <br />

  <h1>🧪 Auto-Ontology Lab</h1>

  <p>
    <img src="https://img.shields.io/badge/PostgreSQL-16%2B-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL 16+" />
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
    <img src="https://img.shields.io/badge/Synthetic_Data-Privacy_Preserving-F59E0B?style=for-the-badge" alt="Synthetic Data" />
    <img src="https://img.shields.io/badge/Auto--Ontology-Semantic_Compiler-10B981?style=for-the-badge" alt="Auto-Ontology" />
  </p>
</div>

---

## 🗺️ Documentation & Implementation Plan

- [`PLAN.md`](PLAN.md) — Master pipeline overview: end-to-end workflow (`lab clone` $\rightarrow$ `lab import` $\rightarrow$ `lab discover` $\rightarrow$ `lab ask` $\rightarrow$ `lab evolve` $\rightarrow$ `lab report`), core principles, and schedule.
- [`docs/UPSTREAM_ARCHITECTURE.md`](docs/UPSTREAM_ARCHITECTURE.md) — Ground-truth architecture of Auto-Ontology analyzed directly from upstream source (`c447003f`) and `auto-ontology-eval` (`ec41b2c5`), along with design implications for the lab.
- [`docs/LAB_PLAN.md`](docs/LAB_PLAN.md) — Operational lab plan: phases, metrics, schema evolution protocol, and methodological adjustments derived from source analysis.
- [`docs/STEP1_CLONE_PLAN.md`](docs/STEP1_CLONE_PLAN.md) — Step 1 implementation plan: `lab clone` (production DB $\rightarrow$ privacy-preserving synthetic DB).
- [`config/freeze.yaml.example`](config/freeze.yaml.example) — Configuration template for pinning upstream commits, model endpoints, and runtime flags.

---

## 📥 Import Dataset into Auto-Ontology

Once your synthetic dataset is generated under `datasets/<project-name>/` (containing `schema.sql`, `seed.sql`, and optionally `validate.sql`), import it directly into the running Auto-Ontology instance with [`scripts/import_dataset.py`](scripts/import_dataset.py):

```powershell
# List all available datasets in datasets/
python scripts/import_dataset.py --list

# Import a dataset, register its connection in Auto-Ontology, and trigger Semantic Compilation
python scripts/import_dataset.py --dataset language-learning-sandbox --db-name language_sandbox --compile

# Recreate the database from scratch if needed
python scripts/import_dataset.py --dataset language-learning-sandbox --db-name language_sandbox --drop --compile
```
