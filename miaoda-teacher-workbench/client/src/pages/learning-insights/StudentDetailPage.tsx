import React, { useCallback } from 'react';
import {
  ArrowLeft,
  BookOpen,
  Bot,
  Clock3,
  FileCheck2,
  ShieldCheck,
  Wrench,
} from 'lucide-react';
import { Link, useParams } from 'react-router-dom';

import { getStudentDetail } from '@client/src/api/learning-insights';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import type {
  EvidenceSummary,
  LearningRecordSummary,
  StudentDetailResponse,
} from '@shared/api.interface';

import LearningRecordCard from './LearningRecordCard';
import {
  EmptyState,
  ErrorState,
  ExternalLinkButton,
  formatDateTime,
  LearningInsightsShell,
  LoadingState,
} from './LearningInsightsShell';
import { masteryVariant } from './StudentCard';
import { useLearningInsightsData } from './use-learning-insights-data';

const StudentDetailPage: React.FC = () => {
  const { learnerKey }: { learnerKey?: string } = useParams<{
    learnerKey: string;
  }>();
  const loadStudent = useCallback((): Promise<StudentDetailResponse> => {
    if (!learnerKey) {
      return Promise.reject(new Error('学生标识缺失'));
    }
    return getStudentDetail(learnerKey);
  }, [learnerKey]);
  const {
    data,
    error,
    loading,
    refetch,
  }: {
    data: StudentDetailResponse | null;
    error: string | null;
    loading: boolean;
    refetch: () => void;
  } = useLearningInsightsData<StudentDetailResponse>(loadStudent);

  if (loading) {
    return (
      <LearningInsightsShell
        title="学生详情"
        description="正在读取统一成长档案与学习证据摘要。"
      >
        <LoadingState />
      </LearningInsightsShell>
    );
  }

  if (error || !data) {
    return (
      <LearningInsightsShell
        title="学生详情"
        description="查看学生的真实任务记录、阶段变化和脱敏 Evidence。"
      >
        <ErrorState message={error ?? '暂无数据'} onRetry={refetch} />
      </LearningInsightsShell>
    );
  }

  const dashboardUrl: string =
    data.records[0]?.dashboardUrl ??
    data.evidence[0]?.dashboardUrl ??
    '暂无数据';

  return (
    <LearningInsightsShell
      title={data.student.learnerAlias}
      description="统一学生成长档案：只呈现 Backend 已同步的客观事实、固定建议与脱敏证据。"
      actions={
        <>
          <Button variant="ghost" asChild data-ai-section-type="button">
            <Link to="/students">
              <ArrowLeft className="size-4" aria-hidden="true" />
              返回学生列表
            </Link>
          </Button>
          <ExternalLinkButton
            href={data.student.growthDocumentUrl}
            label="打开成长档案"
            variant="default"
          />
          <ExternalLinkButton href={dashboardUrl} label="打开班级看板" />
        </>
      }
    >
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-xl">学习档案概览</CardTitle>
              <CardDescription className="mt-1">
                班级 {data.student.classKey} · 模板版本{' '}
                {data.student.templateVersion}
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={masteryVariant(data.student.masteryStage)}>
                {data.student.masteryStage}
              </Badge>
              {data.student.needsAttention ? (
                <Badge variant="destructive">需要关注</Badge>
              ) : (
                <Badge variant="outline">状态稳定</Badge>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-lg bg-muted/60 p-4">
              <BookOpen className="size-4 text-primary" aria-hidden="true" />
              <p className="mt-3 text-xs text-muted-foreground">当前知识点</p>
              <p className="mt-1 break-words text-sm font-semibold">
                {data.student.currentConcept}
              </p>
            </div>
            <div className="rounded-lg bg-muted/60 p-4">
              <Bot className="size-4 text-primary" aria-hidden="true" />
              <p className="mt-3 text-xs text-muted-foreground">AI 辅助</p>
              <p className="mt-1 text-sm font-semibold">
                {data.student.aiAssistanceLabel} ·{' '}
                {data.student.aiAssistanceLevel}
              </p>
            </div>
            <div className="rounded-lg bg-muted/60 p-4">
              <Wrench className="size-4 text-primary" aria-hidden="true" />
              <p className="mt-3 text-xs text-muted-foreground">Skill Patch</p>
              <p className="mt-1 text-sm font-semibold">
                累计 {data.student.skillPatchCount} 次
              </p>
            </div>
            <div className="rounded-lg bg-muted/60 p-4">
              <Clock3 className="size-4 text-primary" aria-hidden="true" />
              <p className="mt-3 text-xs text-muted-foreground">最近学习</p>
              <p className="mt-1 text-sm font-semibold">
                {formatDateTime(data.student.lastActiveAt)}
              </p>
            </div>
          </div>
          {data.student.needsAttention ? (
            <p className="rounded-lg bg-destructive/10 px-4 py-3 text-sm leading-6 text-destructive">
              关注原因：{data.student.attentionReason}
            </p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            档案数据时间：{formatDateTime(data.student.dataTime)}
          </p>
        </CardContent>
      </Card>

      <section className="mt-8" aria-labelledby="student-records-title">
        <div className="mb-4">
          <h2 id="student-records-title" className="text-xl font-semibold">
            每日学习记录
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            任务结果、尝试次数、错误和阶段变化均来自同一次真实学习记录。
          </p>
        </div>
        {data.records.length === 0 ? (
          <EmptyState
            title="暂无学习记录"
            description="该学生尚未同步可展示的真实任务记录。"
          />
        ) : (
          <div className="space-y-4" data-ai-section-type="card-list">
            {data.records.map(
              (record: LearningRecordSummary): React.ReactNode => (
                <LearningRecordCard key={record.learningKey} record={record} />
              ),
            )}
          </div>
        )}
      </section>

      <section className="mt-8" aria-labelledby="student-evidence-title">
        <div className="mb-4">
          <h2
            id="student-evidence-title"
            className="flex items-center gap-2 text-xl font-semibold"
          >
            <ShieldCheck className="size-5 text-primary" aria-hidden="true" />
            脱敏 Evidence
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            此处只读取 evidence_summary
            中已脱敏的摘要与客观事实，不展示原始代码或聊天。
          </p>
        </div>
        {data.evidence.length === 0 ? (
          <EmptyState
            title="暂无 Evidence 摘要"
            description="当前没有与该学生关联的脱敏 Evidence 投影。"
          />
        ) : (
          <div
            className="grid gap-4 lg:grid-cols-2"
            data-ai-section-type="card-list"
          >
            {data.evidence.map(
              (evidence: EvidenceSummary): React.ReactNode => (
                <Card
                  key={evidence.evidenceKey}
                  id={`evidence-${evidence.evidenceKey}`}
                  className="flex h-full flex-col"
                >
                  <CardHeader>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <CardTitle className="flex items-center gap-2 text-lg">
                          <FileCheck2
                            className="size-5 text-primary"
                            aria-hidden="true"
                          />
                          {evidence.evidenceType}
                        </CardTitle>
                        <CardDescription className="mt-1 break-words">
                          Run：{evidence.runId}
                        </CardDescription>
                      </div>
                      <Badge variant="outline">
                        脱敏版本 {evidence.redactionVersion}
                      </Badge>
                    </div>
                  </CardHeader>
                  <CardContent className="flex-1 space-y-4">
                    <div>
                      <p className="text-sm font-semibold">脱敏摘要</p>
                      <p className="mt-2 break-words text-sm leading-6 text-muted-foreground">
                        {evidence.redactedSummary}
                      </p>
                    </div>
                    <div className="rounded-lg bg-muted/60 p-4">
                      <p className="text-sm font-semibold">客观事实</p>
                      <p className="mt-2 break-words text-sm leading-6 text-muted-foreground">
                        {evidence.objectiveFacts}
                      </p>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      数据时间：{formatDateTime(evidence.dataTime)}
                    </p>
                    <div className="flex flex-wrap gap-2">
                      <ExternalLinkButton
                        href={evidence.evidenceUrl}
                        label="查看 Evidence"
                      />
                      <ExternalLinkButton
                        href={evidence.growthDocumentUrl}
                        label="打开成长档案"
                      />
                    </div>
                  </CardContent>
                </Card>
              ),
            )}
          </div>
        )}
      </section>
    </LearningInsightsShell>
  );
};

export default StudentDetailPage;
