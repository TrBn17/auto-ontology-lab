# Auto-Ontology upstream — kiến trúc thật và hệ quả cho lab

> Phân tích tĩnh source `NVIDIA/auto-ontology` @ `c447003f` (v1.2.0, 2026-11-06) và `NVIDIA/auto-ontology-eval` @ `ec41b2c5` (2026-11-03). Không chạy code. Đường dẫn và số dòng theo đúng commit trên. Mục đích: lab bám hành vi **production thật**, không bám README.

---

## 0. Mười phát hiện làm thay đổi thiết kế lab

| # | Phát hiện | Bằng chứng | Hệ quả |
|---|---|---|---|
| 1 | **Cờ certified không ảnh hưởng gì đến câu trả lời.** Không chỗ nào trong `retrieval/`, `semantic/`, `utils/`, `vdb/` đọc `*_certified`. Chỉ dùng cho badge UI và search. | grep toàn repo; `sql_attributes.py:143` select nhưng không filter/rank | Bỏ giả thuyết "certify giúp accuracy". Test "reviewed ontology" phải sửa **nội dung** (description, sample_values, sql attribute), không bật cờ. Certified retention vẫn đo được nhưng là test *bảo toàn dữ liệu*, không phải *chất lượng*. |
| 2 | **Chat không có bước abstain.** `check_answerable` chỉ là tên MCP của `/api/question-entity-coverage`, một endpoint riêng, chat không gọi. `sql_code` và `response` là `NonEmptyStr` → model không thể trả "không trả lời được". Abstain chỉ xảy ra khi fail 3×3 lần sinh SQL hoặc hết 7 lần reconstruct. | `models.py:239-248`; `sql_unconstructable.py:40-91`; `entity_coverage/graph.py:26-49` | Suite 6 phải tách 2 phần: (a) coverage score của `/api/question-entity-coverage` trên câu unanswerable; (b) hành vi thật của chat — kỳ vọng nó **luôn sinh SQL**, tức hallucinate trên câu ngoài schema. Đây là finding lớn. |
| 3 | **Benchmark chính thức không đi qua production path.** `auto-ontology-eval` import Python trực tiếp (`stream_agent_response`), không REST, không MCP, và bật `validate_sql_values=True`, `calculation_only=True`, `shorten_answer=True` — các knob **HTTP route không expose**. Pin commit `dd78d068`, không phải main. | eval `auto_ontology_sql/app.py:70-76, 349-424`; upstream `text_to_sql_graph.py:225-239` (`validate_sql_values` off khi qua worker) | Số của NVIDIA đo một cấu hình người dùng không có được. Lab cần **arm C' = production gateway** (`POST :3000/api/chat/completions` + `x-api-key`) bên cạnh arm C (harness). Chênh C vs C' là contribution riêng. |
| 4 | **Rename = drop + add, có test của chính NVIDIA xác nhận.** Bảng đã có Term **không bao giờ** được compile lại. Cột thêm vào bảng đã compile không bao giờ có ColumnAttribute. Bảng bị drop để lại Term mồ côi + vector cũ. | `catalog/tests/test_incremental_ingest.py:214-226`; `dal/datasources.py:852-882 fetch_all_tables_without_term`; `column_attribute.table_id` là TEXT không FK (`schema.py:497-501`) | Kết quả Suite 4 vanilla cho m01–m05 gần như chắc chắn fail. Lab không "phát hiện" bug — lab **đo tác động** của bug lên câu trả lời và chứng minh patch sửa được. |
| 5 | **Không có endpoint public để re-ingest.** `PUT /api/connections/{db}` chỉ re-ingest khi schema allowlist đổi. Scheduler 24h hard-code (`INGEST_INTERVAL_HOURS` không được đọc). Re-ingest on demand chỉ qua `:3002/ingest` nội bộ, không auth. | `server/connections/service.py:286`; `data_scheduler.py:36`; `ingestion_service/router.py:74-78` | Protocol T1 "chờ 10 phút" vô nghĩa — không có gì chạy. Thay bằng: T1 = xác nhận không có cơ chế tự phát hiện (đọc code + 1 lần chờ qua chu kỳ 24h để có bằng chứng thực nghiệm). T2 bắt buộc gọi `:3002/ingest`. |
| 6 | **`/semantic/reset` xoá sạch certified và manual work.** `delete_semantic_layer` không phân biệt cờ. Reset public (`/api/semantic-compilation/reset`) reset **mọi** database. | `dal/reset.py:66-184`; `server/semantic_compilation/router.py:45-55` | T4 full rebuild = mất toàn bộ chỉnh sửa. Đây là câu trả lời thẳng cho Vladimir. |
| 7 | **Export không chứa certified, synonyms, owner, tag, zone.** `foreign_keys`, `joins`, `semantic_fks` là `SELECT DISTINCT` không ORDER BY. | `dal/model_interchange.py:195-251, 568`; `schemas.py:48-159` | Diff ontology = export YAML (sort 3 list trên) **+** GET `/api/terms`, `/api/terms/{id}/column-attributes`, `/api/sql-attributes` để lấy cờ certified. |
| 8 | **PII detection không mask gì.** Chỉ gắn tag. Sample values của cột PII vẫn vào `catalog_column.sample_values`, vào prompt LLM, vào cả 2 VDB. | grep `pii` trong `semantic/`, `retrieval/`, `embedding_rows` = rỗng; `pii_detection/service.py:150` | Suite 8 thu lại thành một test rẻ và sắc: bật `pii_detection_enabled`, kiểm tra sample value của cột email/phone có nằm trong `vdb.semantic_layer` text không. |
| 9 | **Zones không còn là ranh giới phân quyền.** Route truyền `zone_ids=None`; retrieval không có code zone. | `frontend/auth/proxy-backend.ts:95-97`; `terms/router.py:81,103` | Bỏ "tenant isolation" khỏi lab. Ghi nhận là fact. |
| 10 | **Không ghi token/cost ở đâu cả.** Latency chỉ có 2 timestamp trong `frontend.conversation_analytics`. RERANK model không dùng khi trả lời. Chỉ 1 warm worker → song song bị cold-start. | `schema.prisma:88-102`; `worker.py:5-25, 366` | Đo cost phải đặt proxy OpenAI-compatible có log trước `REASONING_/NON_REASONING_/EMBED_ENDPOINT`. Chạy eval **tuần tự**. Tag traffic bằng header `x-auto-ontology-source: lab`. |

