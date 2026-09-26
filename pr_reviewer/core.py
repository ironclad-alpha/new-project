import os
import re
import json
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

try:
    from groq import Groq
except ImportError:
    Groq = None


class CommentType(Enum):
    NITPICK = "nitpick"
    QUESTION = "question"
    CRITIQUE = "critique"
    SUGGESTION = "suggestion"
    PRAISE = "praise"


@dataclass
class ReviewComment:
    file_path: str
    line_number: int
    comment_type: CommentType
    text: str
    chunk_id: str = ""
    is_new_code: bool = True


@dataclass
class CodeChunk:
    file_path: str
    start_line: int
    end_line: int
    content: str
    chunk_hash: str
    is_new: bool = True


@dataclass
class PRContext:
    pr_number: int
    repo: str
    base_branch: str
    head_branch: str
    title: str
    description: str
    files_changed: list = field(default_factory=list)
    diff: str = ""


class DiffParser:
    @staticmethod
    def parse_diff(diff: str) -> dict:
        files = {}
        current_file = None
        current_chunk = []
        in_hunk = False
        
        for line in diff.split('\n'):
            if line.startswith('diff --git'):
                if current_file and current_chunk:
                    files[current_file] = files.get(current_file, []) + ['\n'.join(current_chunk)]
                match = re.search(r'b/(.+)$', line)
                current_file = match.group(1) if match else None
                current_chunk = []
                in_hunk = False
            elif line.startswith('---') or line.startswith('+++'):
                continue
            elif line.startswith('@@'):
                if current_chunk:
                    files[current_file] = files.get(current_file, []) + ['\n'.join(current_chunk)]
                current_chunk = [line]
                in_hunk = True
            elif in_hunk:
                current_chunk.append(line)
        
        if current_file and current_chunk:
            files[current_file] = files.get(current_file, []) + ['\n'.join(current_chunk)]
        
        return files

    @staticmethod
    def extract_new_lines(file_path: str, diff: str) -> list:
        new_lines = []
        line_num = 0
        
        for line in diff.split('\n'):
            if line.startswith('@@'):
                match = re.search(r'\+(\d+)', line)
                if match:
                    line_num = int(match.group(1)) - 1
            elif line.startswith('+') and not line.startswith('+++'):
                line_num += 1
                new_lines.append((line_num, line[1:]))
            elif not line.startswith('-'):
                line_num += 1
                
        return new_lines

    @staticmethod
    def get_hunks(diff: str) -> list:
        hunks = []
        current_hunk = []
        in_hunk = False
        
        for line in diff.split('\n'):
            if line.startswith('@@'):
                if current_hunk:
                    hunks.append('\n'.join(current_hunk))
                current_hunk = [line]
                in_hunk = True
            elif in_hunk:
                current_hunk.append(line)
                
        if current_hunk:
            hunks.append('\n'.join(current_hunk))
            
        return hunks


