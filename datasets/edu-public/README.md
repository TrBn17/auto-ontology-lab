# edu-public

The public benchmark dataset. The schema is original, modelled on common LMS concepts, and all data is invented, so everything here can be published.

| Path | Written by | Purpose |
|---|---|---|
| `source/` | Hand | Schema and deterministic seed for the source database |
| `clone.yaml` | Hand | Allowlist and the implicit foreign key for `lab clone` |
| `questions.json` | Hand | 60 questions with gold SQL, 10 per category |
| `evolve.json`, `mutations/` | Hand | Probes, edits, and the 8 schema mutations with expected results |
| `metadata.json` | Hand | Column descriptions for the `ddl+meta` mode |
| `unanswerable.json` | Hand | 20 questions the schema cannot answer |
| `ddl/`, `data/`, `manifest.json`, `profile.json` | `lab clone` | The synthetic dataset itself |

## Regenerate

```powershell
python -m lab clone --from-env LAB_SOURCE_DSN --to-env LAB_TARGET_DSN --out datasets/edu-public --scale 1.0 --seed 42
python -m lab import --dataset edu-public
python -m lab measure --dataset edu-public --check-gold
```

`LAB_SOURCE_DSN` points at a scratch database loaded with `source/schema.sql` and then `source/seed.sql`, in one session each.

## Writing questions

Gold SQL may rely only on what cloning preserves. That means allowlisted categorical values, foreign keys and date ranges. Do not filter on generated text such as codes or names, and do not assume two foreign keys agree across tables, such as a student's attendance being in a class that student enrolled in.
