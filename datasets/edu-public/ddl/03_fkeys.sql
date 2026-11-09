ALTER TABLE ONLY public.assignment ADD CONSTRAINT assignment_cls_id_fkey FOREIGN KEY (cls_id) REFERENCES cls_mst(cls_id);

ALTER TABLE ONLY public.attendance ADD CONSTRAINT attendance_sess_id_fkey FOREIGN KEY (sess_id) REFERENCES cls_sess(sess_id);

ALTER TABLE ONLY public.attendance ADD CONSTRAINT attendance_std_id_fkey FOREIGN KEY (std_id) REFERENCES std_mst(std_id);

ALTER TABLE ONLY public.cls_instr ADD CONSTRAINT cls_instr_cls_id_fkey FOREIGN KEY (cls_id) REFERENCES cls_mst(cls_id);

ALTER TABLE ONLY public.cls_instr ADD CONSTRAINT cls_instr_instr_id_fkey FOREIGN KEY (instr_id) REFERENCES instr_mst(instr_id);

ALTER TABLE ONLY public.cls_mst ADD CONSTRAINT cls_mst_crs_id_fkey FOREIGN KEY (crs_id) REFERENCES crs_mst(crs_id);

ALTER TABLE ONLY public.cls_mst ADD CONSTRAINT cls_mst_term_id_fkey FOREIGN KEY (term_id) REFERENCES term_mst(term_id);

ALTER TABLE ONLY public.cls_sess ADD CONSTRAINT cls_sess_cls_id_fkey FOREIGN KEY (cls_id) REFERENCES cls_mst(cls_id);

ALTER TABLE ONLY public.crs_mst ADD CONSTRAINT crs_mst_dept_cd_fkey FOREIGN KEY (dept_cd) REFERENCES dept_mst(dept_cd);

ALTER TABLE ONLY public.enrollment ADD CONSTRAINT enrollment_cls_id_fkey FOREIGN KEY (cls_id) REFERENCES cls_mst(cls_id);

ALTER TABLE ONLY public.enrollment ADD CONSTRAINT enrollment_std_id_fkey FOREIGN KEY (std_id) REFERENCES std_mst(std_id);

ALTER TABLE ONLY public.grade ADD CONSTRAINT grade_enr_id_fkey FOREIGN KEY (enr_id) REFERENCES enrollment(enr_id);

ALTER TABLE ONLY public.instr_mst ADD CONSTRAINT instr_mst_dept_cd_fkey FOREIGN KEY (dept_cd) REFERENCES dept_mst(dept_cd);

ALTER TABLE ONLY public.invoice ADD CONSTRAINT invoice_std_id_fkey FOREIGN KEY (std_id) REFERENCES std_mst(std_id);

ALTER TABLE ONLY public.invoice ADD CONSTRAINT invoice_term_id_fkey FOREIGN KEY (term_id) REFERENCES term_mst(term_id);

ALTER TABLE ONLY public.std_mst ADD CONSTRAINT std_mst_dept_cd_fkey FOREIGN KEY (dept_cd) REFERENCES dept_mst(dept_cd);

ALTER TABLE ONLY public.submission ADD CONSTRAINT submission_asg_id_fkey FOREIGN KEY (asg_id) REFERENCES assignment(asg_id);

ALTER TABLE ONLY public.submission ADD CONSTRAINT submission_std_id_fkey FOREIGN KEY (std_id) REFERENCES std_mst(std_id);
