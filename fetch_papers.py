import argparse
import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import datetime
import logging
import os
import sys
import time
import json
import shutil
import csv
import signal
import atexit

# ------------------------------------------------------------------------------
# 一、持久化文件路径（所有文件均放在 ./output/ 下）
# ------------------------------------------------------------------------------
RUN_CONDITION = "./output/last_run.json"      # 记录“上一次”搜索条件，用于判断是否需要清空历史
DONE_LOG = "./output/done_journals.txt"       # 每行记录已完成的 journal_abbr，防止重复抓取
CACHE_CSV = "./output/papers_cache.csv"       # 追加写入所有已抓到的论文，断点续跑时直接沿用

# ------------------------------------------------------------------------------
# 二、工具函数：已完成期刊的加载 / 标记
# ------------------------------------------------------------------------------
def load_done():
    """返回 set：已完成的 journal_abbr"""
    if not os.path.exists(DONE_LOG):
        return set()
    with open(DONE_LOG, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def mark_done(journal_abbr):
    """原子地写入一条完成记录"""
    with open(DONE_LOG, "a", encoding="utf-8") as f:
        f.write(journal_abbr + "\n")

###############################################################################
# 3. 工具：缓存论文（追加写）
###############################################################################

# ------------------------------------------------------------------------------
# 三、工具函数：论文缓存（追加写 CSV）
# ------------------------------------------------------------------------------
def append_papers(papers):
    """
    将 papers 列表（元素为 dict）追加到 CACHE_CSV
    如果文件不存在则写表头，存在则纯追加
    """
    if not papers:
        return
    df = pd.DataFrame(papers)
    df.to_csv(CACHE_CSV, mode="a", header=not os.path.exists(CACHE_CSV),
              index=False, encoding="utf-8")

# ------------------------------------------------------------------------------
# 四、运行条件读写：用于“增量 or 全量”判断
# ------------------------------------------------------------------------------
def load_last_condition():
    """若存在 RUN_CONDITION 则加载 dict，否则返回 None"""
    if not os.path.exists(RUN_CONDITION):
        return None
    try:
        with open(RUN_CONDITION, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_condition(cond):
    """原子写入 JSON，避免写坏文件"""
    tmp = RUN_CONDITION + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cond, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RUN_CONDITION)


def clear_history():
    """清空所有历史文件，用于“搜索条件变化”时重跑"""
    files = [DONE_LOG, CACHE_CSV, RUN_CONDITION,
             "./output/fetch_papers.log"]
    for f in files:
        if os.path.exists(f):
            os.remove(f)
    # 如果之前生成过 Excel 也顺手删掉
    xlsx = "./output/papers_results.xlsx"
    if os.path.exists(xlsx):
        os.remove(xlsx)

# ------------------------------------------------------------------------------
# 五、日志配置（同时输出到控制台 + 文件）
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # 控制台
        logging.FileHandler("./output/fetch_papers.log", mode='w')  # 文件
    ]
)


# ------------------------------------------------------------------------------
# 六、期刊过滤：根据 CCF 等级、指定缩写、分类号筛选
# ------------------------------------------------------------------------------
def load_and_filter_journals(csv_file, journal_level, selected_journals=None, selected_categories=None):
    """
    读取 CCF 官方 CSV，返回符合条件的期刊/会议列表
    返回：list -> [刊物简称, 刊物全称, 类型, 网址, 级别]
    """
    ccf_levels = ['A', 'B', 'C']
    df = pd.read_csv(csv_file)

    # 1. 按等级过滤
    if journal_level in ccf_levels:
        valid_levels = ccf_levels[:ccf_levels.index(journal_level) + 1]
    else:
        logging.warning(
            f"Invalid journal level: {journal_level}. Using all levels.")
        valid_levels = ccf_levels

    filtered_df = df[df['级别'].isin(valid_levels)]

    # 2. 按指定缩写过滤（命令行 -j 参数）
    if selected_journals:
        selected_journals = [abbr.strip().lower()
                             # Convert to lowercase
                             for abbr in selected_journals]
        filtered_df = filtered_df[filtered_df['刊物简称'].str.lower().isin(
            selected_journals)]

    # 3. 按分类号（序号）过滤（命令行 -c 参数）
    if selected_categories:
        # Convert to string and strip
        selected_categories = [str(cat).strip() for cat in selected_categories]
        filtered_df.loc[:, '序号'] = filtered_df['序号'].astype(
            str).str.strip()  # Ensure '序号' is string and strip
        filtered_df = filtered_df[filtered_df['序号'].isin(selected_categories)]

    # 仅保留所需列，并填充空简称
    filtered_df = filtered_df[['刊物简称', '刊物全称', '类型', '网址', '级别']]
    filtered_df['刊物简称'] = filtered_df['刊物简称'].fillna(filtered_df['刊物全称'])

    return filtered_df.values.tolist()