---

## 1. Topology production

```
client ──x-api-key──▶ Next.js :3000 (auth, permission) ──x-auto-ontology-user-id──▶ FastAPI :3001
                                                                                        │ best-effort HTTP
                                                                                        ▼
                                                                              ingestion-service :3002 (no auth)
                                                                                        │
                                              Postgres 17 + pgvector ◀──────────────────┘
                                              schemas: public (catalog+semantic, Alembic)
                                                       frontend (users, tokens, configurations, Prisma)
                                                       vdb (data_objects_layer, semantic_layer)
```

- **Official install = Helm** (`DEPLOYMENT.md`); Compose là cùng stack trên 1 máy. Helm để `:3001`, `:3002`, `:5432` ở ClusterIP; Compose publish cả ra host.
- **Chỉ `:3000` là public.** `:3001` tin header `x-auto-ontology-user-id` không xác minh (`server/identity.py:127-146`); không header → bỏ qua `enforce_edit` (`owners.py:98-103`). `:3002` không auth, có `/semantic/reset` xoá mọi thứ.
- Auth resolve theo thứ tự: session cookie → API token (`x-api-key` hoặc Bearer `auto_ontology_…`) → OAuth MCP → SSO. Token *act as owner*. Role chỉ có `admin` / `viewer`.
- Ingestion service phải **1 replica** — scheduler in-process, không leader election.
- Helm chỉ truyền `DEFAULT_MODELS_*` + `EMBED_*`; các triplet `REASONING_*`, `RERANK_*` và mọi `DB_PROBE_*` không truyền → production Helm chạy mọi role bằng 1 model mặc định, flag mặc định.
- Vault bật khi đủ 4 biến; thiếu 1 → fallback plaintext **không báo lỗi**.
- `semantic_compilation_enabled` và `pii_detection_enabled` **mặc định false**.

### Hệ quả lab
- Stack Compose trên Windows chấp nhận được, nhưng **mọi client của lab chỉ gọi `:3000` với `x-api-key`**. `import_dataset.py` hiện gọi `:3001` với `x-auto-ontology-user-id` → phải sửa; giữ đường `:3002/ingest` chỉ cho Suite 4 (vì không có đường public) và ghi rõ.
- Tạo 1 admin + 1 viewer; chạy câu hỏi bằng token viewer; certify/export bằng admin.
- Pin `EMBED_MODEL` cứng — đổi là phải re-ingest toàn bộ.

