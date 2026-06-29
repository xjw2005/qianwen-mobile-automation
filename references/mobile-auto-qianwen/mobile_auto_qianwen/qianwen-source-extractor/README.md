# Qianwen Source Extractor

从千问（Qianwen）分享页面提取参考来源，并写回飞书多维表格的独立工具集。

## 核心原理

千问分享页面的 DOM 只渲染平台名称+域名（如"中国食品安全网 www.cfsn.cn"），**不包含真实文章URL**。真实URL（如 `https://www.cfsn.cn/news/detail/2137/343342.html`）只能通过调用 share/info API 获取：

```
POST https://chat2-api.qianwen.com/api/v1/share/info?pr=qwen&fr=mac
Body: {"share_id":"<id>","biz_id":"ai_qwen"}
```

API响应中来源列表路径：
```
data.session.record_list[].response_messages[].meta_data.sources[].content.list[]
```

脚本在页面上下文中重放此API请求（携带正确的cookie/origin），提取真实来源URL。

## 文件结构

```
qianwen-source-extractor/
├── run.js              # 主入口：串联提取+写回完整流程
├── extract-sources.js  # 提取脚本：通过CDP调用千问API获取真实来源URL
├── write-feishu.js     # 写回脚本：将来源写入飞书多维表格
└── package.json        # 独立包配置
```

## 依赖

- Node.js
- playwright-core（用于CDP连接）
- lark-cli（用于飞书写回，需在PATH中可用）

## 前置条件

1. Chrome 以远程调试模式启动（CDP端口默认9222）
2. 已在Chrome中打开目标千问分享页面
3. lark-cli 已安装并可用（飞书写回需要）

## 使用方式

### 方式一：一步到位（提取+写回）

```bash
node qianwen-source-extractor/run.js \
  --url "https://www.qianwen.com/share/chat/<share_id>?biz_id=ai_qwen" \
  --question-id NQ-001 \
  --base-token <feishu_app_token> \
  --table-id <feishu_table_id>
```

### 方式二：仅提取来源

```bash
node qianwen-source-extractor/run.js \
  --url "https://www.qianwen.com/share/chat/<share_id>?biz_id=ai_qwen" \
  --extract-only \
  --output sources.json
```

### 方式三：仅写回飞书（从已有JSON文件）

```bash
node qianwen-source-extractor/run.js \
  --write-only \
  --sources sources.json \
  --question-id NQ-001 \
  --base-token <feishu_app_token> \
  --table-id <feishu_table_id>
```

### 单独运行脚本

```bash
# 仅提取
node qianwen-source-extractor/extract-sources.js \
  --url "https://www.qianwen.com/share/chat/<share_id>?biz_id=ai_qwen" \
  --output sources.json

# 仅写回
node qianwen-source-extractor/write-feishu.js \
  --sources sources.json \
  --base-token <feishu_app_token> \
  --table-id <feishu_table_id> \
  --question-id NQ-001
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--url` | 千问分享页面URL（提取步骤必需） | - |
| `--question-id` | 问题ID（写回步骤必需） | - |
| `--natural-question` | `--question-id` 的兼容别名 | - |
| `--base-token` | 飞书多维表格 app_token（写回步骤必需） | - |
| `--table-id` | 飞书多维表格 table_id（写回步骤必需） | - |
| `--cdp` | CDP端点URL | `http://127.0.0.1:9222` |
| `--timeout` | 页面就绪等待时间(ms) | 15000 |
| `--output` | 保存提取结果到JSON文件 | - |
| `--sources` | 从已有JSON文件加载来源（用于write-only模式） | - |
| `--extract-only` | 仅提取，不写回飞书 | false |
| `--write-only` | 仅写回，不提取 | false |
| `--dry-run` | 预览将要写入的数据，不实际写回 | false |
| `--ai-platform` | AI平台名称 | 千问 |

## 输出格式

提取结果JSON结构：

