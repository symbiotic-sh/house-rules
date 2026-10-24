#!/usr/bin/env python3
"""Synthetic homes and local Git remotes; no network or installed-home writes."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/sync.py"
SPEC = importlib.util.spec_from_file_location("sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
context = sync.session_context



# The always-load rule files the fixture index lists (the real index lists the same seven).
RULES = ("core", "outcome", "delivery", "writing", "git", "priority-labels", "session-writing")
# Python 3.9 and 3.10 cannot read Kimi's config.toml: sync.py reports that, and only the test of that report runs.
NEEDS_TOML = unittest.skipIf(sync.tomllib is None, "Python 3.11 or later reads Kimi's config.toml")

class SyncFixture:
    """Shared setup and assertions for independent test classes."""

    seed_directory = None

    @classmethod
    def setUpClass(cls):
        if SyncFixture.seed_directory is not None:
            cls.seed = Path(SyncFixture.seed_directory.name)
            return
        temporary = tempfile.TemporaryDirectory()
        SyncFixture.seed_directory = temporary
        cls.seed = Path(temporary.name)
        helper = cls()
        helper.base = cls.seed
        with mock.patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test Author", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }):
            helper.build_fixture()

    def build_fixture(self):
        self.origin = self.base / "origin"
        self.origin.mkdir()
        self.git(self.origin, "init", "-b", "main")
        self.write(self.origin / ".gitignore", "custom/\n")
        shutil.copyfile(REPO / "INSTALL-AGENTS.md", self.origin / "INSTALL-AGENTS.md")
        self.write(self.origin / "skills/sample/SKILL.md", "# Sample\n")
        self.write(self.origin / "skills/operator-writing/SKILL.md", "# Operator writing\n")
        (self.origin / "scripts").mkdir()
        shutil.copyfile(REPO / "scripts/session_context.py", self.origin / "scripts/session_context.py")
        self.write(self.origin / "INDEX.md", "# Index\n\n## Always load\n\n" + "".join(
            f"- [{name}](rules/{name}.md)\n" for name in RULES) + "\n## Load when needed\n")
        self.write(self.origin / "ruleset.json", json.dumps({"version": 1, "context": [
            *(f"rules/{name}.md" for name in RULES), "skills/operator-writing/SKILL.md"]}))
        for name in RULES:
            self.write(self.origin / f"rules/{name}.md", f"# {name} rules\n")
        self.commit(self.origin, [".gitignore", "INSTALL-AGENTS.md", "INDEX.md", "ruleset.json", "skills/sample/SKILL.md",
                                  "skills/operator-writing/SKILL.md", "scripts/session_context.py",
                                  *(f"rules/{name}.md" for name in RULES)])
        self.root = self.base / "House Rules checkout"
        self.git(self.base, "clone", str(self.origin), str(self.root))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="house-rules-sync-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test Author", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "KIMI_CODE_HOME"):
            os.environ.pop(variable, None)
        self.origin = self.base / "origin"
        self.root = self.base / "House Rules checkout"
        for name in ("origin", "House Rules checkout"):
            shutil.copytree(self.seed / name, self.base / name)
        config = self.root / ".git/config"
        config.write_text(config.read_text().replace(str(self.seed / "origin"), str(self.origin)))
        self.home = self.base / "user"
        self.home.mkdir()
        self.claude = self.home / ".claude"
        self.codex = self.home / ".codex"
        self.kimi = self.home / ".kimi-code"
        self.claude.mkdir()
        self.kimi.mkdir()
        self.write(self.codex / "config.toml", "# fixture\n")
        patch = mock.patch.object(Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)
        if sync.tomllib is None:
            hook_files = mock.patch.dict(sync.KIMI_HOOK_FILES)
            hook_files.start()
            self.addCleanup(hook_files.stop)
            del sync.KIMI_HOOK_FILES["KIMI_CODE_HOME"]

    def git(self, root, *args):
        return subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, root, paths):
        self.git(root, "add", "--", *paths)
        self.git(root, "commit", "-m", "Fixture change")

    def invoke(self, *extra, root=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sync.main(["--root", str(root or self.root), *extra])
        return code, out.getvalue(), err.getvalue()

    def receipt(self, data):
        self.write(self.root / "custom/installations/fixture.json", json.dumps(data))

    def link(self, path, target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target, target_is_directory=True)

    def assert_conflicts(self, entries):
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        for entry in entries:
            self.assertIn(str(entry), out)
            self.assertTrue(sync.present(entry))

    def changed_during_fetch(self, mutation):
        self.write(self.origin / "remote", "fixture")
        self.commit(self.origin, ["remote"])
        real_git = sync.git

        def git(root, *args):
            result = real_git(root, *args)
            if "fetch" in args:
                if mutation == "dirty":
                    self.write(self.root / "dirty", "fixture")
                elif mutation == "branch":
                    self.git(self.root, "checkout", "-b", "other")
                else:
                    self.git(self.root, "commit", "--allow-empty", "-m", "Concurrent edit")
            return result

        with mock.patch.object(sync, "git", side_effect=git) as calls:
            code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn("changed during update", out)
        self.assertFalse(any(call.args[1] in ("pull", "merge") for call in calls.call_args_list))

    def install_fixture(self):
        for key, home in sync.homes(self.root, self.home):
            instruction = sync.instruction_file(key, home)
            self.write(instruction, sync.template(self.root) + "\n")
        for folder in (self.claude / "skills", self.home / ".agents/skills"):
            for name in ("sample", "operator-writing"):
                path = folder / name
                if not path.is_symlink():
                    self.link(path, self.root / "skills" / name)
        for path, product in ((self.claude / "settings.json", "claude"), (self.codex / "hooks.json", "codex")):
            self.register_hook(path, product)
        self.register_kimi_hook()

    def register_kimi_hook(self, root=None):
        """Kimi's config.toml as INSTALL-AGENTS.md §3 describes it, beside unrelated settings and hooks."""
        self.write(self.kimi / "config.toml", 'default_model = "fixture"\n\n[[hooks]]\nevent = "Stop"\n'
                   'command = "/bin/echo unrelated"\n\n' + context.kimi_toml(sys.executable, root or self.root))

    def register_hook(self, path, product, python=sys.executable, root=None, extra=()):
        """Write the hook file INSTALL-AGENTS.md §3 describes, beside unrelated hooks."""
        entry = context.hooks(product, python, root or self.root)
        other = {"matcher": "*", "hooks": [{"type": "command", "command": "/bin/echo unrelated"}]}
        self.write(path, json.dumps({"model": "fixture", "hooks": {
            "SessionStart": [other, entry, *extra], "Stop": [other]}}, indent=2))


class SyncTests(SyncFixture, unittest.TestCase):
    def test_removed_fix_option_is_rejected_without_installation_writes(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                sync.main(["--root", str(self.root), "--fix"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertFalse((self.claude / "CLAUDE.md").exists())
        self.assertFalse((self.root / "custom").exists())

    def test_default_run_updates_then_reports_new_skill_without_installing(self):
        self.install_fixture()
        self.write(self.origin / "skills/new/SKILL.md", "# New\n")
        self.commit(self.origin, ["skills/new/SKILL.md"])
        with mock.patch.object(sync, "git", wraps=sync.git) as calls:
            code, out, err = self.invoke()
        revision = self.git(self.origin, "rev-parse", "HEAD")
        self.assertTrue(any(call.args[1:] == ("merge", "--ff-only", "--no-overwrite-ignore", revision)
                            for call in calls.call_args_list))
        self.assertEqual(sum("fetch" in call.args for call in calls.call_args_list), 1)
        self.assertFalse(any("pull" in call.args for call in calls.call_args_list))
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"),
                         self.git(self.origin, "rev-parse", "HEAD"))
        for folder in (self.claude / "skills", self.home / ".agents/skills"):
            self.assertIn(str(folder / "new"), out)
            self.assertFalse((folder / "new").exists())
        self.assertFalse((self.root / "custom").exists())

    def test_missing_rule_files_are_reported_without_installation_writes(self):
        self.install_fixture()
        for name in RULES:
            with self.subTest(name=name):
                path = self.root / f"rules/{name}.md"
                text = path.read_text()
                path.unlink()
                code, out, err = self.invoke()
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"missing rule file: {path}", out)
                self.assertFalse(path.exists())
                self.assertEqual(err, "")
                path.write_text(text)

    def test_compaction_wording_is_the_authoritative_template(self):
        self.assertIn("At session start and after every context compaction or reset, read "
                      f"`{self.root}/INDEX.md`.", sync.template(self.root))

    def test_renamed_index_is_required_without_old_index_fallback(self):
        index = self.root / 'INDEX.md'
        self.write(self.root / 'AGENTS.md', index.read_text())
        findings = []
        self.assertEqual(sync.always_load(self.root, findings),
                         [self.root / f'rules/{name}.md' for name in RULES])
        self.assertEqual(findings, [])
        index.unlink()
        self.assertEqual(sync.always_load(self.root, findings), [])
        self.assertEqual(findings, [f'missing index: {index}'])

    def test_later_verification_errors_preserve_earlier_findings(self):
        real_read, real_scan = Path.read_bytes, os.scandir
        for operation in ("read", "scan"):
            with self.subTest(operation=operation):
                def read(path):
                    if operation == "read" and path == self.codex / "AGENTS.md":
                        raise PermissionError("fixture instruction read denied")
                    return real_read(path)

                def scan(path):
                    if operation == "scan" and Path(path) == self.claude / "skills":
                        raise PermissionError("fixture skill scan denied")
                    return real_scan(path)

                self.write(self.codex / "AGENTS.md", "Local rules\n")
                (self.claude / "skills").mkdir(exist_ok=True)
                with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=read), \
                        mock.patch.object(os, "scandir", side_effect=scan):
                    code, out, err = self.invoke()
                self.assertEqual(code, 2, out + err)
                self.assertIn(f"REPORT: missing block: {self.claude / 'CLAUDE.md'}", out)
                self.assertIn("verification incomplete", out)
                self.assertIn("3 homes selected", out)
                self.assertNotIn("homes checked", out)
                self.assertIn(f"{1 if operation == 'read' else 5} findings", out)
                failure = "instruction read" if operation == "read" else "skill scan"
                self.assertIn(f"fixture {failure} denied", err)

    def test_documented_sync_command_quotes_source_paths_with_spaces(self):
        text = (REPO / "INSTALL-AGENTS.md").read_text(encoding="utf-8")
        command = re.search(r"^python3 .*scripts/sync\.py.*$", text, re.M).group()
        root = "/opt/House Rules"
        self.assertEqual(shlex.split(command.replace("<HOUSE_RULES_ROOT>", root), comments=True),
                         ["python3", f"{root}/scripts/sync.py"])


