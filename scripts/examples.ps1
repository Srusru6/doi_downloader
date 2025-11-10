# 示例：常用运行方式（请先执行  .\.venv\Scripts\Activate.ps1  并安装依赖）

# 1) 基本用法：下载 1 层引用
python .\main.py --prod --doi 10.1126/science.177.4047.393 --depth 1 --workers 4

# 2) 多 DOI 输入（逗号分隔）
python .\main.py --prod --doi "10.1126/science.177.4047.393,10.1038/nphys1170" --depth 1

# 3) 使用 Unpaywall（建议配置邮箱）
python .\main.py --prod --doi 10.1038/nphys1170 --unpaywall-email you@example.com

# 4) 指定镜像（由使用者自备并确保合规，按序回退）
python .\main.py --prod --doi 10.1038/nphys1170 --scihub-domains "https://your-mirror-1.example,https://your-mirror-2.example"

# 5) 开启限速与增加重试/超时
python .\main.py --prod --doi 10.1038/nphys1170 --rps 1.5 --timeout 20 --retries 4 --backoff 0.6

# 6) 在第 2 层应用“年轻作者”筛选
python .\main.py --prod --doi 10.1038/nphys1170 --depth 2 --young --young-depth 2 --young-keywords "phd,博士,研究生"

# 7) 下载被引文章（前 15 篇）
python .\main.py --prod --doi 10.1038/nphys1170 --cited --cited-rows 15
