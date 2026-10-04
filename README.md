# ZCBOT 官方插件源

ZCBOT 官方插件市场仓库，与框架仓库（[zcbot](https://github.com/kuangxing6367/zcbot)）配套使用。

通过「插件市场」搜索对应插件名即可一键安装。无需手动 git clone。

## 插件清单（共 39 个）

| addgroup | 1.0.0 | ZGRIC | 生成群邀请链接、机器人主动退群 |
| admin_tools | 1.0.0 | ZGRIC | 注册禁言/解禁/踢人为 llm_core AI 函数，模型经权限校验后协助群管 |
| allinone | 1.0.0 | ZGRIC | Base64/时间戳/随机数/哈希等本地小工具集合 |
| attool | 1.0.0 | ZGRIC | @全体成员 / @多人工具，群管消息触达 |
| auto_invite | 1.0.0 | ZGRIC | 自动接受邀请机器人进群，可选自动通过加群申请 |
| ban_plugins | 1.0.0 | ZGRIC | 超级管理员对任意插件做群级/全局启停管理 |
| broadcast | 2.0.0 | ZGRIC | 超管广播通知套件：向全部/指定群与好友发送通知，支持引用消息转发（文本/图片/文件）、定时周期广播与发送间隔配置 |
| custom_ui | 1.1.1 | ZGRIC | 从 GitHub 拉取网页模板接管 Web 面板，提供模板选择/下载/安装/切换 |
| emoji_kitchen | 1.0.0 | ZGRIC | 将两张 emoji 融合为组合贴纸图片 |
| favour_ultra | 1.0.0 | ZGRIC | 用户与机器人的好感度：签到/赠送/排行 |
| funbox | 1.0.0 | ZGRIC | 本地语料娱乐合集：笑话/绕口令/脑筋急转弯/星座/抽签/今日运势/名言/趣味测试/CP 关系缘分 |
| group_watch | 1.1.0 | ZGRIC | 管理员变动/成员进出/禁言/撤回等群事件主动提示，按群开关，昵称解析 |
| guard | 1.0.0 | ZGRIC | 统一防护套件：全局黑名单/群白名单与唤醒词/敏感词/限流/刷屏检测/LLM 对话拦截，一条拦截链集中管理 |
| help | 1.0.0 | tinker | 查询所有已注册命令，生成图片帮助菜单 |
| image_renderer | 1.1.0 | ZGRIC | 通用图片渲染引擎（原生 Rust 扩展自动加载，缺失自动回退 PIL），提供卡片/文本绘制工具 |
| keyword | 1.0.0 | ZGRIC | 关键词/正则统一触发引擎：自动回复、外部 API 调用、监控私聊通知，支持本群/全局作用域、启用禁用与命中统计 |
| kk_tts | 1.0.0 | ZGRIC | 调用枫雨恶搞语音TTS接口，文字转抽象语音并转码Silk后发送；已注册为LLM AI函数 |
| link_parser | 1.0.0 | ZGRIC | B站/视频分享/网页链接统一解析：自动识别平台分发，支持 LLM 函数 |
| llm_file_upload | 1.0.0 | ZGRIC | 给 LLM 一个发送文件的函数：按 URL 下载并发送图片/语音/视频/文件，同时提供 /发文件 命令 |
| llm_logger | 1.0.0 | ZGRIC | 记录每次 LLM 对话的次数/耗时/工具调用统计，提供 /对话日志 /对话统计 查询 |
| llm_plugin_gen | 1.7.0 | ZGRIC | 对话式 LLM 插件开发（AI 项目助手工作流 V1.0：澄清疑点→6 项待办确认→逐项广播→stop/continue 干预，全异步；支持检查/更新框架源码） |
| markdown_killer | 1.0.0 | ZGRIC | 去除消息中的 Markdown 语法，提供纯文本；支持群内自动模式 |
| member | 1.0.0 | ZGRIC | 群成员与发言数据中枢：消息流水记录、成员索引/查询、发言排行卡片图、成员 CSV 导出 |
| meme | 1.0.0 | ZGRIC | 调用 MemeGen 生成上下文字表情包图片 |
| music | 1.0.0 | ZGRIC | 搜索歌曲并以音乐卡片返回可播放链接（Meting 公共接口） |
| payqr | 1.0.0 | ZGRIC | 生成收款信息二维码（可配置收款方） |
| picture_manager | 1.0.0 | ZGRIC | 保存/取出图片的本地图库（自动记录最近图片） |
| pig | 1.0.0 | ZGRIC | 群内养猪小游戏：认领/投喂/遛弯/屠宰换金币，带全服排行榜 |
| plugin_depgraph | 1.0.0 | ZGRIC | 扫描插件间依赖关系（DB 统一管理），/依赖 文本树 /依赖图 图片 |
| poke | 1.0.0 | ZGRIC | 有人戳机器人时趣味回应，可开启反戳或自定义语料 |
| qqadmin | 1.2.0 | ZGRIC | QQ群管插件：禁言/踢人/全禁/精华/公告/宵禁/违禁词/进群管理/协管/群打卡/数据导出/事件监听/开关机 |
| qzone | 1.0.0 | ZGRIC | 以机器人账号发布/读取 QQ 空间说说（需配置 QZone Cookie） |
| rail_query | 1.0.0 | ZGRIC | 按车次查询列车经停站、到发时间与历时 |
| remote_admin | 1.0.0 | ZGRIC | 超级管理员私聊下发指令远程管理群/广播/查状态 |
| restart_manager | 1.0.1 | zgric | 超级管理员 /重启 原地重启框架，完成后回执内存占用 |
| send_like | 3.0.0 | ZGRIC | 点赞插件：普通用户0~8随机赞，超管每天固定10赞，支持自动点赞 |
| session_waiter | 1.0.0 | ZGRIC | 多轮会话基础设施：插件可等待用户下一条消息（wait_for_user） |
| sysmon | 2.0.0 | ZGRIC | 运行监控套件：框架状态/系统资源/插件内存统计与诊断（/status /info /uptime /plugins /mem /memdiag），支持状态卡片图、仪表盘卡片与 WebUI 页面 |
| weather | 1.0.0 | ZGRIC | 基于 wttr.in 的城市天气查询（当前+3天预报） |

---

## WebUI 模板（共 8 套）

由 `custom_ui` 插件管理：面板内「个性化前端」页可一键下载/切换，模板源即本仓库 `webui/` 目录。

| 模板 | 风格 | 说明 |
| ---- | ---- | ---- |
| default | 现代深色 | 默认完整仪表盘：系统资源、插件管理、实时日志 |
| bigscreen | 中控大屏 | 深色霓虹大屏风格，适合挂机监控展示 |
| logs | 终端日志 | 日志实时查看器，级别筛选/暂停/清空 |
| realtime | 实时信息 | 信息卡片 + 插件列表 + 日志流，紧凑单页 |
| terminal | 黑客终端 | 绿字黑底命令行风格，仿 SSH 终端 |
| gaming | 电竞霓虹 | 粉紫青霓虹渐变，电竞风格控制台 |
| minimal | 极简白 | 浅色极简风格，清爽无干扰 |
| retro | 复古像素 | 像素字体 8-bit 游戏机风格 |

---

**提示**: 开发文档见框架仓库 [docs](https://github.com/kuangxing6367/zcbot/tree/main/docs)。开源协议 MIT。
