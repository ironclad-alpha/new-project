#!/usr/bin/env python3
"""
CLI for local PR review testing
"""
import os
import sys
import json
import argparse
from pathlib import Path

from pr_reviewer.core import PRReviewer, PRContext
from pr_reviewer.github_integration import GitHubPRClient, GitHubConfig, AutoReviewer


def load_env():
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()


def review_local(args):
    load_env()
    
    groq_key = os.getenv("GROQ_API_KEY") or args.groq_key
    if not groq_key:
        print("GROQ_API_KEY required")
        return 1
    
    reviewer = PRReviewer(groq_key)
    
    if args.pr_file:
        with open(args.pr_file) as f:
            pr_data = json.load(f)
        pr_context = PRContext(
            pr_number=pr_data.get("number", 1),
            repo=pr_data.get("repo", "local"),
            base_branch=pr_data.get("base", "main"),
            head_branch=pr_data.get("head", "feature"),
            title=pr_data.get("title", ""),
            description=pr_data.get("description", ""),
            files_changed=pr_data.get("files", []),
            diff=pr_data.get("diff", "")
        )
        repo_files = {}
        for f in pr_context.files_changed:
            path = Path(f)
            if path.exists():
                repo_files[f] = path.read_text()
    else:
        print("Need --pr-file with PR data")
        return 1
    
    comments = reviewer.review_pr(pr_context, repo_files)
    
    for c in comments:
        print(f"{c.file_path}:{c.line_number} [{c.comment_type.value}] {c.text}")
    
    if args.output:
        Path(args.output).write_text(json.dumps([{
            "file": c.file_path,
            "line": c.line_number,
            "type": c.comment_type.value,
            "text": c.text
        } for c in comments], indent=2))
    
    print(f"\nTotal: {len(comments)} comments")
    return 0


def review_github(args):
    load_env()
    
    github_token = os.getenv("GITHUB_TOKEN") or args.github_token
    groq_key = os.getenv("GROQ_API_KEY") or args.groq_key
    repo = args.repo or os.getenv("GITHUB_REPOSITORY")
    
    if not all([github_token, repo, args.pr_number]):
        print("Need --github-token, --repo, --pr-number")
        return 1
    
    reviewer = AutoReviewer(github_token, repo, groq_key)
    comments = reviewer.review_pr(args.pr_number)
    
    for c in comments:
        print(f"{c.file_path}:{c.line_number} [{c.comment_type.value}] {c.text}")
    
    if args.post:
        reviewer.post_review(args.pr_number, comments)
        print("Posted to GitHub")
    
    return 0


def main():
    parser = argparse.ArgumentParser(description="Auto PR Reviewer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    local = subparsers.add_parser("local", help="Review local PR data")
    local.add_argument("--pr-file", required=True, help="JSON file with PR data")
    local.add_argument("--groq-key", help="Groq API key")
    local.add_argument("--output", help="Output JSON file")
    
    github = subparsers.add_parser("github", help="Review GitHub PR")
    github.add_argument("--pr-number", type=int, required=True)
    github.add_argument("--repo", help="owner/repo")
    github.add_argument("--github-token", help="GitHub token")
    github.add_argument("--groq-key", help="Groq API key")
    github.add_argument("--post", action="store_true", help="Post comments to GitHub")
    
    args = parser.parse_args()
    
    if args.command == "local":
        sys.exit(review_local(args))
    elif args.command == "github":
        sys.exit(review_github(args))


if __name__ == "__main__":
    main()