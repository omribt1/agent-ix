"""Upload the checked-out default branch to PR-noia for a full-repo scan.

Run by the main-branch workflow on every push to the default branch. It reads
every tracked file from the checkout, submits them as a ``scan_scope: "repo"``
scan, polls for the finished review, writes a findings summary to the Actions
job summary, and saves the review to ``PRNOIA_REVIEW_OUT`` for later steps
(``prnoia_open_issues.py`` opens GitHub issues from it in private repos).

Uploading the tree (rather than having PR-noia clone the repo) is what makes
private repositories work: the backend holds **no GitHub credential**, so the
workflow's own checkout is the only thing that can read the code. Nothing is
posted back to GitHub by this script and the job never fails on findings —
results live in the job summary and the PR-noia dashboard.

PR-noia allows a limited number of full-repo scans per repository per UTC day
(3 by default). A push over the limit is not scanned: the job logs a warning,
notes it in the job summary and still succeeds.

Environment:
  PRNOIA_SERVER_URL   Base URL of PR-noia (required).
  PRNOIA_API_KEY      API key issued in the dashboard; sent as a Bearer token (required).
  GITHUB_REPOSITORY   owner/repo slug (required).
  GITHUB_REF_NAME     Branch being scanned (set by Actions).
  GITHUB_SHA          Commit being scanned (set by Actions).
  GITHUB_STEP_SUMMARY Job summary file (set by Actions).
  PRNOIA_MAX_FILE_BYTES    Optional per-file cap; larger files are skipped (default 1 MiB).
  PRNOIA_MAX_TOTAL_BYTES   Optional total upload cap (default 16 MiB).
  PRNOIA_TIMEOUT           Optional HTTP timeout per request in seconds (default 120).
  PRNOIA_POLL_TIMEOUT      Optional max seconds to wait for the scan (default 4200).
  PRNOIA_POLL_INTERVAL     Optional seconds between poll attempts (default 20).
  PRNOIA_REVIEW_OUT        Optional path the finished review is saved to
                           (default ./prnoia_scan/review.json).
"""

import json
import os
import subprocess
import sys

import requests

from prnoia_client import fail, poll_for_review, require_env

DEFAULT_MAX_FILE_BYTES = 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 16 * 1024 * 1024
DEFAULT_POLL_TIMEOUT = 4200  # the scanner Job is capped at 60 min, plus queueing
DEFAULT_POLL_INTERVAL = 20
DEFAULT_REVIEW_OUT = "./prnoia_scan/review.json"
MAX_SUMMARY_FINDINGS = 50

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
SEVERITY_BADGE = {
    "critical": "🔴 Critical", "high": "🟠 High", "medium": "🟡 Medium",
    "low": "🔵 Low", "info": "⚪ Info",
}


