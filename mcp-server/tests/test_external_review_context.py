"""Remote review material is immutable, complete and safe before any model sees it."""
from __future__ import annotations

import base64
import copy
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from relay import expert_consult, external_pr, external_review_context as context


REPO = "FourBis/synthetic-review"
HEAD, BASE = "a" * 40, "b" * 40
PATCH = "@@ -1,2 +1,3 @@\n keep\n-old\n+replacement\n+extra"


def git_blob(raw):
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    return sha, {"sha": sha, "encoding": "base64", "size": len(raw),
                 "content": base64.b64encode(raw).decode("ascii")}


@pytest.fixture
def remote(monkeypatch):
    job = {"repo": REPO, "repo_id": 42, "pr_number": 17, "head_sha": HEAD, "base_sha": BASE}
    pr = {"number": 17, "html_url": f"https://github.com/{REPO}/pull/17", "state": "open",
          "changed_files": 1, "head": {"sha": HEAD, "repo": {"id": 42, "full_name": REPO}},
          "base": {"sha": BASE, "repo": {"id": 42, "full_name": REPO}}}
    sha, blob = git_blob(b"keep\nreplacement\nextra\n")
    row = {"filename": "src/change.py", "status": "modified", "sha": sha,
           "additions": 2, "deletions": 1, "patch": PATCH}
    comparison = {"base_commit": {"sha": BASE}, "files": [row]}
    state = SimpleNamespace(job=job, pr=pr, comparison=comparison, blobs={sha: blob},
                            row=row, blob=blob, calls=[], observations=[])

    async def api(endpoint):
        state.calls.append(endpoint)
        if endpoint == f"repos/{REPO}/pulls/17":
            return copy.deepcopy(state.observations.pop(0) if state.observations else state.pr)
        if endpoint == f"repos/{REPO}/compare/{BASE}...{HEAD}?per_page=1":
            return copy.deepcopy(state.comparison)
        prefix = f"repos/{REPO}/git/blobs/"
        if endpoint.startswith(prefix) and endpoint[len(prefix):] in state.blobs:
            return copy.deepcopy(state.blobs[endpoint[len(prefix):]])
        raise AssertionError(f"Unexpected synthetic API request: {endpoint}")

    state.api = AsyncMock(side_effect=api)
    state.model = AsyncMock(side_effect=AssertionError("Context reading must never run a model"))
    monkeypatch.setattr(external_pr, "_api", state.api)
    monkeypatch.setattr(expert_consult, "run_consult", state.model)
    return state


async def test_reads_exact_immutable_compare_and_verified_blob_with_only_changed_right_lines(remote):
    material = await context.read(remote.job)
    assert {key: material[key] for key in remote.job} == remote.job
    assert material["coverage"] == "complete"
    assert material["tests_executed"] is False
    assert material["files"] == [{"path": "src/change.py", "status": "modified",
        "blob_sha": remote.row["sha"], "patch": PATCH, "content": "keep\nreplacement\nextra\n",
        "content_at": "head", "changed_lines": [2, 3], "content_scope": "full"}]
    assert remote.calls == [f"repos/{REPO}/pulls/17",
        f"repos/{REPO}/compare/{BASE}...{HEAD}?per_page=1",
        f"repos/{REPO}/git/blobs/{remote.row['sha']}", f"repos/{REPO}/pulls/17"]
    remote.model.assert_not_awaited()


async def test_removed_file_uses_base_content_without_right_side_inline_locations(remote):
    sha, blob = git_blob(b"keep\nold\n")
    remote.blobs[sha] = blob
    remote.row.update(status="removed", sha=sha, additions=0, deletions=1,
                      patch="@@ -1,2 +1 @@\n keep\n-old")
    row = (await context.read(remote.job))["files"][0]
    assert row["content_at"] == "base"
    assert row["content"] == "keep\nold\n"
    assert row["changed_lines"] == []


@pytest.mark.parametrize("change", ["head", "base", "closed"])
async def test_changed_pr_before_read_is_superseded_before_compare_or_blob(remote, change):
    if change == "closed":
        remote.pr["state"] = "closed"
    else:
        remote.pr[change]["sha"] = "c" * 40
    with pytest.raises(context.Superseded):
        await context.read(remote.job)
    assert remote.calls == [f"repos/{REPO}/pulls/17"]
    remote.model.assert_not_awaited()


