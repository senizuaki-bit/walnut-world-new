import { logger } from '@lark-apaas/client-toolkit/logger';
import { axiosForBackend } from '@lark-apaas/client-toolkit/utils/getAxiosForBackend';
import { isAxiosError, type AxiosResponse } from 'axios';

import type {
  LearningOverviewResponse,
  LearningRecordListResponse,
  StudentDetailResponse,
  StudentListResponse,
} from '@shared/api.interface';

const API_PREFIX: string = '/api/learning-insights';
const FORBIDDEN_MESSAGE: string = '无查看权限，请联系管理员分配教师角色';
const REQUEST_FAILED_MESSAGE: string = '学习洞察加载失败，请稍后重试';

class LearningInsightsForbiddenError extends Error {
  constructor() {
    super(FORBIDDEN_MESSAGE);
    this.name = 'LearningInsightsForbiddenError';
  }
}

class LearningInsightsRequestError extends Error {
  readonly cause: unknown;

  constructor(cause: unknown) {
    super(REQUEST_FAILED_MESSAGE);
    this.name = 'LearningInsightsRequestError';
    this.cause = cause;
  }
}

const requestLearningInsights = async <Response>(
  url: string,
): Promise<Response> => {
  try {
    const response: AxiosResponse<Response> = await axiosForBackend<Response>({
      url,
      method: 'GET',
      meta: { autoJumpToLogin: false },
    });

    if (response.status === 403) {
      throw new LearningInsightsForbiddenError();
    }
    if (response.status < 200 || response.status >= 300) {
      throw new Error(`请求失败，状态码 ${response.status}`);
    }

    return response.data;
  } catch (error: unknown) {
    if (error instanceof LearningInsightsForbiddenError) {
      logger.warn(FORBIDDEN_MESSAGE, { url });
      throw error;
    }
    if (isAxiosError(error) && error.response?.status === 403) {
      logger.warn(FORBIDDEN_MESSAGE, { url });
      throw new LearningInsightsForbiddenError();
    }

    logger.error(REQUEST_FAILED_MESSAGE, { url, error });
    throw new LearningInsightsRequestError(error);
  }
};

const getOverview = (): Promise<LearningOverviewResponse> =>
  requestLearningInsights<LearningOverviewResponse>(`${API_PREFIX}/overview`);

const getStudents = (): Promise<StudentListResponse> =>
  requestLearningInsights<StudentListResponse>(`${API_PREFIX}/students`);

const getStudentDetail = (learnerKey: string): Promise<StudentDetailResponse> =>
  requestLearningInsights<StudentDetailResponse>(
    `${API_PREFIX}/students/${encodeURIComponent(learnerKey)}`,
  );

const getRecords = (): Promise<LearningRecordListResponse> =>
  requestLearningInsights<LearningRecordListResponse>(`${API_PREFIX}/records`);

export { getOverview, getRecords, getStudentDetail, getStudents };
