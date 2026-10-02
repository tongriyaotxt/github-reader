# github-reader

**中文** · [English](README.en.md)

把 GitHub 上的技术动态压缩成一份**你能读完**的日报。

解决的问题不是「找不到项目」，而是「找太慢、太费心力、看完记不住」。

```bash
python -m github_reader --zh
```

跑完在 `digests/2026-09-20.md` 得到一份日报，30 秒能判断今天值不值得花时间。
`--zh` 会额外给每条项目补一行中文摘要。

---

## 中文摘要

英文摘要读起来费劲，所以有 `--zh`：它给每个条目加一行 `**一句话**`，
是英文 description 的中文转述。

```bash
python -m github_reader --zh          # 抓取 + 生成 + 翻译
python -m github_reader --zh-only     # 不抓取，只给已有日报补中文（快，省配额）
```

英文原文**保留**在 `**是什么**` 里，中文作为独立字段附在下面：

```markdown
**是什么**：Continual learning infra for self-improving agents
**一句话**：让 agent 持续自我改进的 continual learning 基础设施
```

这样设计有三个好处：英文原文永远在（日报可随时重新生成而不丢信息）、
译文可以单独重跑（不必重跑抓取）、译文出错时能对照原文排查。

**凭据**：复用 `~/.dsh/.credentials.yaml` 里的 `DEEPSEEK_API_KEY`，所以通常
不用配任何东西。也可以用 `DEEPSEEK_API_KEY` 环境变量覆盖。

**幂等**：重复运行只会补缺失的译文，已翻过的会跳过。所以「再跑一次」
永远是安全的补救手段——不需要手工修补。

**术语保留英文**：agent、prompt、LLM、RAG、MCP、harness 这类词不硬翻。
它们在你的日常语境里就是英文，翻成中文反而增加理解成本。

### 在 agent 会话里用

项目里有 `.dsh/skills/zh-digest/`，所以在这个目录下开的 agent 会话里，
你说「要中文」或「翻译一下日报」，它会按固定流程走：跑 `--zh-only`、验证、
有缺口就重跑。

---

## 它和「打开 GitHub Trending 刷一刷」有什么不同

Trending 给你的是**按热度排序的列表**，你还得自己判断每个项目是干什么的、要不要学。
github-reader 多做三件事：

1. **剔除噪音**。Trending 上长期混着 awesome-list、教程、面试题、常青巨库——
   它们星数极高但永远不是「新闻」。这些会被过滤掉。
2. **给出上手成本**。从 README 结构判断：要 docker 一行启动还是要先配 GPU？
   是有 API key 就能用还是要编译半小时？每条都给出预估分钟数。
3. **记住你昨天看过什么**。第二次出现的项目会标注「连续 3 天在榜，累计 +420 ★」。
   这让日报变成**持续跟进**，而不是每天从零开始。

---

## 安装

不需要安装。只需要：

- **Python 3.10+**（只用标准库，无需 pip install 任何东西）
- **GitHub 凭据**（可选但强烈建议）

### 关于凭据：为什么强烈建议

未认证的 GitHub API 限额是 **60 次/小时**，而认证后是 **5000 次/小时**。
这个差距直接决定日报质量——60 次大概只能扫到十几个候选。

好消息是**你大概率不用做任何事**。github-reader 会自动复用你已登录的 GitHub CLI 凭据：

```bash
gh auth status     # 如果显示已登录，就可以直接用了
```

如果没装 gh，也可以设环境变量：

```bash
# PowerShell
$env:GITHUB_TOKEN = "ghp_xxx"
# bash
export GITHUB_TOKEN=ghp_xxx
```

---

## 用法

```bash
python -m github_reader                      # 生成今天的日报
python -m github_reader --top 8 --feed 30    # 精选 8 条 + 动态 30 条
python -m github_reader --domains ai         # 只看 AI/LLM/Agent 方向
python -m github_reader --offline            # 只用缓存，不消耗配额
python -m github_reader --verbose            # 显示被过滤掉的仓库及原因
python -m github_reader --out ./notes         # 指定输出目录
```

