import 'reflect-metadata';

import { NotFoundException } from '@nestjs/common';
import { Test } from '@nestjs/testing';
import type { INestApplication } from '@nestjs/common';
import type { AddressInfo } from 'node:net';
import { ROLES_KEY } from '@lark-apaas/nestjs-authzpaas';

import { GlobalExceptionFilter } from '@server/common/filters/exception.filter';
import { LearningInsightsController } from '@server/modules/learning-insights/learning-insights.controller';
import { LearningInsightsService } from '@server/modules/learning-insights/learning-insights.service';

const overview = {
  metrics: {
    todayActiveStudents: 1,
    taskCompletionRate: 100,
    averageAttempts: 2,
    aiAssistance: [{ name: '提示', count: 1 }],
    skillPatchUsage: [{ name: '未使用', count: 1 }],
    masteryDistribution: [{ name: '发展中', count: 1 }],
    highFrequencyErrors: [],
    attentionStudents: [],
    last7Days: [],
  },
  links: {
    baseUrl: 'https://example.com/base',
    dashboardUrl: 'https://example.com/dashboard',
    templateUrl: 'https://example.com/template',
    lastSyncedAt: '2026-08-17T12:00:00.000Z',
  },
  dataTime: '2026-08-17T12:00:00.000Z',
};

describe('LearningInsightsController HTTP', () => {
  let app: INestApplication;
  let baseUrl: string;

  const service = {
    getOverview: jest.fn(async () => overview),
    getStudents: jest.fn(async () => ({ items: [], total: 0 })),
    getRecords: jest.fn(async () => ({ items: [], total: 0 })),
    getStudentDetail: jest.fn(async (learnerKey: string) => {
      if (learnerKey === 'missing') {
        throw new NotFoundException('未找到学生档案');
      }
      return {
        student: { learnerKey },
        records: [],
        evidence: [],
      };
    }),
  };

  beforeAll(async () => {
    const moduleRef = await Test.createTestingModule({
      controllers: [LearningInsightsController],
      providers: [
        {
          provide: LearningInsightsService,
          useValue: service,
        },
      ],
    }).compile();

    app = moduleRef.createNestApplication();
    app.useGlobalFilters(new GlobalExceptionFilter());
    await app.listen(0, '127.0.0.1');
    const address = app.getHttpServer().address() as AddressInfo;
    baseUrl = `http://127.0.0.1:${address.port}`;
  });

  afterAll(async () => {
    await app.close();
  });

  it('exposes all four read-only API routes', async () => {
    const responses = await Promise.all([
      fetch(`${baseUrl}/api/learning-insights/overview`),
      fetch(`${baseUrl}/api/learning-insights/students`),
      fetch(`${baseUrl}/api/learning-insights/records`),
      fetch(`${baseUrl}/api/learning-insights/students/fsp_student_01`),
    ]);

    expect(responses.map((response) => response.status)).toEqual([
      200, 200, 200, 200,
    ]);
    await expect(responses[0].json()).resolves.toEqual(overview);
    await expect(responses[3].json()).resolves.toMatchObject({
      student: { learnerKey: 'fsp_student_01' },
      records: [],
      evidence: [],
    });
  });

  it('keeps walnut_teacher metadata on every business handler', () => {
    const handlers = [
      LearningInsightsController.prototype.getOverview,
      LearningInsightsController.prototype.getStudents,
      LearningInsightsController.prototype.getRecords,
      LearningInsightsController.prototype.getStudentDetail,
    ];

    for (const handler of handlers) {
      expect(Reflect.getMetadata(ROLES_KEY, handler)).toEqual({
        roles: ['walnut_teacher'],
      });
    }
  });

  it('returns the normalized error contract for a missing learner', async () => {
    const response = await fetch(
      `${baseUrl}/api/learning-insights/students/missing`,
    );

    expect(response.status).toBe(404);
    await expect(response.json()).resolves.toMatchObject({
      error: {
        code: 'NOT_FOUND',
        message: '未找到学生档案',
      },
    });
  });
});
