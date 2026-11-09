-- =============================================================================
-- Step 1 Acceptance Suite Seed Data (accept_seed.sql)
-- Every non-allowlisted text value, array element, and JSON string contains CANARY_7f3a.
-- PII numeric sentinel: 987654321012 | PII date sentinel: 1901-02-03
-- =============================================================================

SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

-- Excluded plumbing table
INSERT INTO public.schema_migrations (version)
VALUES ('CANARY_7f3a_v001'), ('CANARY_7f3a_v002');

-- 1. region_config (2 rows)
INSERT INTO public.region_config (region_code, description)
VALUES
    ('CANARY_7f3a_NA', 'CANARY_7f3a North America region'),
    ('CANARY_7f3a_EU', 'CANARY_7f3a Europe region');

-- 2. subscription_tier (5 rows)
-- tier_level is allowlisted; internal_bucket is NOT allowlisted and contains CANARY_7f3a
INSERT INTO public.subscription_tier (tier_id, tier_level, internal_bucket, monthly_fee)
VALUES
    (1, 'basic',      'CANARY_7f3a_bucket_a', 29.00),
    (2, 'basic',      'CANARY_7f3a_bucket_a', 49.00),
    (3, 'pro',        'CANARY_7f3a_bucket_b', 99.00),
    (4, 'pro',        'CANARY_7f3a_bucket_b', 149.00),
    (5, 'enterprise', 'CANARY_7f3a_bucket_c', 299.00);

SELECT setval(pg_get_serial_sequence('public.subscription_tier', 'tier_id'), 5, true);

-- 3. organization (50 rows)
INSERT INTO public.organization (
    org_id, org_name, org_slug, region_code, tier_id, tags, settings_json
)
SELECT
    ('00000000-0000-4000-8000-' || lpad(to_hex(i), 12, '0'))::uuid,
    'CANARY_7f3a Org Name ' || i,
    'CANARY_7f3a-org-' || i,
    CASE WHEN i % 2 = 0 THEN 'CANARY_7f3a_NA' ELSE 'CANARY_7f3a_EU' END,
    ((i - 1) % 5) + 1,
    ARRAY['CANARY_7f3a_tag1', 'CANARY_7f3a_tag2']::text[],
    jsonb_build_object('secret', 'CANARY_7f3a_json_' || i)
FROM generate_series(1, 50) AS s(i);

-- 4. department (60 rows, with self-referencing parent_dept_id)
INSERT INTO public.department (dept_id, org_id, parent_dept_id, dept_code, dept_title)
SELECT
    i,
    ('00000000-0000-4000-8000-' || lpad(to_hex(((i - 1) % 50) + 1), 12, '0'))::uuid,
    CASE WHEN i <= 15 THEN NULL ELSE ((i - 1) % 15) + 1 END,
    'CANARY_7f3a_' || i,
    'CANARY_7f3a Department ' || i
FROM generate_series(1, 60) AS s(i);

SELECT setval(pg_get_serial_sequence('public.department', 'dept_id'), 60, true);

-- 5 & 6. research_team (50 rows) <-> team_member (80 rows) cycle
INSERT INTO public.research_team (team_id, org_id, team_title, lead_member_id)
SELECT
    i,
    ('00000000-0000-4000-8000-' || lpad(to_hex(i), 12, '0'))::uuid,
    'CANARY_7f3a Team ' || i,
    NULL
FROM generate_series(1, 50) AS s(i);

SELECT setval(pg_get_serial_sequence('public.research_team', 'team_id'), 50, true);

INSERT INTO public.team_member (member_id, team_id, member_email, role_note)
SELECT
    i,
    ((i - 1) % 50) + 1,
    'CANARY_7f3a_member_' || i || '@example.com',
    CASE WHEN i % 4 = 0 THEN NULL ELSE 'CANARY_7f3a role note ' || i END
FROM generate_series(1, 80) AS s(i);

SELECT setval(pg_get_serial_sequence('public.team_member', 'member_id'), 80, true);

UPDATE public.research_team
SET lead_member_id = team_id
WHERE team_id <= 40;

-- 7. program (60 rows)
INSERT INTO public.program (org_id, program_id, program_title, credits, start_date, end_date)
SELECT
    ('00000000-0000-4000-8000-' || lpad(to_hex(((i - 1) % 50) + 1), 12, '0'))::uuid,
    i,
    'CANARY_7f3a Program ' || i,
    ((i - 1) % 12) + 1,
    DATE '2023-01-01' + ((i % 30) * INTERVAL '1 day')::interval,
    DATE '2023-06-01' + ((i % 30) * INTERVAL '1 day')::interval
FROM generate_series(1, 60) AS s(i);