### 常用组合

```bash
# 想看过滤逻辑是否合理（调参时用这个）
python -m github_reader --verbose

# 想知道某个方向最近有什么新东西
python -m github_reader --domains ai --top 10

# 没网 / 不想消耗配额时（注意：仍会重跑分析，耗时和正常跑差不多）
python -m github_reader --offline
```

### 关于耗时和配额（实测数据）

| 场景 | 耗时 | API 消耗 |
|---|---|---|
| 首次全量（3 领域 24 关键词，360 个候选） | ~105 秒 | ~450 次 |
| 3 小时内重跑（搜索命中缓存） | ~105 秒 | ~50 次 |
| `--offline` | ~105 秒 | 1 次 |
| `--zh` 增加的翻译时间 | ~30 秒 | 0（走 DeepSeek，不占 GitHub 配额） |

耗时主要花在抓 360 篇 README 和解析上，所以重跑不会变快多少——
**缓存省的是配额，不是时间**。

只想补中文时用 `--zh-only`，它跳过抓取，几秒就完事。

---

## 让它自动跑

「省心力」的关键是不用记得去跑。Windows 上可以挂一个计划任务：

```powershell
# 每天上午 9 点生成日报（含中文摘要）
$action  = New-ScheduledTaskAction -Execute "python" `
           -Argument "-m github_reader --zh" -WorkingDirectory "<仓库目录>"
$trigger = New-ScheduledTaskTrigger -Daily -At 9am
Register-ScheduledTask -TaskName "github-reader 日报" -Action $action -Trigger $trigger
```

跑完直接看 `digests/latest.md` 就行（中文摘要会同步写进去）。
`digests/` 建议用 git 管起来，这样半年后你能全文检索「当时那个 Agent 框架叫什么」。

---

## 日报长什么样

分成两层，因为你的注意力是分级的：

**第一层：30 秒速览**（在文件顶部）
今天最值得看的 3-5 条，每条一句话理由。扫完就知道要不要往下读。

**第二层：精选详情**（每条带完整评估）
```
### 1. owner/repo

`840 ★` · `均速 21.9 ★/天` · `Python` · `MIT` · 创建于 8 天前