```json
{
  "ok": true,
  "url": "https://www.qianwen.com/share/chat/...",
  "title": "页面标题",
  "apiPath": "data.session.record_list[0].response_messages[3].meta_data.sources[0].content.list",
  "shareId": "335183e32d8f4908ad34427fb90904b2",
  "count": 22,
  "sources": [
    {
      "index": 1,
      "title": "文章标题",
      "url": "https://www.example.com/article/123",
      "normalizedUrl": "https://www.example.com/article/123",
      "platform": "平台名称",
      "summary": "文章摘要...",
      "publishTime": "2026-06-15",
      "type": "web",
      "reliable": "true",
      "authority": "...",
      "tagName": "...",
      "icon": "..."
    }
  ]
}
```

## 飞书写回字段映射

写入飞书多维表格的字段（与DeepSeek来源表结构对齐）：

| 飞书字段 | 数据来源 | 说明 |
|----------|----------|------|
| 来源标题 | `source.title` | 文章标题 |
| 来源URL | `source.url` | 真实文章URL（非域名） |
| 引用来源类型 | 推断 | 图文/视频（根据URL和内容推断） |
| 引用来源平台 | `source.platform` | 平台名称（清洗后） |
| 问题ID | `--question-id` 参数 | 由调用方传入 |

## 技术要点

1. **CDP连接**：通过 `playwright-core` 的 `connectOverCDP` 连接已运行的Chrome实例
2. **页面定位**：根据URL中的 share_id 匹配正确的浏览器标签页
3. **API重放**：在页面上下文中用 `fetch` 调用 share/info API，自动携带cookie和origin
4. **来源遍历**：递归遍历 `record_list` → `response_messages` → `meta_data.sources` → `content.list`，找到第一个非空来源列表
5. **类型推断**：根据URL/标题/摘要/平台文本判断"视频"或"图文"
6. **平台清洗**：去除平台名称中尾部的日期等噪声文本
7. **lark-cli调用**：通过临时文件传递JSON参数，避免shell转义问题；使用 `powershell -ExecutionPolicy Bypass` 绕过执行策略限制

## 模块API

两个脚本都导出了可复用的函数，供其他脚本调用：

### extract-sources.js

```javascript
const { extractSources } = require('./qianwen-source-extractor/extract-sources');

// 提取来源
const result = await extractSources(
  'http://127.0.0.1:9222',  // CDP端点
  'https://www.qianwen.com/share/chat/xxxx?biz_id=ai_qwen',  // 分享URL
  15000  // 超时(ms)
);
// result.sources 包含来源数组
```

### write-feishu.js

```javascript
const { writeSources, buildRows } = require('./qianwen-source-extractor/write-feishu');

// 写回飞书
const result = writeSources(
  'UiE3bhcHRaCE01sh5Anc1AZanKd',  // base_token
  'tblF1LsniY1BnOt3',  // table_id
  sourcesData,  // extractSources的返回值
  'NQ-001',  // 问题ID
  false  // dryRun
);
```

## 完整调用示例

```javascript
const { extractSources } = require('./qianwen-source-extractor/extract-sources');
const { writeSources } = require('./qianwen-source-extractor/write-feishu');

async function main() {
  // 1. 提取来源
  const sourcesData = await extractSources(
    'http://127.0.0.1:9222',
    'https://www.qianwen.com/share/chat/335183e32d8f4908ad34427fb90904b2?biz_id=ai_qwen',
    15000
  );

  if (!sourcesData.ok) {
    console.error('提取失败:', sourcesData.reason);
    return;
  }
  console.log(`提取到 ${sourcesData.count} 个来源`);

  // 2. 写回飞书
  const result = writeSources(
    'UiE3bhcHRaCE01sh5Anc1AZanKd',
    'tblF1LsniY1BnOt3',
    sourcesData,
    'NQ-001'
  );
  console.log('写回结果:', result);
}

main();
```