-- 8. program_unit (80 rows)
INSERT INTO public.program_unit (org_id, program_id, unit_id, unit_summary)
SELECT
    ('00000000-0000-4000-8000-' || lpad(to_hex(((((i - 1) % 60)) % 50) + 1), 12, '0'))::uuid,
    ((i - 1) % 60) + 1,
    i,
    'CANARY_7f3a Unit Summary ' || i
FROM generate_series(1, 80) AS s(i);

-- 9. unit_assignment (100 rows, sharing (org_id, program_id) across two composite FKs)
INSERT INTO public.unit_assignment (assignment_id, org_id, program_id, unit_id, weight_pct)
SELECT
    i,
    pu.org_id,
    pu.program_id,
    pu.unit_id,
    ROUND((10.0 + (i % 80))::numeric, 2)
FROM generate_series(1, 100) AS s(i)
JOIN public.program_unit pu ON pu.unit_id = ((i - 1) % 80) + 1;

SELECT setval(pg_get_serial_sequence('public.unit_assignment', 'assignment_id'), 100, true);

-- 10. course (60 rows, parent of course_section and target of implicit FK `code`)
INSERT INTO public.course (course_id, org_id, code, min_seats, max_seats)
SELECT
    i,
    ('00000000-0000-4000-8000-' || lpad(to_hex(((i - 1) % 50) + 1), 12, '0'))::uuid,
    'CANARY_7f3a_COURSE_' || i,
    10 + (i % 18),
    (10 + (i % 18)) + 15 + (i % 15)
FROM generate_series(1, 60) AS s(i);

SELECT setval(pg_get_serial_sequence('public.course', 'course_id'), 60, true);

-- 11. course_section (80 rows)
INSERT INTO public.course_section (section_id, course_id, advisor_member_id, room_label)
SELECT
    i,
    ((i - 1) % 60) + 1,
    CASE WHEN i % 5 = 0 THEN NULL ELSE ((i - 1) % 80) + 1 END,
    'CANARY_7f3a Room ' || i
FROM generate_series(1, 80) AS s(i);

SELECT setval(pg_get_serial_sequence('public.course_section', 'section_id'), 80, true);

-- 12. student (80 rows, with PII sentinels 1901-02-03 and 987654321012)
INSERT INTO public.student (
    student_id, org_id, email, full_name, birth_date, phone_number, status, scholarship_tier, is_deleted
)
SELECT
    i,
    ('00000000-0000-4000-8000-' || lpad(to_hex(((i - 1) % 50) + 1), 12, '0'))::uuid,
    'CANARY_7f3a_student_' || i || '@example.org',
    'CANARY_7f3a Student ' || i,
    DATE '1901-02-03',
    987654321012::bigint,
    (ARRAY['active', 'dropped', 'paused', 'completed']::public.enrollment_status[])[((i - 1) % 4) + 1],
    (ARRAY['none', 'half', 'full'])[((i - 1) % 3) + 1],
    (i % 10 = 0)
FROM generate_series(1, 80) AS s(i);

SELECT setval(pg_get_serial_sequence('public.student', 'student_id'), 80, true);

-- 13. enrollment (100 rows)
INSERT INTO public.enrollment (student_id, section_id, enrolled_on)
SELECT
    ((i - 1) % 80) + 1,
    (((i - 1) * 7 + (i / 80)) % 80) + 1,
    DATE '2024-01-15' + ((i % 60) * INTERVAL '1 day')::interval
FROM generate_series(1, 100) AS s(i);

-- 14. payment (100 rows: implicit FK `course_code`, veto-overridden `card_type`, unsatisfiable CHECK on `audit_memo`)
INSERT INTO public.payment (
    payment_id, student_id, course_code, card_type, amount_cents, audit_memo
)
SELECT
    i,
    ((i - 1) % 80) + 1,
    'CANARY_7f3a_COURSE_' || (((i - 1) % 60) + 1),
    CASE
        WHEN i % 10 < 6 THEN 'visa'
        WHEN i % 10 < 9 THEN 'mastercard'
        ELSE 'amex'
    END,
    1000 + (i * 250),
    'CANARY_7f3a_memo'
FROM generate_series(1, 100) AS s(i);

SELECT setval(pg_get_serial_sequence('public.payment', 'payment_id'), 100, true);

-- 15. activity_event (11,000 rows -> 1,100 target rows at scale=0.1 for A11 first-1,000-rows check)
INSERT INTO public.activity_event (
    event_id, student_id, event_kind, duration_sec, ref_note
)
SELECT
    i,
    ((i - 1) % 80) + 1,
    CASE
        WHEN i % 20 < 11 THEN 'view'
        WHEN i % 20 < 17 THEN 'submit'
        ELSE 'grade'
    END,
    30 + (i % 600),
    CASE WHEN i % 5 = 0 THEN NULL ELSE 'CANARY_7f3a activity note ' || i END
FROM generate_series(1, 11000) AS s(i);

SELECT setval(pg_get_serial_sequence('public.activity_event', 'event_id'), 11000, true);
