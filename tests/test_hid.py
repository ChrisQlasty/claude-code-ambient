import types
import weakref

from ambient.drivers import hid


def test_hidapi_exit_cleanup_is_skipped() -> None:
    # hidapi registers hid_exit like this; running it at exit can abort the process on macOS.
    module = types.ModuleType("hid")
    calls: list[str] = []
    finalizer = weakref.finalize(module, calls.append, "hid_exit")
    other_module = types.ModuleType("other")
    other = weakref.finalize(other_module, calls.append, "other")

    hid._skip_exit_cleanup(module)

    assert not finalizer.alive
    assert other.alive
    other()
    assert calls == ["other"]
