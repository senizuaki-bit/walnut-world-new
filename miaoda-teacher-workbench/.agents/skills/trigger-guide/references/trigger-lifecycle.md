# 触发器入参类型与代码示例

本 reference 承载 nestjs-react-fullstack 触发器 handler 的入参类型定义、完整代码示例与常见实现场景。先读主 [trigger-guide](../SKILL.md) 了解目录结构、绑定约束与配置要求。

## 触发器类型与入参

触发器类型（`triggerType`）有三种：

- `record_change`：记录变更触发器，**有入参**
- `cron`：定时触发器，**无入参**
- `webhook`：Webhook 触发器，**有入参**

```typescript
// 有入参触发器的入参类型（triggerType = 'record_change' 时）
interface TaskHandlerArgs {
  attributes: {
    trigger: string;
    triggerID?: string;
    triggerType: 'record_change' | 'cron' | 'webhook';
    instanceID: string;
    startAt?: number;
  };
  content: {
    input: string;  // JSON 字符串，根据 triggerType 解析为对应类型
  };
}

// record_change：input 解析后的数据结构
interface DataChangeEventInput {
  id: string;
  tenant_id: number;
  workspace: string;
  branch: string;
  app: string;
  table: string;
  type: 'INSERT' | 'UPDATE' | 'DELETE';
  timestamp: number;
  before?: Record<string, unknown>;  // DELETE 时有值，其他情况可能为空
  after?: Record<string, unknown>;   // INSERT/UPDATE 时有值，其他情况可能为空
  msg_id: string;
}

// webhook：input 解析后的数据结构
interface WebhookEvent {
  method: 'GET' | 'POST';
  url: string;                          // 完整 URL（含查询参数）
  host: string;                         // 不含查询参数的 URL
  path: string;                         // 路径部分
  query: Record<string, string[]>;      // URL 查询参数，值为字符串数组
  headers: Record<string, string[]>;    // 请求头，值为字符串数组
  body: string;                         // 请求体，JSON 字符串
  meta: {
    timestamp: number;                  // 请求时间戳（秒）
    traceID: string;                    // 追踪 ID
    env: 'development' | 'online';      // 环境：开发/线上
  };
}
```

`DataChangeEventInput.type` 只定义 `INSERT`、`UPDATE`、`DELETE`，不包含 `UPSERT`。

## 代码示例

根据触发器创建后确定的任务名字（应用内唯一），编写并绑定到对应的方法上。使用模板已有的 `@lark-apaas/fullstack-nestjs-core` 聚合入口导入 `Automation` / `BindTrigger`，不要求项目再感知底层 trigger 包。具体代码示例如下：

```typescript
// 文件名：demo.automation.ts
import { Logger } from '@nestjs/common';
// 必须导入
import { Automation, BindTrigger } from '@lark-apaas/fullstack-nestjs-core';

/**
 * 示例自动化任务服务
 * 使用 @Automation 装饰器标记 class，@BindTrigger 绑定具体 function
 */
@Automation()
export class DemoAutomationTasksService {
  // 务必使用 logger 打印日志
  private readonly logger = new Logger(DemoAutomationTasksService.name);

  @BindTrigger('triggerName1')
  // 任务对应具体的实现
  async helloWorld() {
    this.logger.log('执行 Hello World 任务');
    // do logic
    // return logic result or throw Error
  }

  @BindTrigger('triggerName2')
  // 任务对应具体的实现
  async sendNotification() {
    this.logger.log('开始发送通知');
    // do logic
    this.logger.log('通知发送完成');
    // return logic result or throw Error
  }

  @BindTrigger('recordChangeTrigger')
  // 记录变更任务（triggerType = 'record_change'）
  async handleDataChange(event: TaskHandlerArgs) {
    // 1. 校验并解析 input
    const input = event.content.input;
    if (typeof input !== 'string') {
      this.logger.error('input 类型错误');
      return;
    }

    let eventData: DataChangeEventInput;
    try {
      eventData = JSON.parse(input);
    } catch (error) {
      this.logger.error('JSON 解析失败', error);
      return;
    }

    // 2. 根据操作类型获取数据：INSERT/UPDATE 用 after，DELETE 用 before
    const record = eventData.after || eventData.before;
    if (!record) {
      this.logger.error('记录数据为空');
      return;
    }
    this.logger.log(`处理 ${eventData.type} 事件，记录ID: ${record.id}`);
    // do logic
    // return logic result or throw Error
  }

  @BindTrigger('webhookTrigger')
  // Webhook 任务（triggerType = 'webhook'）
  async handleWebhook(event: TaskHandlerArgs) {
    // 1. 校验并解析 input
    const input = event.content.input;
    if (typeof input !== 'string') {
      this.logger.error('input 类型错误');
      return;
    }

    let webhookEvent: WebhookEvent;
    try {
      webhookEvent = JSON.parse(input);
    } catch (error) {
      this.logger.error('JSON 解析失败', error);
      return;
    }

    // 2. 获取请求信息
    const { method, path, query, headers, body } = webhookEvent;
    this.logger.log(`处理 Webhook 请求：${method} ${path}`);

    // 3. 按需解析 body（body 本身也是 JSON 字符串）
    // const bodyData = JSON.parse(body);

    // do logic
    // return logic result or throw Error
  }
}
```

## 技术实现路径参考

以下是一些常见需求的推荐实现路径，帮助你在平台能力限制下找到合理的技术方案。

