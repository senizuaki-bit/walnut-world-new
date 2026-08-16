## PluginInstance 代码编写指南

### 核心原则：根据场景选择调用侧

**默认优先在 Client 侧调用 capabilityClient；但当插件结果需要持久化到数据库时，应考虑在 Server 侧调用或确保前端调用后及时通过后端接口保存。**
**严禁** import ｛ capabilityClient ｝ from '@lark-apaas/client-capability'。
**唯一指定**的导入方式是 import { capabilityClient } from '@lark-apaas/client-toolkit';

| 优先级 | 场景 | 调用方式 |
|-------|------|---------|
| **首选** | 绝大多数场景 | `capabilityClient.load(id).call()` |
| **首选** | 流式输出场景 | `capabilityClient.load(id).callStream()` |
| **兜底** | Client 侧无法满足时 | `CapabilityService.load(id).call()` |

#### 什么情况下应使用 Server 侧？

以下场景适合在 Server 侧调用：

1. **涉及敏感凭证**：调用需要服务端私密 token/secret，不适合暴露给前端
2. **必须后端编排**：多个插件调用之间有强事务依赖，需要后端统一编排
3. **触发器/定时任务场景**：没有前端上下文，只能由后端发起
4. **插件结果需要持久化**：调用结果需要保存到数据库，在 Server 侧可以在同一方法中完成调用+落库，保证数据一致性

> **提示**：如果插件结果不需要存储、仅用于即时展示（如流式生成文本、发送消息），优先在前端调用。但当结果需要保存到数据库时，不要回避使用 Server 侧。

---

### 插件结果持久化决策

当插件返回的结果需要保存到数据库时（如 AI 分类/摘要结果、文档解析的结构化数据、图片识别结果、语音转文字内容等），按以下决策选择方案：

```
插件结果是否需要持久化到数据库？
├── 否 → Client 侧调用（默认）
└── 是 → 推荐方案 A：Server 侧调用，在 Service 中调用插件并在同一方法中落库
         备选方案 B：Client 侧调用插件 → 成功后通过已有 CRUD 接口保存结果
```

| 应避免的做法 | 推荐做法 |
|------------|---------|
| 前端调用插件后不保存结果，导致数据丢失 | 插件调用成功后及时持久化 |
| 为保存插件结果单独新建 API（如 `PATCH /api/xxx/ai-analysis`） | 优先复用已有的 create/update 接口，扩展字段即可 |
| 仅在前端 state 中暂存插件结果，不写入数据库 | 通过后端接口保存到数据库 |

---

### Client 侧调用方式（默认首选）

#### 1. 调用前获取权威依据

在为某个插件实例生成调用代码前，必须先通过 `get_plugin_ai_json` 工具获取该插件实例的运行时投影（plugin_Instance.ai.json），并以其中信息为准：

- `actions[].key`：调用时要传的 `actionKey`
- `actions[].inputSchema / outputSchema`：入参/出参结构
- `actions[].outputMode`：`unary | stream`（决定调用与结果处理方式）

**编码前闸门（必须）**：先产出 Schema 摘录卡，再开始代码编辑。

```markdown
[Schema 摘录卡]
- pluginInstanceId / actionKey / outputMode
- input.required / output.fields / readme.constraints
- 调用侧决策: Client | Server
```

若摘录卡字段缺失，不得进入实现阶段。

#### call / callStream 函数签名

```typescript
.call(actionKey: string, input: object)       // 非流式，返回 Promise<output>
.callStream(actionKey: string, input: object)  // 流式，返回 AsyncIterable<chunk>
```

- **第一个参数 `actionKey`**：必须是字符串，值来自 `get_plugin_ai_json` 返回的 `actions[].key`（如 `'sendFeishuMessage'`、`'textGenerate'`）
- **第二个参数 `input`**：必须是对象，结构符合 `actions[].inputSchema`

