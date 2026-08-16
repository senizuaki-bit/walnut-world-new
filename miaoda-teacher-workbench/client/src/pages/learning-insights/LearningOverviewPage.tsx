import React from 'react';
import {
  Activity,
  Bot,
  CheckCircle2,
  Clock3,
  ExternalLink,
  GraduationCap,
  ShieldAlert,
  TrendingUp,
  Wrench,
  type LucideIcon,
} from 'lucide-react';

import { getOverview } from '@client/src/api/learning-insights';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import type {
  DailyTrendPoint,
  LearningOverviewResponse,
  NamedCount,
  StudentSummary,
} from '@shared/api.interface';

import {
  ExternalLinkButton,
  formatDate,
  formatDateTime,
  LearningInsightsShell,
  LoadingState,
  ErrorState,
} from './LearningInsightsShell';
import StudentCard from './StudentCard';
import { useLearningInsightsData } from './use-learning-insights-data';

interface MetricCardProps {
  label: string;
  value: string;
  hint: string;
  icon: LucideIcon;
}

interface NamedCountGroupProps {
  items: NamedCount[];
  emptyText: string;
}

const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  hint,
  icon: Icon,
}) => (
  <Card>
    <CardHeader className="flex-row items-center justify-between gap-3 space-y-0 pb-3">
      <div>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="mt-2 text-3xl">{value}</CardTitle>
      </div>
      <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
        <Icon className="size-5" aria-hidden="true" />
      </div>
    </CardHeader>
    <CardContent>
      <p className="text-xs text-muted-foreground">{hint}</p>
    </CardContent>
  </Card>
);

const NamedCountGroup: React.FC<NamedCountGroupProps> = ({
  items,
  emptyText,
}) => {
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyText}</p>;
  }

  return (
    <div className="flex flex-wrap gap-2">
      {items.map(
        (item: NamedCount): React.ReactNode => (
          <Badge key={item.name} variant="secondary" className="gap-2 py-1.5">
            <span className="max-w-56 truncate">{item.name}</span>
            <span className="rounded bg-background/80 px-1.5">
              {item.count}
            </span>
          </Badge>
        ),
      )}
    </div>
  );
};