class SyncHomeTests(SyncFixture, unittest.TestCase):
    def test_old_index_blocks_are_stale_in_every_known_home(self):
        self.install_fixture()
        old_block = sync.template(self.root).replace('/INDEX.md', '/AGENTS.md')
        paths = [self.claude / 'CLAUDE.md', self.codex / 'AGENTS.md',
                 self.kimi / 'AGENTS.md', self.codex / 'AGENTS.override.md']
        for path in paths:
            with self.subTest(path=path):
                self.write(path, old_block)
                findings, selected = [], []
                sync.verify(self.root, self.home, findings, selected)
                self.assertEqual(findings, [f'stale block: {path}'])
                self.assertEqual(path.read_text(), old_block)
                if path.name == 'AGENTS.override.md':
                    path.unlink()
                else:
                    self.write(path, sync.template(self.root))

    def test_documented_windows_homes_preserve_backslashes(self):
        text = (REPO / "INSTALL-AGENTS.md").read_text(encoding="utf-8")
        values = [r"C:\Users\example\.codex-extra", r"\\server\share\.codex-extra"]
        for value in values:
            with self.subTest(value=value):
                line = f"CODEX_HOME='{value}'"
                self.assertTrue(line in text, f"missing quoted example: {line}")
                self.assertEqual(shlex.split(line, comments=True), [f"CODEX_HOME={value}"])

    def test_bad_configuration_and_invalid_codex_home_fail_visibly(self):
        for text in ("UNKNOWN=/example\n", 'CODEX_HOME="relative"\n', "CODEX_HOME=$(false)\n"):
            with self.subTest(text=text):
                self.write(self.root / "custom/sync.env", text)
                code, _, err = self.invoke()
                self.assertEqual(code, 2)
                self.assertIn("ERROR:", err)
        (self.root / "custom/sync.env").unlink()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "absent")}):
            code, _, err = self.invoke()
            self.assertEqual(code, 2)
            self.assertIn("CODEX_HOME", err)
        (self.codex / "config.toml").unlink()
        self.assertIn("not a Codex home", self.invoke()[2])

    def test_malformed_duplicate_reversed_and_mixed_markers_are_not_changed(self):
        path = self.claude / "CLAUDE.md"
        cases = ["<!-- forge:begin -->", "<!-- forge:end --><!-- forge:begin -->",
                 "<!-- forge:begin --><!-- house-rules:end -->",
                 sync.template(self.root) + sync.template(self.root)]
        for text in cases:
            with self.subTest(text=text):
                self.write(path, text)
                code, out, err = self.invoke()
                self.assertNotEqual(code, 0)
                self.assertIn("malformed or multiple", out)
                self.assertEqual(path.read_text(), text)

    def test_unrelated_files_directories_and_symlinks_are_never_overwritten(self):
        entries = [self.claude / "skills/sample", self.home / ".agents/skills/sample",
                   self.codex / "skills/sample", self.kimi / "skills/sample"]
        self.write(entries[0], "Foreign file\n")
        self.write(entries[1] / "SKILL.md", "Foreign directory\n")
        self.link(entries[2], self.home / "unrelated")
        self.link(entries[3], self.root / "skills/different-name")
        self.assert_conflicts(entries)
        self.assertEqual(entries[0].read_text(), "Foreign file\n")
        self.assertEqual((entries[1] / "SKILL.md").read_text(), "Foreign directory\n")
        self.assertEqual(os.readlink(entries[2]), str(self.home / "unrelated"))
        self.assertEqual(os.readlink(entries[3]), str(self.root / "skills/different-name"))

    def test_written_link_target_determines_ownership(self):
        path = self.claude / "skills/sample"
        target = self.root / "skills/sample"
        self.link(path, os.path.relpath(target, path.parent))
        self.assertEqual(path.resolve(), target)
        self.assertFalse(sync.owned_link(path, {str(self.root)}))
        self.assert_conflicts([path])

    def test_skill_folder_alias_into_codex_system_is_rejected(self):
        self.install_fixture()
        system = self.codex / "skills/.system"
        self.write(system / "marker", "Protected\n")
        folder = self.claude / "skills"
        folder.rename(self.claude / "original-skills")
        folder.symlink_to(system, target_is_directory=True)
        code, _, err = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("protected", err)
        self.assertFalse(sync.present(system / "sample"))
        self.assertEqual((system / "marker").read_text(), "Protected\n")

    def test_named_home_instruction_duplicates_are_reported(self):
        self.install_fixture()
        for name in ("AGENTS.md", "CLAUDE.md"):
            with self.subTest(name=name):
                path = self.home / name
                self.write(path, "Local rules\n<!-- forge:begin -->old<!-- forge:end -->")
                before = path.read_bytes()
                code, out, err = self.invoke()
                self.assertEqual(code, 1, out + err)
                self.assertIn("duplicate", out)
                self.assertIn(str(path), out)
                self.assertEqual(path.read_bytes(), before)
                path.unlink()

    def test_broken_default_homes_are_visible(self):
        self.claude.rmdir()
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                if dangling:
                    self.link(self.claude, self.home / "absent")
                else:
                    self.write(self.claude, "not a directory")
                try:
                    code, _, err = self.invoke()
                    self.assertEqual(code, 2, err)
                    self.assertIn(str(self.claude), err)
                finally:
                    self.claude.unlink()

    def test_missing_links_stale_and_missing_blocks_are_reported_without_writes(self):
        original = "Operator rules\n<!-- house-rules:begin -->\nstale\n<!-- house-rules:end -->\nTail\n"
        self.write(self.claude / "CLAUDE.md", original)
        self.write(self.codex / "AGENTS.md", "Codex rules\n")
        before = sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*"))
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        for reason in ("stale block", "missing block", "missing or stale skill link"):
            self.assertIn(reason, out)
        self.assertFalse((self.root / "custom").exists())
        self.assertEqual(before, sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*")))
        self.assertEqual((self.claude / "CLAUDE.md").read_text(), original)
        self.assertEqual((self.codex / "AGENTS.md").read_text(), "Codex rules\n")

    def test_manual_installation_is_clean_on_repeated_runs_without_receipts(self):
        self.install_fixture()
        for _ in range(2):
            code, out, err = self.invoke()
            self.assertEqual(code, 0, out + err)
            self.assertIn("3 homes checked; 0 findings", out)
            self.assertFalse((self.root / "custom").exists())

    def test_extra_homes_are_only_checked_when_configured(self):
        self.install_fixture()
        extra = self.home / "extra codex"
        self.write(extra / "config.toml", "# fixture\n")
        self.assertEqual(self.invoke()[0], 0)
        self.write(self.root / "custom/sync.env", f'CODEX_HOME="{extra}" # extra\n')
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn(str(extra / "AGENTS.md"), out)
        self.assertIn("4 homes checked", out)
        self.assertIn(f"missing session context hook: {extra / 'hooks.json'}", out)
        self.assertFalse((extra / "AGENTS.md").exists())
        self.write(extra / "AGENTS.md", sync.template(self.root))
        self.register_hook(extra / "hooks.json", "codex")
        self.assertEqual(self.invoke()[0], 0)

    def test_extra_claude_and_kimi_homes_and_environment_defaults(self):
        extra_claude, extra_kimi = self.home / "claude extra", self.home / "kimi extra"
        extra_claude.mkdir()
        extra_kimi.mkdir()
        self.write(self.root / "custom/sync.env",
                   f'CLAUDE_CONFIG_DIR="{extra_claude}"\nKIMI_CODE_HOME="{extra_kimi}"\n')
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(extra_claude)}):
            code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        for path in (extra_claude / "CLAUDE.md", extra_claude / "skills/sample",
                     extra_kimi / "AGENTS.md"):
            self.assertIn(str(path), out)
            self.assertFalse(path.exists())
        self.assertNotIn(str(self.claude), out)

    def test_override_and_instruction_symlink_are_checked_without_writes(self):
        self.install_fixture()
        target = self.home / "instructions.md"
        self.write(target, "Local rules\n")
        (self.codex / "AGENTS.override.md").symlink_to(target)
        self.write(self.codex / "AGENTS.md", "Inactive rules\n")
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn(str(self.codex / "AGENTS.override.md"), out)
        self.assertNotIn(str(self.codex / "AGENTS.md"), out)
        self.assertTrue((self.codex / "AGENTS.override.md").is_symlink())
        self.assertEqual(target.read_text(), "Local rules\n")
        self.assertEqual((self.codex / "AGENTS.md").read_text(), "Inactive rules\n")
        self.write(target, "Local rules\n" + sync.template(self.root))
        self.assertEqual(self.invoke()[0], 0)

    def test_shared_instruction_target_is_reported_for_every_home(self):
        self.install_fixture()
        target, alias = self.codex / "AGENTS.md", self.kimi / "AGENTS.md"
        alias.unlink()
        alias.symlink_to(target)
        self.write(target, "Shared rules\n")
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        for path in (target, alias):
            self.assertIn(f"missing block: {path}", out)
        self.assertEqual(target.read_text(), "Shared rules\n")
        self.assertTrue(alias.is_symlink())

    def test_legacy_markers_are_reported_without_changing_surrounding_bytes(self):
        for family in ("forge", "groundwork"):
            with self.subTest(family=family):
                path = self.claude / "CLAUDE.md"
                original = f"Before\r\n<!-- {family}:begin -->\r\nold\r\n<!-- {family}:end -->\r\nAfter\r\n"
                path.write_bytes(original.encode())
                path.chmod(0o640)
                code, out, err = self.invoke()
                self.assertEqual(code, 1, err)
                self.assertIn(f"stale block: {path}", out)
                self.assertEqual(path.read_bytes(), original.encode())
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_owned_removed_and_shadowing_links_are_reported_and_system_is_excluded(self):
        self.install_fixture()
        removed = self.home / ".agents/skills/removed"
        shadow = self.codex / "skills/sample"
        kimi_shadow = self.kimi / "skills/sample"
        self.link(removed, self.root / "skills/removed")
        self.link(shadow, self.root / "skills/sample")
        self.link(kimi_shadow, self.root / "skills/sample")
        system = self.codex / "skills/.system"
        self.write(system / "sample/SKILL.md", "Protected\n")
        self.link(system / "removed", self.root / "skills/removed")
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn(f"removed skill: {removed}", out)
        for path in (shadow, kimi_shadow):
            self.assertIn(f"shadowing skill: {path}", out)
        for path in (removed, shadow, kimi_shadow, system / "removed"):
            self.assertTrue(path.is_symlink())
        self.assertEqual((system / "sample/SKILL.md").read_text(), "Protected\n")
        self.assertNotIn(str(system), out)

    def test_prior_receipt_roots_identify_owned_stale_and_removed_links(self):
        old = self.base / "previous-source"
        stale, removed = self.claude / "skills/sample", self.claude / "skills/removed"
        self.link(stale, old / "skills/sample")
        self.link(removed, old / "skills/removed")
        self.receipt({"source_root": str(old)})
        receipt = self.root / "custom/installations/fixture.json"
        before = receipt.read_bytes()
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn(f"missing or stale skill link: {stale}", out)
        self.assertIn(f"removed skill: {removed}", out)
        self.assertEqual(os.readlink(stale), str(old / "skills/sample"))
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse((self.root / "custom/backups").exists())

    def test_alias_skill_folders_are_checked_for_both_scan_roles(self):
        self.install_fixture()
        folder = self.codex / "skills"
        folder.symlink_to(self.home / ".agents/skills", target_is_directory=True)
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn(f"shadowing skill: {folder / 'sample'}", out)
        self.assertTrue((self.home / ".agents/skills/sample").is_symlink())

    def test_stale_written_link_through_source_alias_is_reported(self):
        self.install_fixture()
        old = self.base / "old-source"
        self.link(old, self.root)
        path = self.claude / "skills/sample"
        path.unlink()
        self.link(path, old / "skills/sample")
        self.receipt({"previous_roots": [str(old)]})
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn(f"missing or stale skill link: {path}", out)
        self.assertEqual(os.readlink(path), str(old / "skills/sample"))

    def test_receipt_and_skill_scan_errors_reach_caller(self):
        directory = self.root / "custom/installations"
        directory.mkdir(parents=True)
        real_scan = os.scandir
        for denied in (directory, self.root / "skills", self.claude / "skills"):
            denied.mkdir(exist_ok=True)
            with self.subTest(denied=denied):
                def scan(path):
                    if Path(path) == denied:
                        raise PermissionError("fixture scan denied")
                    return real_scan(path)
                with mock.patch.object(os, "scandir", side_effect=scan):
                    code, _, err = self.invoke()
                self.assertEqual(code, 2, err)
                self.assertIn("fixture scan denied", err)

    def test_malformed_receipt_roots_fail_visibly(self):
        for data in ([], {"previous_roots": None}, {"previous_roots": "not a list"},
                     {"source_root": "relative"}, {"previous_roots": [1]}):
            with self.subTest(data=data):
                self.receipt(data)
                code, _, err = self.invoke()
                self.assertEqual(code, 2, err)
                self.assertIn("fixture.json", err)

    def test_dangling_instruction_link_is_reported_without_replacing_it(self):
        path = self.codex / "AGENTS.override.md"
        self.link(path, self.home / "missing-target")
        code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn(f"instruction symlink has no regular target: {path}", out)
        self.assertTrue(path.is_symlink())