---

## 2. Ingest → catalog → compile (đường đi thật)

### 2.1 Ingest (`ingestion_service/ingest.py:run_ingest` L82-164)
1. `catalog/extract.py:create_dataframe` L80-102: `get_tables/columns/views/queries/pks/fks` → lọc allow/deny regex.
2. `catalog/write.py:populate_tabular_data` L34-60: `populate_db` (diff theo **tên**, không fingerprint) → `populate_fks` (`last_seen`, xoá FK cũ) → `populate_pks` → `populate_queries`.
3. PII (nếu bật): rule + LLM trên **tên cột/bảng/description**, không dùng sample. Tag `PII` khi conf ≥ 0.9. Không mask.
4. Embed data layer: **xoá toàn bộ** `vdb.data_objects_layer` của database rồi embed lại (`vdb/postgres.py:157-186`). Không incremental.
5. Ingest **không** trigger compile. Không có status API; chỉ log + `catalog_database.pulled`.

### 2.2 Re-ingest bảng/cột đã có (`catalog/diff.py:update_diff_from_existing_schema` L76-250)
| Thay đổi | Catalog | Semantic layer |
|---|---|---|
| Thêm bảng | add | compile pass sau tạo Term |
| Drop bảng | delete cascade cột | `term` **mồ côi** (table__term cascade nhưng term còn), `column_attribute` còn (table_id TEXT), vector `semantic_layer` **cũ còn** |
| Thêm cột vào bảng đã compile | add | **không bao giờ** có ColumnAttribute; rơi vào `find_unlinked_fk_columns` mỗi pass |
| Drop cột | delete | ColumnAttribute mồ côi, vẫn link Term |
| Rename cột | drop + add (id mới) | như drop + thêm cộng lại; description/certified **không theo** |
| Retype | update in place, giữ id | `column_attribute.datatype` stale, sample stale |
| Đổi COMMENT nguồn | **bỏ qua** (chỉ diff type/nullable) | — |
| Đổi phân phối data | — | profile/sample/is_unique **không bao giờ** refresh cho bảng đã compile |

### 2.3 Compile (`semantic/compile.py` → `pipeline.py:compile_semantic_layer` L253-366)
- Chọn bảng: `fetch_all_tables_without_term` → chỉ bảng chưa có Term. 3 worker, commit theo thứ tự canonical.
- Mỗi bảng (`visit_enter.py:process_table` L753):
  - **Profile**: `SELECT * LIMIT 1000` (không ORDER BY), timeout 120s. Top-5 giá trị phổ biến. **Loại** cột kiểu date/time/uuid khỏi sample; giá trị string > 30 ký tự bị bỏ. DISTINCT probe cho cột text có < 5 distinct trong sample, giữ nếu ≤ 25 distinct.
  - **FK suggester** (LLM) → chỉ dùng để loại cột khỏi ColumnAttribute candidates, **không ghi edge**. Đánh dấu junction table.
  - **Term extractor**: prompt "prefer ONE Term per table", tối đa 40 cột đưa vào prompt. Fallback deterministic nếu LLM fail. Bảng toàn FK / bridge **không có Term** → bị xử lý lại **mỗi pass**.
  - Embed vào `vdb.semantic_layer`: `"Term: <name>. <desc>"`, `"ColumnAttribute: <attr> of Term <term> (<synonyms>). <desc><samples>"`.
- Sau tất cả bảng, **mỗi pass** chạy lại: `resolve_semantic_fks` → `suggest_sql_attributes` → `build_bridge_tables_sql_attributes`.
- **Semantic FK** (`semantic/semantic_fk.py`): với cột chưa link, k-NN top 3+3 trên ColumnAttribute `is_unique=True`, LLM chọn hoặc null; nếu null → probe **tất cả** distinct sample của cột nguồn phải có trong cột đích. Sau đó `prune_disjoint_semantic_fks` xoá edge nào không có giá trị chung. **Không có confidence số**, chỉ nhị phân.
- **SQL attributes từ query history**: Postgres chỉ đọc `pg_stat_activity` (query đang/ vừa resident, 24h), không phải lịch sử thật. `_usage_score` **luôn trả 0** (bug đã biết, `sql_attribute_suggester.py:69-110`) → ranking theo tần suất không hoạt động.
- Lịch sử compile: `public.semantic_compilation_history` (1 dòng/pass, tất cả DB). Pass bị stop/crash để `status` NULL mãi.