**跟踪**：★ 首次出现
**是什么**：Extract your own local chat history from AI coding assistants...
**关键信息**：安装方式：pip · 需要 API Key / 环境变量 · 8 段代码示例
**靠不靠谱**：
- 有明确开源协议（MIT）
- README 讲了架构/原理
- 近 30 天发了 4 个版本
**上手成本**：🟡 中等（4/10，预估 20 分钟跑通）
**建议**：新增速度很快，属于早期高关注项目；近日仍在活跃提交
**最快启动命令**：
```bash
pip install xxx
```
```

**第三层：动态速览**，其余候选一行一条，快速扫过。

---

## 自定义关注方向

改 `config.json` 就行，不用动代码。想加一个方向：

```json
"domains": {
  "我的方向": ["关键词1", "关键词2"]
}
```

每个关键词会生成 **2 条搜索查询**（一条找新项目、一条找活跃老项目），
所以关键词越多、耗时和配额消耗越大。8-10 个词一组是比较舒服的量。

几个常用调节钮：

| 配置项 | 作用 | 调大的后果 |
|---|---|---|
| `min_stars_fresh` | 新项目的星数门槛 | 更少但更成熟的新项目 |
| `min_stars_established` | 老项目的星数门槛 | 更少但更知名的老项目 |
| `fresh_days` | 「新」的定义（天） | 更宽松的时间窗 |
| `evergreen_star_threshold` | 常青巨库降权阈值 | 更多大库进入日报 |
| `cache_ttl_minutes` | 搜索缓存时长 | 更新鲜但更费配额 |

---

## 输出文件

| 路径 | 说明 |
|---|---|
| `digests/YYYY-MM-DD.md` | 每天的日报，自动归档 |
| `digests/latest.md` | 最新一份的副本 |
| `.cache/state.json` | 跟踪历史（谁出现过、连续几天、星数变化） |
| `.cache/http/` | HTTP 缓存，删掉会重新消耗配额 |

`digests/` 建议用 git 管起来，这样你能全文检索「半年前那个 Agent 框架叫什么」。

---

## 它做不到什么（重要）

诚实地说明边界，避免你误用：

1. **`是什么` 那一行是英文原文，不是翻译**。它优先取 **GitHub 的 description
   字段**——那是作者专门为「这是什么」写的一句话，准确率最高。只有 description
   缺失时才退回 README 抽取。中文在下面的 `**一句话**` 里，两者并存。

2. **「均速 ★/天」只在项目 60 天以内时显示，它是终身平均值而非当前增速**。
   GitHub 不提供任意仓库的真实近期增速，所以更老的项目显示
   「距上次推送 N 天」这种**可验证**的活跃度指标，而不是一个好看但会误导你的数字。
   只有你自己连续跟踪的项目，才会有真实的「累计 +N ★」。

3. **启发式分析会看走眼**。README 写得差的好项目可能被判成「文档偏薄」，
   没写 description 的项目摘要质量也会下降。工具的价值是帮你**排序注意力**，
   不是替你做判断。中文摘要是转述，不是权威解释——存疑时看 `**是什么**` 原文。

4. **不覆盖私有仓库、不跟踪 issue/PR 动态**。它只做一件事：帮你发现和筛选
   值得看的公开项目。

5. **中文摘要需要联网调模型**（DeepSeek）。不想用就不加 `--zh`，
   其余功能全部离线可用、零依赖。

6. **`--offline` 只在当天有效，隔天跑是空转**。搜索查询里带日期窗口
   （`pushed:>2026-09-17`），而缓存 key 是整个 URL 的哈希——日期一变，
   key 就变，缓存全部命中不上。实测隔 4 天再跑 `--offline`，拿到
   **0 个候选**并直接退出。它省的是「同一天内重复跑」的配额，
   不是「断网也能出日报」。

---

## 质量是怎么校准的

`tools/inspect_taglines.py` 是开发期用来抽查摘要质量的工具，它复用本地缓存，
不额外消耗配额：

```bash
python tools/inspect_taglines.py --limit 25
```

在 24 个真实仓库上抽查，当前结果是 **0 个错误、24/24 来自 description**。
校准过程中发现并修掉的问题（都写进了测试）：

| 曾经的问题 | 现在的行为 |
|---|---|
| 摘要抽到「求 star」的话 | 拒绝求关注类话术 |
| 摘要抽到「Read AGENTS.md before...」 | 拒绝贡献者指引 |
| 摘要抽到「Orca is free and open source under the MIT License.」 | 拒绝协议声明 |
| 摘要抽到「To create a macOS application bundle:」 | 以冒号结尾的一律淘汰（是操作引导语） |
| 长段落里藏的好句子被压分 | 拆句后逐句评分，消除隐性长度惩罚 |

---

## 开发

```bash
python tests/test_github_reader.py     # 47 个测试：抓取、筛选、分析、渲染、状态
python tests/test_translate.py    # 29 个测试：中文翻译的结构性工作
```

测试集中在**实际踩过的坑**上，不是覆盖率凑数。改代码时如果把这些坑踩回来，
测试会红。几个典型：

**分析层**
- 真实 README 用 **CRLF 换行**，正则里 `[^\n|]` 这类字符类会把 `\r` 也吃进去，
  导致跨行匹配、把不相关的行串成一条命令。用 LF 写测试永远抓不到。
- README 后段讲依赖的 `pip install xxx` 会被误当成该项目的安装命令。
- 代码块里的 shell 注释（`# No dependencies`）长得像标题，会把章节正文截断。
- 「昨天刚推送」的项目因为信号字段没算，被判成「两周没动静」。
- GPU 判定扫全文，一句「也支持 NVIDIA 显卡」就让纯 CPU 工具被标上「需GPU」。

