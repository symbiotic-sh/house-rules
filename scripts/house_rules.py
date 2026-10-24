#!/usr/bin/env python3
"""House Rules command: resolve a repository's source layers and run House Rules scripts at that version.

The contract is docs/design/218-layered-configuration.md. A repository's house-rules.json lists ordered source
layers (House Rules first, then custom rule and setting repositories), each at a branch, which always resolves to
its latest commit, at a commit, a deliberate pin, or at an exact release tag such as v1.4.0, which resolves like a
branch. This file resolves them into a per-user version cache, builds
the effective configuration with the layer of every value, and logs what it resolved. It keeps no "current
version": the repository's file, else the per-user file's default sources, else House Rules main decides.

The checks runner imports this file as its resolver. It uses only the standard library and runs on CPython 3.9
or later.

  house-rules resolve [--repo PATH] [--rev COMMIT] [--json]
  house-rules path [--repo PATH]
  house-rules exec [--repo PATH] [--hook] [--origin index | --rev COMMIT] PATH [ARGUMENT]...
  house-rules link CHECKOUT [--url URL] | unlink CHECKOUT|URL
  house-rules cache list
  house-rules init [--dry-run] [--profile NAME]...
  house-rules install [--dry-run] | version
"""
import argparse
import calendar
import contextlib
try:
    import fcntl  # Unix only: the lock of links.json; a session hook that only reads the handed record never needs it
except ImportError:  # native Windows
    fcntl = None
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

DEFAULT_URL = "https://github.com/symbiotic-build/house-rules"
CONFIG = "house-rules.json"
VERSION = 2        # the repository configuration version this command knows
FILE_VERSION = 1   # the version of layer files and the global configuration
RULESET = "ruleset.json"  # a rule repository's defaults, at its root
COMMAND = "scripts/house_rules.py"
MAX_RECORD_BYTES = 64 * 1024  # the serialized resolution record fits in one environment value (HOUSE_RULES_RESOLVED)
REUSE_SECONDS = 60          # a branch answer younger than this is reused without asking the remote
RESOLVED = "HOUSE_RULES_RESOLVED"
OVERRIDE = "HOUSE_RULES_REVISION"
LINK = "HOUSE_RULES_LINK"
DEADLINE = "HOUSE_RULES_DEADLINE"  # set by exec --hook: the hook's deadline, in seconds since 1970-01-01 UTC
HOOK_ERROR = "HOUSE RULES HOOK ERROR:"  # the error prefix agent session hooks print, as session_context.py does
COMMIT = re.compile(r"[0-9a-f]{40}")  # a source layer's commit: sources are SHA-1 repositories for now
SHA256_COMMIT = re.compile(r"[0-9a-f]{64}")
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")  # an exact release tag; no version ranges
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TRANSPORT = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9+.-]*)?::")  # Git's <transport>::<address>, as Git detects it
# A fetch fails on lack of progress, never on elapsed time: below 1 byte/s for 30 s over HTTP.
PROGRESS = ("-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=30")
NO_REPLACE = {"GIT_NO_REPLACE_OBJECTS": "1"}
# Variables that bind a Git command to the calling repository; a hook inherits some of them.
LOCAL_GIT_ENV = ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT",
                 "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_GRAFT_FILE", "GIT_IMPLICIT_WORK_TREE",
                 "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX",
                 "GIT_REPLACE_REF_BASE", "GIT_SHALLOW_FILE", "GIT_WORK_TREE")
# Profiles `init` writes, from marker files at the repository root; no tool detects profiles at run time.
PROFILE_MARKERS = (("rust", ("Cargo.toml",)), ("typescript", ("package.json", "tsconfig.json")),
                   ("python", ("pyproject.toml",)), ("svelte", ("svelte.config.js",)),
                   ("specs", ("docs/ENTITIES.json",)))
REPOSITORY_FIELDS = ("version", "sources", "profiles", "budgets", "checks")
RULESET_FIELDS = ("version", "context", "checks", "budgets", "profiles")
PROFILE_FIELDS = ("context", "checks", "budgets")
GLOBAL_FIELDS = ("version", "sources", "budgets")
BUDGET_FIELDS = ("seconds", "reason")


class Failure(Exception):
    """A configuration or resolution failure: one line naming the cause and its repair, exit status 2."""

    def __init__(self, message, status=2):
        super().__init__(message)
        self.status = status


def utc(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def last_line(data):
    lines = data.decode(errors="replace").strip().splitlines()
    return lines[-1] if lines else "no output"


def git(cwd, *args, foreign=False, check=True, deadline=None, keep=()):
    """Run Git; `foreign` drops variables that bind it to a calling repository, as for the cache's own repositories,
    except those in `keep`, such as the index file of a pre-commit hook.

    With a deadline that has a limit, a command still running at it is stopped and DeadlinePassed is raised.
    """
    environ = dict(os.environ)
    if foreign:
        for name in LOCAL_GIT_ENV:
            if name not in keep:
                environ.pop(name, None)
    environ.update(NO_REPLACE)
    left = deadline.left() if deadline is not None else None
    if left is not None and left <= 0:
        raise DeadlinePassed()
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), env=environ, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=left)
    except subprocess.TimeoutExpired:
        raise DeadlinePassed()
    except OSError as exc:
        raise Failure(f"house-rules: cannot run git in {cwd} ({exc.strerror or exc}); install Git and run again")
    if check and proc.returncode:
        raise Failure(f"house-rules: git {' '.join(args[:2])} failed in {cwd} ({last_line(proc.stderr)})")
    return proc


# Folders and keys

def cache_root():
    if os.environ.get("HOUSE_RULES_CACHE"):
        return Path(os.environ["HOUSE_RULES_CACHE"]).resolve()
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]).resolve() / "house-rules"
    return Path.home() / ".cache" / "house-rules"


def config_home():
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]).resolve() / "house-rules"
    return Path.home() / ".config" / "house-rules"


def credentials(url, what, status=2):
    """True when a URL carries a password, or any user name over http or https; an ssh login is part of the URL.

    A URL Python cannot parse, such as one with a bad port or IPv6 authority, is a configuration error. Neither
    message shows the URL, which may hold the credentials; `what` names where it came from.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # parsing the port is where a malformed authority shows
    except ValueError:
        raise Failure(f"house-rules: {what} is not a URL Git can use; check its host and port", status=status)
    if "@" not in parts.netloc:
        return False
    return parts.password is not None or parts.scheme.lower() in ("http", "https")


def encodable(text):
    """True when `text` can become a process argument and a cache key: no lone surrogate, which JSON allows."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_url(url, what, status=2):
    """Refuse a source URL no tool should fetch or store; `what` names it, because the URL itself may hold secrets."""
    if not url:
        raise Failure(f"house-rules: {what} is empty", status=status)
    if not encodable(url):
        raise Failure(f"house-rules: {what} is not valid text", status=status)
    if "\0" in url:
        raise Failure(f"house-rules: {what} contains a NUL character", status=status)
    if TRANSPORT.match(url):  # the address inside escapes every check, so the form is refused
        raise Failure(f"house-rules: {what} uses Git's <transport>:: form, which this command does not accept; use a "
                      "plain URL and a Git credential helper", status=status)
    if credentials(url, what, status):
        raise Failure(f"house-rules: {what} carries credentials; use a Git credential helper", status=status)


def digest(text):
    """The lowercase hexadecimal SHA-256 of `text`'s UTF-8 bytes, undecodable bytes kept: a cache file name."""
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def source_folder(url):
    """sources/<SHA-256 of the URL as written>/: every folder name has one length and none collides with another.

    Two spellings of one URL are two sources; an ssh login is part of the URL and keeps two accounts apart.
    """
    return cache_root() / "sources" / digest(url)


def tree_folder(url, commit):
    """A commit's files; a commit identifies its content, so every source shares one folder per commit."""
    return cache_root() / "revisions" / commit