class SyncHookTests(SyncFixture, unittest.TestCase):
    @NEEDS_TOML
    def test_missing_session_hooks_are_reported_for_claude_codex_and_kimi(self):
        self.install_fixture()
        before = [self.claude / "settings.json", self.codex / "hooks.json", self.kimi / "config.toml"]
        for path in before:
            path.unlink()
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        for path in before:
            self.assertIn(f"REPORT: missing session context hook: {path}", out)
            self.assertFalse(path.exists())

    @NEEDS_TOML
    def test_kimi_config_without_our_hooks_is_a_missing_hook_and_stays_unchanged(self):
        self.install_fixture()
        path = self.kimi / "config.toml"
        original = ('[[hooks]]\nevent = "UserPromptSubmit"\n'
                    'command = "python3 /elsewhere/house-rules-context.py prompt"\n')
        self.write(path, original)
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"REPORT: missing session context hook: {path}", out)
        self.assertEqual(path.read_text(), original)

    @NEEDS_TOML
    def test_stale_kimi_hooks_are_reported_without_writes(self):
        path = self.kimi / "config.toml"
        prompt = context.kimi_toml(sys.executable, self.root)
        cases = {
            "matcher added": prompt.replace("timeout", 'matcher = "x"\ntimeout'),
            "other event": prompt.replace("UserPromptSubmit", "SessionStart"),
            "interpreter missing": context.kimi_toml(str(self.base / "no-python"), self.root),
            "relative interpreter": context.kimi_toml("python3", self.root),
            "non-Python program": context.kimi_toml("/usr/bin/true", self.root),
            "registered twice": prompt + "\n" + prompt,
            "second table with another mode": prompt + "\n" + prompt.replace("UserPromptSubmit", "PreCompact")
                                                                   .replace("kimi prompt", "kimi compact"),
        }
        for name, hooks in cases.items():
            with self.subTest(name=name):
                self.install_fixture()
                self.write(path, hooks)
                code, out, err = self.invoke()
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"REPORT: stale session context hook: {path}", out)
                self.assertEqual(path.read_text(), hooks)

    @NEEDS_TOML
    def test_unreadable_kimi_config_is_reported_and_other_homes_are_still_checked(self):
        self.install_fixture()
        path = self.kimi / "config.toml"
        self.write(path, "[[hooks]\nbroken")
        (self.claude / "settings.json").unlink()
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertRegex(out, rf"REPORT: unreadable Kimi config, session context hook not checked: {re.escape(str(path))}: ")
        self.assertIn(f"REPORT: missing session context hook: {self.claude / 'settings.json'}", out)

    def test_kimi_check_without_a_toml_reader_is_a_visible_finding(self):
        self.install_fixture()
        with mock.patch.object(sync, "tomllib", None), \
                mock.patch.dict(sync.KIMI_HOOK_FILES, {"KIMI_CODE_HOME": "config.toml"}):
            code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"REPORT: Kimi session context hook not checked: Python {sys.version_info[0]}."
                      f"{sys.version_info[1]} has no TOML reader; run sync with Python 3.11 or later: "
                      f"{self.kimi / 'config.toml'}", out)

    def test_other_hooks_without_ours_are_a_missing_hook_and_stay_unchanged(self):
        self.install_fixture()
        path = self.claude / "settings.json"
        original = json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "python3 /elsewhere/house-rules-context.py session 1"}]}]}})
        self.write(path, original)
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"missing session context hook: {path}", out)
        self.assertEqual(path.read_text(), original)

    def test_stale_session_hooks_are_reported_without_writes(self):
        script = str(self.root / "scripts/session_context.py")
        fewer = context.hooks("claude", sys.executable, self.root)
        fewer["hooks"].pop()
        moved = {"matcher": "startup", **context.hooks("claude", sys.executable, self.root)}
        cases = {
            "fewer parts than slots": (self.claude / "settings.json", "claude", {"entry": fewer}),
            "matcher added": (self.claude / "settings.json", "claude", {"entry": moved}),
            "interpreter missing": (self.codex / "hooks.json", "codex", {"python": str(self.base / "missing/python3")}),
            "relative interpreter": (self.codex / "hooks.json", "codex", {"python": "python3"}),
            "registered twice": (self.codex / "hooks.json", "codex", {
                "extra": [context.hooks("codex", sys.executable, self.root)]}),
        }
        for name, (path, product, change) in cases.items():
            with self.subTest(name=name):
                self.install_fixture()
                if "entry" in change:
                    self.write(path, json.dumps({"hooks": {"SessionStart": [change["entry"]]}}))
                else:
                    self.register_hook(path, product, change.get("python", sys.executable),
                                       extra=change.get("extra", ()))
                original = path.read_text()
                code, out, err = self.invoke()
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"REPORT: stale session context hook: {path}", out)
                self.assertEqual(path.read_text(), original)
                self.assertIn(script, original)

    def test_hook_for_a_previous_root_is_stale(self):
        previous = self.base / "old root"
        self.receipt({"source_root": str(self.root), "previous_roots": [str(previous)]})
        self.install_fixture()
        path = self.claude / "settings.json"
        self.register_hook(path, "claude", root=previous)
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"stale session context hook: {path}", out)

    def test_rules_longer_than_the_registered_slots_are_reported(self):
        self.install_fixture()
        path = self.root / "scripts/session_context.py"
        path.write_text(path.read_text().replace("SLOTS = 12 ", "SLOTS = 1 ").replace("CAP = 10_000", "CAP = 1_500"))
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertRegex(out, r"REPORT: session context needs [2-9] parts; the hook registers 1\n")

    def test_section_too_long_for_one_part_is_reported(self):
        self.install_fixture()
        self.write(self.root / "rules/core.md", "# core rules\n\n## Long\n\n" + "word " * 3000 + "\n")
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn('REPORT: session context section too long for one part: rules/core.md, heading "Long"', out)

    def test_unparsable_unrelated_hook_commands_do_not_stop_the_report(self):
        self.install_fixture()
        path = self.claude / "settings.json"
        mention = f"/usr/bin/test -f {shlex.quote(str(self.root / 'scripts/session_context.py'))}"
        broken = [{"hooks": 1}, {"hooks": None}, {"type": "command"},
                  {"hooks": [{"type": "command", "command": []}, {"type": "command", "command": "echo 'open"},
                             "not a handler", {"type": "command"}, {"type": "command", "command": mention}]},
                  "not an entry"]
        self.register_hook(path, "claude", extra=broken)
        code, out, err = self.invoke()
        self.assertEqual(code, 0, out + err)
        self.assertIn("3 homes checked; 0 findings", out)

    def test_registered_command_prints_part_one_from_the_installed_root(self):
        self.install_fixture()
        handler = json.loads((self.claude / "settings.json").read_text())["hooks"]["SessionStart"][1]["hooks"][0]
        result = subprocess.run(handler["command"], shell=True, cwd=self.base, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, check=True, timeout=handler["timeout"])
        self.assertEqual(result.stderr, "")
        self.assertRegex(result.stdout, r"^===== House Rules part 1 of 1 \(revision [0-9a-f]{12}, text [0-9a-f]{12}\); "
                                        r"starts at INDEX\.md, heading \"Index\" =====\n")
        self.assertIn("===== House Rules file rules/core.md =====\n# core rules", result.stdout)
        self.assertIn(f"HOUSE RULES HOOK ERROR: this computer's local index {self.root / 'custom/INDEX.md'} "
                      "is missing", result.stdout)

    @NEEDS_TOML
    def test_registered_kimi_command_prints_the_rules_once_per_session(self):
        self.install_fixture()
        hooks = sync.tomllib.loads((self.kimi / "config.toml").read_text())["hooks"]
        prompt = next(hook for hook in hooks if hook["event"] == "UserPromptSubmit")

        def run(session):
            data = {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(self.base)}
            return subprocess.run(prompt["command"], shell=True, cwd=self.base, stdin=None, input=json.dumps(data),
                                  env={**os.environ, "KIMI_CODE_HOME": str(self.kimi)},
                                  capture_output=True, text=True, check=True, timeout=prompt["timeout"])

        first = run("session_fixture")
        self.assertEqual(first.stderr, "")
        self.assertRegex(first.stdout, r"^===== House Rules part 1 of 1 \(revision [0-9a-f]{12}, text [0-9a-f]{12}\); ")
        self.assertIn("===== House Rules file rules/core.md =====\n# core rules", first.stdout)
        self.assertEqual(run("session_fixture").stdout, "")
        self.assertEqual(run("session_other").stdout, first.stdout)


