# Auto-Ontology Lab — Kế hoạch thực nghiệm hoàn chỉnh

> Tài liệu này là plan vận hành của lab. Nó bổ sung (không thay thế) `ARCHITECTURE_AND_EVAL_PLAN.md`: file kia mô tả kiến trúc pipeline; file này mô tả **làm gì, theo thứ tự nào, đo bằng gì, và khi nào gọi là xong**.

> **Bản sửa đổi sau khi đọc source upstream** (xem `UPSTREAM_ARCHITECTURE.md`). Các mục dưới đây **ghi đè** phần tương ứng bên dưới khi có xung đột.

---

## 0a. Điều chỉnh bắt buộc từ phân tích source

| Mục cũ | Điều chỉnh | Lý do (ref `UPSTREAM_ARCHITECTURE.md`) |
|---|---|---|
| §5.2 — 3 arm A/B/C | Thêm **arm C' = production gateway** (`POST :3000/api/chat/completions`, `x-api-key`, tuần tự). Headline so sánh là **A vs B vs C'**; C (harness) chỉ để đối chiếu với số NVIDIA. | §0.3 — harness bật `validate_sql_values`, `calculation_only` mà production không có |
| §5.3 — chạy harness | Harness pin `dd78d068`; stack lab pin `c447003f`. Thử sửa pin harness; vỡ thì chạy Suite 2-harness trên commit riêng và ghi cả hai trong `freeze.yaml`. | §6 |
| §6.1 — export để diff | Export YAML **+ sort** `foreign_keys`, `joins`, `semantic_fks` **+ GET** `/api/terms`, `/column-attributes`, `/api/sql-attributes` để lấy cờ certified và synonyms. | §0.7 |
| §7.2 — T1 "chờ 10 phút" | Bỏ. Thay: **T1 = ghi nhận không có cơ chế phát hiện** (scheduler 24h hard-code, không public re-ingest). Chạy **một** lần chờ qua chu kỳ 24h cho m01 để có bằng chứng thực nghiệm. T2 = `POST :3002/ingest`. T3 = `POST /api/semantic-compilation/trigger`. T4 = `POST :3002/semantic/reset?database_name=`. | §0.5, §8 |
| §7.2 — certify 3 term | Vẫn certify (đo retention), **nhưng thêm** 3 chỉnh sửa **nội dung** (description, sample_values, 1 sql attribute manual) vì chỉ nội dung mới ảnh hưởng câu trả lời. Đo cả hai loại còn/mất qua T2–T4. | §0.1, §0.6 |
| §7.1 — kỳ vọng vanilla | Nay có bằng chứng code cho m01–m05 (test của NVIDIA xác nhận rename = drop+add; bảng có Term không compile lại). Ghi vào `expected.json` với ref file:line. Lab đo **tác động lên câu trả lời**, không "phát hiện" bug. | §2.2 |
| §4.1 — spec traps | Ràng buộc generator: bẫy trong **1000 dòng đầu**; enum là text ≤ 25 distinct, giá trị ≤ 30 ký tự; semantic_fk trap phải có giá trị chung và cột đích `is_unique`; cột ngày **không có sample** → `ambiguous_date` chỉ test qua tên + description. | §2.3 hệ quả |
| §9 Suite 6 | Tách 2 phần: (a) coverage từ `/api/question-entity-coverage`; (b) chat trả SQL hay abstain trên 30 câu unanswerable — kỳ vọng **luôn trả SQL** vì `sql_code` NonEmpty. Thêm metric "SQL chạy được nhưng vô nghĩa". | §0.2 |
| §9 Suite 8 | Thu lại thành 1 test: bật PII → kiểm tra sample value của cột email/phone có trong `catalog_column.sample_values` và text `vdb.semantic_layer`. Bỏ tenant isolation (zones không phải ranh giới phân quyền). | §0.8, §0.9 |
| §9 Suite 5 | Postgres chỉ đọc `pg_stat_activity` 24h → chạy workload **ngay trước** `:3002/ingest`. Ghi rõ `_usage_score` luôn 0 nên ranking theo tần suất không hoạt động. | §2.3 |
| §9 Suite 7 | Token/cost không được ghi → đặt proxy log trước `*_ENDPOINT`. Latency từ `GET /api/analytics`, filter `source=lab-*`. Chạy **tuần tự** (1 warm worker). | §0.10 |
| §11 `import_dataset.py` | Chuyển mọi call sang `:3000` + `x-api-key`; bật compile qua `PUT /api/configurations/semantic-compilation`; `CONNECTION_STRINGS` để rỗng. Giữ `:3002/ingest` chỉ trong `eval/suite4_evolution/`. | §1, §7 |
| §3 Phase 0 | Snapshot thêm `frontend.configurations`, `frontend.conversation_analytics`, `semantic_compilation_history`. Tạo 1 viewer + token. Role Postgres read-only cho source. | §7 |