def tracked_files():
    """Paths of every file git tracks in the checkout (untracked/ignored excluded)."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        fail(f"could not list tracked files with git: {exc}")
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def collect_tree(max_file_bytes, max_total_bytes):
    """Read tracked text files, skipping symlinks, binaries and oversized files.

    Returns ``(files, skipped)``: ``files`` in the scan payload's changed-file shape,
    ``skipped`` a count per reason so the summary shows what the scan did not see.
    """
    files, total = [], 0
    skipped = {"binary": 0, "too large": 0, "over total cap": 0, "unreadable": 0}
    for path in tracked_files():
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            if size > max_file_bytes:
                skipped["too large"] += 1
                continue
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            skipped["unreadable"] += 1
            continue
        if b"\0" in raw[:8192]:
            skipped["binary"] += 1
            continue
        if total + len(raw) > max_total_bytes:
            skipped["over total cap"] += 1
            continue
        total += len(raw)
        files.append({
            "path": path,
            "status": "modified",
            "truncated": False,
            "content": raw.decode("utf-8", errors="replace"),
        })
    return files, total, {k: v for k, v in skipped.items() if v}


def md_cell(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def write_summary(server_url, repo, ref, sha, review, uploaded, skipped):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    stats = review.get("stats") or {}
    findings = review.get("findings") or []
    findings = sorted(
        findings,
        key=lambda f: SEVERITY_ORDER.index((f.get("severity") or "info").lower())
        if (f.get("severity") or "info").lower() in SEVERITY_ORDER else len(SEVERITY_ORDER),
    )
    lines = [
        "## 🔎 PR-noia main branch scan",
        "",
        f"`{repo}` @ `{ref}` (`{(sha or '')[:12]}`) — {md_cell(review.get('summary')) or 'scan complete'}",
        "",
    ]
    if review.get("ai_review") == "failed":
        lines += [
            "> [!WARNING]",
            "> The AI review failed for this scan, so these are raw scanner results: they may",
            "> miss issues or rate them differently. The next push is scanned normally.",
            "",
        ]
    lines += [
        "| Severity | Findings |",
        "| --- | --- |",
    ]
    for sev in SEVERITY_ORDER:
        lines.append(f"| {SEVERITY_BADGE[sev]} | {stats.get(sev, 0)} |")
    if findings:
        lines += ["", "| Severity | Finding | Location |", "| --- | --- | --- |"]
        for f in findings[:MAX_SUMMARY_FINDINGS]:
            sev = (f.get("severity") or "info").lower()
            loc = f.get("file") or "?"
            if f.get("line"):
                loc += f":{f['line']}"
            lines.append(f"| {SEVERITY_BADGE.get(sev, sev)} | {md_cell(f.get('title') or 'Untitled finding')} | `{md_cell(loc)}` |")
        if len(findings) > MAX_SUMMARY_FINDINGS:
            lines.append(f"\n_…and {len(findings) - MAX_SUMMARY_FINDINGS} more in the dashboard._")
    note = f"{uploaded} file(s) scanned"
    if skipped:
        note += "; skipped " + ", ".join(f"{n} {why}" for why, n in skipped.items())
    lines += ["", f"_{note}._", "", f"[Open in the PR-noia dashboard]({server_url}/repos?repo={repo})", ""]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_limit_summary(repo, ref, sha, body):
    """Job-summary note for a push that was not scanned because of the daily limit."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## 🔎 PR-noia main branch scan — skipped",
        "",
        f"`{repo}` @ `{ref}` (`{(sha or '')[:12]}`) was **not scanned**: "
        f"{md_cell(body.get('error')) or 'the daily full-repo scan limit was reached.'}",
        "",
        "_The next push after the reset will be scanned; the last scanned results are in the dashboard._",
        "",
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    server_url = require_env("PRNOIA_SERVER_URL").rstrip("/")
    api_key = require_env("PRNOIA_API_KEY")
    repo = require_env("GITHUB_REPOSITORY")
    ref = os.environ.get("GITHUB_REF_NAME") or None
    sha = os.environ.get("GITHUB_SHA") or None

    max_file_bytes = int(os.environ.get("PRNOIA_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES))
    max_total_bytes = int(os.environ.get("PRNOIA_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES))
    timeout = float(os.environ.get("PRNOIA_TIMEOUT", "120"))
    poll_timeout = float(os.environ.get("PRNOIA_POLL_TIMEOUT", DEFAULT_POLL_TIMEOUT))
    poll_interval = float(os.environ.get("PRNOIA_POLL_INTERVAL", DEFAULT_POLL_INTERVAL))

    files, total, skipped = collect_tree(max_file_bytes, max_total_bytes)
    if not files:
        fail("no readable tracked files to scan.")
    if skipped:
        print("Skipped: " + ", ".join(f"{n} {why}" for why, n in skipped.items()), file=sys.stderr)

    # No PR number: the backend records this as a main-branch scan. The uploaded
    # tree travels in changed_files, so the scanner never clones the repo.
    payload = {
        "repository": {"full_name": repo, "default_branch": ref},
        "pull_request": {"head_ref": ref, "head_sha": sha},
        "scan_scope": "repo",
        "ref": ref,
        "changed_files": files,
    }

    url = f"{server_url}/api/v1/scans"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    print(f"Submitting full-repo scan of {repo}@{ref} to {url} "
          f"({len(files)} files, {total} bytes)...", file=sys.stderr)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        fail(f"failed to reach PR-noia backend: {exc}")

    if resp.status_code in (401, 403):
        fail(f"authentication failed ({resp.status_code}); check PRNOIA_API_KEY. Body: {resp.text[:500]}")
    if resp.status_code == 413:
        fail(f"payload too large (413); lower PRNOIA_MAX_TOTAL_BYTES. Body: {resp.text[:500]}")
    if resp.status_code == 429:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if body.get("code") == "daily_limit":
            # Expected, not an error: this push simply isn't scanned.
            print(f"::warning title=PR-noia daily limit::{body.get('error') or 'daily full-repo scan limit reached'}")
            write_limit_summary(repo, ref, sha, body)
            return
        fail(f"rate limited by PR-noia backend (429). Body: {resp.text[:500]}")
    if resp.status_code not in (200, 202):
        fail(f"PR-noia backend returned {resp.status_code}. Body: {resp.text[:1000]}")

    try:
        scan_id = resp.json().get("scan_id")
    except ValueError:
        scan_id = None
    if not scan_id:
        fail("backend accepted the scan but returned no scan_id to poll.")
    print(f"Scan accepted: id={scan_id}. Waiting for the scan to complete...", file=sys.stderr)

    review = poll_for_review(server_url, api_key, scan_id, timeout, poll_timeout, poll_interval)
    write_summary(server_url, repo, ref, sha, review, len(files), skipped)
    review_out = os.environ.get("PRNOIA_REVIEW_OUT") or DEFAULT_REVIEW_OUT
    os.makedirs(os.path.dirname(review_out) or ".", exist_ok=True)
    with open(review_out, "w", encoding="utf-8") as fh:
        json.dump(review, fh)
    stats = review.get("stats") or {}
    if review.get("ai_review") == "failed":
        print("::warning title=PR-noia AI review failed::raw scanner results only for this scan")
    print(f"Scan complete: {stats.get('total', 0)} finding(s). {review.get('summary') or ''}", file=sys.stderr)


if __name__ == "__main__":
    main()
