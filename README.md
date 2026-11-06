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

- Xem chi tiết yêu cầu kiến trúc **Live Source DB $\rightarrow$ Synthetic DB $\rightarrow$ NVIDIA Auto-Ontology**, cơ chế **Smart Incremental & Semantic Evolution** và kế hoạch **8 Test Suites** tại [`ARCHITECTURE_AND_EVAL_PLAN.md`](ARCHITECTURE_AND_EVAL_PLAN.md).

---

## 📥 Import dataset into Auto-Ontology

Once your synthetic dataset is generated under `datasets/<project-name>/` (containing `schema.sql`, `seed.sql`, and optionally `validate.sql`), import it directly into the running Auto-Ontology instance with [`scripts/import_dataset.py`](scripts/import_dataset.py):

```powershell
# List all available datasets in datasets/
python scripts/import_dataset.py --list

# Import a dataset, register its connection in Auto-Ontology, and trigger Semantic Compilation
python scripts/import_dataset.py --dataset language-learning-sandbox --db-name language_sandbox --compile

# Recreate the database from scratch if needed
python scripts/import_dataset.py --dataset language-learning-sandbox --db-name language_sandbox --drop --compile
```
