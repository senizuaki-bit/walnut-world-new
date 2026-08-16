import { Inject, Injectable, NotFoundException } from '@nestjs/common';
import {
  DRIZZLE_DATABASE,
  type PostgresJsDatabase,
} from '@lark-apaas/fullstack-nestjs-core';
import { asc, desc, eq } from 'drizzle-orm';

import {
  dailyLearningRecord,
  evidenceSummary,
  learningCenterConfig,
  studentProfile,
} from '@server/database/schema';
import type {
  DailyTrendPoint,
  EvidenceSummary,
  LearningCenterLinks,
  LearningOverviewResponse,
  LearningRecordListResponse,
  LearningRecordSummary,
  MasteryStage,
  NamedCount,
  StudentDetailResponse,
  StudentListResponse,
  StudentSummary,
} from '@shared/api.interface';

type StudentProfileRow = typeof studentProfile.$inferSelect;
type DailyLearningRecordRow = typeof dailyLearningRecord.$inferSelect;
type EvidenceSummaryRow = typeof evidenceSummary.$inferSelect;
type LearningCenterConfigRow = typeof learningCenterConfig.$inferSelect;

const EMPTY_VALUE: string = '暂无数据';
const ONE_DAY_MS: number = 24 * 60 * 60 * 1000;
const BASE_URL_CONFIG_KEY: string = 'base_url';
const DASHBOARD_URL_CONFIG_KEY: string = 'dashboard_url';
const TEMPLATE_URL_CONFIG_KEY: string = 'template_url';
const LAST_SYNCED_AT_CONFIG_KEY: string = 'last_synced_at';

@Injectable()
export class LearningInsightsService {
  constructor(
    @Inject(DRIZZLE_DATABASE)
    private readonly db: PostgresJsDatabase,
  ) {}

  async getOverview(): Promise<LearningOverviewResponse> {
    const profiles: StudentProfileRow[] = await this.db
      .select()
      .from(studentProfile)
      .orderBy(asc(studentProfile.learnerAlias));
    const records: DailyLearningRecordRow[] = await this.db
      .select()
      .from(dailyLearningRecord)
      .orderBy(desc(dailyLearningRecord.learningDate));
    const configs: LearningCenterConfigRow[] = await this.db
      .select()
      .from(learningCenterConfig);
    const latestEvidence: EvidenceSummaryRow[] = await this.db
      .select()
      .from(evidenceSummary)
      .orderBy(desc(evidenceSummary.dataTime))
      .limit(1);

    const todayRecords: DailyLearningRecordRow[] = records.filter(
      (record: DailyLearningRecordRow): boolean => record.isToday,
    );
    const attentionStudents: StudentSummary[] = profiles
      .filter((profile: StudentProfileRow): boolean => profile.needsAttention)
      .map(
        (profile: StudentProfileRow): StudentSummary =>
          this.mapStudent(profile),
      );
    const dataTimes: Array<Date | null> = [
      ...profiles.map((profile: StudentProfileRow): Date => profile.dataTime),
      ...records.map((record: DailyLearningRecordRow): Date => record.dataTime),
      ...configs.map(
        (config: LearningCenterConfigRow): Date => config.dataTime,
      ),
      latestEvidence[0]?.dataTime ?? null,
    ];

    return {
      metrics: {
        todayActiveStudents: profiles.filter(
          (profile: StudentProfileRow): boolean => profile.activeToday,
        ).length,
        taskCompletionRate: this.completionRate(todayRecords),
        averageAttempts: this.averageAttempts(todayRecords),
        aiAssistance: this.countNames(
          todayRecords.map((record: DailyLearningRecordRow): string =>
            this.requiredText(record.aiAssistanceLabel),
          ),
        ),
        skillPatchUsage: this.countNames(
          todayRecords.map((record: DailyLearningRecordRow): string =>
            record.usedSkillPatch ? '已使用' : '未使用',
          ),
        ),
        masteryDistribution: this.countNames(
          profiles.map((profile: StudentProfileRow): string =>
            this.masteryStage(profile.masteryStage),
          ),
        ),
        highFrequencyErrors: this.countNames(
          todayRecords
            .map((record: DailyLearningRecordRow): string | null =>
              this.optionalText(record.mainError),
            )
            .filter((error: string | null): error is string => error !== null),
        ),
        attentionStudents,
        last7Days: this.buildLast7Days(records),
      },
      links: this.buildLinks(configs),
      dataTime: this.latestIso(dataTimes),
    };
  }

  async getStudents(): Promise<StudentListResponse> {
    const profiles: StudentProfileRow[] = await this.db
      .select()
      .from(studentProfile)
      .orderBy(
        desc(studentProfile.needsAttention),
        desc(studentProfile.lastActiveAt),
        asc(studentProfile.learnerAlias),
      );
    const items: StudentSummary[] = profiles.map(
      (profile: StudentProfileRow): StudentSummary => this.mapStudent(profile),
    );

    return { items, total: items.length };
  }