async def test_changed_head_during_read_never_returns_the_old_material(remote):
    changed = copy.deepcopy(remote.pr)
    changed["head"]["sha"] = "c" * 40
    remote.observations = [copy.deepcopy(remote.pr), changed]
    with pytest.raises(context.Superseded):
        await context.read(remote.job)
    assert remote.calls[-1] == f"repos/{REPO}/pulls/17"
    assert len(remote.calls) == 4
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("foreign", ["id", "name", "url", "number"])
async def test_foreign_pr_identity_is_rejected_before_reading_code(remote, foreign):
    if foreign == "id":
        remote.pr["base"]["repo"]["id"] = 99
    elif foreign == "name":
        remote.pr["base"]["repo"]["full_name"] = "Other/private-repo"
    elif foreign == "url":
        remote.pr["html_url"] = "https://outside.example/pull/17"
    else:
        remote.pr["number"] = 18
    with pytest.raises(context.ReviewContextError):
        await context.read(remote.job)
    assert len(remote.calls) == 1
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "src/../outside.py",
    "src/./change.py", "src\\change.py", "src/line\nname.py", ""])
async def test_unverifiable_paths_are_rejected_before_blob_read(remote, path):
    remote.row["filename"] = path
    with pytest.raises(context.ReviewContextError):
        await context.read(remote.job)
    assert len(remote.calls) == 2
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("kind", ["missing_file", "wrong_base", "duplicate_file", "too_many_files"])
async def test_incomplete_or_excessive_compare_is_never_claimed_complete(remote, kind):
    if kind == "missing_file":
        remote.pr["changed_files"] = 2
    elif kind == "wrong_base":
        remote.comparison["base_commit"]["sha"] = "c" * 40
    elif kind == "duplicate_file":
        remote.pr["changed_files"] = 2
        remote.comparison["files"].append(copy.deepcopy(remote.row))
    else:
        remote.pr["changed_files"] = 51
    with pytest.raises(context.ReviewContextError):
        await context.read(remote.job)
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("patch,additions,deletions", [
    ("@@ -1 +1 @@\n-old\n+new", 1, 1),
    ("@@ -0,0 +1,2 @@\n+one\n+two", 2, 0),
    ("@@ -4 +3,0 @@\n-removed", 0, 1),
    ("@@ -1,2 +1,2 @@\n keep\n-old\n+new\n@@ -5 +5,2 @@\n-old\n+newer\n+extra", 3, 2),
    ("@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file", 1, 1),
])
def test_changed_lines_validates_counts_and_returns_only_added_right_lines(patch, additions, deletions):
    expected = [1, 2] if additions == 2 else [2, 5, 6] if additions == 3 else [1] if additions else []
    assert context.changed_lines(patch, additions, deletions) == expected


@pytest.mark.parametrize("patch,additions,deletions", [
    ("@@ -1 +1,2 @@\n-old\n+new", 1, 1),
    ("@@ -1 +1 @@\n-old\n+new\n+overflow", 2, 1),
    ("@@ -1 +1 @@\n-old\n+new", 2, 1),
    ("@@ -1 +1 @@\n-old\n+new", 1, 0),
    ("not a unified diff", 0, 0),
    ("@@ -1 +1 @@\n?unexpected", 0, 0),
])
def test_changed_lines_rejects_truncation_overflow_and_mismatched_totals(patch, additions, deletions):
    with pytest.raises(context.ReviewContextError):
        context.changed_lines(patch, additions, deletions)


@pytest.mark.parametrize("kind", ["missing_patch", "truncated_patch", "bool_additions"])
async def test_unverifiable_patch_metadata_blocks_before_blob_or_model(remote, kind):
    if kind == "missing_patch":
        remote.row.pop("patch")
    elif kind == "truncated_patch":
        remote.row["patch"] = "@@ -1,2 +1,3 @@\n keep\n-old\n+replacement"
    else:
        remote.row["additions"] = True
    with pytest.raises(context.ReviewContextError):
        await context.read(remote.job)
    assert len(remote.calls) == 2
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("kind", ["wrong_sha", "wrong_size", "oversized", "invalid_base64", "binary", "non_utf8"])
async def test_blob_integrity_and_text_limits_block_before_any_model(remote, kind):
    if kind == "wrong_sha":
        remote.blob["content"] = base64.b64encode(b"different content").decode()
        remote.blob["size"] = len(b"different content")
    elif kind == "wrong_size":
        remote.blob["size"] += 1
    elif kind == "oversized":
        remote.blob["size"] = 100_001
    elif kind == "invalid_base64":
        remote.blob["content"] = "!not-base64!"
    else:
        raw = b"binary\0content" if kind == "binary" else b"\xff\xfe"
        sha, blob = git_blob(raw)
        remote.row["sha"] = sha
        remote.blobs[sha] = blob
    with pytest.raises(context.ReviewContextError):
        await context.read(remote.job)
    remote.model.assert_not_awaited()


