import os
import json
import requests
from typing import Optional
from dataclasses import dataclass

from .core import PRReviewer, PRContext, ReviewComment, create_pr_context_from_github


@dataclass
class GitHubConfig:
    token: str
    repo: str
    api_base: str = "https://api.github.com"


class GitHubPRClient:
    def __init__(self, config: GitHubConfig):
        self.config = config
        self.headers = {
            "Authorization": f"token {config.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        }
    
    def get_pr(self, pr_number: int) -> dict:
        url = f"{self.config.api_base}/repos/{self.config.repo}/pulls/{pr_number}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()
    
    def get_pr_files(self, pr_number: int) -> list:
        url = f"{self.config.api_base}/repos/{self.config.repo}/pulls/{pr_number}/files"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()
    
    def get_pr_diff(self, pr_number: int) -> str:
        url = f"{self.config.api_base}/repos/{self.config.repo}/pulls/{pr_number}"
        headers = {**self.headers, "Accept": "application/vnd.github.v3.diff"}
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.text
    
    def get_file_content(self, file_path: str, ref: str) -> str:
        url = f"{self.config.api_base}/repos/{self.config.repo}/contents/{file_path}"
        params = {"ref": ref}
        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            import base64
            content = response.json().get("content", "")
            return base64.b64decode(content).decode("utf-8")
        return ""
    
    def post_review_comment(self, pr_number: int, comment: ReviewComment) -> dict:
        url = f"{self.config.api_base}/repos/{self.config.repo}/pulls/{pr_number}/comments"
        data = {
            "body": self._format_comment(comment),
            "path": comment.file_path,
            "line": comment.line_number,
            "side": "RIGHT"
        }
        response = requests.post(url, headers=self.headers, json=data)
        return response.json()
    
    def post_general_comment(self, pr_number: int, body: str) -> dict:
        url = f"{self.config.api_base}/repos/{self.config.repo}/issues/{pr_number}/comments"
        data = {"body": body}
        response = requests.post(url, headers=self.headers, json=data)
        return response.json()
    
    def create_review(self, pr_number: int, comments: list[ReviewComment], event: str = "COMMENT") -> dict:
        url = f"{self.config.api_base}/repos/{self.config.repo}/pulls/{pr_number}/reviews"
        review_comments = []
        for c in comments:
            review_comments.append({
                "path": c.file_path,
                "line": c.line_number,
                "body": self._format_comment(c),
                "side": "RIGHT"
            })
        
        data = {
            "body": self._generate_summary(comments),
            "event": event,
            "comments": review_comments
        }
        response = requests.post(url, headers=self.headers, json=data)
        return response.json()
    
    def _format_comment(self, comment: ReviewComment) -> str:
        prefixes = {
            "nitpick": "nit: ",
            "question": "❓ ",
            "critique": "⚠️ ",
            "suggestion": "💡 ",
            "praise": "✨ "
        }
        prefix = prefixes.get(comment.comment_type.value, "")
        return f"{prefix}{comment.text}"
    
    def _generate_summary(self, comments: list[ReviewComment]) -> str:
        if not comments:
            return "lgtm"
        
        counts = {}
        for c in comments:
            counts[c.comment_type.value] = counts.get(c.comment_type.value, 0) + 1
        
        parts = []
        if counts.get("critique"):
            parts.append(f"{counts['critique']} issue{'s' if counts['critique'] > 1 else ''}")
        if counts.get("question"):
            parts.append(f"{counts['question']} question{'s' if counts['question'] > 1 else ''}")
        if counts.get("nitpick"):
            parts.append(f"{counts['nitpick']} nit{'s' if counts['nitpick'] > 1 else ''}")
        if counts.get("suggestion"):
            parts.append(f"{counts['suggestion']} suggestion{'s' if counts['suggestion'] > 1 else ''}")
        
        summary = ", ".join(parts) if parts else "few things"
        return f"left some comments ({summary}). not blocking unless critiques are real."
    
    def get_repo_files(self, pr_context: PRContext) -> dict:
        files = {}
        for file_path in pr_context.files_changed:
            content = self.get_file_content(file_path, pr_context.head_branch)
            if content:
                files[file_path] = content
        return files


class AutoReviewer:
    def __init__(self, github_token: str, repo: str, groq_api_key: Optional[str] = None):
        self.github = GitHubPRClient(GitHubConfig(github_token, repo))
        self.reviewer = PRReviewer(groq_api_key)
    
    def review_pr(self, pr_number: int) -> list[ReviewComment]:
        pr_data = self.github.get_pr(pr_number)
        pr_data["files"] = self.github.get_pr_files(pr_number)
        pr_data["diff"] = self.github.get_pr_diff(pr_number)
        
        pr_context = create_pr_context_from_github(pr_data)
        repo_files = self.github.get_repo_files(pr_context)
        
        comments = self.reviewer.review_pr(pr_context, repo_files)
        return comments
    
    def post_review(self, pr_number: int, comments: list[ReviewComment]):
        if comments:
            self.github.create_review(pr_number, comments)
        else:
            self.github.post_general_comment(pr_number, "lgtm. nothing jumped out.")


def run_github_action():
    import sys
    pr_number = int(os.getenv("PR_NUMBER", "0"))
    repo = os.getenv("GITHUB_REPOSITORY", "")
    github_token = os.getenv("GITHUB_TOKEN", "")
    groq_key = os.getenv("GROQ_API_KEY", "")
    
    if not all([pr_number, repo, github_token]):
        print("Missing required env vars")
        sys.exit(1)
    
    reviewer = AutoReviewer(github_token, repo, groq_key)
    comments = reviewer.review_pr(pr_number)
    reviewer.post_review(pr_number, comments)
    print(f"Posted {len(comments)} comments")


if __name__ == "__main__":
    run_github_action()