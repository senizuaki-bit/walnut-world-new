BEGIN;

DROP POLICY IF EXISTS "修改全部数据" ON student_profile;
DROP POLICY IF EXISTS "修改本人数据" ON student_profile;
DROP POLICY IF EXISTS "查看全部数据" ON student_profile;
DROP POLICY IF EXISTS "教师只读" ON student_profile;
DROP POLICY IF EXISTS "修改全部数据" ON daily_learning_record;
DROP POLICY IF EXISTS "修改本人数据" ON daily_learning_record;
DROP POLICY IF EXISTS "查看全部数据" ON daily_learning_record;
DROP POLICY IF EXISTS "教师只读" ON daily_learning_record;
DROP POLICY IF EXISTS "修改全部数据" ON evidence_summary;
DROP POLICY IF EXISTS "修改本人数据" ON evidence_summary;
DROP POLICY IF EXISTS "查看全部数据" ON evidence_summary;
DROP POLICY IF EXISTS "教师只读" ON evidence_summary;
DROP POLICY IF EXISTS "修改全部数据" ON learning_center_config;
DROP POLICY IF EXISTS "修改本人数据" ON learning_center_config;
DROP POLICY IF EXISTS "查看全部数据" ON learning_center_config;
DROP POLICY IF EXISTS "教师只读" ON learning_center_config;

CREATE POLICY "教师只读" ON student_profile
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);
CREATE POLICY "教师只读" ON daily_learning_record
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);
CREATE POLICY "教师只读" ON evidence_summary
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);
CREATE POLICY "教师只读" ON learning_center_config
  AS PERMISSIVE FOR SELECT TO authenticated USING (true);

COMMIT;