def prepared(tree):
    """A tree is ready when its folder exists: it is renamed into place whole."""
    return Path(tree).is_dir()


def partial(folder):
    folder.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".partial-", dir=str(folder)))


class Deadline:
    """Hook mode's limit on ls-remote and fetches: 2 s before the agent product's 10 s hook timeout.

    Outside hook mode there is no deadline; Git's own progress limit (PROGRESS) is the only one.
    """

    def __init__(self, seconds=None):
        self.seconds = seconds
        self.end = None if seconds is None else time.monotonic() + seconds

    def left(self):
        return None if self.end is None else max(0.0, self.end - time.monotonic())


HOOK_SECONDS = 8   # the hook timeout, 10 s, minus 2 s to print what happened
NO_DEADLINE = Deadline()


class DeadlinePassed(Exception):
    pass


# Reading and checking configuration files

def load_json(data, where):
    try:
        value = json.loads(data)
    except UnicodeDecodeError as exc:
        raise Failure(f"house-rules: {where}: not UTF-8 ({exc.reason})")
    except json.JSONDecodeError as exc:
        raise Failure(f"house-rules: {where}: invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}")
    except ValueError as exc:  # such as an integer longer than Python converts
        raise Failure(f"house-rules: {where}: invalid JSON: {exc}")
    if not isinstance(value, dict):
        raise Failure(f"house-rules: {where}: the file must hold one JSON object")
    return value


def known_fields(value, allowed, where):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise Failure(f"house-rules: {where}: unknown field {unknown[0]}; allowed: {', '.join(allowed)}")


def check_version(value, where, version=FILE_VERSION):
    if value.get("version") != version:
        if isinstance(value.get("version"), int) and value["version"] > version:
            raise Failure(f"house-rules: {where} has version {value['version']}; this house-rules command knows "
                          f"version {version}; install the newer command: python3 <House Rules checkout>/scripts/house_rules.py install")
        raise Failure(f"house-rules: {where}: version must be {version}")


def check_sources(sources, where):
    """Validate a sources list and return it with defaults filled in: url for house-rules, ref main."""
    if not isinstance(sources, list) or not sources:
        raise Failure(f"house-rules: {where}: sources must be a non-empty list of layer entries")
    result, names = [], set()
    for index, entry in enumerate(sources):
        at = f"{where}: sources[{index}]"
        if not isinstance(entry, dict):
            raise Failure(f"house-rules: {at}: a layer entry is an object with name, url and ref")
        known_fields(entry, ("name", "url", "ref"), at)
        name = entry.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise Failure(f"house-rules: {at}: name must be lower kebab case, such as team-rules")
        if index == 0 and name != "house-rules":
            raise Failure(f"house-rules: {where}: sources[0] must be house-rules; House Rules is always the first layer")
        if index and name == "house-rules":
            raise Failure(f"house-rules: {at}: only the first layer may be named house-rules")
        if name in names:
            raise Failure(f"house-rules: {at}: the name {name} is used twice")
        names.add(name)
        url = entry.get("url", DEFAULT_URL if name == "house-rules" else None)
        if not isinstance(url, str) or not url:
            raise Failure(f"house-rules: {at}: layer {name} needs a url")
        check_url(url, f"{at}: the URL of {name}")
        ref = entry.get("ref", "main")
        if not isinstance(ref, str) or not ref or "\0" in ref or not encodable(ref):
            raise Failure(f"house-rules: {at}: ref must be a branch name, a full commit identity or a release tag")
        if SHA256_COMMIT.fullmatch(ref.lower()):
            raise Failure(f"house-rules: {name}: SHA-256 sources are not supported yet")
        if not COMMIT.fullmatch(ref.lower()) and git(".", "check-ref-format", "--branch", ref,
                                                    foreign=True, check=False).returncode:
            raise Failure(f"house-rules: {at}: {ref!r} is not a branch name Git accepts")
        result.append({"name": name, "url": url, "ref": ref.lower() if COMMIT.fullmatch(ref.lower()) else ref})
    return result


def check_map(value, field, where):
    if not isinstance(value, dict) or not all(isinstance(entry, dict) for entry in value.values()):
        raise Failure(f"house-rules: {where}: {field} must map each key to an object")
    return value


def check_budgets(budgets, where):
    """A budget entry replaces its stage's entry whole, so every entry is complete: a stage, positive seconds."""
    check_map(budgets, "budgets", where)
    for stage, entry in budgets.items():
        if stage not in STAGES:
            raise Failure(f"house-rules: {where}: budgets.{stage}: the stages are {', '.join(STAGES)}")
        known_fields(entry, BUDGET_FIELDS, f"{where}: budgets.{stage}")
        seconds = entry.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0 \
                or (isinstance(seconds, float) and not math.isfinite(seconds)):
            raise Failure(f"house-rules: {where}: budgets.{stage}.seconds must be a positive number")
        if "reason" in entry and not (isinstance(entry["reason"], str) and entry["reason"].strip()):
            raise Failure(f"house-rules: {where}: budgets.{stage}.reason must be text")
    return budgets


