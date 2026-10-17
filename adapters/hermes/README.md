# WinUse · Hermes 插件（门面 1）

把 WinUse 的三个粗粒度工具挂进 Hermes 工具表。**瘦门面**：这里只有参数翻译，
业务逻辑全在项目核心 `<项目>/winuse/`（唯一边界 `winuse/driver/cua.py`）。

## 安装 / 卸载

```bash
# 部署（幂等，可反复跑；会记录核心位置到部署位的 _location.py）
python adapters/hermes/install.py --home <HERMES_HOME>

# 只校验部署副本与项目源是否一致（只读，不改任何文件）
python adapters/hermes/install.py --home <HERMES_HOME> --verify

# 插件默认 opt-in，必须显式启用
hermes plugins enable winuse
hermes plugins list          # 确认已启用
```

路径写错会被拦住：目标落在**项目目录内**、或已存在却不像 Hermes 主目录时，部署直接拒绝
（`--force` 可跳过）。这条门是为一次真实事故加的——在 git-bash 里用反斜杠写 `--home <HERMES_HOME>`
时反斜杠被 shell 吃掉、参数退化成裸目录名，副本就这样装进了项目目录且没有任何报错。
**推荐用正斜杠写路径**，或给整个路径加引号。

卸载：`python adapters/hermes/install.py --uninstall` 然后 `hermes plugins disable winuse`。

## 目录角色

| 路径 | 角色 |
|---|---|
| `<项目>/adapters/hermes/` | **源码唯一副本**，随项目 git 管理、随项目分发 |
| `<HERMES_HOME>/plugins/winuse/` | 部署副本，由 `install.py` 生成，**可随时重建**，不属于版本管理对象 |

改插件代码只改项目内这一份，然后重跑 install.py 同步。

副本是**静态文件**，改了源码忘了重跑就会**静默过期**：工具表由核心现读、看着是新的，
运行中的 agent 用的却是旧翻译层（「schema 新、翻译层旧」的半新错位）。两道兜底——
`install.py` 部署后自带自检，`scripts/verify.sh` 也会比一次（本机未部署则跳过并说明）。

## 工具

| 工具 | 作用 | 安全默认 |
|---|---|---|
| `winuse_state` | 文本优先取状态：可交互元素 + 索引/令牌/快照、窗口清单、真前台窗口 | 不下发截图；只读文本元素已裁掉 |
| `winuse_act` | 元素级动作：click / invoke / toggle / select / expand / set_value / type / key / scroll / activate | **默认干跑**，`execute: true` 才真动 |
| `winuse_display` | 多屏事实：每屏物理矩形、工作区、**各屏独立缩放**、窗口归属 | 只读 |

## 两条使用纪律

1. **索引必须绑定快照**：`winuse_state` 拿到的 `element_index` 只在同一次返回的
   `snapshot_id` 内有效，动作时一起传；缺快照会被明确拒绝（`SNAPSHOT_REQUIRED`），
   防止拿过期索引点到别的元素。
2. **大窗口必须按需取**：窗口元素成千时用 `query` 只取匹配元素（只回匹配行 + 祖先链）。
   实测：4804 个元素的窗口，全取再裁 ≈ 14,788 token，按需投影后 ≈ 2,285 token。

## 为什么走子进程

核心只有一份实现，插件不 vendored 副本（防漂移）；代价是每次调用多一次 Python 启动
（约 150–300ms）。核心调用失败一律返回结构化 JSON（带稳定 code），不抛异常给对话流程。
