#!/usr/bin/env python3
"""Update the checkout and report House Rules installation drift; see INSTALL-AGENTS.md §6.

With --install-hooks it also writes the session-context hook into each Claude Code and Codex
hook file (INSTALL-AGENTS.md §3 step 10). It writes no other file. With --command PATH the hook runs
through the House Rules command at PATH (`house-rules exec --hook scripts/session_context.py ...`),
which resolves each session's repository's House Rules version first.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import secrets
import time
import types

try:
    import tomllib
except ImportError:   # Python 3.9 and 3.10 have no TOML reader; the Kimi hook check reports it.
    tomllib = None


MARKERS = re.compile(r"<!-- (house-rules|forge|groundwork):(begin|end) -->")
def load_session_context(path):
    """The hook generator compiled from the current bytes of one checkout's script.

    Compiling the source directly never reuses cached bytecode, which can survive an update that
    keeps the file's size and modification second. main() loads the selected checkout's generator
    after updating it, so installation and verification build the entry sessions will run.
    """
    module = types.ModuleType("session_context")
    module.__file__ = str(path)
    try:
        exec(compile(Path(path).read_bytes(), str(path), "exec"), module.__dict__)
    except SyntaxError as error:
        raise ValueError(f"hook generator does not compile: {path}: {error}") from error
    return module


session_context = load_session_context(Path(__file__).with_name("session_context.py"))
# Native hook file and session_context.py product name for each product the installer registers.
HOOK_FILES = {"CLAUDE_CONFIG_DIR": ("settings.json", "claude"), "CODEX_HOME": ("hooks.json", "codex")}
# Kimi Code's hook is registered by hand (INSTALL-AGENTS.md §3 step 10); verification checks its config.toml.
KIMI_HOOK_FILES = {"KIMI_CODE_HOME": "config.toml"}


def status(path, *, follow_symlinks=True):
    """Treat only a missing path as absent; propagate other filesystem errors."""
    try:
        return path.stat() if follow_symlinks else path.lstat()
    except FileNotFoundError:
        return None


def is_dir(path):
    info = status(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def is_file(path):
    info = status(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def is_link(path):
    info = status(path, follow_symlinks=False)
    return info is not None and stat.S_ISLNK(info.st_mode)


def present(path):
    return status(path, follow_symlinks=False) is not None


def git(root, *args):
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def update(root):
    """Never update dirty, non-default, ahead or diverged checkouts."""
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        return ["checkout is dirty; not pulled"]
    branch = git(root, "branch", "--show-current")
    head = git(root, "rev-parse", "HEAD")
    remote = git(root, "ls-remote", "--symref", "origin", "HEAD")
    match = re.search(r"^ref: refs/heads/(.+)\s+HEAD$", remote, re.M)
    if not match:
        raise ValueError("origin does not advertise a default branch")
    default = match.group(1)
    if branch != default:
        return [f"checkout is on {branch or 'detached HEAD'}, not {default}; not pulled"]
    git(root, "fetch", "origin", default)
    revision = git(root, "rev-parse", "FETCH_HEAD")
    ahead, behind = map(int, git(root, "rev-list", "--left-right", "--count",
                                f"HEAD...{revision}").split())
    if (git(root, "branch", "--show-current") != branch or
            git(root, "rev-parse", "HEAD") != head or
            git(root, "status", "--porcelain", "--untracked-files=all")):
        return ["checkout changed during update; not pulled"]
    if ahead:
        return [f"checkout is {'diverged' if behind else 'ahead'}; not pulled"]
    if behind:
        print(git(root, "merge", "--ff-only", "--no-overwrite-ignore", revision))
    return []


def template(root):
    """Derive the adapter from its single authoritative Markdown template."""
    text = (root / "INSTALL-AGENTS.md").read_text(encoding="utf-8")
    match = re.search(r"```markdown\n(.*?)\n\s*```", text, re.S)
    if not match:
        raise ValueError("managed block template is missing")
    block = "\n".join(line.removeprefix("   ") for line in match.group(1).splitlines())
    if not block.startswith("<!-- house-rules:begin -->") or not block.endswith(
            "<!-- house-rules:end -->"):
        raise ValueError("invalid managed block template")
    return block.replace("<HOUSE_RULES_ROOT>", str(root))


def homes(root, home):
    """Select present default homes and explicitly configured extra homes."""
    defaults = {
        "CLAUDE_CONFIG_DIR": Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude")),
        "CODEX_HOME": Path(os.environ.get("CODEX_HOME", home / ".codex")),
        "KIMI_CODE_HOME": Path(os.environ.get("KIMI_CODE_HOME", home / ".kimi-code")),
    }
    selected = []
    for key, path in defaults.items():
        if key in os.environ and (not path.is_absolute() or not is_dir(path)):
            raise ValueError(f"{key} must be an existing absolute directory")
        if present(path) and not is_dir(path):
            raise ValueError(f"{key} default home is not a directory: {path}")
        if is_dir(path):
            selected.append((key, path.absolute()))
    config = root / "custom/sync.env"
    if present(config):
        for number, line in enumerate(config.read_text(encoding="utf-8").splitlines(), 1):
            tokens = shlex.split(line, comments=True)
            if not tokens:
                continue
            if len(tokens) != 1 or "=" not in tokens[0]:
                raise ValueError(f"{config}:{number}: expected KEY=path")
            key, value = tokens[0].split("=", 1)
            if key not in defaults or not value:
                raise ValueError(f"{config}:{number}: unknown key or empty home")
            path = Path(value).expanduser()
            if not path.is_absolute() or not is_dir(path):
                raise ValueError(f"{config}:{number}: home must be an existing absolute directory")
            selected.append((key, path))
    for key, path in selected:
        if not path.is_absolute():
            raise ValueError(f"{key} must be an absolute directory")
        if key == "CODEX_HOME" and not any(is_file(path / name)
                                           for name in ("config.toml", "auth.json")):
            raise ValueError(f"not a Codex home (no config.toml or auth.json): {path}")
    return list(dict.fromkeys(selected))


def children(path):
    """Enumerate with errors intact on every supported Python version."""
    with os.scandir(path) as entries:
        return sorted(Path(entry.path) for entry in entries)


def receipts(root):
    """(path, receipt object) for each machine-readable installation receipt."""
    directory = root / "custom/installations"
    result = []
    for path in children(directory) if present(directory) else []:
        if path.suffix != ".json" or path.name.startswith("sync-"):
            continue
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError(f"receipt must be an object: {path}")
        result.append((path, receipt))
    return result


def replaced_hooks(root):
    """Exact commands of earlier hand-made House Rules hooks, which receipts list under replaced_hooks."""
    commands = set()
    for path, receipt in receipts(root):
        listed = receipt.get("replaced_hooks", [])
        if not isinstance(listed, list) or any(not isinstance(value, str) or not value for value in listed):
            raise ValueError(f"invalid receipt replaced_hooks: {path}")
        commands.update(listed)
    return commands


def ownership_roots(root):
    """Read manual installation receipts solely to recognize prior-root links."""
    roots = {str(root)}
    for path, receipt in receipts(root):
        previous = receipt.get("previous_roots", [])
        if not isinstance(previous, list):
            raise ValueError(f"invalid receipt roots: {path}")
        recorded = [receipt.get("source_root", str(root)), *previous]
        if any(not isinstance(value, str) or not Path(value).is_absolute() for value in recorded):
            raise ValueError(f"invalid receipt roots: {path}")
        roots.update(recorded)
    return roots


def owned_link(path, roots):
    # Compare the target as written; never follow an unrelated alias into ownership.
    return is_link(path) and os.readlink(path) in {
        str(Path(root) / "skills" / path.name) for root in roots}


def instruction_file(key, home):
    if key == "CLAUDE_CONFIG_DIR":
        return home / "CLAUDE.md"
    override = home / "AGENTS.override.md"
    return override if key == "CODEX_HOME" and present(override) else home / "AGENTS.md"


def protected_paths(selected):
    return [(path / "skills/.system").resolve()
            for key, path in selected if key == "CODEX_HOME"]


def check_protected(path, selected):
    resolved = path.resolve()
    if any(resolved == system or system in resolved.parents
           for system in protected_paths(selected)):
        raise ValueError(f"path aliases protected Codex .system content: {path}")


def skill_folders(selected, home):
    """Check each configured scan location, including aliases with different roles."""
    folders = set()
    for key, product_home in selected:
        if key == "CLAUDE_CONFIG_DIR":
            folders.add((product_home / "skills", True))
        else:
            folders.update(((home / ".agents/skills", True), (product_home / "skills", False)))
    return sorted(folders)


def verify(root, home, findings, selected, hook_root=None, generator=None):
    """Collect results in caller-owned lists so a later error cannot discard them."""
    generator = generator or session_context
    block = template(root)
    selected.extend(homes(root, home))
    roots = ownership_roots(root) | {str(hook_root or root)}
    skills = {path.name: path for path in children(root / "skills")
              if is_dir(path) and is_file(path / "SKILL.md")}
    if ".system" in skills:
        raise ValueError(".system is reserved; cannot install a House Rules skill there")
    instructions = {instruction_file(key, product_home) for key, product_home in selected}
    for path in (home / "AGENTS.md", home / "CLAUDE.md"):
        if path not in instructions and present(path) and MARKERS.search(
                path.read_bytes().decode("utf-8")):
            findings.append(f"duplicate home-level managed block: {path}")
    for path in sorted(instructions):
        if is_link(path) and not is_file(path):
            findings.append(f"instruction symlink has no regular target: {path}")
            continue
        check_protected(path, selected)
        text = path.read_bytes().decode("utf-8") if present(path) else ""
        markers = list(MARKERS.finditer(text))
        if not markers:
            findings.append(f"missing block: {path}")
        elif (len(markers) != 2 or markers[0].group(2) != "begin" or
              markers[1].group(2) != "end" or markers[0].group(1) != markers[1].group(1)):
            findings.append(f"malformed or multiple managed blocks: {path}")
        elif text[markers[0].start():markers[1].end()] != block:
            findings.append(f"stale block: {path}")
    for folder, install in skill_folders(selected, home):
        check_protected(folder, selected)
        entries = {path.name: path for path in children(folder)} if present(folder) else {}
        for name in sorted(set(skills) | set(entries)):
            if name == ".system":
                continue
            path = folder / name
            exists = name in entries
            ours = exists and owned_link(path, roots)
            if install and name in skills:
                if exists and not ours:
                    findings.append(f"same-name skill conflict: {path}")
                elif not is_link(path) or os.readlink(path) != str(skills[name]):
                    findings.append(f"missing or stale skill link: {path}")
            elif ours:
                reason = "shadowing skill" if name in skills else "removed skill"
                findings.append(f"{reason}: {path}")
            elif exists and not install and name in skills:
                findings.append(f"same-name shadowing conflict: {path}")

    for path in always_load(root, findings):
        if not is_file(path):
            findings.append(f"missing rule file: {path}")
    for key, product_home in selected:
        if key in HOOK_FILES:
            check_hook(hook_root or root, roots, product_home / HOOK_FILES[key][0], HOOK_FILES[key][1], findings,
                       replaced_hooks(root), generator)
        elif key in KIMI_HOOK_FILES:
            check_kimi_hook(hook_root or root, roots, product_home / KIMI_HOOK_FILES[key], findings, generator)
    _, packed, too_long = generator.parts(root)
    if len(packed) > generator.SLOTS:
        findings.append(f"session context needs {len(packed)} parts; the hook registers {generator.SLOTS}")
    for name, heading in too_long:
        findings.append(f"session context section too long for one part: {name}, heading \"{heading}\"")


def read_hooks(path, data=None):
    """The hook file's object and its SessionStart list; a file that is not a JSON object is an error.

    data: the file's bytes when the caller already read them, so it can detect later changes.
    """
    if data is None and present(path):
        data = path.read_bytes()
    try:
        settings = json.loads(data.decode("utf-8")) if data is not None else {}
    except ValueError as error:
        raise ValueError(f"hook file is not valid JSON: {path}: {error}") from error
    if not isinstance(settings, dict):
        raise ValueError(f"hook file is not a JSON object: {path}")
    hooks = settings.get("hooks", {})
    entries = hooks.get("SessionStart", []) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"hook file has no SessionStart list: {path}")
    return settings, entries


def check_hook(root, roots, path, product, findings, replaced=frozenset(), generator=None):
    """Exactly the INSTALL-AGENTS.md §3 SessionStart entry, run by an existing absolute interpreter."""
    _, entries = read_hooks(path)
    scripts = {str(Path(other) / "scripts/session_context.py") for other in roots}
    handlers = [handler for entry in entries if isinstance(entry, dict) and isinstance(entry.get("hooks"), list)
                for handler in entry["hooks"]]
    if any(owner(handler, scripts, replaced) == "replaced" for handler in handlers):
        findings.append(f"replaced House Rules hook still registered: {path}")
    owned = [(entry, handler_commands(entry)) for entry in entries if isinstance(entry, dict) and isinstance(
        entry.get("hooks"), list) and any(owner(handler, scripts) == "ours" for handler in entry["hooks"])]
    if not owned:
        findings.append(f"missing session context hook: {path}")
        return
    entry, commands = owned[0]
    python = commands[0][0] if commands[0] else ""
    through = exec_form(commands[0]) if commands[0] else False
    expected = (generator or session_context).hooks(product, python, root, *((python,) if through else ()))
    if len(owned) != 1 or not Path(python).is_absolute() or not is_file(Path(python)) or entry != expected or \
            (through and not os.access(python, os.X_OK)):  # a command that cannot run delivers no rules
        findings.append(f"stale session context hook: {path}")
    elif not through and any(len(tokens) > 2 and not is_file(Path(tokens[2])) for tokens in commands):
        findings.append(f"session context hook runs a missing script: {path}")


PYTHON = re.compile(r"python([0-9]+(\.[0-9]+)*t?)?(\.exe)?")


def owner(handler, scripts, replaced=frozenset(), python=None):
    """ "ours", "replaced" or None for one hook handler; nothing is inferred from a command's shape.

    Ours: exactly a form session_context.hooks() prints, <python> -I <script> followed by
    `claude <n>`, `codex`, `approved` or `approved codex`, with one of our scripts and a Python
    interpreter (the installer's own file, or a program named python, python3, python3.14t ...). Replaced: a command a receipt
    lists verbatim under replaced_hooks.
    """
    command = handler.get("command") if isinstance(handler, dict) else None
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []
    # The current form wins over the receipt list, which may name commands the current entry reuses.
    if len(tokens) in (4, 5) and tokens[1] == "-I" and tokens[2] in scripts and session_arguments(tokens[3:]) and (
            PYTHON.fullmatch(Path(tokens[0]).name) or same_file(tokens[0], python or sys.executable)):
        return "ours"
    if exec_form(tokens) and session_arguments(tokens[4:]):
        return "ours"
    return "replaced" if command in replaced else None


def session_arguments(arguments):
    """The arguments of one Claude Code or Codex session-context handler: claude <n>, codex, approved, approved codex."""
    return arguments in (["codex"], ["approved"], ["approved", "codex"]) or (
        len(arguments) == 2 and arguments[0] == "claude" and arguments[1].isdigit())


def exec_form(tokens):
    """<house-rules> exec --hook scripts/session_context.py ...: a handler run through the House Rules command."""
    return (len(tokens) >= 5 and Path(tokens[0]).name == "house-rules" and Path(tokens[0]).is_absolute()
            and tokens[1:4] == ["exec", "--hook", "scripts/session_context.py"])


def same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def check_kimi_hook(root, roots, path, findings, generator=None):
    """Exactly the [[hooks]] table `hooks kimi` prints, run by an existing absolute interpreter."""
    if tomllib is None:
        findings.append(f"Kimi session context hook not checked: Python {sys.version_info[0]}.{sys.version_info[1]} "
                        f"has no TOML reader; run sync with Python 3.11 or later: {path}")
        return
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8")) if present(path) else {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        findings.append(f"unreadable Kimi config, session context hook not checked: {path}: {error}")
        return
    scripts = {str(Path(other) / "scripts/session_context.py") for other in roots}
    tables = config.get("hooks") if isinstance(config.get("hooks"), list) else []
    ours = []
    for table in tables:
        command = table.get("command") if isinstance(table, dict) else None
        try:
            tokens = shlex.split(command) if isinstance(command, str) else []
        except ValueError:
            tokens = []
        if (len(tokens) >= 3 and tokens[1] == "-I" and tokens[2] in scripts) or exec_form(tokens):
            ours.append((table, tokens))
    if not ours:
        findings.append(f"missing session context hook: {path}")
        return
    python = ours[0][1][0]
    through = exec_form(ours[0][1])
    # The same Python-program test the installer's owner() applies: an existing non-Python program
    # (for example /usr/bin/true) would run without delivering the rules.
    interpreter = through or PYTHON.fullmatch(Path(python).name) or same_file(python, sys.executable)
    expected = (generator or session_context).hooks("kimi", python, root, *((python,) if through else ()))["hooks"]
    if not Path(python).is_absolute() or not is_file(Path(python)) or not interpreter or \
            (through and not os.access(python, os.X_OK)) or [table for table, _ in ours] != expected:
        findings.append(f"stale session context hook: {path}")


def handler_commands(entry):
    """Each handler's command as tokens; a handler without a parsable command gives no tokens."""
    result = []
    for handler in entry.get("hooks") if isinstance(entry.get("hooks"), list) else []:
        command = handler.get("command") if isinstance(handler, dict) else None
        try:
            result.append(shlex.split(command) if isinstance(command, str) else [])
        except ValueError:
            result.append([])
    return result


def install_hooks(root, hook_root, home, python, report, generator=None, command=None):
    """Write the current hook entry into each Claude Code and Codex hook file.

    report(event, path, backup) runs at once after each backup ("backup") and each replacement
    ("registered"), so a later failure cannot hide a backup or a file already changed.

    Removes House Rules handlers of any recorded root and receipt-listed replaced hooks, keeps every
    other entry and setting, backs up an existing file beside it and replaces the file atomically.
    A file whose only House Rules entry is already the current one is not written. The file is
    read again just before replacement, and a change since the first read is an error; a change
    in the instant between that check and the replacement is not detected, so installation runs
    while no agent session changes its settings (INSTALL-AGENTS.md §3 step 10).

    With command, the path of the installed House Rules command, the entry runs through it, so each session
    gets its repository's resolved House Rules version.

    Each home folder is opened once, its open handle is matched to the folder that passes the
    protected-content check, and every read and write happens inside that handle, so renaming or
    relinking the folder's path afterwards cannot redirect a write.
    """
    generator = generator or session_context
    if not hasattr(os, "fchdir") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("--install-hooks needs directory handles (os.fchdir), which this platform lacks")
    roots = ownership_roots(root) | {str(hook_root)}
    scripts = {str(Path(other) / "scripts/session_context.py") for other in roots}
    replaced = replaced_hooks(root)
    selected = homes(root, home)
    folders = []
    try:
        for key, product_home in selected:
            if key in HOOK_FILES:
                handle = os.open(product_home, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    real = product_home.resolve(strict=True)
                except BaseException as error:
                    close_all([handle], error)
                    raise
                folders.append((handle, real, *HOOK_FILES[key]))
        for handle, real, name, _ in folders:  # every destination, before the first write
            opened, checked = os.fstat(handle), os.stat(real)
            if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
                raise ValueError(f"hook folder changed while it was checked: {real}")
            check_protected(real / name, selected)
            check_protected(real, selected)
        for handle, real, name, product in folders:
            with inside(handle):
                install_one(real, name, generator.hooks(product, python, hook_root, *((command,) if command else ())),
                            scripts, replaced, python, report)
    except BaseException as error:
        close_all([handle for handle, *_ in folders], error)
        raise
    close_all([handle for handle, *_ in folders], None)


def close_all(handles, error):
    """Close every handle; a failure is raised, carrying `error` when one is already propagating."""
    failures = []
    for handle in handles:
        try:
            os.close(handle)
        except OSError as failure:
            failures.append(str(failure))
    if failures:
        message = "; ".join(failures)
        raise OSError(f"{error}; closing hook folders also failed: {message}" if error else
                      f"closing hook folders failed: {message}") from error


def inside(handle):
    """Run a block with the working directory set to an open folder, then return to the old one.

    A failure to return keeps the block's own error in the message, so neither is lost.
    """
    class Inside:
        def __enter__(self):
            self.previous = os.open(".", os.O_RDONLY)
            try:
                os.fchdir(handle)
            except BaseException as error:
                close_all([self.previous], error)
                raise

        def __exit__(self, kind, error, trace):
            try:
                os.fchdir(self.previous)
            except OSError as failure:
                close_all([self.previous], error)
                raise OSError(f"{error}; returning to the previous directory also failed: {failure}"
                              if error else f"returning to the previous directory failed: {failure}") from error
            close_all([self.previous], error)
            return False
    return Inside()


def read_relative(name):
    try:
        with open(name, "rb") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


def install_one(real, name, expected, scripts, replaced, python, report):
    """Install one hook file; the working directory is its checked folder, so every path is `name`."""
    path = real / name  # for messages only
    data = read_relative(name)
    settings, entries = read_hooks(path, data) if data is not None else ({}, [])
    kept, owned = [], []
    for entry in entries:
        handlers = entry.get("hooks") if isinstance(entry, dict) else None
        if isinstance(handlers, list):
            remaining = [handler for handler in handlers if owner(handler, scripts, replaced, python) is None]
            if len(remaining) != len(handlers):
                owned.append(entry)
                if not remaining:
                    continue  # an entry that held only House Rules handlers; other empty entries stay
                entry = {**entry, "hooks": remaining}
        kept.append(entry)
    if owned == [expected]:
        return
    settings.setdefault("hooks", {})["SessionStart"] = kept + [expected]
    mode = stat.S_IMODE(os.stat(name).st_mode) if data is not None else 0o600
    backup = backup_copy(Path(name), data, mode) if data is not None else None
    if backup is not None:
        report("backup", path, real / backup.name)
    temporary = f".{name}.{secrets.token_hex(6)}"
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(settings, indent=2) + "\n")
        os.chmod(temporary, mode)
        if read_relative(name) != data:
            raise ValueError(f"hook file changed during installation; not written: {path}")
        os.replace(temporary, name)
    except BaseException as error:
        if os.path.lexists(temporary):
            discard(temporary, error)
        raise
    report("registered", path, real / backup.name if backup else None)


def discard(path, error):
    """Remove a partial file after `error`; a failed removal reports both failures."""
    try:
        os.unlink(path)
    except OSError as cleanup:
        raise OSError(f"{error}; the incomplete file {path} could not be removed either: {cleanup}") from error


def backup_copy(path, data, mode):
    """A new backup beside the file, never replacing an earlier one, readable no wider than its owner."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for number in range(1000):
        backup = path.with_name(f"{path.name}.house-rules-backup-{stamp}" + (f"-{number}" if number else ""))
        try:
            handle = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode & 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
        except BaseException as error:
            discard(backup, error)  # an incomplete backup must not look like a good one
            raise
        return backup
    raise ValueError(f"no free backup name beside {path}")


def always_load(root, findings):
    """Rule files the index (INDEX.md "Always load") requires, so the check follows the index."""
    index = root / "INDEX.md"
    if not is_file(index):
        findings.append(f"missing index: {index}")
        return []
    links = session_context.always_load(index.read_text(encoding="utf-8"))
    if not links:
        findings.append(f"index lists no always-load rule files: {index}")
    return [root / link for link in links]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    # The default keeps the path this script was run by, so a stable link stays in hook entries.
    parser.add_argument("--root", type=Path, default=Path(os.path.abspath(__file__)).parents[1])
    parser.add_argument("--install-hooks", action="store_true",
                        help="write the session-context hook into each Claude Code and Codex hook file")
    parser.add_argument("--command", type=Path,
                        help="register hooks that run through the House Rules command at this absolute path")
    args = parser.parse_args(argv)
    if args.command is not None and (not args.command.is_absolute() or args.command.name != "house-rules"):
        parser.error("--command needs the absolute path of the installed house-rules command")
    root, started = args.root.resolve(), time.monotonic()
    # Hook entries name the root as given, so a stable link to a moving checkout stays valid.
    hook_root = Path(os.path.abspath(args.root))
    failed = False
    findings = []
    selected = []
    verification_complete = False
    # A failed checkout update must not prevent checking the installed homes.
    generator = []

    def load():
        # The selected checkout's generator, read after its update, builds the entry its sessions run.
        script = root / "scripts/session_context.py"
        if not is_file(script):
            raise ValueError(f"hook generator is missing: {script}; no hook file written")
        generator.append(load_session_context(script))

    def install():
        if not generator:
            raise ValueError("hook generator was not loaded; no hook file written")
        install_hooks(root, hook_root, Path.home(), sys.executable, lambda event, path, backup: print(
            f"BACKUP: {backup} (copy of {path})" if event == "backup" else
            f"REGISTERED: session context hook in {path}"), generator[0],
            str(args.command) if args.command else None)

    for name, operation in (
            ("update", lambda: findings.extend(update(root))),
            ("load", load),
            *((("install", install),) if args.install_hooks else ()),
            ("verification", lambda: verify(root, Path.home(), findings, selected, hook_root,
                                            generator[0] if generator else None))):
        try:
            operation()
        except (OSError, ValueError, RecursionError, subprocess.CalledProcessError) as error:
            failed = True
            print(f"ERROR: {error}", file=sys.stderr)
            if isinstance(error, subprocess.CalledProcessError):
                print(error.stderr, file=sys.stderr)
        else:
            if name == "verification":
                verification_complete = True
    for message in findings:
        print(f"REPORT: {message}")
    coverage = ("homes checked" if verification_complete else
                "homes selected; verification incomplete")
    print(f"{len(selected)} {coverage}; {len(findings)} findings; "
          f"{time.monotonic() - started:.3f}s")
    return 2 if failed else 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