### 场景一：用户需要管理页面控制定时任务的启停

平台侧不支持通过 API 动态启停触发器。推荐方案：**平台定时触发器始终保持开启，在任务执行时查询数据库中的开关状态，决定是否真正执行业务逻辑。**

实现步骤：

1. 在数据库中建一张配置表（或复用已有配置表），存储任务开关状态
2. 前端管理页面提供开关操作，修改数据库中的状态
3. 定时任务触发时，先查询开关状态，关闭则直接跳过

```typescript
@Automation()
export class ReportAutomationService {
  private readonly logger = new Logger(ReportAutomationService.name);

  constructor(private readonly configService: ConfigService) {}

  @BindTrigger('dailyReportTrigger')
  async generateDailyReport() {
    // 1. 先查询任务开关状态
    const config = await this.configService.getTaskConfig('dailyReport');
    if (!config?.enabled) {
      this.logger.log('每日报告任务已被管理员关闭，跳过执行');
      return;
    }

    // 2. 开关开启，执行实际业务逻辑
    this.logger.log('开始生成每日报告');
    // do logic
  }
}
```

### 场景二：定时任务需要将结果通知给特定用户

自动化任务执行时无法获取当前用户上下文。推荐方案：**在数据库中预存需要通知的用户 ID，任务执行时从数据库查询目标用户，再调用飞书插件发送通知。**

```typescript
@Automation()
export class NotifyAutomationService {
  private readonly logger = new Logger(NotifyAutomationService.name);

  constructor(
    private readonly userConfigService: UserConfigService,
  ) {}

  @BindTrigger('weeklyDigestTrigger')
  async sendWeeklyDigest() {
    // 1. 从数据库查询订阅了周报的用户列表
    const subscribers = await this.userConfigService.getSubscribers('weeklyDigest');
    if (!subscribers.length) {
      this.logger.log('无订阅用户，跳过发送');
      return;
    }

    // 2. 生成周报内容
    const reportContent = await this.buildWeeklyReport();

    // 3. 逐个发送通知
    for (const user of subscribers) {
      // 调用插件发送飞书消息
      this.logger.log(`已发送周报给用户: ${user.userId}`);
    }
  }
}
```

### 场景三：记录变更触发器需要做防抖/去重

高频数据变更场景下，同一条记录可能短时间内触发多次。推荐方案：**利用数据库记录最近一次处理时间戳，对比 event 时间戳进行去重。**

```typescript
@BindTrigger('orderStatusChange')
async handleOrderChange(event: TaskHandlerArgs) {
  const eventData: DataChangeEventInput = JSON.parse(event.content.input);
  const record = eventData.after;
  if (!record) return;

  const orderId = record.id as string;

  // 查询上次处理时间，跳过短时间内的重复事件
  const lastProcessed = await this.orderService.getLastProcessedTime(orderId);
  if (lastProcessed && eventData.timestamp - lastProcessed < 5000) {
    this.logger.log(`订单 ${orderId} 短时间内重复触发，跳过`);
    return;
  }

  // 记录本次处理时间并执行业务逻辑
  await this.orderService.updateLastProcessedTime(orderId, eventData.timestamp);
  this.logger.log(`处理订单状态变更: ${orderId}`);
  // do logic
}
```

### 场景四：用户需要自定义定时任务的触发时间

平台侧的 cron 表达式在触发器创建后无法由用户动态修改。推荐方案：**平台设置一个固定的高频定时器（如每 30 分钟执行一次），在任务执行时从数据库读取用户配置的触发时间，判断当前是否命中再决定是否执行。**

实现步骤：

1. 平台侧创建一个每 30 分钟执行的 cron 触发器（最小粒度）
2. 数据库中存储用户配置的期望执行时间（如 `"09:00"`、`"每周一 14:00"` 等）
3. 前端管理页面提供时间配置界面，用户可随时修改
4. 每次触发时，读取配置并判断当前时间是否匹配，不匹配则跳过

```typescript
@Automation()
export class ScheduleAutomationService {
  private readonly logger = new Logger(ScheduleAutomationService.name);

  constructor(private readonly scheduleConfigService: ScheduleConfigService) {}

  @BindTrigger('fixedIntervalTrigger') // 平台侧固定每 30 分钟触发
  async checkAndExecuteTasks() {
    // 1. 查询所有用户配置的定时任务
    const tasks = await this.scheduleConfigService.getAllActiveTasks();

    const now = new Date();
    for (const task of tasks) {
      // 2. 判断当前时间是否命中用户配置的执行时间
      if (!this.isTimeMatched(now, task.scheduledTime)) {
        continue;
      }

      // 3. 命中则执行对应业务逻辑
      this.logger.log(`执行任务: ${task.name}, 配置时间: ${task.scheduledTime}`);
      await this.executeTask(task);
    }
  }

  private isTimeMatched(now: Date, scheduledTime: string): boolean {
    // 将当前时间取到半小时精度，与用户配置的时间比较
    // 例如 scheduledTime = "09:00"，当前 08:46~09:15 之间的某次触发即命中
    const [hour, minute] = scheduledTime.split(':').map(Number);
    return now.getHours() === hour && now.getMinutes() === minute;
  }
}
```

> 注意：由于平台最小调度间隔为 30 分钟，用户可配置的时间精度也应限制为 30 分钟的整数倍（如 `09:00`、`09:30`），前端做好校验提示。
