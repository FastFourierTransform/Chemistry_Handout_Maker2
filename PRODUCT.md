# Product

<!-- impeccable:product-schema 1 -->

## Platform

desktop（Windows 桌面客户端，PySide6；未来界面工作目标为现有桌面 GUI，非 web / ios / android / adaptive）

## Users

- 核心用户：**高中化学教师**。自己使用为主，也会把工具分享给同事使用。
- 场景与任务：备课与讲义整理——把自己（或同事）的化学课件 PDF/PPT 批量转成符合《讲义格式规范》的结构化 Markdown 讲义，发给学生或存档；转换结果需人工微调、复核（尤其数值与实验条件）。
- 使用环境：个人 Windows 本机，conda 环境 `Handout-Maker2`（Python 3.12），无 GPU、不挂代理，使用智谱免费视觉模型额度。

## Product Purpose

把化学课件幻灯片（PDF/PPT）转成**连续的、格式钉死的 Markdown 讲义**。成功标准：一份交付物只需人工微调与复核即可使用；化学式、方程式、表格的写法由程序保证一致，而不是由模型临场发挥。

## Positioning

同赛道产品无法轻易复制的机制组合：**不做 OCR + 版面分析传统流水线**，而是把每页 PDF 渲染成 JPEG 直接交给视觉大模型「看图写讲义」，再用一层确定性后处理把化学式、方程式、表格格式钉死。不需要 GPU、不需要本地权重、不需要代理，默认降级链 `glm-4.6v-flash → glm-4v-flash` 全程不花额度。CLI 与桌面 GUI 共享同一条转换链路，产物逐字节一致。

## Operating Context

- 运行：Windows 本机，`python main_gui.py`（GUI）或 CLI 入口（`handout_maker.py` / `batch.py` / `normalize.py` / `verify.py`）。
- 产物阅读环境：Obsidian、VS Code 预览、教学平台等带 KaTeX/MathJax 的渲染器；`verify.py --html` 生成的预览页自带 KaTeX CDN。
- 交付物契约由《讲义格式规范》（`讲义格式规范.md`，由四份已定稿讲义归纳而来）与 README 第 2 节共同定义。
- PPT 输入依赖外部工具（LibreOffice headless 或 PowerPoint COM + pywin32），缓存于 `%TEMP%\HandoutMaker2\ppt_cache`。
- 转换刻意**永远串行**（智谱限流是并发维度）；逐页渲染 → 逐页请求 → 逐批落盘。
- 真实联调素材：`test1.pdf`（15 页示例课件）。

## Capabilities and Constraints

已确认能力：单文件转换与批量转换（断点续传、JSON 报表、体检、HTML 预览）、GUI 拖拽导入（PDF/PPT/文件夹）、模型配置档案（GLM / Gemini / OpenAI 兼容三引擎，JSON 持久化、Key 打码）、实时日志与逐页进度、文件级暂停 / 请求级终止、.part 半成品语义（已落盘页面不丢）。

硬约束（未来工作必须保留）：

- 输出契约：表格结构用原生 HTML；表格内外化学记号一律行内 LaTeX `$...$` + `\text{}`；禁用裸化学式、`<sub>/<sup>`、`\ce{}`、管道表。
- 方程式一律等号 `=`；可逆用 `\rightleftharpoons`；`→` 只用于转化/流程；条件压等号上下（`\overset` / `\underset`）。
- 交付物是连续讲义：无「第 N 页」标题、无 `---` 分隔线；文件名 = 全文唯一 H2 标题（`[N-M]知识点1+知识点2`）。
- 串行执行，不引入并发批量（限流语义依赖串行）。
- 归一化流水线顺序不可随意调换（删 HTML 注释必须先于「箭头→等号」；表格抽占位符处理）。
- GUI 解耦规则：底层脚本只允许 `gui/core/bridge.py` import；展示层不 import 底层。
- 已知限制（承认并保留）：模型可能幻觉（数值/实验条件需人工复核）；结构式/装置图不重建，只留 `>[图示: ...]` 占位；`glm-4v-flash` 输出上限 1024 tokens；渲染环境必须挂 KaTeX/MathJax；同目录同名 `.md` 会被覆盖。

## Brand Commitments

暂无品牌承诺。「Handout-Maker2 · 化学讲义批量工厂」仅为内部代号，未来设计不将其视为不可改动的品牌事实。

## Evidence on Hand

- `test1.pdf`：15 页真实课件，示例输入与联调素材。
- `讲义格式规范.md`：交付物契约的权威规范（含四份定稿讲义的结构速览与现状偏差清单）。规范引用的四份定稿讲义（`[3-1]`…`[3-4]`）**不在本仓库内**，未来工作不得虚构其内容。
- `docs/gui_preview.png`：GUI 截图（现状参考）。
- `tests/`：`selfcheck.py` 227 项断言、`gui_smoke.py` 离线冒烟、`gui_live.py` 真机联调。
- 真实 API 实测记录（README 第 4.6 节）：模型可用性、错误码分类、写法怪癖，均为实测证据。
- 缺失且不得伪造：用户口碑、外部评测、分发数据。

## Product Principles

1. **确定性兜底**：模型输出只是原料，格式由后处理钉死——「一次解析、多种渲染」，正文与表格不会出现同一种物质两种长相。
2. **产物即契约**：交付物必须逐项符合《讲义格式规范》；转换只是起点，人工微调与体检（`verify.py`）是流程的一部分。
3. **零门槛、不花额度**：免费视觉模型 + 自动降级链，无需 GPU/权重/代理，个人 Windows 本机开箱即用。
4. **串行与可续传**：限流友好的串行执行；增量落盘与 `.part` 语义保证任何中断都不丢已完成的页面。
5. **界面不重写底层**：GUI 与 CLI 共享同一条转换链路、产物逐字节一致；底层脚本原样可用，GUI 只做编排与呈现。

## Accessibility & Inclusion

无已确认的特定无障碍要求；界面语言为中文，运行于 Windows 桌面环境。未建立的要求不在此处虚构。