**翻译层**
- 幂等性必须用**两阶段扫描**：源字段总在它自己的译文之前出现，一趟扫描
  边记录边判断会导致每次重跑都重新翻译。
- 同一个仓库会同时出现在「精选」和「动态速览」里，只用仓库名做幂等 key
  会让速览的译文被详情的记录误判成「已翻译」。
- `**建议**` 字段本来就是中文，翻它只会得到逐字重复的两行。
- `--zh-only` 曾只改日期文件，`latest.md` 停在旧版本——而 README 让你看
  `latest.md`。
- 「已经是中文」的判定漏过术语密集的短句。单元测试的样本带句号
  （`给 coding agent 用的 Chrome DevTools。`），被「含中文标点」那条规则
  接住了，所以「CJK 占比」那条其实**从没兜住过它**。真实日报里那行没有
  句号，汉字占比 0.091 低于 15%，会被再翻一遍。现在补了第三条规则：
  按「汉字数 : 英文词数」判定——实测真实日报 34 个中文字段（译文和
  本身就是中文的源字段）最低 0.429，5 个英文源字段恒为 0.000
  （英文里没有汉字），阈值 0.3 落在中间，两边都有余量。

**测试层**
- 别用固定时间常量造「1 天前」。`analyze()` 内部的 `ensure_signals()`
  不接受外部传入 `now`，用的是真实时钟；测试里拿硬编码的 `NOW` 造
  `pushed_at`，随着日历往前走会变成「4.9 天前」，断言自己变红。
  时间敏感的断言要用相对当前时刻造数据。

代码结构：

| 文件 | 职责 |
|---|---|
| `github_reader/github_client.py` | API 客户端：凭据解析、限流退避、磁盘缓存 |
| `github_reader/discover.py` | 候选发现：多路搜索、动量评分、噪音过滤 |
| `github_reader/analyze.py` | README 启发式分析：摘要、安装方式、上手成本 |
| `github_reader/state.py` | 跨运行状态：连续天数、星数增量 |
| `github_reader/render.py` | Markdown 渲染 |
| `github_reader/llm.py` | LLM 调用（复用 ~/.dsh 凭据，零依赖） |
| `github_reader/translate.py` | 中文摘要：定位字段、分批、校验、写回 |
| `github_reader/cli.py` | 主流程编排 |
| `.dsh/skills/zh-digest/` | 项目级 skill：何时补中文、怎么验证完成 |
| `tools/simulate_next_day.py` | 模拟第二天运行，验证跨天趋势（否则得真等一天） |
| `tools/inspect_taglines.py` | 抽查摘要质量，校准用 |

### 设计取舍记录

几个刻意的决定，避免以后自己或别人「优化」掉：

- **只用标准库**。装依赖本身就是一种心力成本，`urllib` 对这个任务完全够用
  ——包括调 LLM 那一层。
- **LLM 只做翻译这一件事**。抓取、筛选、排序、上手成本评估全是启发式的：
  免费、离线可跑、结果可复现。只有「英文→中文」非 LLM 不可，
  所以模型调用被限制在 `translate.py` 里，且完全可选（不加 `--zh` 就不触发）。
- **中文是新增字段而非替换原文**。英文原文永远在，日报可随时重新生成，
  译文可单独重跑，出错时能对照排查。
- **翻译的确定性落在代码里，不在 skill 里**。字段定位、分批、解析、校验、
  写回都是可测试的代码；skill 只负责触发时机和「什么时候真的算完成」。
- **拿不准就不输出**。安装命令提取宁可漏判（回落到「建议查看安装章节」），
  也不误判——用户会照着敲。摘要宁可退回 description，也不硬凑一句。

