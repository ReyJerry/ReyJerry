#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, datetime, time
from dateutil.relativedelta import relativedelta
import requests

LOGIN = os.environ.get("GH_LOGIN", "").strip()
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

API = "https://api.github.com/graphql"
HEADERS = {"Authorization": f"bearer {TOKEN}"}

def gql(query, variables=None):
    r = requests.post(API, headers=HEADERS, json={"query": query, "variables": variables or {}})
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]

# ---------- data fetch ----------

def get_own_public_repos_and_total_stars():
    """own public (non-fork) repos + total stars, sorted by stars -> forks"""
    repos, total = [], 0
    cursor = None
    while True:
        q = """
        query($login:String!, $cursor:String) {
          user(login:$login){
            repositories(ownerAffiliations: OWNER, isFork:false, privacy:PUBLIC, first:100, after:$cursor){
              pageInfo { hasNextPage endCursor }
              nodes { nameWithOwner url stargazerCount forkCount }
            }
          }
        }"""
        d = gql(q, {"login": LOGIN, "cursor": cursor})
        page = d["user"]["repositories"]
        for n in page["nodes"]:
            total += n["stargazerCount"]
            repos.append({
                "name": n["nameWithOwner"],
                "url": n["url"],
                "stars": n["stargazerCount"],
                "forks": n["forkCount"],
            })
        if page["pageInfo"]["hasNextPage"]:
            cursor = page["pageInfo"]["endCursor"]
        else:
            break
    repos.sort(key=lambda x: (-x["stars"], -x["forks"]))
    return repos, total

def get_years():
    q = """
    query($login:String!){
      user(login:$login){
        contributionsCollection { contributionYears }
      }
    }"""
    d = gql(q, {"login": LOGIN})
    years = sorted(set(d["user"]["contributionsCollection"]["contributionYears"]))
    y = datetime.datetime.utcnow().year
    if y not in years:
        years.append(y)
    return years

def collect_by_year(year):
    start = datetime.datetime(year, 1, 1)
    end = datetime.datetime(year + 1, 1, 1) - relativedelta(seconds=1)
    q = """
    query($login:String!, $from:DateTime!, $to:DateTime!){
      user(login:$login){
        contributionsCollection(from:$from, to:$to){
          pullRequestContributionsByRepository(maxRepositories:100) {
            repository { nameWithOwner url stargazerCount forkCount }
            contributions(first:1){ totalCount }
          }
          issueContributionsByRepository(maxRepositories:100) {
            repository { nameWithOwner url stargazerCount forkCount }
            contributions(first:1){ totalCount }
          }
          commitContributionsByRepository(maxRepositories:100) {
            repository { nameWithOwner url stargazerCount forkCount }
            contributions(first:1){ totalCount }
          }
        }
      }
    }"""
    d = gql(q, {"login": LOGIN, "from": start.isoformat(), "to": end.isoformat()})
    cc = d["user"]["contributionsCollection"]

    repo_map = {}
    def add(repo, key, n):
        k = repo["nameWithOwner"]
        repo_map.setdefault(k, {
            "url": repo["url"], "stars": repo["stargazerCount"], "forks": repo["forkCount"],
            "commit": 0, "pr": 0, "issue": 0
        })
        repo_map[k][key] += n
        repo_map[k]["stars"] = repo["stargazerCount"]
        repo_map[k]["forks"] = repo["forkCount"]
        repo_map[k]["url"] = repo["url"]

    for r in cc["commitContributionsByRepository"]:
        add(r["repository"], "commit", r["contributions"]["totalCount"])
    for r in cc["pullRequestContributionsByRepository"]:
        add(r["repository"], "pr", r["contributions"]["totalCount"])
    for r in cc["issueContributionsByRepository"]:
        add(r["repository"], "issue", r["contributions"]["totalCount"])

    return repo_map

def get_pr_lines():
    """Fallback: Get additions and deletions from user's merged PRs"""
    pr_stats = {}
    cursor = None
    try:
        while True:
            q = """
            query($login:String!, $cursor:String) {
              user(login:$login){
                pullRequests(states: [MERGED], first: 100, after: $cursor){
                  pageInfo { hasNextPage endCursor }
                  nodes {
                    repository { nameWithOwner }
                    additions
                    deletions
                  }
                }
              }
            }"""
            d = gql(q, {"login": LOGIN, "cursor": cursor})
            page = d["user"]["pullRequests"]
            for n in page["nodes"]:
                repo = n["repository"]["nameWithOwner"]
                if repo not in pr_stats:
                    pr_stats[repo] = {"additions": 0, "deletions": 0}
                pr_stats[repo]["additions"] += n["additions"]
                pr_stats[repo]["deletions"] += n["deletions"]
            
            if page["pageInfo"]["hasNextPage"]:
                cursor = page["pageInfo"]["endCursor"]
            else:
                break
    except Exception as e:
        print(f"Warning: Fetching PR lines failed: {e}", file=sys.stderr)
        
    return pr_stats

