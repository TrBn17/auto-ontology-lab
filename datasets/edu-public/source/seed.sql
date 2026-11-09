-- Deterministic source data for edu-public. Run in one session after schema.sql, so setseed applies throughout.
-- Every value is invented; there is no personal or production data here.

SELECT setseed(0.42);

INSERT INTO dept_mst
SELECT 'D' || lpad(i::text, 2, '0'), 'Department ' || i FROM generate_series(1, 8) AS i;

INSERT INTO std_mst
SELECT i, 'Student ' || i, 'student' || i || '@example.test', '+1-555-' || lpad((i % 10000)::text, 4, '0'),
       'D' || lpad((1 + i % 8)::text, 2, '0'),
       CASE WHEN random() < 0.08 THEN 'deleted' ELSE 'active' END,
       date '2023-01-01' + (random() * 1000)::int
FROM generate_series(1, 2000) AS i;

INSERT INTO instr_mst
SELECT i, 'Instructor ' || i, 'instructor' || i || '@example.test', 'D' || lpad((1 + i % 8)::text, 2, '0'),
       date '2015-01-01' + (random() * 3000)::int
FROM generate_series(1, 120) AS i;

INSERT INTO crs_mst
SELECT i, 'CRS' || lpad(i::text, 4, '0'), 'Course ' || i, 'D' || lpad((1 + i % 8)::text, 2, '0'),
       1 + (random() * 5)::int, 'OLD-' || i
FROM generate_series(1, 150) AS i;

INSERT INTO term_mst
SELECT i, 'T' || (2023 + (i - 1) / 2) || CASE WHEN i % 2 = 1 THEN 'S' ELSE 'F' END,
       date '2023-01-15' + (i - 1) * 182, date '2023-01-15' + (i - 1) * 182 + 120
FROM generate_series(1, 8) AS i;

INSERT INTO cls_mst
SELECT i, 1 + (random() * 149)::int, 1 + (random() * 7)::int, date '2023-01-15' + (random() * 1300)::int,
       15 + (random() * 45)::int,
       CASE WHEN random() < 0.15 THEN 'cancelled' WHEN random() < 0.5 THEN 'closed' ELSE 'open' END
FROM generate_series(1, 600) AS i;

INSERT INTO cls_instr
SELECT DISTINCT 1 + (i % 600), 1 + (random() * 119)::int FROM generate_series(0, 699) AS i;

INSERT INTO enrollment
SELECT i, 1 + (random() * 1999)::int, 1 + (random() * 599)::int,
       date '2023-01-01' + (random() * 1300)::int, date '2023-01-01' + (random() * 1300)::int,
       CASE WHEN random() < 0.12 THEN 'dropped' WHEN random() < 0.6 THEN 'completed' ELSE 'enrolled' END
FROM generate_series(1, 6000) AS i;

INSERT INTO cls_sess
SELECT i, 1 + (random() * 599)::int, date '2023-01-15' + (random() * 1300)::int,
       CASE WHEN random() < 0.18 THEN 'cancelled' ELSE 'held' END
FROM generate_series(1, 3000) AS i;

INSERT INTO attendance
SELECT i, 1 + (random() * 2999)::int, 1 + (random() * 1999)::int,
       CASE WHEN random() < 0.78 THEN 'present' WHEN random() < 0.6 THEN 'absent' ELSE 'late' END
FROM generate_series(1, 12000) AS i;

INSERT INTO assignment
SELECT i, 1 + (random() * 599)::int, 'Assignment ' || i, date '2023-02-01' + (random() * 1300)::int,
       10 * (1 + (random() * 9)::int)
FROM generate_series(1, 1200) AS i;

INSERT INTO submission
SELECT i, 1 + (random() * 1199)::int, 1 + (random() * 1999)::int, date '2023-02-01' + (random() * 1300)::int,
       round((random() * 100)::numeric, 2)
FROM generate_series(1, 6000) AS i;

INSERT INTO grade
SELECT i, round((40 + random() * 60)::numeric, 2),
       (ARRAY['A', 'B', 'C', 'D', 'F'])[1 + (random() * 4)::int]
FROM generate_series(1, 4000) AS i;

INSERT INTO invoice
SELECT i, 'INV-' || lpad(i::text, 6, '0'), 1 + (random() * 1999)::int, 1 + (random() * 7)::int,
       round((200 + random() * 3000)::numeric, 2), date '2023-01-01' + (random() * 1300)::int,
       CASE WHEN random() < 0.05 THEN 'void' WHEN random() < 0.7 THEN 'paid' ELSE 'unpaid' END
FROM generate_series(1, 3000) AS i;

INSERT INTO payment
SELECT i, 'INV-' || lpad((1 + (random() * 2999)::int)::text, 6, '0'), round((50 + random() * 2000)::numeric, 2),
       date '2023-01-01' + (random() * 1300)::int,
       CASE WHEN random() < 0.6 THEN 'card' WHEN random() < 0.8 THEN 'transfer' ELSE 'cash' END
FROM generate_series(1, 2500) AS i;
