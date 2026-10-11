# Antigravity 项目（Project）与会话跨环境同步故障排查与去重实践

> 时间线：2026-10-11
> 涉及环境：Windows (`kamus`) ⇄ WSL Ubuntu-24.04 (`kamus`)
> 涉及模块：`antigravity-sync`、Antigravity 2.0 LanguageServer、`~/.gemini/config/projects/`
> 结论速览：**Antigravity 的 Project 列表不仅由数据库维护，其源头定义位于 `~/.gemini/config/projects/<project_id>.json`**。仅同步 `~/.gemini/antigravity/` 数据会导致 LanguageServer 在启动校对（reconcile）时无法解析外来会话的 Project UUID，进而将 FTS 归属清空；同时多工作区或不同路径打开易导致后缀为 ` 2` 的重复项目产生。

---

## 1. 故障现象

从 Windows 环境同步 `mopheus` 项目相关会话至 WSL 环境后：

1. **WSL Antigravity 侧边栏完全未出现 `mopheus` 项目**：已同步的 80 余条会话没有被归入 `mopheus`，在 UI 中处于未分组状态。
2. **存在带数字后缀的重复项目**：侧边栏出现 `mes-cli 2` 与 `db-ops-skills 2`，与原有的 `mes-cli`、`db-ops-skills` 并存，且部分会话被分散在后缀 `2` 的项目下。

---

## 2. 根因剖析（Root Cause Analysis）

### 2.1 Project 体系的双层映射模型

Antigravity 2.0 运行时的项目管理并非单点存储，而是通过 **配置层 + 运行时数据库层 + 会话轨迹层** 协同工作：

```
[~/.gemini/config/projects/<uuid>.json] (源头定义: name, gitFolder URI)
                      │
                      ▼ (LanguageServer 启动扫描与校验)
[conversation_summaries.db]
  ├── conversation_summaries 表 (存储 project_id UUID, workspace_uris)
  └── conversations_fts 虚拟表  (chunk_index=-1 行, 存储 project/workspace 目录 Slug 字符串)
                      │
                      ▼ (会话加载与上下文解析)
[conversations/<id>.db / .pb]
  └── trajectory_metadata_blob  (protobuf 编码 project UUID 与 workspace URI)
```

### 2.2 故障一：`mopheus` 项目不显示的根因

1. **缺失全局 Project 定义文件**：
   - 跨机/跨环境同步工具主要打包了 `~/.gemini/antigravity/`（数据库、会话文件、annotations 等），未包含 `~/.gemini/config/projects/`。
   - WSL 中缺少 `cc43b06d-e24a-4b42-8a2f-4112ee58ef8b.json`。
   - LanguageServer 启动日志出现明确报错：
     ```text
     projects store: project_store_get_file_missing: missing project file: open /home/kamus/.gemini/config/projects/cc43b06d-e24a-4b42-8a2f-4112ee58ef8b.json: no such file or directory
     ```
2. **FTS 索引被置空**：
   - 因为无法定位 `cc43b06d...` 对应的项目名称，LanguageServer 在执行 `[Summaries] reconcile` 时，将 `conversations_fts` 中的 `project` 字段置为 `''`（空字符串），导致 UI 侧边栏不显示该分组。
3. **`workspace_uris` 未完全 POSIX 归一化**：
   - 部分会话的 `workspace_uris` 仍保留 Windows 格式 `["file:///c%3A/Users/kamus/CascadeProjects/mopheus"]`，未能对齐 WSL 的 `/home/kamus/CascadeProjects/mopheus`。

### 2.3 故障二：后缀 ` 2` 重复项目的根因

在 WSL 中先后以多目录组合或不同工作区打开了同一代码目录，导致系统自动分配了新的 Project UUID 并生成附带编号的配置文件：

- `mes-cli`（标准，UUID: `f8d15084...`） vs `mes-cli 2`（UUID: `ec60515b...`，含 3 个会话）
- `db-ops-skills`（标准，UUID: `e166eb67...`） vs `db-ops-skills 2`（UUID: `752b77a9...`，含 3 个会话）

---

## 3. 解决步骤与修复方法

### 3.1 步骤 1：补齐目标环境的 Project 配置文件

在 `/home/kamus/.gemini/config/projects/cc43b06d-e24a-4b42-8a2f-4112ee58ef8b.json` 写入标准定义，确保路径为 POSIX 格式：

