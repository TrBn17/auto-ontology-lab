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

## 🤖 Just give this prompt to AI is in your project

> Analyze the current project's database schema, business logic, and existing data to generate a **realistic, privacy-preserving synthetic dataset** for use in a separate local repository.

### 📋 Requirements

1. **Core Business Scope:** Focus strictly on the **core domain modules** (typically **20–45 high-value business tables**) that represent primary business entities, workflows, and analytical relationships. Exclude technical/infrastructure plumbing tables (`schema_migrations`, audit/event logs, outbox/webhook queues, background jobs, rate limits, auth tokens/sessions) and ensure every table created in `schema.sql` is populated with rows in `seed.sql` (no empty tables).
2. **Synthetic Schema:** Generate a domain-equivalent relational schema with fictional, generic table/column names. Preserve meaningful entities, relationships, PK/FK constraints, and data types without exposing proprietary database structures.
3. **Synthetic Data:** Generate realistic fictional records reflecting domain-specific distributions, correlations, business workflows, and edge cases. Never copy real records, PII, credentials, or confidential business rules.
4. **Data Integrity:** Ensure referential integrity, valid business states, tenant isolation (if applicable), and temporal consistency.
5. **Reproducibility:** Use deterministic generation with a fixed random seed, executable seed scripts, and database restoration instructions.
6. **Validation:** Verify schema compatibility, data consistency, privacy, and successful restoration into an empty database.
7. **Safety:** Read-only access to the source project. Never modify the original application or production database.

### 📦 Output Contract (Strict Layout for Auto-Ontology Importer)

Create a self-contained **PostgreSQL 16+** dataset directly at the root of `datasets/<project-name>/` with this exact flat file structure:

```text
datasets/<project-name>/
├── schema.sql
├── seed.sql
├── validate.sql      (optional)
├── manifest.json
└── README.md
```

- `schema.sql` — Single self-contained SQL file containing all required extensions (`CREATE EXTENSION IF NOT EXISTS ...`), roles (`DO $$ BEGIN IF NOT EXISTS ... CREATE ROLE ... END $$;`), schemas, tables, views, PK/FK/CHECK constraints, and indexes. Do **not** split into `schema/` subfolders and do **not** include `pg_dump` `\restrict` / `\unrestrict` meta-commands.
- `seed.sql` — Pre-generated, ready-to-import SQL file containing all synthetic `INSERT` statements in valid FK dependency order. If you write a `generate.py` script, you **must execute it** so `seed.sql` and `manifest.json` already exist on disk.
- `validate.sql` *(optional)* — SQL assertions to verify row counts, constraints, and privacy guarantees after import.
- `manifest.json` — Database engine (`PostgreSQL 16+`), `"load_order": ["schema.sql", "seed.sql"]`, entities, relationships, and row counts.
- `README.md` — Restoration instructions and validation results.

### 🚀 Delivery

Locate the local `auto-ontology-lab` repository and place the dataset under `datasets/<project-name>/`.

- Do not overwrite existing files.
- Keep the dataset local and excluded from Git tracking by default.
- Do not modify the destination application's source code.
- If the repository cannot be located, request its path.

> [!IMPORTANT]
> **Goal:** Produce a realistic, independently restorable synthetic database that preserves the domain's relational complexity without exposing the original project's confidential implementation details.

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
