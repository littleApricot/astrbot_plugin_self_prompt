# 自维护提示词

让 AstrBot 通过工具维护当前人格提示词，暂时作为可编辑的长期记忆。提示词分为三个部分：

| 部分 | 内容 |
| --- | --- |
| 性格和人设（包括昵称） | 身份、性格、昵称、称呼等设定 |
| 输出偏好要求 | 语言、长短、语气、格式等长期偏好 |
| 其他需要记住的事情 | 值得跨轮次保留的事实、约定和信息 |

修改直接保存到 AstrBot 的人格数据库，下一次正式回复请求自动加载；也会出现在原有的人格设定页面。共用同一人格的群聊和私聊共享这些内容，不按群另建一份记忆。调用工具的本轮会看到修改结果，新提示词从下一次请求开始注入。

## 安装与使用

适配 AstrBot 4.29.1。推荐在插件市场添加以下插件源，找到“自维护提示词”并安装：

```text
https://raw.githubusercontent.com/littleApricot/astrbot_plugin_self_prompt/main/plugins.json
```

也可以在插件页通过仓库地址安装：

```text
https://github.com/littleApricot/astrbot_plugin_self_prompt
```

手动安装可下载 [GitHub Releases 中的 ZIP](https://github.com/littleApricot/astrbot_plugin_self_prompt/releases/latest)，在插件页上传。安装后启用“自维护提示词”。插件提供 `get_self_prompt` 和 `update_self_prompt` 两个工具，无需填写新的 API Key。

通过插件源安装后，后续发布会出现在插件页的更新检查中，点击更新即可。通过仓库地址安装则从该仓库更新。ZIP 安装的插件需在插件详情中绑定上述插件源，才能使用一键更新。更新程序文件保留人格数据库和插件备份数据。

机器人继续使用已有回复模型，需要该模型支持工具调用。当前人格使用全部工具时会自动获得这两个工具；若在人格页面配置了工具白名单，将这两个名字加入白名单。

可以在对话里告诉机器人“以后回答简短一点，请记住”，或者纠正需要长期保存的事实。插件会在每次请求里说明三个分区和读写方法，由回复模型判断是否调用工具；仅口头回答“记住了”不能证明已保存，以成功的工具返回值为准。

初次安装和读取工具都保留原人格文本。第一次成功修改时，原文本完整归入“性格和人设”，并创建另外两个分区。之后只替换指定分区，其他分区及文档周围的内容保留。提示词中的管理标记用于定位分区，在人格页面手工编辑时应保留它们。

## 工具参考

`get_self_prompt` 无模型入参，返回 `ok`、当前 `persona_id`、完整提示词内容对应的 `revision`，以及三个分区组成的 `sections`。没有可编辑人格或文档标记损坏时返回错误。

`update_self_prompt` 参数：

| 参数 | 必填 | 含义 |
| --- | --- | --- |
| `section` | 是 | `persona`、`output_preferences` 或 `memory` |
| `content` | 是 | 指定部分修改后的完整内容；空字符串清空这一部分 |
| `expected_revision` | 是 | 最近一次读取工具返回的 `revision` |

成功时返回 `ok: true`、新 `revision`、更新后的 `sections` 和 `effective: next_request`。`revision_conflict` 表示读取后内容已被其他编辑改变，模型应重新读取、合并后再提交。工具绑定本轮实际选择的人格，模型不能通过参数指定另一个人格。

工具遵循 AstrBot 的插件工具权限设置。默认情况下普通成员的对话也可能促使模型修改当前人格；如需限制修改，在工具管理中设置 `update_self_prompt` 的权限。

## 保存与备份

每次修改前，将原始提示词备份到 `data/plugin_data/astrbot_plugin_self_prompt/backups/`。备份以人格 ID 的摘要分目录，文件名是原提示词的内容摘要。日志只记录修改的分区名称和错误类型。

人格原有的工具白名单、技能设置、开场白和错误提示设置继续由 AstrBot 管理。插件更新保留数据库里的提示词及插件数据目录中的备份。

## 验证范围

在本插件目录运行：

```bash
ASTRBOT_SOURCE=/path/to/AstrBot
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ASTRBOT_SOURCE:$PWD/.." \
  ASTRBOT_ROOT=/tmp/astrbot-self-prompt-test \
  "$ASTRBOT_SOURCE/.venv/bin/python" -m pytest tests -q
```

测试使用隔离 SQLite 数据库和 AstrBot 原有的人格管理、请求组装代码，检查三个部分的修改、并发版本冲突、重建人格管理器后的持久化、下一轮提示词加载、人格作用范围和原有工具配置保留。不调用真实模型或向 QQ 发消息。

## 发布更新

发布新版本时，同时修改 `metadata.yaml` 的 `version`、`plugins.json` 中的两个版本号及 `download_url` 的版本标签。提交后创建同名 Git 标签并推送到 GitHub，再发布对应 Release。插件源中的下载地址固定到版本标签，避免安装时版本信息与代码不一致。
