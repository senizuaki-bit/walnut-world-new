import { Module } from '@nestjs/common';

import { LearningInsightsController } from './learning-insights.controller';
import { LearningInsightsService } from './learning-insights.service';

@Module({
  controllers: [LearningInsightsController],
  providers: [LearningInsightsService],
})
export class LearningInsightsModule {}
