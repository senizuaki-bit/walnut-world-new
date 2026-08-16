import React from 'react';
import { ShieldX } from 'lucide-react';

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';

import { LearningInsightsShell } from './LearningInsightsShell';

const UnauthorizedPage: React.FC = () => (
  <LearningInsightsShell
    title="无法访问教师工作台"
    description="当前账号未通过教师角色校验。"
  >
    <Card className="mx-auto max-w-xl">
      <CardHeader className="items-center text-center">
        <div className="mb-2 flex size-12 items-center justify-center rounded-xl bg-destructive/10 text-destructive">
          <ShieldX className="size-6" aria-hidden="true" />
        </div>
        <CardTitle className="text-xl">需要 walnut_teacher 角色</CardTitle>
        <CardDescription>
          为保护学生学习数据，班级概览、学生档案、每日记录和 Evidence
          摘要仅向教师开放。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p className="rounded-lg bg-muted px-4 py-3 text-center text-sm text-muted-foreground">
          请联系应用管理员核对角色分配后重新进入。
        </p>
      </CardContent>
    </Card>
  </LearningInsightsShell>
);

export default UnauthorizedPage;
