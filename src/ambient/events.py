"""Claude Code hook events that `ambient` handles. Stdlib-only: imported on the `fire` path."""

from typing import Final, Literal, TypeGuard

HookEvent = Literal["UserPromptSubmit", "Stop"]

# Hooks installed by `install-hooks` and events `fire` acts on. `Notification` is accepted in the
# config but not handled yet.
HANDLED_EVENTS: Final[tuple[HookEvent, ...]] = ("UserPromptSubmit", "Stop")


def is_handled(name: object) -> TypeGuard[HookEvent]:
    return name in HANDLED_EVENTS
