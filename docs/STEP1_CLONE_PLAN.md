# Bước 1 — `lab clone`: từ DB production ra DB synthetic

> Mục tiêu: một lệnh, cắm vào DB Postgres bất kỳ bằng user read-only, ra một DB synthetic cùng schema, cùng phân phối, không chứa dòng thật nào. Postgres-only ở v1.

```powershell
python -m lab clone --from "postgresql://ro_user:***@prod-host:5432/lms" `
                    --to   "postgresql://postgres:***@localhost:5432/lms_synth" `
                    --out  datasets/lms_synth --scale 0.1 --seed 42
```

Kết quả: `datasets/lms_synth/` chứa DDL, data giả, manifest cấu trúc, và report so sánh phân phối. DB `lms_synth` đã được tạo và nạp, sẵn sàng cho `import_dataset.py`.

---

## 1. Ranh giới dữ liệu — thứ duy nhất rời khỏi prod

`lab clone` chỉ chạy **3 loại** câu lệnh trên prod:

| Loại | Câu lệnh | Rời khỏi prod |
|---|---|---|
| DDL | `pg_dump --schema-only` (hoặc fallback đọc `pg_catalog`) | Tên bảng/cột, kiểu, PK/FK/CHECK/UNIQUE, view, enum type |
| Đếm | `SELECT count(*)`, `count(*) FILTER (WHERE c IS NULL)`, `count(DISTINCT c)`, `min(c)`, `max(c)`, `avg(length(c))` | Con số tổng hợp |
| Phân phối | `SELECT c, count(*) FROM t GROUP BY c` — **chỉ** khi `count(DISTINCT c) ≤ 50` **và** cột không khớp regex PII **và** `max(length(c)) ≤ 64` | Danh sách giá trị categorical + tần suất (ví dụ `status: active 70%, dropped 25%`) |

Không có `SELECT *`, không có `LIMIT n`, không có sample dòng. Toàn bộ output ghi vào `profile.json`; người dùng mở ra xem được **trước** khi bước sinh chạy (`--profile-only` dừng tại đây).

Cột PII (regex trên tên: `email|phone|mobile|name|address|street|dob|birth|ssn|tax|passport|iban|card`, cộng `--pii-extra`) chỉ lấy: kiểu, null rate, min/max length. Không lấy min/max giá trị, không GROUP BY.

Bảng kỹ thuật bị loại mặc định: `schema_migrations|alembic_version|flyway_|audit_|_log$|sessions?$|outbox|celery_|django_migrations|pg_`. Thêm bằng `--exclude`.

Bảng lớn (`count > 1_000_000`): các aggregate chạy trên `TABLESAMPLE SYSTEM (1)` để không treo prod. Mọi query set `statement_timeout = 60s`.

---

## 2. Cấu trúc code

```
lab/
├── __main__.py        # python -m lab <cmd>
├── cli.py             # argparse: clone | profile | generate | load | validate
├── introspect.py      # prod → profile.json
├── ddl.py             # pg_dump wrapper + sanitize + fallback từ pg_catalog
├── pii.py             # regex + mapping cột → Faker provider
├── plan.py            # topo-sort FK, tính số dòng mỗi bảng theo --scale
├── generate.py        # profile.json → rows (generator theo cột)
├── writers.py         # ghi data/*.csv, seed.sql, manifest.json
├── load.py            # tạo DB đích, chạy DDL, COPY csv, setval sequences
├── validate.py        # re-profile DB đích, so với profile.json → validate_report.json
└── tests/
    ├── fixtures/mini_lms.sql   # 4 bảng: student, course, enrollment, payment
    └── test_*.py
```

Dependencies: `psycopg[binary]`, `faker`, `numpy`. Không framework. Python 3.12. `pg_dump` lấy từ container Postgres của stack (`docker exec postgres pg_dump`) vì Windows thường không có trên PATH; fallback `ddl.py` tự dựng DDL từ `pg_catalog` nếu không có.

---

## 3. `profile.json` — hợp đồng dữ liệu

```json
{
  "source": {"dialect": "postgresql", "server_version": "16.3", "profiled_at": "2026-11-10T09:00:00+07:00", "scale_note": "TABLESAMPLE 1% on tables > 1M rows"},
  "excluded_tables": ["public.schema_migrations"],
  "enums": {"public.enrollment_status": ["active", "dropped", "paused"]},
  "tables": {
    "public.student": {
      "row_count": 48213,
      "pk": ["std_id"],
      "unique": [["email"]],
      "checks": ["CHECK ((age >= 5))"],
      "columns": {
        "std_id":    {"type": "integer", "nullable": false, "null_rate": 0.0, "distinct": 48213, "min": 1, "max": 51002, "is_identity": true},
        "email":     {"type": "character varying(255)", "nullable": false, "null_rate": 0.0, "pii": "email", "len": {"min": 9, "max": 48, "avg": 23.1}},
        "status":    {"type": "text", "nullable": false, "null_rate": 0.0, "distinct": 3,
                      "categorical": {"active": 0.71, "dropped": 0.24, "paused": 0.05}},
        "enrolled_at": {"type": "date", "nullable": true, "null_rate": 0.02, "min": "2019-01-07", "max": "2026-11-01"},
        "is_deleted": {"type": "boolean", "nullable": false, "categorical": {"false": 0.95, "true": 0.05}},
        "notes":     {"type": "text", "nullable": true, "null_rate": 0.81, "len": {"min": 0, "max": 2000, "avg": 140.0}}
      }
    }
  },
  "foreign_keys": [
    {"from": "public.enrollment.std_id", "to": "public.student.std_id", "null_rate": 0.0, "children_per_parent": {"avg": 3.2, "max": 11}, "coverage": 0.88}
  ],
  "views": ["public.v_active_students"]
}
```

