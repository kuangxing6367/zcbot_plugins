# ZCBOT 官方插件源

ZCBOT 官方插件市场仓库，与框架仓库（[zcbot](https://github.com/kuangxing6367/zcbot)）配套使用。

## 快速使用

1. 克隆本仓库，或在 ZCBOT 管理面板的「插件市场」中安装：
   ```bash
   git clone https://github.com/kuangxing6367/zcbot_plugins.git
   ```
2. 在框架 `config.yaml` 中把插件目录指向本仓库的 `plugins/`：
   ```yaml
   plugin:
     dir: ../zcbot_plugins/plugins
   ```
3. 重启框架（或面板「插件」页重载）即可加载全部插件。

> 插件市场索引见 [registry.json](./registry.json)，ZCBOT 管理面板据此安装/更新插件。

## 插件清单（16 个）

| 插件 | 版本 | 作者 | 说明 |
| ---- | ---- | ---- | ---- |
| llm_chat | 1.8.0 | ZGRIC | QQ 内大模型对话：全异步、自动上下文记忆、AI 函数调用、人格预设、好感度、长期记忆 + 自研 WebUI 控制台 |
| qqadmin | 1.1.0 | ZGRIC | QQ 群管：禁言/踢人/全员禁言/精华/公告/宵禁/违禁词(本地+API)/进群管理/协管 |
| fun_score | 2.1.0 | zgric | 签到积分：每日签到/积分/排行榜/等级/违禁词检测/LLM 工具（Pillow 图片渲染版） |
| video_parse | 1.0.0 | ZGRIC | 自动解析群内视频分享链接，返回信息卡片；已注册为 LLM AI 函数 |
| file | 1.3.0 | Chris | 文件操作：发送、删除、移动、复制、查看目录、删除目录、上传（仅超管） |
| send_like | 3.0.0 | ZGRIC | 点赞：普通用户 0~8 随机赞，超管每天固定 10 赞，支持自动点赞 |
| image_renderer | 1.1.0 | ZGRIC | 通用图片渲染引擎（原生 Rust 扩展自动加载，缺失回退 PIL），卡片/文本绘制 |
| help | 1.0.0 | tinker | 查询所有已注册命令，生成图片帮助菜单 |
| echo | 1.0.0 | ZGRIC | 原样返回用户文本消息，无参数时返回 PONG |
| runtime_status | 1.1.0 | ZGRIC | 运行状态监控：/status /info /help /uptime /plugins，支持 Web UI 配置 |
| restart_manager | 1.0.1 | zgric | 超管 /重启 原地重启框架，完成后回执内存占用 |
| llm_blacklist | 1.0.0 | ZGRIC | LLM 对话黑名单管理（/插件拉黑 <QQ号>），预防上下文滥用 |
| plugin_depgraph | 1.0.0 | ZGRIC | 插件依赖关系扫描（DB 统一管理），/依赖 文本树、/依赖图 图片 |
| plugin_memmon | 1.1.0 | ZGRIC | 插件内存统计 + 进程内存诊断（/mem /memdiag），WebUI 展示 |
| session_waiter | 1.0.0 | ZGRIC | 多轮会话基础设施：插件可等待用户下一条消息（wait_for_user） |
| ui_ext_demo | 1.0.0 | ZGRIC | 演示群组/用户管理页插件扩展（列 + 详情面板） |

## 开发约定

- 一个插件 = 一个文件夹，入口文件 `main.py`，通过 `__plugin_meta__` 声明元信息。
- 需要 Web 配置页的插件，可在插件目录放 `_conf_schema.json`。
- 开发文档见框架仓库 [docs](https://github.com/kuangxing6367/zcbot/tree/main/docs)。

## 开源协议

MIT License。
