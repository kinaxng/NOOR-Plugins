# NOOR 插件页面设计约束

插件可以贡献 sidebar 页面、dashboard widget、字幕搜索源、下载器等能力，但视觉上仍属于 NOOR 主产品，不能形成独立站点风格。

## 1. 页面职责

插件页面只承载该插件自己的业务主界面，例如：RSS 浏览、片单、搜索、下载器状态。不要在插件页重复 NOOR 的全局标题、侧边栏、设置页结构。

## 2. 加载与状态

- 插件模块加载由主程序 `PluginPageHost` 负责，插件 `mount()` 内不要立即绘制大面积 loading 框。
- 插件业务数据加载时，只允许一个内容区状态：`loading` / `empty` / `error` 三者互斥。
- 推荐结构：

```html
<div class="plugin-state">
  <span class="plugin-spinner"></span>
  <span>加载中...</span>
</div>
```

- 空状态保持一句话：`暂无内容`、`暂无订阅内容`。
- 错误状态只显示简短原因；详细异常应进入日志或 devtools。

## 3. 反馈

- 使用 `sdk.toast.success/error/warning/info()`。
- 不使用浏览器 `alert()` 作为常规反馈。
- 成功、失败、警告文案短句化，不把长路径、长 URL、异常堆栈放进 toast。

## 4. 信息层级

- 一级切换：使用 NOOR Vision Tabs 风格。用于 RSS / 片单 / 搜索等页面级切换。
- 二级对象选择：使用较实的 pill/button。用于选择订阅源、片单、下载器实例。
- 三级筛选：使用轻量 chip。用于分类、状态、标签等筛选。
- 状态信息：放在右侧工具区，用轻量 badge，例如 `缓存 7 天`、`已缓存 8 张`。

## 5. 卡片

- 卡片内容只放核心信息：封面、标题、时间、大小、主操作。
- 分类、来源、状态等横向维度优先放到筛选区，不在每张卡片里重复堆叠。
- 标题最多两行，超过省略。
- 卡片圆角、间距和 hover 必须遵守主程序设计规范；如页面已有特殊约束，应写入插件自身 README 或设计备注。

## 6. CSS 约束

- 只能使用 NOOR 设计 token：`--color-*`、`--radius-*`、`--font-*`、`--shadow-*`、`--transition-*`。
- 不引入额外高饱和色、渐变按钮、独立字体。
- 不硬编码新的品牌色。
- 不覆盖全局 body/html/sidebar 样式。
- 插件 CSS class 必须加插件前缀，避免污染主程序，例如 `mteam-*`。

## 7. 交互

- 主操作按钮放在用户视线末端或右侧工具区。
- 危险动作默认隐藏或弱化，进入编辑/管理状态后再出现。
- 删除类动作必须可逆或有明确确认；如果只是删除本地插件配置，可用明确的按钮和 toast 反馈。

## 8. 推荐插件 SDK 使用

```js
export async function mount(el, sdk) {
  const notify = (type, msg) => sdk.toast?.[type]?.(msg)
  // sdk.api.plugin('/rss/items') 可访问当前插件 API
  return () => { el.innerHTML = '' }
}
```

插件应优先使用 `sdk.api.plugin()` / `sdk.api.fetch()`，不要硬编码后端 host/port。


Top action row / 顶部操作行
-------------------------
用于页面级 tabs 同一行右侧的状态与操作，例如：`额度受限`、`已连接`、`刷新`、`新建任务`。

统一结构：左侧为一级 tabs，右侧为 actions；移动端 actions 必须排在 tabs 上方，避免主操作被横向 tabs 挤到第二视觉层级。

尺寸：
- 状态 badge、普通按钮、主按钮统一高度 30px。
- 间距 8px，整行 gap 12px，可换行。

类型与颜色：
- `muted/default`：中性白灰，用于普通状态、次操作按钮。
- `info`：品牌蓝，用于进行中、可点击的当前能力。
- `success`：绿色，用于已连接、已启用、成功。
- `warning`：琥珀，用于额度受限、未配置、需要注意但不阻断。
- `error`：红色，用于已过期、连接失败、阻断错误。
- `primary button`：品牌蓝，只用于当前最重要动作，例如“刷新”“新建任务”。

插件应优先使用 SDK / 全局类：`.noor-plugin-topbar`、`.noor-plugin-topbar__actions`、`.noor-plugin-badge--{tone}`、`.noor-plugin-btn--primary`，不要在每个插件里重新发明高度和色板。

## 9. NOOR Kit / SDK UI 统一约束

插件页面必须优先使用主程序注入的 `sdk.ui.*`，只有业务布局和插件特有内容可以写私有 CSS。

推荐对应关系：

| 目的 | SDK |
| --- | --- |
| 页面根节点 | `sdk.ui.page()` |
| 顶部操作区 | `sdk.ui.topBar()` 或 `.noor-plugin-topbar` |
| 一级 tab | `sdk.ui.tabs()` |
| 按钮 | `sdk.ui.button()` |
| 按钮内提交进度 | `sdk.ui.submitButton()` |
| 状态徽章 | `sdk.ui.badge()` |
| 轻量筛选 | `sdk.ui.chip()` |
| 分页 | `sdk.ui.pagination()` |
| 输入/选择/搜索 | `sdk.ui.input()` / `sdk.ui.select()` / `sdk.ui.search()` |
| 空/加载/错误 | `sdk.ui.emptyState()` / `sdk.ui.loadingState()` / `sdk.ui.errorState()` |
| 弹窗 | `sdk.ui.modal()` / `sdk.ui.confirm()` |

移动端规则：顶部 actions 排在 tabs 上方；插件不要自行反转顺序，使用 `.noor-plugin-topbar` 即可继承全站规则。

## 10. 反向组件沉淀

主程序不是唯一组件来源。插件中如果出现已经被实际页面验证、符合 NOOR token 且可复用的结构，应提升为通用 SDK 组件，再反哺主程序。

当前标准化来源：

| 来源场景 | 沉淀组件 | 用途 |
| --- | --- | --- |
| qB / 迅雷 顶部状态与操作 | `sdk.ui.topBar()`、`sdk.ui.actionRow()` | tabs + 状态 badge + 主操作按钮 |
| qB / 迅雷 / JavDB 统计区 | `sdk.ui.statCard()`、`sdk.ui.statGrid()` | 任务数、速度、缓存、影片数等指标 |
| M-Team / JavDB / AVDB 横向作品卡 | `sdk.ui.mediaCard()` | backdrop/fanart 媒体卡片 |
| 各插件加载状态 | `sdk.ui.loadingState()` | spinner + 短文案 |

判断标准：

- 至少两个页面/插件可能复用。
- 只依赖 NOOR token，不依赖插件私有配色。
- 能明确命名和定义交互边界。
- 抽出来后不会让业务逻辑进入主程序。