def repository_config(data, where=CONFIG):
    """Parse a repository configuration; return it with sources defaulted to None and the other fields filled in."""
    value = load_json(data, where)
    check_version(value, where, VERSION)
    known_fields(value, REPOSITORY_FIELDS, where)
    if "sources" not in value:
        raise Failure(f"house-rules: {where}: sources is required; list at least "
                      '{"name": "house-rules", "ref": "main"}')
    profiles = value.get("profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(p, str) and NAME.fullmatch(p) for p in profiles) \
            or len(set(profiles)) != len(profiles):
        raise Failure(f"house-rules: {where}: profiles must be a list of distinct lower kebab case names")
    checks = check_map(value.get("checks", {}), "checks", where)
    for identity, entry in checks.items():
        if "enforced" in entry:
            raise Failure(f"house-rules: {where}: checks.{identity}.enforced: only a layer file decides whether a "
                          "check refuses or warns")
    return {"sources": check_sources(value["sources"], where), "profiles": profiles,
            "budgets": check_budgets(value.get("budgets", {}), where), "checks": checks}


def ruleset_entry(value, where):
    """The context, checks and budgets of a ruleset's top level or one profile entry."""
    context = value.get("context", [])
    if not isinstance(context, list) or not all(isinstance(p, str) and p.endswith(".md") and not p.startswith("/")
                                                and ".." not in Path(p).parts for p in context):
        raise Failure(f"house-rules: {where}: context must list Markdown files inside the layer")
    return {"checks": check_map(value.get("checks", {}), "checks", where),
            "budgets": check_budgets(value.get("budgets", {}), where), "context": context}


def ruleset(data, where):
    """Parse a ruleset.json: its top level and {profile: entry}, each with context, checks and budgets."""
    value = load_json(data, where)
    if "sources" in value:
        raise Failure(f"house-rules: {where}: only house-rules.json lists sources")
    check_version(value, where)
    known_fields(value, RULESET_FIELDS, where)
    profiles = check_map(value.get("profiles", {}), "profiles", where)
    entries = {}
    for name, entry in profiles.items():
        if not NAME.fullmatch(name):
            raise Failure(f"house-rules: {where}: profiles.{name}: a profile name is lower kebab case")
        known_fields(entry, PROFILE_FIELDS, f"{where}: profiles.{name}")
        entries[name] = ruleset_entry(entry, f"{where}: profiles.{name}")
    return {"top": ruleset_entry(value, where), "profiles": entries}


def global_path():
    return config_home() / CONFIG


def global_config(warnings, need_sources=False):
    """The global configuration, or None. A problem in it only warns, except when its sources are needed."""
    path = global_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        data, error = None, Failure(f"house-rules: cannot read {path} ({exc.strerror or exc})")
    try:
        if data is None:
            raise error
        value = load_json(data, str(path))
        check_version(value, str(path))
        known_fields(value, GLOBAL_FIELDS, str(path))
        return {"sources": check_sources(value["sources"], str(path)) if "sources" in value else None,
                "budgets": check_budgets(value.get("budgets", {}), str(path))}
    except Failure as failure:
        if need_sources:
            raise
        warnings.append(f"warning: {str(failure)[len('house-rules: '):]}; ignoring the global configuration")
        return None


# Finding the repository configuration

def find_repository(path=None):
    """The Git top level of `path`, or None outside a working tree.

    Inherited GIT_DIR, GIT_WORK_TREE and similar bindings are dropped, so the folder named decides, not a hook's
    environment.
    """
    if path is not None and not Path(path).is_dir():
        return None
    proc = git(path or ".", "rev-parse", "--show-toplevel", check=False, foreign=True)
    if proc.returncode:
        return None
    return os.fsdecode(proc.stdout).rstrip("\n")


def read_configuration(repository, rev=None):
    """Return (configuration bytes or None, origin) from the working tree, or from commit `rev`."""
    if rev is None:
        path = Path(repository) / CONFIG
        try:
            return path.read_bytes(), {"origin": "working tree", "commit": None}
        except FileNotFoundError:
            return None, {"origin": "working tree", "commit": None}
        except OSError as exc:
            raise Failure(f"house-rules: cannot read {path} ({exc.strerror or exc}); repair access and run again")
    commit = git(repository, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False, foreign=True)
    if commit.returncode:
        raise Failure(f"house-rules: {rev} is no commit in {repository}")
    commit = commit.stdout.decode().strip()
    return staged_or_committed(repository, ["ls-tree", "-z", commit, "--", CONFIG], 2, f"commit {commit}"), \
        {"origin": "commit", "commit": commit}


def staged_or_committed(repository, listing, column, where):
    """The bytes of house-rules.json in a commit, or None when it has none.

    `listing` is the ls-tree command that lists the file, and `column` the field holding its object.
    A listing or read that Git fails is a failure that names it, never a missing file.
    """
    listed = git(repository, *listing, check=False, foreign=True)
    if listed.returncode:
        raise Failure(f"house-rules: cannot read {CONFIG} from {where} in {repository} ({last_line(listed.stderr)})")
    if not listed.stdout:
        return None
    blob = listed.stdout.split(b"\t", 1)[0].split(b" ")[column].decode()
    shown = git(repository, "cat-file", "blob", blob, check=False, foreign=True)
    if shown.returncode:
        raise Failure(f"house-rules: cannot read {CONFIG} from {where} in {repository} ({last_line(shown.stderr)})")
    return shown.stdout


def staged_configuration(repository):
    """(configuration bytes or None, origin) from the staged index, for the pre-commit hook.

    The hook keeps Git's GIT_INDEX_FILE, so a partial commit reads the index it commits.
    """
    keep = ("GIT_INDEX_FILE",)
    shown = git(repository, "show", f":{CONFIG}", check=False, foreign=True, keep=keep)
    if not shown.returncode:
        return shown.stdout, {"origin": "index", "commit": None}
    listed = git(repository, "ls-files", "-z", "--", CONFIG, check=False, foreign=True, keep=keep)
    if not listed.returncode and not listed.stdout:  # nothing is staged at or below that path
        return None, {"origin": "index", "commit": None}
    raise Failure(f"house-rules: cannot read {CONFIG} from the index in {repository} ({last_line(shown.stderr)})")


# Resolving one layer

def ask_ref(url, reference, deadline=NO_DEADLINE):
    """Return (the commit `reference` names, None), or (None, "missing"), or (None, Git's error).

    An annotated tag's own line names the tag object; its peeled ^{} line names the commit, so that one wins.
    """
    peeled = reference + "^{}"
    cache_root().mkdir(parents=True, exist_ok=True)  # Git runs there, outside any clone and its configuration
    proc = git(cache_root(), *PROGRESS, "ls-remote", url, reference, peeled, foreign=True, check=False,
               deadline=deadline)
    if proc.returncode:
        return None, last_line(proc.stderr)
    # Records end at LF only: a branch name may hold other line separators that Git accepts.
    lines = (line.split(b"\t", 1) for line in proc.stdout.split(b"\n") if b"\t" in line)
    heads = {name: commit for commit, name in lines}
    commit = heads.get(os.fsencode(peeled), heads.get(os.fsencode(reference)))
    if commit is None:
        return None, "missing"
    commit = commit.decode("ascii", "replace")
    if not COMMIT.fullmatch(commit):
        return None, f"ls-remote answered {commit!r}"
    return commit, None


def reachable(url, deadline=NO_DEADLINE):
    """Git's error when the remote cannot be reached at all, else None."""
    proc = git(cache_root(), *PROGRESS, "ls-remote", "--heads", url, "refs/heads/main", foreign=True, check=False,
               deadline=deadline)
    return last_line(proc.stderr) if proc.returncode else None


ANSWER_FOLDERS = {"branch": "branches", "tag": "tags"}


def answer_path(url, kind, name):
    """One file per branch or release tag: branches/ or tags/ <SHA-256 of the name>.json in the source's folder."""
    return source_folder(url) / ANSWER_FOLDERS[kind] / f"{digest(name)}.json"


def read_answer(url, kind, name):
    """The recorded answer for one branch or tag, as {"url", kind, "revision", "fetched", "seconds"}, or None.

    The checks runner writes the same branch records, so both share one answer. A record belongs to the source
    whose key hash names its folder; its url, stored as messages show it, is for display only.
    """
    try:
        record = json.loads(answer_path(url, kind, name).read_text())
        seconds = calendar.timegm(time.strptime(record["fetched"], "%Y-%m-%dT%H:%M:%SZ"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (isinstance(record, dict) and isinstance(record.get("url"), str) and record.get(kind) == name
            and isinstance(record.get("revision"), str) and COMMIT.fullmatch(record["revision"])):
        return None
    return dict(record, seconds=seconds)


def write_json(path, value):
    """Replace `path` with `value` whole; a failed write removes its staging file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".partial-write-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle)
        os.replace(temporary, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def write_answer(url, kind, record):
    write_json(answer_path(url, kind, record[kind]), record)  # the record's url never holds credentials


def extract(repository, commit, folder, deadline=NO_DEADLINE):
    environ = dict({k: v for k, v in os.environ.items() if k not in LOCAL_GIT_ENV}, **NO_REPLACE)
    archive = subprocess.Popen(["git", "archive", "--format=tar", commit], cwd=str(repository),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=environ)
    try:
        tar = subprocess.run(["tar", "-xf", "-", "-C", str(folder)], stdin=archive.stdout, capture_output=True,
                             timeout=deadline.left())
    except subprocess.TimeoutExpired:
        archive.kill()
        archive.communicate()
        raise DeadlinePassed()
    except OSError as exc:
        archive.kill()
        archive.communicate()
        raise Failure(f"house-rules: cannot start tar ({exc.strerror or exc}); install tar and run again")
    archive.stdout.close()
    error = archive.stderr.read()
    archive.stderr.close()
    if archive.wait() or tar.returncode:
        raise Failure(f"house-rules: cannot extract {commit} ({last_line(error + tar.stderr)}); run again")


def fetch_commit(url, commit, deadline=NO_DEADLINE):
    """Put one commit's files into the cache unless they are there; return None, or Git's error.

    Each fetch uses its own new SHA-1 bare repository in the source's folder, fetches the commit with depth 1 and
    extracts it into a temporary folder there, then renames the folder into place, so a tree at its ready path is
    always complete. An existing tree is never replaced: a fetch that loses the rename removes its copy and uses the
    winner's tree.
    """
    final = tree_folder(url, commit)
    if prepared(final):
        return None
    repository = partial(source_folder(url))
    cause = temporary = None  # the fetch's own failure, kept when cleaning up fails as well
    try:
        git(repository, "init", "--quiet", "--bare", "--template=", "--object-format=sha1", ".", foreign=True)
        proc = git(repository, *PROGRESS, "fetch", "--quiet", "--depth=1", "--no-tags", url,
                   f"{commit}:refs/pins/{commit}", foreign=True, check=False, deadline=deadline)
        if proc.returncode:
            cause = last_line(proc.stderr)
            return cause
        temporary = partial(source_folder(url))
        extract(repository, commit, temporary, deadline)
        for folder, _, names in os.walk(str(temporary)):
            for name in names:
                path = os.path.join(folder, name)
                if not os.path.islink(path):
                    os.chmod(path, os.stat(path).st_mode & ~0o222)
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(str(temporary), str(final))
        except OSError:
            if not final.is_dir():
                raise
            remove_tree(temporary)  # another process placed the same commit first
        return None
    except (Failure, OSError) as exc:
        cause = str(exc)
        raise
    except DeadlinePassed:
        cause = "the deadline passed"
        raise
    finally:
        left = []  # every folder is tried, so one that cannot be removed never keeps another
        for folder in (temporary, repository):  # the extraction is already gone when the rename published it
            try:
                if folder is not None:
                    remove_tree(folder)
            except OSError as exc:
                left.append(f"{folder} ({exc.strerror or exc})")
        if left:
            earlier = f"; the fetch had failed first: {cause}" if cause else ""
            raise Failure(f"house-rules: cannot remove the temporary fetch folder {' and '.join(left)}{earlier}; "
                          "remove it by hand and run again")


def checkout_state(path):
    """The working tree root's commit and whether it has changes, or None when `path` is not a working tree's root."""
    top = git(path, "rev-parse", "--show-toplevel", foreign=True, check=False)
    if top.returncode or os.path.realpath(os.fsdecode(top.stdout).rstrip("\n")) != os.path.realpath(str(path)):
        return None
    head = git(path, "rev-parse", "HEAD", foreign=True, check=False)
    if head.returncode:
        return None
    dirty = git(path, "status", "--porcelain", "--untracked-files=normal", foreign=True, check=False)
    if dirty.returncode:
        raise Failure(f"house-rules: cannot read the state of the linked checkout {path} "
                      f"({last_line(dirty.stderr)}); repair the checkout or unlink it")
    return {"path": str(path), "head": head.stdout.decode().strip(), "dirty": bool(dirty.stdout.strip())}


def ref_kind(ref):
    return "commit" if COMMIT.fullmatch(ref) else "tag" if TAG.fullmatch(ref) else "branch"


def linked_layer(entry, path, how):
    state = checkout_state(path) if Path(path).is_dir() else None
    if state is None:
        raise Failure(f"house-rules: link {entry['url']}: {path} is not the root of a Git working tree; "
                      f"run house-rules unlink {path}" if how == "links.json"
                      else f"house-rules: {LINK}={path} is not the root of a Git working tree; unset it or name one")
    changes = " with uncommitted changes" if state["dirty"] else ""
    warning = (f"warning: LINKED {entry['name']} -> {path} at {state['head']}{changes}; "
               "this run does not use the fetched source")
    layer = dict(entry, ref_kind=ref_kind(entry["ref"]), revision=state["head"], source="link", fetched=None,
                 tree=str(path), link=state)
    return layer, warning


def offline_text(error, url):
    return error if error.startswith("the remote did not answer") else f"cannot reach {url} ({error})"


def resolve_layer(entry, links, warnings, deadline=NO_DEADLINE):
    """Resolve one layer entry to {name, url, ref, ref_kind, revision, source, fetched, tree, link}.

    For the house-rules layer, the one-run variables HOUSE_RULES_LINK and HOUSE_RULES_REVISION win over links.json.
    A branch falls back to its last answer only when the remote cannot be asked (offline, or the hook deadline);
    a remote that names a commit it then refuses to send is a failure, never a quiet return to older rules.
    """
    url, ref, name = entry["url"], entry["ref"], entry["name"]
    override = os.environ.get(OVERRIDE) if name == "house-rules" else None
    if name == "house-rules" and os.environ.get(LINK):
        layer, warning = linked_layer(entry, os.path.abspath(os.path.expanduser(os.environ[LINK])), LINK)
        warnings.append(warning)
        return layer
    if override is None and url in links:
        layer, warning = linked_layer(entry, links[url], "links.json")
        warnings.append(warning)
        return layer
    if override is not None:
        if not COMMIT.fullmatch(override.lower()):
            raise Failure(f"house-rules: {OVERRIDE}={override!r} is not a full commit identity; set a full commit "
                          "or unset it", status=1)
        commit, source = override.lower(), "override"
        warnings.append(f"warning: TEST OVERRIDE {OVERRIDE}={commit}; this run does not use house-rules {ref}")
    elif COMMIT.fullmatch(ref):
        commit, source = ref, "pinned"
    else:
        commit, source = None, None
    kind = ref_kind(ref)
    try:
        if commit is not None and source in ("pinned", "override"):
            fetch_pinned(entry, commit, deadline)
            fetched = None  # a pin asks nothing, so it has no answer time
        else:
            commit, source, fetched = resolve_named(entry, kind, warnings, deadline)
        return dict(entry, ref_kind=kind, revision=commit, source=source, fetched=fetched,
                    tree=str(tree_folder(url, commit)), link=None)
    except OSError as exc:
        raise Failure(f"house-rules: cannot write the cache {cache_root()} ({exc.strerror or exc}); free space, "
                      "make it writable or set HOUSE_RULES_CACHE")


def fetch_pinned(entry, commit, deadline):
    """Make a pinned commit's files available; a commit that cannot be fetched is a failure naming why."""
    url, name = entry["url"], entry["name"]
    if prepared(tree_folder(url, commit)):
        return
    try:
        error = fetch_commit(url, commit, deadline)
    except DeadlinePassed:
        error = f"the remote did not answer within {HOOK_SECONDS} s"
    if error:
        try:
            offline = error.startswith("the remote did not answer") or reachable(url, deadline)
        except DeadlinePassed:
            offline = True
        if offline:
            raise Failure(f"house-rules: {offline_text(error, url)} and {name} {commit} is not cached; "
                          "run again when online")
        raise Failure(f"house-rules: {name}: commit {commit} not found at {url} ({error}); "
                      "check the ref")


def resolve_named(entry, kind, warnings, deadline):
    """(commit, source, fetched) for a branch or release tag: reused, fetched, or offline from the last answer.

    A tag is asked on every run, like a branch, so a tag that moves on the remote is followed, as CI with an empty
    cache does. `fetched` is the time of the answer used.
    """
    url, ref, name = entry["url"], entry["ref"], entry["name"]
    noun, reference = ("branch", f"refs/heads/{ref}") if kind == "branch" else ("release tag", f"refs/tags/{ref}")
    record, error = read_answer(url, kind, ref), None
    if record and 0 <= time.time() - record["seconds"] < REUSE_SECONDS and \
            prepared(tree_folder(url, record["revision"])):
        return record["revision"], "reused", record["fetched"]
    asked = utc()  # the answer's time is when the remote gave it, not when preparation ended
    try:
        commit, error = ask_ref(url, reference, deadline)
        if error == "missing":
            raise Failure(f"house-rules: {name}: no {noun} {ref} at {url}; check the ref in {CONFIG}")
        if not error:
            try:
                failed = fetch_commit(url, commit, deadline)
            except DeadlinePassed:
                failed, error = None, f"the remote did not answer within {HOOK_SECONDS} s"
            if failed:
                raise Failure(f"house-rules: {name}: {url} names {commit} for {ref} but cannot send it "
                              f"({failed}); run again, or check the server")
            if not error:
                # Published only now that the commit's tree is complete.
                write_answer(url, kind, {"url": url, kind: ref, "revision": commit, "fetched": asked})
                return commit, "fetched", asked
    except DeadlinePassed:
        error = f"the remote did not answer within {HOOK_SECONDS} s"
    if record is None or not prepared(tree_folder(url, record["revision"])):
        raise Failure(f"house-rules: {offline_text(error, url)} and no earlier fetch of {name} {ref} is cached; "
                      "run again when online")
    warnings.append(f"warning: {offline_text(error, url)}; using {name} {ref} as last answered "
                    f"{record['fetched']}, {record['revision']}")
    return record["revision"], "offline", record["fetched"]


# Links

def links_path():
    return config_home() / "links.json"


def read_links():
    """links.json as {source key: checkout path}; a malformed file is a failure that names it."""
    path = links_path()
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise Failure(f"house-rules: cannot read {path} ({exc}); repair or delete it")
    entries = value.get("links") if isinstance(value, dict) and value.get("version") == 1 else None
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and set(e) == {"key", "path"} and isinstance(e["key"], str) and e["key"]
            and isinstance(e["path"], str) and e["path"] for e in entries):
        raise Failure(f"house-rules: {path} is not a links file (version 1, a list of key and path); "
                      "repair or delete it")
    return {link["key"]: link["path"] for link in entries}


@contextlib.contextmanager
def links_lock():
    """Serialize link and unlink: each reads, changes and replaces links.json under this lock."""
    config_home().mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(config_home() / "links.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def write_links(links):
    path = links_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"version": 1, "links": [{"key": key, "path": links[key]} for key in sorted(links)]})


# Resolution

def default_sources(configured):
    if configured and configured["sources"]:
        return configured["sources"], "global"
    return [{"name": "house-rules", "url": DEFAULT_URL, "ref": "main"}], "default"


def resolve(repository=None, rev=None, data=None, origin=None, deadline=NO_DEADLINE, machine=True):
    """Resolve the layers for `repository` (or for no repository) and return the resolution record.

    The configuration is `data` when the caller read it (the checks runner reads it from its snapshot, with its
    own `origin`; an origin without data means that snapshot has none), else the file at commit `rev`, else the
    working tree's file. The record's `context` lists every layer's context files in layer order, House Rules'
    always-load rules first. Its serialized JSON is at most MAX_RECORD_BYTES, so it fits in one environment value.
    `machine` reads this machine's global configuration and keeps its warnings; CI passes False.
    """
    if os.environ.get(LINK) and os.environ.get(OVERRIDE):
        raise Failure(f"house-rules: set {LINK} or {OVERRIDE}, not both", status=1)
    warnings, config = [], None
    if repository is not None and data is None and origin is None:
        data, origin = read_configuration(repository, rev)
    if data is not None:
        config = repository_config(data)
    # Read once: its sources when the repository names none, its budgets on this machine.
    configured = global_config(warnings, need_sources=config is None) if config is None or machine else None
    if config is not None:
        sources = config["sources"]
    else:
        sources, kind = default_sources(configured)
        origin = {"origin": kind, "commit": None}
    links = read_links()
    layers = [resolve_layer(entry, links, warnings, deadline) for entry in sources]
    record = {"version": 1, "repository": repository, "configuration": origin,
              "profiles": config["profiles"] if config else [], "layers": layers, "context": [],
              "warnings": warnings}
    # Every resolution builds the effective configuration, so a broken ruleset or unknown profile fails here.
    built = effective(record, config, repository, hooks=machine, configured=configured)
    record["context"] = [{"layer": label, "path": str(Path(path).relative_to(layer_tree(record, label.split("/")[0])))}
                         for label, path in built["context"]]
    warnings.extend(built["warnings"])
    size = len(json.dumps(record).encode())
    if size > MAX_RECORD_BYTES:
        counts = {}
        for item in record["context"]:
            counts[item["layer"].split("/")[0]] = counts.get(item["layer"].split("/")[0], 0) + 1
        most = max(counts, key=counts.get) if counts else layers[0]["name"]
        raise Failure(f"house-rules: the record for this run is {size} bytes, over the 64 KiB limit; {most} lists "
                      f"the most context files ({counts.get(most, 0)})")
    if config is None:
        point_default(layers[0])
    return record


def point_default(layer):
    """Move the cache's `default` link to the House Rules tree of a resolution of the global default sources.

    A linked checkout becomes the default too, so every consumer of `default` reads what the resolution chose.
    Something other than a symbolic link at that path is never replaced; that, and any failure to move the link,
    is a failure that names the path.
    """
    link = cache_root() / "default"
    if link.is_symlink() and os.readlink(str(link)) == layer["tree"]:
        return
    if link.exists() and not link.is_symlink():
        raise Failure(f"house-rules: {link} is not a symbolic link; move it away so the global default can be "
                      "published there, and run again")
    temporary = cache_root() / f".default-{os.getpid()}"
    try:
        cache_root().mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        os.symlink(layer["tree"], str(temporary))
        os.replace(str(temporary), str(link))
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise Failure(f"house-rules: cannot point {link} at {layer['tree']} ({exc.strerror or exc}); make the cache "
                      "writable and run again")


def handed():
    """The resolution a starting tool passed in HOUSE_RULES_RESOLVED, or None."""
    try:
        record = json.loads(os.environ.get(RESOLVED) or "null")
    except ValueError:
        return None
    if isinstance(record, dict) and record.get("version") == 1 and isinstance(record.get("layers"), list):
        return record
    return None


def layer_tree(record, name="house-rules"):
    for layer in record["layers"]:
        if layer["name"] == name:
            return Path(layer["tree"])
    raise Failure(f"house-rules: no layer named {name}; the layers are "
                  f"{', '.join(layer['name'] for layer in record['layers'])}", status=1)


def describe_configuration(record):
    origin = record["configuration"]
    where = f"repository {record['repository']}, " if record["repository"] else "no repository, "
    if origin["origin"] in ("global", "default"):
        return f"house-rules: {where}configuration none ({'global default' if origin['origin'] == 'global' else 'default'} sources)"
    at = {"working tree": "working tree", "index": "index", "commit": f"commit {origin.get('commit')}",
          "base": f"base {origin.get('commit')}"}[origin["origin"]]
    return f"house-rules: {where}configuration {CONFIG} ({at})"


def describe_layer(layer, width):
    """One layer's line: name, URL, ref, commit, source and, for a branch or tag, when the remote answered."""
    name, url = layer["name"].ljust(width), layer["url"]
    if layer["source"] == "link":
        changes = " with uncommitted changes" if layer["link"]["dirty"] else ""
        return f"house-rules: layer {name} {url} ref {layer['ref']} link {layer['tree']} at {layer['revision']}{changes}"
    if layer["source"] == "pinned":
        return f"house-rules: layer {name} {url} ref {layer['ref']} (pinned)"
    if layer["source"] == "override":
        return f"house-rules: layer {name} {url} ref {layer['ref']} -> {layer['revision']} (override {OVERRIDE})"
    when = f"fetched {layer['fetched']}"
    detail = when if layer["source"] == "fetched" else f"{layer['source']}, {when}"
    return f"house-rules: layer {name} {url} ref {layer['ref']} -> {layer['revision']} ({detail})"


def describe(record):
    """The resolution record's text form: warnings, the configuration line, one line per layer, the profiles."""
    width = max(len(layer["name"]) for layer in record["layers"])
    lines = list(record["warnings"]) + [describe_configuration(record)]
    lines += [describe_layer(layer, width) for layer in record["layers"]]
    if record["profiles"]:
        lines.append(f"house-rules: profiles {', '.join(record['profiles'])}")
    return lines


# The effective configuration

STAGES = ("pre-commit", "pre-push", "ci")
SCOPES = ("tree", "files", "diff")
# A check field's type, as the checks runner's configuration fields define it.
CHECK_FIELDS = {"enabled": bool, "run": "strings", "stages": "stages", "scope": "scope", "paths": "strings",
                "requires": "strings", "fix": str, "reason": str, "enforced": bool}
COMPLETE_FIELDS = ("run", "stages", "paths", "fix")
PLACEHOLDERS = ("files", "base", "snapshot", "house_rules", "python", "layer")  # the runner's, and {layer}
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
HOOK_STAGES = ("pre-commit", "pre-push")


def check_entry(entry, where, enforced_allowed):
    """Refuse an unknown field or a value of the wrong type in one check entry, partial or complete."""
    for field, value in entry.items():
        kind = CHECK_FIELDS.get(field)
        if kind is None or (field == "enforced" and not enforced_allowed):
            raise Failure(f"house-rules: {where}: unknown check field {field}" if kind is None else
                          f"house-rules: {where}: enforced: only a layer file decides whether a check refuses or warns")
        if isinstance(kind, type):
            valid = isinstance(value, kind)
        elif kind == "scope":
            valid = value in SCOPES
        else:
            valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
            if kind == "stages":
                valid = valid and bool(value) and all(v in STAGES for v in value)
            if field == "run":
                valid = valid and bool(value)
        if field == "reason":
            valid = valid and bool(value.strip())
        if not valid:
            raise Failure(f"house-rules: {where}: {field} has the wrong type or value")


def check_identity(identity, where):
    if not NAME.fullmatch(identity):
        raise Failure(f"house-rules: {where}: check identity {identity} must be lower kebab case, such as cargo-deny")


def check_complete(identity, fields, where):
    """A complete definition, as the checks runner requires it: every needed field, known placeholders, {files}
    only as a whole argument of a files-scope check, and {snapshot} in every check that lists a hook stage."""
    values = {field: value[0] if isinstance(value, tuple) else value for field, value in fields.items()}
    missing = [field for field in COMPLETE_FIELDS if field not in values]
    if missing:
        raise Failure(f"house-rules: {where}: check {identity} has no {missing[0]}; a check needs run, stages, paths "
                      "and fix")
    run, scope = values["run"], values.get("scope", "tree")
    names = [name for arg in run for name in PLACEHOLDER.findall(arg)]
    unknown = [name for name in names if name not in PLACEHOLDERS]
    if unknown:
        raise Failure(f"house-rules: {where}: check {identity} uses unknown placeholder {{{unknown[0]}}}")
    if "files" in names and (scope != "files" or any("{files}" in arg and arg != "{files}" for arg in run)):
        raise Failure(f"house-rules: {where}: check {identity} may use {{files}} only as a whole argument of a "
                      "files-scope check")
    if scope == "files" and "{files}" not in run:
        raise Failure(f"house-rules: {where}: check {identity} has scope files but its run has no {{files}} argument")
    hooks = [stage for stage in values["stages"] if stage in HOOK_STAGES]
    if hooks and "snapshot" not in names:
        raise Failure(f"house-rules: {where}: check {identity} lists {hooks[0]} but its run has no {{snapshot}}; a "
                      "hook stage needs a check that reads the snapshot, otherwise list only ci")


def merge_entry(state, settings, section, key, entry, label, need_reason, earlier_defined):
    """Apply one entry field by field; record each value it sets with its layer, and require a reason for it.

    One rule: an entry that sets any field of a check an earlier step defined carries a reason, whether or not
    the value is equal, so every value after House Rules' top level says why it is there.
    """
    current = state[section].get(key)
    if current is None:
        check_complete(key, entry, label)  # an identity is introduced whole; a later file only changes it
        if label == "repository" and not isinstance(entry.get("reason"), str):
            raise Failure(f"house-rules: {CONFIG} checks.{key}: a repository check needs a reason")
        state[section][key] = {field: (value, label) for field, value in entry.items()}
        if earlier_defined:
            settings.append({"key": f"{section}.{key}", "value": entry, "layer": label, "replaces": None,
                             "reason": entry.get("reason")})
        return
    changed = [field for field in entry if field != "reason"]
    if changed and need_reason and not isinstance(entry.get("reason"), str):
        earlier = current[changed[0]][1] if changed[0] in current else current[next(iter(current))][1]
        raise Failure(f"house-rules: {label} {section}.{key}.{changed[0]}: sets a field that {earlier} set; add a reason")
    for field in changed:
        replaces = current[field][1] if field in current else None
        settings.append({"key": f"{section}.{key}.{field}", "value": entry[field], "layer": label,
                         "replaces": replaces, "reason": entry.get("reason")})
    for field, value in entry.items():
        if field == "reason" and not changed:
            continue
        current[field] = (value, label)


def merge_budget(state, settings, stage, entry, label, need_reason):
    """A budget entry replaces the earlier entry for its stage whole, and needs a reason, as a check entry does."""
    current = state["budgets"].get(stage)
    if current is not None and need_reason and not isinstance(entry.get("reason"), str):
        raise Failure(f"house-rules: {label} budgets.{stage}: sets a field that {current['seconds'][1]} set; add a reason")
    if current is not None or need_reason:
        settings.append({"key": f"budgets.{stage}", "value": entry, "layer": label,
                         "replaces": current["seconds"][1] if current else None, "reason": entry.get("reason")})
    state["budgets"][stage] = {field: (value, label) for field, value in entry.items()}


def read_ruleset(layer):
    """A layer's ruleset.json, parsed, or None when the layer has none."""
    path = Path(layer["tree"]) / RULESET
    root = os.path.realpath(layer["tree"])
    if os.path.lexists(str(path)) and not os.path.realpath(str(path)).startswith(root + os.sep):
        raise Failure(f"house-rules: {layer['name']} {RULESET} leaves the layer's tree; it must be a file inside it")
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Failure(f"house-rules: {layer['name']} {RULESET}: cannot read ({exc.strerror or exc})")
    return ruleset(data, f"{layer['name']} {RULESET}")


UNREAD = object()  # effective() reads the global configuration itself unless its caller already did


def effective(record, config=None, repository=None, hooks=False, configured=UNREAD):
    """Build the effective configuration: each layer's ruleset top level then its selected profiles, the
    repository's settings, and in hooks the global configuration. Every value keeps the label of the step that set
    it. Checks merge field by field, budgets replace per stage and context appends.

    Returns {"checks": {id: {field: value}}, "budgets": {...}, "budget_layers": {stage: label},
    "context": [(label, path)], "layers_of": {...},
    "settings": [...], "warnings": [...]}.
    """
    state = {"checks": {}, "budgets": {}}
    settings, context, warnings = [], [], []
    profiles = record["profiles"]
    defined = {profile: False for profile in profiles}
    first = True  # only House Rules' top level sets defaults without reasons; every later step's change is listed
    for layer in record["layers"]:
        content = read_ruleset(layer)
        steps = []
        if content is not None:
            steps = [(layer["name"], content["top"])]
            for profile in profiles:
                if profile in content["profiles"]:
                    defined[profile] = True
                    steps.append((f"{layer['name']}/{profile}", content["profiles"][profile]))
        root = os.path.realpath(layer["tree"])
        for label, content in steps:
            if content is not None:
                for key, entry in content["checks"].items():
                    check_identity(key, f"{label} checks")
                    check_entry(entry, f"{label} checks.{key}", True)
                    merge_entry(state, settings, "checks", key, entry, label, not first, not first)
                for stage, entry in content["budgets"].items():
                    merge_budget(state, settings, stage, entry, label, not first)
                for name in content["context"]:
                    path = Path(layer["tree"]) / name
                    real = os.path.realpath(str(path))
                    if not real.startswith(root + os.sep) or not os.path.isfile(real):
                        raise Failure(f"house-rules: {label}: context file {name} is not a file inside the layer")
                    context.append((label, str(path)))
            first = False
    missing = [profile for profile, found in defined.items() if not found]
    if missing:
        raise Failure(f"house-rules: profile {missing[0]} is defined by no layer "
                      f"({', '.join(layer['name'] for layer in record['layers'])})")
    if config is not None:
        for key, entry in config["checks"].items():
            check_identity(key, f"{CONFIG} checks")
            check_entry(entry, f"{CONFIG} checks.{key}", False)
        for key, entry in config["checks"].items():
            merge_entry(state, settings, "checks", key, entry, "repository", True, True)
        for stage, entry in config["budgets"].items():
            merge_budget(state, settings, stage, entry, "repository", True)
    for identity, fields in state["checks"].items():
        check_complete(identity, fields, next(iter(fields.values()), (None, "repository"))[1])
    if hooks:
        configured = global_config(warnings) if configured is UNREAD else configured
        for stage, entry in (configured or {}).get("budgets", {}).items():
            try:  # the one merge rule holds here too, but a machine file only warns
                merge_budget(state, settings, stage, entry, "global", True)
            except Failure as failure:
                warnings.append(f"warning: {str(failure)[len('house-rules: '):]}; the global budget is ignored")
    flat = {section: {key: {field: value for field, (value, _) in entry.items()} for key, entry in state[section].items()}
            for section in state}
    layers_of = {key: {field: label for field, (_, label) in entry.items()} for key, entry in state["checks"].items()}
    budget_layers = {stage: entry["seconds"][1] for stage, entry in state["budgets"].items()}
    return {"checks": flat["checks"], "budgets": flat["budgets"], "budget_layers": budget_layers,
            "context": context, "layers_of": layers_of,
            "settings": settings, "warnings": warnings}


def describe_settings(settings):
    lines = []
    for item in settings:
        value = json.dumps(item["value"], separators=(", ", ": "))
        reason = f" (reason: {item['reason']})" if item["reason"] else ""
        replaces = f"; replaces {item['replaces']}" if item["replaces"] else ""
        lines.append(f"  {item['key']} = {value}  from {item['layer']}{replaces}{reason}")
    return lines


# Commands

def cmd_resolve(args):
    record = resolve(args.repository, args.rev)
    stale = installed_differs(record)
    if stale:
        record["warnings"].append(stale)
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print("\n".join(describe(record)))
    return 0


def installed_differs(record):
    """A warning when this installed command differs from the command in the global default's House Rules tree."""
    own = Path(__file__).resolve()
    default = cache_root() / "default" / COMMAND
    if "revisions" in own.parts or not default.is_file():
        return None
    try:
        mine, theirs = own.read_bytes(), default.read_bytes()
        if mine[mine.index(b"\n"):] != theirs[theirs.index(b"\n"):]:  # the interpreter line differs by machine
            return ("warning: this house-rules command differs from the one in the global default's House Rules; "
                    f"install it: python3 {default} install")
    except (OSError, ValueError):
        return None
    return None


def command_repository(args):
    if args.repo:
        repository = find_repository(args.repo)
        if repository is None:
            raise Failure(f"house-rules: {args.repo} is not inside a Git working tree", status=1)
        return repository
    return find_repository()


def cmd_path(args):
    record = resolve(command_repository(args))
    print("\n".join(describe(record)), file=sys.stderr)  # standard output carries only the path
    print(layer_tree(record))
    return 0


def script_in(tree, relative):
    """The script `relative` inside `tree`; an absolute path, a .. step or a link that leaves the tree is refused."""
    if os.path.isabs(relative) or ".." in Path(relative).parts:
        raise Failure(f"house-rules: {relative} is not a path inside the House Rules tree; name it relative to the "
                      "tree, such as scripts/session_context.py", status=1)
    script = Path(tree) / relative
    root = os.path.realpath(str(tree))
    if not os.path.realpath(str(script)).startswith(root + os.sep):
        raise Failure(f"house-rules: {relative} leaves the House Rules tree {tree}; exec runs only scripts inside it",
                      status=1)
    return script


def cmd_exec(args):
    stdin = None
    folder = args.repo
    if args.hook:
        stdin = sys.stdin.buffer.read()
    if args.hook:
        try:
            hook_cwd = json.loads(stdin or b"{}").get("cwd")
        except (ValueError, AttributeError):
            hook_cwd = None
        folder = folder or (hook_cwd if isinstance(hook_cwd, str) and hook_cwd else None)
    environ = dict(os.environ)
    environ.pop(RESOLVED, None)  # a resolution inherited from an outer run never describes this one
    deadline = NO_DEADLINE
    if args.hook:
        # One deadline for the whole hook, resolution and script together, before the agent product's timeout
        # kills it without a trace; the script reads it from HOUSE_RULES_DEADLINE.
        deadline = Deadline(HOOK_SECONDS)
        environ[DEADLINE] = f"{time.time() + HOOK_SECONDS:.3f}"
    try:
        repository = find_repository(folder) if folder else find_repository()
        if (args.repo or args.origin or args.rev) and repository is None:
            raise Failure(f"house-rules: {args.repo or os.getcwd()} is not inside a Git working tree", status=1)
        data = origin = None
        if args.origin:
            data, origin = staged_configuration(repository)
        record = resolve(repository, rev=args.rev, data=data, origin=origin, deadline=deadline)
        tree = layer_tree(record)
    except Failure as failure:
        if not args.hook:
            raise
        # A failed resolution never stops an agent session: name it, then the global default's rules, labelled.
        repository = None
        tree, record = Path(os.path.realpath(str(cache_root() / "default"))), None
        known = prepared(tree) and (tree / args.path).is_file()
        print(f"{HOOK_ERROR} {failure}. " + (
            f"The House Rules below are the global default's ({tree}), not this repository's resolved rules, and "
            "this repository's custom layers are missing." if known else "No House Rules tree is cached, so this "
            "context holds no House Rules; fix the cause above and start a new session."))
        sys.stdout.flush()
        if not known:
            return 0
    script = script_in(tree, args.path)
    if not script.is_file():
        layer = record["layers"][0]["revision"] if record else tree.name
        raise Failure(f"house-rules: House Rules {layer} has no {args.path}; check the path or the house-rules ref")
    if record is not None:
        for warning in record["warnings"]:
            print(warning, file=sys.stderr)  # a linked, overridden or offline run says so, whatever the script prints
        environ[RESOLVED] = json.dumps(record)
    environ.pop(LINK, None)
    environ.pop(OVERRIDE, None)
    command = [sys.executable, "-I", str(script), *args.arguments] if script.suffix == ".py" else [str(script), *args.arguments]
    try:
        if stdin is not None:  # a hook's input, already read for its cwd, goes on to the script
            proc = subprocess.run(command, input=stdin, stdin=None, env=environ, cwd=repository or None)
        else:  # any other script reads this command's own standard input, as a Git hook's references
            proc = subprocess.run(command, stdin=sys.stdin, env=environ, cwd=repository or None)
    except OSError as exc:
        raise Failure(f"house-rules: cannot start {script} ({exc.strerror or exc})")
    return proc.returncode


def cmd_link(args):
    checkout = os.path.abspath(os.path.expanduser(args.checkout))
    if checkout_state(checkout) is None:
        raise Failure(f"house-rules: {checkout} is not the root of a Git working tree", status=1)
    url = args.url
    if url is None:
        proc = git(checkout, "config", "--get", "remote.origin.url", foreign=True, check=False)
        if proc.returncode:
            raise Failure(f"house-rules: {checkout} has no remote.origin.url; name the source with --url URL", status=1)
        url = os.fsdecode(proc.stdout).strip()  # undecodable bytes reach check_url, which refuses them
    check_url(url, "the --url value" if args.url is not None else f"remote.origin.url of {checkout}", status=1)
    key = url
    with links_lock():
        links = read_links()
        links[key] = checkout
        write_links(links)
    print(f"house-rules: linked {key} -> {checkout}; every run on this machine that lists this source uses the "
          "checkout and warns")
    return 0


def cmd_unlink(args):
    target = os.path.abspath(os.path.expanduser(args.target))
    with links_lock():
        links = read_links()
        keys = [key for key, path in links.items() if path == target]
        if not keys and args.target in links:
            keys = [args.target]
        if not keys:
            raise Failure(f"house-rules: no link names {args.target}; house-rules cache list shows the links",
                          status=1)
        for key in keys:
            del links[key]
        write_links(links)
    for key in keys:
        print(f"house-rules: unlinked {key}")
    return 0


def folder_size(path, skip=()):
    """The bytes of every file below `path`, leaving out the folders in `skip`; an unreadable path is an error."""
    def fail(exc):
        raise exc
    total = 0
    for folder, subfolders, names in os.walk(str(path), onerror=fail):
        subfolders[:] = [name for name in subfolders if os.path.join(folder, name) not in skip]
        for name in names:
            total += os.lstat(os.path.join(folder, name)).st_size
    return total


def cache_sources():
    """The folder of every source the cache has used: sources/<source hash>/."""
    root = cache_root() / "sources"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def answers(source):
    """(record path, kind, name, revision, fetched, url) of every branch and tag record of one source."""
    records = []
    for kind, folder in ANSWER_FOLDERS.items():
        for path in sorted((source / folder).glob("*.json")):
            try:
                value = json.loads(path.read_text())
                fields = (value[kind], value["revision"], value["fetched"], value["url"])
            except (OSError, ValueError, KeyError, TypeError):
                fields = ()
            if fields and all(isinstance(field, str) for field in fields):
                records.append((path, kind, *fields))
            else:
                print(f"warning: {path} is not a usable {kind} record; it is left out", file=sys.stderr)
    return records


def trees():
    folder = cache_root() / "revisions"
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if path.is_dir() and COMMIT.fullmatch(path.name))


