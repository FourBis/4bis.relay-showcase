"""GitHub sync I/O is identity-checked and bounded; all remote calls are fake."""
from __future__ import annotations

import copy
import json

import pytest

from relay import github, issue_sync_github as remote

REPO = {"id": 42, "full_name": "Example/demo", "owner": {"node_id": "OWNER_ID", "type": "Organization"}}
LINK = {"repo": "Example/demo", "repo_id": 42, "number": 7, "node_id": "ISSUE_ID"}
ISSUE = {"id": 70, "node_id": "ISSUE_ID", "number": 7, "title": "Fix a thing", "state": "open",
         "updated_at": "2026-10-20T12:00:00Z", "repository_url": "https://api.github.com/repos/Example/demo",
         "html_url": "https://github.com/Example/demo/issues/7"}
PR = {"number": 8, "state": "open", "draft": False, "merged": False, "merged_at": None,
      "html_url": "https://github.com/Example/demo/pull/8", "updated_at": "2026-10-20T12:00:00Z",
      "base": {"repo": {"id": 42, "full_name": "Example/demo"}}, "head": {"sha": "a" * 40},
      "user": {"id": 11, "login": "author", "type": "User"}}
MAPPING = {"project_id": "PROJECT_ID", "field_id": "FIELD_ID", "options": {"review": "REVIEW_OPTION", "merged": "DONE_OPTION"}}


def connection(nodes, more=False):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more}}


