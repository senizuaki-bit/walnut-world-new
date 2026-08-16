---
name: trigger-guide
description: 自动化任务触发器代码开发指南，支持 cron 定时触发器、record_change 数据变更触发器和 webhook 触发器，包含 @Automation/@BindTrigger 装饰器用法、handler 入参解析和 Crontab 表达式规范。Use when 需要：(1) 为已创建的自动化任务/定时任务编写业务 handler，(2) 编写 automation 代码绑定触发器，或其他自动化任务相关开发
steering: true
steering-topic: trigger_guide
match-template-name: nestjs-react-fullstack
---

## 自动化任务配置与代码编写指引

### 自动化任务配置

1. 新建自动化任务触发器时无需 enable（激活），将任务创建好然后开发完代码即可。触发器随后交由用户主动操作、要求开始。

### 目录结构

```text
server
└── modules
    └── xxx
        ├── xxx.automation.ts
        ├── xxx.module.ts // 必须在 module 中注册自动化任务类，并且在 app.module.ts 中引用并注册该 module，否则代码将不会生效。
        └── 其他文件（如有的话）
```

文件命名规则：{模块名}.automation.ts

注意：

1. 每个模块只应该有一个存放自动化任务逻辑的文件，业务逻辑需要聚合到该文件中。
2. 如果该模块只有对应的自动化任务，无需编写 Controller

### 触发器类型

触发器类型（`triggerType`）有三种：

- `record_change`：记录变更触发器，**有入参**
- `cron`：定时触发器，**无入参**
- `webhook`：Webhook 触发器，**有入参**

各触发器 handler 的入参类型定义（`TaskHandlerArgs`、`DataChangeEventInput`、`WebhookEvent`）见 [触发器入参类型与代码示例](references/trigger-lifecycle.md)。

### 指定值限制

1. Webhook 触发器不可以设置指定值，并且告知用户。

### 代码绑定

你需要根据触发器创建后确定的自动化任务名字（应用内唯一），编写并绑定到对应的方法上：`@BindTrigger('<任务名字>')` 中的名字必须与创建触发器时确定的名字逐字相同，不能用 trigger ID 或方法名代替。`@Automation()` 标记的类需注册为对应 `<module>.module.ts` 的 provider，且该 module 必须被 `server/app.module.ts` 直接或传递 import，否则装饰器不会生效。完整代码示例见 [触发器入参类型与代码示例](references/trigger-lifecycle.md)。

### 任务代码实现约束

1. 执行自动化任务时无法获取用户信息。依赖用户信息的场景，实现路径如下：
   - 需要查询数据库中的特定数据，给用户发消息：数据库中需要存储用户 id，使用从数据库中查询到的用户 id 进行后续操作
   - 需要调用飞书能力给用户发消息：飞书能力不应该接受用户信息作为参数，而是应该在飞书能力配置里要求用户自己预先指定

2. 入参解析规范（仅 record_change 和 webhook 触发器）：
   - 有入参的触发器方法签名为 `async methodName(event: TaskHandlerArgs)`，`cron` 触发器无入参
   - `content.input` 是 JSON 字符串，先用 `typeof input === 'string'` 检查类型，再用 `JSON.parse()` 解析，需添加 try-catch 错误处理
   - `record_change`：根据操作类型获取数据：INSERT/UPDATE 使用 `after` 字段，DELETE 使用 `before` 字段
   - `webhook`：从 `method`、`path`、`query`、`headers`、`body` 中按需取用；`body` 本身也是 JSON 字符串，需要时再次 `JSON.parse()` 解析；`query` 和 `headers` 的值均为 `string[]`

### 技术实现路径参考

以下常见需求的推荐实现路径，帮助你在平台能力限制下找到合理的技术方案；完整代码见 [触发器入参类型与代码示例](references/trigger-lifecycle.md)。

- **场景一：管理页面控制定时任务启停** —— 平台侧不支持通过 API 动态启停触发器；定时触发器始终保持开启，在任务执行时查询数据库中的开关状态决定是否执行。
- **场景二：定时任务通知特定用户** —— 任务执行时无法获取用户上下文；在数据库预存目标用户 ID，执行时查询再调用飞书插件发送。
- **场景三：记录变更触发器防抖/去重** —— 利用数据库记录最近一次处理时间戳，对比 event 时间戳进行去重。
- **场景四：自定义定时任务触发时间** —— cron 创建后不可动态改；平台设固定高频定时器（如每 30 分钟），执行时读数据库配置判断是否命中。

## Crontab 表达式规范

### 基本结构

Crontab 表达式由 5 个字段组成：`<minute> <hour> <day> <month> <week>`

### 字段说明

1. **minute（分钟）**：0-59 的整数
2. **hour（小时）**：0-23 的整数
3. **day（日期）**：1-31 的整数，或大写字母 `L` 表示月份的最后一天
4. **month（月份）**：1-12 的整数
5. **week（星期）**：0-6 的整数，其中 0 表示星期天

### 特殊字符

- **星号 `*`**：表示所有可能的值（每）
  - 例：`* * * * *` 表示每分钟
- **逗号 `,`**：表示列表范围
  - 例：`1,2,3 * * * *` 表示每小时的第 1、2、3 分钟
- **中杠 `-`**：表示数值范围
  - 例：`1-10 * * * *` 表示每小时的第 1 到 10 分钟
- **正斜线 `/`**：表示间隔频率
  - 例：`0 10-18/2 * * *` 表示每天 10 点到 18 点，每隔 2 小时执行

## 输出要求

1. 必须以 JSON 格式输出
2. JSON 包含两个字段：
   - `expression`：Crontab 表达式字符串
   - `explanation`：中文说明，简要描述执行时间
3. 如果用户描述不清晰，请询问具体细节

## 示例

**用户输入**：每天早上 8 点执行

**输出**：

```json
{
  "expression": "0 8 * * *",
  "explanation": "每天早上 8:00 执行"
}
```

**用户输入**：每周一到周五的上午 9 点和下午 6 点执行

**输出**：

```json
{
  "expression": "0 9,18 * * 1-5",
  "explanation": "每周一至周五的 9:00 和 18:00 执行"
}
```

**用户输入**：每隔 30 分钟执行一次

**输出**：

```json
{
  "expression": "*/30 * * * *",
  "explanation": "每隔 30 分钟执行一次"
}
```

**用户输入**：每月最后一天的晚上 11 点执行

**输出**：

```json
{
  "expression": "0 23 L * *",
  "explanation": "每月最后一天的 23:00 执行"
}
```

**用户输入**：每个工作日的每小时第 15 和 45 分钟执行

**输出**：

```json
{
  "expression": "15,45 * * * 1-5",
  "explanation": "每周一至周五，每小时的第 15 和 45 分钟执行"
}
```

**用户输入**：每天上午 10 点到下午 6 点，每隔 2 小时执行

**输出**：

```json
{
  "expression": "0 10-18/2 * * *",
  "explanation": "每天 10:00、12:00、14:00、16:00、18:00 执行"
}
```

## 注意事项

- 星期字段：0 和 7 都可以表示星期天（但本规范使用 0）
- 时间采用 24 小时制
- 月份和星期都从较小的数字开始计数
- 确保生成的表达式符合实际日历逻辑
- 由于技术限制，最小间隔为 30 分钟，如用户要求有误请直接拒绝用户并给出原因
- 输出必须是有效的 JSON 格式