def cmd_cache_list(args):
    """Every source by the URL its records hold, with its records and link, then every tree and the total size.

    A source folder holds only hashes, so a source without a record, such as one used only at a pinned commit, is
    not named; its trees are listed. The cache only grows; delete its folder while nothing runs to clear it.
    """
    links = read_links()
    for source in cache_sources():
        records = answers(source)
        if not records:
            continue
        urls = {url for *_, url in records}
        key = urls.pop() if len(urls) == 1 else f"source {source.name}"  # every record holds its source's URL
        print(key)
        for _, kind, name, commit, fetched, _ in records:
            print(f"  {kind} {name} -> {commit} (fetched {fetched})")
        if key in links:
            print(f"  linked to {links.pop(key)}")
    for key, path in sorted(links.items()):  # a link to a source this machine never fetched
        print(f"{key}\n  linked to {path}")
    sizes = {str(tree): folder_size(tree) for tree in trees()}
    for tree, size in sizes.items():
        print(f"tree {Path(tree).name} {size / 1_048_576:.1f} MiB")
    total = sum(sizes.values()) + (folder_size(cache_root(), skip=set(sizes)) if cache_root().is_dir() else 0)
    print(f"cache {cache_root()} {total / 1_048_576:.1f} MiB in total")
    return 0


