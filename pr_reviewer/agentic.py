"""
Agentic PR Reviewer - Multi-agent architecture with ReAct loop
Based on research: Shared State Pattern, Tool-First Approach, deterministic analysis
"""
import os
import re
import json
import ast
import hashlib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

from .core import (
    DiffParser, ChunkManager, HumanisticReviewer,
    PRContext, ReviewComment, CodeChunk, CommentType
)


@dataclass
class IssueFinding:
    file_path: str
    line_number: int
    category: str
    severity: str
    text: str


@dataclass
class ReviewState:
    """Shared state pattern: all agents read/write here"""
    pr_context: PRContext
    repo_files: dict = field(default_factory=dict)
    diff: str = ""
    files_changed: list = field(default_factory=list)
    static_issues: list = field(default_factory=list)
    llm_comments: list = field(default_factory=list)
    reviewed_chunks: set = field(default_factory=set)
    llm_calls: int = 0
    token_usage: int = 0
    summary: str = ""


class StaticAnalyzerAgent:
    """Tool-first deterministic analysis before LLM
    
    Runs fast, reliable checks without LLM cost.
    Catches common issues: hardcoded secrets, SQL injection,
    unused imports, long functions, etc.
    """
    
    def __init__(self):
        self.rules = [
            self._check_hardcoded_secrets,
            self._check_sql_injection,
            self._check_command_injection,
            self._check_long_functions,
            self._check_unused_imports,
            self._check_password_hashing,
        ]
    
    def analyze(self, state: ReviewState) -> list[IssueFinding]:
        findings = []
        for file_path in state.files_changed:
            content = state.repo_files.get(file_path, "")
            if not content:
                continue
            for rule in self.rules:
                try:
                    findings.extend(rule(file_path, content))
                except Exception:
                    pass
        state.static_issues.extend(findings)
        return findings
    
    def _check_hardcoded_secrets(self, file_path: str, content: str) -> list[IssueFinding]:
        findings = []
        secret_patterns = [
            (r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]", "potential hardcoded secret"),
            (r"changeme", "default password 'changeme'"),
        ]
        for line_num, line in enumerate(content.split('\n'), 1):
            for pattern, desc in secret_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    findings.append(IssueFinding(
                        file_path=file_path,
                        line_number=line_num,
                        category="security",
                        severity="critique",
                        text=f"looks like a hardcoded secret: {desc}"
                    ))
        return findings
    
    def _check_sql_injection(self, file_path: str, content: str) -> list[IssueFinding]:
        findings = []
        sql_patterns = [
            r"execute\s*\(\s*f['\"]",
            r"execute\s*\(\s*['\"].*%.*%.*['\"]\s*%",
            r"cursor\.execute\s*\(\s*['\"].*\+.*['\"]",
        ]
        for line_num, line in enumerate(content.split('\n'), 1):
            for pat in sql_patterns:
                if re.search(pat, line):
                    findings.append(IssueFinding(
                        file_path=file_path,
                        line_number=line_num,
                        category="security",
                        severity="critique",
                        text="possible SQL injection. use parameterized queries"
                    ))
        return findings
    
    def _check_command_injection(self, file_path: str, content: str) -> list[IssueFinding]:
        findings = []
        dangerous = [
            r"os\.system\s*\(",
            r"subprocess\.call\s*\(.*shell=True",
            r"subprocess\.Popen\s*\(.*shell=True",
            r"eval\s*\(",
        ]
        for line_num, line in enumerate(content.split('\n'), 1):
            for pat in dangerous:
                if re.search(pat, line):
                    findings.append(IssueFinding(
                        file_path=file_path,
                        line_number=line_num,
                        category="security",
                        severity="critique",
                        text="dangerous call. user input here could be an injection vector"
                    ))
        return findings
    
    def _check_long_functions(self, file_path: str, content: str) -> list[IssueFinding]:
        findings = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return findings
        
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if hasattr(node, 'end_lineno') and node.end_lineno:
                    length = node.end_lineno - node.lineno + 1
                    if length > 50:
                        findings.append(IssueFinding(
                            file_path=file_path,
                            line_number=node.lineno,
                            category="complexity",
                            severity="suggestion",
                            text=f"this function is {length} lines. consider splitting"
                        ))
        return findings
    
    def _check_unused_imports(self, file_path: str, content: str) -> list[IssueFinding]:
        findings = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return findings
        
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append((alias.name.split('.')[0], node.lineno))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imports.append((alias.name, node.lineno))
        
        for imp_name, line_num in imports:
            usage_count = len(re.findall(rf'\b{re.escape(imp_name)}\b', content))
            if usage_count <= 1:
                findings.append(IssueFinding(
                    file_path=file_path,
                    line_number=line_num,
                    category="cleanup",
                    severity="nitpick",
                    text=f"unused import? '{imp_name}'"
                ))
        return findings
    
    def _check_password_hashing(self, file_path: str, content: str) -> list[IssueFinding]:
        findings = []
        if 'hashlib.md5' in content or 'hashlib.sha1' in content:
            for line_num, line in enumerate(content.split('\n'), 1):
                if 'hashlib.md5' in line or 'hashlib.sha1' in line:
                    findings.append(IssueFinding(
                        file_path=file_path,
                        line_number=line_num,
                        category="security",
                        severity="critique",
                        text="use bcrypt/argon2 for passwords, not md5/sha1"
                    ))
        return findings


