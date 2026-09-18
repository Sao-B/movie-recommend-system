#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TMDB 电影信息回填脚本（全项目唯一一个抓 TMDB 的脚本，海报/简介/演职员都在这里补）。

用途：给 movies 表补齐 TMDB 富化列——海报、简介、导演、编剧、主演、制片国家/地区、
语言、上映日期、片长。**只补空不置空**：UPDATE 一律走 COALESCE，取到值就写、取不到
就保留原值（所以绝不会有已填好的列被写回 NULL；海报会按 config 里的尺寸统一覆盖）。

每部电影按下述顺序取数，命中即止：

  主路径  links.csv 的 tmdbId -> /movie 直接查（MovieLens 映射权威，不做标题校验）
  兜底级联（主路径 404 或没取到封面/简介/导演时才走，从精确到宽松）：
    A. links.csv 的 imdbId -> /find 精确查找（覆盖 movie + tv）
    B. links.csv 的 tmdbId -> /movie 与 /tv 直接查（验证从严：标题精确 + 年份 ±2）
    C. /search/movie|tv 按标题 + 年份（年份容差 ±2，允许长词弱包含）
    D. 同上但不带年份（捕捉 TMDB 与 MovieLens 年份差得远的纪录片等）
    E. 无年份条目：仅按标题搜，只接受归一化后精确同名
    F. 冒号标题（"Sherlock: The Blind Banker" 实为剧集分集）退回冒号前缀搜节目名

历史教训（都修在里面了，别再改回去）：
  - 不用 requests/urllib3：本机单隧道代理会拒绝它的 CONNECT 握手（502/超时），
    但接受最小化 CONNECT 报文，所以裸 socket 直连代理。backend/venv 也没装 requests。
  - 海报尺寸统一取 config.yaml 的 tmdb.poster_base_url（默认 w500），不要再写死。
  - poster_url/director/writers/countries/languages 是 varchar(500)，超长会把整个任务
    崩掉（Data too long）；overview/cast 是 TEXT，不需要截断。
  - 搜索词不能带括号/年份（"Planet Earth (2006)" 极差）
  - MovieLens 倒装冠词（"... , The"）要还原自然语序
  - 搜索不带 language=zh-CN（会返回中文标题导致匹配失败），详情才用 zh-CN
  - 标题里的 "(2006–2007)" 年份区间可提取起始年当 eff_year
  - 弱包含匹配必须限长度（>=8 字符）且年份吻合，防 "Hide" 误挂
  - 不要再引入进度/队列表：待办与失败记录都由 movies 的缺列状态表达，一条 SQL 就能查
    （曾用过 tmp_search_queue、tmp_movie_fetch_queue，2026-09-16 一起取消）

进度不另存：待办和"上次没抓到"的记录都由数据库给出——缺 poster_url / overview /
director 的行既是待办、也是失败记录，一条 SQL 就能列出来（脚本结尾也会打印这个查询）::

    SELECT movie_id, title, year FROM movies
    WHERE COALESCE(poster_url, '') = '' OR overview IS NULL OR director IS NULL;

补上的行自动退出这个集合，没补上的（网络异常、TMDB 无匹配）下次运行自然重来。
单次运行内网络异常只跳过该条、不阻塞后面的条目，也不会留任何中间状态。

用法::

    python scripts/fetch_posters.py                # 全量补空，可反复运行（进度就是库里的缺口）
    python scripts/fetch_posters.py --ids=1,2,3    # 只跑指定 movie_id（调试用）
