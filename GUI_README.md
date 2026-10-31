# Handout-Maker2 桌面客户端（GUI）—— 架构与使用说明

> 把 `batch.py` 的批量转换能力包成一个现代化桌面客户端：
> **拖拽导入（PDF/PPT）→ 选模型配置 → 一键批量转换 → 实时日志/进度 → 交付体检**。
> 启动方式：`python main_gui.py`（工程根目录，conda 环境 `Handout-Maker2`）。

---

## 1. 技术选型：为什么是 PySide6

| 方案 | 优点 | 问题 | 结论 |
| --- | --- | --- | --- |
| **PySide6（Qt6）** ✅ | 与核心脚本**同进程同语言**，直接调用 `batch.convert_one()`；QThread/信号槽、拖拽、表格、进度条、QSS 主题全部开箱即用；一个 `pip install` 就完事 | 需要遵守 Qt 的线程纪律（本仓库已经踩完坑并写进了代码注释） | **推荐**：对「Python 核心 + 桌面壳」来说最轻、最稳、最省 |
| CustomTkinter | 更轻、上手快 | 没有成熟的数据表/富日志/拖拽控件，复杂任务列表要手搓；主题能力弱 | 只适合「一张表单」级别的工具 |
| Tauri / Electron | UI 最漂亮、可上架商店 | 必须把 Python 打包成**独立 sidecar 进程** + JSON-RPC 桥；双运行时、打包体积巨大；每次改参数都要同步两端协议 | 对「一个人维护的 Python 核心」纯属负担 |

**核心事实**：转换逻辑在 `batch.py` 里是**纯函数 + print 进度**，没有全局状态依赖。
同进程 import 就能拿到的能力（逐页进度、.part 语义、体检、报表），走子进程反而要
重新发明一套序列化协议。所以本 GUI 选「**进程内模块化调用**」；`batch.py` 的 CLI
仍然原样可用，两套入口共享同一条转换链路、产物完全一致。

---

## 2. 架构分层与解耦

```
┌──────────────────────────── 展示层（绝不 import 底层脚本）────────────────────────────┐
│  widgets/file_panel.py    多文件导入 / 任务列表 / 拖拽 / 清空重置                       │
│  widgets/model_panel.py   配置档案下拉选择 / 预览 / 新建编辑删除 / 测试连接              │
│  widgets/profile_dialog.py配置编辑弹窗（高级参数表由注册表自动生成）                     │
│  widgets/log_panel.py     实时日志（彩色分级 / 自动滚动 / 可折叠）                       │
│  main_window.py           编排：选项持久化、进度条、开始/暂停/终止、菜单、关闭善后       │
└──────────────────────────────────────┬───────────────────────────────────────────────┘
                                       │ 只通过「信号/槽 + 纯数据」
┌──────────────────────────────────────┴───────────────────────────────────────────────┐
│                             core 层（桥接与并发）                                      │
│  core/models.py       FileTask / ApiProfile / ConvertOptions + PARAM_SPECS 参数注册表  │
│  core/config_store.py JSON 持久化（原子写、默认值合并、schema 版本）                    │
│  core/worker.py       ConversionWorker(QThread)：串行批次循环、暂停/终止、              │
│                       _LogCapture（stdout→事件队列）、Prep/Check 辅助线程               │
│  core/bridge.py  ★    唯一允许 import batch.py / handout_maker.py / handout_maker2.py  │
└──────────────────────────────────────┬───────────────────────────────────────────────┘
                                       │ 进程内函数调用（bridge.convert_one → batch.convert_one）
┌──────────────────────────────────────┴───────────────────────────────────────────────┐
│  底层（一行未改）：batch.py 规划/.part/体检/报表 + handout_maker.py(GLM) /              │
│  handout_maker2.py(Gemini) 渲染→VLM→normalize 全链路                                   │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**解耦规则只有一条**：`batch.py` 及其引擎脚本只允许出现在 `core/bridge.py` 的 import
列表里。UI 与 worker 跟底层交互的全部接口是 bridge 暴露的纯函数与 `FileTask`/`ApiProfile`
纯数据。底层将来升级、换引擎、甚至改成远程服务，都只改 bridge 一个文件。

为什么**不用 CLI 子进程**做桥：底层进度是 print 出来的文本（“正在处理第 3 页（共 15 页）…”），
进程内用自定义 stdout 捕获器可以实时把它变成逐页进度与日志信号；子进程只能靠轮询猜。
进程内还能直接复用 `batch.plan_jobs` / `verify_md` / `_emit_report`，产物语义与 CLI
逐字节一致。代价是底层 bug 与 GUI 同进程 —— 用「文件级异常隔离 + .part 语义 + 每批
汇总」兜住（与 batch 自己的批量循环同一套防线）。CLI 与 GUI 并存，互不依赖。

---

## 3. 一次「开始转换」的数据流

```
[开始转换]
  │ 主线程：收集 startable_tasks → 当前 ApiProfile → ConvertOptions
  ▼