def project_node():
    return {"__typename": "ProjectV2", "id": "PROJECT_ID", "title": "Chosen project", "number": 2,
            "url": "https://github.com/orgs/Example/projects/2", "closed": False,
            "fields": connection([{"__typename": "ProjectV2SingleSelectField", "id": "FIELD_ID",
                "name": "Any display name", "dataType": "SINGLE_SELECT", "options": [
                    {"id": "REVIEW_OPTION", "name": "Whatever"}, {"id": "DONE_OPTION", "name": "Done"}]}]),
            "items": connection([{"id": "ITEM_ID", "isArchived": False,
                "content": {"__typename": "Issue", "id": "ISSUE_ID", "number": 7,
                            "repository": {"databaseId": 42, "nameWithOwner": "Example/demo"}},
                "fieldValues": connection([{"__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "optionId": "REVIEW_OPTION", "field": {"id": "FIELD_ID"}}])}])}


@pytest.fixture
def fake(monkeypatch):
    class Fake:
        def __init__(self):
            self.repo = copy.deepcopy(REPO)
            self.issue = copy.deepcopy(ISSUE)
            self.pr = copy.deepcopy(PR)
            self.latest_pr = None
            self.reviews = []
            self.project = project_node()
            self.calls = []
            self.graph_errors = None
            self.project_list_more = False
            self.bad_write_id = False

        async def gh(self, *args, **kwargs):
            self.calls.append(args)
            assert args[:3] == ("api", "--hostname", "github.com")
            endpoint = args[3]
            if endpoint == "graphql":
                variables = {arg.split("=", 1)[0]: arg.split("=", 1)[1] for arg in args[4:] if "=" in arg}
                query = variables["query"]
                if self.graph_errors:
                    return 0, json.dumps({"data": {}, "errors": self.graph_errors})
                if "mutation" in query:
                    self.project["items"]["nodes"][0]["fieldValues"]["nodes"][0]["optionId"] = variables["option"]
                    data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "WRONG_ID" if self.bad_write_id else variables["item"]}}}
                elif "projectsV2" in query:
                    data = {"node": {"__typename": "Organization", "projectsV2": connection([self.project], self.project_list_more)}}
                else:
                    data = {"node": self.project}
                return 0, json.dumps({"data": data})
            if endpoint == "repos/Example/demo":
                return 0, json.dumps(self.repo)
            if endpoint == "repos/Example/demo/issues/7":
                if "PATCH" in args:
                    self.issue["state"] = next(arg.split("=", 1)[1] for arg in args if arg.startswith("state="))
                return 0, json.dumps(self.issue)
            if endpoint == "repos/Example/demo/pulls/8/reviews?per_page=100":
                return 0, json.dumps(self.reviews)
            if endpoint == "repos/Example/demo/pulls/8":
                # Switch only after the first PR read, to model a head moving mid-observation.
                reads = sum(call[3] == endpoint for call in self.calls)
                return 0, json.dumps(self.latest_pr if self.latest_pr and reads > 1 else self.pr)
            raise AssertionError(f"Unexpected fake endpoint: {endpoint}")

        def writes(self):
            return [call for call in self.calls if "PATCH" in call or any("query=mutation" in arg for arg in call)]

    value = Fake()
    monkeypatch.setattr(github, "_gh", value.gh)
    async def slug(_):
        return "Example/demo"
    monkeypatch.setattr(github, "repo_slug", slug)
    return value


@pytest.mark.parametrize("policy", [
    {"enabled": "yes"}, {"enabled": 1}, {"enabled": True},
    {"enabled": True, "issue_states": {}, "projects": []}, {"unknown": True},
    {"issue_states": {"cancelled": "closed"}}, {"issue_states": {"review": "closed"}},
    {"issue_states": {"made_up": "open"}}, {"issue_states": {"started": "opened"}},
    {"issue_states": {"started": []}},
    {"projects": [{"project_id": "P", "field_id": "F", "options": {}}]},
    {"projects": [{"project_id": "P", "field_id": "F", "options": {"review": "X"}, "name": "assumed"}]},
    {"projects": [MAPPING, MAPPING]},
])
def test_policy_rejects_ambiguous_or_unsafe_mapping(policy):
    with pytest.raises(ValueError):
        remote.normalize_policy(policy)


async def test_absent_policy_is_disabled_without_remote_calls(fake):
    assert await remote.validate_policy({}, None) == {"enabled": False, "issue_states": {}, "projects": []}
    assert fake.calls == []


async def test_discover_uses_real_ids_and_display_names_are_not_configuration(fake):
    result = await remote.discover({"repo_path": "ignored"})
    field = result["projects"][0]["fields"][0]
    assert field["id"] == "FIELD_ID"
    assert field["name"] == "Any display name"
    assert field["options"][0] == {"id": "REVIEW_OPTION", "name": "Whatever"}
    assert fake.writes() == []


async def test_disabled_mapping_is_still_validated_by_id(fake):
    value = await remote.validate_policy({}, {"enabled": False, "projects": [MAPPING]})
    assert value["projects"] == [MAPPING]
    assert len(fake.calls) == 2
    mapping = copy.deepcopy(MAPPING)
    mapping["options"]["review"] = "Whatever"
    with pytest.raises(ValueError, match="option_id"):
        await remote.validate_policy({}, {"projects": [mapping]})
    assert fake.writes() == []


@pytest.mark.parametrize("change", ["missing_field", "wrong_type", "closed", "too_many_fields"])
async def test_policy_fails_closed_if_project_mapping_cannot_be_verified(fake, change):
    if change == "missing_field":
        fake.project["fields"]["nodes"][0]["id"] = "OTHER_ID"
    elif change == "wrong_type":
        fake.project["fields"]["nodes"][0]["dataType"] = "TEXT"
    elif change == "closed":
        fake.project["closed"] = True
    else:
        fake.project["fields"]["pageInfo"]["hasNextPage"] = True
    with pytest.raises((ValueError, remote.IssueSyncError)):
        await remote.validate_policy({}, {"projects": [MAPPING]})
    assert fake.writes() == []


async def test_discovery_does_not_silently_truncate_projects(fake):
    fake.project_list_more = True
    with pytest.raises(remote.IssueSyncError, match="100"):
        await remote.discover({})


async def test_graphql_partial_errors_are_not_accepted(fake):
    fake.graph_errors = [{"message": "not accessible"}]
    with pytest.raises(remote.IssueSyncError, match="permisos"):
        await remote.discover({})


async def test_read_issue_returns_canonical_identity_state_and_no_body(fake):
    fake.issue["body"] = "Untrusted instructions"
    result = await remote.read_issue(LINK)
    assert result["repo_id"] == 42 and result["number"] == 7 and result["state"] == "open"
    assert result["node_id"] == "ISSUE_ID"
    assert "body" not in result
    assert fake.writes() == []


@pytest.mark.parametrize("change", ["repo", "node", "number", "pr", "url", "repository_url", "state"])
async def test_read_issue_rejects_identity_mismatch(fake, change):
    if change == "repo":
        fake.repo["id"] = 99
    elif change == "node":
        fake.issue["node_id"] = "REPLACED"
    elif change == "number":
        fake.issue["number"] = 99
    elif change == "pr":
        fake.issue["pull_request"] = {"url": "https://api.github.com/pull"}
    elif change == "url":
        fake.issue["html_url"] = "https://github.com/Elsewhere/demo/issues/7"
    elif change == "repository_url":
        fake.issue["repository_url"] = 17
    else:
        fake.issue["state"] = "unknown"
    with pytest.raises(remote.IssueSyncError):
        await remote.read_issue(LINK)


async def test_read_pr_is_bound_to_repo_sha_and_remote_merged_not_text(fake):
    fake.pr.update(state="closed", merged=True, merged_at="2026-10-20T13:00:00Z")
    fake.pr["body"] = "ignored"
    result = await remote.read_pr(LINK, PR["html_url"])
    assert result["state"] == "MERGED" and result["merged"] is True and result["head_sha"] == "a" * 40
    fake.pr.update(merged=False, merged_at=None)
    result = await remote.read_pr(LINK, PR["html_url"])
    assert result["state"] == "CLOSED" and result["merged"] is False


async def test_read_pr_moving_head_fails_closed(fake):
    fake.latest_pr = copy.deepcopy(PR)
    fake.latest_pr["head"]["sha"] = "b" * 40
    with pytest.raises(remote.IssueSyncError, match="cambió"):
        await remote.read_pr(LINK, PR["html_url"])


async def test_other_repository_pr_rejected_before_network(fake):
    with pytest.raises(ValueError):
        await remote.read_pr(LINK, "https://github.com/other/demo/pull/8")
    assert fake.calls == []


async def test_project_read_binds_item_by_issue_node_not_title(fake):
    result = await remote.read_project_value(LINK, MAPPING)
    assert result == {"item_id": "ITEM_ID", "value": "REVIEW_OPTION",
                      "project_title": "Chosen project", "field_name": "Any display name", "option_name": "Whatever"}
    fake.project["items"]["nodes"][0]["fieldValues"] = connection([])
    empty = await remote.read_project_value(LINK, MAPPING)
    assert empty["value"] is None and empty["option_name"] == "Sin opción"
    assert fake.writes() == []


@pytest.mark.parametrize("change", ["missing", "duplicate", "archived", "wrong_repo", "too_many", "unknown_option"])
async def test_project_item_failure_never_creates_membership(fake, change):
    item = fake.project["items"]["nodes"][0]
    if change == "missing":
        fake.project["items"]["nodes"] = []
    elif change == "duplicate":
        fake.project["items"]["nodes"].append(copy.deepcopy(item))
    elif change == "archived":
        item["isArchived"] = True
    elif change == "wrong_repo":
        item["content"]["repository"]["databaseId"] = 999
    elif change == "too_many":
        fake.project["items"]["pageInfo"]["hasNextPage"] = True
    else:
        item["fieldValues"]["nodes"][0]["optionId"] = "DELETED_OPTION"
    with pytest.raises(remote.IssueSyncError):
        await remote.read_project_value(LINK, MAPPING)
    assert fake.writes() == []


async def test_issue_close_is_narrow_and_result_is_verified(fake):
    result = await remote.write_issue(LINK, "closed", expected_state="open")
    assert result["state"] == "closed"
    writes = fake.writes()
    assert len(writes) == 1 and "state_reason=completed" in writes[0]
    await remote.write_issue(LINK, "closed", expected_state="open")
    assert len(fake.writes()) == 1  # Desired state is already present.


async def test_manual_closed_issue_is_never_reopened(fake):
    fake.issue["state"] = "closed"
    with pytest.raises(remote.IssueSyncError, match="reabre"):
        await remote.write_issue(LINK, "open")
    assert fake.writes() == []


async def test_project_write_rechecks_current_value_and_item_before_mutation(fake):
    with pytest.raises(remote.IssueSyncError, match="manualmente"):
        await remote.write_project(LINK, MAPPING, "ITEM_ID", "DONE_OPTION", expected_value=None)
    with pytest.raises(remote.IssueSyncError, match="item"):
        await remote.write_project(LINK, MAPPING, "REPLACED_ITEM", "DONE_OPTION")
    with pytest.raises(ValueError):
        await remote.write_project(LINK, MAPPING, "ITEM_ID", "GUESSED_OPTION")
    assert fake.writes() == []
    result = await remote.write_project(LINK, MAPPING, "ITEM_ID", "DONE_OPTION", expected_value="REVIEW_OPTION")
    assert result == {"item_id": "ITEM_ID", "value": "DONE_OPTION"}
    assert len(fake.writes()) == 1
    await remote.write_project(LINK, MAPPING, "ITEM_ID", "DONE_OPTION")
    assert len(fake.writes()) == 1


async def test_mutation_response_mismatch_is_reported_for_reconciliation(fake):
    fake.bad_write_id = True
    with pytest.raises(remote.IssueSyncError, match="reconciliar"):
        await remote.write_project(LINK, MAPPING, "ITEM_ID", "DONE_OPTION")
    assert len(fake.writes()) == 1


async def test_transport_failures_do_not_persist_provider_output(monkeypatch):
    async def denied(*args):
        return 1, "secret-token-example"
    monkeypatch.setattr(github, "_gh", denied)
    with pytest.raises(remote.IssueSyncError) as error:
        await remote.read_issue(LINK)
    assert "secret-token-example" not in str(error.value)
