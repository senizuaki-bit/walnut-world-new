/* eslint-disable */
/** auto generated, do not edit */
import { sql } from 'drizzle-orm';
import { boolean, date, index, integer, pgTable, text, uniqueIndex, uuid, varchar, customType } from "drizzle-orm/pg-core"

export const customTimestamptz = customType<{
  data: Date;
  driverData: string;
  config: { precision?: number };
}>({
  dataType(config) {
    const precision = typeof config?.precision !== 'undefined'
      ? ` (${config.precision})`
      : '';
    return `timestamptz${precision}`;
  },
  toDriver(value: Date | string | number) {
    if (value == null) return value as any;
    if (typeof value === 'number') return new Date(value).toISOString();
    if (typeof value === 'string') return value;
    if (value instanceof Date) return value.toISOString();
    throw new Error('Invalid timestamp value');
  },
  fromDriver(value: string | Date): Date {
    if (value instanceof Date) return value;
    return new Date(value);
  },
});

export const userProfile = customType<{
  data: string;
  driverData: string;
}>({
  dataType() {
    return 'user_profile';
  },
  toDriver(value: string) {
    return sql`ROW(${value})::user_profile`;
  },
  fromDriver(value: string) {
    const [userId] = value.slice(1, -1).split(',');
    return userId.trim();
  },
});

export type FileAttachment = {
  bucket_id: string;
  file_path: string;
};

export const fileAttachment = customType<{
  data: FileAttachment;
  driverData: string;
}>({
  dataType() {
    return 'file_attachment';
  },
  toDriver(value: FileAttachment) {
    return sql`ROW(${value.bucket_id},${value.file_path})::file_attachment`;
  },
  fromDriver(value: string): FileAttachment {
    const [bucketId, filePath] = value.slice(1, -1).split(',');
    return { bucket_id: bucketId.trim(), file_path: filePath.trim() };
  },
});

export function escapeLiteral(str: string): string {
  return "'" + str.replace(/'/g, "''") + "'";
}

export const userProfileArray = customType<{
  data: string[];
  driverData: string;
}>({
  dataType() {
    return 'user_profile[]';
  },
  toDriver(value: string[]) {
    if (!value || value.length === 0) {
      return sql`'{}'::user_profile[]`;
    }
    const elements = value.map(id => `ROW(${escapeLiteral(id)})::user_profile`).join(',');
    return sql.raw(`ARRAY[${elements}]::user_profile[]`);
  },
  fromDriver(value: string): string[] {
    if (!value || value === '{}') return [];
    const inner = value.slice(1, -1);
    const matches = inner.match(/\([^)]*\)/g) || [];
    return matches.map(m => m.slice(1, -1).split(',')[0].trim());
  },
});

export const fileAttachmentArray = customType<{
  data: FileAttachment[];
  driverData: string;
}>({
  dataType() {
    return 'file_attachment[]';
  },
  toDriver(value: FileAttachment[]) {
    if (!value || value.length === 0) {
      return sql`'{}'::file_attachment[]`;
    }
    const elements = value.map(f =>
      `ROW(${escapeLiteral(f.bucket_id)},${escapeLiteral(f.file_path)})::file_attachment`
    ).join(',');
    return sql.raw(`ARRAY[${elements}]::file_attachment[]`);
  },
  fromDriver(value: string): FileAttachment[] {
    if (!value || value === '{}') return [];
    const inner = value.slice(1, -1);
    const matches = inner.match(/\([^)]*\)/g) || [];
    return matches.map(m => {
      const [bucketId, filePath] = m.slice(1, -1).split(',');
      return { bucket_id: bucketId.trim(), file_path: filePath.trim() };
    });
  },
});

