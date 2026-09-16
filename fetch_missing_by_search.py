#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对 movies 表中信息不全的剩余电影，用全部可用策略补抓 TMDB 数据。

策略级联（从精确到宽松，每部电影按序尝试，命中即止）：
  A. links.csv 的 imdbId -> /find 精确查找（覆盖 movie+tv，MovieLens 映射权威，验证从宽）
  B. links.csv 的 tmdbId -> /movie 与 /tv 直接查（验证从严：标题精确 + 年份 ±2）
  C. /search/movie|tv 按标题+年份（年份容差 ±2，允许长词弱包含）
  D. 同上但不带年份（捕捉 TMDB 与 MovieLens 年份差得远的纪录片等）
  E. 无年份条目：仅按标题搜，只接受归一化后精确同名
  F. 冒号标题（如 "Sherlock: The Blind Banker" 实为剧集分集）退回冒号前缀搜节目名

历史教训（都修在里面了）：
  - 搜索词不能带括号/年份（"Planet Earth (2006)" 极差）
  - MovieLens 倒装冠词（"... , The"）要还原自然语序
  - 搜索不带 language=zh-CN（会返回中文标题导致匹配失败），详情才用 zh-CN
  - 标题里的 "(2006–2007)" 年份区间可提取起始年当 eff_year
  - 弱包含匹配必须限长度（>=8 字符）且年份吻合，防 "Hide" 误挂