def remove_tree(path):
    path = Path(path)
    if not path.exists():
        return
    for folder, _, _ in os.walk(str(path)):
        with contextlib.suppress(OSError):
            os.chmod(folder, 0o755)
    shutil.rmtree(str(path))


def detect_profiles(repository):
    return [name for name, markers in PROFILE_MARKERS if all((Path(repository) / m).is_file() for m in markers)]


def cmd_init(args):
    repository = command_repository(args) or None
    if repository is None:
        raise Failure("house-rules: not inside a Git working tree; run init in a repository", status=1)
    path = Path(repository) / CONFIG
    if path.exists():
        raise Failure(f"house-rules: {path} exists; edit it instead", status=1)
    profiles = args.profile if args.profile else detect_profiles(repository)
    text = json.dumps({"version": VERSION, "sources": [{"name": "house-rules", "ref": "main"}], "profiles": profiles,
                       "checks": {}}, indent=2) + "\n"
    repository_config(text, f"--profile {' '.join(profiles)}")  # write only what resolution accepts
    if args.dry_run:
        print(f"would create {path}:")
        print(text, end="")
        return 0
    create_once(path, text)
    print(f"house-rules: created {path} with profiles {', '.join(profiles) or 'none'}; review it and commit it")
    return 0


def create_once(path, text):
    """Create `path` with `text` only if nothing is there yet; never truncate a file another writer created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise Failure(f"house-rules: {path} exists; edit it instead", status=1)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def install_target():
    return Path.home() / ".local" / "bin" / "house-rules"


def installed_text(source):
    """This file's text with its first line naming the interpreter that runs the install, isolated (-I)."""
    text = Path(source).read_text()
    return f"#!{sys.executable} -I\n" + text.split("\n", 1)[1]