def aggregate_contributions_all_time():
    years = get_years()
    merged = {}
    for y in years:
        part = collect_by_year(y)
        for name, rec in part.items():
            if name not in merged:
                merged[name] = rec.copy()
            else:
                for k in ("commit", "pr", "issue"):
                    merged[name][k] += rec[k]
            merged[name]["stars"] = rec["stars"]
            merged[name]["forks"] = rec["forks"]
            merged[name]["url"] = rec["url"]

    mine, others = [], []
    for name, v in merged.items():
        total = v["commit"] + v["pr"] + v["issue"]
        if total == 0:
            continue
        row = {
            "name": name, "url": v["url"], "stars": v["stars"], "forks": v["forks"],
            "commit": v["commit"], "pr": v["pr"], "issue": v["issue"], "total": total,
            "additions": 0, "deletions": 0
        }
        owner = name.split("/")[0].lower() if "/" in name else ""
        (mine if owner == LOGIN.lower() else others).append(row)

    keyf = lambda r: (-r["stars"], -r["forks"])
    mine.sort(key=keyf)
    others.sort(key=keyf)

    # Fetch Code Lines Data
    pr_lines = get_pr_lines()

    def fetch_lines(repos):
        for r in repos:
            repo_name = r["name"]
            url = f"https://api.github.com/repos/{repo_name}/stats/contributors"
            found_in_rest = False
            
            try:
                for _ in range(3):
                    resp = requests.get(url, headers=HEADERS)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            for item in data:
                                author = item.get("author")
                                if author and author.get("login", "").lower() == LOGIN.lower():
                                    r["additions"] = sum(w["a"] for w in item["weeks"])
                                    r["deletions"] = sum(w["d"] for w in item["weeks"])
                                    found_in_rest = True
                                    break
                        break
                    elif resp.status_code == 202:
                        time.sleep(2)
                    else:
                        break
            except Exception as e:
                print(f"Warning: Fetching REST API stats failed for {repo_name}: {e}", file=sys.stderr)
            
            if not found_in_rest or (r["additions"] == 0 and r["deletions"] == 0):
                if repo_name in pr_lines:
                    r["additions"] = pr_lines[repo_name]["additions"]
                    r["deletions"] = pr_lines[repo_name]["deletions"]

    fetch_lines(mine)
    fetch_lines(others)

    return {"mine": mine, "others": others, "count_total": len(mine) + len(others)}

# ---------- pretty formatting ----------

def to_k_plus(n: int) -> str:
    """1_234 -> 1.2k+ ; 987 -> 987"""
    if n >= 1000:
        v = f"{n/1000:.1f}".rstrip("0").rstrip(".")
        return f"{v}k+"
    return str(n)

def pretty_repo_text(full_name: str) -> str:
    """
    'owner/repo-name_x' -> 'Repo Name X' (title-cased), but keep URL link text only.
    """
    repo = full_name.split("/")[-1]
    pretty = repo.replace("-", " ").replace("_", " ")
    pretty = " ".join(w.capitalize() for w in pretty.split())
    return pretty

def repo_chip(name, url, stars, forks):
    """Repository link with inline star/fork chips, and 🔥 for 1k+ stars."""
    star_text = to_k_plus(stars)
    fire = " 🔥" if stars >= 1000 else ""
    pretty = pretty_repo_text(name)
    return f'<a href="{url}">{pretty}</a> <sub>· ⭐ {star_text}{fire} · 🍴 {forks}</sub>'

def md_table_contrib(rows):
    if not rows:
        return "_(empty)_"
    
    # 严格对齐Markdown表格格式，去掉了可能导致解析问题的括号
    header = (
        "| Repository | 📝 Commits | 🔀 PRs | 🐛 Issues | 💻 Code | ∑ Total |\n"
        "| :--- | ---: | ---: | ---: | ---: | ---: |"
    )
    lines = []
    for r in rows:
        adds = r.get("additions", 0)
        dels = r.get("deletions", 0)
        
        if adds == 0 and dels == 0:
            code_str = "-"
        else:
            code_str = f"+{adds} / -{dels}"
            
        line = (f'| {repo_chip(r["name"], r["url"], r["stars"], r["forks"])} | '
                f'`{r["commit"]}` | `{r["pr"]}` | `{r["issue"]}` | `{code_str}` | **`{r["total"]}`** |')
        lines.append(line)
        
    return "\n".join([header] + lines)

def md_list_own_stars(rows):
    if not rows:
        return "_(empty)_"
    items = [f'- {repo_chip(r["name"], r["url"], r["stars"], r["forks"])}' for r in rows]
    return "\n".join(items)

# ---------- render blocks ----------

def render_markdown(own_repos, total_stars, contrib):
    stars_block = f"""
<details>
  <summary><b>⭐ Total Stars Earned:</b> <code>{to_k_plus(total_stars)}</code></summary>

  <br/>
{md_list_own_stars(own_repos)}
</details>
""".strip()

    contrib_block = f"""
<details>
  <summary><b>🤝 Contributed to:</b> <code>{contrib["count_total"]}</code></summary>

  <br/>

  <div><b>👥 Other Repos</b></div>

{md_table_contrib(contrib["others"])}

  <br/><br/>

  <div><b>📦 My Repos</b></div>

{md_table_contrib(contrib["mine"])}

</details>
""".strip()

    return f"""
<div align="left">

{stars_block}

<br/>

{contrib_block}

</div>
""".strip()

# ---------- main ----------

def main():
    own_repos, total_stars = get_own_public_repos_and_total_stars()
    contrib = aggregate_contributions_all_time()
    block = render_markdown(own_repos, total_stars, contrib)

    with open("README.md", "r", encoding="utf-8") as f:
        content = f.read()

    # 替换 README 内容
    pattern = re.compile(r"()(.*?)()", re.S)
    new = re.sub(pattern, r"\1\n" + block + r"\n\3", content)

    if new != content:
        with open("README.md", "w", encoding="utf-8") as f:
            f.write(new)
        print("README updated.")
    else:
        print("No changes.")

if __name__ == "__main__":
    if not LOGIN or not TOKEN:
        print("Missing GH_LOGIN or GITHUB_TOKEN.", file=sys.stderr)
        sys.exit(1)
    main()
