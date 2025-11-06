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

def get_total_stars():
    total = 0
    cursor = None
    while True:
        q = """
        query($login:String!, $cursor:String) {
          user(login:$login){
            repositories(ownerAffiliations: OWNER, isFork:false, privacy:PUBLIC, first:100, after:$cursor){
              pageInfo { hasNextPage endCursor }
              nodes { stargazerCount }
            }
          }
        }"""
        d = gql(q, {"login": LOGIN, "cursor": cursor})
        repos = d["user"]["repositories"]
        total += sum(n["stargazerCount"] for n in repos["nodes"])
        if repos["pageInfo"]["hasNextPage"]:
            cursor = repos["pageInfo"]["endCursor"]
        else:
            break
    return total

def get_years():
    q = """
    query($login:String!){
      user(login:$login){
        contributionsCollection {
          contributionYears
        }
      }
    }"""
    d = gql(q, {"login": LOGIN})
    years = d["user"]["contributionsCollection"]["contributionYears"]
    years = sorted(set(years))
    y = datetime.datetime.utcnow().year
    if y not in years:
        years.append(y)
    return years

def get_pr_issue_totals_all_time():
    q = """
    query($login:String!){
      user(login:$login){
        pullRequests(states:[OPEN,MERGED,CLOSED]) { totalCount }
        issues(states:[OPEN,CLOSED]) { totalCount }
      }
    }"""
    d = gql(q, {"login": LOGIN})
    return d["user"]["pullRequests"]["totalCount"], d["user"]["issues"]["totalCount"]

def collect_by_year(year):
    start = datetime.datetime(year, 1, 1)
    end = datetime.datetime(year + 1, 1, 1) - relativedelta(seconds=1)
    q = """
    query($login:String!, $from:DateTime!, $to:DateTime!){
      user(login:$login){
        contributionsCollection(from:$from, to:$to){
          totalCommitContributions
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

    data = {
        "commits": cc["totalCommitContributions"],
        "by_repo": {}
    }

    def add(repo, key, n):
        k = repo["nameWithOwner"]
        if k not in data["by_repo"]:
            data["by_repo"][k] = {
                "url": repo["url"],
                "stars": repo["stargazerCount"],
                "forks": repo["forkCount"],
                "commit": 0, "pr": 0, "issue": 0
            }
        data["by_repo"][k][key] += n

    for r in cc["commitContributionsByRepository"]:
        add(r["repository"], "commit", r["contributions"]["totalCount"])
    for r in cc["pullRequestContributionsByRepository"]:
        add(r["repository"], "pr", r["contributions"]["totalCount"])
    for r in cc["issueContributionsByRepository"]:
        add(r["repository"], "issue", r["contributions"]["totalCount"])

    return data

def aggregate_all_time():
    years = get_years()
    total_commits = 0
    repo_map = {}

    for y in years:
        ydata = collect_by_year(y)
        total_commits += ydata["commits"]
        for name, rec in ydata["by_repo"].items():
            if name not in repo_map:
                repo_map[name] = rec.copy()
            else:
                for k in ("commit", "pr", "issue"):
                    repo_map[name][k] += rec[k]
            repo_map[name]["stars"] = rec["stars"]
            repo_map[name]["forks"] = rec["forks"]
            repo_map[name]["url"] = rec["url"]

    total_prs, total_issues = get_pr_issue_totals_all_time()
    contributed_total = sum(1 for v in repo_map.values() if (v["commit"] + v["pr"] + v["issue"]) > 0)

    repo_list = sorted(
        [
            {
                "name": name,
                "url": v["url"],
                "stars": v["stars"],
                "forks": v["forks"],
                "commit": v["commit"],
                "pr": v["pr"],
                "issue": v["issue"],
                "total": v["commit"] + v["pr"] + v["issue"],
            }
            for name, v in repo_map.items()
        ],
        key=lambda x: (-x["total"], -x["stars"])
    )

    return {
        "total_commits": total_commits,
        "total_prs": total_prs,
        "total_issues": total_issues,
        "contributed_total": contributed_total,
        "repo_list": repo_list
    }

def render_md(total_stars, agg):
    def repo_line(r):
        return (
            f'- <a href="{r["url"]}">{r["name"]}</a> — '
            f'贡献 <b>{r["total"]}</b> 次（Commit {r["commit"]} · PR {r["pr"]} · Issue {r["issue"]}）'
            f' · ⭐ <b>{r["stars"]}</b> · Fork <b>{r["forks"]}</b>'
        )
    contributed_md = "\n".join(repo_line(r) for r in agg["repo_list"][:80]) or "_(no public records)_"

    return f"""
<div align="left">

<details>
  <summary><b>⭐ Total Stars Earned:</b> <code>{total_stars}</code></summary>
  - 你名下公开非 fork 仓库的 Star 合计：**{total_stars}**
</details>

<details>
  <summary><b>🧮 Total Commits:</b> <code>{agg["total_commits"]}</code></summary>
  - 历史总提交：**{agg["total_commits"]}**（按年份累计）
</details>

<details>
  <summary><b>🔀 Total PRs:</b> <code>{agg["total_prs"]}</code></summary>
  - 历史总 PR：**{agg["total_prs"]}**
</details>

<details>
  <summary><b>🐛 Total Issues:</b> <code>{agg["total_issues"]}</code></summary>
  - 历史总 Issue：**{agg["total_issues"]}**
</details>

<details>
  <summary><b>🤝 Contributed to:</b> <code>{agg["contributed_total"]}</code></summary>

{contributed_md}
</details>

</div>
""".strip()

def main():
    total_stars = get_total_stars()
    agg = aggregate_all_time()
    block = render_md(total_stars, agg)

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
