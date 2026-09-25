import json
from pathlib import Path
from typing import Any

import pytest

from ambient import hooks_install as hi

CMD = "/opt/venv/bin/ambient fire"


def ours(command: str = CMD) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=4))


def after(change: hi.Change) -> Any:
    assert change.after is not None
    return json.loads(change.after)


@pytest.fixture
def settings(tmp_path: Path) -> Path:
    return tmp_path / "claude" / "settings.json"


def test_settings_file_honours_claude_config_dir(tmp_path: Path) -> None:
    assert hi.settings_file() == tmp_path / "claude" / "settings.json"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ambient fire", True),
        ("/usr/local/bin/ambient fire", True),
        ("'/Users/me/my venv/bin/ambient' fire", True),
        ('"/Users/me/my venv/bin/ambient" fire', True),
        ("ambient fire --verbose", False),
        ("not-ambient fire", False),
        ("echo /x/ambient fire", False),
        ("say done", False),
        (None, False),
    ],
)
def test_is_ambient_command(command: object, expected: bool) -> None:
    assert hi.is_ambient_command(command) is expected


def test_fire_command_quotes_paths_with_spaces() -> None:
    command = hi.fire_command(Path("/Users/me/my venv/bin/ambient"))
    assert command == "'/Users/me/my venv/bin/ambient' fire"
    assert hi.is_ambient_command(command)


def test_ambient_executable_is_absolute() -> None:
    exe = hi.ambient_executable()
    assert exe.is_absolute()
    assert exe.name == "ambient"


def test_install_into_missing_file(settings: Path) -> None:
    change = hi.plan_install(settings, CMD)
    assert change.before is None
    assert after(change) == {"hooks": {"UserPromptSubmit": [ours()], "Stop": [ours()]}}
    assert hi.apply(change) is None  # nothing to back up
    assert json.loads(settings.read_text()) == after(change)


def test_install_keeps_other_settings_and_hooks(settings: Path) -> None:
    other = {"hooks": [{"type": "command", "command": "say done"}]}
    write(
        settings,
        {
            "model": "opus",
            "hooks": {
                "Stop": [other],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}],
            },
        },
    )
    data = after(hi.plan_install(settings, CMD))
    assert data["model"] == "opus"
    assert data["hooks"]["Stop"] == [other, ours()]
    assert data["hooks"]["UserPromptSubmit"] == [ours()]
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "Bash"


def test_install_does_not_add_notification(settings: Path) -> None:
    assert "Notification" not in after(hi.plan_install(settings, CMD))["hooks"]


def test_install_is_idempotent_and_leaves_formatting_alone(settings: Path) -> None:
    hi.apply(hi.plan_install(settings, CMD))
    write(settings, json.loads(settings.read_text()))  # reformat with 4-space indent
    change = hi.plan_install(settings, CMD)
    assert not change.changed
    assert hi.apply(change) is None


def test_install_replaces_a_stale_path(settings: Path) -> None:
    write(settings, {"hooks": {"Stop": [ours("/old/bin/ambient fire")]}})
    data = after(hi.plan_install(settings, CMD))
    assert data["hooks"]["Stop"] == [ours()]


def test_install_dedupes_our_hook_sharing_a_group(settings: Path) -> None:
    group = {
        "hooks": [
            {"type": "command", "command": "say done"},
            {"type": "command", "command": "/old/ambient fire"},
        ]
    }
    write(settings, {"hooks": {"Stop": [group]}})
    data = after(hi.plan_install(settings, CMD))
    assert data["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "say done"}]},
        ours(),
    ]


def test_apply_backs_up_the_original(settings: Path) -> None:
    write(settings, {"model": "opus"})
    original = settings.read_text()
    backup = hi.apply(hi.plan_install(settings, CMD))
    assert backup is not None
    assert backup.parent == settings.parent
    assert backup.read_text() == original
    # A second write in the same second gets its own backup.
    second = hi.apply(hi.plan_uninstall(settings))
    assert second is not None
    assert second != backup
    assert json.loads(second.read_text())["hooks"]


def test_apply_writes_through_a_symlink(settings: Path, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    write(real, {"model": "opus"})
    settings.parent.mkdir(parents=True)
    settings.symlink_to(real)
    backup = hi.apply(hi.plan_install(settings, CMD))
    assert settings.is_symlink()
    assert settings.resolve() == real.resolve()
    assert json.loads(real.read_text())["hooks"]
    assert backup is not None
    assert json.loads(backup.read_text()) == {"model": "opus"}


def test_uninstall_removes_only_ours(settings: Path) -> None:
    other = {"hooks": [{"type": "command", "command": "say done"}]}
    write(
        settings,
        {
            "model": "opus",
            "hooks": {
                "Stop": [other, ours()],
                "UserPromptSubmit": [ours()],
                "Notification": [ours("ambient fire")],  # from an older install
            },
        },
    )
    assert after(hi.plan_uninstall(settings)) == {"model": "opus", "hooks": {"Stop": [other]}}


def test_uninstall_drops_empty_hooks_section(settings: Path) -> None:
    hi.apply(hi.plan_install(settings, CMD))
    assert after(hi.plan_uninstall(settings)) == {}


def test_uninstall_without_our_hooks_changes_nothing(settings: Path) -> None:
    change = hi.plan_uninstall(settings)
    assert not change.changed
    hi.apply(change)
    assert not settings.exists()

    write(settings, {"hooks": {"Stop": [ours("say done")]}})
    assert not hi.plan_uninstall(settings).changed


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{nope", "not valid JSON"),
        ("[]", "expected a JSON object"),
        ('{"hooks": []}', '"hooks"'),
        ('{"hooks": {"Stop": {}}}', "hooks.Stop"),
    ],
)
def test_malformed_settings_are_rejected(settings: Path, content: str, message: str) -> None:
    settings.parent.mkdir(parents=True)
    settings.write_text(content)
    with pytest.raises(hi.HooksError, match=message):
        hi.plan_install(settings, CMD)
