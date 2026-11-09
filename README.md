# 校园知识咨询

广东工业大学计科作业「构建一个能说会做的智能体」的本机与试用服务。导入课表后，可以用中文问课室、教师、教学内容和考试，按课程做知识问答，并从教务带入成绩。

作业说明：<https://edu.cnblogs.com/campus/gdgy/Class56-Grade2024-CS/homework/16760>

试用地址：<http://8.134.98.200:8088/campus/>

## 能做什么

- 注册本站账号。密码只存哈希，14 天登录状态留在本机 Cookie。
- 登录学校教务后导入个人课表。浏览器登录只适合在你自己的电脑上使用；服务器上改用粘贴 Cookie，或先载入示例课表。
- 课表有更新时，用已保存的教务会话点「导入刷新」。教务密码不保存。
- 查询今天、明天、本周课表。课表按星期和节次展开，点击课程可看时间、课室和教师。
- 考试提醒，以及从教务带入的总评、平时、期中、期末、学分和绩点。不能手记成绩。成绩窗口可以做百分制分析。
- 课堂复盘、课程评价。评价按课程和按教师分开，并进入检索。
- 咨询台有两套会话：校园咨询，以及按每门课分开的课程问答。
- 课程问答优先查本机教学内容和复盘；需要公开课时优先搜索 B 站，给出标题和链接。
- 对话会保存。页面只显示最近一页，滚到顶部再加载更早的记录。超过上限的旧对话会压进检索后删除原文。
- 网页可以用浏览器语音输入，也可以朗读最近一条回答。

不做选课或抢课，也不调用日在广工的小程序接口。课表和成绩走学校教务 `jxfw.gdut.edu.cn`。

## 本机使用

需要 Python 3.10 或更高版本，只使用标准库。

```powershell
cd D:\code\campus-knowledge-agent
python -m campus_agent
```

浏览器打开 <http://127.0.0.1:8765>。

```powershell
python -m campus_agent.cli --demo
python -m unittest discover -s tests -v
```

复制 `.env.example` 为 `.env` 后填写模型密钥。没有密钥时，仍按问题调用真实工具回答，不会编造课室和分数。

```text
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

接口兼容 OpenAI Chat Completions。密钥不要提交到仓库。

示例问法：

- 明天有什么课？课室和老师是谁？
- 软件工程讲什么？
- 最近有哪些考试要提醒？
- 分析一下我的成绩
- 在「课程问答」里选中一门课后：这门课在讲什么？推荐一门公开课

CSV 表头：

```text
课程,教师,课室,星期,开始时间,结束时间,周次,教学内容
```

星期可以写 `周一` 或 `1`。周次可以写 `1-16`、`1-8,10`、`1-16单`。学期代码 `202601` 表示 2026-2027 学年第一学期。

## 技术细节

网页、命令行和 MCP 都调用同一套工具。有模型密钥时，由模型选择工具；失败或没有密钥时，按中文关键词直接调用工具。

```text
浏览器 → web.py → Agent
                      ├─ 有密钥：DeepSeek 等 OpenAI 兼容接口，最多 5 轮工具
                      └─ 无密钥：关键词路由
                 MCP stdio（JSON-RPC 2.0，Content-Length 帧）
                 tools.py → SQLite data/campus.db
```

主要数据表：`users`、`sessions`、`user_meta`、`courses`、`exams`、`reviews`、`evaluations`、`grades`、`messages`、`memories`、`chunks`。账号之间的数据用 `user_id` 分开。示例账号占用 id 1，不能用密码登录。

- 密码：PBKDF2-HMAC-SHA256，120000 次。会话 Cookie 名为 `campus_session`，HttpOnly，14 天。
- 教务 Cookie 存在 `user_meta`，只用于下次导入刷新，接口响应里不返回。
- 检索：中文单字、双字和英文数字串上的 BM25。课程、考试、复盘、评价、成绩和长期记忆都会进索引。
- 长期记忆：用户说「记住」「请记得」等句子时写入，最多 30 条。
- 对话：模型只看最近 8 条、约 6000 字。页面每次加载 12 条。同一会话超过 36 条时，更早的原文压缩成一条记忆并删除，保留最近 18 条。
- 课程问答：每门课一个会话编号。系统提示把回答限制在当前课程。
- 成绩：教务 `xskccjxx!getDataList.action`。再次导入只替换来源为教务的记录。
- 推荐课：B 站公开搜索。接口没有返回视频时，只给搜索页链接。

目录：

```text
campus_agent/tools.py          课表、考试、复盘、评价、成绩、推荐
campus_agent/gdut.py           教务课表和成绩
campus_agent/agent.py          对话循环和课程问答
campus_agent/rag.py            BM25 索引
campus_agent/memory.py         长期记忆
campus_agent/mcp_server.py     MCP 工具进程
campus_agent/web.py            本机网页
campus_agent/static/index.html 页面
samples/schedule.json          示例学期
deploy/                        本机服务单元
```

## 试用

<http://8.134.98.200:8088/campus/>

点「打开学校登录页」会在你自己的浏览器里打开教务登录页。教务的会话 Cookie 带有 HttpOnly，网页不能读取另一个网站的 Cookie。登录后需要在教务页面的网络请求里复制 Cookie，再贴回来。程序跑在你自己的电脑上、并且本机有 Edge 或 Chrome 时，仍会尝试自动读取这一次登录的 Cookie。不保存教务密码。

服务器上的 `.env` 单独放置，不进 Git。浏览器自动打开教务登录窗口只在本机可用。