const LearningOverviewPage: React.FC = () => {
  const {
    data,
    error,
    loading,
    refetch,
  }: {
    data: LearningOverviewResponse | null;
    error: string | null;
    loading: boolean;
    refetch: () => void;
  } = useLearningInsightsData<LearningOverviewResponse>(getOverview);

  if (loading) {
    return (
      <LearningInsightsShell
        title="班级学习概览"
        description="正在读取真实学习投影，请稍候。"
      >
        <LoadingState />
      </LearningInsightsShell>
    );
  }

  if (error || !data) {
    return (
      <LearningInsightsShell
        title="班级学习概览"
        description="集中查看学生进度、共性问题和近期学习趋势。"
      >
        <ErrorState message={error ?? '暂无数据'} onRetry={refetch} />
      </LearningInsightsShell>
    );
  }

  const metrics: LearningOverviewResponse['metrics'] = data.metrics;

  return (
    <LearningInsightsShell
      title="班级学习概览"
      description="基于 Backend 同步的真实学习投影，快速识别班级进展与教学关注点。"
      actions={
        <>
          <ExternalLinkButton
            href={data.links.baseUrl}
            label="打开学习洞察 Base"
          />
          <ExternalLinkButton
            href={data.links.templateUrl}
            label="查看档案母版"
          />
          <ExternalLinkButton
            href={data.links.dashboardUrl}
            label="打开班级看板"
            variant="default"
          />
        </>
      }
    >
      <div
        className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4"
        data-ai-section-type="card-stat"
      >
        <MetricCard
          label="今日活跃学生"
          value={String(metrics.todayActiveStudents)}
          hint="以最新同步的今日活跃标记统计"
          icon={Activity}
        />
        <MetricCard
          label="任务完成率"
          value={`${metrics.taskCompletionRate}%`}
          hint="今日真实学习记录中的完成任务占比"
          icon={CheckCircle2}
        />
        <MetricCard
          label="平均尝试次数"
          value={String(metrics.averageAttempts)}
          hint="今日每条学习记录的平均尝试次数"
          icon={Clock3}
        />
        <MetricCard
          label="需要关注"
          value={String(metrics.attentionStudents.length)}
          hint="由权威学习投影标记的关注学生"
          icon={ShieldAlert}
        />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Bot className="size-5 text-primary" aria-hidden="true" />
              AI 辅助情况
            </CardTitle>
            <CardDescription>
              仅统计 Backend 投影中已有的辅助标签。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <NamedCountGroup
              items={metrics.aiAssistance}
              emptyText="今日暂无 AI 辅助数据"
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Wrench className="size-5 text-primary" aria-hidden="true" />
              Skill Patch 使用
            </CardTitle>
            <CardDescription>
              区分今日记录是否使用过 Skill Patch。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <NamedCountGroup
              items={metrics.skillPatchUsage}
              emptyText="今日暂无 Skill Patch 数据"
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <GraduationCap
                className="size-5 text-primary"
                aria-hidden="true"
              />
              知识点阶段分布
            </CardTitle>
            <CardDescription>
              展示每位学生最新的知识点掌握阶段。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <NamedCountGroup
              items={metrics.masteryDistribution}
              emptyText="暂无知识点阶段数据"
            />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <ShieldAlert className="size-5 text-primary" aria-hidden="true" />
              高频错误
            </CardTitle>
            <CardDescription>按今日真实记录中的主要错误聚合。</CardDescription>
          </CardHeader>
          <CardContent>
            <NamedCountGroup
              items={metrics.highFrequencyErrors}
              emptyText="今日暂无主要错误数据"
            />
          </CardContent>
        </Card>
      </div>

      <section className="mt-8" aria-labelledby="attention-title">
        <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="attention-title" className="text-xl font-semibold">
              需要关注的学生
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              关注原因来自同步后的学生档案，不在工作台内二次推断。
            </p>
          </div>
        </div>
        {metrics.attentionStudents.length > 0 ? (
          <div
            className="grid gap-4 md:grid-cols-2 xl:grid-cols-3"
            data-ai-section-type="card-list"
          >
            {metrics.attentionStudents.map(
              (student: StudentSummary): React.ReactNode => (
                <StudentCard
                  key={student.learnerKey}
                  student={student}
                  compact
                />
              ),
            )}
          </div>
        ) : (
          <div className="rounded-xl border bg-background px-5 py-6 text-sm text-muted-foreground">
            暂无需要关注的学生
          </div>
        )}
      </section>

      <section className="mt-8" aria-labelledby="trend-title">
        <Card>
          <CardHeader>
            <CardTitle
              id="trend-title"
              className="flex items-center gap-2 text-xl"
            >
              <TrendingUp className="size-5 text-primary" aria-hidden="true" />
              最近 7 天学习趋势
            </CardTitle>
            <CardDescription>
              以每日卡片呈现活跃人数、任务数和完成率，不使用推测数据。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-7">
              {metrics.last7Days.map(
                (point: DailyTrendPoint): React.ReactNode => (
                  <div
                    key={point.date}
                    className="rounded-lg border bg-muted/30 p-3"
                  >
                    <p className="text-sm font-semibold">
                      {formatDate(point.date)}
                    </p>
                    <div className="mt-3 space-y-1.5 text-xs text-muted-foreground">
                      <p>活跃 {point.activeStudents} 人</p>
                      <p>任务 {point.taskCount} 个</p>
                      <p>完成率 {point.completionRate}%</p>
                    </div>
                  </div>
                ),
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      <div className="mt-6 flex flex-col gap-3 rounded-xl border bg-background px-5 py-4 text-sm sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div>
          <p className="font-medium">数据更新时间</p>
          <p className="text-muted-foreground">
            {formatDateTime(data.dataTime)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <ExternalLink className="size-4" aria-hidden="true" />
          <span>同步时间：{formatDateTime(data.links.lastSyncedAt)}</span>
        </div>
      </div>
    </LearningInsightsShell>
  );
};

export default LearningOverviewPage;