  async getRecords(): Promise<LearningRecordListResponse> {
    const rows: DailyLearningRecordRow[] = await this.db
      .select()
      .from(dailyLearningRecord)
      .orderBy(
        desc(dailyLearningRecord.learningDate),
        desc(dailyLearningRecord.dataTime),
      );
    const items: LearningRecordSummary[] = rows.map(
      (row: DailyLearningRecordRow): LearningRecordSummary =>
        this.mapRecord(row),
    );

    return { items, total: items.length };
  }

  async getStudentDetail(learnerKey: string): Promise<StudentDetailResponse> {
    const profiles: StudentProfileRow[] = await this.db
      .select()
      .from(studentProfile)
      .where(eq(studentProfile.learnerKey, learnerKey))
      .limit(1);
    const profile: StudentProfileRow | undefined = profiles[0];

    if (!profile) {
      throw new NotFoundException('未找到学生档案');
    }

    const recordRows: DailyLearningRecordRow[] = await this.db
      .select()
      .from(dailyLearningRecord)
      .where(eq(dailyLearningRecord.learnerKey, learnerKey))
      .orderBy(
        desc(dailyLearningRecord.learningDate),
        desc(dailyLearningRecord.dataTime),
      );
    const evidenceRows: EvidenceSummaryRow[] = await this.db
      .select()
      .from(evidenceSummary)
      .where(eq(evidenceSummary.learnerKey, learnerKey))
      .orderBy(desc(evidenceSummary.dataTime));

    return {
      student: this.mapStudent(profile),
      records: recordRows.map(
        (row: DailyLearningRecordRow): LearningRecordSummary =>
          this.mapRecord(row),
      ),
      evidence: evidenceRows.map(
        (row: EvidenceSummaryRow): EvidenceSummary => this.mapEvidence(row),
      ),
    };
  }

  private mapStudent(row: StudentProfileRow): StudentSummary {
    return {
      learnerKey: this.requiredText(row.learnerKey),
      learnerAlias: this.requiredText(row.learnerAlias),
      classKey: this.requiredText(row.classKey),
      currentConcept: this.requiredText(row.currentConcept),
      masteryStage: this.masteryStage(row.masteryStage),
      aiAssistanceLevel: row.aiAssistanceLevel,
      aiAssistanceLabel: this.requiredText(row.aiAssistanceLabel),
      skillPatchCount: row.skillPatchCount,
      lastActiveAt: row.lastActiveAt?.toISOString() ?? null,
      activeToday: row.activeToday,
      needsAttention: row.needsAttention,
      attentionReason: this.requiredText(row.attentionReason),
      growthDocumentUrl: this.requiredText(row.growthDocumentUrl),
      templateVersion: 'v1',
      dataTime: row.dataTime.toISOString(),
    };
  }

  private mapRecord(row: DailyLearningRecordRow): LearningRecordSummary {
    return {
      learningKey: this.requiredText(row.learningKey),
      learnerKey: this.requiredText(row.learnerKey),
      classKey: this.requiredText(row.classKey),
      learningDate: this.requiredText(row.learningDate),
      taskId: this.requiredText(row.taskId),
      taskName: this.requiredText(row.taskName),
      completionResult: this.requiredText(row.completionResult),
      completionValue: row.completionValue,
      attemptCount: row.attemptCount,
      mainError: this.requiredText(row.mainError),
      aiAssistanceLevel: row.aiAssistanceLevel,
      aiAssistanceLabel: this.requiredText(row.aiAssistanceLabel),
      usedSkillPatch: row.usedSkillPatch,
      knowledgePoint: this.requiredText(row.knowledgePoint),
      stageBefore: this.masteryStage(row.stageBefore),
      stageAfter: this.masteryStage(row.stageAfter),
      dailyProgress: this.requiredText(row.dailyProgress),
      nextSuggestion: this.requiredText(row.nextSuggestion),
      runId: this.requiredText(row.runId),
      evidenceRefs: this.parseEvidenceRefs(row.evidenceRefs),
      growthDocumentUrl: this.requiredText(row.growthDocumentUrl),
      dashboardUrl: this.requiredText(row.dashboardUrl),
      dataTime: row.dataTime.toISOString(),
    };
  }

  private mapEvidence(row: EvidenceSummaryRow): EvidenceSummary {
    return {
      evidenceKey: this.requiredText(row.evidenceKey),
      learnerKey: this.requiredText(row.learnerKey),
      learningKey: this.requiredText(row.learningKey),
      evidenceType: this.requiredText(row.evidenceType),
      redactedSummary: this.requiredText(row.redactedSummary),
      objectiveFacts: this.requiredText(row.objectiveFacts),
      runId: this.requiredText(row.runId),
      evidenceUrl: this.requiredText(row.evidenceUrl),
      growthDocumentUrl: this.requiredText(row.growthDocumentUrl),
      dashboardUrl: this.requiredText(row.dashboardUrl),
      redactionVersion: 'v1',
      dataTime: row.dataTime.toISOString(),
    };
  }

