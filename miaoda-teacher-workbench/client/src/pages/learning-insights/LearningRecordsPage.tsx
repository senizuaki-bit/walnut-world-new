import React from 'react';

import { getRecords } from '@client/src/api/learning-insights';
import { Badge } from '@/components/ui/badge';
import type {
  LearningRecordListResponse,
  LearningRecordSummary,
} from '@shared/api.interface';

import LearningRecordCard from './LearningRecordCard';
import {
  EmptyState,
  ErrorState,
  LearningInsightsShell,
  LoadingState,
} from './LearningInsightsShell';
import { useLearningInsightsData } from './use-learning-insights-data';

const LearningRecordsPage: React.FC = () => {
  const {
    data,
    error,
    loading,
    refetch,
  }: {
    data: LearningRecordListResponse | null;
    error: string | null;
    loading: boolean;
    refetch: () => void;
  } = useLearningInsightsData<LearningRecordListResponse>(getRecords);

  if (loading) {
    return (
      <LearningInsightsShell
        title="每日学习记录"
        description="正在读取真实 Run 对应的学习投影。"
      >
        <LoadingState />
      </LearningInsightsShell>
    );
  }

  if (error || !data) {
    return (
      <LearningInsightsShell
        title="每日学习记录"
        description="按最新日期查看任务结果、尝试、错误、辅助和阶段变化。"
      >
        <ErrorState message={error ?? '暂无数据'} onRetry={refetch} />
      </LearningInsightsShell>
    );
  }

  return (
    <LearningInsightsShell
      title="每日学习记录"
      description="每条记录使用稳定 learningKey 关联同一次 Run、成长档案和脱敏 Evidence。"
      actions={<Badge variant="secondary">共 {data.total} 条记录</Badge>}
    >
      {data.items.length === 0 ? (
        <EmptyState
          title="暂无每日学习记录"
          description="学生完成真实任务并同步后，记录会按日期出现在这里。"
        />
      ) : (
        <div className="space-y-4" data-ai-section-type="card-list">
          {data.items.map(
            (record: LearningRecordSummary): React.ReactNode => (
              <LearningRecordCard
                key={record.learningKey}
                record={record}
                showStudentLink
              />
            ),
          )}
        </div>
      )}
    </LearningInsightsShell>
  );
};

export default LearningRecordsPage;
