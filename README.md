# KiraAI History Plugin（跨会话历史消息插件）

> 提供工具让 AI 可以跨会话获取群聊/私聊消息历史，支持 NapCat / LLOneBot / SnowLuma 三种 OneBot 实现。

## 这是什么？

让机器人能查看指定群聊或私聊的聊天记录，用于总结、分析、转发等场景。支持图片 URL、消息 ID 等完整信息。

## 特性

- ✅ **跨实现兼容**：NapCat / LLOneBot / SnowLuma 都能稳定获取真实历史
- ✅ **双通道**：WS 通道优先（与适配器同一 ID 命名空间，get_msg 可反查），HTTP 通道兜底
- ✅ **强解析**：引用消息显示 `[引用 msg_id:xxx]`，图片带真实 URL，不再出现 `[引用消息]` 占位
- ✅ **自动过滤**：无法解析的空引用占位消息自动过滤，不污染 LLM 上下文
- ✅ **get_msg 刷新**：媒体源缺失的消息自动调 get_msg 刷新（SnowLuma 会刷新图片 URL）
- ✅ **防循环**：同一会话 120 秒内递减重试自动拦截，防止 AI 反复查询
- ✅ **权限控制**：主人全权限，普通用户只能看自己的私聊和非限制群聊

## 安装

1. 安装依赖：`pip install httpx`
2. OneBot 程序开启 HTTP 服务（默认端口 3000），确认 token
3. 复制 `history_plugin` 文件夹到 `data/plugins/`
4. WebUI 中配置主人账号

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `http_host` | string | `localhost` | OneBot HTTP 服务地址 |
| `http_port` | integer | `3000` | OneBot HTTP 端口 |
| `access_token` | string | 空 | OneBot 设置的访问令牌 |
| `use_ws` | switch | `true` | 优先使用适配器 WS 通道（推荐开启，ID 与转发/撤回一致）；关闭则仅用 HTTP |
| `master_id` | string | 空 | 主人 QQ 号，拥有所有权限 |
| `allowed_users` | string | 空 | 其他允许使用的用户 QQ，逗号分隔 |
| `restricted_groups` | string | 空 | 仅主人可查看的群号，逗号分隔 |

## 使用

跟 AI 说"总结某某群/某某人最近聊了些什么"即可。默认条数可在配置中调整，也可直接使用时说明。

## 工作原理

```
用户要求总结/查看历史
        ↓
get_history 工具被调用
        ↓
1. WS 通道优先（复用适配器连接，ID 与 OneBot 层一致）
   → 失败自动降级 HTTP 通道（QQ 客户端数据库，数据更全）
2. 强解析：
   - raw_message 为占位（如 SnowLuma 的 [引用消息]）→ 改用 message 段数组
   - reply 段 → [引用 msg_id:xxx]
   - 媒体缺源 → 标记待刷新
3. get_msg 批量刷新（最多 10 条/次）恢复媒体 URL
4. 过滤仍无法解析的空引用占位消息
5. 返回带 msg_id 的格式化文本
```

## 常见问题

**Q：为什么之前会显示 `[引用消息]`？**
A：这是 SnowLuma 的 reply 段转换失败时生成的 raw_message 占位（消息内容实际为空）。v1.3.0 起会自动过滤这类占位，改用真实段数组解析。

**Q：WS 和 HTTP 都开会不会冲突？**
A：不会。同一轮只走一个通道（WS 优先，失败才走 HTTP）。两者互补：WS 的 ID 与适配器一致（可被 get_msg/转发反查），HTTP 数据更全（含启动前消息）。

**Q：需要额外配置 WS 连接吗？**
A：不需要。WS 通道复用 KiraAI 框架已有的适配器连接，零配置。

## 开源协议

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 开源。

<details>
<summary><b>更新日志</b></summary>

### v1.3.0（2026-09-02）

- **新增**：WS 通道优先（复用适配器连接，与转发/撤回同一 ID 命名空间），HTTP 兜底，`use_ws` 开关
- **新增**：强解析——raw_message 为占位时改用 message 段数组；reply 段显示 `[引用 msg_id:xxx]`
- **新增**：get_msg 批量刷新（最多 10 条/次），恢复 SnowLuma 存储历史中缺失的媒体 URL
- **新增**：自动过滤无法解析的空引用占位消息，不再污染 LLM 上下文
- **保持**：权限检查 / 防循环缓存 / count 限制 / HTTP 通道全部原样保留

### v1.2.0

- 提供正确的消息 ID 和图片消息真实 URL，辅助合并转发功能
- 防止 AI 犯蠢一直重复读取

### v1.1.0

- 初始版本：跨会话获取群聊/私聊消息历史

</details>
