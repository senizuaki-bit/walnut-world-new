import React from 'react';
import {
  ArrowRight,
  Bot,
  CalendarDays,
  CircleAlert,
  Lightbulb,
  Target,
  Wrench,
} from 'lucide-react';
import { Link } from 'react-router-dom';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import type { LearningRecordSummary } from '@shared/api.interface';

import {
  ExternalLinkButton,
  formatDate,
  formatDateTime,
} from './LearningInsightsShell';
import { masteryVariant } from './StudentCard';

interface LearningRecordCardProps {
  record: LearningRecordSummary;
  showStudentLink?: boolean;
}

const completionVariant = (
  completionValue: number,
): 'default' | 'destructive' =>
  completionValue > 0 ? 'default' : 'destructive';

const LearningRecordCard: React.FC<LearningRecordCardProps> = ({
  record,
  showStudentLink = false,
}) => (
  <Card className="overflow-hidden">
    <CardHeader className="gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            <CalendarDays className="size-4" aria-hidden="true" />
            {formatDate(record.learningDate)}
          </p>
          <CardTitle className="mt-2 break-words text-lg">
            {record.taskName}
          </CardTitle>
          <p className="mt-1 break-words text-xs text-muted-foreground">
            任务标识：{record.taskId}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={completionVariant(record.completionValue)}>
            {record.completionResult}
          </Badge>
          <Badge variant={record.usedSkillPatch ? 'secondary' : 'outline'}>
            {record.usedSkillPatch
              ? '已使用 Skill Patch'
              : '未使用 Skill Patch'}
          </Badge>
        </div>
      </div>
    </CardHeader>
    <CardContent className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-lg bg-muted/60 p-3">
          <p className="text-xs text-muted-foreground">尝试次数</p>
          <p className="mt-1 text-lg font-semibold">{record.attemptCount}</p>
        </div>
        <div className="rounded-lg bg-muted/60 p-3">
          <p className="text-xs text-muted-foreground">AI 辅助</p>
          <p className="mt-1 text-sm font-semibold">
            {record.aiAssistanceLabel} · {record.aiAssistanceLevel}
          </p>
        </div>
        <div className="rounded-lg bg-muted/60 p-3">
          <p className="text-xs text-muted-foreground">知识点</p>
          <p className="mt-1 break-words text-sm font-semibold">
            {record.knowledgePoint}
          </p>
        </div>
        <div className="rounded-lg bg-muted/60 p-3">
          <p className="text-xs text-muted-foreground">阶段变化</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Badge variant={masteryVariant(record.stageBefore)}>
              {record.stageBefore}
            </Badge>
            <ArrowRight
              className="size-3.5 text-muted-foreground"
              aria-hidden="true"
            />
            <Badge variant={masteryVariant(record.stageAfter)}>
              {record.stageAfter}
            </Badge>
          </div>
        </div>
      </div>

      <div className="grid gap-3 lg:grid-cols-3">
        <div className="rounded-lg border p-4">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <CircleAlert
              className="size-4 text-destructive"
              aria-hidden="true"
            />
            主要错误
          </p>
          <p className="mt-2 break-words text-sm leading-6 text-muted-foreground">
            {record.mainError}
          </p>
        </div>
        <div className="rounded-lg border p-4">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <Target className="size-4 text-primary" aria-hidden="true" />
            今日进步
          </p>
          <p className="mt-2 break-words text-sm leading-6 text-muted-foreground">
            {record.dailyProgress}
          </p>
        </div>
        <div className="rounded-lg border p-4">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <Lightbulb className="size-4 text-primary" aria-hidden="true" />
            下一步建议
          </p>
          <p className="mt-2 break-words text-sm leading-6 text-muted-foreground">
            {record.nextSuggestion}
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-2 rounded-lg bg-muted/40 px-4 py-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between sm:gap-5">
        <div className="flex flex-wrap items-center gap-2">
          <Bot className="size-4" aria-hidden="true" />
          <span>Run：{record.runId}</span>
          <Wrench className="ml-1 size-4" aria-hidden="true" />
          <span>
            Evidence：
            {record.evidenceRefs.length > 0
              ? record.evidenceRefs.join('、')
              : '暂无数据'}
          </span>
        </div>
        <span>数据时间：{formatDateTime(record.dataTime)}</span>
      </div>
    </CardContent>
    <CardFooter className="flex flex-wrap gap-2">
      {showStudentLink ? (
        <Button variant="secondary" asChild data-ai-section-type="button">
          <Link to={`/students/${encodeURIComponent(record.learnerKey)}`}>
            查看学生
            <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
        </Button>
      ) : null}
      <ExternalLinkButton
        href={record.growthDocumentUrl}
        label="打开成长档案"
      />
      <ExternalLinkButton href={record.dashboardUrl} label="打开班级看板" />
    </CardFooter>
  </Card>
);

export default LearningRecordCard;