ConversionWorker.start()  （QThread，串行循环，与 batch.py main 同构）
  │
  ├─ bridge.resolve_profile(profile)   → G(引擎模块) / api_key / base_url / models / proxy
  │     · base_url 必须在这里解析成字符串（入口函数只在 CLI main 里调 resolve_base_url，
  │       直传 None 会炸在 base_url.rstrip("/") —— 冒烟测试实测抓到的坑）
  ├─ bridge.build_args(profile, options) → batch.convert_one 需要的 args 命名空间
  │     · 高级参数经 PARAM_SPECS 强转 + kwarg 映射（含 normalize→no_normalize 取反）
  │
  ├─ contextlib.redirect_stdout(_LogCapture)   ← 全局接管底层一切 print
  │     _LogCapture.write()：
  │       ① 终止检查（cancel 且当前是转换线程 → 抛 RequestCancelled，底层 finally 正常走）
  │       ② 按行拆解 → 抹掉 API Key → 逐页进度正则解析
  │       ③ 事件塞进 queue.Queue：("line", 级别, 文本) / ("page", uid, 页, 总页)
  │       ⚠ 绝不在这里发射 Qt 信号（见第 4 节血泪史）
  │
  ├─ 每个文件：skip-existing 检查 → 转 .part → 成功才 os.replace → 可选 verify →
  │     file_updated(dict) 直发（状态/输出/用时/页数）
  ├─ 收尾：JSON 报表（复用 batch._emit_report）→ 汇总打印（复用 batch._print_summary）
  ▼
主线程 QTimer(100ms).drain_events()
  │ 把队列事件变成 log_line / page_progress 信号（只有主线程发射这两个信号）
  ▼
