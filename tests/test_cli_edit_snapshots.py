"""Actual CLI dispatch preserves the base of file-backed definition edits."""
from __future__ import annotations

import json

import pytest

from agilecode.cli import execute, parser
from agilecode.core.defaults import standard_team
from agilecode.core.types import AgileCodeError, Conflict, ProviderConfig, Settings


@pytest.fixture
async def cli(repository, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Definition CLI constructed execution runtime")
    monkeypatch.setattr("agilecode.core.application.Application._initialize_runtime", forbidden)
    base = ["-p", str(repository), "--global-dir", str(tmp_path / "global")]
    async def run(*arguments):
        return await execute(parser().parse_args([*base, *arguments]))
    await run("init")
    return run


def write(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "project"])
async def test_team_create_and_captured_edit_work_across_cli_hosts(cli, tmp_path, scope):
    value = standard_team().model_copy(update={"id": "custom"}).model_dump(mode="json")
    raw = write(tmp_path, "team.json", value)
    assert await cli("teams", "save", raw, "--scope", scope) == {"saved": scope + ":custom"}
    assert await cli("teams", "export", scope + ":custom") == value
    with pytest.raises(Conflict, match="teams export.*--for-edit"):
        await cli("teams", "save", raw, "--scope", scope)
    draft = await cli("teams", "export", scope + ":custom", "--for-edit")
    draft["value"]["name"] = "Edited team"
    assert draft["expected"]["name"] == value["name"]
    # Saved scope is carried by the envelope when the flag is omitted.
    await cli("teams", "save", write(tmp_path, "edit.json", draft))
    assert (await cli("teams", "export", scope + ":custom"))["name"] == "Edited team"
    with pytest.raises(AgileCodeError, match="scope differs"):
        await cli("teams", "save", write(tmp_path, "edit.json", draft), "--scope", "global" if scope == "project" else "project")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "project"])
async def test_team_stale_edit_conflicts_but_different_team_survives(cli, tmp_path, scope):
    for identity in ("one", "two"):
        value = standard_team().model_copy(update={"id": identity}).model_dump(mode="json")
        await cli("teams", "save", write(tmp_path, identity + ".json", value), "--scope", scope)
    one = await cli("teams", "export", scope + ":one", "--for-edit")
    stale = await cli("teams", "export", scope + ":one", "--for-edit")
    two = await cli("teams", "export", scope + ":two", "--for-edit")
    one["value"]["name"] = "First writer"
    await cli("teams", "save", write(tmp_path, "first.json", one))
    stale["value"]["name"] = "Stale writer"
    with pytest.raises(Conflict):
        await cli("teams", "save", write(tmp_path, "stale.json", stale))
    two["value"]["name"] = "Independent writer"
    await cli("teams", "save", write(tmp_path, "second.json", two))
    assert (await cli("teams", "export", scope + ":one"))["name"] == "First writer"
    assert (await cli("teams", "export", scope + ":two"))["name"] == "Independent writer"


@pytest.mark.asyncio
async def test_provider_raw_create_and_edit_envelope(cli, tmp_path):
    value = ProviderConfig(id="custom", name="Custom").model_dump(mode="json")
    raw = write(tmp_path, "provider.json", value)
    await cli("providers", "save", raw)
    assert await cli("providers", "export", "custom") == value
    with pytest.raises(Conflict, match="providers export.*--for-edit"):
        await cli("providers", "save", raw)
    draft = await cli("providers", "export", "custom", "--for-edit")
    draft["value"]["name"] = "Edited provider"
    assert draft["expected"]["name"] == "Custom"
    await cli("providers", "save", write(tmp_path, "edit.json", draft), "--make-default")
    pool = await cli("providers", "list")
    assert pool["default_provider_id"] == "custom"
    assert (await cli("providers", "export", "custom"))["name"] == "Edited provider"


@pytest.mark.asyncio
async def test_provider_stale_same_entry_conflicts_and_disjoint_entry_survives(cli, tmp_path):
    await cli("providers", "save", write(tmp_path, "new.json", ProviderConfig(id="other", name="Other").model_dump(mode="json")))
    first = await cli("providers", "export", "local", "--for-edit")
    stale = await cli("providers", "export", "local", "--for-edit")
    disjoint = await cli("providers", "export", "other", "--for-edit")
    first["value"]["name"] = "First writer"
    await cli("providers", "save", write(tmp_path, "first.json", first))
    stale["value"]["profile"]["model"] = "stale-model"
    with pytest.raises(Conflict):
        await cli("providers", "save", write(tmp_path, "stale.json", stale))
    disjoint["value"]["profile"]["model"] = "independent-model"
    await cli("providers", "save", write(tmp_path, "disjoint.json", disjoint))
    assert (await cli("providers", "export", "local"))["name"] == "First writer"
    assert (await cli("providers", "export", "other"))["profile"]["model"] == "independent-model"


