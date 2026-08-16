import { Controller, Get, Param } from '@nestjs/common';
import { CanRole } from '@lark-apaas/fullstack-nestjs-core';

import type {
  LearningOverviewResponse,
  LearningRecordListResponse,
  StudentDetailResponse,
  StudentListResponse,
} from '@shared/api.interface';

import { LearningInsightsService } from './learning-insights.service';

@Controller('api/learning-insights')
export class LearningInsightsController {
  constructor(
    private readonly learningInsightsService: LearningInsightsService,
  ) {}

  @CanRole(['walnut_teacher'])
  @Get('overview')
  getOverview(): Promise<LearningOverviewResponse> {
    return this.learningInsightsService.getOverview();
  }

  @CanRole(['walnut_teacher'])
  @Get('students')
  getStudents(): Promise<StudentListResponse> {
    return this.learningInsightsService.getStudents();
  }

  @CanRole(['walnut_teacher'])
  @Get('records')
  getRecords(): Promise<LearningRecordListResponse> {
    return this.learningInsightsService.getRecords();
  }

  @CanRole(['walnut_teacher'])
  @Get('students/:learnerKey')
  getStudentDetail(
    @Param('learnerKey') learnerKey: string,
  ): Promise<StudentDetailResponse> {
    return this.learningInsightsService.getStudentDetail(learnerKey);
  }
}