日志面板彩色追加 · 进度条按「已完成页/总页」推进 · 状态栏 ETA（按每页均速估，同 batch.py）
```

### 暂停 / 终止的精确语义

| 操作 | 生效点 | 机制 |
| --- | --- | --- |
| 暂停 | **当前文件跑完后**停在队列里（串行链路中途强停反而干净） | 线程间 `threading.Event`；停在暂停点后工作线程自报 `paused`，按钮变「继续」 |
| 终止 | 底层**下一次 print** 时（即当前这一次 API 请求结束后；每页开头必有 print，实测通常几秒到几十秒） | `RequestCancelled` 在转换线程的 print 调用栈里抛出 —— 底层脚本自己的 finally（清临时图片）都会走完，跟 Ctrl+C 同一条退出路径；半成品 `.part` 原地保留 |
| 关闭窗口（转换中） | 同上，等 5 秒未退出则强杀线程 | `.part` 语义保证已落盘的页不丢，重跑勾「跳过已存在输出」即可续传 |

---

## 4. 线程纪律（都是实测踩出来的坑，已写进代码注释）

1. **同一 QObject 的信号只能有一个线程发射。** 两个线程并发 emit 同一个信号会直接崩 Qt
   （0xC0000409）。所以 `pause()/resume()/cancel()` 只置 `threading.Event`，不发信号；
   `log_line/page_progress` 只由主线程（排空队列时）发射，`file_updated/state_changed/
   batch_finished` 只由工作线程发射。
2. **绝不在底层 `print` 的调用栈里发射 Qt 信号。** 早期实现让日志捕获器直接 emit 信号：
   任何线程往 stdout 打一行字（哪怕只是测试里的 `print`）都会在「print → 捕获 → emit →
   槽函数 → print」之间无限递归，实测 STATUS_STACK_OVERFLOW 硬崩。现在的做法是捕获器只往
   `queue.Queue` 塞事件，主线程 QTimer 排空 —— 彻底消除重入。
3. **PySide6 的 QThread 不是 Python 的 threading.Thread**（连 `ident` 属性都没有），
   `threading.current_thread() is self` 在 run() 里恒为 False。判断「是否在转换线程」
   必须用 `threading.get_ident()` 与 run() 开头记录的线程号比较 —— 否则终止检查永远不
   触发，点「终止」后转换会继续发真实请求。
4. **QThread 里的 Python print 不落地**（PySide6 会吞掉工作线程的 stdout）：排查工作线程
   问题要往文件写，或者经事件队列走日志面板。

---

## 5. 配置持久化与安全

- 位置：`config/app_config.json`（随项目便携；已加入 `.gitignore` —— 里面存 API Key）。
- 写入：先写 `.json.tmp` 再 `os.replace`（原子写），坏文件自动降级默认配置并重建。
- 结构：`profiles[]`（name/engine/base_url/api_key/models/params）、`selected_profile`、
  `options`（输出目录/跳过已存在/体检/HTML 预览/失败即停/启动前自检/页范围/报表路径）、
  `ui`（窗口几何、分栏比例、日志折叠/自动滚动）。带 `schema_version`，老配置缺键自动补默认。
- Key 安全：配置文件里明文存（本地工具惯例）；**日志里自动打码**（捕获器把 Key 替换成
  `***`）；「测试连接」只发一张现场生成的探针小图。
- 留空即「交给引擎默认」：`api_key/base_url/models` 留空 → 引擎脚本自己的
  「环境变量 → 脚本兜底」解析链（glm/gemini）；OpenAI 兼容引擎没有通用默认网关，
  Base URL **必填**、Key 必填或用 `DEEPSEEK_API_KEY`/`OPENAI_API_KEY` 环境变量，
  缺失时转换启动前即报出明确提示；`params` 只存用户覆盖过的值，未覆盖的用注册表默认值。

---

## 6. 扩展指南：加东西不需要重构 UI

### 6.1 新增一种模型 API（引擎）
在 `gui/core/bridge.py` 的 `ENGINE_SPECS` 加一条（引擎下拉框由它自动生成）：
```python
ENGINE_SPECS["myengine"] = EngineSpec(
    "myengine", my_module, "显示名", "一句话说明",
    base_url="", models=("my-model",),
    api_key_envs=("MY_API_KEY",),
    require_key=True, use_module_defaults=False,
)
```
要求模块提供与 `handout_maker.py` 对齐的接口：`convert_pdf_to_handout` / `parse_models` /
`resolve_base_url` / `resolve_api_key` / `check_api` / `DEFAULT_MODELS` / `RENDER_DPI` /
`PAGES_PER_REQUEST` / `MAX_TOKENS`。

内置三个引擎，其中 **`openai` 引擎覆盖「GLM/Gemini 以外的一切 OpenAI 兼容 API」**：
DeepSeek、GPT-4o、Qwen-VL、中转站、本地 vLLM/Ollama —— 新建配置时把引擎选成
「OpenAI 兼容」，填 Base URL（如 `https://api.deepseek.com/v1`）、Key、模型名即可，
`use_module_defaults=False` 保证不回落 GLM 的默认网关、`require_key=True` 保证不会把
GLM 内置 Key 错发给别的网关。⚠ 模型必须支持图片输入（视觉模型）；纯文本模型
（如 `deepseek-chat`）会被接口拒绝，换 `gpt-4o` / `qwen-vl-max` 等视觉模型即可。

### 6.2 新增一个可调参数
在 `gui/core/models.py` 的 `PARAM_SPECS` 加一条 `ParamSpec`（键名/显示名/类型/默认值/
目标 kwarg/适用引擎/说明）。配置弹窗的高级参数表、类型强转、kwarg 映射**全部自动跟上**，
一行 UI 代码都不用改。

### 6.3 新增任务状态 / 列表列
`models.TaskStatus` 加枚举值 + `file_panel._STATUS_COLORS` 加配色即可；状态机
（can_start/finished）已按语义集中在枚举上。

---

## 7. 目录结构

```
Chemistry_Handout_Maker2/
├── main_gui.py                # GUI 启动器：python main_gui.py
├── batch.py / handout_maker.py / handout_maker2.py / normalize.py / verify.py
│                              # 底层核心（一行未改，CLI 原样可用）
├── gui/
│   ├── app.py                 # QApplication + Fusion + 装配主题 + 入口
│   ├── theme.py               # ★ 排版/配色令牌（字阶 16/13/12、字色、行距、QSS）
│   ├── main_window.py         # 主窗口编排（菜单/选项/进度/执行控制/关闭善后）
│   ├── core/
│   │   ├── models.py          # 数据模型 + PARAM_SPECS 参数注册表（扩展点）
│   │   ├── config_store.py    # JSON 持久化（原子写/默认值合并/迁移）
│   │   ├── bridge.py          # ★ 底层对接桥（唯一 import batch 的模块）
│   │   └── worker.py          # ConversionWorker / PrepWorker / CheckWorker
│   ├── converters/
│   │   └── ppt_to_pdf.py      # PPT→PDF（LibreOffice headless → PowerPoint COM）
│   └── widgets/               # file_panel / model_panel / profile_dialog / overwrite_dialog / log_panel
├── tests/
│   ├── gui_smoke.py           # 离线冒烟（不联网不花额度）：预演/PPT失败/协作式终止
│   └── gui_live.py            # 真机联调（HM_GUI_LIVE=1 才跑，≈1 次请求）
├── config/app_config.json     # 首次启动自动生成（已 gitignore，含 API Key）
├── requirements-gui.txt       # GUI 新增依赖
└── GUI_README.md              # 本文档
```

