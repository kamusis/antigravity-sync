# Antigravity 持久会话标题（Persistent Session Title）跨机同步：调试全过程与最终修复原理

> 时间线：2026-10-10 ~ 2026-10-11
> 涉及机器：Windows (`kamus`) ⇄ macOS (`kamus@macbook`)
> 涉及仓库：`antigravity-sync`（最终提交 `7458176`）
> 结论速览：**会话显示标题的权威存储是 `annotations/<id>.pbtxt`**，此前所有修复（FTS、raw_summary field 1、field 15.1、pb 缓存）都只是下游，每次点开会话都被注解层的缺失打回。

---

## 1. 问题现象

1. Windows 侧边栏 `📁 mopheus` 会话数不对：Mac 导入的 35 条会话 `project=''`，不归组。
2. 导入会话标题为空或显示原始 prompt（如 `/using-git-worktrees ...`、`https://dev.mopheus.ai/...`）。
3. **最顽固的问题**：修复后的标题在侧边栏显示正常，但**点开会话约 1 秒后标题被打回**为原始 prompt 形式，且反复出现——修一层被打回一层。

---

## 2. 数据架构：会话元数据的存储地图

调试确立的完整存储分层（Antigravity 2.0 LanguageServer）：

| 层 | 位置 | 内容 | 谁读它 |
|---|---|---|---|
| L1 | `conversation_summaries.db` → `conversations_fts`（`chunk_index=-1` 行） | 侧边栏列表查询源；`project`/`workspace` 列存**目录 slug**（`'mopheus'`），不是 UUID | UI 列表 |
| L2 | `conversations/<id>.db` → `trajectory_metadata_blob`（`id='main'`，列 `data`） | protobuf：field 18 = 36 字节项目 UUID，field 1/7 = workspace URI（**长度前缀字段**） | LanguageServer 启动/重扫 |
| L3 | `conversation_summaries` 表 + `agyhub_summaries_proto.pb` | 摘要缓存；raw_summary protobuf **field 1 = 标题，field 15.1 = 持久标题态** | 摘要重建 |
| L4 | `annotations/<id>.pbtxt`（text proto） | **权威显示标题**（`title:"..."`）、`archived:true`、`last_user_view_time:{...}` | **点开会话时** |

关键行为：

- LanguageServer 启动/重扫时读 L2：发现**非本机**项目 UUID → 把 `conversations_fts.project` 重置为 `''`、`project_id` 重置为 blob 中的 UUID → 导入会话脱组。
- **点开会话时**（`cascade_run_state` 加载路径，日志可见）读 L4：注解有 title → 沿用；没有 → **从首条用户消息重新派生标题**，并回写打回 L1/L3 全部层级（约 1s 延迟即此重建）。
- `conversation_summaries.title` 列对"标题=首条消息"的会话为空是**原生正常态**（Windows 原生 137 条中 80 条如此），UI 列表只读 FTS，不是缺陷。

---

## 3. 调试过程时间线

### Phase 0：交接文档的三层根因（上一会话成果）

上一会话已定位：L1 的 FTS project 用 UUID 匹配（应为 slug）、L2 的 blob 外机 UUID 重扫打回、L3 的标题 preview 兜底污染。并留有两侧 pristine 备份（win `bak_20261010_231228` 137 条 / mac `bak_20261010_231113` 66 条）。

### Phase 1：全量盘点（先摸清真实状态再动手）

对两侧 live DB 与 pristine 备份做 ID 全量 diff，发现：

- 两侧 live 均已含全部 **203** 条（137 win + 66 mac，无交集、无缺失、无新增）——**数据搬运早已完成，剩下的是纯元数据修复**。
- `conversation_summaries` 203 行 vs FTS 195 行：8 条 Mac 会话**原生就没有 FTS 行**（3 multica + 2 ruflo + 3 mopheus），两侧 UI 均不可见，属 Mac 原生行为，不应补建。
- Mac 的 mopheus 项目含 7 条历史目录名 `moclaw` 的会话（同 pid `05f52f38`），且 Mac 原生 FTS 已把它们归在 `mopheus` 下——改名场景应按 **pid→slug** 推导，而非 URI 目录名。
- blob 普查：48 条 Mac 会话 blob 含外机 UUID；9 条无 `.db` 文件（两侧都没有，纯 summaries+pb 会话）；blob 内 URI 是长度前缀字段，**路径替换变长必须重编码长度前缀**（36 字节 UUID 可定长替换）。

