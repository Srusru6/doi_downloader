# DOI Downloader

一个用于按 DOI 下载论文并可递归下载其参考文献的命令行工具。支持：
- 多来源策略：优先出版社直链，其次 Unpaywall（开放获取），最后备选 Sci-Hub（可配置镜像）
- 并发 BFS 下载：按层并行下载引用文献，显著提升吞吐
- 请求重试、退避与全局限速（RPS）
- 标题校验：用 CrossRef 官方标题与下载页标题做相似度校验（Levenshtein）
- 引用提取：从页面 / CrossRef 元数据中提取引用 DOI，进行递归
- 可选“年轻作者”筛选：在某一深度仅保留含有“学生/博士/研究生等”关键字的作者单位
- 下载历史与去重：已下载的 DOI 会被缓存并跳过，历史记录写入 `.history.json`
 - 被引（引用该论文的其他文章）抓取与下载：可通过 `--cited` + `--cited-rows` 启用（存放在 `cited/` 目录）

> 仅供学术研究与学习使用。请遵守所在地区与资源平台的法律法规与使用条款。对因使用 Sci-Hub 等第三方服务产生的风险自行负责。

---

## 快速开始（Windows + PowerShell）

1) 准备 Python 3.10+ 环境（建议 3.10/3.11/3.12）

2) 创建虚拟环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

若安装 `python-Levenshtein` 报编译错误，可先安装 VS C++ 构建工具；或改用轮子源/镜像。你也可以选择替代库 `rapidfuzz` 并修改代码，但本项目默认使用 Levenshtein。

3) 运行一个 DOI（演示，深度 1）：

```powershell
python .\main.py --prod --doi 10.1126/science.177.4047.393 --depth 1
```

下载的 PDF 将保存到 `Downloads_pdf/sample/<层目录>/`，如 `main/`、`ref1/`、`ref2/`。

---

## 使用说明

常用参数：
- `--prod`：生产模式，输出更简洁
- `--doi`：要处理的 DOI（可逗号或空格分隔多个）
- `--depth`：递归深度（>=0）。0 表示只下载主文献；1 表示再下载其引用的第一层；以此类推
- `--workers`：每一层并发数（默认 4）
- `--rps`：全局限速（每秒请求数），0 表示不限制
- `--timeout`：HTTP 超时时间（秒）
- `--retries`、`--backoff`：请求重试次数与退避因子（指数退避）
- `--unpaywall-email`：调用 Unpaywall API 的联系人邮箱（推荐填写以提高成功率）
- `--scihub-domains`：以逗号分隔的 Sci-Hub 基础域名列表，会按顺序尝试
- `--young`：开启“年轻作者”筛选（配合 `--young-depth`）
- `--young-depth`：在哪一层应用年轻作者筛选（默认 2）
- `--young-keywords`：自定义筛选关键字，逗号分隔（默认内置中英文关键词）
 - `--cited`：同时抓取引用（被引）该 DOI 的文章并下载（不再递归）
 - `--cited-rows`：被引文章最大数量（默认 10）

### 示例

- 基本用法（深度 1，并发 4）：
```powershell
python .\main.py --prod --doi 10.1126/science.177.4047.393 --depth 1 --workers 4
```

- 多 DOI 输入（空格或逗号分隔）：
```powershell
python .\main.py --prod --doi "10.1126/science.177.4047.393,10.1038/nphys1170" --depth 1
```

- 使用 Unpaywall（推荐设置邮箱）：
```powershell
python .\main.py --prod --doi 10.1038/nphys1170 --unpaywall-email you@example.com
```

- 指定 Sci-Hub 镜像（按顺序回退）：
```powershell
# 使用者需自备可合法访问的镜像域名，以下为占位示例（请用实际可访问的域名替换）
python .\main.py --prod --doi 10.1038/nphys1170 --scihub-domains "https://your-mirror-1.example,https://your-mirror-2.example"
```

- 限速与超时/重试：
```powershell
python .\main.py --prod --doi 10.1038/nphys1170 --rps 1.5 --timeout 20 --retries 4 --backoff 0.6
```

- 在第 2 层仅保留“年轻作者”文章：
```powershell
python .\main.py --prod --doi 10.1038/nphys1170 --depth 2 --young --young-depth 2 --young-keywords "phd,博士,研究生"
```

- 抓取并下载被引文章（前 15 篇）：
```powershell
python .\main.py --prod --doi 10.1038/nphys1170 --cited --cited-rows 15
```

---

## 工作原理（简述）

1. 标题与引用：
   - 先用 CrossRef API 获取“官方标题”，作为比对基准；并尽量获取引用
   - 再访问 DOI 目标页（或 CrossRef / Sci-Hub 页面）提取 `<title>` 与引用 DOI
2. 下载策略（按优先级降序）：
   1) 出版社页面解析直链（含 .pdf、/pdf/ 等启发式）
   2) Unpaywall API 返回的开放获取 PDF
   3) Sci-Hub（可配置多个镜像）
3. 校验与存储：
  - 用 Levenshtein 比较“官方标题”和“下载页标题”，相似度过低则删除文件
  - 保存路径：`Downloads_pdf/sample/<main|refN>/`；历史：`Downloads_pdf/sample/.history.json`
4. 并发 BFS：
  - 按层并发下载，上一层完成后扩展到下一层；支持全局限速与 HTTP 重试
5. 不再硬编码任何第三方镜像域名：
  - 若需启用额外来源，必须由使用者通过参数显式提供（例如 `--scihub-domains`），仓库本身不附带具体域名。

---

## 目录结构

```
Downloads_pdf/
  sample/
    .history.json        # 下载历史缓存
    main/                # 根 DOI 的 PDF
    ref1/                # 第一层引用 PDF
    ref2/                # 第二层引用 PDF
    cited/               # 引用（被引）该 DOI 的其他文章 PDF（不递归）
```

---

## 常见问题（FAQ）

- 安装 `python-Levenshtein` 失败？
  - 解决：安装 "Microsoft C++ Build Tools"，或使用国内源/轮子；亦可改用 `rapidfuzz` 并修改代码相似度逻辑
- 无法从出版社下载？
  - 许多期刊需要校园网或机构授权（VPN）。可尝试 Unpaywall 或配置可用的 Sci-Hub 镜像
- 下载到的不是 PDF？
  - 程序会检查 `Content-Type` 和文件头并拒绝非 PDF 内容；可在日志中查看提示
- 递归很慢或被限流？
  - 使用 `--workers` + 合理的 `--rps`，并适当提高 `--timeout` 与 `--retries`

---

## 免责声明

- 本项目不提供、也不鼓励任何侵犯版权的行为。使用者需自行确保下载来源与方式符合当地法律及平台条款。
- 作者不对使用本工具造成的任何直接或间接后果负责。