`coverage` = tỉ lệ dòng cha có ít nhất 1 dòng con (để sinh học viên chưa có enrollment cho đúng thực tế).

---

## 4. Quy tắc sinh — `generate.py`

Thứ tự: topo-sort theo FK (`plan.py`); self-reference (`parent_id`) sinh sau khi bảng đã có id. Số dòng = `max(50, round(row_count × scale))`, cap `--max-rows` (mặc định 50 000). Bảng con tính lại theo `children_per_parent` để tỉ lệ giữ nguyên, không theo scale thô.

| Tín hiệu từ profile | Generator |
|---|---|
| `pii` | Faker theo loại: `email→email()`, `phone→phone_number()`, `name→name()`, `address→address()`, `dob→date_of_birth()`, khác → `pystr(len)`. Locale `vi_VN` mặc định, `--locale` đổi được. |
| `categorical` | `numpy.random.choice(values, p=weights)` — giữ đúng tần suất, tự nhiên thỏa ≤ 25 distinct mà Auto-Ontology cần để lấy sample |
| enum type | như categorical; nếu không có phân phối → uniform trên `pg_enum` |
| `boolean` | theo categorical |
| integer/bigint/numeric không PK/FK | uniform `[min, max]`; nếu `distinct` nhỏ (≤ 50) → coi là categorical |
| `is_identity` / serial PK | sinh tuần tự 1..n; `setval` sau khi load |
| uuid PK | `uuid4` seeded |
| FK | bốc từ pool key cha theo `children_per_parent` (Poisson quanh avg, cắt tại max); `null_rate` giữ; `coverage` < 1 → một phần cha không có con |
| date/timestamp | uniform `[min, max]`; nếu có cặp tên `start_*/end_*` hoặc `*_at` và `created_at` cùng bảng → ép `end ≥ start` |
| text tự do (không categorical, không PII) | `faker.text()` cắt theo `len.avg ± std`, giữ `null_rate` |
| `character varying(n)` | cắt ở n |
| jsonb | `{}` ở v1; v2 lấy key qua `jsonb_object_keys` aggregate |
| array | 0–3 phần tử kiểu gốc |
| UNIQUE | set theo dõi; va chạm → sinh lại tối đa 20 lần, rồi thêm suffix |
| CHECK đơn giản (`IN (...)`, `>= x`, `BETWEEN`) | parse regex và tôn trọng; CHECK phức tạp → bỏ qua, bắt ở bước load (xem §6) |

Mọi RNG seed từ `--seed`; Faker `seed_instance`. Cùng profile + seed → byte-identical output.

---

## 5. Output — `writers.py`

```
datasets/lms_synth/
├── profile.json            # từ prod (chỉ aggregate)
├── ddl/
│   ├── 01_types.sql        # CREATE TYPE / extension
│   ├── 02_tables.sql       # bảng + PK + CHECK + UNIQUE (chưa FK)
│   ├── 03_fkeys.sql
│   └── 04_views.sql
├── data/
│   └── public.student.csv  # header, NULL = \N — đúng format auto-ontology-eval
├── seed.sql                # INSERT multi-row 1000/dòng, để import_dataset.py hiện tại nạp được
├── manifest.json           # entities = bảng, relationships = FK; semantic để trống chờ người điền
└── validate_report.json    # sau bước load
```

DDL sanitize: bỏ `OWNER TO`, `GRANT/REVOKE`, `ALTER ... SET TABLESPACE`, `CREATE POLICY` (RLS), `\restrict`, comment license của pg_dump. Giữ `COMMENT ON` (Auto-Ontology không đọc, nhưng người đọc DDL thì có). Đổi tên DB trong `\connect` nếu có.

Hai output `data/` và `seed.sql` cùng nội dung — một cho harness NVIDIA, một cho script hiện có. Khi `import_dataset.py` được refactor để nạp `data/` thì bỏ `seed.sql`.

---

## 6. Load — `load.py`