```typescript
// ❌ 错误：把参数 JSON.stringify 后当作 actionKey
plugin.call(JSON.stringify({ meeting_title: '...' }));
// ❌ 错误：漏掉 actionKey，直接传参数对象
plugin.call({ meeting_title: '...' });

// ✅ 正确：第一个参数是 actionKey 字符串，第二个参数是 input 对象
plugin.call('send_feishu_message', { meeting_title: '...' });
```

#### 2. 非流式调用（outputMode = "unary"）

```typescript
import { capabilityClient } from '@lark-apaas/client-toolkit';
import { logger } from "@lark-apaas/client-toolkit/logger";

const result = await capabilityClient
  .load('create_feishu_group')
  .call('createGroup', {
    group_name: '项目讨论群',
    members: ['user_001', 'user_002'],
  });

logger.info(result);
```

#### 3. 流式调用（outputMode = "stream"）

##### 必须处理返回形态差异（重点）

`callStream()` 可能返回 `AsyncIterable<chunk>` 或 `{ output: AsyncIterable<chunk> }`，必须先归一化。

```typescript
type AnyRecord = Record<string, unknown>;

function isAsyncIterable(value: unknown): value is AsyncIterable<AnyRecord> {
  return !!value && typeof (value as AnyRecord)[Symbol.asyncIterator] === 'function';
}

function normalizeStream(resultOrStream: unknown): AsyncIterable<AnyRecord> {
  if (isAsyncIterable(resultOrStream)) {
    return resultOrStream;
  }
  if (
    resultOrStream &&
    typeof resultOrStream === 'object' &&
    'output' in (resultOrStream as AnyRecord) &&
    isAsyncIterable((resultOrStream as AnyRecord).output)
  ) {
    return (resultOrStream as AnyRecord).output as AsyncIterable<AnyRecord>;
  }
  throw new Error('Invalid callStream result: cannot find AsyncIterable stream');
}

function readFirstStringField(
  chunk: AnyRecord,
  keys: string[],
): string {
  for (const key of keys) {
    const value = chunk[key];
    if (typeof value === 'string') {
      return value;
    }
  }
  return '';
}
```

##### 场景判断与推荐方案

| 场景 | 特征 | 推荐度 |
|-----|------|-------|
| **多插件并行流式** | 多个插件各返回单一输出，并行调用 |  **优先推荐** |
| **单插件 JSON 流式解析** | 单插件返回结构化 JSON，需边接收边解析 | ⚠️ 仅在必要时 |

**核心原则**：在插件设计阶段按「原子化拆解」拆分，避免单插件返回多字段 JSON。

##### 推荐：多插件并行流式

适用于需求涉及多种输出（标题、正文、图片等），各输出相对独立。

```tsx
import { logger } from "@lark-apaas/client-toolkit/logger";

function MultiPluginStreamExample() {
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [coverUrl, setCoverUrl] = useState('');

  const handleGenerate = async (keywords: string) => {
    // 1. 封面图（非流式，异步不阻塞）
    capabilityClient
      .load('cover_generator')
      .call<{ images: string[] }>('textToImage', { keywords })
      .then(res => res?.images?.[0] && setCoverUrl(res.images[0]))
      .catch(err => logger.warn('封面生成失败', err));

    // 2. 标题（非流式）
    capabilityClient
      .load('title_generator')
      .call<{ content: string }>('textGenerate', { keywords })
      .then(res => res?.content && setTitle(res.content));

    // 3. 正文（流式，边生成边展示）
    const streamResult = capabilityClient
      .load('content_generator')
      .callStream<{ content: string }>('textGenerate', { keywords });
    const contentStream = normalizeStream(streamResult);

    // 🎯 按 outputSchema 字段提取，禁止把 chunk 当字符串
    for await (const chunk of contentStream) {
      const delta = readFirstStringField(
        chunk as Record<string, unknown>,
        ['content'], // 必须来自 get_plugin_ai_json.actions[].outputSchema
      );
      if (delta) {
        setContent(prev => prev + delta);
      }
    }
  };

  return (/* 各字段独立渲染 */);
}
```

**优点**：代码简洁、各插件独立、某个失败不影响其他。

##### ⚠️ 兜底：单插件 JSON 流式解析