# ------------------------------------------------------------------------------
# 七、统一调度：根据期刊/会议类型分发到具体抓取函数
# ------------------------------------------------------------------------------
def fetch_papers(abbr, name, journal_type, baseurl, years, keywords, ccf_rank):
    """
    外部唯一入口：根据类型调用 journal 或 conference 抓取器
    返回：list[dict]
    """
    fetcher = {
        'conference': fetch_conference_papers,
        'journal': fetch_journal_papers
    }.get(journal_type.lower())

    if fetcher:
        # 多传一个 ccf_rank
        return fetcher(abbr, name, baseurl, years, keywords, ccf_rank)
    else:
        logging.error(f"Unknown type: {journal_type}")
        return []


# ------------------------------------------------------------------------------
# 八、DBLP 真正抓取的通用逻辑（含重试、被封检测）
# ------------------------------------------------------------------------------
def fetch_from_dblp(abbr, name, baseurl, years, keywords, entry_type, volume_pattern=False, ccf_rank=None):
    """
    核心通用抓取函数
    参数：
        volume_pattern: True  代表期刊（按 Volume/year 跳转）
                       False 代表会议（按年份 h2 跳转）
    """
    papers = []
    max_retries = 10

    for year in years:
        logging.info(f"Fetching papers in {abbr} ({year})...")
        retries = 0
        resp = None
        while retries < max_retries:
            try:
                resp = requests.get(baseurl, timeout=30)
                if resp.status_code != 200:
                    # 任何异常状态码都视为“被封”，立即保存并退出
                    logging.error(
                        f"HTTP {resp.status_code} — 可能被 DBLP 封禁，立即保存现场并退出。")
                    append_papers(papers)          # 把已抓到的先落盘
                    sys.exit(1)
                break
            except requests.exceptions.RequestException as e:
                logging.error(f"Request exception: {e}")
                retries += 1
                if retries >= max_retries:
                    logging.error("Max retries reached, 主动退出。")
                    append_papers(papers)
                    sys.exit(1)
                time.sleep(5)
                continue

        # ---------- 解析当前列表页 ----------
        soup = BeautifulSoup(resp.text, 'html.parser')

        if volume_pattern:
            pattern = re.compile(rf"Volume\s+\d+[:,]\s*{year}")
            link_tag = soup.find('a', string=pattern)
            if not link_tag:
                logging.warning(f"No data for {year}")
                continue
            url = link_tag['href'] if volume_pattern else baseurl
            logging.info(f"Search in {url}...")
            year_papers = parse_paper_entries(
                url, abbr, name, year, keywords, entry_type, ccf_rank)
            papers += year_papers
            logging.info(f"Year {year}: Found {len(year_papers)} papers.")

        else:
            # 会议：找 <h2 id="year"> 下相邻的 publ-list
            h2_tag = soup.find('h2', {'id': str(year)})
            link_tag = h2_tag.find_next(
                'ul', class_='publ-list') if h2_tag else None
            if not link_tag:
                logging.warning(f"No data for {year}")
                continue
            navs = link_tag.find_all('nav', class_='publ')
            year_papers = []
            for nav in navs:
                url = nav.find_next(
                    'li', class_='drop-down').find_next('a')['href']
                logging.info(f"Search in {url}...")
                year_papers += parse_paper_entries(
                    url, abbr, name, year, keywords, entry_type, ccf_rank)
            papers += year_papers
            logging.info(f"Year {year}: Found {len(year_papers)} papers.")

    return papers


