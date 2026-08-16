export type MasteryStage =
  | '未观察'
  | '初现'
  | '发展中'
  | '熟练'
  | '需复习'
  | '暂无数据';

export interface LearningCenterLinks {
  baseUrl: string;
  dashboardUrl: string;
  templateUrl: string;
  lastSyncedAt: string | null;
}

export interface StudentSummary {
  learnerKey: string;
  learnerAlias: string;
  classKey: string;
  currentConcept: string;
  masteryStage: MasteryStage;
  aiAssistanceLevel: number;
  aiAssistanceLabel: string;
  skillPatchCount: number;
  lastActiveAt: string | null;
  activeToday: boolean;
  needsAttention: boolean;
  attentionReason: string;
  growthDocumentUrl: string;
  templateVersion: 'v1';
  dataTime: string;
}

export interface LearningRecordSummary {
  learningKey: string;
  learnerKey: string;
  classKey: string;
  learningDate: string;
  taskId: string;
  taskName: string;
  completionResult: string;
  completionValue: number;
  attemptCount: number;
  mainError: string;
  aiAssistanceLevel: number;
  aiAssistanceLabel: string;
  usedSkillPatch: boolean;
  knowledgePoint: string;
  stageBefore: MasteryStage;
  stageAfter: MasteryStage;
  dailyProgress: string;
  nextSuggestion: string;
  runId: string;
  evidenceRefs: string[];
  growthDocumentUrl: string;
  dashboardUrl: string;
  dataTime: string;
}

export interface EvidenceSummary {
  evidenceKey: string;
  learnerKey: string;
  learningKey: string;
  evidenceType: string;
  redactedSummary: string;
  objectiveFacts: string;
  runId: string;
  evidenceUrl: string;
  growthDocumentUrl: string;
  dashboardUrl: string;
  redactionVersion: 'v1';
  dataTime: string;
}

export interface NamedCount {
  name: string;
  count: number;
}

export interface DailyTrendPoint {
  date: string;
  activeStudents: number;
  taskCount: number;
  completionRate: number;
}

export interface LearningOverviewMetrics {
  todayActiveStudents: number;
  taskCompletionRate: number;
  averageAttempts: number;
  aiAssistance: NamedCount[];
  skillPatchUsage: NamedCount[];
  masteryDistribution: NamedCount[];
  highFrequencyErrors: NamedCount[];
  attentionStudents: StudentSummary[];
  last7Days: DailyTrendPoint[];
}

export interface LearningOverviewResponse {
  metrics: LearningOverviewMetrics;
  links: LearningCenterLinks;
  dataTime: string | null;
}

export interface StudentListResponse {
  items: StudentSummary[];
  total: number;
}

export interface StudentDetailResponse {
  student: StudentSummary;
  records: LearningRecordSummary[];
  evidence: EvidenceSummary[];
}

export interface LearningRecordListResponse {
  items: LearningRecordSummary[];
  total: number;
}
