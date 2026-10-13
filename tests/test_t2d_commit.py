"""The t2d_commit provenance key: the version helper, the startup line and the card.

The helper is tested against a fake ``subprocess.run`` so each git outcome is
exact. The card and startup tests use the same harness as test_card_gulp_block.
"""

import json
import logging
import re
import subprocess
from pathlib import Path

import pytest
import yaml

import casm_t2
from casm_t2.apps import t2d
from test_card_gulp_block import CFG, GULP, arm, synthetic_gulp

SHA = "0123456789ab"
REPO = str(Path(casm_t2.__file__).resolve().parent.parent)
HEAD_CMD = ["git", "-C", REPO, "rev-parse", "--short=12", "HEAD"]
STATUS_CMD = ["git", "-C", REPO, "status", "--porcelain", "--untracked-files=no"]


class FakeGit:
    """subprocess.run stand-in for the two git calls source_version makes.

    Each answer is (returncode, stdout), or an exception instance to raise.
    """

    def __init__(self, head=(0, SHA + "\n"), status=(0, "")):
        self.head = head
        self.status = status
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        answer = self.head if "rev-parse" in args else self.status
        if isinstance(answer, BaseException):
            raise answer
        rc, out = answer
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="")


def test_unmodified_tree_gives_the_short_sha(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(t2d.subprocess, "run", git)
    assert t2d.source_version() == SHA
    (head_args, head_kw), (status_args, status_kw) = git.calls
    assert head_args == HEAD_CMD
    assert status_args == STATUS_CMD
    assert 0 < head_kw["timeout"] <= 10 and 0 < status_kw["timeout"] <= 10


def test_tracked_changes_mark_the_version_dirty(monkeypatch):
    monkeypatch.setattr(t2d.subprocess, "run",
                        FakeGit(status=(0, " M casm_t2/apps/t2d.py\n")))
    assert t2d.source_version() == f"{SHA}+dirty"


@pytest.mark.parametrize("head, status", [
    ((128, "fatal: not a git repository\n"), (0, "")),
    ((0, "\n"), (0, "")),
    ((0, SHA + "\n"), (128, "")),
])
def test_git_error_exit_reports_unknown(monkeypatch, head, status):
    monkeypatch.setattr(t2d.subprocess, "run", FakeGit(head=head, status=status))
    assert t2d.source_version() == "unknown"


@pytest.mark.parametrize("stage", ["head", "status"])
@pytest.mark.parametrize("exc", [
    FileNotFoundError("git"),
    subprocess.TimeoutExpired(["git"], 5.0),
])
def test_git_that_cannot_run_reports_unknown(monkeypatch, stage, exc):
    monkeypatch.setattr(t2d.subprocess, "run", FakeGit(**{stage: exc}))
    assert t2d.source_version() == "unknown"


def test_real_checkout_gives_a_well_formed_version():
    assert re.fullmatch(r"[0-9a-f]{12}(\+dirty)?|unknown", t2d.source_version())


def test_version_is_read_once_and_stamped_on_every_card(daemon, ingest,
                                                        monkeypatch, tmp_path):
    calls = []

    def once():
        calls.append(1)
        return f"{SHA}+dirty"

    monkeypatch.setattr(t2d, "source_version", once)
    d = daemon(**CFG)
    spool = arm(d, monkeypatch, tmp_path)
    for seed in range(2):
        ingest(d, synthetic_gulp(seed=seed), gulp=GULP + seed)

    cards = [json.loads(p.read_text()) for p in sorted(spool.glob("*.json"))]
    assert len(cards) == 2
    assert all(card["t2d_commit"] == f"{SHA}+dirty" for card in cards)
    assert len(calls) == 1


def test_startup_line_names_the_commit(monkeypatch, tmp_path, caplog):
    cfg_path = tmp_path / "t2d.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "db": str(tmp_path / "t2.sqlite"),
        "weights_registry": str(tmp_path / "registry")}))

    async def no_serve(self):
        return None

    monkeypatch.setattr(t2d.logsetup, "setup", lambda logfile: None)
    monkeypatch.setattr(t2d.T2Daemon, "serve", no_serve)
    monkeypatch.setattr(t2d, "source_version", lambda: f"{SHA}+dirty")
    monkeypatch.setattr("sys.argv", ["t2d", str(cfg_path), "--log-file", ""])
    caplog.set_level(logging.INFO, logger="t2d")

    t2d.main()

    starts = [r.getMessage() for r in caplog.records
              if r.getMessage().startswith("t2d starting")]
    assert len(starts) == 1
    assert f"t2d_commit={SHA}+dirty" in starts[0]
