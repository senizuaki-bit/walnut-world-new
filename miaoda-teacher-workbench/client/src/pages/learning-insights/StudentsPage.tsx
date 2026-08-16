import React from 'react';

import { getStudents } from '@client/src/api/learning-insights';
import { Badge } from '@/components/ui/badge';
import type {
  StudentListResponse,
  StudentSummary,
} from '@shared/api.interface';

import {
  EmptyState,
  ErrorState,
  LearningInsightsShell,
  LoadingState,
} from './LearningInsightsShell';
import StudentCard from './StudentCard';
import { useLearningInsightsData } from './use-learning-insights-data';

const StudentsPage: React.FC = () => {
  const {
    data,
    error,
    loading,
    refetch,
  }: {
    data: StudentListResponse | null;
    error: string | null;
    loading: boolean;
    refetch: () => void;
  } = useLearningInsightsData<StudentListResponse>(getStudents);

  if (loading) {
    return (
      <LearningInsightsShell
        title="学生列表"
        description="正在读取统一学生档案。"
      >
        <LoadingState />
      </LearningInsightsShell>
    );
  }

  if (error || !data) {
    return (
      <LearningInsightsShell
        title="学生列表"
        description="查看每位学生的最新知识点阶段与学习支持情况。"
      >
        <ErrorState message={error ?? '暂无数据'} onRetry={refetch} />
      </LearningInsightsShell>
    );
  }

  return (
    <LearningInsightsShell
      title="学生列表"
      description="每个学生仅对应一份长期成长档案，列表事实来自真实学习投影。"
      actions={<Badge variant="secondary">共 {data.total} 名学生</Badge>}
    >
      {data.items.length === 0 ? (
        <EmptyState
          title="暂无学生档案"
          description="完成 Backend 到妙搭投影同步后，学生会出现在这里。"
        />
      ) : (
        <div
          className="grid gap-4 md:grid-cols-2 xl:grid-cols-3"
          data-ai-section-type="card-list"
        >
          {data.items.map(
            (student: StudentSummary): React.ReactNode => (
              <StudentCard key={student.learnerKey} student={student} />
            ),
          )}
        </div>
      )}
    </LearningInsightsShell>
  );
};

export default StudentsPage;
