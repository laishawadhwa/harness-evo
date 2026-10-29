"""Best-effort analysis of Bash commands: reads, writes, test runs, commits, destructive actions.

Harnesses do not label what a shell command does, so the adapter infers it here.
Commands are split into simple commands. Shell keywords, wrapper programs
(sudo, xargs, timeout, env, ...), `bash -c` / `eval` strings and `$(...)` / backtick
substitutions are unwrapped, and every rule then matches on the program actually run.
Parsing is heuristic: an unparseable command yields no reads, writes or test run, and
is still checked against the destructive rules.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

_OPS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}
_REDIRECTS = {">", ">>", "&>", "&>>", ">|", "1>", "2>", "1>>", "2>>"}
_READERS = {"cat", "head", "tail", "less", "more", "bat", "nl", "view"}
_KEYWORDS = {"do", "then", "else", "elif", "{", "!", "time", "coproc"}
_HEADERS = {"for", "case", "select", "function", "done", "fi", "esac", "}", "in"}
# wrapper program -> its options that take a value
_WRAPPERS = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D"}, "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"}, "nohup": set(), "command": set(), "exec": {"-a"},
    "builtin": set(), "xargs": {"-I", "-i", "-n", "-P", "-d", "-L", "-s", "-E", "-a"}, "nice": {"-n"},
    "ionice": {"-c", "-n"}, "timeout": {"-s", "-k", "--signal", "--kill-after"}, "stdbuf": {"-i", "-o", "-e"},
    "chronic": set(), "caffeinate": set(), "time": {"-f", "-o"}, "unbuffer": set(), "npx": {"-p", "--package"},
    "bunx": set(), "pipx": set(),
}
_RUN_PREFIX = {"uv": {"run"}, "poetry": {"run"}, "pipenv": {"run"}, "hatch": {"run"}, "pdm": {"run"},
               "rye": {"run"}, "pnpm": {"exec", "dlx"}, "yarn": {"dlx", "exec"}, "bundle": {"exec"},
               "pipx": {"run"}}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_FILTER_FLAGS = {"-k", "-m", "--grep", "-g", "-t", "--testNamePattern", "-run", "--run", "--filter", "-E",
                 "--tests", "-Dtest"}
_FLAGS_WITH_VALUE = {"-c", "-p", "-n", "-o", "--rootdir", "--maxfail", "--timeout", "--reporter",
                     "--config", "-C", "--prefix", "-w", "--workspace", "--project"}


# ---------------------------------------------------------------- splitting

def tokenize(command: str) -> list[str]:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return command.split()


def substitutions(command: str) -> list[str]:
    """Bodies of $(...) and `...` outside single quotes (they run as commands)."""
    out, i, quote = [], 0, None
    while i < len(command):
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
        elif c == "\\":
            i += 1
        elif c == "'" and quote is None:
            quote = "'"
        elif c == '"':
            quote = None if quote == '"' else '"'
        elif c == "`":
            end = command.find("`", i + 1)
            if end == -1:
                break
            out.append(command[i + 1:end])
            i = end
        elif command.startswith("$(", i) and not command.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(command) and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            out.append(command[i + 2:j - 1])
            i = j - 1
        i += 1
    return out


_FD_DUP = re.compile(r"(?<![\w/.-])\d*(?:>>?|<)&(?:\d+|-)")     # 2>&1, >&2, 1>&-
_FD_NUM = re.compile(r"(?<![\w/.-])\d(?=>)")                     # the 2 in 2>/dev/null


def parseable(command: str) -> bool:
    try:
        shlex.split(command)
        return True
    except ValueError:
        return False


def _split(command: str) -> list[list[str]]:
    out, cur = [], []
    command = _FD_NUM.sub("", _FD_DUP.sub(" ", command))
    for tok in tokenize(command.replace("\\\n", " ").replace("\n", " ; ")):
        if tok in _OPS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def strip_prefixes(argv: list[str]) -> list[str]:
    """Drop env assignments, shell keywords and wrapper programs (with their options)."""
    i = 0
    while i < len(argv):
        a = argv[i]
        base = a.rsplit("/", 1)[-1]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a) or a in _KEYWORDS:
            i += 1
            continue
        if base in _WRAPPERS:
            opts = _WRAPPERS[base]
            i += 1
            while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
                flag = argv[i]
                i += 1
                if flag in opts and i < len(argv):
                    i += 1
            if i < len(argv) and argv[i] == "--":
                i += 1
            if base == "timeout" and i < len(argv):
                i += 1  # the duration
            continue
        if base in _RUN_PREFIX and i + 1 < len(argv) and argv[i + 1] in _RUN_PREFIX[base]:
            i += 2
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            continue
        break
    return argv[i:]


def simple_commands(command: str, _depth: int = 0) -> list[list[str]]:
    """Every simple command the line would run, unwrapped (including bash -c, eval, substitutions)."""
    out = []
    for argv in _split(command):
        if argv and argv[0] in _HEADERS:
            # `for x in ...` / `case` headers are not commands; `done`, `fi` and `}` close blocks
            if argv[0] in {"done", "fi", "esac", "}"} and len(argv) > 1:
                argv = argv[1:]
            else:
                continue
        argv = strip_prefixes(argv)
        if not argv:
            continue
        prog = argv[0].rsplit("/", 1)[-1]
        if _depth < 3 and prog in _SHELLS:
            script = next((argv[j + 1] for j, a in enumerate(argv[:-1])
                           if a.startswith("-") and not a.startswith("--") and "c" in a), None)
            if script is not None:
                out += simple_commands(script, _depth + 1)
                continue
        if _depth < 3 and prog == "eval":
            out += simple_commands(" ".join(argv[1:]), _depth + 1)
            continue
        out.append(argv)
    if _depth < 3:
        for body in substitutions(command):
            out += simple_commands(body, _depth + 1)
    return out


def _redirections(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
    clean, reads, writes = [], [], []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _REDIRECTS and i + 1 < len(argv):
            target = argv[i + 1]
            if not target.startswith("&") and target != "/dev/null":
                writes.append(target)
            i += 2
            continue
        if tok == "<" and i + 1 < len(argv):
            reads.append(argv[i + 1])
            i += 2
            continue
        clean.append(tok)
        i += 1
    return clean, reads, writes


# ---------------------------------------------------------------- reads, writes, commits

@dataclass
class ShellFacts:
    reads: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)       # files created or overwritten
    edits: list[str] = field(default_factory=list)        # files edited in place (sed -i, perl -pi)
    deletes: list[str] = field(default_factory=list)
    is_commit: bool = False


def analyse(command: str) -> ShellFacts:
    facts = ShellFacts()
    if not parseable(command):
        return facts  # unbalanced quotes: no reads or writes are inferred (destructive rules still run)
    for raw in simple_commands(command):
        argv, reads, writes = _redirections(raw)
        facts.reads += reads
        facts.writes += writes
        argv = strip_prefixes(argv)
        if not argv:
            continue
        prog = argv[0].rsplit("/", 1)[-1]
        args = argv[1:]
        positional = [a for a in args if not a.startswith("-")]
        if prog in _READERS:
            facts.reads += [a for a in positional if not re.fullmatch(r"\d+", a)]
        elif prog == "sed":
            in_place = any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in args)
            (facts.edits if in_place else facts.reads).extend(_sed_files(args))
        elif prog == "perl" and any(re.match(r"^-\w*i", a) for a in args):
            facts.edits += positional[1:] if positional else []
        elif prog == "tee":
            facts.writes += positional
        elif prog in {"cp", "install"} and len(positional) >= 2:
            facts.writes.append(positional[-1])
        elif prog == "mv" and len(positional) >= 2:
            facts.writes.append(positional[-1])
            facts.deletes += positional[:-1]
        elif prog in {"rm", "unlink", "shred"}:
            facts.deletes += positional
        elif prog in {"touch", "truncate"}:
            facts.writes += [a for a in positional if not re.fullmatch(r"[+-]?\d+[KMG]?", a)]
        elif prog == "git" and _git_subcommand(args)[0] == "commit":
            facts.is_commit = True
    for name in ("reads", "writes", "edits", "deletes"):
        setattr(facts, name, list(dict.fromkeys(getattr(facts, name))))
    return facts


def _sed_files(args: list[str]) -> list[str]:
    files, script_seen, skip = [], False, False
    for a in args:
        if skip:
            skip = False
            continue
        if a in {"-e", "-f", "--expression", "--file"}:
            script_seen = True
            skip = True
            continue
        if a.startswith("-"):
            continue
        if not script_seen:
            script_seen = True
            continue
        files.append(a)
    return files


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, args[i + 1:]
    return None, []


# ---------------------------------------------------------------- test runs

_RUNNERS = {"pytest", "py.test", "jest", "vitest", "mocha", "rspec", "ava", "nose2", "phpunit", "ctest", "tox", "nox"}
# program -> test subcommands; True: the subcommand must be the first positional argument
_TEST_SUBCOMMANDS = {
    "go": ({"test"}, True), "cargo": ({"test", "nextest"}, True), "npm": ({"test", "t", "run"}, True),
    "pnpm": ({"test", "t", "run"}, True), "yarn": ({"test", "run"}, True), "bun": ({"test", "run"}, True),
    "dotnet": ({"test"}, True), "mix": ({"test"}, True), "deno": ({"test"}, True),
    "make": ({"test", "check"}, False), "gradle": ({"test", "check"}, False), "gradlew": ({"test", "check"}, False),
    "mvn": ({"test", "verify"}, False), "mvnw": ({"test", "verify"}, False),
}
_INFO_FLAGS = {"--version", "-V", "--help", "-h", "--collect-only", "--co", "--setup-plan", "--fixtures",
               "--markers", "--listTests", "--list", "-list", "--dry-run", "--showconfig"}


@dataclass
class TestInvocation:
    command: str
    targets: list[str]
    filters: list[str]
    __test__ = False  # not a pytest test class

    @property
    def full_suite(self) -> bool:
        return not self.targets and not self.filters


def _normalise_target(t: str) -> str:
    t = t.split("::", 1)[0]
    if t.startswith("./"):
        t = t[2:]
    if t == "...":
        return "."  # go test ./... runs every package
    if t.endswith("/..."):
        t = t[:-4]
    return t.rstrip("/") or "."


def _runner_args(argv: list[str]) -> list[str] | None:
    """Arguments after the test runner, or None when the program run is not a test runner."""
    prog = argv[0].rsplit("/", 1)[-1]
    if re.fullmatch(r"python[0-9.]*", prog) and len(argv) > 2 and argv[1] == "-m":
        argv, prog = argv[2:], argv[2]
    if prog in _RUNNERS:
        return argv[1:]
    if prog in _TEST_SUBCOMMANDS:
        words, first_only = _TEST_SUBCOMMANDS[prog]
        positional = [(i, a) for i, a in enumerate(argv[1:], 1) if not a.startswith("-")]
        for n, (i, a) in enumerate(positional):
            if a in words:
                if a == "run":  # `npm run test`, `npm run test:unit`
                    nxt = positional[n + 1][1] if n + 1 < len(positional) else ""
                    if not nxt.startswith("test"):
                        return None
                    return argv[positional[n + 1][0] + 1:]
                return argv[i + 1:]
            if first_only:
                return None
    return None


def test_invocation(command: str, patterns) -> TestInvocation | None:
    """A test run is a simple command that matches a profile pattern *and* runs a test runner."""
    if not parseable(command):
        return None
    for raw in simple_commands(command):
        argv = strip_prefixes(_redirections(raw)[0])
        if not argv:
            continue
        text = " ".join(argv)
        if not any(p.search(text) for p in patterns):
            continue
        rest = _runner_args(argv)
        if rest is None or any(a in _INFO_FLAGS for a in rest):
            continue
        targets, filters = [], []
        j = 0
        while j < len(rest):
            a = rest[j]
            if a in _FILTER_FLAGS:
                if j + 1 < len(rest):
                    filters.append(rest[j + 1])
                j += 2
                continue
            if a.startswith("-") and "=" in a and a.split("=", 1)[0] in _FILTER_FLAGS:
                filters.append(a.split("=", 1)[1])
                j += 1
                continue
            if a in _FLAGS_WITH_VALUE:
                j += 2
                continue
            if not a.startswith("-"):
                targets.append(_normalise_target(a))
            j += 1
        return TestInvocation(command=text, targets=targets, filters=filters)
    return None


_FAILURE_SUMMARY = [
    re.compile(r"\b([1-9]\d*) (?:failed|errors?|failing)\b"),
    re.compile(r"\bno tests ran\b", re.I),
    re.compile(r"^(?:FAIL|--- FAIL)\b|\bBUILD FAILED\b|\btest result: FAILED\b", re.M),
    re.compile(r"\bTests:\s+[1-9]\d* failed\b"),
]


def test_passed(exit_code: int | None, output: str) -> bool:
    """A non-zero exit fails the run. A zero exit still fails if the runner's summary reports
    failures, which catches `pytest ... | tail` hiding the real status."""
    if exit_code not in (None, 0):
        return False
    return not any(rx.search(output or "") for rx in _FAILURE_SUMMARY)


# ---------------------------------------------------------------- destructive actions

_SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "duckdb", "clickhouse-client", "cqlsh",
                "mongosh", "mongo", "bq", "snowsql", "trino", "presto", "beeline", "spark-sql"}
_SQL = [
    ("dropping a table", re.compile(r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b", re.I)),
    ("truncating a table", re.compile(r"\bTRUNCATE\s+TABLE\b", re.I)),
    ("deleting all rows", re.compile(r"\bDELETE\s+FROM\s+[\w.\"`\[\]]+\s*(?:;|$|'|\"|\))", re.I | re.M)),
]
_HARNESS_SUBCOMMANDS = {"mute", "unmute", "uninstall", "install", "promote"}


def _allowed(target: str, allow) -> bool:
    from ..config import glob_match
    t = target.rstrip("/")
    if t.startswith("./"):
        t = t[2:]
    if not t or t in {".", "..", "/", "~", "*", "./*"} or t.startswith(("~", "$")) or ".." in t.split("/"):
        return False
    if t.startswith("/"):
        return glob_match(t, [a for a in allow if a.startswith("/")])
    if set(t) <= set("*?/."):
        return False
    return glob_match(t, allow) or any(part in allow for part in t.split("/"))


def _rm_hits(argv: list[str], allow) -> list[str]:
    flags = "".join(a.lstrip("-") for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
    long_flags = {a for a in argv[1:] if a.startswith("--")}
    recursive = "r" in flags or "R" in flags or "--recursive" in long_flags
    force = "f" in flags or "--force" in long_flags
    if not (recursive and force):
        return []
    targets = [a for a in argv[1:] if not a.startswith("-")] or ["(no target)"]
    return [f"rm -rf {t}" for t in targets if not _allowed(t, allow)]


def _find_hits(args: list[str], allow) -> list[str]:
    deleting = "-delete" in args
    for flag in ("-exec", "-execdir", "-ok", "-okdir"):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args) and args[i + 1].rsplit("/", 1)[-1] in {"rm", "shred", "unlink"}:
                deleting = True
    if not deleting:
        return []
    names = [args[i + 1] for i, a in enumerate(args[:-1]) if a in {"-name", "-iname"}]
    if names and all(_allowed(n, allow) for n in names):
        return []
    starts = []
    for a in args:
        if a.startswith(("-", "(", "!")):
            break
        starts.append(a)
    if starts and all(_allowed(s, allow) for s in starts):
        return []
    return [f"find {' '.join(starts) or '.'} -delete"]


def _git_hits(args: list[str]) -> list[str]:
    sub, rest = _git_subcommand(args)
    flags = {a for a in rest if a.startswith("-")}
    short = "".join(a[1:] for a in rest if a.startswith("-") and not a.startswith("--"))
    if sub == "push":
        if {"--force", "--mirror"} & flags or any(a.startswith("--force-with-lease") for a in rest) \
                or "f" in short or any(a.startswith("+") for a in rest if not a.startswith("-")):
            return ["git push --force"]
    elif sub == "reset" and "--hard" in flags:
        return ["git reset --hard"]
    elif sub == "clean" and ("f" in short or "--force" in flags):
        return ["git clean -f"]
    elif sub == "checkout" and "." in rest:
        return ["git checkout ."]
    elif sub == "restore" and "." in rest and not ({"--staged", "-S"} & flags and not {"--worktree", "-W"} & flags):
        return ["git restore ."]
    elif sub == "stash" and rest[:1] == ["clear"]:
        return ["git stash clear"]
    return []


def destructive_matches(command: str, allow=(), extra=()) -> list[str]:
    hits = []
    sql_client = False
    for raw in simple_commands(command):
        argv = strip_prefixes(_redirections(raw)[0])
        if not argv:
            continue
        prog = argv[0].rsplit("/", 1)[-1]
        if prog == "rm":
            hits += _rm_hits(argv, allow)
        elif prog == "find":
            hits += _find_hits(argv[1:], allow)
        elif prog == "git":
            hits += _git_hits(argv[1:])
        elif prog in _SQL_CLIENTS:
            sql_client = True
        elif prog in {"mkfs", "dd"} or prog.startswith("mkfs."):
            if prog != "dd" or any(a.startswith("of=/dev/") for a in argv[1:]):
                hits.append(f"{prog} to a device")
        if (prog == "harness-evo" or (re.fullmatch(r"python[0-9.]*", prog) and argv[1:3] == ["-m", "harness_evo"])):
            sub = argv[3] if prog != "harness-evo" and len(argv) > 3 else argv[1] if len(argv) > 1 else ""
            if sub in _HARNESS_SUBCOMMANDS or (sub == "calibrate" and "--apply" in argv):
                hits.append(f"harness-evo {sub} (changing the harness's own settings)")
    if sql_client:
        hits += [name for name, rx in _SQL if rx.search(command)]
    hits += [f"pattern {rx.pattern}" for rx in extra if rx.search(command)]
    return list(dict.fromkeys(hits))