async def test_aggregate_context_limit_blocks_instead_of_silently_truncating(remote):
    raw = ("x" * 60_000 + "\n").encode()
    sha, blob = git_blob(raw)
    remote.blobs[sha] = blob
    remote.pr["changed_files"] = 2
    patch = "@@ -0,0 +1 @@\n+" + raw.decode().rstrip("\n")
    remote.comparison["files"] = [{"filename": path, "status": "added", "sha": sha,
        "patch": patch, "additions": 1, "deletions": 0} for path in ("one.txt", "two.txt")]
    with pytest.raises(context.ReviewContextError):
        await context.read(remote.job)
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("path", [".env", ".env.local", "config/.env.example", "cert.pem",
    "private.key", "identity.pfx", "identity.p12", "id_rsa", ".aws/credentials",
    "credentials.json", "appsettings.json", "src/appsettings.Development.json"])
async def test_sensitive_paths_block_before_fetching_content_or_calling_model(remote, path):
    remote.row["filename"] = path
    with pytest.raises(context.ReviewContextError):
        await context.read(remote.job)
    assert len(remote.calls) == 2
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("secret", ["ghp_" + "a" * 30, '"password": "synthetic-private-value"',
    "Authorization: Bearer synthetic-private-value", "api_key=synthetic-private-value"])
@pytest.mark.parametrize("location", ["content", "patch"])
async def test_detected_credentials_block_without_echoing_sensitive_values(remote, secret, location):
    if location == "content":
        sha, blob = git_blob((secret + "\n").encode())
        remote.row["sha"] = sha
        remote.blobs[sha] = blob
    else:
        remote.row.update(patch="@@ -0,0 +1 @@\n+" + secret, additions=1, deletions=0)
    with pytest.raises(context.ReviewContextError) as failure:
        await context.read(remote.job)
    assert secret not in str(failure.value)
    assert "synthetic-private-value" not in str(failure.value)
    remote.model.assert_not_awaited()


@pytest.mark.parametrize("field", ["access_token", "auth_token", "token"])
@pytest.mark.parametrize("location", ["content", "patch"])
async def test_unquoted_credentials_are_blocked_without_model_or_echo(remote, field, location):
    secret = f"{field}: SYNTHETIC_EXAMPLE_VALUE_123"
    if location == "content":
        sha, blob = git_blob((secret + "\n").encode())
        remote.row["sha"] = sha
        remote.blobs[sha] = blob
    else:
        remote.row.update(patch="@@ -0,0 +1 @@\n+" + secret, additions=1, deletions=0)
    with pytest.raises(context.ReviewContextError) as failure:
        await context.read(remote.job)
    assert "SYNTHETIC_EXAMPLE_VALUE_123" not in str(failure.value)
    remote.model.assert_not_awaited()


def test_large_files_shrink_to_numbered_excerpts_keeping_the_whole_diff(monkeypatch):
    monkeypatch.setattr(context, "MAX_CONTEXT_CHARS", 3_000)
    big = "".join(f"linea {n}\n" for n in range(1, 401))
    small = "a\nb\n"
    files = [{"path": "big.cs", "patch": "@@ -200,1 +200,1 @@\n-old\n+new", "content": big, "content_at": "head"},
             {"path": "small.cs", "patch": "@@ -1 +1 @@\n-x\n+a", "content": small, "content_at": "head"}]
    context._fit_context(files)
    assert files[1]["content_scope"] == "full" and files[1]["content"] == small
    assert files[0]["content_scope"] == "excerpt"
    assert files[0]["content"].startswith("@@ líneas 160-240 de 400 @@\nlinea 160\n")
    assert "linea 200\n" in files[0]["content"] and "linea 241" not in files[0]["content"]
    assert files[0]["patch"].endswith("+new")


def test_excerpt_merges_close_hunks_and_uses_base_lines_for_removed_files():
    content = "".join(f"l{n}\n" for n in range(1, 101))
    ranges = context.hunk_ranges("@@ -10,2 +10,3 @@\n x\n@@ -30 +31 @@\n-y\n+z", "head")
    assert ranges == [(10, 3), (31, 1)]
    text = context.excerpt(content, ranges, radius=5)
    assert text.count("@@ líneas") == 2 and text.startswith("@@ líneas 5-17 de 100 @@")
    merged = context.excerpt(content, [(10, 1), (14, 1)], radius=3)
    assert merged.count("@@ líneas") == 1 and merged.startswith("@@ líneas 7-17 de 100 @@")
    assert context.hunk_ranges("@@ -50,4 +0,0 @@\n-a", "base") == [(50, 4)]


def test_still_fails_honestly_when_the_diff_alone_does_not_fit(monkeypatch):
    monkeypatch.setattr(context, "MAX_CONTEXT_CHARS", 10)
    files = [{"path": "a", "patch": "@@ -1 +1 @@\n-" + "x" * 50 + "\n+y", "content": "y\n", "content_at": "head"}]
    with pytest.raises(context.ReviewContextError, match="requiere revisión manual"):
        context._fit_context(files)