### Phase 2：第一轮修复（代码 + 数据）

代码修复（提交 `6b97e59`）：

1. `proto_utils.py`：通用 protobuf 字节变换器 `transform_proto_bytes`——递归解析 wire format，对可解析的嵌套消息下钻、不可解析的 payload 做单遍替换，**重算所有长度前缀**；顶层不可解析时退化为仅定长替换（防帧损坏）。
2. `merger.py`：
   - FTS slug 对齐：从**目标机原生行**推导 `project_id → (project, workspace)` 映射，对导入会话按映射后的 pid 写 slug；`outside-of-project` → `''/''`；未知外机 UUID 保留源值（LanguageServer 重扫会自行归零）。
   - 标题回填顺序：源 FTS title → summaries title → preview 兜底。
   - 路径重映射改为 **Python 单遍替换**（顺序 `REPLACE()` 会把 `file:///c%3A/Users/kamus` 再匹配短规则腐蚀成 `c%3Ac:/...`）。
   - `import_bundle` 部署 `<id>.db` 后**原地修补 blob**（UUID 映射 + 路径规则）。
3. `workspace_mapper.py`：**历史目录改名推断**——源 slug 不在目标机但其 pid 映射到目标项目 → 生成 `CascadeProjects/moclaw → CascadeProjects/mopheus` 替换对。

数据修复：新工具 `scripts/repair_metadata.py`（对称设计：在哪家执行修哪家，对端真值经快照 JSON 提供；默认 dry-run，`--apply` 写入；blob 原始字节先存 JSON 备份）。Windows 侧执行后验证：**mopheus 57 未归档 / 21 归档 ✓，blob 外机 UUID 残留 0，标题与 Mac 原始值偏差 0**。

### Phase 3：打回问题 Round 1——raw_summary field 1 假设

用户实测：计数 56 ✓，但点开会话标题被打回。

- 解剖 `<id>.db` 全部表：无 title 列；blob 完整解码无标题字段。
- 对比当前态 vs Mac 快照：发现 `raw_summary` **field 1 = 标题**，被打回会话的 field 1 = 原始 prompt，且点开后 `FTS title == raw1` 逐字节相等 → 假设"点开时 FTS := raw1"。
- 修复：`set_leading_string_field`（后演化为 `set_summary_title`）重写 field 1，`repair_metadata` 对导入会话同步写 summaries 列 + pb 缓存。
- **结果：仍被打回**。反思：被打回会话的 field 1 与 FTS 同为 prompt，可能同源（都派生自首条消息），"FTS := raw1"是相关非因果。

### Phase 4：打回问题 Round 2——field 15.1 假设

- 结构对比：标题稳定的 Windows 原生会话 raw_summary 的 **field 15.1 = 标题字符串**；被打回会话 field 15 无 15.1（仅时间戳）。
- 相关性普查：57 条 win 原生 `15.1 == FTS title`（0 反例）→ 假设"重建时有 15.1 用 15.1，没有从首条消息派生"。
- 修复：`set_summary_title` 同时写 field 1 + 插入 15.1（field 15 不可解析时跳过防损）。Windows 35 条、Mac 25 条补写，验证 58/58 全一致。
- **结果：仍被打回**。反思：15.1 的样本值（`create-new-issue-1`、`assets`、`private-ticket`）更像分支名——与标题一致是**巧合相关**（那些会话标题本就取自分支/首条消息），15.1 不是因果位。

### Phase 5：决定性排查——三路并进

