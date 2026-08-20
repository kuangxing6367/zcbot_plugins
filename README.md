# ZCBOT 官方插件源

ZCBOT 官方插件市场仓库，与框架仓库（[zcbot](https://github.com/kuangxing6367/zcbot)）配套使用。

通过「插件市场」搜索对应插件名即可一键安装。无需手动 git clone。

## 插件清单（共 24 个）

| 插件 | 版本 | 作者 | 说明 |
| ---- | ---- | ---- | ---- |
| broadcast | 1.0.0 | ZGRIC | 全局广播：定时向所有群/私聊发送消息，支持文本/图片/文件，可配置发送间隔 |
| custom_ui | 1.1.1 | ZGRIC | 从 GitHub 拉取网页模板接管 Web 面板，提供模板选择/下载/安装/切换 |
| dbcj-mcstatus | 1.2.1 | ZGRIC | Minecraft 服务器状态探测：/mcstatus 查询在线人数、延迟、 motd，支持多服务器 |
| echo | 1.0.0 | ZGRIC | 原样返回用户文本消息，无参数时返回 PONG |
| file | 1.3.0 | Chris | 文件操作：发送、删除、移动、复制、查看文件夹内容、删除目录、上传（仅超管） |
| fun_score | 2.1.0 | zgric | 签到积分：每日签到/积分/排行榜/等级/违禁词检测/LLM 工具（Pillow 图片渲染版） |
| help | 1.0.0 | tinker | 查询所有已注册命令，生成图片帮助菜单 |
| hitokoto | 1.0.0 | ZGRIC | 一言：/一言 随机返回一句经典语录，支持分类筛选（动画/文学/哲学等） |
| image_renderer | 1.1.0 | ZGRIC | 通用图片渲染引擎（原生 Rust 扩展自动加载，缺失自动回退 PIL），提供卡片/文本绘制工具 |
| keyword_api | 1.0.0 | ZGRIC | 关键词 API 回复：配置关键词触发规则，调用外部 API 返回动态内容 |
| llm_blacklist | 1.0.0 | ZGRIC | LLM 对话黑名单管理（超管 /插件拉黑 <QQ号> 拉黑，预防上下文滥用） |
| llm_chat | 1.8.0 | ZGRIC | QQ 内大模型对话：全异步、自动上下文记忆、AI 函数调用、人格预设、好感度、长期记忆 + 自研 WebUI 控制台（/chat 或直接 @机器人） |
| llm_plugin_gen | 1.7.0 | ZGRIC | LLM 插件生成器：对话式开发插件，AI 辅助编写代码、调试、部署，支持 WebUI 控制台 |
| message_guard | 1.0.0 | ZGRIC | 消息防护：限流/防刷/敏感词过滤，保护机器人不被滥用，支持黑白名单 |
| minecraftconsole | 1.6.1 | zgric | Minecraft 控制台：远程控制 MC 服务器，执行命令、查看日志、玩家管理 |
| plugin_depgraph | 1.0.0 | ZGRIC | 扫描插件间依赖关系（DB 统一管理），/依赖 文本树、/依赖图 图片 |
| plugin_memmon | 1.1.0 | ZGRIC | 插件内存统计 + 进程内存诊断（/mem 统计 /memdiag 诊断报告），WebUI 展示 |
| qqadmin | 1.1.0 | ZGRIC | QQ 群管插件：禁言/踢人/全员禁言/精华/公告/宵禁/违禁词(本地+API)/进群管理/协管等 |
| restart_manager | 1.0.1 | zgric | 超级管理员 /重启 原地重启框架，完成后回执内存占用 |
| runtime_status | 1.1.0 | ZGRIC | 框架运行状态监控，提供 /status /info /help /uptime /plugins 命令，支持 Web UI 配置 |
| send_like | 3.0.0 | ZGRIC | 点赞插件：普通用户 0~8 随机赞，超管每天固定 10 赞，支持自动点赞 |
| session_waiter | 1.0.0 | ZGRIC | 多轮会话基础设施：插件可等待用户下一条消息（wait_for_user） |
| ui_ext_demo | 1.0.0 | ZGRIC | 演示群组/用户管理页插件扩展（列 + 详情面板） |
| video_parse | 1.0.0 | ZGRIC | 自动解析群内视频分享链接，返回信息卡片（封面图+文字，不含直链）；已注册为 LLM AI 函数 |

---

**提示**: 开发文档见框架仓库 [docs](https://github.com/kuangxing6367/zcbot/tree/main/docs)。开源协议 MIT。