  private buildLinks(configs: LearningCenterConfigRow[]): LearningCenterLinks {
    return {
      baseUrl: this.configValue(configs, BASE_URL_CONFIG_KEY) ?? EMPTY_VALUE,
      dashboardUrl:
        this.configValue(configs, DASHBOARD_URL_CONFIG_KEY) ?? EMPTY_VALUE,
      templateUrl:
        this.configValue(configs, TEMPLATE_URL_CONFIG_KEY) ?? EMPTY_VALUE,
      lastSyncedAt:
        this.configValue(configs, LAST_SYNCED_AT_CONFIG_KEY) ?? EMPTY_VALUE,
    };
  }

  private configValue(
    configs: LearningCenterConfigRow[],
    key: string,
  ): string | null {
    const row: LearningCenterConfigRow | undefined = configs.find(
      (config: LearningCenterConfigRow): boolean => config.configKey === key,
    );
    return this.optionalText(row?.configValue);
  }

  private buildLast7Days(records: DailyLearningRecordRow[]): DailyTrendPoint[] {
    const today: string = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date());
    const anchor: Date = new Date(`${today}T00:00:00.000Z`);
    const dates: string[] = Array.from(
      { length: 7 },
      (_value: unknown, index: number): string => {
        const date: Date = new Date(
          anchor.getTime() - (6 - index) * ONE_DAY_MS,
        );
        return date.toISOString().slice(0, 10);
      },
    );

    return dates.map((date: string): DailyTrendPoint => {
      const dailyRows: DailyLearningRecordRow[] = records.filter(
        (record: DailyLearningRecordRow): boolean =>
          record.learningDate === date,
      );
      const learners: Set<string> = new Set<string>(
        dailyRows.map(
          (record: DailyLearningRecordRow): string => record.learnerKey,
        ),
      );

      return {
        date,
        activeStudents: learners.size,
        taskCount: dailyRows.length,
        completionRate: this.completionRate(dailyRows),
      };
    });
  }

  private completionRate(records: DailyLearningRecordRow[]): number {
    if (records.length === 0) {
      return 0;
    }
    const completed: number = records.filter(
      (record: DailyLearningRecordRow): boolean => record.completionValue > 0,
    ).length;
    return this.roundOneDecimal((completed / records.length) * 100);
  }

  private averageAttempts(records: DailyLearningRecordRow[]): number {
    if (records.length === 0) {
      return 0;
    }
    const attempts: number = records.reduce(
      (total: number, record: DailyLearningRecordRow): number =>
        total + record.attemptCount,
      0,
    );
    return this.roundOneDecimal(attempts / records.length);
  }

  private countNames(names: string[]): NamedCount[] {
    const counts: Map<string, number> = new Map<string, number>();
    names.forEach((name: string): void => {
      counts.set(name, (counts.get(name) ?? 0) + 1);
    });

    return Array.from(
      counts.entries(),
      ([name, count]: [string, number]): NamedCount => ({ name, count }),
    ).sort(
      (left: NamedCount, right: NamedCount): number =>
        right.count - left.count ||
        left.name.localeCompare(right.name, 'zh-CN'),
    );
  }

  private parseEvidenceRefs(value: string | null): string[] {
    const normalized: string | null = this.optionalText(value);
    if (!normalized) {
      return [];
    }

    try {
      const parsed: unknown = JSON.parse(normalized);
      if (Array.isArray(parsed)) {
        return parsed
          .filter((item: unknown): item is string => typeof item === 'string')
          .map((item: string): string => item.trim())
          .filter((item: string): boolean => item.length > 0);
      }
    } catch {
      return normalized
        .split(/[,\n]/u)
        .map((item: string): string => item.trim())
        .filter((item: string): boolean => item.length > 0);
    }

    return [];
  }

  private masteryStage(value: string | null): MasteryStage {
    switch (value) {
      case '未观察':
      case '初现':
      case '发展中':
      case '熟练':
      case '需复习':
        return value;
      default:
        return '暂无数据';
    }
  }

  private requiredText(value: string | null | undefined): string {
    return this.optionalText(value) ?? EMPTY_VALUE;
  }

  private optionalText(value: string | null | undefined): string | null {
    if (!value) {
      return null;
    }
    const normalized: string = value.trim();
    return normalized.length > 0 ? normalized : null;
  }

  private latestIso(values: Array<Date | null>): string | null {
    const timestamps: number[] = values
      .filter((value: Date | null): value is Date => value !== null)
      .map((value: Date): number => value.getTime())
      .filter((value: number): boolean => Number.isFinite(value));
    if (timestamps.length === 0) {
      return null;
    }
    return new Date(Math.max(...timestamps)).toISOString();
  }

  private roundOneDecimal(value: number): number {
    return Math.round(value * 10) / 10;
  }
}