@pytest.mark.asyncio
async def test_provider_default_change_checks_captured_default(cli, tmp_path):
    for identity in ("one", "two"):
        await cli("providers", "save", write(tmp_path, identity + ".json", ProviderConfig(id=identity, name=identity).model_dump(mode="json")))
    draft = await cli("providers", "export", "one", "--for-edit")
    await cli("providers", "default", "two")
    with pytest.raises(Conflict, match="Default provider"):
        await cli("providers", "save", write(tmp_path, "edit.json", draft), "--make-default")
    assert (await cli("providers", "list"))["default_provider_id"] == "two"


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "project"])
async def test_settings_raw_create_then_captured_edit(cli, tmp_path, scope):
    value = Settings(max_corrections=3).model_dump(mode="json")
    raw = write(tmp_path, "settings.json", value)
    await cli("settings", "save", raw, "--scope", scope)
    assert await cli("settings", "show") == value
    with pytest.raises(Conflict, match="settings show --for-edit"):
        await cli("settings", "save", raw, "--scope", scope)
    draft = await cli("settings", "show", "--scope", scope, "--for-edit")
    draft["value"]["max_corrections"] = 6
    assert draft["expected"]["effective"]["max_corrections"] == 3
    await cli("settings", "save", write(tmp_path, "edit.json", draft))
    assert (await cli("settings", "show"))["max_corrections"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["global", "project"])
async def test_settings_stale_same_field_conflicts_disjoint_field_survives(cli, tmp_path, scope):
    first = await cli("settings", "show", "--scope", scope, "--for-edit")
    stale = await cli("settings", "show", "--scope", scope, "--for-edit")
    disjoint = await cli("settings", "show", "--scope", scope, "--for-edit")
    first["value"]["max_corrections"] = 4
    await cli("settings", "save", write(tmp_path, "first.json", first))
    stale["value"]["max_corrections"] = 8
    with pytest.raises(Conflict):
        await cli("settings", "save", write(tmp_path, "stale.json", stale))
    disjoint["value"]["theme"] = "light"
    await cli("settings", "save", write(tmp_path, "disjoint.json", disjoint))
    current = await cli("settings", "show")
    assert current["max_corrections"] == 4 and current["theme"] == "light"


@pytest.mark.asyncio
async def test_default_read_shapes_and_edit_input_validation(cli, tmp_path):
    assert "format" not in await cli("settings", "show")
    assert "providers" in await cli("providers", "list")
    assert isinstance(await cli("teams", "list"), list)
    assert (await cli("teams", "export", "builtin:standard"))["id"] == standard_team().id
    with pytest.raises(AgileCodeError, match="Built-in teams are immutable"):
        await cli("teams", "export", "builtin:standard", "--for-edit")
    draft = await cli("providers", "export", "local", "--for-edit")
    with pytest.raises(AgileCodeError, match="mismatched edit envelope"):
        await cli("teams", "save", write(tmp_path, "wrong.json", draft))
    del draft["expected_default"]
    with pytest.raises(AgileCodeError, match="retain expected_default"):
        await cli("providers", "save", write(tmp_path, "missing.json", draft))


@pytest.mark.asyncio
async def test_actual_cli_cursor_direction_boundaries_and_latest_default(cli, repository, tmp_path):
    from agilecode.adapters.store import ProjectStore
    await cli("settings", "save", write(tmp_path, "paging.json", Settings(conversation_page_size=2).model_dump(mode="json")))
    store = ProjectStore(repository / ".agilecode" / "agilecode.db")
    await store.open()
    try:
        for text in ("first", "second", "third", "fourth", "fifth"):
            await store.message("cli-paging", {"role": "user", "content": text})
    finally:
        await store.close()
    latest = await cli("conversation", "cli-paging")
    first = await cli("conversation", "cli-paging", "--after", "0")
    second = await cli("conversation", "cli-paging", "--after", str(first[-1]["seq"]))
    final = await cli("conversation", "cli-paging", "--after", str(second[-1]["seq"]))
    previous = await cli("conversation", "cli-paging", "--before", str(latest[0]["seq"]))
    assert [r["body"]["content"] for r in latest] == ["fourth", "fifth"]
    assert [r["body"]["content"] for r in previous] == ["second", "third"]
    combined = first + second + final
    assert [r["body"]["content"] for r in combined] == ["first", "second", "third", "fourth", "fifth"]
    assert len({r["seq"] for r in combined}) == 5
    assert await cli("conversation", "cli-paging", "--before", str(first[0]["seq"])) == []
    assert await cli("conversation", "cli-paging", "--after", str(final[-1]["seq"])) == []
