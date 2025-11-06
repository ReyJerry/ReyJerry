#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, datetime
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

# 1) own public repos: list + total stars (sorted by stars -> forks)
def get_own_public_repos_and_total_stars():
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

# 2) contribution years
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

# 3) per-year contributions aggregated by repo
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

    repo_map = {}  # name -> {url, stars, forks, commit, pr, issue}
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

# 4) all-time merge + split into mine / others; sort by stars -> forks
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
            "commit": v["commit"], "pr": v["pr"], "issue": v["issue"], "total": total
        }
        owner = name.split("/")[0].lower() if "/" in name else ""
        (mine if owner == LOGIN.lower() else others).append(row)

    keyf = lambda r: (-r["stars"], -r["forks"])
    mine.sort(key=keyf)
    others.sort(key=keyf)

    return {"mine": mine, "others": others, "count_total": len(mine) + len(others)}

# 5) pretty rendering
def render_markdown(own_repos, total_stars, contrib):
    def repo_badge(r):
        # repo link + inline stars/forks badges
        return f'<a href="{r["url"]}">{r["name"]}</a> <sub>· ⭐ {r["stars"]} · 🍴 {r["forks"]}</sub>'

    def tbl(rows):
        if not rows:
            return "_(empty)_"
        header = (
            "| Repository | 📝 Commits | 🔀 PRs | 🐛 Issues | ∑ Total |\n"
            "|---|---:|---:|---:|---:|"
        )
        lines = [
            f'| {repo_badge(r)} | {r["commit"]} | {r["pr"]} | {r["issue"]} | **{r["total"]}** |'
            for r in rows
        ]
        return "\n".join([header] + lines)

    def list_stars(rows):
        if not rows:
            return "_(empty)_"
        # bullet list for a cleaner look
        return "\n".join([f'- {repo_badge(r)}' for r in rows])

    stars_block = f"""
<details>
  <summary><b>⭐ Total Stars Earned:</b> <code>{total_stars}</code></summary>

  <br/>
{list_stars(own_repos)}

</details>
""".strip()

    contrib_block = f"""
<details>
  <summary><b>🤝 Contributed to:</b> <code>{contrib["count_total"]}</code></summary>

  <br/>
  <div><b>👥 Other Repos</b></div>

{tbl(contrib["others"])}

  <br/><br/>
  <div><b>📦 My Repos</b></div>

{tbl(contrib["mine"])}

</details>
""".strip()

    return f"""
<div align="left">

{stars_block}

{contrib_block}

</div>
""".strip()

def main():
    own_repos, total_stars = get_own_public_repos_and_total_stars()
    contrib = aggregate_contributions_all_time()
    block = render_markdown(own_repos, total_stars, contrib)

    with open("README.md", "r", encoding="utf-8") as f:
        content = f.read()

    pattern = re.compile(r"(<!--STATS:START-->)(.*?)(<!--STATS:END-->)", re.S)
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