1. `CREATE DATABASE` (hoặc `--drop` → `DROP ... WITH (FORCE)`).
2. Chạy `01_types.sql`, `02_tables.sql`.
3. `COPY ... FROM STDIN (FORMAT csv, HEADER, NULL '\N')` từng bảng theo topo order, batch.
4. Dòng vi phạm CHECK phức tạp: load theo transaction/bảng; nếu fail → fallback chèn từng batch 1000, ghi dòng lỗi ra `rejected/<table>.csv`, bỏ qua, tiếp. Report số dòng bị bỏ.
5. `03_fkeys.sql`, `04_views.sql`.
6. `setval` mọi sequence = max(id)+1.
7. `ANALYZE`.
8. Tạo role `lab_ro` read-only (`GRANT SELECT`, `default_transaction_read_only=on`, `statement_timeout=30s`) — connection string này đưa cho Auto-Ontology và cho scorer.

---

## 7. Validate — `validate.py`

Chạy lại `introspect` trên DB đích (không cần sampling), so với `profile.json`:

| Chỉ số | Ngưỡng pass |
|---|---|
| FK integrity | 100% (query orphan = 0) |
| UNIQUE/PK vi phạm | 0 |
| Row count mỗi bảng | trong ±5% của mục tiêu sau scale |
| Null rate mỗi cột | ±3 điểm phần trăm |
| Categorical: khoảng cách L1 giữa 2 phân phối | ≤ 0.10 |
| Số distinct cột categorical | bằng nhau |
| Dòng bị reject | ≤ 1% mỗi bảng |

In bảng ra console, ghi `validate_report.json`, exit code ≠ 0 nếu fail. Đây là **tiêu chí hoàn thành** của bước 1.

---

## 8. Thứ tự làm và ước lượng

| # | Việc | Ước lượng | Done khi |
|---|---|---|---|
| 1 | `tests/fixtures/mini_lms.sql` — 4 bảng, có FK, enum, unique, CHECK, nullable FK, cột PII, cột categorical, 2 cột ngày | 1h | Nạp được vào Postgres local |
| 2 | `introspect.py` + `pii.py` → `profile.json` trên mini_lms | 3h | JSON đúng §3; test: không có key `categorical` trên cột `email` |
| 3 | `ddl.py` — pg_dump qua `docker exec` + sanitize; fallback pg_catalog chỉ cho CREATE TABLE/PK/FK | 2h | DDL nạp lại được vào DB rỗng |
| 4 | `plan.py` + `generate.py` — đủ bảng §4 trừ jsonb/array | 5h | Sinh mini_lms scale 1.0 không lỗi FK/unique |
| 5 | `writers.py` + `load.py` | 3h | DB đích có đủ bảng, `setval` đúng |
| 6 | `validate.py` | 2h | Pass trên mini_lms |
| 7 | `cli.py` nối thành `clone`; `--profile-only`, `--dry-run` | 1h | Lệnh một dòng chạy end-to-end |
| 8 | **Chạy trên `hahlex-edu` thật** (read-only), đọc `profile.json` tay, chỉnh regex PII/exclude | 2h | validate pass; bạn xác nhận không có giá trị thật nào trong profile |
| 9 | `import_dataset.py --dataset lms_synth` → Auto-Ontology ingest + compile → export YAML xem nó sinh gì | 1h | Có YAML đầu tiên |

Tổng ≈ 2.5 ngày làm việc. Mục 8 là mốc quan trọng nhất — mọi thứ trước đó chỉ là chuẩn bị cho nó.

---

## 9. Rủi ro riêng của bước này

| Rủi ro | Xử lý |
|---|---|
| Prod chậm vì aggregate | `TABLESAMPLE` cho bảng > 1M, `statement_timeout 60s`, chạy ngoài giờ, `--tables` để chọn bảng |
| Regex PII bỏ sót cột (ví dụ `contact_info`) | `--profile-only` bắt buộc review tay lần đầu; `--pii-extra`; cột text free có `distinct > 50` không bao giờ GROUP BY nên không lọt giá trị |
| Giá trị categorical bản thân là nhạy cảm (ví dụ tên trường con) | ngưỡng `max(length) ≤ 64` + `--no-categorical col` ; in danh sách categorical ra console để duyệt |
| CHECK phức tạp làm reject nhiều dòng | report; v2 parse thêm; không chặn bước 1 |
| pg_dump version mismatch | dùng pg_dump 17 trong container, tương thích ngược |
| FK vòng (A→B→A) | topo-sort phát hiện, sinh A với FK null trước, UPDATE sau khi B có |
| Phân phối cột số lệch (uniform ≠ thật) | chấp nhận ở v1 — Auto-Ontology không profile phân phối số, chỉ min/max/top-5; v2 thêm percentile |

---

## 10. Không làm ở bước 1

- MySQL/SQLite/Snowflake — chỉ Postgres.
- Incremental sync prod → synth. Chạy lại toàn bộ là đủ.
- `manifest.json` phần semantic (quan hệ ngầm, công thức) — để trống, người điền sau.
- Sinh câu hỏi/gold SQL.
- Mutation / schema evolution — là bước `lab evolve`, sau.
