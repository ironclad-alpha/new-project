#!/usr/bin/env python3
"""
Test the PR reviewer with mocked LLM responses
"""
import sys
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent))

from pr_reviewer.core import PRReviewer, PRContext, DiffParser, ChunkManager, HumanisticReviewer


def cleanup_cache():
    shutil.rmtree(".pr_review_cache", ignore_errors=True)
    shutil.rmtree(".test_cache", ignore_errors=True)
    shutil.rmtree(".test_cache2", ignore_errors=True)


def test_diff_parser():
    cleanup_cache()
    diff = """diff --git a/auth.py b/auth.py
new file mode 100644
@@ -0,0 +1,10 @@
+import jwt
+import os
+
+SECRET = 'changeme'
+
+def create_token(user_id: int) -> str:
+    return jwt.encode({'sub': user_id}, SECRET)
"""
    files = DiffParser.parse_diff(diff)
    assert 'auth.py' in files
    print("[OK] DiffParser.parse_diff works")
    
    new_lines = DiffParser.extract_new_lines('auth.py', diff)
    assert len(new_lines) == 7
    print("[OK] DiffParser.extract_new_lines works")
    
    hunks = DiffParser.get_hunks(diff)
    assert len(hunks) == 1
    print("[OK] DiffParser.get_hunks works")


def test_chunk_manager():
    cleanup_cache()
    cm = ChunkManager(".test_cache")
    
    content = "\n".join([f"line {i}" for i in range(100)])
    diff = """diff --git a/test.py b/test.py
@@ -0,0 +1,100 @@
""" + "\n".join([f"+line {i}" for i in range(100)])
    
    chunks = cm.chunk_code("test.py", content, diff)
    assert len(chunks) > 1
    print(f"[OK] ChunkManager created {len(chunks)} chunks")
    
    new_chunks = cm.get_unreviewed_chunks(chunks)
    assert len(new_chunks) == len(chunks)
    print("[OK] All chunks are new initially")
    
    cm.mark_reviewed(new_chunks[:2])
    remaining = cm.get_unreviewed_chunks(chunks)
    assert len(remaining) == len(chunks) - 2
    print("[OK] Cache marks chunks as reviewed")
    
    cleanup_cache()


def test_reviewer_with_mock():
    cleanup_cache()
    full_diff = """diff --git a/auth.py b/auth.py
new file mode 100644
@@ -0,0 +1,6 @@
+import jwt
+import os
+SECRET = "changeme"
+def create_token(user_id: int) -> str:
+    return jwt.encode({"sub": user_id}, SECRET)"""

    pr_context = PRContext(
        pr_number=42,
        repo="test/repo",
        base_branch="main",
        head_branch="feature",
        title="Add auth",
        description="JWT auth",
        files_changed=["auth.py"],
        diff=full_diff
    )
    
    repo_files = {
        "auth.py": 'import jwt\nimport os\nSECRET = "changeme"\ndef create_token(user_id: int) -> str:\n    return jwt.encode({"sub": user_id}, SECRET)'
    }
    
    reviewer = PRReviewer(groq_api_key="test-key")
    
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps({
        "comments": [
            {"line": 3, "type": "critique", "text": "hardcoded secret. use env var"},
            {"line": 5, "type": "question", "text": "why HS256? RS256 better for prod"}
        ]
    })
    mock_client.chat.completions.create.return_value = mock_response
    reviewer.reviewer.client = mock_client
    
    comments = reviewer.review_pr(pr_context, repo_files)
    
    assert len(comments) == 2
    assert comments[0].comment_type.value == "critique"
    assert comments[1].comment_type.value == "question"
    print("[OK] PRReviewer works with mocked LLM")
    print(f"  Comments: {[f'{c.file_path}:{c.line_number} [{c.comment_type.value}] {c.text}' for c in comments]}")
    
    cleanup_cache()


def test_incremental_review():
    cleanup_cache()
    cm = ChunkManager(".test_cache2")
    
    content = "\n".join([f"line {i}" for i in range(50)])
    diff = """diff --git a/test.py b/test.py
@@ -0,0 +1,50 @@
""" + "\n".join([f"+line {i}" for i in range(50)])
    
    chunks1 = cm.chunk_code("test.py", content, diff)
    new1 = cm.get_unreviewed_chunks(chunks1)
    cm.mark_reviewed(new1)
    
    new_content = content + "\nline 50\nline 51"
    new_diff = diff + "\n+line 50\n+line 51"
    chunks2 = cm.chunk_code("test.py", new_content, new_diff)
    new2 = cm.get_unreviewed_chunks(chunks2)
    
    print(f"[OK] Incremental review: {len(new2)}/{len(chunks2)} new chunks (cache: {len(cm.reviewed_hashes)})")
    
    cleanup_cache()


if __name__ == "__main__":
    print("Running tests...\n")
    test_diff_parser()
    test_chunk_manager()
    test_reviewer_with_mock()
    test_incremental_review()
    print("\n[OK] All tests passed!")