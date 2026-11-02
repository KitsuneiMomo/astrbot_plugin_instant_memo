<div align="center">
  <img src="./logo.png" width="128" height="128" alt="logo"/>
  <h1>速记备忘录 (astrbot_plugin_instant_memo)</h1>
</div>

一款为 AstrBot 设计的 **AI 自治备忘录**插件。与传统的“用户手动设闹钟”不同，本插件让 AI 自身拥有“记事本”和“日程表”——它可以自主记录状态备忘、创建定时/周期任务、注册关键词搭话监听，且一切操作均在对话中自然完成，无需用户学习额外指令。

同时，本插件提供了一个现代化的前端管理面板，支持对条目进行可视化增删改查。

![聊天使用示例](./example.png)

---

## ![diff](https://img.icons8.com/material-outlined/24/idea.png) 与传统备忘录/定时任务插件有何不同？

| 对比维度 | 传统备忘录/定时插件 | 速记备忘录 (Instant Memo) |
| :--- | :--- | :--- |
| **触发方式** | 用户手动输入指令设置 | AI 在对话中自主判断并创建 |
| **WebUI 可视化管理** | 仅支持查看 | 提供完备的增删改查可视化管理面板 |
| **AI 权限控制** | 无法限制 | 细粒度控制 AI 的增/改/删权限以及启用的备忘形式 |
| **定时任务内容** | 固定文本或简单模板 | 触发前动态读取上下文，由 LLM 实时渲染生成 |
| **周期任务支持** | 通常仅支持单次提醒 | 支持单次 (one_off) / 每日 (daily) / 工作日 (workday) / 间隔循环 (interval) 四种模式 |
| **关键词搭话** | 不支持 | AI 可注册关键词监听，命中时结合上下文主动搭话 |
| **状态备忘录** | 不支持 | AI 可为自己设置临时人设/状态，到期自动过期 |

---

## ![wrench](https://img.icons8.com/material-outlined/24/wrench.png) WebUI 可视化管理面板

插件内置了现代、美观、契合 AstrBot 原生夜间模式的毛玻璃管理界面，提供两大主功能区：

### 1. 条目管理 (Item Management)

![条目管理](./screenshot_memos.png)

* **短期状态与人设备忘录**：实时查看 AI 当前的临时记忆状态及剩余时间。支持管理员手动新增、编辑或删除状态备忘。
* **定时计划与周期性提醒**：列出所有运行中的定时和周期提醒任务（单次、每日、工作日、定期间隔），以及任务的最新生成状态。
* **关键词搭话与主动监听**：管理已注册的关键词主动搭话监听器，可直接修改触发词、搭话策略及引用的上下文深度。

### 2. 插件设置 (Rich Settings)

![插件设置](./screenshot_settings.png)

* **基础运行配置**：设置定时任务后台轮询频率，切换触发模式（Tool Call / XML Tag），以及设置生成定时提醒时大模型获取的默认聊天上下文消息条数。
* **大模型操作权限控制**：
  * `ai_allow_add`：是否允许 AI 主动创建条目。
  * `ai_allow_update`：是否允许 AI 在对话中修改条目参数。
  * `ai_allow_delete`：是否允许 AI 主动删除条目。
* **AI 备忘形式限制**：
  * 可单独开关状态备忘录、定时任务、关键词监听，防止大模型胡乱设置任务。
* **群聊启用设置**：
  * 支持**全局备忘录**开关。
  * 支持**群聊启用过滤模式**（全部群聊启用 / 仅白名单启用 / 黑名单禁用），可在文本框中配置目标群聊号码列表，控制注入、创作及搭话是否生效。

---

## ![config](https://img.icons8.com/material-outlined/24/settings.png) 配置说明