def write_command(source, dry_run):
    target = install_target()
    print(f"{'would write' if dry_run else 'write'} {target}")
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".house-rules-{os.getpid()}")
    try:
        temporary.write_text(installed_text(source))
        temporary.chmod(0o755)
        os.replace(str(temporary), str(target))
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def cmd_install(args):
    write_command(__file__, args.dry_run)
    path = global_path()
    if not path.exists():
        print(f"{'would create' if args.dry_run else 'create'} {path}")
        if not args.dry_run:
            with contextlib.suppress(Failure):  # another installer created it meanwhile: keep theirs
                create_once(path, json.dumps({"version": FILE_VERSION}) + "\n")
    custom, own = config_home() / "custom", Path(__file__).resolve().parents[1] / "custom"
    if own.is_dir() and not custom.exists() and not custom.is_symlink():
        print(f"{'would link' if args.dry_run else 'link'} {custom} -> {own}")
        if not args.dry_run:
            custom.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(str(own), str(custom))
    # Prepare the global default before anything points through the cache's `default` link.
    print(f"{'would resolve' if args.dry_run else 'resolve'} the global default sources")
    if not args.dry_run:
        record = resolve(None)
        print("\n".join(describe(record)))  # warnings included: a linked or offline default says so
    return 0


def cmd_version(args):
    own = Path(__file__).resolve()
    print(f"house-rules command at {own}, repository configuration version {VERSION}")
    return 0