class InstallHookTests(SyncFixture, unittest.TestCase):
    """sync.py --install-hooks writes only hook files: back up, drop old House Rules entries, add the current one."""

    def setUp(self):
        super().setUp()
        self.install_fixture()
        self.settings, self.codex_hooks = self.claude / "settings.json", self.codex / "hooks.json"
        self.legacy = str(self.root / "custom/hooks/old-context.py")
        self.unrelated = {"type": "command", "command": "/bin/echo unrelated"}

    def read(self, path):
        return json.loads(path.read_text())

    def backups(self, path):
        return sorted(path.parent.glob(path.name + ".house-rules-backup-*"))

    def test_registers_the_hook_beside_other_settings_and_backs_up_the_file(self):
        original = json.dumps({"model": "fixture", "hooks": {"SessionStart": [{"hooks": [self.unrelated]}],
                                                              "Stop": [{"hooks": [self.unrelated]}]}})
        self.write(self.settings, original)
        self.codex_hooks.unlink()
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"REGISTERED: session context hook in {self.settings}", out)
        self.assertIn("3 homes checked; 0 findings", out)
        settings = self.read(self.settings)
        self.assertEqual(settings["model"], "fixture")
        self.assertEqual(settings["hooks"]["Stop"], [{"hooks": [self.unrelated]}])
        self.assertEqual(settings["hooks"]["SessionStart"],
                         [{"hooks": [self.unrelated]}, context.hooks("claude", sys.executable, self.root)])
        self.assertEqual([path.read_text() for path in self.backups(self.settings)], [original])
        self.assertEqual(self.read(self.codex_hooks)["hooks"]["SessionStart"],
                         [context.hooks("codex", sys.executable, self.root)])
        self.assertEqual(self.backups(self.codex_hooks), [])
        self.assertFalse((self.kimi / "settings.json").exists() or (self.kimi / "hooks.json").exists())

    def test_hooks_through_the_house_rules_command_replace_the_interpreter_form(self):
        command = self.base / "bin" / "house-rules"
        self.write(command, "#!/bin/sh\n")
        command.chmod(0o755)
        self.write(self.settings, json.dumps({"hooks": {"SessionStart": [
            {"hooks": [self.unrelated]}, context.hooks("claude", sys.executable, self.root)]}}))
        code, out, err = self.invoke("--install-hooks", "--command", str(command))
        self.assertEqual(code, 0, out + err)
        for path, product in ((self.settings, "claude"), (self.codex_hooks, "codex")):
            entry = context.hooks(product, house_rules=str(command))
            self.assertTrue(all(handler["command"].startswith(f"{command} exec --hook scripts/session_context.py ")
                                for handler in entry["hooks"]))
            entries = self.read(path)["hooks"]["SessionStart"]
            self.assertEqual(entries[-1], entry)
            self.assertFalse([handler for other in entries[:-1] for handler in other["hooks"]
                              if "session_context.py" in handler["command"]])
        self.assertEqual(self.read(self.settings)["hooks"]["SessionStart"][0], {"hooks": [self.unrelated]})
        code, out, err = self.invoke()
        self.assertEqual(code, 0, out + err)
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read(self.settings)["hooks"]["SessionStart"],
                         [{"hooks": [self.unrelated]}, context.hooks("claude", sys.executable, self.root)])
        self.invoke("--install-hooks", "--command", str(command))
        command.chmod(0o644)  # a command that cannot run delivers no rules
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"REPORT: stale session context hook: {self.settings}", out)
        command.unlink()
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"REPORT: stale session context hook: {self.settings}", out)

    def test_replaces_older_versions_and_hooks_a_receipt_lists_as_replaced(self):
        legacy = [{"type": "command", "command": f"{sys.executable} -I {shlex.quote(self.legacy)} session {n}"}
                  for n in (1, 2)]
        self.receipt({"source_root": str(self.root), "previous_roots": [str(self.base / "old root")],
                      "replaced_hooks": [handler["command"] for handler in legacy]})
        fewer = context.hooks("claude", sys.executable, self.root)
        fewer["hooks"].pop()
        self.write(self.settings, json.dumps({"hooks": {"SessionStart": [
            fewer, context.hooks("claude", "/usr/bin/python3", self.base / "old root"),
            {"matcher": "*", "hooks": [*legacy, self.unrelated]}, {"hooks": legacy}]}}))
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertIn(f"REPORT: replaced House Rules hook still registered: {self.settings}", out)
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read(self.settings)["hooks"]["SessionStart"], [
            {"matcher": "*", "hooks": [self.unrelated]}, context.hooks("claude", sys.executable, self.root)])

    def test_a_registration_without_the_approved_handler_is_replaced_whole(self):
        for path, product in ((self.settings, "claude"), (self.codex_hooks, "codex")):
            older = context.hooks(product, sys.executable, self.root)
            older["hooks"] = [handler for handler in older["hooks"] if " approved" not in handler["command"]]
            self.write(path, json.dumps({"hooks": {"SessionStart": [older]}}))
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        for path, product in ((self.settings, "claude"), (self.codex_hooks, "codex")):
            self.assertEqual(self.read(path)["hooks"]["SessionStart"], [context.hooks(product, sys.executable, self.root)])

    def test_unrelated_entries_with_empty_handler_lists_are_kept(self):
        empty = [{"matcher": "startup", "hooks": []}, {"hooks": []}]
        self.write(self.settings, json.dumps({"hooks": {"SessionStart": empty}}))
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read(self.settings)["hooks"]["SessionStart"],
                         [*empty, context.hooks("claude", sys.executable, self.root)])

    def test_listed_commands_that_the_current_entry_reuses_stay_current(self):
        current = context.hooks("claude", sys.executable, self.root)
        self.receipt({"source_root": str(self.root),
                      "replaced_hooks": [handler["command"] for handler in current["hooks"][:3]]})
        older = {"hooks": current["hooks"][:-1]}
        self.write(self.settings, json.dumps({"hooks": {"SessionStart": [older]}}))
        for _ in range(2):
            code, out, err = self.invoke("--install-hooks")
            self.assertEqual(code, 0, out + err)
            self.assertNotIn("replaced House Rules hook", out)
        self.assertEqual(self.read(self.settings)["hooks"]["SessionStart"], [current])

    def test_an_update_that_changes_the_hook_generator_installs_the_updated_entry(self):
        path = self.origin / "scripts/session_context.py"
        path.write_text(path.read_text().replace("SLOTS = 12 ", "SLOTS = 13 "))
        self.commit(self.origin, ["scripts/session_context.py"])
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.claude_parts(), 13)

    def test_the_selected_checkouts_current_generator_is_used_despite_cached_bytecode(self):
        path = self.root / "scripts/session_context.py"
        spec = importlib.util.spec_from_file_location("fixture_generator", path)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))  # writes timestamp bytecode
        times = path.stat()
        path.write_text(path.read_text().replace("SLOTS = 12 ", "SLOTS = 13 "))  # same size
        os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(self.claude_parts(), 13, out + err)

    def claude_parts(self):
        handlers = self.read(self.settings)["hooks"]["SessionStart"][-1]["hooks"]
        return sum(handler["command"].split()[-2] == "claude" for handler in handlers)

    def test_the_default_root_keeps_the_link_it_was_run_through(self):
        shutil.copyfile(SCRIPT, self.root / "scripts/sync.py")
        alias = self.base / "stable root"
        alias.symlink_to(self.root, target_is_directory=True)
        run = [sys.executable, "-B", str(alias / "scripts/sync.py")]
        environment = {**os.environ, "HOME": str(self.home)}
        installed = subprocess.run([*run, "--install-hooks"], env=environment, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True)
        self.assertIn(f"REGISTERED: session context hook in {self.claude.resolve() / 'settings.json'}",
                      installed.stdout, installed.stdout + installed.stderr)
        command = self.read(self.settings)["hooks"]["SessionStart"][-1]["hooks"][0]["command"]
        self.assertEqual(shlex.split(command)[2], str(alias / "scripts/session_context.py"))
        # Kimi is registered by hand (INSTALL-AGENTS.md §3 step 10), here for the same linked root.
        self.register_kimi_hook(root=alias)
        checked = subprocess.run(run, env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        # Without a TOML reader the Kimi hook is reported as not checked; no other hook may be reported.
        reported = [line for line in checked.stdout.splitlines() if "session context hook" in line and not (
            sync.tomllib is None and "Kimi session context hook not checked" in line)]
        self.assertEqual(reported, [], checked.stdout + checked.stderr)

    def test_a_home_link_swapped_after_the_check_cannot_redirect_writes(self):
        real = self.base / "claude real"
        real.mkdir()
        protected = self.codex / "skills/.system/claude"
        protected.mkdir(parents=True)
        self.write(protected / "settings.json", "{}")
        link = self.base / "claude link"
        link.symlink_to(real, target_is_directory=True)
        real_open = os.open

        def swap(path, flags, *args, **kwargs):  # folders are open and checked; relink before the write
            handle = real_open(path, flags, *args, **kwargs)
            if str(path).startswith(".settings.json."):
                link.unlink()
                link.symlink_to(protected, target_is_directory=True)
            return handle

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(link)}), \
                mock.patch.object(sync.os, "open", side_effect=swap):
            code, out, err = self.invoke("--install-hooks")
        self.assertEqual(os.path.realpath(link), str(protected.resolve()), "the link was swapped")
        self.assertEqual(sorted(path.name for path in protected.iterdir()), ["settings.json"])
        self.assertEqual((protected / "settings.json").read_text(), "{}")
        self.assertEqual(self.read(real / "settings.json")["hooks"]["SessionStart"],
                         [context.hooks("claude", sys.executable, self.root)])

    def test_a_correct_installation_is_not_rewritten(self):
        before = self.settings.read_text()
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("REGISTERED", out)
        self.assertEqual(self.settings.read_text(), before)
        self.assertEqual(self.backups(self.settings), [])

    def test_a_symlinked_root_is_written_as_given_so_moves_of_its_target_keep_working(self):
        alias = self.base / "stable root"
        alias.symlink_to(self.root, target_is_directory=True)
        code, out, err = self.invoke("--install-hooks", root=alias)
        self.assertIn(f"REGISTERED: session context hook in {self.settings}", out)
        commands = [handler["command"] for handler in self.read(self.settings)["hooks"]["SessionStart"][-1]["hooks"]]
        self.assertIn(shlex.quote(str(alias / "scripts/session_context.py")), commands[0])
        self.assertEqual(len(self.read(self.settings)["hooks"]["SessionStart"]), 2)

    def test_unreadable_hook_file_is_an_error_and_stays_unchanged(self):
        self.write(self.settings, "{not json")
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertEqual(self.settings.read_text(), "{not json")
        self.assertEqual(self.backups(self.settings), [])
        self.assertIn(str(self.settings), err)

    def test_files_written_before_a_later_failure_are_reported(self):
        self.settings.unlink()
        self.write(self.codex_hooks, "[]")
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"REGISTERED: session context hook in {self.settings}", out)
        self.assertIn(f"hook file is not a JSON object: {self.codex_hooks}", err)
        self.assertEqual(self.codex_hooks.read_text(), "[]")

    def test_backups_are_owner_only_and_never_replace_an_earlier_backup(self):
        self.settings.chmod(0o644)
        with mock.patch.object(sync.time, "strftime", return_value="20260101T000000"):
            for model in ("first", "second"):
                settings = self.read(self.settings)
                settings["hooks"]["SessionStart"].pop()
                settings["model"] = model
                self.write(self.settings, json.dumps(settings))
                code, out, err = self.invoke("--install-hooks")
                self.assertEqual(code, 0, out + err)
        backups = self.backups(self.settings)
        self.assertEqual([self.read(path)["model"] for path in backups], ["first", "second"])
        self.assertEqual({stat.S_IMODE(path.stat().st_mode) for path in backups}, {0o600})
        self.assertEqual(stat.S_IMODE(self.settings.stat().st_mode), 0o644)

    def test_only_the_printed_form_and_listed_commands_are_house_rules_owned(self):
        script = shlex.quote(str(self.root / "scripts/session_context.py"))
        listed = [f"/usr/bin/python3 -X utf8 {shlex.quote(self.legacy)} session 1", shlex.quote(self.legacy)]
        self.receipt({"source_root": str(self.root), "replaced_hooks": listed})
        kept = [f"/bin/echo -I {script} claude 1", f"/opt/bin/pygmentize -I {script} claude 1",
                f"/usr/bin/test -f {script}", f"/usr/bin/python3 -Icprint(1) {script}",
                f"/usr/bin/python3 -I {script} claude 1 --extra", f"/usr/bin/python3 -I {script}",
                f"/usr/bin/python3 -X utf8 {shlex.quote(self.legacy)} session 2"]
        removed = [*listed, f"/usr/bin/python3.14t -I {script} claude 3", f"/usr/bin/python -I {script} codex",
                   f"/usr/bin/python3 -I {script} approved", f"/usr/bin/python3 -I {script} approved codex"]
        self.write(self.settings, json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": command} for command in kept + removed]}]}}))
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read(self.settings)["hooks"]["SessionStart"], [
            {"hooks": [{"type": "command", "command": command} for command in kept]},
            context.hooks("claude", sys.executable, self.root)])

    def test_no_hook_file_is_written_when_one_destination_is_protected(self):
        protected = self.codex / "skills/.system/claude"
        protected.mkdir(parents=True)
        self.write(protected / "settings.json", "{}")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(protected)}):
            code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn("protected Codex .system content", err)
        self.assertEqual((protected / "settings.json").read_text(), "{}")
        self.assertNotIn("REGISTERED", out)

    def test_a_symlinked_hook_file_in_a_protected_folder_is_not_replaced(self):
        protected = self.codex / "skills/.system/claude"
        protected.mkdir(parents=True)
        outside = self.base / "outside-settings.json"
        self.write(outside, "{}")
        (protected / "settings.json").symlink_to(outside)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(protected)}):
            code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn("protected Codex .system content", err)
        self.assertTrue((protected / "settings.json").is_symlink())
        self.assertEqual(sorted(path.name for path in protected.iterdir()), ["settings.json"])

    def test_a_backup_that_cannot_be_written_is_removed_and_the_file_kept(self):
        settings = self.read(self.settings)
        settings["hooks"]["SessionStart"].pop()
        self.write(self.settings, json.dumps(settings))
        before = self.settings.read_text()
        real = os.fdopen

        def failing(handle, mode="r", *args, **kwargs):
            stream = real(handle, mode, *args, **kwargs)
            if mode == "wb":
                stream.write = mock.Mock(side_effect=OSError("fixture disk full"))
            return stream

        with mock.patch.object(sync.os, "fdopen", side_effect=failing):
            code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn("fixture disk full", err)
        self.assertEqual(self.backups(self.settings), [])
        self.assertEqual(self.settings.read_text(), before)

    def test_an_installer_interpreter_with_any_name_recognizes_its_own_entry(self):
        python = self.base / "bin/interpreter"
        python.parent.mkdir()
        python.symlink_to(sys.executable)
        with mock.patch.object(sync.sys, "executable", str(python)):
            for _ in range(2):
                code, out, err = self.invoke("--install-hooks")
                self.assertEqual(code, 0, out + err)
        entries = self.read(self.settings)["hooks"]["SessionStart"]
        self.assertEqual(entries[1:], [context.hooks("claude", str(python), self.root)])

    def test_a_file_changed_by_another_writer_during_installation_is_not_overwritten(self):
        self.write(self.settings, json.dumps({"model": "before"}))
        real_chmod = os.chmod

        def concurrent(path, mode, *args, **kwargs):  # the staged file is ready; another writer saves now
            self.write(self.settings, json.dumps({"model": "other writer"}))
            return real_chmod(path, mode, *args, **kwargs)

        with mock.patch.object(sync.os, "chmod", side_effect=concurrent):
            code, out, err = self.invoke("--install-hooks")
        settings = self.claude.resolve() / "settings.json"
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"hook file changed during installation; not written: {settings}", err)
        self.assertEqual(self.read(self.settings), {"model": "other writer"})
        self.assertEqual(sorted(p.name for p in self.claude.iterdir() if p.name.startswith(".settings.json.")), [])
        backups = self.backups(self.settings)
        self.assertEqual(len(backups), 1)
        self.assertIn(f"BACKUP: {self.claude.resolve() / backups[0].name} (copy of {settings})", out)

    def test_a_current_entry_followed_by_other_entries_is_not_rewritten(self):
        settings = self.read(self.settings)
        settings["hooks"]["SessionStart"].append({"hooks": [self.unrelated]})
        self.write(self.settings, json.dumps(settings))
        before = self.settings.read_text()
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.settings.read_text(), before)
        self.assertEqual(self.backups(self.settings), [])

    def test_a_failed_cleanup_reports_the_write_failure_too(self):
        settings = self.read(self.settings)
        settings["hooks"]["SessionStart"].pop()
        self.write(self.settings, json.dumps(settings))
        real_fdopen = os.fdopen

        def failing(handle, mode="r", *args, **kwargs):
            stream = real_fdopen(handle, mode, *args, **kwargs)
            if mode == "wb":
                stream.write = mock.Mock(side_effect=OSError("fixture disk full"))
            return stream

        with mock.patch.object(sync.os, "fdopen", side_effect=failing), \
                mock.patch.object(sync.os, "unlink", side_effect=OSError("fixture unlink denied")):
            code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn("fixture disk full", err)
        self.assertIn("could not be removed either: fixture unlink denied", err)

    def test_a_missing_generator_in_the_selected_checkout_writes_nothing(self):
        (self.root / "scripts/session_context.py").unlink()
        before = self.settings.read_text()
        code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn(f"hook generator is missing: {self.root / 'scripts/session_context.py'}", err)
        self.assertEqual(self.settings.read_text(), before)
        self.assertIn(f"REPORT: session context hook runs a missing script: {self.settings}", out)

    def test_a_failed_return_to_the_previous_directory_keeps_the_write_error(self):
        settings = self.read(self.settings)
        settings["hooks"]["SessionStart"].pop()
        self.write(self.settings, json.dumps(settings))
        real_fchdir, calls = os.fchdir, []
        self.addCleanup(os.chdir, os.getcwd())  # the fixture denies the return to it

        def fchdir(handle):
            calls.append(handle)
            if len(calls) == 2:  # the return after the failed write
                raise OSError("fixture return denied")
            return real_fchdir(handle)

        with mock.patch.object(sync.os, "fchdir", side_effect=fchdir), \
                mock.patch.object(sync.os, "replace", side_effect=OSError("fixture replace denied")):
            code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn("fixture replace denied; returning to the previous directory also failed: "
                      "fixture return denied", err)

    def test_a_failed_close_after_a_failed_resolve_keeps_the_resolve_error(self):
        real_close, real_resolve = os.close, Path.resolve
        home = self.claude

        def failing_close(handle):
            real_close(handle)
            raise OSError("fixture close denied")

        def resolve(path, *args, **kwargs):  # only the Claude home folder fails to resolve
            if path == home and kwargs.get("strict"):
                raise OSError("fixture resolve denied")
            return real_resolve(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=resolve), \
                mock.patch.object(sync.os, "close", side_effect=failing_close):
            code, out, err = self.invoke("--install-hooks")
        self.assertEqual(code, 2, out + err)
        self.assertIn("fixture resolve denied; closing hook folders also failed: fixture close denied", err)

    def test_default_run_still_writes_no_hook_file(self):
        self.settings.unlink()
        code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertFalse(self.settings.exists())