### Hệ quả lab — ràng buộc cho generator
- **Bẫy phải nằm trong 1000 dòng đầu** của mỗi bảng (không ORDER BY, thực tế là thứ tự heap). Sinh data sao cho distribution của 1000 dòng đầu đại diện.
- Cột enum/status phải là **text, ≤ 25 distinct**, giá trị ≤ 30 ký tự → mới vào sample_values.
- **Semantic FK trap** (JOIN không FK vật lý): cột nguồn và đích **phải có giá trị chung đủ nhiều**, và cột đích phải `is_unique` trong 1000 dòng đầu. Nếu không, edge bị prune hoặc không bao giờ được đề xuất → bẫy thành vô nghĩa.
- Cột ngày **không bao giờ có sample** → bẫy `ambiguous_date` chỉ phân biệt được qua **tên cột và description**. Đây là lý do nên có arm B (schema+metadata).
- Bridge table không có Term → câu hỏi JOIN qua bridge test đường `bridgeTable` sql_attribute.
- Mỗi re-ingest = re-embed toàn bộ data layer; mỗi compile pass = chạy lại 3 stage cho cả DB → budget LLM theo đó.

---

## 3. Answer path (`POST /api/chat/completions`)

- 409 nếu chưa có Term `source='semantic'` nào (`router.py:566`). 409 nếu conversation đang chạy. Chạy trong subprocess worker, **1 warm standby**.
- Graph (`text_to_sql_graph.py:329-612`, `recursion_limit=45`), model role:
  1. `question_intent` (NON_REASONING) → information / prediction / calculation; mặc định calculation.
  2. `question_extraction` (NON_REASONING) → entities, subject.
  3. `retrieve_candidates`: k-NN semantic index — CustomAnalysis k=3, SqlAttribute k=3, ColumnAttribute k=3 (evidence) + k=2/entity, Term k=1 (subject). LLM "intent filter" làm reranker. **RERANK model không được gọi.**
  4. `prepare_candidates`: LLM chọn anchor column, join path từ FK + semantic FK, thêm bảng từ data index (cap 20), LLM table-relevance filter (bỏ qua nếu ≤ 2 bảng).
  5. `construct_sql_from_candidates` (**REASONING**): structured output `{thought, sql_code, response}`; 3×3 lần thử.
  6. `validate_sql_query`: sqlglot + LLM intent validation; nếu LLM fail → coi là hợp lệ.
  7. `execute_sql_query` (timeout 30s mặc định) → lỗi → `reconstruct_sql` (LLM phân loại + viết lại), tối đa **7** lần.
  8. Empty result → `check_empty_like_result`, `check_value_repair` (probe DB).
  9. Hết → `unconstructable_sql_response`; toàn pipeline chạy lại 1 lần nữa (`_MAX_FULL_PIPELINE_ATTEMPTS=2`); lần 2 fail → trả **SQL cuối cùng bị intent-validation từ chối** như fallback (`returned_deterministic_fallback`, không đánh dấu trong payload).
- `validate_sql_values`, `calculation_only`, `shorten_answer`, `sql_examples`, `value_anchors` là payload-only → **production HTTP không bật**.
- `response` (prose) được LLM viết **trước** khi SQL chạy; không có LLM call sau execute.

### Prompt sinh SQL chứa gì (`sql_from_semantic.py:207-348`)
Có: custom prompts, evidence, calculation template, custom analyses (name/desc/sql), sql attributes (name/desc/expression/full_query/term), **term synonyms**, acronyms, schema các bảng được chọn (table description, pk, cột: type/nullable/semantic_fk target/**column description**/**sample_values**/format), join paths, example SQL từ custom analyses.
**Không có**: ColumnAttribute description (chỉ ảnh hưởng retrieval), Term description (chỉ retrieval), tag, zone, PII flag, **mọi cờ certified**.

### Phân loại kết quả từ SSE
- Abstain: `sql_code==""` và `sql_response_from_db==null`.
- Information-type: cũng `sql_code==""`, phân biệt qua `step` event `node=="information_agent"`.
- Deterministic fallback: `sql_code!=""` nhưng không có rows; chỉ thấy trong log WARNING.