---

## 8. 使用说明

```bash
python main_gui.py                    # 启动
python tests/gui_smoke.py             # 离线冒烟（改代码后必跑）
$env:HM_GUI_LIVE="1"; python tests/gui_live.py   # 真机联调（真实调用一次 API）
```

1. **导入**：拖文件/文件夹到窗口任意位置，或「添加文件/添加文件夹」；支持 `.pdf/.pptx/.ppt`。
   PPT 会先在后台转成 PDF（缓存于 `%TEMP%\HandoutMaker2\ppt_cache`，重复导入秒回），
   需要 LibreOffice 或 PowerPoint（后者 `pip install pywin32`）；没有就先把 PPT 另存为 PDF 再拖。
2. **选配置**：右侧下拉选模型档案；「新建/编辑」改名称、引擎、Base URL、Key、模型降级链
   （逗号分隔）与高级参数（temperature / max_tokens / thinking / dpi / …）；「测试连接」
   真发一张探针小图验证连通性。引擎除 GLM / Gemini 外还有 **「OpenAI 兼容」**：
   DeepSeek、GPT-4o、Qwen-VL、本地 vLLM 等任何 OpenAI wire-format 网关都能配
   （Base URL 必填，Key 可填配置或用 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` 环境变量；
   注意模型必须支持图片输入，纯文本模型会被接口拒绝）。
3. **转换选项**：输出目录（留空 = 与源文件同目录）、只转指定页（`1-3,7` 试参数用）、
   跳过已存在输出（续传）、转换后体检、HTML 预览、失败即停、启动前自检、JSON 报表。
4. **执行**：「预演」只列计划不花额度（和「开始转换」并排，Ctrl+P）；「开始转换」（Ctrl+Enter）串行
   逐个跑（智谱限流是并发维度，刻意不并发）。**如果目标位置已有同名产物，开跑前会先弹窗
   列出会被覆盖的讲义**，默认选项是「跳过已有文件」；只有你明确选「仍然覆盖」才会重写
   （手改过的定稿不会在不知情的情况下没掉）。「暂停」在当前文件跑完后停住（进度条转琥珀、
   文案写明「已暂停」）；「终止」在当前请求结束后停止，半成品 `.part` 保留可排查。
5. **日志**：底层 stdout 实时滚动（渲染尺寸、每次尝试、重试退避、体检明细），彩色分级，
   可折叠（Ctrl+L）；API Key 自动打码，可放心截图。

---

## 9. 已知限制

- **暂停粒度 = 文件级**：当前文件的当前请求不会被打断（串行链路 + 限流友好是刻意设计）。
- **终止延迟 = 当前请求剩余时间**（通常几秒到几十秒，极端情况受请求超时 300s 上限约束）。
- **PPT 依赖外部工具**：LibreOffice（免费）或 PowerPoint + pywin32，二者都没有时提示
  手动另存 PDF。
- **OpenAI 兼容引擎需要视觉模型**：流水线是「渲染页图 → VLM 看图转写」，DeepSeek 官方
  API（deepseek-chat / deepseek-reasoner）目前是纯文本模型，不含视觉输入，转写会被接口
  拒绝（日志会原样显示接口报错）；换视觉模型（gpt-4o / qwen-vl-max / glm-4v 等）或等
  DeepSeek 上线视觉接口即可。
- 输出名契约与 CLI 一致：`<输入名>.md`，同目录同名会被覆盖（用「输出目录」隔离）。
- 关窗强杀线程的兜底路径依赖 `.part` 语义保证不丢已落盘页面。

---

## 10. 测试矩阵

| 测试 | 命令 | 覆盖 |
| --- | --- | --- |
| 离线冒烟 | `python tests/gui_smoke.py` | 窗口实例化、配置读写、导入+预演全链路、PPT 失败路径、协作式终止（渲染完成即取消，零网络）、OpenAI 兼容引擎配置校验（缺 Key 快速失败不触网） |
| 真机联调 | `HM_GUI_LIVE=1 python tests/gui_live.py` | 1 页小图真实走完「渲染→GLM→normalize→落盘」，断言产物含规范化化学式 |
| 底层回归 | `python selfcheck.py test1.pdf` | 底层脚本 227 项断言（GUI 未改动底层，仍应全绿） |
