CREATE TABLE public.assignment (
    asg_id integer NOT NULL,
    cls_id integer NOT NULL,
    title text NOT NULL,
    due_dt date NOT NULL,
    max_pts integer NOT NULL,
    CONSTRAINT assignment_pkey PRIMARY KEY (asg_id)
);

CREATE TABLE public.attendance (
    att_id integer NOT NULL,
    sess_id integer NOT NULL,
    std_id integer NOT NULL,
    att_sts text NOT NULL,
    CONSTRAINT attendance_pkey PRIMARY KEY (att_id)
);

ALTER TABLE public.attendance ADD CONSTRAINT chk_att_sts CHECK (att_sts = ANY (ARRAY['present'::text, 'absent'::text, 'late'::text]));

CREATE TABLE public.cls_instr (
    cls_id integer NOT NULL,
    instr_id integer NOT NULL,
    CONSTRAINT cls_instr_pkey PRIMARY KEY (cls_id, instr_id)
);

CREATE TABLE public.cls_mst (
    cls_id integer NOT NULL,
    crs_id integer NOT NULL,
    term_id integer NOT NULL,
    start_date date NOT NULL,
    capacity integer NOT NULL,
    cls_sts text NOT NULL,
    CONSTRAINT cls_mst_pkey PRIMARY KEY (cls_id)
);

ALTER TABLE public.cls_mst ADD CONSTRAINT chk_cls_sts CHECK (cls_sts = ANY (ARRAY['open'::text, 'closed'::text, 'cancelled'::text]));

CREATE TABLE public.cls_sess (
    sess_id integer NOT NULL,
    cls_id integer NOT NULL,
    sess_dt date NOT NULL,
    sess_sts text NOT NULL,
    CONSTRAINT cls_sess_pkey PRIMARY KEY (sess_id)
);

ALTER TABLE public.cls_sess ADD CONSTRAINT chk_sess_sts CHECK (sess_sts = ANY (ARRAY['held'::text, 'cancelled'::text]));

CREATE TABLE public.crs_mst (
    crs_id integer NOT NULL,
    crs_code text NOT NULL,
    title text NOT NULL,
    dept_cd text NOT NULL,
    credits integer NOT NULL,
    legacy_code text,
    CONSTRAINT crs_mst_pkey PRIMARY KEY (crs_id),
    CONSTRAINT crs_mst_crs_code_key UNIQUE (crs_code)
);

ALTER TABLE public.crs_mst ADD CONSTRAINT chk_crs_credits CHECK (credits >= 1 AND credits <= 6);

CREATE TABLE public.dept_mst (
    dept_cd text NOT NULL,
    dept_nm text NOT NULL,
    CONSTRAINT dept_mst_pkey PRIMARY KEY (dept_cd)
);

CREATE TABLE public.enrollment (
    enr_id integer NOT NULL,
    std_id integer NOT NULL,
    cls_id integer NOT NULL,
    enrolled_at date NOT NULL,
    created_at date NOT NULL,
    enr_sts text NOT NULL,
    CONSTRAINT enrollment_pkey PRIMARY KEY (enr_id)
);

ALTER TABLE public.enrollment ADD CONSTRAINT chk_enr_sts CHECK (enr_sts = ANY (ARRAY['enrolled'::text, 'dropped'::text, 'completed'::text]));

CREATE TABLE public.grade (
    enr_id integer NOT NULL,
    final_score numeric(5,2) NOT NULL,
    letter text NOT NULL,
    CONSTRAINT grade_pkey PRIMARY KEY (enr_id)
);

ALTER TABLE public.grade ADD CONSTRAINT chk_grade_letter CHECK (letter = ANY (ARRAY['A'::text, 'B'::text, 'C'::text, 'D'::text, 'F'::text]));

CREATE TABLE public.instr_mst (
    instr_id integer NOT NULL,
    full_nm text NOT NULL,
    email text NOT NULL,
    dept_cd text NOT NULL,
    hire_dt date NOT NULL,
    CONSTRAINT instr_mst_pkey PRIMARY KEY (instr_id)
);

CREATE TABLE public.invoice (
    inv_id integer NOT NULL,
    code text NOT NULL,
    std_id integer NOT NULL,
    term_id integer NOT NULL,
    amount numeric(10,2) NOT NULL,
    issued_dt date NOT NULL,
    inv_sts text NOT NULL,
    CONSTRAINT invoice_pkey PRIMARY KEY (inv_id),
    CONSTRAINT invoice_code_key UNIQUE (code)
);

ALTER TABLE public.invoice ADD CONSTRAINT chk_inv_sts CHECK (inv_sts = ANY (ARRAY['paid'::text, 'unpaid'::text, 'void'::text]));

CREATE TABLE public.payment (
    pay_id integer NOT NULL,
    ref_code text NOT NULL,
    amount numeric(10,2) NOT NULL,
    paid_dt date NOT NULL,
    method text NOT NULL,
    CONSTRAINT payment_pkey PRIMARY KEY (pay_id)
);

ALTER TABLE public.payment ADD CONSTRAINT chk_pay_method CHECK (method = ANY (ARRAY['card'::text, 'transfer'::text, 'cash'::text]));

CREATE TABLE public.std_mst (
    std_id integer NOT NULL,
    full_nm text NOT NULL,
    email text NOT NULL,
    phone text,
    dept_cd text NOT NULL,
    rec_sts text NOT NULL,
    created_at date NOT NULL,
    CONSTRAINT std_mst_pkey PRIMARY KEY (std_id)
);

ALTER TABLE public.std_mst ADD CONSTRAINT chk_std_rec_sts CHECK (rec_sts = ANY (ARRAY['active'::text, 'deleted'::text]));

CREATE TABLE public.submission (
    sub_id integer NOT NULL,
    asg_id integer NOT NULL,
    std_id integer NOT NULL,
    submitted_at date NOT NULL,
    score numeric(6,2) NOT NULL,
    CONSTRAINT submission_pkey PRIMARY KEY (sub_id)
);

CREATE TABLE public.term_mst (
    term_id integer NOT NULL,
    term_cd text NOT NULL,
    start_dt date NOT NULL,
    end_dt date NOT NULL,
    CONSTRAINT term_mst_pkey PRIMARY KEY (term_id),
    CONSTRAINT term_mst_term_cd_key UNIQUE (term_cd)
);

ALTER TABLE public.term_mst ADD CONSTRAINT chk_term_dates CHECK (end_dt >= start_dt);