---

## 0. Định vị

Lab trả lời 3 câu hỏi, theo đúng thứ tự ưu tiên:

| # | Câu hỏi | Ai đã trả lời? |
|---|---|---|
| Q1 | Ontology sinh tự động **đúng đến đâu** (entities / relationships / terms) so với ground truth? | Chưa ai — benchmark public không có ground truth ontology |
| Q2 | Khi schema thay đổi, Auto-Ontology **tự phát hiện / tự cập nhật / cần rebuild**, và certified edits có sống không? Sau khi áp **patch của lab**, cải thiện bao nhiêu? | NVIDIA tự thừa nhận chưa (issue #258 mở, #259, #260) |
| Q3 | Ontology cải thiện Text-to-SQL **bao nhiêu và ở loại câu nào**, trên domain giáo dục? | NVIDIA đã trả lời cho BIRD/BEAVER — lab **tái sử dụng harness của họ**, chỉ đổi dataset và thêm breakdown theo category |

Mọi thứ khác (performance, security, governance) là **phụ lục**, không phải finding chính.

### Hai chế độ của lab

- **Audit mode**: Auto-Ontology nguyên bản (vanilla), pin commit. Không can thiệp.
- **Patch mode**: vanilla + `smart_evolve.py` chạy như sidecar. Mọi kết quả Patch mode **phải** có kết quả Audit mode tương ứng đặt cạnh.

Không bao giờ công bố số của Patch mode mà không có baseline.

---

## 1. Nguyên tắc phương pháp (bất biến)

1. **Pin mọi thứ**: commit hash `NVIDIA/auto-ontology`, commit hash `NVIDIA/auto-ontology-eval`, tên + version 4 model role (REASONING / NON_REASONING / EMBED / RERANK), `.env` đã ẩn secret. Ghi vào `config/freeze.yaml`. Mọi report trích dẫn file này.
2. **Baseline trước, patch sau**: Suite 4 chạy vanilla đến hết, lưu kết quả, rồi mới bật patch.
3. **≥ 3 lượt** cho mọi số có LLM tham gia. Báo mean ± std và số câu flip đúng↔sai giữa các lượt.
4. **Gold SQL phải chạy được** trên DB synthetic và trả về kết quả không rỗng (trừ nhóm unanswerable). Không nhận SQL do LLM sinh mà chưa execute.
5. **Dataset public là bắt buộc**: ít nhất một dataset spec-first commit vào repo. Dataset nội bộ (`hahlex-edu`, `language-learning-sandbox`) giữ gitignore.
6. **Không sửa catalog bằng tay** trong lúc đo. Mọi can thiệp đi qua script có log.
7. **Reset được**: trước mỗi suite, restore catalog DB về snapshot `T0` bằng `pg_dump`/`pg_restore`. Không "chạy tiếp" trên trạng thái đã bị suite trước làm bẩn.

---

## 2. Cấu trúc repo mục tiêu

```
auto-ontology-lab/
├── config/
│   ├── freeze.yaml              # commit hashes, model names, container names
│   └── lab.env.example          # URL backend/ingestion, pg creds (không secret thật)
├── spec/
│   └── edu_public.yaml          # GROUND TRUTH: entities, relationships, terms, traps
├── generators/
│   ├── from_spec.py             # spec.yaml → schema.sql + seed.sql + manifest.json
│   ├── source_introspector.py   # (Tầng 1) live DB → profile.json, không copy PII
│   └── synthetic_generator.py   # (Tầng 2) profile.json → DDL + fake data
├── datasets/
│   ├── edu-public/              # COMMIT — sinh từ spec, tái lập được
│   │   ├── schema.sql
│   │   ├── seed.sql
│   │   ├── manifest.json        # ground truth ontology (copy từ spec, dạng phẳng)
│   │   ├── evaluation.json      # format của auto-ontology-eval
│   │   ├── metadata.json        # mô tả cột — dùng cho arm "schema+metadata"
│   │   └── mutations/           # m01..m08 (SQL) + expected.json
│   └── hahlex-edu/              # GITIGNORE — từ DB nội bộ
├── questions/
│   ├── templates/               # template câu hỏi theo category
│   └── build_evaluation.py      # template × tham số → evaluation.json, validate gold SQL
├── eval/
│   ├── suite1_discovery/        # export ontology → diff manifest → P/R/F1
│   ├── suite2_text2sql/         # wrapper gọi auto-ontology-eval, thêm arm thứ 3
│   ├── suite4_evolution/        # mutation runner, T0–T3 protocol
│   ├── suite5_learning/         # seed query log → đo SQL attribute sinh ra
│   ├── suite6_abstention/       # check_answerable trên câu unanswerable/ambiguous
│   └── common/                  # snapshot/restore catalog, poll readiness, export
├── patch/
│   └── smart_evolve.py          # (Tầng 3) sidecar — chỉ bật ở Patch mode
├── scripts/
│   └── import_dataset.py        # đã có — xem §8 để refactor
├── reports/
│   ├── runs/<timestamp>/        # raw output mỗi lần chạy
│   └── RESULTS.md               # bảng tổng hợp, sinh bằng script
└── docs/
    └── LAB_PLAN.md              # file này
```

---

## 3. Phase 0 — Đóng băng môi trường (0.5 ngày)

**Mục tiêu**: mọi số chạy ra đều quy về một trạng thái duy nhất.

- [ ] Ghi `config/freeze.yaml`: commit hash 2 repo NVIDIA, 4 model, container names, ngày đóng băng.
- [ ] Pin `auto-ontology/` bằng git submodule **hoặc** script `scripts/pin_upstream.sh` checkout đúng hash. Không dựa vào `main`.
- [ ] Viết `eval/common/snapshot.py`: `pg_dump` catalog DB (schema `vdb`, `frontend`, và các schema Auto-Ontology dùng) → `snapshots/T0_<dataset>.dump`; hàm `restore()`.
- [ ] Viết `eval/common/readiness.py`: poll `check_readiness` (MCP) hoặc bảng `semantic_compilation_history` cho đến khi compile xong. Dùng chung cho mọi suite.
- [ ] Xác nhận stack hiện tại (đã import + compile `hahlex-edu`) tạo được snapshot và restore lại chạy bình thường.

**Exit**: `restore()` + `readiness()` chạy end-to-end dưới 5 phút, kết quả `ask_question` cho 5 câu smoke test giống hệt trước/sau restore.

---

## 4. Phase 1 — Dataset public spec-first `edu-public` (2 ngày)

### 4.1 Spec là ground truth

`spec/edu_public.yaml` khai báo:

```yaml
entities:
  - name: student
    table: std_mst            # tên bảng "xấu" cố ý
    description: ...
    key: std_id
relationships:
  - from: enrollment.std_id
    to: student.std_id
    kind: fk                  # fk | semantic_fk (không có FK vật lý) | bridge
terms:
  - name: attendance_rate
    sql: "SUM(CASE WHEN att.status='present' THEN 1 ELSE 0 END)::float / COUNT(*)"
    grain: [student, class]
    depends_on: [attendance, enrollment]
traps:
  - type: ambiguous_date
    columns: [enrollment.enrolled_at, enrollment.created_at, class.start_date]
  - type: soft_delete
    column: student.is_deleted
  - type: abbreviation
    mapping: {std_id: student_id, cls_sess: class_session}
  - type: no_physical_fk
    pairs: [[payment.ref_code, invoice.code]]
```

Nguyên tắc chọn bẫy: **mỗi bẫy phải có cơ sở từ schema thật** (Moodle, Canvas, hoặc pattern từ `hahlex-edu`). Ghi nguồn tham chiếu vào field `rationale`. Mục đích là chống lại lời phản bác "domain tự thiết kế để nó fail".

### 4.2 Generator

`generators/from_spec.py`:
- Sinh `schema.sql` (DDL đúng spec, bao gồm cột không có FK vật lý khi spec nói `semantic_fk`).
- Sinh `seed.sql` deterministic (seed cố định), topo-sort theo FK, phân phối thực tế (ví dụ 15–20% buổi học `cancelled`, 5% học viên soft-deleted) để value anchoring có ý nghĩa.
- Sinh `manifest.json`: spec làm phẳng thành 3 list `entities`, `relationships`, `terms` — đây là file Suite 1 đối chiếu.
- Sinh `metadata.json` theo format `auto-ontology-eval` (mô tả bảng/cột + sample values).

### 4.3 Kích cỡ

- 12–18 bảng, 1 bridge table, 1–2 view.
- 20k–100k dòng tổng — đủ để JOIN 4–6 bảng có kết quả không rỗng, đủ nhỏ để ingest + compile < 15 phút.

**Exit**: `python generators/from_spec.py spec/edu_public.yaml` tạo đủ 4 file; `import_dataset.py --dataset edu-public` import + compile thành công; snapshot `T0_edu-public.dump` tạo xong.

---

## 5. Phase 2 — Bộ câu hỏi + Suite 2 Text-to-SQL (2 ngày)

### 5.1 Bộ câu hỏi

`questions/templates/*.yaml` theo category; `build_evaluation.py` sinh `evaluation.json`:

| Category | Số câu | Ví dụ | Mục đích |
|---|---|---|---|
| `simple` | 30 | Có bao nhiêu học viên đang active? | sanity |
| `join_2_3` | 30 | Doanh thu theo lớp trong Q3 | JOIN cơ bản |
| `join_4_6` | 25 | Tỉ lệ chuyên cần theo giáo viên theo tháng | JOIN sâu |
| `business_metric` | 25 | Attendance rate, outstanding tuition, churn | dùng đúng term SQL |
| `ambiguous_date` | 20 | "Học viên đăng ký tháng 10" (enrolled_at vs created_at) | bẫy ngày |
| `soft_delete` | 10 | Số học viên (phải loại is_deleted) | bẫy filter ngầm |
| `no_fk_join` | 10 | JOIN payment↔invoice qua ref_code | semantic FK |
| `unanswerable` | 20 | Hỏi về khái niệm ngoài schema | Suite 6 |
| `ambiguous_intent` | 10 | "Lớp nào tốt nhất?" | Suite 6 |

Tổng ≈ 180 câu; 150 có gold SQL, 30 không. Mỗi câu có `question_id`, `category`, `difficulty`, `evidence` (nếu dùng), `SQL`.

Validation bắt buộc trong `build_evaluation.py`:
- Execute gold SQL trên DB synthetic; fail nếu lỗi hoặc rỗng (trừ unanswerable).
- Với `ambiguous_date`, phải có thêm field `wrong_sql_variants` — SQL dùng cột sai — và khẳng định kết quả **khác** gold. Nếu không khác, bẫy vô dụng, loại câu.

### 5.2 Ba arm

| Arm | Input cho model | Có sẵn trong eval repo? |
|---|---|---|
| A. `schema-only` | DDL thô | Có (control) |
| B. `schema+metadata` | DDL + `metadata.json` descriptions | **Lab thêm** — tách tác dụng của *mô tả* khỏi *ontology* |
| C. `ontology` | Auto-Ontology agent | Có (treatment) |

Arm B là đóng góp phương pháp: nếu B ≈ C thì giá trị nằm ở mô tả cột, không phải ontology. README eval repo tự thừa nhận delta A→C đo cả workflow.

### 5.3 Chạy

- Wrapper `eval/suite2_text2sql/run.py` gọi `auto-ontology-eval` với `--database-name edu-public`, `--eval-workers` hợp lý, `--run-dir reports/runs/<ts>/suite2/`.
- 3 lượt × 3 arm. Giữ `*_aggregate_metrics.json` và `*_failures.jsonl` gốc.
- Tôn trọng health gate của eval repo (>5% lỗi health → không in headline). Nếu bị gate, giảm `--eval-workers`, không bỏ qua gate.

### 5.4 Metrics

- EX (execution match) tổng và **theo category**.
- Δ(A→C), Δ(A→B), Δ(B→C) — mean ± std.
- Flip rate: % câu đổi kết quả giữa các lượt cùng arm.
- Failure taxonomy từ `*_failures.jsonl`: wrong_join / wrong_column / wrong_filter / wrong_aggregation / syntax / timeout / abstained.

**Exit**: `reports/RESULTS.md` có bảng 3 arm × 9 category, 3 lượt. Ít nhất một category có Δ(B→C) có ý nghĩa (hoặc kết luận rõ là không).

---

## 6. Phase 3 — Suite 1 Ontology Discovery (1.5 ngày)

### 6.1 Lấy ontology đã sinh

Hai đường, dùng cả hai và đối chiếu:
1. `POST /api/model/export` → Ossie YAML (giao diện public, ổn định hơn).
2. Query trực tiếp `term`, `column_attribute`, `column__semantic_fk` (chi tiết hơn, nhưng phụ thuộc version — ghi rõ trong report).

### 6.2 Matching rules (phải cố định trước khi nhìn kết quả)

| Loại | Match khi | Ghi chú |
|---|---|---|
| Entity | term sinh ra map tới đúng `table` trong manifest | tên khác được, bảng phải đúng |
| Relationship | cặp (cột A, cột B) trùng, không phân biệt chiều | semantic_fk tính riêng vì khó hơn fk vật lý |
| Term (business) | SQL attribute sinh ra, khi execute trên DB, cho kết quả **bằng** gold trên ≥ 95% grain | so kết quả, không so chuỗi SQL |

Tính P/R/F1 cho từng loại. Báo riêng `fk` vs `semantic_fk`, `clean_name` vs `abbreviated`.

### 6.3 Phân tích lỗi

Liệt kê false positive quan hệ (ontology bịa JOIN) — đây là lỗi nguy hiểm nhất vì sinh SQL "chạy được nhưng sai". Đếm bao nhiêu câu Suite 2 fail do FP này.

**Exit**: bảng P/R/F1 3 loại × 2 dataset (`edu-public`, `hahlex-edu`), kèm danh sách FP relationships.

---

## 7. Phase 4 — Suite 4 Schema Evolution, **Audit mode** (2 ngày)

### 7.1 Catalogue mutation — map thẳng vào acceptance criteria của #258

| ID | Mutation | Tiêu chí #258 | Kỳ vọng vanilla (giả thuyết — phải kiểm chứng) |
|---|---|---|---|
| m01 | `ALTER TABLE enrollment RENAME COLUMN enrolled_at TO enrollment_date` | rename | Drop+add; term mất mapping; certified description mất |
| m02 | `ALTER TABLE student ADD COLUMN scholarship_pct numeric` | add | Bảng đã có Term → không compile lại cột mới |
| m03 | `ALTER TABLE class DROP COLUMN legacy_code` | drop | ColumnAttribute mồ côi còn lại |
| m04 | `ALTER TABLE payment ALTER COLUMN amount TYPE numeric(12,2)` | retype | Có thể không phát hiện |
| m05 | `COMMENT ON COLUMN ... IS '...'` | description | Không reprocess |
| m06 | `ALTER TABLE invoice ADD CONSTRAINT fk_...` (thêm FK vật lý cho cặp vốn là semantic_fk) | key | Quan hệ trùng hoặc không nhận |
| m07 | Sửa định nghĩa term: `UPDATE` dữ liệu để `attendance` có status mới `'excused'` | data/enum | sample_values cũ |
| m08 | `CREATE TABLE` mới + FK vào `student` | new table | Được xử lý (chưa có Term) — control case |

Mỗi mutation có `expected.json`: ontology sau mutation **nên** trông thế nào (diff so với manifest).

### 7.2 Protocol T0→T3 cho mỗi mutation

```
restore(T0)
certify 3 term + 3 description qua API    → ghi lại nội dung
snapshot(T0_certified)
run 20 câu probe → baseline answers
apply mutation
T1: chờ 10 phút, KHÔNG gọi gì            → export, diff, probe   (tự phát hiện?)
T2: POST /ingest (catalog refresh)        → export, diff, probe   (refresh đủ?)
T3: POST /semantic/compile                → export, diff, probe   (compile đủ?)
T4: reset + full rebuild                  → export, diff, probe   (rebuild đúng?)
so với expected.json; kiểm tra 6 certified items còn/mất
```

### 7.3 Metrics

- **Detection**: ở bước nào ontology phản ánh mutation (T1/T2/T3/T4/không bao giờ).
- **Correctness**: diff vs `expected.json` — đúng / thiếu / sai / FP mới sinh.
- **Latency**: thời gian từ apply đến khi `readiness` báo xong ở bước phát hiện.
- **Certified retention**: 6/6 còn? Cái nào mất ở bước nào.
- **Probe regression**: trong 20 câu probe, bao nhiêu câu đúng→sai sau mutation.

**Exit**: ma trận 8 mutation × 5 bước, 2 dataset. Đây là **finding trung tâm** của bài.

---

## 8. Phase 5 — Patch `smart_evolve.py` + Suite 4 **Patch mode** (3 ngày)

### 8.1 Thiết kế patch: sidecar, không fork

- Chạy ngoài process Auto-Ontology, đọc catalog DB + gọi ingestion API. Không sửa code upstream → vẫn tái lập trên commit đã pin.
- Input: fingerprint schema trước/sau (lưu trong `patch/state/`).
- Bốn hành vi như `ARCHITECTURE_AND_EVAL_PLAN.md` §1 Tầng 3: auto-rename migration, surgical column compile, orphan cleanup, incremental profiling.
- Mỗi hành vi là một hàm riêng, bật/tắt bằng flag → đo được **đóng góp từng phần**.
- Output: báo cáo created / updated / retained / removed / unresolved — đúng format #258 yêu cầu.

### 8.2 Rủi ro phải nói rõ trong report

- Ghi thẳng vào bảng `vdb.*` / `term` là phụ thuộc version. Patch chỉ được tuyên bố tương thích với commit đã pin.
- Rename heuristic (ordinal + type + sample similarity) có thể nhận sai. Đo **rename precision** riêng: tạo 10 case rename thật + 5 case drop-then-add-khác-nghĩa, xem patch phân biệt được không.

### 8.3 Chạy

Lặp lại toàn bộ §7.2 với patch bật. Ablation: bật từng hành vi riêng cho m01, m02, m03.

**Exit**: bảng **vanilla vs patch** 8 mutation; bảng ablation; rename precision/recall.

---

## 9. Phase 6 — Các suite phụ (1.5 ngày)

### Suite 6 — Abstention
- 30 câu `unanswerable` + `ambiguous_intent` → gọi `check_answerable` và `ask_question`.
- Metrics: abstention precision (từ chối đúng câu không trả lời được), abstention recall, và **false abstention** trên 150 câu answerable (từ chối câu đáng ra trả lời được — cái này đau hơn).

### Suite 5 — Continuous Learning (thu hẹp)
- Seed 50 query thật vào query history (hoặc `/api/sql-attributes`) → compile lại → đếm SQL attribute mới sinh, và EX trên `business_metric` tăng bao nhiêu.
- Không hứa đo "learning" rộng hơn thế.

### Suite 3 — gộp vào Suite 2
`ambiguous_date`, `business_metric`, `join_4_6` đã là category của Suite 2. Không cần suite riêng.

### Suite 7, 8 — phụ lục
- Latency P50/P95 lấy từ log Suite 2; chi phí token ước lượng. Một bảng, không phân tích.
- PII detection: bật flag, chạy 10 câu chạm cột PII, ghi kết quả. Không mở rộng.

---

## 10. Phase 7 — Report & công bố (1.5 ngày)

### `reports/RESULTS.md` sinh tự động, gồm
1. Bảng freeze (commit, model).
2. Suite 2: 3 arm × category, 2 dataset, mean ± std.
3. Suite 1: P/R/F1 + FP relationships.
4. Suite 4: ma trận vanilla; ma trận patch; ablation.
5. Suite 6: abstention.
6. Phụ lục 5/7/8.
7. Danh sách **mọi** failure case kèm SQL — để người đọc tự kiểm.

### Bài LinkedIn — 3 finding, không hơn
1. Ontology đúng đến đâu (F1) và sai ở đâu (FP join).
2. Schema thay đổi: vanilla hỏng ở m01–m05 như thế nào; patch sửa được gì, chưa sửa được gì.
3. Δ accuracy theo category — và Δ(B→C) để nói thẳng ontology thêm được gì ngoài mô tả cột.

### Repo public cần có
- `spec/`, `datasets/edu-public/`, `questions/`, `eval/`, `patch/`, `config/freeze.yaml`, `reports/RESULTS.md`.
- `hahlex-edu` chỉ xuất hiện dưới dạng số tổng hợp, không schema, không data.

---

## 11. Refactor `scripts/import_dataset.py` (làm trong Phase 0–1)

Script hiện tại chạy được nhưng cần sửa để sống lâu:

| Hiện tại | Nên |
|---|---|
| Hardcode container `postgres`, `auto-ontology` | Đọc từ `config/freeze.yaml` |
| Ghi thẳng `frontend.configurations` để bật compile | Kiểm tra OpenAPI spec xem có endpoint settings; nếu không, giữ nhưng comment rõ "version-specific, pinned to <hash>" |
| `ensure_clean_env` tự sửa `.env` và `docker compose up` | Tách thành `scripts/doctor.py` riêng; import không nên có side effect lên môi trường |
| Poll `vdb.data_objects_layer` 60 giây cố định | Dùng `eval/common/readiness.py` chung |
| Không ghi log run | Ghi `reports/runs/<ts>/import.json`: dataset, db_name, thời gian ingest, thời gian compile |

---

## 12. Rủi ro & cách xử lý

| Rủi ro | Xử lý |
|---|---|
| Upstream đổi schema `vdb.*` giữa chừng | Đã pin commit; không `git pull` trong suốt lab |
| Delta Suite 2 trong khoảng nhiễu | 3 lượt + báo std; nếu std > Δ thì kết luận "không phân biệt được" — đó cũng là finding |
| Rate limit NIM làm health gate đỏ | Giảm workers; chạy đêm; không bỏ gate |
| Bẫy synthetic bị chê "cố tình" | Mỗi bẫy có `rationale` + nguồn tham chiếu schema thật trong spec |
| Patch nhận rename sai | Đo rename precision riêng (§8.2); báo cả FP |
| Scope lan sang Suite 7/8 | Phụ lục một bảng, không phân tích |
| Dataset nội bộ lộ | `datasets/hahlex-edu/` gitignore; report chỉ số tổng hợp; review trước khi push |

---

## 13. Lịch 3 tuần

| Tuần | Phase | Deliverable kiểm chứng được |
|---|---|---|
| 1 | 0, 1, 2 | `freeze.yaml`; `edu-public` import được; Suite 2 có số 3 arm |
| 2 | 3, 4 | Suite 1 F1; ma trận Suite 4 vanilla |
| 3 | 5, 6, 7 | Patch + ma trận patch; abstention; `RESULTS.md`; bài |

Nếu trượt: cắt Suite 5 trước, rồi ablation patch, rồi dataset nội bộ (giữ public). **Không cắt baseline vanilla.**

---

## 14. Việc làm ngay (thứ tự)

1. Tạo `config/freeze.yaml` từ stack đang chạy (`git -C auto-ontology rev-parse HEAD`, model names trong `.env`).
2. Viết `eval/common/snapshot.py` + `readiness.py`, test restore trên `hahlex-edu` đang có.
3. Viết `spec/edu_public.yaml` — bắt đầu từ 8 bảng, 4 bẫy; mở rộng sau khi pipeline chạy thông.
4. Viết `generators/from_spec.py` tối thiểu (schema + seed + manifest), import, compile, snapshot.
5. Viết 30 câu `simple` + `join_2_3` trước, chạy Suite 2 arm A và C **một lượt** để thông pipeline eval. Mở rộng category sau.