当无法拆分为多插件时，需处理不完整 JSON 的逐字符到达，需实现 `parseStreamingStringField` 和 `extractJsonObject` 工具函数。**强烈建议在插件设计阶段拆分为多插件并行流式调用，避免此场景。**

---

### 失败日志最小集（必须）

失败日志至少包含以下字段：

```typescript
{
  pluginInstanceId: string,
  actionKey: string,
  outputMode: 'unary' | 'stream',
  inputKeys: string[],
  resultType?: string,
  resultKeys?: string[],
  firstChunkKeys?: string[],
  error: string
}
```

### 改后冒烟验证清单（必须）

完成调用代码后，最少执行并记录：

1. 一个 `unary` action 的真实调用结果（字段按 `outputSchema` 读取）
2. 一个 `stream` action 的真实调用结果（chunk 按 `outputSchema` 字段读取）
3. 调用失败时的最小日志字段齐全
4. 若无法执行真实调用，必须明确写明阻塞原因，禁止直接标记"开发完成"

### Server 侧调用方式（仅兜底场景）

> 以下场景适合使用 Server 侧调用，特别是涉及数据持久化时不要回避后端。

#### 1. 何时适合用 Server 侧？

| 场景 | 原因 | 示例 |
|------|------|------|
| 触发器/Webhook | 无前端上下文 | 数据变更时自动发送通知 |
| 定时任务 | 无前端上下文 | 每日定时生成报告 |
| 敏感凭证调用 | 凭证不能暴露给前端 | 调用需要 admin token 的 API |
| 强事务编排 | 多步骤需要原子性 | 创建记录 → 发通知 → 更新状态必须全成功或全回滚 |
| 插件结果需持久化 | 调用结果需保存到数据库 | AI 分类/摘要结果需落库、文档解析的结构化数据需入库、图片识别结果需关联业务记录、语音转文字结果需存档等 |

#### 2. NestJS 注入方式

```typescript
import { Injectable, Inject, Logger } from '@nestjs/common';
import { CapabilityService } from '@lark-apaas/fullstack-nestjs-core';

@Injectable()
export class XxxService {
  private readonly logger = new Logger(XxxService.name);

  constructor(
    @Inject() private readonly capabilityService: CapabilityService,
  ) {}
}
```

#### 3. 调用示例

```typescript
const inputParams = {
  // 严格按 get_plugin_ai_json.actions[].inputSchema 构造
};

try {
  const output = await this.capabilityService
    .load('')
    .call('', inputParams);
  return output;
} catch (error) {
  this.logger.error('pluginInstance call failed', {
    pluginInstanceId: '',
    actionKey: '',
    error: error instanceof Error ? error.message : 'Unknown error',
  });
  throw error;
}
```

#### 4. Server 侧编排与容错原则

- PluginInstance 调用在 Server 侧通常属于 **外部依赖 / side-effect**
- 除非业务明确要求强一致性，**默认不应阻塞主业务流程**

推荐写法：异步触发 + catch 兜底：

```typescript
this.somePluginInstanceSideEffect(input).catch(error => {
  this.logger.warn('PluginInstance side-effect failed, ignored', {
    error: error instanceof Error ? error.message : 'Unknown error'
  });
});
```

---

### outputMode 与调用侧选择

先通过 `get_plugin_ai_json(pluginInstanceId)` 获取 `actions[].outputMode`：

| outputMode | 推荐调用侧 | 调用方式 |
|------------|-----------|---------|
| `unary` | **Client 侧优先** | `capabilityClient.load(id).call(actionKey, input)` |
| `stream` | **Client 侧优先** | `capabilityClient.load(id).callStream(actionKey, input)` |
| 任意（兜底场景） | Server 侧 | `capabilityService.load(id).call(actionKey, input)` |

**选择原则**：

- 不涉及持久化时，优先在 Client 侧直接调用
- `outputMode = stream` 时，Client 侧使用 `callStream` 做渐进式渲染
- 涉及持久化、触发器、敏感凭证、事务编排等场景时，使用 Server 侧

---
