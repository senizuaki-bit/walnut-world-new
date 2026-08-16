import React from 'react';
import {
  BookOpenCheck,
  ExternalLink,
  LayoutDashboard,
  ListChecks,
  RefreshCw,
  SearchX,
  Users,
} from 'lucide-react';
import { NavLink, type NavLinkRenderProps } from 'react-router-dom';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import { Skeleton } from '@/components/ui/skeleton';

interface LearningInsightsShellProps {
  title: string;
  description: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}

interface ExternalLinkButtonProps {
  href: string;
  label: string;
  variant?: 'default' | 'outline' | 'secondary' | 'ghost';
}

interface ErrorStateProps {
  message: string;
  onRetry: () => void;
}

interface EmptyStateProps {
  title: string;
  description: string;
}

const NAVIGATION_ITEMS: Array<{
  label: string;
  path: string;
  icon: React.ComponentType<{ className?: string }>;
  end: boolean;
}> = [
  { label: '班级概览', path: '/', icon: LayoutDashboard, end: true },
  { label: '学生列表', path: '/students', icon: Users, end: false },
  { label: '每日记录', path: '/records', icon: ListChecks, end: false },
];

const navigationClassName = ({ isActive }: NavLinkRenderProps): string =>
  [
    'inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium',
    'transition-colors',
    isActive
      ? 'bg-primary text-primary-foreground'
      : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
  ].join(' ');

const isUsableUrl = (href: string): boolean =>
  href !== '暂无数据' && /^https?:\/\//u.test(href);

const formatDateTime = (value: string | null): string => {
  if (!value || value === '暂无数据') {
    return '暂无数据';
  }
  const date: Date = new Date(value);
  if (!Number.isFinite(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
};

const formatDate = (value: string): string => {
  if (!value || value === '暂无数据') {
    return '暂无数据';
  }
  const date: Date = new Date(`${value}T00:00:00+08:00`);
  if (!Number.isFinite(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    weekday: 'short',
  }).format(date);
};

const LearningInsightsShell: React.FC<LearningInsightsShellProps> = ({
  title,
  description,
  actions,
  children,
}) => {
  return (
    <div className="min-h-screen bg-muted/40 text-foreground">
      <header className="sticky top-0 z-20 border-b bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-col gap-3 px-4 py-4 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:gap-6 lg:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
              <BookOpenCheck className="size-5" aria-hidden="true" />
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">核桃世界</p>
              <p className="truncate text-xs text-muted-foreground">
                教师学习数据中心 · 竞赛版
              </p>
            </div>
          </div>
          <nav
            className="flex flex-wrap items-center gap-2"
            aria-label="教师工作台导航"
          >
            {NAVIGATION_ITEMS.map(
              (item: (typeof NAVIGATION_ITEMS)[number]): React.ReactNode => {
                const Icon: React.ComponentType<{ className?: string }> =
                  item.icon;
                return (
                  <NavLink
                    key={item.path}
                    to={item.path}
                    end={item.end}
                    className={navigationClassName}
                  >
                    <Icon className="size-4" />
                    {item.label}
                  </NavLink>
                );
              },
            )}
          </nav>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
        <div className="mb-7 flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between lg:gap-8">
          <div className="max-w-3xl">
            <p className="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-primary">
              Learning insights
            </p>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              {title}
            </h1>
            <p className="mt-2 text-sm leading-6 text-muted-foreground sm:text-base">
              {description}
            </p>
          </div>
          {actions ? (
            <div className="flex flex-wrap items-center gap-2">{actions}</div>
          ) : null}
        </div>
        {children}
      </main>
    </div>
  );
};

const ExternalLinkButton: React.FC<ExternalLinkButtonProps> = ({
  href,
  label,
  variant = 'outline',
}) => {
  if (!isUsableUrl(href)) {
    return (
      <Button
        type="button"
        variant={variant}
        disabled
        data-ai-section-type="button"
      >
        {label} · 暂无数据
      </Button>
    );
  }

  return (
    <Button variant={variant} asChild data-ai-section-type="button">
      <a href={href} target="_blank" rel="noreferrer">
        {label}
        <ExternalLink className="size-4" aria-hidden="true" />
      </a>
    </Button>
  );
};

const LoadingState: React.FC = () => (
  <div
    className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4"
    aria-label="正在加载"
  >
    {Array.from(
      { length: 4 },
      (_value: unknown, index: number): number => index,
    ).map(
      (index: number): React.ReactNode => (
        <Skeleton key={index} className="h-32 w-full" />
      ),
    )}
  </div>
);

const ErrorState: React.FC<ErrorStateProps> = ({ message, onRetry }) => (
  <Alert variant="destructive">
    <SearchX className="size-4" aria-hidden="true" />
    <AlertTitle>数据加载失败</AlertTitle>
    <AlertDescription>
      <p>{message}</p>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={onRetry}
        data-ai-section-type="button"
      >
        <RefreshCw className="size-4" aria-hidden="true" />
        重新加载
      </Button>
    </AlertDescription>
  </Alert>
);

const EmptyState: React.FC<EmptyStateProps> = ({ title, description }) => (
  <Empty className="border bg-background">
    <EmptyHeader>
      <EmptyMedia variant="icon">
        <SearchX className="size-5" aria-hidden="true" />
      </EmptyMedia>
      <EmptyTitle>{title}</EmptyTitle>
      <EmptyDescription>{description}</EmptyDescription>
    </EmptyHeader>
  </Empty>
);

export {
  EmptyState,
  ErrorState,
  ExternalLinkButton,
  formatDate,
  formatDateTime,
  isUsableUrl,
  LearningInsightsShell,
  LoadingState,
};
