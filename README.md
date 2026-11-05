# Personal Workspace 个人工作台

以「今天」为主入口、以时间为线索的个人工作与生活管理工作台。
待办 · 日程 · 提醒 · 每日总结 · 收集箱 · 重要日子 · 时间轴 · 工作台概览。

技术栈：**Python 3.9+**；本地 `ThreadingHTTPServer` + 单文件 webui（vanilla JS）+
**pywebview 原生窗口（WebView2）** + SQLite 存储 + PyInstaller 打包 onefile exe。
服务与网页版仍只用标准库；只有 `--app` 原生窗口需要 pywebview，缺失时自动回退
Chromium `--app` 窗口。

## 快速开始

```text
python run.py                 # 默认 http://127.0.0.1:8766，自动开浏览器
python run.py --app           # 独立客户端窗口（无地址栏，像桌面应用）
python run.py --port 8899     # 换端口（被占用自动往后找）
python run.py --no-browser    # 只起服务
```

命令行入口（等价 `python -m personal_workspace`）：

```text
python -m personal_workspace today    # 今日概览
python -m personal_workspace list     # 待办分组列表
python -m personal_workspace backup   # 立即备份
python -m personal_workspace stats    # 数据表行数
```

## 打包 exe

```text
pip install -r requirements-dev.txt      # pyinstaller + pywebview
python -m PyInstaller --noconfirm --clean PersonalWorkspace.spec
dist\PersonalWorkspace.exe               # 双击即用（窗口版，无控制台）
```

客户端窗口使用系统自带的 Edge WebView2 运行时（Windows 11 自带；Windows 10
随 Edge 安装）。重新打包前先关掉正在运行的 exe（Windows 文件锁）。

## 数据与目录

```text
%LOCALAPPDATA%\PersonalWorkspace\
├── workspace.db      # SQLite（WAL 模式；时刻存 epoch 秒，纯日期存本地 ISO 日期）
├── Backups\          # 「设置 → 立即备份」的备份文件（SQLite Backup API），最多保留最近 7 份
├── Logs\app.log      # 日志（每天滚动，保留最近 15 天）
├── webview-<端口>\   # 原生客户端窗口（WebView2）的浏览器数据
└── app-profile-<端口>\  # 回退 Edge 窗口时的浏览器 profile
```

环境变量 `PWS_DATA_DIR` 可把整个数据目录指到别处（测试/便携使用）。

## 项目结构

```text
run.py                            # 开发与 exe 共用入口
PersonalWorkspace.spec            # PyInstaller 配置
src/personal_workspace/
├── db.py                         # 连接 / 建表迁移 / 备份恢复
├── core.py                       # 业务规则（日期、完成历史、提醒调度数据面…）+ CLI
├── server.py                     # HTTP 服务、/api 路由、应用窗口生命周期、调度线程
├── notify.py                     # PowerShell 弹 Windows Toast（后台驻留时用）
└── webui/                        # 单文件界面（index.html + vendor/lucide.min.js）
tests/test_rules.py               # 核心规则单测（python -m unittest discover -s tests）
doc/                              # 产品需求与原型文档
个人工作台-交互原型.html           # 高保真交互原型（界面的设计来源）
```

## 设计要点（与 PRD 对应）

- **今天优先**：启动页默认「今天」，无计划日期的待办不挤进今天列表。
- **原生窗口**：`--app` 用 pywebview + WebView2 开自己的窗口，窗口属于本进程，
  任务栏图标和固定项都是应用自己的；pywebview/WebView2 不可用时自动回退
  Chromium `--app`。
- **待办 ≠ 日程**：`biz_task` 与 `biz_calendar_event` 分表，日程可关联待办。
- **逾期是展示状态**：由 planned_date/due_date 与状态实时计算，不改任务状态字段。
- **完成历史不可覆盖**：`biz_task_history` 记录 created/status/edit 流水。
- **日报联动**：每日总结逐行自动核对待办（同名勾掉、没有的新建，幂等）；
  按「同一天 + 标题」去重，删行/改名同步日报生成的完成记录，未来日期只存文字；
  昨晚「明日计划」第二天出现在今天页，可逐条/全部转为待办。
- **提醒**：调度线程每 15s 把到期提醒置「已触发」；窗口内轮询横幅 + 系统通知；
  重启后自动补查错过的提醒。设置里可开「后台驻留」（关窗后服务继续，PowerShell 弹 Toast）。
- **安全**：只绑 127.0.0.1，Host/Origin 校验，静态文件白名单，body 上限 1MB。
- **备份恢复**：SQLite Backup API；恢复前自动再备份当前库。

## 版本

V1.2.1 —— 待办列表按时间倒序：已完成行带上完整年月日（如「2026年11月5日 16:14 完成」），今天完成的数据一眼可辨。

V1.2.0 —— 每日总结跨天自动回到今天：页面一直开着跨过零点，第二天进入页面或窗口重新获得焦点时自动切到新的今天；手动翻往日/未来时保持不动。

V1.1.0 —— 客户端窗口换成 pywebview 原生窗口（WebView2）：窗口属于本进程，固定到任务栏不再变成 Edge 图标；关窗/完全退出的生命周期保持不变。

V1.0.5 —— 日报逐条对齐：别的日期删掉的同名任务不再挡住今天日报记入，日报 8 行对应今天页 8 条完成。

V1.0.4 —— 修复「每日总结」与「今天页」完成数据不一致：日报按天各记一条、删行/改名双向同步、未来日期不再生成完成记录。

V1.0.3 —— 关闭 Edge 内置组件扩展的后台渲染进程、GPU 进程并入主进程，客户端内存占用约降低一半。

V1.0.2 —— 客户端窗口默认最大化；限制浏览器渲染进程数与 V8 堆，降低常驻内存占用。

V1.0.1 —— 设置增强：日志按天滚动、保留最近 15 天并支持一键清除；备份最多保留最近 7 份；修复文件日志未实际写入的问题。

V1.0.0 —— PRD V1.0 范围全量：今天 / 快速创建 / 收集箱 / 待办 + 详情 / 日历（日·周·月）/
提醒中心 / 每日总结 / 搜索 / 分类标签 / 重要日子 / 时间轴 / 工作台 / 设置 / 备份恢复。
