import { NotFoundException } from '@nestjs/common';
import type { PostgresJsDatabase } from '@lark-apaas/fullstack-nestjs-core';

import { LearningInsightsService } from './learning-insights.service';

interface FakeQuery extends PromiseLike<unknown[]> {
  from: (...args: unknown[]) => FakeQuery;
  limit: (...args: unknown[]) => FakeQuery;
  orderBy: (...args: unknown[]) => FakeQuery;
  where: (...args: unknown[]) => FakeQuery;
}

const queuedDatabase = (results: unknown[][]): PostgresJsDatabase => {
  let index = 0;
  return {
    select: jest.fn((): FakeQuery => {
      const rows = results[index++] ?? [];
      const query: FakeQuery = {
        from: (): FakeQuery => query,
        limit: (): FakeQuery => query,
        orderBy: (): FakeQuery => query,
        where: (): FakeQuery => query,
        then: (onFulfilled, onRejected) =>
          Promise.resolve(rows).then(onFulfilled, onRejected),
      };
      return query;
    }),
  } as unknown as PostgresJsDatabase;
};

const student = (overrides: Record<string, unknown> = {}) => ({
  learnerKey: 'fsp_student_01',
  learnerAlias: '学生-01',
  classKey: 'class_cpp_01',
  currentConcept: '循环边界',
  masteryStage: '发展中',
  aiAssistanceLevel: 1,
  aiAssistanceLabel: '提示',
  skillPatchCount: 1,
  lastActiveAt: new Date('2026-08-17T10:00:00.000Z'),
  activeToday: true,
  needsAttention: true,
  attentionReason: '连续边界错误',
  growthDocumentUrl: 'https://example.com/growth/01',
  templateVersion: 'v1',
  dataTime: new Date('2026-08-17T10:00:00.000Z'),
  ...overrides,
});

const record = (overrides: Record<string, unknown> = {}) => ({
  learningKey: 'learning_01',
  learnerKey: 'fsp_student_01',
  classKey: 'class_cpp_01',
  learningDate: '2026-08-17',
  isToday: true,
  isLast7Days: true,
  taskId: 'task_watering',
  taskName: '循环浇水',
  completionResult: '完成',
  completionValue: 1,
  attemptCount: 1,
  mainError: '边界错误',
  aiAssistanceLevel: 1,
  aiAssistanceLabel: '提示',
  usedSkillPatch: false,
  knowledgePoint: 'for 循环',
  stageBefore: '初现',
  stageAfter: '发展中',
  dailyProgress: '能够完成主路径',
  nextSuggestion: '练习边界条件',
  runId: 'run_real_01',
  evidenceRefs: '["evidence_01", " evidence_02 "]',
  growthDocumentUrl: 'https://example.com/growth/01',
  dashboardUrl: 'https://example.com/dashboard',
  documentAppendStatus: 'appended',
  documentAppendKey: 'append_01',
  dataTime: new Date('2026-08-17T10:30:00.000Z'),
  ...overrides,
});

const evidence = (overrides: Record<string, unknown> = {}) => ({
  evidenceKey: 'evidence_01',
  learnerKey: 'fsp_student_01',
  learningKey: 'learning_01',
  evidenceType: 'run_result',
  redactedSummary: '任务运行完成，隐私字段已脱敏',
  objectiveFacts: 'completion_value=1',
  runId: 'run_real_01',
  evidenceUrl: 'https://example.com/evidence/01',
  growthDocumentUrl: 'https://example.com/growth/01',
  dashboardUrl: 'https://example.com/dashboard',
  redactionVersion: 'v1',
  dataTime: new Date('2026-08-17T12:00:00.000Z'),
  ...overrides,
});