class SyncSourceTests(SyncFixture, unittest.TestCase):
    def test_update_preserves_ignored_files_colliding_with_incoming_paths(self):
        path = "custom/INDEX.md"
        self.write(self.root / path, "Machine-local index\n")
        self.write(self.origin / path, "Incoming tracked file\n")
        self.git(self.origin, "add", "--force", "--", path)
        self.git(self.origin, "commit", "-m", "Add colliding path")
        head = self.git(self.root, "rev-parse", "HEAD")
        code, out, err = self.invoke()
        self.assertEqual((self.root / path).read_text(), "Machine-local index\n")
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(code, 2, out + err)
        self.assertIn("ERROR:", err)
        self.assertIn("missing block", out)
        self.assertIn("3 homes checked", out)

    def test_feature_branch_and_detached_head_are_not_pulled(self):
        self.git(self.root, "checkout", "-b", "work")
        self.assertIn("not main", self.invoke()[1])
        self.git(self.root, "checkout", "--detach")
        self.assertIn("detached HEAD", self.invoke()[1])

    def test_git_failure_reaches_caller(self):
        self.git(self.root, "remote", "set-url", "origin", str(self.base / "absent"))
        code, out, err = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("ERROR:", err)
        self.assertIn("missing block", out)
        self.assertIn("3 homes checked", out)
        self.assertFalse((self.claude / "CLAUDE.md").exists())

    def test_hidden_untracked_files_prevent_pull(self):
        self.git(self.root, "config", "status.showUntrackedFiles", "no")
        self.write(self.root / "hidden-untracked", "fixture")
        with mock.patch.object(sync, "git", wraps=sync.git) as calls:
            code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn("dirty", out)
        self.assertFalse(any(call.args[1] in ("pull", "merge") for call in calls.call_args_list))

    def test_rechecks_cleanliness_after_fetch(self):
        self.changed_during_fetch("dirty")

    def test_rechecks_branch_after_fetch(self):
        self.changed_during_fetch("branch")

    def test_rechecks_revision_after_fetch(self):
        self.changed_during_fetch("revision")

    def test_rechecks_before_pull(self):
        self.write(self.origin / "remote", "fixture")
        self.commit(self.origin, ["remote"])
        real_git = sync.git

        def git(root, *args):
            result = real_git(root, *args)
            if "rev-list" in args:
                self.write(self.root / "concurrent-untracked", "fixture")
            return result

        with mock.patch.object(sync, "git", side_effect=git) as calls:
            code, out, err = self.invoke()
        self.assertEqual(code, 1, out + err)
        self.assertFalse(any(call.args[1] in ("pull", "merge") for call in calls.call_args_list))

    def test_source_status_errors_preserve_installed_links(self):
        self.install_fixture()
        real_stat = Path.stat
        real_is_dir, real_is_file = Path.is_dir, Path.is_file
        for denied in (self.root / "skills/sample", self.root / "skills/sample/SKILL.md"):
            with self.subTest(denied=denied):
                def stat(path, *args, **kwargs):
                    if path == denied:
                        raise PermissionError("source status unavailable")
                    return real_stat(path, *args, **kwargs)

                # Model Python 3.14's error-suppressing predicates on older Python too.
                def predicate(method):
                    return lambda path: False if path == denied else method(path)

                with mock.patch.object(Path, "stat", autospec=True, side_effect=stat), \
                        mock.patch.object(Path, "is_dir", autospec=True,
                                          side_effect=predicate(real_is_dir)), \
                        mock.patch.object(Path, "is_file", autospec=True,
                                          side_effect=predicate(real_is_file)):
                    code, _, err = self.invoke()
                self.assertEqual(code, 2, err)
                self.assertIn("source status unavailable", err)
                self.assertTrue((self.claude / "skills/sample").is_symlink())

    def test_report_disables_optional_git_locks(self):
        real_run = subprocess.run

        def run(command, **kwargs):
            if command[0] == "git":
                self.assertTrue("--no-optional-locks" in command or
                                kwargs.get("env", {}).get("GIT_OPTIONAL_LOCKS") == "0")
            return real_run(command, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=run):
            sync.update(self.root)

    def test_dirty_checkout_is_reported_and_never_pulled(self):
        self.write(self.root / "untracked", "dirty\n")
        with mock.patch.object(sync, "git", wraps=sync.git) as calls:
            code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn("dirty", out)
        self.assertFalse(any(call.args[1] in ("pull", "merge") for call in calls.call_args_list))
        self.assertIn("3 homes checked", out)

    def test_diverged_and_ahead_checkouts_are_reported_and_never_pulled(self):
        self.write(self.root / "local", "local\n")
        self.commit(self.root, ["local"])
        self.assertIn("ahead", self.invoke()[1])
        self.write(self.origin / "remote", "remote\n")
        self.commit(self.origin, ["remote"])
        with mock.patch.object(sync, "git", wraps=sync.git) as calls:
            code, out, err = self.invoke()
        self.assertEqual(code, 1, err)
        self.assertIn("diverged", out)
        self.assertFalse(any(call.args[1] in ("pull", "merge") for call in calls.call_args_list))




