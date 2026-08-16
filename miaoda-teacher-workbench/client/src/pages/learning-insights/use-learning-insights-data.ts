import { useCallback, useEffect, useState } from 'react';

interface LearningDataState<Response> {
  data: Response | null;
  error: string | null;
  loading: boolean;
  refetch: () => void;
}

const errorMessage = (error: unknown): string => {
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return '学习洞察加载失败，请稍后重试';
};

const useLearningInsightsData = <Response>(
  loader: () => Promise<Response>,
): LearningDataState<Response> => {
  const [data, setData] = useState<Response | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [revision, setRevision] = useState<number>(0);

  useEffect((): (() => void) => {
    let cancelled: boolean = false;

    const load = async (): Promise<void> => {
      setLoading(true);
      setError(null);
      try {
        const response: Response = await loader();
        if (!cancelled) {
          setData(response);
        }
      } catch (loadError: unknown) {
        if (!cancelled) {
          setData(null);
          setError(errorMessage(loadError));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void load();
    return (): void => {
      cancelled = true;
    };
  }, [loader, revision]);

  const refetch = useCallback((): void => {
    setRevision((current: number): number => current + 1);
  }, []);

  return { data, error, loading, refetch };
};

export { useLearningInsightsData };
