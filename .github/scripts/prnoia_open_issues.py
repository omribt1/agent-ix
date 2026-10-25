#!/usr/bin/env python3
"""Open GitHub issues for the main-branch scan's critical/high findings.

Runs in the main-branch workflow *after* prnoia_repo_scan.py has polled PR-noia
and saved the finished review. Like post_comment.py for PRs, it talks to GitHub
with the run's own GITHUB_TOKEN (needs ``issues: write``) — the PR-noia backend
never holds a GitHub credential.

**Private repositories only.** An issue in a public repo would disclose an
unfixed vulnerability to everyone, so the script checks the repo's visibility
and does nothing unless it is private.

One issue per finding, labelled ``prnoia-security``. Every scan re-runs this, so
issues are keyed by a fingerprint stored in a hidden marker in the issue body:
file + weakness class (the finding's category, else CWE, else title) — no line
number, so editing code above a finding doesn't open a duplicate. Findings
sharing a key become one issue listing every location. The AI doesn't label a
finding identically every scan (the CWE especially drifts), so a finding also
matches an issue keyed the pre-category way (file + CWE) or one that reports the
same file within 3 lines. For each finding:

- no issue yet            → open one (at most ``PRNOIA_ISSUE_MAX_NEW`` per run)
- open issue              → refresh its body (locations, last-seen commit)
- closed as completed     → reopen it: the finding is back
- closed as not planned   → leave it alone (marked a false positive / accepted)

Open ``prnoia-security`` issues whose finding is no longer reported are closed
as completed (disable with ``PRNOIA_ISSUE_AUTO_CLOSE=false``) -- but only after
``PRNOIA_ISSUE_CLOSE_AFTER`` consecutive scans miss it (default 2), because the
AI reviewer doesn't report every finding on every scan. The miss count lives in
a hidden marker in the issue body and resets when the finding is seen again.
A finding that is still reported at a lower severity than the threshold keeps
its issue open (it isn't fixed); it just doesn't open new issues.

Only a trustworthy scan may change issues. The review's ``ai_review`` field says
whether the AI reviewer's findings are what came back:

- ``failed``  → the reviewer errored and the backend fell back to raw scanner
  candidates (noisy, mis-rated): issues are left untouched this run.
- ``ok`` / ``off`` (a scanners-only deployment) → full sync, including auto-close.
- missing (older backend) → open/update only; nothing is auto-closed.

The script never fails the workflow: errors are logged as warnings.

Usage:
  prnoia_open_issues.py [--review ./prnoia_scan/review.json] [--dry-run]

Environment:
  GH_TOKEN                   Token `gh` authenticates with (the run's GITHUB_TOKEN).
  GITHUB_REPOSITORY          owner/repo (set by Actions).
  GITHUB_SHA, GITHUB_REF_NAME, GITHUB_SERVER_URL, GITHUB_RUN_ID  (set by Actions).
  GITHUB_STEP_SUMMARY        Job summary file (set by Actions).
  PRNOIA_SERVER_URL          PR-noia base URL, for the dashboard link.
  PRNOIA_ISSUE_MIN_SEVERITY  Lowest severity that gets an issue (default high).
  PRNOIA_ISSUE_MAX_NEW       Max issues opened per run (default 10).
  PRNOIA_ISSUE_AUTO_CLOSE    Close issues for fixed findings (default true).
  PRNOIA_ISSUE_CLOSE_AFTER   Consecutive scans a finding must be missing before
                             its issue is closed (default 2).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

LABEL = "prnoia-security"
LABEL_COLOR = "d73a4a"
MARKER = "<!-- pr-noia:issue fp={} -->"
MARKER_RE = re.compile(r"<!-- pr-noia:issue fp=([0-9a-f]{16}) -->")
MISSED = "<!-- pr-noia:missed n={} -->"
MISSED_RE = re.compile(r"\n*<!-- pr-noia:missed n=(\d+) -->")
LOC_RE = re.compile(r"^- \[`(.+?):(\d+)(?:-\d+)?`\]", re.M)
NEAR_LINES = 3

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
SEVERITY_BADGE = {
    "critical": "🔴 Critical", "high": "🟠 High", "medium": "🟡 Medium",
    "low": "🔵 Low", "info": "⚪ Info",
}
DEFAULT_REVIEW = "./prnoia_scan/review.json"
DEFAULT_MAX_NEW = 10
DEFAULT_CLOSE_AFTER = 2


def warn(message):
    print(f"::warning title=PR-noia issues::{message}")


def sev_rank(f):
    sev = (f.get("severity") or "info").lower()
    return SEVERITY_ORDER.index(sev) if sev in SEVERITY_ORDER else len(SEVERITY_ORDER)


def env_bool(name, default):
    val = (os.environ.get(name) or "").strip().lower()
    return default if not val else val not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------- #
# gh helpers
# --------------------------------------------------------------------------- #

def gh(args, params=None, check=True):
    """Run `gh api ...`; JSON params (if any) are piped on stdin via --input -."""
    cmd = ["gh", *args]
    stdin = None
    if params is not None:
        cmd += ["--input", "-"]
        stdin = json.dumps(params)
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=check)


def repo_is_private(repo):
    res = gh(["api", f"repos/{repo}", "--jq", ".private"])
    return res.stdout.strip() == "true"


def ensure_label(repo):
    if gh(["api", f"repos/{repo}/labels/{LABEL}"], check=False).returncode == 0:
        return
    gh(["api", f"repos/{repo}/labels", "-X", "POST"], check=False, params={
        "name": LABEL, "color": LABEL_COLOR,
        "description": "Security finding from the PR-noia main-branch scan",
    })


def existing_issues(repo):
    """PR-noia issues with their marker ``fp`` and reported ``locs``, open ones first."""
    res = gh([
        "api", f"repos/{repo}/issues?labels={LABEL}&state=all&per_page=100", "--paginate",
        "--jq", ".[] | select(.pull_request == null) | {number, state, state_reason, title, body, html_url}",
    ])
    found = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        issue = json.loads(line)
        body = issue.get("body") or ""
        m = MARKER_RE.search(body)
        if not m:
            continue
        issue["fp"] = m.group(1)
        issue["locs"] = [(path, int(n)) for path, n in LOC_RE.findall(body)]
        found.append(issue)
    # A match prefers the live issue over a closed duplicate.
    found.sort(key=lambda i: i["state"] != "open")
    return found


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _fp(f, *fields):
    weakness = next((f[k] for k in fields if f.get(k)), None) or re.sub(r"\W+", " ", f.get("title") or "")
    key = f"{(f.get('file') or '').strip()}|{weakness.strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def fingerprint(f):
    return _fp(f, "category", "cwe")


def legacy_fingerprint(f):
    # The key issues were opened with before category took precedence over CWE.
    return _fp(f, "cwe", "category")


def is_near(issue, group):
    return any(
        path == f.get("file") and abs(n - int(f["line"])) <= NEAR_LINES
        for f in group if f.get("file") and f.get("line")
        for path, n in issue["locs"]
    )


def match_issues(groups, issues):
    """``{fp: issue}``: each existing issue claimed by at most one finding group.

    Pass 1 matches by fingerprint (current or legacy key) for every group before
    pass 2 falls back to location, so a loose match can't steal an exact one.
    """
    matched, claimed = {}, set()
    for exact in (True, False):
        for fp, group in groups.items():
            if fp in matched:
                continue
            keys = {fp, *(legacy_fingerprint(f) for f in group)}
            for issue in issues:
                if issue["number"] in claimed:
                    continue
                if (issue["fp"] in keys) if exact else is_near(issue, group):
                    matched[fp] = issue
                    claimed.add(issue["number"])
                    break
    return matched


def md(text):
    # Findings quote scanned code / model output: keep them from @-mentioning people.
    return str(text or "").replace("@", "@​").strip()


def fence(text):
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}\n{text.rstrip()}\n{ticks}"


def issue_title(group):
    top = group[0]
    sev = (top.get("severity") or "info").lower().capitalize()
    title = md(top.get("title") or "Security finding")
    where = f" in {top['file']}" if top.get("file") else ""
    return f"[PR-noia] {sev}: {title}{where}"[:250]


def issue_body(fp, group, ctx):
    top = group[0]
    sev = (top.get("severity") or "info").lower()
    meta = [f"**Severity:** {SEVERITY_BADGE.get(sev, sev)}"]
    for label, key in (("CWE", "cwe"), ("OWASP", "owasp"), ("Confidence", "confidence"), ("Source", "source")):
        if top.get(key):
            meta.append(f"**{label}:** {md(top[key])}")
    lines = [MARKER.format(fp), " · ".join(meta), "", "### Location"]
    for f in group:
        path = f.get("file")
        if not path:
            lines.append("- _(no file reported)_")
            continue
        loc, anchor = path, ""
        if f.get("line"):
            loc += f":{f['line']}"
            anchor = f"#L{f['line']}"
            if f.get("end_line") and f["end_line"] > f["line"]:
                loc += f"-{f['end_line']}"
                anchor += f"-L{f['end_line']}"
        lines.append(f"- [`{loc}`]({ctx['server']}/{ctx['repo']}/blob/{ctx['sha']}/{path}{anchor})")
    if top.get("description"):
        lines += ["", "### Description", md(top["description"])]
    if top.get("recommendation"):
        lines += ["", "### Remediation", md(top["recommendation"])]
    if top.get("code_snippet"):
        lines += ["", "### Evidence", fence(str(top["code_snippet"]))]
    lines += [
        "", "---",
        f"_Reported by the PR-noia main-branch scan — last seen in `{ctx['sha'][:12]}` on "
        f"`{ctx['ref']}` ([workflow run]({ctx['run_url']})). This issue is updated on every scan "
        "and closed automatically once the finding is no longer detected. Close it as "
        "**not planned** to mark a false positive; PR-noia won't reopen it._",
    ]
    if ctx.get("dashboard"):
        lines.append(f"\n[Open in the PR-noia dashboard]({ctx['dashboard']})")
    return "\n".join(lines)


def group_findings(findings, min_rank):
    """``(groups, below)``: finding groups at/above the threshold, and the rest.

    Grouping covers every finding so a weakness keeps one key whatever severity
    each location got; a group belongs to ``groups`` if its worst finding meets
    the threshold. ``below`` groups never open issues but still count as
    detected, so a downgraded finding doesn't look fixed.
    """
    groups, below = {}, {}
    for f in sorted(findings, key=sev_rank):
        groups.setdefault(fingerprint(f), []).append(f)
    for fp in list(groups):
        if sev_rank(groups[fp][0]) > min_rank:
            below[fp] = groups.pop(fp)
    return groups, below


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and lines:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(["", "### GitHub issues", "", *lines, ""]))


def sync(review, repo, ctx, min_rank, max_new, auto_close, dry_run, close_after=DEFAULT_CLOSE_AFTER):
    groups, below = group_findings(review.get("findings") or [], min_rank)
    issues = [] if dry_run else existing_issues(repo)
    # Threshold groups first, so they win an issue over a below-threshold group.
    existing = match_issues({**groups, **below}, issues)
    if groups and not dry_run:
        ensure_label(repo)

    done = {"opened": [], "updated": [], "reopened": [], "closed": [], "not seen this scan (kept open)": []}
    skipped_new = 0
    for fp, group in groups.items():
        title, body = issue_title(group), issue_body(fp, group, ctx)
        issue = existing.get(fp)
        if dry_run:
            print(f"--- {'update' if issue else 'open'}: {title}\n{body}\n")
            continue
        if issue is None:
            if len(done["opened"]) >= max_new:
                skipped_new += 1
                continue
            res = gh(["api", f"repos/{repo}/issues", "-X", "POST"],
                     params={"title": title, "body": body, "labels": [LABEL]})
            done["opened"].append(json.loads(res.stdout)["html_url"])
        elif issue["state"] == "open":
            if issue.get("body") != body or issue.get("title") != title:
                gh(["api", f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH"],
                   params={"title": title, "body": body})
                done["updated"].append(issue["html_url"])
        elif issue.get("state_reason") == "not_planned":
            continue
        else:
            gh(["api", f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH"],
               params={"state": "open", "title": title, "body": body})
            gh(["api", f"repos/{repo}/issues/{issue['number']}/comments", "-X", "POST"],
               params={"body": f"Detected again by the PR-noia scan of `{ctx['sha'][:12]}` — reopening."})
            done["reopened"].append(issue["html_url"])

    # Still detected below the threshold: leave the issue as it is, but clear a
    # pending miss count since the finding was seen.
    for fp in below:
        issue = existing.get(fp)
        if not dry_run and issue and issue["state"] == "open" and MISSED_RE.search(issue.get("body") or ""):
            gh(["api", f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH"],
               params={"body": MISSED_RE.sub("", issue["body"])})

    if auto_close and not dry_run:
        still_found = {i["number"] for i in existing.values()}
        for issue in issues:
            if issue["state"] != "open" or issue["number"] in still_found:
                continue
            body = issue.get("body") or ""
            m = MISSED_RE.search(body)
            missed = (int(m.group(1)) if m else 0) + 1
            if missed < close_after:
                # Not closed yet: the AI may simply not have reported it this scan.
                gh(["api", f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH"],
                   params={"body": MISSED_RE.sub("", body) + "\n\n" + MISSED.format(missed)})
                done["not seen this scan (kept open)"].append(issue["html_url"])
                continue
            gh(["api", f"repos/{repo}/issues/{issue['number']}/comments", "-X", "POST"], params={
                "body": f"Not detected by the last {missed} PR-noia scans (latest `{ctx['sha'][:12]}`) — closing. "
                        "It will be reopened if the finding comes back.",
            })
            gh(["api", f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH"],
               params={"state": "closed", "state_reason": "completed"})
            done["closed"].append(issue["html_url"])
    return done, skipped_new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", default=os.environ.get("PRNOIA_REVIEW_OUT") or DEFAULT_REVIEW)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.review):
        print("No scan result (scan skipped or not finished); no issues to sync.", file=sys.stderr)
        return 0
    with open(args.review, "r", encoding="utf-8") as fh:
        review = json.load(fh)

    repo = os.environ.get("GITHUB_REPOSITORY") or review.get("repository")
    if not repo:
        warn("could not determine the repository; skipping issues.")
        return 0
    min_sev = (os.environ.get("PRNOIA_ISSUE_MIN_SEVERITY") or "high").strip().lower()
    if min_sev not in SEVERITY_ORDER:
        warn(f"unknown PRNOIA_ISSUE_MIN_SEVERITY {min_sev!r}; using high.")
        min_sev = "high"
    max_new = int(os.environ.get("PRNOIA_ISSUE_MAX_NEW") or DEFAULT_MAX_NEW)
    close_after = max(1, int(os.environ.get("PRNOIA_ISSUE_CLOSE_AFTER") or DEFAULT_CLOSE_AFTER))
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    prnoia = (os.environ.get("PRNOIA_SERVER_URL") or "").rstrip("/")
    ctx = {
        "repo": repo,
        "server": server,
        "sha": os.environ.get("GITHUB_SHA") or review.get("head_sha") or "HEAD",
        "ref": os.environ.get("GITHUB_REF_NAME") or "default branch",
        "run_url": f"{server}/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
        "dashboard": f"{prnoia}/repos?repo={repo}" if prnoia else "",
    }

    if not args.dry_run and not repo_is_private(repo):
        print(f"{repo} is public: not opening issues (they would disclose unfixed findings).", file=sys.stderr)
        return 0

    ai_review = review.get("ai_review")
    if ai_review == "failed":
        warn("the AI review failed for this scan (raw scanner results only); issues left unchanged.")
        write_summary(["- Not synced: the AI review failed for this scan, so its results "
                       "aren't reliable enough to open or close issues. The next scan will sync them."])
        return 0
    auto_close = env_bool("PRNOIA_ISSUE_AUTO_CLOSE", True) and ai_review in ("ok", "off")

    done, skipped_new = sync(review, repo, ctx, SEVERITY_ORDER.index(min_sev), max_new,
                             auto_close, args.dry_run, close_after)
    if args.dry_run:
        return 0

    summary = []
    for what, urls in done.items():
        if urls:
            summary.append(f"- {len(urls)} {what}: " + ", ".join(urls))
    if skipped_new:
        summary.append(f"- {skipped_new} more finding(s) not opened this run "
                       f"(limit {max_new}); they will be opened on the next scans.")
    if not summary:
        summary.append(f"- No changes (nothing at {min_sev} or above needs a new issue).")
    write_summary(summary)
    print("Issues: " + "; ".join(f"{len(v)} {k}" for k, v in done.items()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        warn(f"gh failed: {(exc.stderr or exc.stdout or '').strip()[:300]}")
    except Exception as exc:  # noqa: BLE001 - issues are best-effort, never fail the run
        warn(f"could not sync issues: {exc}")
    sys.exit(0)
