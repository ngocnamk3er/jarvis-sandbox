import os

import pytest

os.environ.setdefault("SANDBOX_ROLE", "agent")

from app.agent import runner
from app.core.config import settings


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_DIR", str(tmp_path))
    return tmp_path


async def test_exec_basic():
    out = await runner.exec_command("echo hello && pwd", 10)
    assert out["exit_code"] == 0
    assert "hello" in out["stdout"]
    assert out["timed_out"] is False


async def test_exec_nonzero_exit():
    out = await runner.exec_command("exit 7", 10)
    assert out["exit_code"] == 7


async def test_exec_cwd_is_workspace(workspace):
    out = await runner.exec_command("pwd", 10)
    assert out["stdout"].strip() == str(workspace)


async def test_exec_persists_files_across_calls(workspace):
    await runner.exec_command("echo data > note.txt", 10)
    out = await runner.exec_command("cat note.txt", 10)
    assert out["stdout"].strip() == "data"


async def test_exec_timeout_is_killed():
    out = await runner.exec_command("sleep 5", 1)
    assert out["timed_out"] is True
    assert out["exit_code"] is None


async def test_exec_output_truncation():
    out = await runner.exec_command("yes x | head -c 200000", 20)
    assert "truncated" in out["stdout"]


def test_read_file_ok(workspace):
    (workspace / "report.txt").write_text("body")
    data, mime, name = runner.read_file("report.txt")
    assert data == b"body"
    assert name == "report.txt"


def test_read_file_accepts_workspace_absolute_and_dot_slash(workspace):
    (workspace / "report.txt").write_text("body")
    (workspace / "out").mkdir()
    (workspace / "out" / "chart.png").write_bytes(b"png")
    # the agent/LLM mixes these forms for the same file
    assert runner.read_file("report.txt")[0] == b"body"
    assert runner.read_file("./report.txt")[0] == b"body"
    assert runner.read_file("/workspace/report.txt")[0] == b"body"
    assert runner.read_file("/workspace/out/chart.png")[0] == b"png"


def test_read_file_rejects_traversal(workspace):
    with pytest.raises(ValueError):
        runner.read_file("../secret")
    with pytest.raises(ValueError):
        runner.read_file("/etc/passwd")
    with pytest.raises(ValueError):
        runner.read_file("/workspace/../etc/passwd")


def test_read_file_rejects_symlink_escape(workspace, tmp_path):
    secret = tmp_path.parent / "outside.txt"
    secret.write_text("nope")
    (workspace / "link").symlink_to(secret)
    with pytest.raises(ValueError):
        runner.read_file("link")


def test_read_file_missing(workspace):
    with pytest.raises(FileNotFoundError):
        runner.read_file("nope.txt")


def test_reset_wipes_workspace(workspace):
    (workspace / "a.txt").write_text("x")
    (workspace / "sub").mkdir()
    (workspace / "sub" / "b.txt").write_text("y")
    runner.reset()
    assert list(workspace.iterdir()) == []
