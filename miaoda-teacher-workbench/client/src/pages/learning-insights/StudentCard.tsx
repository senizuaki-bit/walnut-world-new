import React from 'react';
import {
  ArrowRight,
  BookOpen,
  BrainCircuit,
  Clock3,
  FileText,
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
import type { MasteryStage, StudentSummary } from '@shared/api.interface';

import { formatDateTime, isUsableUrl } from './LearningInsightsShell';

interface StudentCardProps {
  student: StudentSummary;
  compact?: boolean;
}

const masteryVariant = (
  stage: MasteryStage,
): 'default' | 'secondary' | 'outline' | 'destructive' => {
  if (stage === '熟练') {
    return 'default';
  }
  if (stage === '需复习') {
    return 'destructive';
  }
  if (stage === '发展中' || stage === '初现') {
    return 'secondary';
  }
  return 'outline';
};

const StudentCard: React.FC<StudentCardProps> = ({
  student,
  compact = false,
}) => (
  <Card className="flex h-full flex-col overflow-hidden">
    <CardHeader className="gap-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <CardTitle className="truncate text-lg">
            {student.learnerAlias}
          </CardTitle>
          <p className="mt-1 truncate text-xs text-muted-foreground">
            班级：{student.classKey}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={masteryVariant(student.masteryStage)}>
            {student.masteryStage}
          </Badge>
          {student.needsAttention ? (
            <Badge variant="destructive">需要关注</Badge>
          ) : null}
        </div>
      </div>
    </CardHeader>
    <CardContent className="flex-1 space-y-3 text-sm">
      <div className="flex items-start gap-2">
        <BookOpen
          className="mt-0.5 size-4 shrink-0 text-primary"
          aria-hidden="true"
        />
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">当前知识点</p>
          <p className="break-words font-medium">{student.currentConcept}</p>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex items-start gap-2">
          <BrainCircuit
            className="mt-0.5 size-4 shrink-0 text-primary"
            aria-hidden="true"
          />
          <div>
            <p className="text-xs text-muted-foreground">AI 辅助</p>
            <p className="font-medium">{student.aiAssistanceLabel}</p>
          </div>
        </div>
        <div className="flex items-start gap-2">
          <Wrench
            className="mt-0.5 size-4 shrink-0 text-primary"
            aria-hidden="true"
          />
          <div>
            <p className="text-xs text-muted-foreground">Skill Patch</p>
            <p className="font-medium">{student.skillPatchCount} 次</p>
          </div>
        </div>
      </div>
      {!compact ? (
        <div className="flex items-start gap-2 border-t pt-3">
          <Clock3
            className="mt-0.5 size-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
          <div>
            <p className="text-xs text-muted-foreground">最近学习</p>
            <p>{formatDateTime(student.lastActiveAt)}</p>
          </div>
        </div>
      ) : null}
      {student.needsAttention ? (
        <p className="rounded-lg bg-destructive/10 px-3 py-2 text-xs leading-5 text-destructive">
          {student.attentionReason}
        </p>
      ) : null}
    </CardContent>
    <CardFooter className="flex flex-wrap gap-2">
      <Button
        variant="secondary"
        size="sm"
        asChild
        data-ai-section-type="button"
      >
        <Link to={`/students/${encodeURIComponent(student.learnerKey)}`}>
          查看详情
          <ArrowRight className="size-4" aria-hidden="true" />
        </Link>
      </Button>
      {isUsableUrl(student.growthDocumentUrl) ? (
        <Button
          variant="outline"
          size="sm"
          asChild
          data-ai-section-type="button"
        >
          <a href={student.growthDocumentUrl} target="_blank" rel="noreferrer">
            <FileText className="size-4" aria-hidden="true" />
            成长档案
          </a>
        </Button>
      ) : null}
    </CardFooter>
  </Card>
);

export { masteryVariant };
export default StudentCard;