### Hệ quả lab
- Arm C' qua gateway chạy **tuần tự**, timeout client ≥ 900s, header `x-auto-ontology-source: lab-<suite>`, conversation mới mỗi câu.
- Latency: `GET /api/analytics` (admin) → `response_timestamp - question_timestamp`, filter `source`.
- Token: proxy log trước model endpoint. Ước lượng success path: ~7–8 NON_REASONING + 1 REASONING + ~3+N EMBED; mỗi vòng repair +2 NON_REASONING.
- Suite 6: 30 câu unanswerable → đo (a) coverage từ `/api/question-entity-coverage`; (b) chat trả SQL hay abstain; (c) nếu trả SQL, SQL có "chạy được nhưng vô nghĩa" không.

---

## 4. Certification, write API, export

- Cờ: `term.name_certified/description_certified`, `column_attribute.certified`, `sql_attribute.certified`, `catalog_*.description_certified`. Rollup Term = certified / partial / pending (`dal/terms.py:156-239`). Không có workflow/audit.
- Write (qua `:3000`, cần `catalog:edit` = admin hoặc owner-group): `PATCH /api/terms/{id}`, `PATCH /api/terms/{id}/column-attributes/{attr}` (có `sample_values` → ghi vào `catalog_column`), `PATCH|PUT /api/sql-attributes/{id}`, `PATCH /api/nodes/{id}`. Chỉ `PUT` sql-attribute re-parse SQL. Không có POST tạo Term rỗng.
- Thứ **thực sự** đổi câu trả lời: `description`/`name` (re-embed), `sample_values` (vào prompt), sql attribute, custom analysis, acronyms, custom prompts. Có hiệu lực ngay câu hỏi sau.
- Export `POST /api/model/export {databases:[], format:"auto_ontology"|"ossie", include_ontology}` → YAML. Có: catalog + sample_values + FK/join/semantic_fk + sql attributes + custom analyses. **Không có** certified, synonyms, owner, tag, zone. 3 list quan hệ không có thứ tự.
- Import `replace=true` mặc định; bug mở #250, #265, #267.
- Quy trình kiểm chứng chính thức của NVIDIA (`skills/nvidia-ontology-management/references/publication.md`): export + SHA-256 → apply → re-export → so sánh exact IDs/structure/counts → probe. **Lab dùng đúng quy trình này** cho Suite 1 và 4.

---

## 5. Eval harness chính thức (`auto-ontology-eval`)

- Pin `auto-ontology-server @ dd78d068` (không phải main), `nemo-gym==0.7.0`, Python 3.13.
- **Ingest/compile in-process**, không qua API. Dataset Postgres: `datasets/<db>/{evaluation.json, metadata.json?, custom_analyses.json?, ddl/, data/}`; tên folder = `current_database()` = `db_id` (case-sensitive).
- **Control**: 1 call LLM, prompt = question + evidence + **toàn bộ folder `ddl/` nối lại** (DDL thô, không sample, không metadata). SQL extract bằng regex bird_sql: `--` comment **cắt mất phần sau**, whitespace trong literal bị collapse.
- **Treatment**: `stream_agent_response` trực tiếp với `validate_sql_values=True, calculation_only=True, shorten_answer=True`; lấy `sql_code` raw. Nhận `metadata.json` descriptions + value_examples + custom analyses mà control không có. Abstain = `no_model_output` → tính vào health gate 5%.
- Evidence đưa cho **cả hai** arm.
- Scorer: `set(gold)==set(pred)` — bỏ duplicate, bỏ order, **không tolerance float**, cột thừa = fail. Gold lỗi → loại task khỏi cả 2 arm. **Không read-only transaction, không statement_timeout server-side**, dùng DSN đầu tiên trong `CONNECTION_STRINGS`.
- Model control (`GYM_MODEL`) và treatment (`REASONING_MODEL`) cấu hình riêng, **không bị ép bằng nhau**. Temperature không set. Không seed, 1 rollout/task, không CI.
- Thêm arm 3: ~100–150 dòng, chạm `tasks.py` (Arm literal, `SERVER_FOR_ARM`, `_schema_for`), `ddl.py` (`metadata_description_block`), copy `resources_servers/schema_only_sql/` → `schema_metadata_sql/`, `main.py` (`RUN_LABEL`, compare pairwise).

