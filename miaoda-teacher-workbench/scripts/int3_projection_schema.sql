BEGIN;

CREATE TABLE student_profile (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  learner_key varchar(128) NOT NULL UNIQUE,
  learner_alias varchar(80) NOT NULL,
  class_key varchar(128) NOT NULL,
  current_concept text,
  mastery_stage varchar(40),
  ai_assistance_level integer NOT NULL DEFAULT 0,
  ai_assistance_label varchar(80) NOT NULL DEFAULT '未使用',
  skill_patch_count integer NOT NULL DEFAULT 0,
  last_active_at TIMESTAMP(3) WITH TIME ZONE,
  active_today boolean NOT NULL DEFAULT false,
  needs_attention boolean NOT NULL DEFAULT false,
  attention_reason text,
  growth_document_url text,
  template_version varchar(16) NOT NULL DEFAULT 'v1',
  data_time TIMESTAMP(3) WITH TIME ZONE NOT NULL,
  _created_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _created_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  ),
  _updated_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _updated_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  )
);
ALTER TABLE student_profile ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_role_bypass_policy ON student_profile
  TO service_role USING (true);
CREATE POLICY "教师只读" ON student_profile
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);
CREATE INDEX idx_student_profile_class_key ON student_profile(class_key);
CREATE INDEX idx_student_profile_attention ON student_profile(needs_attention);

CREATE TABLE daily_learning_record (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  learning_key varchar(128) NOT NULL UNIQUE,
  learner_key varchar(128) NOT NULL,
  class_key varchar(128) NOT NULL,
  learning_date date NOT NULL,
  is_today boolean NOT NULL DEFAULT false,
  is_last_7_days boolean NOT NULL DEFAULT false,
  task_id text,
  task_name text,
  completion_result varchar(40) NOT NULL,
  completion_value integer NOT NULL DEFAULT 0,
  attempt_count integer NOT NULL DEFAULT 0,
  main_error text,
  ai_assistance_level integer NOT NULL DEFAULT 0,
  ai_assistance_label varchar(80) NOT NULL DEFAULT '未使用',
  used_skill_patch boolean NOT NULL DEFAULT false,
  knowledge_point text,
  stage_before varchar(40),
  stage_after varchar(40),
  daily_progress text,
  next_suggestion text,
  run_id varchar(128) NOT NULL,
  evidence_refs text,
  growth_document_url text,
  dashboard_url text,
  document_append_status varchar(32) NOT NULL DEFAULT 'pending',
  document_append_key varchar(160),
  data_time TIMESTAMP(3) WITH TIME ZONE NOT NULL,
  _created_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _created_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  ),
  _updated_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _updated_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  )
);
ALTER TABLE daily_learning_record ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_role_bypass_policy ON daily_learning_record
  TO service_role USING (true);
CREATE POLICY "教师只读" ON daily_learning_record
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);
CREATE INDEX idx_daily_learning_record_learner ON daily_learning_record(learner_key);
CREATE INDEX idx_daily_learning_record_date ON daily_learning_record(learning_date);
CREATE INDEX idx_daily_learning_record_run ON daily_learning_record(run_id);

CREATE TABLE evidence_summary (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  evidence_key varchar(128) NOT NULL UNIQUE,
  learner_key varchar(128) NOT NULL,
  learning_key varchar(128) NOT NULL,
  evidence_type varchar(80) NOT NULL,
  redacted_summary text NOT NULL,
  objective_facts text NOT NULL,
  run_id varchar(128) NOT NULL,
  evidence_url text,
  growth_document_url text,
  dashboard_url text,
  redaction_version varchar(32) NOT NULL DEFAULT 'v1',
  data_time TIMESTAMP(3) WITH TIME ZONE NOT NULL,
  _created_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _created_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  ),
  _updated_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _updated_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  )
);
ALTER TABLE evidence_summary ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_role_bypass_policy ON evidence_summary
  TO service_role USING (true);
CREATE POLICY "教师只读" ON evidence_summary
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);
CREATE INDEX idx_evidence_summary_learner ON evidence_summary(learner_key);
CREATE INDEX idx_evidence_summary_learning ON evidence_summary(learning_key);
CREATE INDEX idx_evidence_summary_run ON evidence_summary(run_id);

CREATE TABLE learning_center_config (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  config_key varchar(80) NOT NULL UNIQUE,
  config_value text NOT NULL,
  data_time TIMESTAMP(3) WITH TIME ZONE NOT NULL,
  _created_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _created_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  ),
  _updated_at TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
  _updated_by user_profile DEFAULT (
    CASE
      WHEN current_setting('app.user_id', TRUE) = '' THEN NULL
      ELSE concat('(', current_setting('app.user_id', TRUE), ')')::user_profile
    END
  )
);
ALTER TABLE learning_center_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY service_role_bypass_policy ON learning_center_config
  TO service_role USING (true);
CREATE POLICY "教师只读" ON learning_center_config
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);

COMMENT ON TABLE student_profile IS 'INT3 Feishu read-model cache; PostgreSQL Backend remains authoritative.';
COMMENT ON TABLE daily_learning_record IS 'INT3 Feishu read-model cache keyed by a Backend run.';
COMMENT ON TABLE evidence_summary IS 'INT3 redacted Evidence read-model cache; raw evidence is forbidden.';
COMMENT ON TABLE learning_center_config IS 'INT3 Feishu asset links and synchronization metadata.';

COMMIT;