def fetch_conference_papers(abbr, name, baseurl, years, keywords, ccf_rank):
    return fetch_from_dblp(abbr, name, baseurl, years, keywords,
                           entry_type='entry inproceedings', ccf_rank=ccf_rank)

def fetch_journal_papers(abbr, name, baseurl, years, keywords, ccf_rank):
    return fetch_from_dblp(abbr, name, baseurl, years, keywords,
                           entry_type='entry article', volume_pattern=True, ccf_rank=ccf_rank)

# Parse paper entries from DBLP


# ------------------------------------------------------------------------------
# 九、解析 DBLP 详情页：提取标题、DOI、关键字匹配
# ------------------------------------------------------------------------------
def parse_paper_entries(url, abbr, name, year, keywords, entry_type, ccf_rank):
    """
    进入 DBLP 的详情页（如 html/db/conf/xxx/xxxx.html）
    返回命中关键词的论文列表
    """
    response = requests.get(url)
    if response.status_code != 200:
        logging.error(f"Failed to fetch paper entries: {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, 'html.parser')
    papers = []

    for entry in soup.find_all('li', class_=entry_type):
        title_tag = entry.find('span', class_='title')
        if not title_tag:
            continue

        title = title_tag.text.strip()
        doi = entry.find_next('nav', class_='publ').find_next(
            'li', class_='drop-down').find_next('a')['href']
        
        # 关键词匹配：任一关键词命中即记录
        for keyword in keywords:
            if keyword.lower() in title.lower():
                papers.append({
                    'Name': name,
                    'Abbreviation': abbr,
                    'CCF_Rank': ccf_rank,
                    'Type': 'Conference' if 'inproceedings' in entry_type else 'Journal',
                    'Year': year,
                    'Keyword': keyword,
                    'Title': title,
                    'DOI': doi
                })
                break   # 避免同一篇论文因多个关键词重复插入
    return papers


# ------------------------------------------------------------------------------
# 十、Excel 工作表名非法字符处理
# ------------------------------------------------------------------------------
def sanitize_sheet_name(sheet_name):
    """Replace invalid characters in sheet name with underscores."""
    return re.sub(r'[\\/*?:\[\]]', '_', sheet_name)


# ------------------------------------------------------------------------------
# 十一、主流程：命令行参数 -> 过滤期刊 -> 循环抓取 -> 落盘
# ------------------------------------------------------------------------------
def main(csv_file_path, journal_level, num_years, keywords,
         selected_journals, selected_categories):
     # -------------------- 1. 构造本次条件对象 --------------------
    this_cond = {
        "csv": os.path.abspath(csv_file_path),
        "level": journal_level,
        "years": num_years,
        "keywords": sorted(keywords),          # 排序防止顺序不同被判为不同
        "journals": sorted(selected_journals) if selected_journals else None,
        "categories": sorted(selected_categories) if selected_categories else None
    }

    # -------------------- 2. 与上次条件对比 --------------------
    last_cond = load_last_condition()
    if last_cond != this_cond:
        logging.info("搜索条件与上次不同，清空历史记录，重新抓取。")
        clear_history()
        save_condition(this_cond)
    else:
        logging.info("搜索条件与上次一致，沿用历史结果，继续增量抓取。")

    current_year = datetime.datetime.now().year
    years_to_search = list(
        range(current_year - num_years + 1, current_year + 1))

    # -------------------- 3. 打印本次参数 --------------------
    logging.info("----------------------------------------------------")
    logging.info(f"Searching for papers from the following parameters:")
    logging.info(f"Journal level: {journal_level}")
    logging.info(f"Years: {years_to_search}")
    logging.info(f"Keywords: {', '.join(keywords)}")
    if selected_journals:
        logging.info(
            f"Selected journals/conferences: {', '.join(selected_journals)}")
    else:
        logging.info("All journals/conferences will be included.")
    if selected_categories:
        logging.info(f"Selected categories: {', '.join(selected_categories)}")
    else:
        logging.info("All categories will be included.")
    logging.info("----------------------------------------------------")

    # -------------------- 4. 加载并过滤期刊 --------------------
    journal_list = load_and_filter_journals(
        csv_file_path, journal_level, selected_journals, selected_categories)

    # -------------------- 5. 准备输出目录 --------------------
    output_dir = "output"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    output_file = os.path.join(output_dir, "papers_results.xlsx")

    # 删除已存在文件
    if os.path.exists(output_file):
        os.remove(output_file)

    # -------------------- 6. 断点续跑：跳过已完成的 --------------------
    done_set = load_done()
    todo_list = [j for j in journal_list if j[0] not in done_set]
    total_journals = len(todo_list)
    logging.info(
        f"跳过 {len(journal_list)-total_journals} 个已完成期刊，剩余 {total_journals} 个待抓。")
    os.makedirs("output", exist_ok=True)

    total_count = 0

    # -------------------- 7. 主循环：逐个期刊抓取 --------------------
    for idx, journal in enumerate(todo_list, start=1):
        abbr, name, jtype, url, ccf_rank = journal
        logging.info("----------------------------------------------------")
        logging.info(f"Processing journal {idx}/{total_journals}  {abbr}")
        logging.info("----------------------------------------------------")

        try:
            papers = fetch_papers(abbr, name, jtype, url,
                                  years_to_search, keywords, ccf_rank)
        except SystemExit:
            # fetch_from_dblp 里已经做了退出前的保存，这里直接 re-raise
            raise
        except Exception as e:
            # 其它未预料的异常也保存后退出
            logging.exception("Unexpected error, 保存后退出。")
            sys.exit(1)

        if papers:
            append_papers(papers)
            total_count += len(papers)
            logging.info(f"{abbr} 抓到 {len(papers)} 篇，已追加到 {CACHE_CSV}")
        else:
            logging.info(f"{abbr} 无论文。")

        # 无论成功/空结果，都视为“完成”，防止反复重试
        mark_done(abbr)

    logging.info(f"全部完成，总计 {total_count} 篇论文，缓存文件：{CACHE_CSV}")


# ------------------------------------------------------------------------------
# 十二、命令行入口
# ------------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Fetch papers from CCF journals and conferences.")
    # CCF 官方期刊/会议 CSV 文件路径
    parser.add_argument('--csv', type=str, default='./ccf2022.csv',
                        help='Path to the CCF journal/conference CSV file.')
    # CCF 等级筛选（A/B/C）
    parser.add_argument('--level', type=str, default='C',
                        help='CCF rank to filter (e.g., A, B, C).')
    # 抓取最近 N 年（含今年）
    parser.add_argument('--years', type=int, default=3,
                        help='Number of recent years to search.')
    # 英文关键词，半角逗号分隔（如 "federated learning,edge computing"）
    parser.add_argument('--keywords', type=str, required=True,
                        help='Comma-separated keywords to search.')
    # 指定期刊/会议缩写，半角逗号分隔（如 tc,tpds,isca）
    parser.add_argument('--journals', type=str, default=None,
                        help='Comma-separated journal/conference abbreviations to filter (e.g., tc,tpds,isca).')
    # 指定分类号（序号），半角逗号分隔（如 1,2,3）
    parser.add_argument('--categories', type=str, default=None,
                        help='Comma-separated categories (序号) to filter. If not specified, all categories will be included.')

    args = parser.parse_args()

    # 将命令行字符串拆成 list
    keywords_list = [kw.strip().replace('_', ' ')
                     for kw in args.keywords.split(',')]
    journals_list = [abbr.strip().replace('_', ' ') for abbr in args.journals.split(
        ',')] if args.journals else None
    categories_list = [cat.strip() for cat in args.categories.split(
        ',')] if args.categories else None

    # 启动主流程
    main(args.csv, args.level, args.years,
         keywords_list, journals_list, categories_list)