### Bất đối xứng phải công bố khi dùng harness
1. Model không ép bằng nhau; treatment có thêm embed + non-reasoning model.
2. Control 1 call không DB; treatment ~19–23 call, **chạy SQL và probe giá trị trên DB thật** khi sinh.
3. Treatment có metadata/sample/custom analyses; control chỉ DDL.
4. Regex extract control có thể cắt SQL; treatment dùng raw.
5. Abstain của treatment chặn report; control không có SQL block → `SELECT 1` → miss im lặng.
6. Treatment knob `validate_sql_values`, `calculation_only` **không có trong production**.

---

## 6. Quyết định pin

| Lựa chọn | Ưu | Nhược |
|---|---|---|
| Pin `dd78d068` (theo eval repo) | Dùng được harness nguyên bản cho Suite 2 | Cũ hơn main ~1 tháng; Suite 4 đo trên code cũ |
| Pin `c447003f` (main hiện tại) | Mới nhất, đúng với hành vi hôm nay | Harness có thể lệch (connector naming, payload); phải sửa |

**Khuyến nghị**: pin `c447003f` cho stack chạy (Suite 1, 4, 5, 6, C'); chạy harness Suite 2 trên **cùng** stack bằng cách sửa pin trong `pyproject.toml` của eval repo và test smoke 5 câu. Nếu vỡ, fallback pin `dd78d068` cho **riêng** Suite 2 và ghi rõ hai commit trong `freeze.yaml`. Không trộn.

---

## 7. Checklist môi trường "giống production"

- [ ] `:3001`, `:3002` không expose ra ngoài máy; lab script dùng `:3000` + `x-api-key`. Ngoại lệ duy nhất: `:3002/ingest` và `:3002/semantic/status` cho Suite 4 (không có đường public), ghi rõ trong report.
- [ ] 1 admin + 1 viewer; token viewer cho hỏi, token admin cho certify/export/analytics.
- [ ] `semantic_compilation_enabled=true`, `pii_detection_enabled=true` qua `PUT /api/configurations/...` (không ghi thẳng bảng).
- [ ] Pin toàn bộ model triplet; `EMBED_*` giống nhau giữa ingestion và backend; `LLM_MAX_INFLIGHT` ≤ 6.
- [ ] `CONNECTION_STRINGS` **rỗng**, mọi connection thêm qua UI/API (tránh precedence lệch: ingestion ưu tiên env, chat ưu tiên UI).
- [ ] Role Postgres read-only cho source DB, `default_transaction_read_only=on`, `statement_timeout`.
- [ ] Proxy log trước model endpoint để đếm token.
- [ ] Snapshot trước mỗi suite: `public.*` (catalog + semantic + `semantic_compilation_history`), `frontend.configurations`, `frontend.conversation_analytics`, `vdb.*`. Restore bằng `pg_restore`, không "chạy tiếp".
- [ ] Không `git pull` upstream trong suốt lab. Schema upstream đổi ~4–5 ngày/lần (7 migration trong 4 tuần).

---

## 8. Bảng tra nhanh: endpoint lab cần

| Việc | Gọi | Auth |
|---|---|---|
| Thêm connection + ingest lần đầu | `POST /api/connections/test`, `POST /api/connections` | admin |
| Re-ingest DB đã có (Suite 4) | `POST :3002/ingest {"database":"<db>"}` | **không có** — nội bộ |
| Trigger compile | `POST /api/semantic-compilation/trigger` | admin |
| Trạng thái compile | `GET /api/semantic-compilation/status` + `GET :3002/semantic/status` + bảng `semantic_compilation_history` | viewer / nội bộ / SQL |
| Full rebuild 1 DB | `POST :3002/semantic/reset?database_name=<db>` (public reset = **tất cả** DB) | nội bộ |
| Hỏi | `POST /api/chat/completions` SSE, header `x-auto-ontology-source` | viewer |
| Coverage / answerable | `POST /api/question-entity-coverage {question, max_distance}` | viewer |
| Certify | `PATCH /api/terms/{id}`, `/column-attributes/{attr}`, `/api/sql-attributes/{id}`, `/api/nodes/{id}` | admin |
| Export ontology | `POST /api/model/export {"databases":[],"format":"auto_ontology"}` | admin |
| Đọc cờ certified | `GET /api/terms`, `/api/terms/{id}`, `/api/terms/{id}/column-attributes`, `/api/sql-attributes` | viewer |
| Latency | `GET /api/analytics?limit=500` | admin |
| Thêm "reviewed" content | `PATCH description/sample_values`, `POST /api/sql-attributes`, `POST /api/custom-analyses` | admin |