class Parser(argparse.ArgumentParser):
    """A usage error exits 1; exit status 2 is kept for configuration and resolution failures."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def parser():
    top = Parser(prog="house-rules", description="Resolve House Rules source layers per repository.")
    commands = top.add_subparsers(dest="command", required=True)
    p = commands.add_parser("resolve")
    p.add_argument("--repo")
    p.add_argument("--rev")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_resolve)
    p = commands.add_parser("path")
    p.add_argument("--repo")
    p.set_defaults(run=cmd_path)
    p = commands.add_parser("exec")
    p.add_argument("--repo")
    p.add_argument("--hook", action="store_true")
    origins = p.add_mutually_exclusive_group()
    origins.add_argument("--origin", choices=("index",))
    origins.add_argument("--rev")
    p.add_argument("path")
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    p.set_defaults(run=cmd_exec)
    p = commands.add_parser("link")
    p.add_argument("checkout")
    p.add_argument("--url")
    p.set_defaults(run=cmd_link)
    p = commands.add_parser("unlink")
    p.add_argument("target")
    p.set_defaults(run=cmd_unlink)
    cache = commands.add_parser("cache").add_subparsers(dest="cache_command", required=True)
    cache.add_parser("list").set_defaults(run=cmd_cache_list)
    p = commands.add_parser("init")
    p.add_argument("--repo")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--profile", action="append")
    p.set_defaults(run=cmd_init)
    p = commands.add_parser("install")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(run=cmd_install)
    commands.add_parser("version").set_defaults(run=cmd_version)
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "resolve":
            args.repository = command_repository(args)
        return args.run(args)
    except Failure as failure:
        print(str(failure), file=sys.stderr)
        return failure.status
    except OSError as exc:
        print(f"house-rules: {exc.filename or 'a file'}: {exc.strerror or exc}; repair access and run again",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