class SessionContextTests(unittest.TestCase):
    """The hook's text, split into parts, on synthetic House Rules files without Git."""

    LABEL = re.compile(r'===== House Rules part (\d+) of (\d+) \(revision unknown, text ([0-9a-f]{12})\); '
                       r'starts at (\S+), heading "([^"]*)" =====\n')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="house-rules-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.write("INDEX.md", "# Index\n\n## Always load\n\n- [core](rules/core.md)\n- [writing](rules/writing.md)\n"
                               "\n## Load when needed\n")
        sections = "".join(f"## Section {number}\n\n" + f"Rule {number} sentence with several words. " * 40 + "\n\n"
                           for number in range(30))
        self.write("rules/core.md", "# Core rules\n\n" + sections)
        self.write("rules/writing.md", "# Writing rules\n\n```markdown\n## Not a heading\n```\n\n" + sections)
        self.write("skills/operator-writing/SKILL.md", "# Operator writing\n\nWrite plainly.\n")
        self.write("ruleset.json", json.dumps({"version": 1, "context": [
            "rules/core.md", "rules/writing.md", "skills/operator-writing/SKILL.md"]}))
        self.write("custom/INDEX.md", "# Machine-local capability index\n\nLocal policy line.\n")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def rendered(self):
        rev, packed, _ = context.parts(self.root)
        with mock.patch.object(context, "root", return_value=self.root):
            return [context.render(number, rev, packed) for number in range(1, len(packed) + 1)]

    def bodies(self, outputs):
        """Each part's text, exactly, without its label, part 1 introduction and end line."""
        result = []
        for number, output in enumerate(outputs, 1):
            end = f"===== End of House Rules part {number} of {len(outputs)} ====="
            self.assertTrue(output.endswith(end), output[-200:])
            result.append(output[:-len(end)].split("\n", 2 if number == 1 else 1)[-1])
        return result

    def source(self):
        """The injected files, in order, each after its separator line."""
        return "".join(f"\n===== House Rules file {name} =====\n" + (self.root / name).read_text()
                       for name in ("INDEX.md", "rules/core.md", "rules/writing.md",
                                    "skills/operator-writing/SKILL.md", "custom/INDEX.md"))

    def test_parts_stay_under_the_cap_and_split_only_at_file_or_heading_boundaries(self):
        outputs = self.rendered()
        self.assertGreater(len(outputs), 2)
        for number, (output, body) in enumerate(zip(outputs, self.bodies(outputs)), 1):
            with self.subTest(part=number):
                self.assertLess(context.units(output), context.CAP)
                first = body.lstrip("\n").split("\n", 1)[0]
                self.assertRegex(first, r"^(===== House Rules file \S+ =====|#{1,6} \S)")
                self.assertNotRegex(first, "Not a heading")
        self.assertEqual("".join(self.bodies(outputs)), self.source())

    def test_blank_lines_at_a_part_boundary_are_kept(self):
        self.write("rules/core.md", "# Core rules\n\n" + "".join(
            f"## Section {number}\n\n" + "word " * 400 + "\n\n\n" for number in range(10)))
        outputs = self.rendered()
        self.assertGreater(len(outputs), 1)
        self.assertEqual("".join(self.bodies(outputs)), self.source())

    def test_every_part_names_its_number_count_file_and_starting_heading(self):
        outputs = self.rendered()
        for number, output in enumerate(outputs, 1):
            match = self.LABEL.match(output)
            self.assertIsNotNone(match, output[:200])
            self.assertEqual((int(match[1]), int(match[2])), (number, len(outputs)))
            body = self.bodies(outputs)[number - 1].lstrip("\n")
            heading = re.search(r"^#{1,6} (.*)$", body, re.M)[1]
            self.assertEqual(match[5], heading)
            self.assertIn(match[4], ("INDEX.md", "rules/core.md", "rules/writing.md"))
            self.assertEqual(match[3], self.LABEL.match(outputs[0])[3])
        self.assertIn("The parts can arrive in any order: read them in part-number order.", outputs[0])

    def test_custom_index_is_injected_last_and_its_absence_is_a_visible_error(self):
        outputs = self.rendered()
        self.assertIn("===== House Rules file custom/INDEX.md =====\n# Machine-local capability index\n\n"
                      "Local policy line.", outputs[-1])
        (self.root / "custom/INDEX.md").unlink()
        self.assertIn(f"HOUSE RULES HOOK ERROR: this computer's local index {self.root / 'custom/INDEX.md'} "
                      "is missing, so its local policies are not loaded.", self.rendered()[-1])

    def test_missing_rule_file_is_a_visible_error_line(self):
        (self.root / "rules/writing.md").unlink()
        text = "".join(self.rendered())
        self.assertIn(f"HOUSE RULES HOOK ERROR: {self.root / 'rules/writing.md'} is missing.", text)
        self.assertIn("# Operator writing", text)

    def test_section_longer_than_a_part_is_a_visible_error_and_never_split(self):
        self.write("rules/core.md", "# Core rules\n\n## Long section\n\n" + "word " * 3000 + "\n\n## Short\n\nKept.\n")
        _, packed, too_long = context.parts(self.root)
        self.assertEqual(too_long, [("rules/core.md", "Long section")])
        text = "".join(self.rendered())
        self.assertRegex(text, r'HOUSE RULES HOOK ERROR: the section "Long section" is \d+ characters, '
                               r'more than the \d+ one part holds, so it is not in this context\.')
        self.assertNotIn("word word", text)
        self.assertIn("## Short\n\nKept.", text)
        self.assertIn("===== House Rules file rules/core.md =====\n# Core rules", text)

    def test_more_parts_than_slots_is_reported_in_part_one_and_the_last_slot(self):
        rev, packed, _ = context.parts(self.root)
        slots = len(packed) - 1
        with mock.patch.object(context, "SLOTS", slots), mock.patch.object(context, "root", return_value=self.root):
            first, last = context.claude(1), context.claude(slots)
        message = f"HOUSE RULES HOOK ERROR: the rules need {len(packed)} parts but the hook delivers only {slots}"
        self.assertIn(message, first)
        self.assertIn(message, last)
        for output in (first, last):
            self.assertLess(context.units(output), context.CAP)

    def test_codex_receives_every_part_in_order_as_one_context_value(self):
        with mock.patch.object(context, "root", return_value=self.root):
            output = json.loads(context.codex())["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertEqual(output["additionalContext"], "\n\n".join(self.rendered()))
        with mock.patch.object(context, "SLOTS", 1), mock.patch.object(context, "root", return_value=self.root):
            capped = json.loads(context.codex())["hookSpecificOutput"]["additionalContext"]
        self.assertIn("HOUSE RULES HOOK ERROR: the rules need", capped)
        self.assertNotIn("House Rules part 2 of", capped)

    def test_parts_read_after_a_change_name_another_text(self):
        before = self.rendered()
        self.write("custom/INDEX.md", "# Machine-local capability index\n\nChanged policy line.\n")
        after = self.rendered()
        self.assertNotEqual(self.LABEL.match(before[0])[3], self.LABEL.match(after[-1])[3])
        self.assertIn(f"Every part names text {self.LABEL.match(before[0])[3]}. A missing part number", before[0])

    def test_unreadable_rule_file_prints_a_visible_error_instead_of_nothing(self):
        (self.root / "rules/core.md").write_bytes(b"# Core rules\n\xff\xfe not UTF-8\n")
        out = io.StringIO()
        with mock.patch.object(context, "root", return_value=self.root), contextlib.redirect_stdout(out):
            context.main(["claude", "1"])
        self.assertRegex(out.getvalue(), r"^HOUSE RULES HOOK ERROR: 'utf-8' codec can't decode byte 0xff")

    def script(self):
        """The real hook script, copied into this synthetic checkout so it reads these files."""
        path = self.root / "scripts/session_context.py"
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            shutil.copyfile(REPO / "scripts/session_context.py", path)
        return [sys.executable, "-I", str(path)]

    def kimi_env(self):
        return {**os.environ, "KIMI_CODE_HOME": str(self.root / "kimi-home")}

    def kimi(self, mode, data):
        """Run one Kimi hook through its command line, as Kimi runs it; return its output."""
        result = subprocess.run([*self.script(), "kimi", mode], cwd=self.root, env=self.kimi_env(),
                                stdin=None, input=data if isinstance(data, str) else json.dumps(data),
                                capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_kimi_prints_the_rules_on_the_first_prompt_of_each_session_only(self):
        codex = subprocess.run([*self.script(), "codex"], cwd=self.root, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=30, check=True).stdout
        rules = json.loads(codex)["hookSpecificOutput"]["additionalContext"] + "\n"
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "session_a", "prompt": []}
        self.assertEqual(self.kimi("prompt", prompt), rules)
        self.assertEqual(self.kimi("prompt", prompt), "")
        self.assertEqual(self.kimi("prompt", {**prompt, "session_id": "session_b"}), rules)
        self.assertEqual(self.kimi("prompt", {**prompt, "is_steer": True}), "")
        self.assertEqual(self.kimi("prompt", {**prompt, "session_id": "session_b"}), "")

    def test_kimi_input_without_a_usable_session_id_prints_the_rules_with_an_error(self):
        for name, data in {"no session id": {"hook_event_name": "UserPromptSubmit"},
                           "path in session id": {"session_id": "../outside"},
                           "not JSON": "not json"}.items():
            with self.subTest(name=name):
                output = self.kimi("prompt", data)
                self.assertTrue(output.startswith("HOUSE RULES HOOK ERROR: Kimi gave no usable session id"), output)
                self.assertIn("===== House Rules file rules/core.md =====", output)
                self.assertFalse((self.root / "outside").exists())

    def test_kimi_retries_on_the_next_prompt_after_a_failed_read(self):
        prompt = {"session_id": "session_a"}
        original = (self.root / "rules/core.md").read_bytes()
        (self.root / "rules/core.md").write_bytes(b"# Core rules\n\xff not UTF-8\n")
        self.assertTrue(self.kimi("prompt", prompt).startswith("HOUSE RULES HOOK ERROR: 'utf-8' codec"))
        (self.root / "rules/core.md").write_bytes(original)
        self.assertIn("===== House Rules file rules/core.md =====", self.kimi("prompt", prompt))

    def test_kimi_missing_rule_file_or_index_is_retried_until_the_complete_rules_print(self):
        for number, name in enumerate(("rules/core.md", "INDEX.md")):
            with self.subTest(missing=name):
                prompt = {"session_id": f"session_{number}"}
                original = (self.root / name).read_text()
                (self.root / name).unlink()
                record = self.root / "kimi-home" / context.KIMI_STATE / prompt["session_id"]
                for _ in range(2):
                    # (a) The missing file is reported and no completed delivery is recorded.
                    # The script names paths under its resolved location.
                    self.assertIn(f"HOUSE RULES HOOK ERROR: {self.root.resolve() / name} is missing.",
                                  self.kimi("prompt", prompt))
                    self.assertFalse(record.exists())
                self.write(name, original)
                # (b) The same session's next prompt delivers every always-load file the printed index lists.
                delivered = self.kimi("prompt", prompt)
                printed = delivered.split("===== House Rules file INDEX.md =====\n", 1)[1].split("\n=====", 1)[0]
                listed = context.always_load(printed)
                self.assertTrue(listed, printed)
                for listed_name in listed:
                    self.assertIn(f"===== House Rules file {listed_name} =====\n", delivered)
                    self.assertIn((self.root / listed_name).read_text().split("\n", 1)[0], delivered)
                self.assertTrue(record.exists())
                self.assertEqual(self.kimi("prompt", prompt), "")

    @unittest.skipIf(os.geteuid() == 0, "root reads a folder without search permission")
    def test_kimi_unreadable_local_index_is_not_recorded_and_is_delivered_once_readable(self):
        prompt = {"session_id": "session_a"}
        folder = self.root / "custom"
        record = self.root / "kimi-home" / context.KIMI_STATE / "session_a"
        folder.chmod(0)
        self.addCleanup(folder.chmod, 0o755)
        self.assertIn("HOUSE RULES HOOK ERROR: [Errno 13] Permission denied", self.kimi("prompt", prompt))
        self.assertFalse(record.exists())
        folder.chmod(0o755)
        self.assertIn("Local policy line.", self.kimi("prompt", prompt))
        self.assertTrue(record.exists())
        self.assertEqual(self.kimi("prompt", prompt), "")

    def test_kimi_missing_local_index_is_reported_once_per_session(self):
        prompt = {"session_id": "session_a"}
        (self.root / "custom/INDEX.md").unlink()
        self.assertIn(f"HOUSE RULES HOOK ERROR: this computer's local index {self.root.resolve() / 'custom/INDEX.md'} "
                      "is missing", self.kimi("prompt", prompt))
        self.assertEqual(self.kimi("prompt", prompt), "")

    def test_kimi_rules_quoting_the_error_prefix_still_print_once(self):
        prompt = {"session_id": "session_a"}
        self.write("custom/INDEX.md", "# Machine-local capability index\n\n```text\n"
                                      "HOUSE RULES HOOK ERROR: example of a hook error line\n```\n")
        self.assertIn("HOUSE RULES HOOK ERROR: example of a hook error line", self.kimi("prompt", prompt))
        self.assertEqual(self.kimi("prompt", prompt), "")

    def test_kimi_section_too_long_for_a_part_is_retried(self):
        prompt = {"session_id": "session_a"}
        self.write("rules/core.md", "# Core rules\n\n## Long section\n\n" + "word " * 3000 + "\n")
        for _ in range(2):
            self.assertIn('the section "Long section" is', self.kimi("prompt", prompt))

    def delivered_with_index_changing(self, first_read, later_reads):
        """Delivered text and failures while INDEX.md changes between filesystem checks."""
        index, changed = self.root / "INDEX.md", []
        real_read, real_is_file = Path.read_text, Path.is_file

        # The change lands right after the first read, or right after the index is first found missing.
        def read_text(path, *args, **kwargs):
            if path != index:
                return real_read(path, *args, **kwargs)
            text = later_reads if changed else first_read
            changed.append(True)
            return text

        def is_file(path):
            if path != index:
                return real_is_file(path)
            present = (later_reads if changed else first_read) is not None
            if not present:
                changed.append(True)
            return present

        with mock.patch.object(Path, "read_text", read_text), mock.patch.object(Path, "is_file", is_file):
            rev, packed, _ = context.parts(self.root)
        return "".join(context.render(number, rev, packed) for number in range(1, len(packed) + 1))

    def assert_complete_or_visibly_missing(self, text):
        """Either every always-load file the printed index lists is delivered, or the index is reported missing."""
        printed = text.split("===== House Rules file INDEX.md =====\n", 1)[1].split("\n=====", 1)[0]
        if printed.startswith("HOUSE RULES HOOK ERROR:"):
            self.assertIn(f"HOUSE RULES HOOK ERROR: {self.root / 'INDEX.md'} is missing.", printed)
            return
        listed = context.always_load(printed)
        self.assertTrue(listed, printed)
        for name in listed:
            self.assertIn(f"===== House Rules file {name} =====", text)

    def test_index_changing_while_read_never_prints_a_file_list_it_did_not_load(self):
        self.write("rules/extra.md", "# Extra rules\n")
        original = (self.root / "INDEX.md").read_text()
        updated = original.replace("- [writing](rules/writing.md)\n",
                                   "- [writing](rules/writing.md)\n- [extra](rules/extra.md)\n")
        self.assert_complete_or_visibly_missing(self.delivered_with_index_changing(original, updated))
        # Found missing first, restored later.
        self.assert_complete_or_visibly_missing(self.delivered_with_index_changing(None, updated))

    def test_kimi_hook_stopped_before_printing_leaves_the_next_prompt_to_print(self):
        # The reader of the hook's output is gone before the hook writes, so printing fails.
        hook = subprocess.Popen([*self.script(), "kimi", "prompt"], cwd=self.root, env=self.kimi_env(),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        hook.stdout.close()
        _, error = hook.communicate(json.dumps({"session_id": "session_a"}).encode(), timeout=30)
        self.assertIn(b"BrokenPipeError", error)
        self.assertFalse((self.root / "kimi-home" / context.KIMI_STATE / "session_a").exists())
        self.assertIn("===== House Rules file rules/core.md =====", self.kimi("prompt", {"session_id": "session_a"}))

    def test_kimi_session_records_are_kept_however_old(self):
        prompt = {"session_id": "session_old"}
        self.kimi("prompt", prompt)
        record = self.root / "kimi-home" / context.KIMI_STATE / "session_old"
        stamp = record.stat().st_mtime - 400 * 24 * 3600
        os.utime(record, (stamp, stamp))
        self.kimi("prompt", {"session_id": "session_new"})
        self.assertTrue(record.exists())
        self.assertEqual(self.kimi("prompt", prompt), "")

    @NEEDS_TOML
    def test_printed_kimi_hooks_parse_as_toml_and_keep_unusual_paths_exact(self):
        base = Path('/opt/house "rules" \\ ünï \x7f')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            context.main(["hooks", "kimi", str(base)])
        hooks = sync.tomllib.loads(out.getvalue())["hooks"]
        self.assertEqual(hooks, context.hooks("kimi", sys.executable, base)["hooks"])
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["event"], "UserPromptSubmit")
        self.assertEqual(shlex.split(hooks[0]["command"]),
                         [sys.executable, "-I", str(base / "scripts/session_context.py"), "kimi", "prompt"])
        self.assertEqual(hooks[0]["timeout"], context.TIMEOUT)

    def test_house_rules_itself_fits_the_registered_slots(self):
        rev, packed, too_long = context.parts(REPO)
        self.assertEqual(too_long, [])
        self.assertLessEqual(len(packed), context.SLOTS, "raise SLOTS: the rules outgrow the registered hooks")
        for number in range(1, len(packed) + 1):
            self.assertLess(context.units(context.render(number, rev, packed)), context.CAP)


class PointerTemplateTests(unittest.TestCase):
    EXPECTED_BLOCK = """<!-- house-rules:begin -->
# Shared operating foundation (House Rules)

At session start and after every context compaction or reset, read `<HOUSE_RULES_ROOT>/INDEX.md`.
It is the index for collaboration, verification, autonomy, and durable
execution rules. Load the rule files and Skills that its conditions select.
Repository-local rules supply project details and win over shared preferences; all work remains subject to the host's instruction hierarchy
and access controls.

Load only the House Rules Skills relevant to the task. Use
`<HOUSE_RULES_ROOT>/STRUCTURE.md` when deciding artifact paths and naming. Keep
model, permission, MCP, plugin, and hook configuration in the native tool
settings.

Then read `<HOUSE_RULES_ROOT>/custom/INDEX.md` when it exists. It is this
computer's local index: installed tools and the operator's local policies,
such as which model and tool run workers. Its policies apply to every session
on this computer; repository rules still win where they conflict.

Before sending any reply, check it against `<HOUSE_RULES_ROOT>/rules/writing.md`
and the operator-writing Skill.
<!-- house-rules:end -->"""

    def test_parent_folder_cleanup_preserves_house_rules_repository_pointer(self):
        installer = (REPO / 'INSTALL-AGENTS.md').read_text()
        cleanup = installer.split('5. Put ', 1)[1].split('6. Preserve ', 1)[0]
        self.assertIn('remove such a block instead of migrating it.', cleanup)
        self.assertIn('Preserve `<HOUSE_RULES_ROOT>/AGENTS.md`, the canonical repository '
                      'pointer,\n   even when the House Rules checkout is a parent folder.',
                      cleanup)

    def test_global_discovery_requires_neutral_directory_before_counting_headings(self):
        installer = (REPO / 'INSTALL-AGENTS.md').read_text()
        verification = installer.split('Runtime verification:\n', 1)[1].split(
            'Report filesystem and runtime verification separately.', 1)[0]
        setup, checks = verification.split('1. Prefer discovery views', 1)
        self.assertIn('Run global-installation discovery verification from a neutral '
                      'working directory\noutside any repository.', setup)
        self.assertIn('Check that neither it nor any parent folder contains\n'
                      'instruction files such as `AGENTS.md` or `CLAUDE.md`.', setup)
        self.assertIn('exactly one `Shared operating foundation (House Rules)` heading',
                      checks)

    def test_installed_pointer_keeps_old_wording_with_renamed_index(self):
        self.assertEqual(sync.template(REPO), self.EXPECTED_BLOCK.replace(
            '<HOUSE_RULES_ROOT>', str(REPO)))

    def test_house_rules_pointer_keeps_portable_old_wording(self):
        self.assertEqual((REPO / 'AGENTS.md').read_text().strip(),
                         self.EXPECTED_BLOCK.replace('<HOUSE_RULES_ROOT>/', '').replace(
                             "read `INDEX.md`.\n",
                             "read `INDEX.md`.\n`INDEX.md` sits in the same folder as this file.\n"))

    def test_managed_repository_keeps_three_line_loader(self):
        structure = (REPO / 'STRUCTURE.md').read_text()
        loader = structure.split('```markdown\n', 1)[1].split('```', 1)[0]
        self.assertEqual(loader, '# AGENTS.md\n'
                         'This repository is managed by [House Rules](<House Rules URL>). '
                         'Read House Rules `INDEX.md` first and follow it.\n'
                         "This repository's own rules are in [.agents/rules.md](.agents/rules.md).\n")
        self.assertIn('A managed repository uses exactly this `AGENTS.md`:', structure)
        self.assertIn('A managed repository without its own rules omits the third line.', structure)

    def test_preferences_changes_only_index_filename(self):
        self.assertIn('These stack defaults apply whenever House Rules is installed. '
                      'Overrides follow INDEX.md,\n'
                      'and an existing coherent project stack also takes precedence over them.',
                      (REPO / 'PREFERENCES.md').read_text())

    def test_transcript_gaps_changes_only_index_filename(self):
        self.assertIn('Read the batch,\n'
                      'INDEX.md, all rule, skill and prompt files, and the configured '
                      'decisions file in full.',
                      (REPO / 'prompts/util/transcript-gaps.md').read_text())



class ApprovedWorkListingTests(unittest.TestCase):
    """The session-start hook lists open `approved` issues so approved work survives compaction."""

    HEAD = "Open approved work. Continue the items you own; the others belong to the owners named:"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base, self.bin = Path(self.tmp.name) / "root", Path(self.tmp.name) / "bin"
        (self.base / "custom").mkdir(parents=True)
        self.bin.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def fake_gh(self, stdout, code=0):
        """A gh that records its arguments and prints a fixed response."""
        gh = self.bin / "gh"
        gh.write_text("#!/bin/sh\necho \"$@\" >> " + shlex.quote(str(self.bin / "calls")) +
                      "\nprintf '%s' " + shlex.quote(stdout) + f"\nexit {code}\n")
        gh.chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})

    def configure(self, text="# owners\nexample-org\n"):
        (self.base / "custom/approved-work.conf").write_text(text)

    def issue(self, number, title, body=""):
        return {"repository": {"nameWithOwner": "example-org/tool"}, "number": number, "title": title, "body": body}

    def test_lists_open_items_sorted_with_their_owner_line(self):
        self.configure()
        issues = [self.issue(9, "No owner yet"), self.issue(7, "Ship the cache", "Plan.\nOwner: lane-3, after the parser PR\n")]
        with self.fake_gh(json.dumps(issues)):
            self.assertEqual(context.approved(self.base), "\n".join([
                self.HEAD,
                "- example-org/tool#7 Ship the cache — Owner: lane-3, after the parser PR",
                "- example-org/tool#9 No owner yet — Owner: no Owner: line"]))

    def test_comments_are_not_scopes(self):
        self.configure("example-org  # approved backlog\n   # indented comment\n")
        with self.fake_gh("[]"):
            self.assertEqual(context.approved(self.base), "")
        self.assertEqual((self.bin / "calls").read_text().split()[:5], ["search", "issues", "--owner", "example-org", "--label"])

    def test_prints_nothing_without_configuration_or_items(self):
        self.assertEqual(context.approved(self.base), "")
        self.configure()
        with self.fake_gh("[]"):
            self.assertEqual(context.approved(self.base), "")

    def test_failures_and_malformed_responses_become_one_visible_line(self):
        self.configure()
        bad_issues = [{**self.issue(3, "t"), "number": None}, {**self.issue(3, "t"), "title": {"a": 1}},
                      {**self.issue(3, "t"), "body": 42}, {**self.issue(3, "t"), "repository": "example-org/tool"},
                      {**self.issue(3, "t"), "number": True}]
        cases = [("", 1), ("", 0), ("not json", 0), ("{}", 0), ("[null]", 0), (json.dumps({"a": [1, 2]}, indent=2), 0),
                 *((json.dumps([bad]), 0) for bad in bad_issues)]
        for stdout, code in cases:
            with self.subTest(stdout=stdout, code=code), self.fake_gh(stdout, code):
                listing = context.approved(self.base)
                self.assertTrue(listing.startswith(context.ERROR), listing)
                self.assertNotIn("\n", listing)
        (self.base / "custom/approved-work.conf").unlink()
        (self.base / "custom/approved-work.conf").mkdir()
        self.assertTrue(context.approved(self.base).startswith(context.ERROR))

    def test_a_multiline_gh_error_stays_one_line(self):
        self.configure()
        gh = self.bin / "gh"
        gh.write_text("#!/bin/sh\nprintf 'HTTP 502\\nbad gateway\\n' >&2\nexit 1\n")
        gh.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(context.approved(self.base),
                             f"{context.ERROR} approved-work listing for example-org failed: HTTP 502 bad gateway")

    def test_no_search_starts_after_the_shared_deadline(self):
        self.configure("example-org\nother-org\n")
        clock = iter([0.0, 1.0, context.GH_TIMEOUT + 1.0])
        issues = [self.issue(7, "Found before the deadline")]
        with self.fake_gh(json.dumps(issues)), mock.patch.object(context.time, "monotonic", lambda: next(clock)):
            listing = context.approved(self.base)
        self.assertEqual(listing, "\n".join([
            self.HEAD, self.row(7, "Found before the deadline"),
            f"{context.ERROR} the approved-work search was cut short at the hook's deadline before searching other-org."]))
        self.assertEqual(len((self.bin / "calls").read_text().splitlines()), 1)

    def test_the_search_stops_at_the_deadline_house_rules_exec_passes(self):
        self.configure("example-org\n")
        with self.fake_gh("[]"), mock.patch.dict(os.environ, {"HOUSE_RULES_DEADLINE": str(time.time() - 1)}):
            listing = context.approved(self.base)
        self.assertEqual(listing, "\n".join([self.HEAD, f"{context.ERROR} the approved-work search was cut short at "
                                                          "the hook's deadline before searching example-org."]))
        self.assertFalse((self.bin / "calls").exists())

    NOTICE = ("HOUSE RULES HOOK ERROR: this list may not show every open approved item. Close the issues of "
              "finished approved work, so the label marks only work that still waits.")

    def row(self, number, title):
        return f"- example-org/tool#{number} {title} — Owner: no Owner: line"

    def test_a_full_search_ends_the_list_with_the_notice(self):
        self.configure("example-org/tool\n")
        numbers = range(100, 100 + context.GH_LIMIT)
        with self.fake_gh(json.dumps([self.issue(n, "t") for n in numbers])), \
                mock.patch.object(context, "APPROVED_CAP", 100_000):
            self.assertEqual(context.approved(self.base),
                             "\n".join([self.HEAD, *(self.row(n, "t") for n in numbers), self.NOTICE]))

    def test_a_long_list_keeps_whole_rows_and_the_notice_inside_the_cap(self):
        self.configure()
        with self.fake_gh(json.dumps([self.issue(n, "x" * 40) for n in range(10, 50)])), \
                mock.patch.object(context, "APPROVED_CAP", 500):
            listing = context.approved(self.base)
        self.assertEqual(listing, "\n".join([self.HEAD, self.row(10, "x" * 40), self.row(11, "x" * 40), self.NOTICE]))
        self.assertLessEqual(len(listing), 500)

    def test_a_decoder_recursion_error_is_one_visible_line(self):
        self.configure()
        with self.fake_gh("[]"), mock.patch.object(context.json, "loads", side_effect=RecursionError("too deep")):
            self.assertEqual(context.approved(self.base), f"{context.ERROR} approved-work listing failed: too deep")

    def test_both_products_run_the_listing_as_its_own_hook(self):
        for product in ("claude", "codex"):
            with self.subTest(product=product):
                commands = [h["command"] for h in context.hooks(product, "/usr/bin/python3", self.base)["hooks"]]
                expected = "scripts/session_context.py approved" + (" codex" if product == "codex" else "")
                self.assertEqual(sum(c.endswith(expected) for c in commands), 1)


def load_tests(loader, tests, pattern):
    SyncTests.setUpClass()
    return tests


if __name__ == "__main__":
    unittest.main()