插件启用后，可在 AstrBot 控制面板或插件管理 WebUI 中配置以下选项：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `poll_interval` | `int` | `15` | 后台循环检查定时任务的频率（秒）。 |
| `trigger_mode` | `string` | `tool` | 触发模式：`tool`（工具调用模式）或 `xml`（XML 标签模式）。 |
| `context_history_limit` | `int` | `5` | 定时任务/关键词搭话触发时，大模型能够阅读的最近上下文历史消息数。 |
| `ai_allow_add` | `bool` | `true` | 是否允许 AI 新建备忘条目。 |
| `ai_allow_update` | `bool` | `true` | 是否允许 AI 修改备忘条目。 |
| `ai_allow_delete` | `bool` | `true` | 是否允许 AI 删除备忘条目。 |
| `enable_status_memo_ai` | `bool` | `true` | 是否允许 AI 使用和写入“状态备忘录”。 |
| `enable_task_ai` | `bool` | `true` | 是否允许 AI 使用和设立“定时任务”。 |
| `enable_keyword_trigger_ai` | `bool` | `true` | 是否允许 AI 使用和设立“关键词搭话”。 |
| `allow_global_memo` | `bool` | `true` | 是否允许创建全局生效的备忘录。 |
| `group_filter_mode` | `string` | `all` | 群聊启用模式：`all` (全部群聊启用) / `whitelist` (仅白名单启用) / `blacklist` (黑名单禁用)。 |
| `group_list` | `string` | `""` | 目标群号列表，用逗号、空格或换行分隔。 |
| `image_render_enabled` | `bool` | `false` | 是否让 `/memo list` 优先发送图片。默认关闭，保持旧版纯文字行为；渲染/发送失败会自动回退文字。 |
| `image_cache_enabled` | `bool` | `true` | 是否复用内容未变化的缓存图片。缓存位于插件数据目录的 `image_cache/`。 |
| `image_cache_max_mb` | `int` | `32` | 图片缓存容量上限（MB），超出后按最旧文件淘汰。 |
| `image_cache_ttl_hours` | `int` | `168` | 图片缓存有效期（小时），填 `0` 表示不按时间过期。 |
| `image_style_version` | `string` | `"1"` | 图片样式版本；修改模板/样式后递增即可让旧缓存失效。 |
| `image_scale_level` | `string` | `high` | 图片清晰度：`normal` (1.0x) / `high` (1.3x，默认) / `ultra` (1.8x)。等级越高文字越锐利，渲染稍慢、图片稍大。 |
| `image_max_items` | `int` | `50` | 图片最多展示的条目数，超出时提示使用文字模式查看完整列表。 |
| `image_item_max_chars` | `int` | `160` | 图片中单条内容的最大字符数，超出自动截断。 |

---

## ![usage](https://img.icons8.com/material-outlined/24/book.png) 功能详解

### 1. 状态/人设备忘录
AI 可以为自己设置临时的状态或人设备忘录，在有效期内会被注入到 System Prompt 中，到期后自动清除。
- 支持设定有效时长（分钟），填 `0` 或 `-1` 代表**永久有效**（永不过期，除非手动/AI主动删除）
- 支持全局、特定群聊（`123`）、指定群员（`123:456,678`）及跨群全局指定用户（`GLOBAL:456`）等丰富作用域
- AI 可自主覆盖更新或删除不再需要的备忘录

### 2. 定时/周期任务
AI 可以自主创建主动推送任务，在触发时刻到来前自动读取最新会话上下文，由 LLM 动态生成推送内容（支持全局广播下发至当前活跃会话）。
- **单次定时 (one_off)**：指定分钟数后触发一次
- **每日定时 (daily)**：每天固定时刻触发（HH:MM 格式）
- **工作日定时 (workday)**：周一至周五固定时刻触发，跳过周末（HH:MM 格式）
- **间隔循环 (interval)**：每隔固定分钟数周期性触发

### 3. 关键词搭话监听
AI 可以注册关键词监听器，当群聊或私聊中出现指定关键词时，自动结合上下文生成搭话内容并主动发送。
- 支持全局或当前会话作用域
- 可配置上下文历史条数
- AI 可自主管理（创建/删除）触发器

---

## ![admin](https://img.icons8.com/material-outlined/24/shield.png) 用户管理指令

除了 Web 管理页面外，管理员在聊天中可通过以下指令手动管理所有备忘录、定时任务和关键词搭话（内置管理员鉴权校验）：

| 指令 | 说明 |
| :--- | :--- |
| `/memo` 或 `/memo list` | 列出所有备忘录、定时任务与搭话监听器 |
| `/memo list text` | 强制使用纯文字列表（图片渲染开启时也适用，便于查看未截断的完整内容） |
| `/memo del <序号>` | 删除指定的条目 |
| `/memo edit <序号> <属性> <新值>` | 修改指定条目的属性 |

> 图片渲染默认关闭。开启 `image_render_enabled` 后，`/memo list` 会优先发送图片；图片渲染、图片发送或缓存读写失败时会自动回退到上表描述的原文字输出，并记录 warning 日志。
>
> 图片沿用原有的卡片版式（浅灰页面底 + 白色圆角卡片）。为保证清晰度，默认使用无损 PNG 截图，并要求渲染服务按设备像素比缩放（`scale=device` + `device_scale_factor_level`），默认 `high`（1.3x）；如需在手机端进一步看清小字，可把 `image_scale_level` 调为 `ultra`（1.8x）。清晰度等级会参与缓存 key，修改后旧图自动失效。
>
> 截图高度随内容自适应：渲染选项把视口高度压到 1px（`viewport_height`），full-page 截图的高度就只由内容决定。否则渲染服务的默认 720px 视口会在条目被删减后留下底部空白。

**可修改属性：**
- **状态备忘录**：`content`（内容）、`expire`（过期时间/分钟，0表示永久）、`global`（是否全局）
- **定时任务**：`desc`（描述）、`value`（触发时间）、`type`（类型: one_off/daily/workday/interval）
- **关键词搭话**：`keyword`（触发词）、`desc`（回复设定）、`global`（是否全局）
