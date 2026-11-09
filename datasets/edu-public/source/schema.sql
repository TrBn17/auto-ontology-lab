-- edu-public source schema: an original schema modelled on common LMS concepts.
-- Names are abbreviated on purpose, as in many production systems, to test what naming alone can reveal.
-- Traps: ambiguous dates (enrollment.enrolled_at / created_at, cls_mst.start_date), soft delete
-- (std_mst.rec_sts), a join with no FK (payment.ref_code = invoice.code), a bridge table (cls_instr).

CREATE TABLE dept_mst (
    dept_cd text PRIMARY KEY,
    dept_nm text NOT NULL
);

CREATE TABLE std_mst (
    std_id integer PRIMARY KEY,
    full_nm text NOT NULL,
    email text NOT NULL,
    phone text,
    dept_cd text NOT NULL REFERENCES dept_mst (dept_cd),
    rec_sts text NOT NULL,
    created_at date NOT NULL,
    CONSTRAINT chk_std_rec_sts CHECK (rec_sts IN ('active', 'deleted'))
);

CREATE TABLE instr_mst (
    instr_id integer PRIMARY KEY,
    full_nm text NOT NULL,
    email text NOT NULL,
    dept_cd text NOT NULL REFERENCES dept_mst (dept_cd),
    hire_dt date NOT NULL
);

CREATE TABLE crs_mst (
    crs_id integer PRIMARY KEY,
    crs_code text NOT NULL UNIQUE,
    title text NOT NULL,
    dept_cd text NOT NULL REFERENCES dept_mst (dept_cd),
    credits integer NOT NULL,
    legacy_code text,
    CONSTRAINT chk_crs_credits CHECK (credits BETWEEN 1 AND 6)
);

CREATE TABLE term_mst (
    term_id integer PRIMARY KEY,
    term_cd text NOT NULL UNIQUE,
    start_dt date NOT NULL,
    end_dt date NOT NULL,
    CONSTRAINT chk_term_dates CHECK (end_dt >= start_dt)
);

CREATE TABLE cls_mst (
    cls_id integer PRIMARY KEY,
    crs_id integer NOT NULL REFERENCES crs_mst (crs_id),
    term_id integer NOT NULL REFERENCES term_mst (term_id),
    start_date date NOT NULL,
    capacity integer NOT NULL,
    cls_sts text NOT NULL,
    CONSTRAINT chk_cls_sts CHECK (cls_sts IN ('open', 'closed', 'cancelled'))
);

CREATE TABLE cls_instr (
    cls_id integer NOT NULL REFERENCES cls_mst (cls_id),
    instr_id integer NOT NULL REFERENCES instr_mst (instr_id),
    PRIMARY KEY (cls_id, instr_id)
);

CREATE TABLE enrollment (
    enr_id integer PRIMARY KEY,
    std_id integer NOT NULL REFERENCES std_mst (std_id),
    cls_id integer NOT NULL REFERENCES cls_mst (cls_id),
    enrolled_at date NOT NULL,
    created_at date NOT NULL,
    enr_sts text NOT NULL,
    CONSTRAINT chk_enr_sts CHECK (enr_sts IN ('enrolled', 'dropped', 'completed'))
);

CREATE TABLE cls_sess (
    sess_id integer PRIMARY KEY,
    cls_id integer NOT NULL REFERENCES cls_mst (cls_id),
    sess_dt date NOT NULL,
    sess_sts text NOT NULL,
    CONSTRAINT chk_sess_sts CHECK (sess_sts IN ('held', 'cancelled'))
);

CREATE TABLE attendance (
    att_id integer PRIMARY KEY,
    sess_id integer NOT NULL REFERENCES cls_sess (sess_id),
    std_id integer NOT NULL REFERENCES std_mst (std_id),
    att_sts text NOT NULL,
    CONSTRAINT chk_att_sts CHECK (att_sts IN ('present', 'absent', 'late'))
);

CREATE TABLE assignment (
    asg_id integer PRIMARY KEY,
    cls_id integer NOT NULL REFERENCES cls_mst (cls_id),
    title text NOT NULL,
    due_dt date NOT NULL,
    max_pts integer NOT NULL
);

CREATE TABLE submission (
    sub_id integer PRIMARY KEY,
    asg_id integer NOT NULL REFERENCES assignment (asg_id),
    std_id integer NOT NULL REFERENCES std_mst (std_id),
    submitted_at date NOT NULL,
    score numeric(6, 2) NOT NULL
);

CREATE TABLE grade (
    enr_id integer PRIMARY KEY REFERENCES enrollment (enr_id),
    final_score numeric(5, 2) NOT NULL,
    letter text NOT NULL,
    CONSTRAINT chk_grade_letter CHECK (letter IN ('A', 'B', 'C', 'D', 'F'))
);

CREATE TABLE invoice (
    inv_id integer PRIMARY KEY,
    code text NOT NULL UNIQUE,
    std_id integer NOT NULL REFERENCES std_mst (std_id),
    term_id integer NOT NULL REFERENCES term_mst (term_id),
    amount numeric(10, 2) NOT NULL,
    issued_dt date NOT NULL,
    inv_sts text NOT NULL,
    CONSTRAINT chk_inv_sts CHECK (inv_sts IN ('paid', 'unpaid', 'void'))
);

-- ref_code holds invoice.code with no declared FK, as in systems fed by a payment provider.
CREATE TABLE payment (
    pay_id integer PRIMARY KEY,
    ref_code text NOT NULL,
    amount numeric(10, 2) NOT NULL,
    paid_dt date NOT NULL,
    method text NOT NULL,
    CONSTRAINT chk_pay_method CHECK (method IN ('card', 'transfer', 'cash'))
);