describe('LearningInsightsService', () => {
  beforeAll(() => {
    jest.useFakeTimers();
    jest.setSystemTime(new Date('2026-08-17T12:30:00.000Z'));
  });

  afterAll(() => {
    jest.useRealTimers();
  });

  it('computes the overview from projection rows', async () => {
    const profiles = [
      student(),
      student({
        learnerKey: 'fsp_student_02',
        learnerAlias: '学生-02',
        masteryStage: null,
        activeToday: false,
        needsAttention: false,
      }),
    ];
    const records = [
      record(),
      record({
        learningKey: 'learning_02',
        learnerKey: 'fsp_student_02',
        completionResult: '未完成',
        completionValue: 0,
        attemptCount: 4,
        aiAssistanceLabel: '未使用',
        usedSkillPatch: true,
      }),
    ];
    const configs = [
      {
        configKey: 'base_url',
        configValue: 'https://example.com/base',
        dataTime: new Date('2026-08-17T09:00:00.000Z'),
      },
      {
        configKey: 'dashboard_url',
        configValue: 'https://example.com/dashboard',
        dataTime: new Date('2026-08-17T09:00:00.000Z'),
      },
      {
        configKey: 'template_url',
        configValue: 'https://example.com/template',
        dataTime: new Date('2026-08-17T09:00:00.000Z'),
      },
      {
        configKey: 'last_synced_at',
        configValue: '2026-08-17T12:00:00.000Z',
        dataTime: new Date('2026-08-17T12:00:00.000Z'),
      },
    ];
    const service = new LearningInsightsService(
      queuedDatabase([profiles, records, configs, [evidence()]]),
    );

    const overview = await service.getOverview();

    expect(overview.metrics.todayActiveStudents).toBe(1);
    expect(overview.metrics.taskCompletionRate).toBe(50);
    expect(overview.metrics.averageAttempts).toBe(2.5);
    expect(overview.metrics.aiAssistance).toEqual(
      expect.arrayContaining([
        { name: '提示', count: 1 },
        { name: '未使用', count: 1 },
      ]),
    );
    expect(overview.metrics.skillPatchUsage).toEqual(
      expect.arrayContaining([
        { name: '已使用', count: 1 },
        { name: '未使用', count: 1 },
      ]),
    );
    expect(overview.metrics.masteryDistribution).toEqual(
      expect.arrayContaining([
        { name: '发展中', count: 1 },
        { name: '暂无数据', count: 1 },
      ]),
    );
    expect(overview.metrics.attentionStudents).toHaveLength(1);
    expect(overview.metrics.last7Days).toHaveLength(7);
    expect(overview.metrics.last7Days.at(-1)).toEqual({
      date: '2026-08-17',
      activeStudents: 2,
      taskCount: 2,
      completionRate: 50,
    });
    expect(overview.links).toEqual({
      baseUrl: 'https://example.com/base',
      dashboardUrl: 'https://example.com/dashboard',
      templateUrl: 'https://example.com/template',
      lastSyncedAt: '2026-08-17T12:00:00.000Z',
    });
    expect(overview.dataTime).toBe('2026-08-17T12:00:00.000Z');
  });

  it('returns a detail response with only redacted evidence fields', async () => {
    const service = new LearningInsightsService(
      queuedDatabase([[student()], [record()], [evidence()]]),
    );

    const detail = await service.getStudentDetail('fsp_student_01');

    expect(detail.student.learnerKey).toBe('fsp_student_01');
    expect(detail.records[0]).toMatchObject({
      runId: 'run_real_01',
      evidenceRefs: ['evidence_01', 'evidence_02'],
      stageBefore: '初现',
      stageAfter: '发展中',
    });
    expect(detail.evidence[0]).toEqual({
      evidenceKey: 'evidence_01',
      learnerKey: 'fsp_student_01',
      learningKey: 'learning_01',
      evidenceType: 'run_result',
      redactedSummary: '任务运行完成，隐私字段已脱敏',
      objectiveFacts: 'completion_value=1',
      runId: 'run_real_01',
      evidenceUrl: 'https://example.com/evidence/01',
      growthDocumentUrl: 'https://example.com/growth/01',
      dashboardUrl: 'https://example.com/dashboard',
      redactionVersion: 'v1',
      dataTime: '2026-08-17T12:00:00.000Z',
    });
  });

  it('returns not found for an unknown opaque learner key', async () => {
    const service = new LearningInsightsService(queuedDatabase([[]]));

    await expect(service.getStudentDetail('missing')).rejects.toBeInstanceOf(
      NotFoundException,
    );
  });
});