class LlmReviewAgent:
    """LLM-powered humanistic review
    
    Uses ReAct pattern: review chunks, observe issues, refine
    """
    
    def __init__(self, api_key: Optional[str] = None, model: str = None):
        self.reviewer = HumanisticReviewer(api_key, model)
        self.chunk_manager = ChunkManager(".pr_review_cache")
    
    def review(self, state: ReviewState) -> list[ReviewComment]:
        all_comments = []
        
        for file_path in state.files_changed:
            content = state.repo_files.get(file_path, "")
            if not content:
                continue
            
            file_diff = self._get_file_diff(state.diff, file_path)
            chunks = self.chunk_manager.chunk_code(file_path, content, file_diff)
            new_chunks = self.chunk_manager.get_unreviewed_chunks(chunks)
            
            for chunk in new_chunks:
                if not chunk.is_new:
                    continue
                comments = self.reviewer.review_chunk(chunk, state.pr_context)
                all_comments.extend(comments)
                state.reviewed_chunks.add(chunk.chunk_hash)
            
            self.chunk_manager.mark_reviewed(new_chunks)
        
        state.llm_comments = all_comments
        return all_comments
    
    def _get_file_diff(self, full_diff: str, file_path: str) -> str:
        files = DiffParser.parse_diff(full_diff)
        return '\n'.join(files.get(file_path, []))


class SummaryAgent:
    """Composes human-like overall summary from static + LLM findings"""
    
    def summarize(self, state: ReviewState) -> str:
        if not state.static_issues and not state.llm_comments:
            return "lgtm. nothing jumped out at me."
        
        parts = []
        
        crit_count = len([i for i in state.static_issues if i.severity == "critique"])
        question_count = len([c for c in state.llm_comments if c.comment_type == CommentType.QUESTION])
        nit_count = len([i for i in state.static_issues if i.severity == "nitpick"])
        suggestion_count = len([c for c in state.llm_comments if c.comment_type == CommentType.SUGGESTION])
        
        if crit_count:
            parts.append(f"{crit_count} critiqué{'s' if crit_count > 1 else ''}")
        if question_count:
            parts.append(f"{question_count} question{'s' if question_count > 1 else ''}")
        if nit_count:
            parts.append(f"{nit_count} nit{'s' if nit_count > 1 else ''}")
        if suggestion_count:
            parts.append(f"{suggestion_count} suggestion{'s' if suggestion_count > 1 else ''}")
        
        summary = ", ".join(parts) if parts else "several things"
        return f"left some comments ({summary}). not blocking unless critiques are real."
    
    def summarize_inline_comments(self, state: ReviewState) -> list[ReviewComment]:
        """Merge static findings into ReviewComments format"""
        comments = []
        for finding in state.static_issues:
            comments.append(ReviewComment(
                file_path=finding.file_path,
                line_number=finding.line_number,
                comment_type=CommentType(finding.severity) if finding.severity in [ct.value for ct in CommentType] else CommentType.NITPICK,
                text=finding.text,
                is_new_code=True
            ))
        comments.extend(state.llm_comments)
        return comments


class OrchestratorAgent:
    """Coordinates the review process using ReAct pattern"""
    
    def __init__(self, api_key: Optional[str] = None, model: str = None):
        self.static_analyzer = StaticAnalyzerAgent()
        self.llm_agent = LlmReviewAgent(api_key, model)
        self.summary_agent = SummaryAgent()
    
    def review_pr(self, pr_context: PRContext, repo_files: dict) -> list[ReviewComment]:
        state = ReviewState(
            pr_context=pr_context,
            repo_files=repo_files,
            diff=pr_context.diff,
            files_changed=pr_context.files_changed
        )
        
        # Phase 1: Static analysis (fast, deterministic, no LLM cost)
        static_findings = self.static_analyzer.analyze(state)
        print(f"  [Static] Found {len(static_findings)} issues")
        
        # Phase 2: LLM review (context-aware, humanistic)
        llm_comments = self.llm_agent.review(state)
        print(f"  [LLM] Generated {len(llm_comments)} comments")
        
        # Phase 3: Synthesize summary
        state.summary = self.summary_agent.summarize(state)
        all_comments = self.summary_agent.summarize_inline_comments(state)
        
        print(f"  [Summary] {state.summary}")
        
        return all_comments


def create_agentic_reviewer(api_key: Optional[str] = None, model: str = None) -> OrchestratorAgent:
    return OrchestratorAgent(api_key, model)