1. **全树字符串搜索**：在 `~/.gemini/antigravity`、`AppData\Roaming\Antigravity`、`AppData\Local\Antigravity` 全部 6 万+ 文件中搜干净标题串（UTF-8 与 UTF-16LE 双编码）→ 干净标题**只存在于** summaries.db 与修复备份；`<id>.db`、brain 目录、Electron 存储均无。
2. **点击时刻文件抓取**：从 `language_server.log` 定位点击时间点（`cascade_run_state.go` 为 5aa4c028/11e0ae7f 创建 run state 的日志行），按 mtime 扫描该时间窗内被写入的文件 → `annotations/<cid>.pbtxt`（恰好是被点击会话）+ `agyhub_summaries_proto.pb` + `app_storage.json`。
3. **注解内容检查**：`annotations/031ce632.pbtxt` 含 `title:"Creating Desktop Symlink For Agent"`——**注解文件就是标题存储**。被点击的 2 条会话注解只有 `last_user_view_time`，无 title。

相关性终验：66 条 Mac 会话注解 **0/66 有 title**（60 个连文件都没有）；抽样 50 条标题稳定的 win 原生会话 **50/50 有**。机制闭环：点开 → 读注解 → 无 title → 从首条消息派生 → 回写打回全部下游层。

### Phase 6：途中发现的幂等性缺陷（事故与自愈）

第二轮修复重跑时发现 48 个 blob 被二次腐蚀：裸路径规则 `/Users/kamus → c:/Users/kamus` 的**输出包含自身模式**，单遍替换只保证单次运行内不复匹配，**跨运行不幂等**（`c%3A/Users` 被再匹配成 `c%3Ac:/Users`）。

修复（提交 `5c4660b`）：

- `build_one_pass_replacer` 支持编译正则规则（编号捕获组 + `m.lastindex` 定位分支）。
- 裸路径规则加 lookbehind：`(?<![%:A-Za-z0-9])/Users/kamus`——在已重映射 URI（`c%3A/Users` 前是 `A`、`c:/Users` 前是 `:`）内部不再匹配。
- 加自愈规则 `c%3Ac:/ → c%3A/`、`c:c:/ → c:/`，一次重跑即治愈全部 48 个 blob（两侧扫描 0 残留）。
- 教训：**替换规则集必须整体验证幂等性**（每条规则的 old 不得是任何规则 new 的子串，否则必须 lookbehind）。

---

## 4. 最终修复的技术原理

### 4.1 标题修复链（提交 `51b37d3` + `5e8357f`）

```
点开会话
  └─ LanguageServer 读 annotations/<id>.pbtxt
       ├─ 有 title:"..." ──→ 沿用，回写 FTS/summaries/pb（稳定 ✓）
       └─ 无 title ──────→ 从首条用户消息派生，打回全部下游层（✗）
```

修复 = 把标题写进**权威位**，并让下游各层与之一致：

| 层 | 修复动作 | 代码 |
|---|---|---|
| annotations（权威） | `title:"..."` 写入/创建，保留 `last_user_view_time` 等其余字段，按需补 `archived:true` | `antigravity_sync/annotations.py` |
| raw_summary field 1 + 15.1 | protobuf 顶层 field 1 重写 + field 15.1 插入（field 15 不可解析则整体跳过防损） | `proto_utils.set_summary_title` |
| pb 缓存 | 同步重写对应 entry | `repair_metadata.py` |
| FTS / summaries | title/project/workspace/archived 按真值重设 | `repair_metadata.py` |

### 4.2 blob 修补与 protobuf 长度重编码

`trajectory_metadata_blob` 内嵌外机项目 UUID（field 18，36 字节定长）与 workspace URI（**长度前缀字段**，路径替换会变长）。盲目原地字节替换会破坏 protobuf 帧。

`transform_proto_bytes` 的处理：

1. 严格解析 wire format（varint / fixed64 / length-delimited / fixed32 / SGROUP-EGROUP），任何不干净终止即判非消息。
2. 可解析 → 递归下钻；不可解析 → 视为字符串/二进制 payload 做单遍替换。
3. 自底向上重序列化，**所有长度前缀重算**。
4. 顶层不可解析时仅施加定长替换（UUID 36→36 安全；变长路径规则排除），防帧损坏。
5. 真实数据 roundtrip 字节级恒等验证通过。

### 4.3 FTS slug 对齐算法

`conversations_fts.project/workspace` 存目录 slug 而非 UUID，因此：