class ChunkManager:
    MAX_CHUNK_SIZE = 80
    OVERLAP = 10
    
    def __init__(self, cache_dir: str = ".pr_review_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.reviewed_hashes = self._load_cache()
    
    def _load_cache(self) -> set:
        cache_file = self.cache_dir / "reviewed_chunks.json"
        if cache_file.exists():
            return set(json.loads(cache_file.read_text()))
        return set()
    
    def _save_cache(self):
        cache_file = self.cache_dir / "reviewed_chunks.json"
        cache_file.write_text(json.dumps(list(self.reviewed_hashes)))
    
    def chunk_code(self, file_path: str, content: str, diff: str = "") -> list[CodeChunk]:
        lines = content.split('\n')
        new_line_nums = set()
        
        if diff:
            for line_num, _ in DiffParser.extract_new_lines(file_path, diff):
                new_line_nums.add(line_num)
        
        chunks = []
        i = 0
        while i < len(lines):
            end = min(i + self.MAX_CHUNK_SIZE, len(lines))
            chunk_lines = lines[i:end]
            
            is_new = any((i + j + 1) in new_line_nums for j in range(len(chunk_lines)))
            chunk_content = '\n'.join(chunk_lines)
            chunk_hash = hashlib.md5(f"{file_path}:{i}:{chunk_content}".encode()).hexdigest()[:12]
            
            chunks.append(CodeChunk(
                file_path=file_path,
                start_line=i + 1,
                end_line=end,
                content=chunk_content,
                chunk_hash=chunk_hash,
                is_new=is_new
            ))
            
            i += self.MAX_CHUNK_SIZE - self.OVERLAP
        
        return chunks
    
    def get_unreviewed_chunks(self, chunks: list[CodeChunk]) -> list[CodeChunk]:
        return [c for c in chunks if c.chunk_hash not in self.reviewed_hashes]
    
    def mark_reviewed(self, chunks: list[CodeChunk]):
        for chunk in chunks:
            self.reviewed_hashes.add(chunk.chunk_hash)
        self._save_cache()


class HumanisticReviewer:
    SYSTEM_PROMPT = """You're a senior dev doing a code review. Be human - lazy, opinionated, sometimes helpful.

Style:
- Short. Real devs don't write essays.
- Ask questions more than dictate.
- Nitpick stuff that actually matters.
- Point out footguns before they bite.
- Skip the obvious. Don't say "this variable holds the user" on `user = get_user()`.
- Sound like you're at a keyboard, not a lecture hall.
- Use lowercase. contractions. fragments. it's fine.
- sometimes you miss things. that's fine too.

Comment types:
- nitpick: small thing, not blocking. "trailing comma?" "why not `const`?"
- question: genuine confusion. "what happens if this is null?" "why 42?"
- critique: actual problem. "this races." "leaks memory." "sql injection."
- suggestion: better way. "consider `map` instead of loop" "extract this?"
- praise: rare. "clean." "nice abstraction."

Don't:
- use bullet points or numbered lists
- say "great job" or "thanks for the PR"
- explain basics
- be thorough for thoroughness' sake
- use emojis
- sound like a bot"""

    REVIEW_PROMPT = """Review this code chunk. Context: {context}

File: {file_path} (lines {start_line}-{end_line})
```{language}
{code}
```

Only comment on NEW/CHANGED lines. Ignore unchanged context.

Output JSON array of comments:
[{{
  "line": <relative line in chunk>,
  "type": "nitpick|question|critique|suggestion|praise",
  "text": "<human comment>"
}}]

Empty array if nothing worth saying."""

    def __init__(self, api_key: Optional[str] = None, model: str = None):
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.model = model or os.getenv("PR_REVIEW_MODEL", "qwen/qwen3-32b")
        self.client = Groq(api_key=self.api_key) if Groq and self.api_key else None
    
    def _detect_language(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        return {
            '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
            '.jsx': 'javascript', '.tsx': 'typescript', '.go': 'go',
            '.rs': 'rust', '.java': 'java', '.cpp': 'cpp', '.c': 'c',
            '.cs': 'csharp', '.rb': 'ruby', '.php': 'php', '.swift': 'swift',
            '.kt': 'kotlin', '.scala': 'scala', '.clj': 'clojure',
            '.sh': 'bash', '.sql': 'sql', '.html': 'html', '.css': 'css',
            '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
            '.md': 'markdown', '.txt': 'text'
        }.get(ext, 'text')
    
    def review_chunk(self, chunk: CodeChunk, pr_context: PRContext) -> list[ReviewComment]:
        if not self.client:
            return []
        
        if not chunk.is_new:
            return []
        
        context = f"PR #{pr_context.pr_number}: {pr_context.title}. {pr_context.description[:200]}"
        language = self._detect_language(chunk.file_path)
        
        prompt = self.REVIEW_PROMPT.format(
            context=context,
            file_path=chunk.file_path,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            language=language,
            code=chunk.content
        )
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            comments = result.get("comments", [])
            
            review_comments = []
            for c in comments:
                abs_line = chunk.start_line + c.get("line", 0) - 1
                review_comments.append(ReviewComment(
                    file_path=chunk.file_path,
                    line_number=abs_line,
                    comment_type=CommentType(c.get("type", "nitpick")),
                    text=c.get("text", ""),
                    chunk_id=chunk.chunk_hash,
                    is_new_code=chunk.is_new
                ))
            
            return review_comments
            
        except Exception as e:
            print(f"Review error for {chunk.file_path}:{chunk.start_line}: {e}")
            return []


class PRReviewer:
    def __init__(self, groq_api_key: Optional[str] = None, cache_dir: str = ".pr_review_cache"):
        self.reviewer = HumanisticReviewer(groq_api_key)
        self.chunk_manager = ChunkManager(cache_dir)
        self.diff_parser = DiffParser()
    
    def review_pr(self, pr_context: PRContext, repo_files: dict[str, str]) -> list[ReviewComment]:
        all_comments = []
        
        for file_path in pr_context.files_changed:
            content = repo_files.get(file_path, "")
            if not content:
                continue
            
            file_diff = self._get_file_diff(pr_context.diff, file_path)
            chunks = self.chunk_manager.chunk_code(file_path, content, file_diff)
            new_chunks = self.chunk_manager.get_unreviewed_chunks(chunks)
            
            for chunk in new_chunks:
                comments = self.reviewer.review_chunk(chunk, pr_context)
                all_comments.extend(comments)
            
            self.chunk_manager.mark_reviewed(new_chunks)
        
        return all_comments
    
    def _get_file_diff(self, full_diff: str, file_path: str) -> str:
        files = self.diff_parser.parse_diff(full_diff)
        return '\n'.join(files.get(file_path, []))


def create_pr_context_from_github(pr_data: dict) -> PRContext:
    return PRContext(
        pr_number=pr_data.get("number", 0),
        repo=pr_data.get("base", {}).get("repo", {}).get("full_name", ""),
        base_branch=pr_data.get("base", {}).get("ref", ""),
        head_branch=pr_data.get("head", {}).get("ref", ""),
        title=pr_data.get("title", ""),
        description=pr_data.get("body", "") or "",
        files_changed=[f.get("filename") for f in pr_data.get("files", [])],
        diff=pr_data.get("diff", "")
    )