"""
import csv
import json
import os
import re
import socket
import ssl
import sys
import time
from urllib.parse import urlencode, urlsplit

import pymysql

# 本文件在 scripts/ 下：ROOT 是项目根目录（data/ 与 config.yaml 都在那里）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config_loader import mysql_kwargs, tmdb_config  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

LINKS_CSV = os.path.join(ROOT, "data", "raw", "links.csv")

# TMDB（凭据从 config.yaml 读取）
_tmdb = tmdb_config()
TMDB_BASE_URL = _tmdb["base_url"].rstrip("/")
TMDB_API_KEY = _tmdb["api_key"]
TMDB_IMAGE_BASE = _tmdb["poster_base_url"].rstrip("/")

# 本机单隧道代理（requests 走不通，见文件头说明）
PROXY_HOST, PROXY_PORT = "127.0.0.1", 65532

_SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- 网络层

def http_get_via_proxy(url, timeout=20):
    """裸 socket 实现的 HTTPS GET。

    本机单隧道代理会拒绝 urllib3/requests 的 CONNECT 握手（502/超时），
    但接受最小化 CONNECT 报文，因此绕开 requests 直接走 socket。
    返回 (status_code, body_bytes)；网络层异常向上抛出。
    """
    u = urlsplit(url)
    host, path = u.hostname, u.path + (f"?{u.query}" if u.query else "")
    sock = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("proxy closed during CONNECT")
            data += chunk
        status_line = data.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200" not in status_line:
            raise ConnectionError(f"proxy tunnel failed: {status_line}")
        tls_sock = _SSL_CTX.wrap_socket(sock, server_hostname=host)
        try:
            tls_sock.sendall(
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                f"User-Agent: Mozilla/5.0 (poster-fetch)\r\nConnection: close\r\n\r\n".encode()
            )
            body = b""
            while True:
                chunk = tls_sock.recv(65536)
                if not chunk:
                    break
                body += chunk
        finally:
            tls_sock.close()
        status = int(body.split(b"\r\n", 1)[0].split()[1])
        head, _, payload = body.partition(b"\r\n\r\n")
        if head.lower().find(b"transfer-encoding: chunked") >= 0:
            payload = _decode_chunked(payload)
        return status, payload
    except Exception:
        sock.close()
        raise


def _decode_chunked(data):
    out = b""
    while True:
        line_end = data.find(b"\r\n")
        if line_end < 0:
            break
        size = int(data[:line_end].split(b";")[0], 16)
        if size == 0:
            break
        out += data[line_end + 2 : line_end + 2 + size]
        data = data[line_end + 2 + size + 2 :]
    return out


def tmdb_get(path, params, retry=3):
    """GET TMDB。返回 (data|None, ok)；ok=False 表示网络异常，调用方跳过该条。"""
    url = f"{TMDB_BASE_URL}{path}?{urlencode(params)}"
    last_error = None
    for attempt in range(retry):
        try:
            status, body = http_get_via_proxy(url, timeout=20)
            if status == 200:
                return json.loads(body.decode("utf-8")), True
            if status == 404:
                return None, True
            if status == 429:
                print("  429 限速，等待 5s", flush=True)
                time.sleep(5)
                continue
            return None, True
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:80]}"
            if attempt < retry - 1:
                time.sleep(2)
                continue
    print(f"  网络失败 {path}: {last_error}", flush=True)
    return None, False


# ---------------------------------------------------------------- 字段工具

def cut(value, limit=500):
    """按列宽截断，防止 Data too long（历史崩溃根因）。"""
    if value is None:
        return None
    value = str(value).strip() or None
    if value and len(value) > limit:
        value = value[: limit - 3] + "..."
    return value


def norm_title(s):
    """标题归一化：小写、去任意括号段、去标点、挪动冠词（", The" -> "the ..."）。"""
    s = s.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s).strip()
    m = re.match(r"^(.*) (the|a|an|la|le|el|los|les|der|die|das|il|l)$", s)
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    return s


ARTICLE_RE = re.compile(
    r"^(.+), ((?i:The|An|A|La|Le|Les|Los|Las|El|Il|Der|Die|Das|Det|Den|De|Dem|Ein|Eine|Ett|O|Os|Un|Una|Une))$"
)


def build_query_variants(title, year):
    """返回 (搜索词列表, 归一化标题匹配集合, 生效年份)。

    - 搜索词去括号、还原倒装冠词；冒号标题追加"冒号前缀"变体（剧集分集退回节目名）
    - year 为空时尝试从标题里的 "(2006)" / "(2006–2007)" 提取
    """
    eff_year = year
    if eff_year is None:
        m = re.search(r"\((\d{4})", title)
        if m:
            eff_year = int(m.group(1))
    base = re.sub(r"\([^)]*\)", "", title).strip()
    m_inv = ARTICLE_RE.match(base)
    if m_inv:
        base = f"{m_inv.group(2).capitalize()} {m_inv.group(1)}"
    queries = []
    if base:
        queries.append(base)
    if ":" in base:
        prefix = base.split(":", 1)[0].strip()
        if len(prefix) >= 3:
            queries.append(prefix)
    titles = {norm_title(title), norm_title(base)}
    titles.discard("")
    return queries, titles, eff_year


def item_year(item):
    date = item.get("release_date") or item.get("first_air_date") or ""
    return int(date[:4]) if len(date) >= 4 else None


def item_title(item):
    return item.get("title") or item.get("name") or ""


def title_match(item, titles, year, weak_ok):
    """判断搜索结果条目是否可接受。

    - 归一化标题精确命中 + 年份 ±2（任一方无年份视为通过）
    - weak_ok 时允许 >=8 字符的弱包含 + 年份 ±2（捕捉副标题/音译差异）
    - year 传 None 时只接受精确命中（无年份条目防误挂）
    """
    n = norm_title(item_title(item))
    if not n:
        return False
    y = item_year(item)
    year_ok = y is None or year is None or abs(y - year) <= 2
    if n in titles and year_ok:
        return True
    if weak_ok and year is not None and year_ok:
        for t in titles:
            if len(t) >= 8 and (t in n or n in t):
                return True
    return False


def find_hit(results, titles, year, weak_ok=False):
    for item in results:  # TMDB 结果按热度排序，首个命中即取
        if title_match(item, titles, year, weak_ok):
            return item
    return None


# ---------------------------------------------------------------- 取数

def fetch_detail(kind, tid):
    """拉取详情（先 zh-CN，简介为空再回退 en-US），整理为待写入字段。

    返回 (info, ok)；ok=False 表示网络异常。info 为 None 表示 TMDB 无此条目。
    """
    detail, ok = tmdb_get(
        f"/{kind}/{tid}",
        {"api_key": TMDB_API_KEY, "language": "zh-CN", "append_to_response": "credits"},
    )
    if not ok:
        return None, False
    if not detail:
        return None, True
    if not (detail.get("overview") or "").strip():
        data2, ok2 = tmdb_get(
            f"/{kind}/{tid}",
            {"api_key": TMDB_API_KEY, "language": "en-US", "append_to_response": "credits"},
        )
        if ok2 and data2:
            detail = data2
    poster_path = detail.get("poster_path")
    crew = (detail.get("credits") or {}).get("crew", [])
    directors = [c["name"] for c in crew if c.get("job") == "Director"]
    writers = [c["name"] for c in crew if c.get("department") == "Writing"]
    cast = (detail.get("credits") or {}).get("cast", [])
    runtime = detail.get("runtime")
    if kind == "tv" and not runtime:
        eps = detail.get("episode_run_time") or []
        runtime = eps[0] if eps else None
    info = {
        "poster_url": f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None,
        "overview": (detail.get("overview") or "").strip() or None,
        "director": ", ".join(directors[:3]) if directors else None,
        "writers": ", ".join(writers[:5]) if writers else None,
        "cast": ", ".join(c["name"] for c in cast[:10]) if cast else None,
        "countries": ", ".join(c.get("name", "") for c in detail.get("production_countries", [])) or None,
        "languages": ", ".join(l.get("name", "") for l in detail.get("spoken_languages", [])) or None,
        "release_date": detail.get("release_date") or detail.get("first_air_date") or None,
        "runtime": runtime,
    }
    return info, True


def _search_paths(query, titles, year, weak_ok):
    """一个搜索词对 movie/tv 各搜一次（带年份），返回 ((kind,id)|None, ok)。"""
    for path, ykey, kind in (("/search/movie", "year", "movie"),
                             ("/search/tv", "first_air_date_year", "tv")):
        params = {"api_key": TMDB_API_KEY, "query": query}
        if year:
            params[ykey] = year
        data, ok = tmdb_get(path, params)
        if not ok:
            return None, False
        if data and data.get("results"):
            hit = find_hit(data["results"], titles, year, weak_ok=weak_ok and bool(year))
            if hit:
                return (kind, hit["id"]), True
        time.sleep(0.15)
    return None, True


def full_cascade(title, year, imdb_id, tmdb_id):
    """兜底策略级联。返回 (info, ok)；ok=False 表示网络异常，调用方跳过该条。"""
    queries, titles, eff_year = build_query_variants(title, year)
    if not queries:
        return None, True
    # 每个搜索词自身的归一化形式也纳入匹配集合——
    # 冒号前缀变体（如 "Sherlock"）搜到的是节目名，必须允许前缀本身精确命中
    titles |= {norm_title(q) for q in queries}

    # A. imdbId 精确查找：MovieLens 映射权威，标题或年份任一大致吻合即接受
    if imdb_id:
        data, ok = tmdb_get(
            f"/find/{imdb_id}",
            {"api_key": TMDB_API_KEY, "external_source": "imdb_id"},
        )
        if not ok:
            return None, False
        if data:
            for key, kind in (("movie_results", "movie"), ("tv_results", "tv")):
                for item in (data.get(key) or [])[:2]:
                    y, n = item_year(item), norm_title(item_title(item))
                    year_close = year is not None and y is not None and abs(y - year) <= 3
                    if year_close or (n in titles and (y is None or year is None)):
                        return fetch_detail(kind, item["id"])

    # B. tmdbId 直接查（movie/tv 都试）：验证从严，标题精确 + 年份 ±2
    if tmdb_id:
        for kind in ("movie", "tv"):
            data, ok = tmdb_get(f"/{kind}/{tmdb_id}", {"api_key": TMDB_API_KEY})
            if not ok:
                return None, False
            if data:
                n = norm_title(data.get("title") or data.get("name") or "")
                date = data.get("release_date") or data.get("first_air_date") or ""
                y = int(date[:4]) if len(date) >= 4 else None
                if n in titles and (y is None or eff_year is None or abs(y - eff_year) <= 2):
                    return fetch_detail(kind, tmdb_id)
            time.sleep(0.15)

    # C-F. 搜索级联：每个搜索词先带年份搜，再不带年份搜兜底；
    #       无年份条目（eff_year 无或 year 无）只接受精确同名
    for qi, query in enumerate(queries):
        weak_ok = qi == 0  # 冒号前缀变体只接受精确标题，防误挂
        for use_year in ((True, False) if eff_year else (False,)):
            hit, ok = _search_paths(query, titles, eff_year if use_year else None, weak_ok=weak_ok)
            if not ok:
                return None, False
            if hit:
                return fetch_detail(hit[0], hit[1])
    return None, True


def resolve(title, year, imdb_id, tmdb_id):
    """取一部电影的信息：主路径（tmdbId 直查）优先，没拿到再用兜底级联。

    返回 (info, ok)；ok=False 表示网络异常，调用方跳过该条（库里仍是缺口，下次重来）。
    """
    if tmdb_id:
        info, ok = fetch_detail("movie", tmdb_id)
        if not ok:
            return None, False
        if info and (info["poster_url"] or info["overview"] or info["director"]):
            return info, True
    return full_cascade(title, year, imdb_id, tmdb_id)


# ---------------------------------------------------------------- 写库

# poster_url/director/writers/countries/languages 是 varchar(500)，必须截断；
# overview/cast 是 TEXT，截断反而会丢内容。
CUT_COLS = ("poster_url", "director", "writers", "countries", "languages")
WRITE_COLS = ("poster_url", "overview", "director", "writers", "cast",
              "countries", "languages", "release_date", "runtime")

UPDATE_SQL = f"""UPDATE movies SET
    poster_url = COALESCE(%s, poster_url),
    overview = COALESCE(%s, overview),
    director = COALESCE(%s, director),
    writers = COALESCE(%s, writers),
    cast = COALESCE(%s, `cast`),
    countries = COALESCE(%s, countries),
    languages = COALESCE(%s, languages),
    release_date = COALESCE(%s, release_date),
    runtime = COALESCE(%s, runtime)
    WHERE movie_id = %s"""


def update_movie(cur, movie_id, info):
    """写库：每列 COALESCE 到已有值——取到值就写（会覆盖，例如把旧尺寸海报统一成
    config 里的尺寸），取不到就保留原值；绝不会把已填好的列写回 NULL。"""
    values = []
    for col in WRITE_COLS:
        v = info.get(col)
        values.append(cut(v) if col in CUT_COLS else v)
    values.append(movie_id)
    cur.execute(UPDATE_SQL, values)


def load_links():
    """movie_id -> (imdb_id 'tt.......' | None, tmdb_id int | None)"""
    links = {}
    with open(LINKS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                mid = int(row["movieId"])
            except (TypeError, ValueError):
                continue
            imdb_raw = (row.get("imdbId") or "").strip()
            tmdb_raw = (row.get("tmdbId") or "").strip()
            imdb_id = f"tt{int(imdb_raw):07d}" if imdb_raw.isdigit() and int(imdb_raw) > 0 else None
            tmdb_id = int(tmdb_raw) if tmdb_raw.isdigit() and int(tmdb_raw) > 0 else None
            links[mid] = (imdb_id, tmdb_id)
    return links


# ---------------------------------------------------------------- 主流程

# "待办清单"和"失败记录"是同一条 SQL：缺哪一列，就是哪一列没抓到。
GAP_WHERE = "COALESCE(poster_url, '') = '' OR overview IS NULL OR director IS NULL"
GAP_ORDER = "avg_rating IS NULL, avg_rating DESC, rating_count DESC"
FAIL_ABORT_AFTER = 20  # 连续这么多次网络失败即判定代理不可用，提前收工


def process_one(cur, conn, movie_id, title, year, links, stats, order):
    """抓一部并写库。返回 True=已处理（抓到，或确认 TMDB 无匹配），False=网络异常跳过。"""
    imdb_id, tmdb_id = links.get(movie_id, (None, None))
    info, ok = resolve(title, year, imdb_id, tmdb_id)
    prefix = f"[{order}] {title}"
    if not ok:
        stats["errors"] += 1
        print(f"{prefix} 网络异常，跳过", flush=True)
        return False
    if info and (info["poster_url"] or info["overview"] or info["director"]):
        update_movie(cur, movie_id, info)
        conn.commit()
        stats["success"] += 1
        got = "|".join(k for k, v in (("封面", info["poster_url"]), ("简介", info["overview"]),
                                      ("导演", info["director"])) if v)
        print(f"{prefix} -> {got}", flush=True)
    else:
        stats["no_match"] += 1
        print(f"{prefix} -> 无匹配", flush=True)
    return True


def gap_rows(cur):
    """待补清单——库里的缺口就是进度，也是"上次没抓到"的记录。"""
    cur.execute(f"SELECT movie_id, title, year FROM movies WHERE {GAP_WHERE} ORDER BY {GAP_ORDER}")
    return cur.fetchall()


def print_report(cur, stats):
    print("\n本轮结束", flush=True)
    print(f"  成功入库: {stats['success']} 部", flush=True)
    print(f"  无匹配:   {stats['no_match']} 部", flush=True)
    print(f"  网络异常跳过: {stats['errors']} 部", flush=True)
    print("  失败/待补记录直接查库（下次运行会自动重来）:", flush=True)
    print("    SELECT movie_id, title, year FROM movies", flush=True)
    print(f"    WHERE {GAP_WHERE};", flush=True)
    cur.execute(f"SELECT COUNT(*) FROM movies WHERE {GAP_WHERE}")
    print(f"  库里仍缺数据: {cur.fetchone()[0]} 部", flush=True)
    cur.execute("SELECT COUNT(*) FROM movies WHERE COALESCE(poster_url, '') <> ''")
    print(f"  有封面: {cur.fetchone()[0]}", flush=True)
    cur.execute("SELECT COUNT(*) FROM movies WHERE overview IS NOT NULL")
    print(f"  有简介: {cur.fetchone()[0]}", flush=True)


def run_ids(cur, conn, movie_ids, links):
    """--ids= 调试模式：只跑指定 id。"""
    fmt = ",".join(["%s"] * len(movie_ids))
    cur.execute(f"SELECT movie_id, title, year FROM movies WHERE movie_id IN ({fmt})", tuple(movie_ids))
    rows = cur.fetchall()
    stats = {"success": 0, "no_match": 0, "errors": 0}
    for i, (mid, title, year) in enumerate(rows, 1):
        process_one(cur, conn, mid, title, year, links, stats, i)
    conn.commit()
    print(f"\n调试完成：成功 {stats['success']}，无匹配 {stats['no_match']}，网络异常 {stats['errors']}", flush=True)


def run_all(cur, conn, links):
    """全量补空：取一次库里的缺口清单，逐个处理，网络失败的直接跳过、不留状态。"""
    rows = gap_rows(cur)
    if not rows:
        print("库里没有待补条目", flush=True)
        return
    print(f"待补 {len(rows)} 部（按评分降序，主页缺口优先）", flush=True)
    stats = {"success": 0, "no_match": 0, "errors": 0}
    streak = 0
    for i, (mid, title, year) in enumerate(rows, 1):
        if process_one(cur, conn, mid, title, year, links, stats, i):
            streak = 0
            time.sleep(0.2)
            continue
        streak += 1
        if streak >= FAIL_ABORT_AFTER:
            print(f"连续 {streak} 次网络失败，判定代理不可用，提前收工", flush=True)
            break
        time.sleep(1)  # 只是给代理一点恢复时间，跳过的条目下次运行再来
    print_report(cur, stats)


def main():
    movie_ids = None
    for arg in sys.argv[1:]:
        if arg.startswith("--ids="):
            movie_ids = [int(x) for x in arg.split("=", 1)[1].split(",") if x.strip()]
        else:
            print(f"未知参数: {arg}\n用法: python scripts/fetch_posters.py [--ids=1,2,3]", flush=True)
            return 2

    if not os.path.exists(LINKS_CSV):
        print(f"缺少 {LINKS_CSV}，无法建立 movie_id -> tmdbId/imdbId 映射", flush=True)
        return 1

    conn = pymysql.connect(**mysql_kwargs())
    cur = conn.cursor()
    links = load_links()
    print(f"links.csv 加载 {len(links)} 条映射", flush=True)
    print(f"海报尺寸: {TMDB_IMAGE_BASE}（config.yaml 的 tmdb.poster_base_url）", flush=True)

    try:
        if movie_ids is not None:
            run_ids(cur, conn, movie_ids, links)
        else:
            run_all(cur, conn, links)
    except KeyboardInterrupt:
        print("\n手动中断，已提交的部分保留在库里，下次运行继续", flush=True)
    finally:
        cur.close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