- 从**目标机原生行**（排除本次导入行，防源 slug 污染映射）推导 `project_id → (project, workspace)`；
- 导入会话按 `project_id_map` 映射后的 pid 查表写 slug；
- 映射到 `outside-of-project` → 写 `''/''`（与原生行为一致）；
- 未知外机 UUID → 保留源值（LanguageServer 重扫会归零，无需预测）。

### 4.4 历史目录改名推断（moclaw → mopheus）

源 slug 不在目标机工作区集合中，但其 pid 经其他会话的 slug 匹配已映射到目标 pid → 反查目标 pid 的 slug，生成 URI 替换对。7 条 `moclaw` 会话由此正确归入 `mopheus`，无需硬编码。

### 4.5 annotations 纳入 bundle（未来同步不再丢标题）

`bundle.py` 打包 `annotations/<cid>.pbtxt`；`import_bundle` 部署到目标机。此前 bundle 只含 summaries/conversations/brain，注解层从未跨机流动——这是标题问题在同步场景下的结构性根源。

---

## 5. 验证方法与结果

| 验证项 | 方法 | 结果 |
|---|---|---|
| 分组计数 | `SELECT count(*) FROM conversations_fts WHERE chunk_index=-1 AND project='mopheus' AND archived=0/1` | 两侧均 57 / 21 ✓ |
| blob UUID 普查 | 正则 `\x92\x01$` + 36 字节提取，两侧全量 | 外机 UUID 残留 0 ✓ |
| 路径腐蚀扫描 | `c%3Ac:/`、`c:c:/` 子串扫描（blob/pb/uris/fts） | 0 残留 ✓ |
| 标题一致性 | 当前 FTS/raw1/15.1/注解 vs 对端 pristine 快照逐条比对 | 偏差 0 ✓ |
| 端到端行为 | 用户重启 Antigravity 实际点击 | 标题保持 ✓（用户确认） |
| 双向同步态 | `sync --dry-run` | 0 push / 0 pull ✓ |
| 单元测试 | `python -m unittest tests.test_sync` | 16 个全过 ✓ |

---

## 6. 附录

### 6.1 raw_summary protobuf 字段地图（实测）

```
1        (str)   标题
2        (varint) step_count
3        (msg)   时间戳 {1: seconds, 2: nanos}
4        (str)   trajectory_id（对应 <id>.db trajectory_meta）
5        (varint)
7        (msg)   时间戳
9        (msg)   workspace 信息 {1/2: URI, 3: {1: git remote, 2: git url}, 4: branch}
10       (msg)   时间戳
15       (msg)   状态 {1: 标题态(部分会话有), 4/5: 标志, 7: 时间戳}
16       (varint)
17       (msg)   trajectory metadata 副本 {1: workspace, 6: conv id, 18: project uuid}
22       (varint)
```

### 6.2 trajectory_metadata_blob 字段地图（实测）

```
1        (msg)   {1/2: URI, 3: {1: git remote, 2: git url}, 4: branch}
2        (msg)   时间戳
3        (str)   会话级 UUID（非项目）
6        (str)   conversation_id
7        (str)   workspace URI
15       (msg)   计时/性能数据（packed varint，非标题）
18       (str)   项目 UUID（36 字节，`\x92\x01$` 前缀）
```

### 6.3 经验教训

1. **先全量盘点再动手**：两侧 ID diff 直接把"数据搬运"从问题域排除，避免无效重传。
2. **相关性 ≠ 因果**：`FTS == raw1`、`15.1 == FTS` 两度把调试引向下游层；最终靠"点击时刻文件 mtime 抓取"定位权威位。
3. **日志是最高信噪比证据**：`language_server.log` 的 `cascade_run_state` 行直接给出点击时刻与动作。
4. **替换规则集必须整体验证幂等性**，跨运行腐蚀比单次腐蚀更隐蔽。
5. **修数据前必须确认 `language_server` 已死**，否则退出时内存态回写覆盖一切外部修改。
6. 修复工具默认 dry-run、写前备份（blob 原始字节 JSON + pb `.bak`），是多次试错的安全网。
