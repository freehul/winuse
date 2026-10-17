# WinUse

中文说明 · **[English](README.md)**

**给 AI 智能体的桌面能力统一门面。** cua-driver 是引擎，WinUse 是方向盘。

WinUse 自己不做驱动——不注入鼠标键盘、不读像素、不管窗口句柄。它只做一件事：把 [cua-driver](https://github.com/trycua/cua) 的驱动级原语，翻译成智能体能**安全、省钱、准确**调用的粗粒度工具。

---

## 为什么需要它

把驱动层直接交给智能体，会付三种代价：

| | |
|---|---|
| **贵** | 默认走「截图 + 视觉模型」，看一眼约 2,100–2,700 个词元 |
| **不准** | 混合缩放的多屏环境下，截图像素与原生坐标必须换算；换算错了，点击就落到别处 |
| **险** | 任何智能体都能直接调驱动，绕过审批门禁 |

WinUse 的答案：

1. **文本优先**：默认读 Windows 无障碍树（UIA）。截图是**显式升级**动作，永远不是默认路径。可交互元素只占整棵树的一小部分（实测约 38%），只读文本一律裁掉。
2. **元素优先于坐标**：语义动作（`Invoke` / `ValuePattern`）与坐标、缩放、屏幕无关。像素点击只留给自绘界面兜底（画布、视频、WebGL、自绘控件）。
3. **动作 → 门禁 → 验证**：危险操作过内部确认门，并写入审计日志。动作下发后拿到的是**确定性断言**，不是「看起来动了」。

## 为什么更省钱——实测，不是宣称

| 取状态的方式 | 词元 |
|---|---|
| 4,804 个元素的窗口：全量快照再裁剪 | 约 14,788 |
| 同一个窗口：用 `query` 按需投影 | 约 2,285 |

省下来的是**按需投影**，不是有损截断：你只要需要的那几行，元素索引仍在同一个快照内有效。

## 工具（22 个）

### 看——不靠像素也知道有什么

| 工具 | 作用 |
|---|---|
| `winuse_state` | 单窗口文本优先状态：可交互元素（带 `element_index`、`element_token`、`snapshot_id`）、窗口清单、真前台窗口 |
| `winuse_find` | 按文本模糊找元素——记错一个字也能命中 |
| `winuse_read` | 一次往返读多个元素的值 / 状态 |
| `winuse_display` | 每块屏的物理矩形、工作区、各屏独立缩放与 DPI、窗口归属 |
| `winuse_screenshot` | 截图落盘（显式升级；唯一的像素通道） |
| `winuse_ocr` | Windows 自带 OCR——不联网、零第三方依赖 |
| `winuse_search` | 通过 Everything（`es.exe`）全盘搜文件，只读 |

### 动——一个手势，按元素寻址

| 工具 | 作用 |
|---|---|
| `winuse_act` | 元素级动作：`click` / `invoke` / `toggle` / `select` / `expand` / `set_value` / `type` / `key` / `scroll` / `activate`——**默认只干跑** |
| `winuse_drag` | 拖拽（按住 → 移动 → 松开），元素到元素或按坐标 |
| `winuse_multi` | 一次下发多步界面动作——**批量输入一律算危险动作** |
| `winuse_menu` | 按路径调用菜单项，逐层解析 |
| `winuse_window` | 窗口生命周期：最小化 / 最大化 / 还原 / 置顶 / 移动缩放 / 关闭 |
| `winuse_app` | 应用生命周期：启动（不抢焦点）/ 列举 / 结束 |

### 等待、验证与系统事实

| 工具 | 作用 |
|---|---|
| `winuse_wait` | 条件等待：窗口出现 / 消失、标题匹配、元素出现、文字出现 |
| `winuse_verify` | 对单个窗口做确定性断言（谓词数组，三态结果——`unknown` 绝不等于成功） |
| `winuse_system` | 系统盘点：操作系统 / 处理器 / 内存 / 磁盘 / 电源，以及内存占用前几名的进程 |
| `winuse_session` | 会话与桌面状态：是否锁屏、是否安全桌面、各能力当前是否可用 |
| `winuse_virtual_desktop` | 窗口属于哪个虚拟桌面、是否在当前桌面 |
| `winuse_registry` | 读注册表（默认只读；敏感分支直接拒绝） |
| `winuse_clipboard` | 读 / 写剪贴板——`read` 默认只列类型，要正文需显式指定 |
| `winuse_notify` | Windows 通知气泡——默认只干跑 |
| `winuse_audit` | 读 WinUse 自己的审计日志（只读） |

## 环境要求

| 项目 | 必要性 | 说明 |
|---|---|---|
| Windows 10 / 11 | 必需 | 本项目按设计只支持 Windows |
| Python ≥ 3.10 | 必需 | **运行时零第三方依赖**——只用标准库（Windows 接口走 `ctypes`） |
| [`cua-driver`](https://github.com/trycua/cua) ≥ 0.28.0 | **必需** | 唯一驱动。按 `~/.cua-driver/packages/current` → `%LOCALAPPDATA%/Programs/Cua/...` → `PATH` 顺序自动探测，也可设 `WINUSE_CUA_DRIVER` |
| `powershell.exe`（Windows PowerShell 5.1） | OCR / 通知必需 | PowerShell 7（`pwsh.exe`）**不能替代**——没有内建 WinRT 投影 |
| `msg.exe` | 可选 | 仅通知的兜底通道 |

「零依赖」是**刻意的设计约束**，不是「还没加」：核心与每个门面都只用标准库，所以任何 Python 都能跑，也不会污染项目外的环境。

## 安装

```bash
git clone https://github.com/freehul/winuse.git
cd winuse
python -m winuse doctor              # 自检：配置 -> 驱动 -> 一次真实只读探针
python -m winuse doctor --no-probe   # 只验驱动版本，不做真实调用
```

用命令行不需要安装步骤。要作为包安装（并跑测试）：

```bash
python -m pip install -e ".[test]"   # 测试依赖在这里声明，运行时永不需要
python -m pytest -q
```

`scripts/verify.sh` 一次跑完（编译 -> 测试 -> 警告升级）：

```bash
sh scripts/verify.sh                 # PYTHON='py -3.11' 可指定解释器
sh scripts/verify.sh --doctor        # 额外跑一次真机只读自检
```

可选：把 `.env.example` 复制为 `.env` 覆盖默认配置。`.env` 永不入库。

## 在自己的智能体里使用

### 任意 MCP 客户端

```bash
python -m winuse mcp serve           # 标准输入输出上的 MCP 服务
python -m winuse mcp tools           # 列出工具表
```

### Hermes（插件适配器）

```bash
python adapters/hermes/install.py --home <HERMES_HOME>
hermes plugins enable winuse
hermes plugins list                  # 确认已启用
```

部署模型与两道安全门见 `adapters/hermes/README.md`。

### 纯命令行

```bash
python -m winuse state               # 前台窗口的文本优先状态
python -m winuse display             # 多屏事实
python -m winuse act --help          # 所有动作在 --execute 之前都是干跑
```

## 安全模型

- **默认干跑。** `winuse act` 只打印将要下发的命令；只有 `--execute`（命令行）或 `execute: true`（MCP）才真正动桌面。
- **确认门在核心里。** 破坏性与批量操作必须显式 `confirm`，且门禁写在核心而非门面——新客户端无法「忘记」它。
- **审计日志。** 每个执行的动作追加到 `~/.winuse/audit.jsonl`（路径与轮转可配）。
- **快照绑定。** `element_index` 只在产生它的 `snapshot_id` 内有效；不带同源快照的动作会被拒绝（`SNAPSHOT_REQUIRED`），防止拿过期索引点到别的元素。
- **一套驱动，不装第二套。** 第二套会撞焦点、撞坐标口径、绕过审批门禁。
- **不起常驻 HTTP / SSE 服务。** 进程内用走子进程，对外走标准输入输出上的 MCP——不占端口、不对外暴露。

## 目录结构

```
winuse/
  config.py          配置（只读项目内 .env，刻意不读进程环境变量）
  cli.py             门面 3：命令行（兜底 + 排查）
  driver/
    cua.py           cua-driver 调用封装——唯一的 IO 边界
    errors.py        稳定错误码 + 人话提示
adapters/hermes/     Hermes 插件适配器——只做协议翻译，不含业务逻辑
scripts/             verify.sh、端到端冒烟、钩子安装器、Windows 包装脚本
tests/               单元测试；一律用假驱动，不碰真实桌面
```

设计契约、待办与调研笔记作为内部文档维护，**不随本仓库发布**——代码与这份说明就是公开面。

## 跑测试

单元测试从不接触真实驱动与桌面：`tests/fake_driver.py` 与各用例里的桩驱动替代它。真机验证走 `python -m winuse doctor` 与受控探针。

```bash
python -m pytest -q            # 唯一验收命令
python -m pytest -q -W error   # 把警告当错误：协议 / 资源类问题常只在这里暴露
```

## 参与贡献

欢迎提 issue 与合并请求。项目规则（零第三方依赖、只有一套驱动、测试先行、数据卫生）见 `AGENTS.md`；验收入口是 `sh scripts/verify.sh`——没跑绿就不算完成。

数据卫生由提交钩子兜底：每台机器执行一次 `sh scripts/install_git_hooks.sh` 启用。

## 许可证

MIT——见 [LICENSE](LICENSE)。