断点与进度存于临时队列表 tmp_search_queue（movie_id PK），跑空即完成。
测试模式：python fetch_missing_by_search.py --ids=1,2,3 （只跑指定 id，不动队列）
"""
import csv
import difflib
import json
import re
import socket
import ssl
import sys
import time
import pymysql

from urllib.parse import urlencode, urlsplit

from config_loader import mysql_kwargs, tmdb_config

_t = tmdb_config()
TMDB_BASE_URL = _t["base_url"]  # https://api.themoviedb.org
TMDB_API_KEY = _t["api_key"]
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w780"
PROXY_HOST, PROXY_PORT = "127.0.0.1", 65532

QUEUE_TABLE = "tmp_search_queue"
LINKS_CSV = "data/raw/links.csv"

_SSL_CTX = ssl.create_default_context()


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


def fuzzy_match(results, titles, year, threshold=0.85, min_len=6):
    """difflib 相似度兜底：整页结果里挑与标题集最像的一条。

    门槛从紧：相似度 >= threshold、最短标题 >= min_len 字符
    （防 "Alien"/"Aliens" 这类短词互挂）、年份 ±3（任一方无年份放行）。
    """
    best, best_r = None, 0.0
    for item in results:
        n = norm_title(item_title(item))
        if not n:
            continue
        r = max(difflib.SequenceMatcher(None, t, n).ratio() for t in titles)
        if r > best_r:
            best, best_r = item, r
    if best is None or best_r < threshold:
        return None
    if min(len(t) for t in titles) < min_len:
        return None
    y = item_year(best)
    if year is not None and y is not None and abs(y - year) > 3:
        return None
    return best


def fetch_detail(kind, tid):
    """拉取详情（先 zh-CN，简介为空再回退 en-US），整理为待写入字段。"""
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
    """策略级联入口。返回 (info, ok)；ok=False 表示网络异常需重试。"""
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
                    loose_year = year is not None and y is not None and abs(y - year) <= 3
                    if (n in titles and (y is None or year is None or loose_year or abs(y - year) <= 3)) or loose_year:
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


def load_links():
    """movie_id -> (imdb_id 'tt.......' | None, tmdb_id | None)"""
    links = {}
    with open(LINKS_CSV, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                mid = int(row["movieId"])
            except (TypeError, ValueError):
                continue
            imdb_raw = (row.get("imdbId") or "").strip()
            tmdb_raw = (row.get("tmdbId") or "").strip()
            imdb_id = None
            if imdb_raw.isdigit() and int(imdb_raw) > 0:
                imdb_id = f"tt{int(imdb_raw):07d}"
            tmdb_id = None
            if tmdb_raw.isdigit() and int(tmdb_raw) > 0:
                tmdb_id = int(tmdb_raw)
            links[mid] = (imdb_id, tmdb_id)
    return links


def process_one(cur, conn, movie_id, title, year, links, stats, total, done):
    imdb_id, tmdb_id = links.get(movie_id, (None, None))
    info, ok = full_cascade(title, year, imdb_id, tmdb_id)
    if not ok:
        stats["errors"] += 1
        cur.execute(f"UPDATE {QUEUE_TABLE} SET attempts = attempts + 1 WHERE movie_id = %s", (movie_id,))
        conn.commit()
        print(f"[{done + stats['errors']}/{total}] {title} 网络异常，跳过稍后重试", flush=True)
        time.sleep(3)
        return False
    if info and (info["poster_url"] or info["overview"] or info["director"]):
        cur.execute(
            """UPDATE movies SET poster_url = COALESCE(%s, poster_url),
                   overview = COALESCE(%s, overview), director = COALESCE(%s, director),
                   writers = COALESCE(%s, writers), cast = COALESCE(%s, `cast`),
                   countries = COALESCE(%s, countries), languages = COALESCE(%s, languages),
                   release_date = COALESCE(%s, release_date), runtime = COALESCE(%s, runtime)
               WHERE movie_id = %s""",
            (
                cut(info["poster_url"]), cut(info["overview"]), cut(info["director"]),
                cut(info["writers"]), cut(info["cast"]), cut(info["countries"]),
                cut(info["languages"]), cut(info["release_date"]),
                info["runtime"], movie_id,
            ),
        )
        stats["success"] += 1
        got = "|".join(k for k, v in (("封面", info["poster_url"]), ("简介", info["overview"]), ("导演", info["director"])) if v)
        print(f"[{done + stats['errors'] + stats['success'] + stats['no_match']}/{total}] {title} -> {got}", flush=True)
    else:
        stats["no_match"] += 1
        print(f"[{done + stats['errors'] + stats['success'] + stats['no_match']}/{total}] {title} -> 无匹配", flush=True)
    return True


def main():
    conn = pymysql.connect(**mysql_kwargs())
    cur = conn.cursor()
    cur.execute(
        f"""CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
            movie_id INT PRIMARY KEY,
            attempts INT NOT NULL DEFAULT 0
        ) ENGINE=InnoDB"""
    )
    conn.commit()

    test_ids = None
    for arg in sys.argv[1:]:
        if arg.startswith("--ids="):
            test_ids = [int(x) for x in arg.split("=", 1)[1].split(",") if x.strip()]

    links = load_links()
    print(f"links.csv 加载 {len(links)} 条映射", flush=True)

    if test_ids is not None:
        fmt = ",".join(["%s"] * len(test_ids))
        cur.execute(f"SELECT movie_id, title, year FROM movies WHERE movie_id IN ({fmt})", tuple(test_ids))
        rows = cur.fetchall()
        conn.commit()
        total = len(rows)
        stats = {"success": 0, "no_match": 0, "errors": 0}
        for i, (mid, title, year) in enumerate(rows):
            process_one(cur, conn, mid, title, year, links, stats, total, i)
        conn.commit()
        print("测试完成", stats)
        cur.close()
        conn.close()
        return

    cur.execute(f"SELECT COUNT(*) FROM {QUEUE_TABLE}")
    queued = cur.fetchone()[0]
    if queued == 0:
        cur.execute(
            """INSERT INTO tmp_search_queue (movie_id)
               SELECT movie_id FROM movies
               WHERE COALESCE(poster_url, '') = '' OR overview IS NULL OR director IS NULL
               ORDER BY avg_rating IS NULL, avg_rating DESC, rating_count DESC"""
        )
        conn.commit()
        print(f"队列灌入 {cur.rowcount} 部（按 avg_rating 降序，主页缺口优先）", flush=True)
    else:
        print(f"从队列恢复，剩余 {queued} 部", flush=True)

    stats = {"success": 0, "no_match": 0, "errors": 0}
    done = 0
    cur.execute(f"SELECT COUNT(*) FROM {QUEUE_TABLE}")
    total = cur.fetchone()[0]
    failed_ids = []        # 本轮网络失败的电影，先跳过换下一部，轮空一轮后重试
    all_failed_rounds = 0  # 整轮全部网络失败的次数，连续 3 轮说明网络确实不可用
    while True:
        skip_clause = ""
        params = ()
        if failed_ids:
            skip_clause = "WHERE q.movie_id NOT IN (" + ",".join(["%s"] * len(failed_ids)) + ")"
            params = tuple(failed_ids)
        cur.execute(
            f"""SELECT q.movie_id, m.title, m.year FROM {QUEUE_TABLE} q
                JOIN movies m ON m.movie_id = q.movie_id
                {skip_clause}
                ORDER BY m.avg_rating IS NULL, m.avg_rating DESC, m.rating_count DESC
                LIMIT 1""",
            params,
        )
        row = cur.fetchone()
        if not row:
            if failed_ids:
                all_failed_rounds += 1
                if all_failed_rounds >= 3:
                    print("连续 3 轮全部网络失败，判定网络不可用，中止", flush=True)
                    break
                print(f"第 {all_failed_rounds} 轮 {len(failed_ids)} 部全部网络失败，开始重试...", flush=True)
                failed_ids = []
                continue
            break
        movie_id, title, year = row
        ok = process_one(cur, conn, movie_id, title, year, links, stats, total, done)
        if not ok:
            failed_ids.append(movie_id)
            continue
        failed_ids.clear()
        all_failed_rounds = 0
        cur.execute(f"DELETE FROM {QUEUE_TABLE} WHERE movie_id = %s", (movie_id,))
        conn.commit()
        done += 1
        time.sleep(0.2)

    print("\n完成！", flush=True)
    print(f"  成功入库: {stats['success']} 部", flush=True)
    print(f"  无匹配: {stats['no_match']} 部", flush=True)
    print(f"  网络异常留队: {stats['errors']} 部", flush=True)
    cur.execute(f"SELECT COUNT(*) FROM {QUEUE_TABLE}")
    print(f"  队列剩余: {cur.fetchone()[0]} 部", flush=True)
    cur.execute("SELECT COUNT(*) FROM movies WHERE COALESCE(poster_url, '') <> ''")
    print(f"  有封面: {cur.fetchone()[0]}", flush=True)
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