```json
{
  "id": "cc43b06d-e24a-4b42-8a2f-4112ee58ef8b",
  "name": "mopheus",
  "projectResources": {
    "resources": [
      {
        "gitFolder": {
          "folderUri": "file:///home/kamus/CascadeProjects/mopheus",
          "defaultBranch": "main"
        }
      }
    ]
  },
  "settings": {},
  "updatedAt": "2026-08-18T15:10:51.107047600Z",
  "isWorkspaceOnly": false
}
```

（同理将同目录的 `mopheus-dsh-acp` 项目定义同步补齐）。

### 3.2 步骤 2：对齐数据库与 FTS 索引

使用 SQLite 对 `conversation_summaries.db` 进行安全事务更新：

```sql
-- 1. 更新 mopheus 路径与 FTS 分组
UPDATE conversation_summaries
SET workspace_uris = '["file:///home/kamus/CascadeProjects/mopheus"]'
WHERE project_id = 'cc43b06d-e24a-4b42-8a2f-4112ee58ef8b';

UPDATE conversations_fts
SET project = 'mopheus', workspace = 'mopheus'
WHERE workspace = 'mopheus' AND chunk_index = -1;

-- 2. 将 mes-cli 2 会话归入 mes-cli
UPDATE conversation_summaries
SET project_id = 'f8d15084-988a-4f66-a46a-fff33d5649be',
    workspace_uris = '["file:///home/kamus/CascadeProjects/mes-cli"]'
WHERE project_id = 'ec60515b-e2b4-4c6f-9d7a-422ae3297fbd';

UPDATE conversations_fts
SET project = 'mes-cli'
WHERE project = 'mes-cli 2' AND chunk_index = -1;

-- 3. 将 db-ops-skills 2 会话归入 db-ops-skills
UPDATE conversation_summaries
SET project_id = 'e166eb67-1f55-4610-97f1-b4781c5607be',
    workspace_uris = '["file:///home/kamus/CascadeProjects/db-ops-skills"]'
WHERE project_id = '752b77a9-0cfb-4271-96a8-793c3a0e193b';

UPDATE conversations_fts
SET project = 'db-ops-skills'
WHERE project = 'db-ops-skills 2' AND chunk_index = -1;
```

### 3.3 步骤 3：重写 Protobuf 摘要与缓存

`raw_summary` 及 `agyhub_summaries_proto.pb` 中包含序列化的项目 UUID 和路径前缀。
使用 `antigravity_sync.proto_utils.transform_proto_bytes` 递归下钻并重新计算 wire-format 长度前缀，防止粗暴替换导致 protobuf 帧损坏：

```python
replacements = [
    (b'ec60515b-e2b4-4c6f-9d7a-422ae3297fbd', b'f8d15084-988a-4f66-a46a-fff33d5649be'),
    (b'752b77a9-0cfb-4271-96a8-793c3a0e193b', b'e166eb67-1f55-4610-97f1-b4781c5607be'),
    (b'file:///c%3A/Users/kamus', b'file:///home/kamus'),
    (b'file:///c:/Users/kamus', b'file:///home/kamus'),
]
new_raw = transform_proto_bytes(raw, replacements)
```

### 3.4 步骤 4：删除冗余的后缀 `2` 配置文件

在确保所有会话都已完成迁移后，安全删除旧项目定义文件：

```bash
rm /home/kamus/.gemini/config/projects/ec60515b-e2b4-4c6f-9d7a-422ae3297fbd.json
rm /home/kamus/.gemini/config/projects/752b77a9-0cfb-4271-96a8-793c3a0e193b.json
```

---

## 4. 验证结果与防范措施

### 4.1 校验矩阵

1. **项目配置文件**：`~/.gemini/config/projects/` 中仅包含基准项目，无任何 ` 2` 后缀文件。
2. **侧边栏列表统计 (`conversations_fts`)**：
   - `mopheus`: 78 条
   - `mes-cli`: 8 条（原 5 条 + 归并 3 条）
   - `db-ops-skills`: 9 条（原 6 条 + 归并 3 条）
   - `claude-code`: 6 条
   - `vscode-picgo-paste`: 3 条
   - `axon-cli`: 2 条
   - `mopheus-dsh-acp`: 1 条
   - `CLI Project`: 1 条
3. **数据库完整性**：`PRAGMA integrity_check` 输出 `ok`，WAL 日志完全截断写入。

### 4.2 最佳实践建议

1. **同步范围拓展**：在后续版本升级中，`antigravity_sync` 打包应考虑纳管 `~/.gemini/config/projects/*.json`，在跨机导入时自动依据项目名进行合并与路径重映射。
2. **严防进程在线写入**：操作 `conversation_summaries.db` 或 `projects/*.json` 前，务必确认 `language_server` 进程已终止，防止内存状态回写踩踏。