export const learningCenterConfig = pgTable("learning_center_config", {
  id: uuid("id").primaryKey().defaultRandom(),
  configKey: varchar("config_key", { length: 80 }).notNull().unique(),
  configValue: text("config_value").notNull(),
  dataTime: customTimestamptz("data_time", { precision: 3 }).notNull(),
  // System field: Creation time (auto-filled, do not modify)
  createdAt: customTimestamptz("_created_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Creator (auto-filled, do not modify)
  createdBy: userProfile("_created_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
  // System field: Update time (auto-filled, do not modify)
  updatedAt: customTimestamptz("_updated_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Updater (auto-filled, do not modify)
  updatedBy: userProfile("_updated_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
}, (table) => [
  uniqueIndex("learning_center_config_config_key_key").on(table.configKey),
]);

export const evidenceSummary = pgTable("evidence_summary", {
  id: uuid("id").primaryKey().defaultRandom(),
  evidenceKey: varchar("evidence_key", { length: 128 }).notNull().unique(),
  learnerKey: varchar("learner_key", { length: 128 }).notNull(),
  learningKey: varchar("learning_key", { length: 128 }).notNull(),
  evidenceType: varchar("evidence_type", { length: 80 }).notNull(),
  redactedSummary: text("redacted_summary").notNull(),
  objectiveFacts: text("objective_facts").notNull(),
  runId: varchar("run_id", { length: 128 }).notNull(),
  evidenceUrl: text("evidence_url"),
  growthDocumentUrl: text("growth_document_url"),
  dashboardUrl: text("dashboard_url"),
  redactionVersion: varchar("redaction_version", { length: 32 }).notNull().default('v1'),
  dataTime: customTimestamptz("data_time", { precision: 3 }).notNull(),
  // System field: Creation time (auto-filled, do not modify)
  createdAt: customTimestamptz("_created_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Creator (auto-filled, do not modify)
  createdBy: userProfile("_created_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
  // System field: Update time (auto-filled, do not modify)
  updatedAt: customTimestamptz("_updated_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Updater (auto-filled, do not modify)
  updatedBy: userProfile("_updated_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
}, (table) => [
  uniqueIndex("evidence_summary_evidence_key_key").on(table.evidenceKey),
  index("idx_evidence_summary_learner").on(table.learnerKey),
  index("idx_evidence_summary_learning").on(table.learningKey),
  index("idx_evidence_summary_run").on(table.runId),
]);

export const dailyLearningRecord = pgTable("daily_learning_record", {
  id: uuid("id").primaryKey().defaultRandom(),
  learningKey: varchar("learning_key", { length: 128 }).notNull().unique(),
  learnerKey: varchar("learner_key", { length: 128 }).notNull(),
  classKey: varchar("class_key", { length: 128 }).notNull(),
  learningDate: date("learning_date").notNull(),
  isToday: boolean("is_today").notNull().default(false),
  isLast7Days: boolean("is_last_7_days").notNull().default(false),
  taskId: text("task_id"),
  taskName: text("task_name"),
  completionResult: varchar("completion_result", { length: 40 }).notNull(),
  completionValue: integer("completion_value").notNull().default(0),
  attemptCount: integer("attempt_count").notNull().default(0),
  mainError: text("main_error"),
  aiAssistanceLevel: integer("ai_assistance_level").notNull().default(0),
  aiAssistanceLabel: varchar("ai_assistance_label", { length: 80 }).notNull().default('未使用'),
  usedSkillPatch: boolean("used_skill_patch").notNull().default(false),
  knowledgePoint: text("knowledge_point"),
  stageBefore: varchar("stage_before", { length: 40 }),
  stageAfter: varchar("stage_after", { length: 40 }),
  dailyProgress: text("daily_progress"),
  nextSuggestion: text("next_suggestion"),
  runId: varchar("run_id", { length: 128 }).notNull(),
  evidenceRefs: text("evidence_refs"),
  growthDocumentUrl: text("growth_document_url"),
  dashboardUrl: text("dashboard_url"),
  documentAppendStatus: varchar("document_append_status", { length: 32 }).notNull().default('pending'),
  documentAppendKey: varchar("document_append_key", { length: 160 }),
  dataTime: customTimestamptz("data_time", { precision: 3 }).notNull(),
  // System field: Creation time (auto-filled, do not modify)
  createdAt: customTimestamptz("_created_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Creator (auto-filled, do not modify)
  createdBy: userProfile("_created_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
  // System field: Update time (auto-filled, do not modify)
  updatedAt: customTimestamptz("_updated_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Updater (auto-filled, do not modify)
  updatedBy: userProfile("_updated_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
}, (table) => [
  uniqueIndex("daily_learning_record_learning_key_key").on(table.learningKey),
  index("idx_daily_learning_record_learner").on(table.learnerKey),
  index("idx_daily_learning_record_date").on(table.learningDate),
  index("idx_daily_learning_record_run").on(table.runId),
]);

export const studentProfile = pgTable("student_profile", {
  id: uuid("id").primaryKey().defaultRandom(),
  learnerKey: varchar("learner_key", { length: 128 }).notNull().unique(),
  learnerAlias: varchar("learner_alias", { length: 80 }).notNull(),
  classKey: varchar("class_key", { length: 128 }).notNull(),
  currentConcept: text("current_concept"),
  masteryStage: varchar("mastery_stage", { length: 40 }),
  aiAssistanceLevel: integer("ai_assistance_level").notNull().default(0),
  aiAssistanceLabel: varchar("ai_assistance_label", { length: 80 }).notNull().default('未使用'),
  skillPatchCount: integer("skill_patch_count").notNull().default(0),
  lastActiveAt: customTimestamptz("last_active_at", { precision: 3 }),
  activeToday: boolean("active_today").notNull().default(false),
  needsAttention: boolean("needs_attention").notNull().default(false),
  attentionReason: text("attention_reason"),
  growthDocumentUrl: text("growth_document_url"),
  templateVersion: varchar("template_version", { length: 16 }).notNull().default('v1'),
  dataTime: customTimestamptz("data_time", { precision: 3 }).notNull(),
  // System field: Creation time (auto-filled, do not modify)
  createdAt: customTimestamptz("_created_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Creator (auto-filled, do not modify)
  createdBy: userProfile("_created_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
  // System field: Update time (auto-filled, do not modify)
  updatedAt: customTimestamptz("_updated_at", { precision: 3 }).notNull().default(sql`CURRENT_TIMESTAMP`),
  // System field: Updater (auto-filled, do not modify)
  updatedBy: userProfile("_updated_by").default(sql`CASE
    WHEN (current_setting('app.user_id'::text, true) = ''::text) THEN NULL`),
}, (table) => [
  uniqueIndex("student_profile_learner_key_key").on(table.learnerKey),
  index("idx_student_profile_class_key").on(table.classKey),
  index("idx_student_profile_attention").on(table.needsAttention),
]);

// table aliases
export const dailyLearningRecordTable = dailyLearningRecord;
export const evidenceSummaryTable = evidenceSummary;
export const learningCenterConfigTable = learningCenterConfig;
export const studentProfileTable = studentProfile;
