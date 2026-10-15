"""Shell lexing for the rulebook hook: quoting, heredocs, separators, and
what a Bash command touches — the directory each segment runs in
(`call_dir`, `bash_dirs`) and the files it writes, with their text
(`bash_write_targets`).

Pure functions of the command text and an environment mapping: no git, no
I/O beyond `os.path` resolution. Split out of `rulebook_hook.py` (which
imports every name back, so `rulebook_hook.<name>` still works) to keep that
file under the release tree's per-file size limit; the TS mod's twin is
`mod/engine/shell.ts`.
"""
import os
import re
import shlex


# A Bash call that wrote files is an edit too (see `bash_written_files`).
BASH_EDIT_MAX_FILES = 40            # more than this in one call is a generator, not an edit


BASH_EDIT_MAX_BYTES = 512 * 1024    # per file; bigger is data, not source


# ── shell-only segment ──────────────────────────────────────────────────────
_HD_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")   # delimiter must be a word, so `x << 2` is a shift


def shell_only(cmd):
    """Drop heredoc BODY lines; keep every shell line, including commands
    after a terminator (`commit -F - <<'MSG' … && git push`).
    Known edge: a bit-shift in a multi-line command can arm a bogus skip."""
    out, skip_until = [], None
    for line in cmd.split("\n"):
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        out.append(line)
        m = _HD_OPEN.search(line)
        if m:
            skip_until = m.group(2)
    return "\n".join(out)


# Quoted spans, blanked to spaces IN PLACE. `shlex` would tokenise properly
# but throws on the half-quoted strings real commands contain, and this only
# needs the contents neutralised, not the tokens. Length-preserving on
# purpose: the separator scans below find operators in the blanked copy and
# slice the ORIGINAL at those offsets, so quoted text survives intact.
# A double-quoted span honours backslash escapes; a single-quoted span has
# none in POSIX shell.
_QUOTED_SINGLE = r"'[^']*'"


_QUOTED_DOUBLE = r'"(?:\\.|[^"\\])*"'


_QUOTED_RX = re.compile(_QUOTED_SINGLE + "|" + _QUOTED_DOUBLE)


# `&&` and `||` first, so the lone-operator alternatives only see what is
# left. A standalone `&` backgrounds the command to its left and the next one
# runs anyway — `gh pr view -R other & git push` is TWO commands. The
# lookarounds keep redirection out: `2>&1`, `cmd >&2`, `cmd &> log`.
_SEPARATOR_RX = re.compile(r"&&|\|\||;|\n|\||(?<![>&])&(?![>&])")


_ESCAPE_RX = re.compile(r"\\.", re.S)   # a backslash escape, outside quotes


# A `#` starts a comment at the start of a word: after whitespace, an
# operator, or a grouping paren/brace. NOT after `{` — `${#files}` is a length.
_COMMENT_RX = re.compile(r"(?<![^\s;&|()}])#[^\n]*")


def blank_quoted(text):
    """`text` with the contents of quoted spans — and every backslash escape
    outside them — replaced by spaces, character for character, so every
    offset still points at the same place.

    A `\\|` is not a pipe: an escape is data, just as quoting is."""
    return _COMMENT_RX.sub(lambda m: " " * len(m.group(0)), blank_syntax(text))


def blank_syntax(text):
    """Quotes and escapes blanked, comments LEFT IN PLACE, length preserved.

    Exposed for `strip_comments`. Escapes become a NON-space placeholder,
    because `\\ ` joins two words in bash (`.title\\ #literal` is no comment)."""
    blanked = _QUOTED_RX.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2)
                             + m.group(0)[-1], text or "")
    return _ESCAPE_RX.sub("\x01\x01", blanked)


# ── a leading assignment is not part of the command ─────────────────────────
#
# `FOO=1 git push` execs `git push`, and a rule has to read it the same way,
# or an ANCHORED rule (`^git\s+push`) is silently bypassed by any prefix —
# including a refused `RULEBOOK_OVERRIDE=` that `strip_override` leaves in.
_ASSIGN_TOKEN_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s*")


_ASSIGN_NAME_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def strip_leading_assignments(shell):
    """`shell` with the env assignments that BEGIN a segment removed, byte for
    byte identical everywhere else.

    Tokenised per line by `find_override`'s shlex walk, so a quoted
    assignment is data and an unparseable line is handed back untouched. A
    run is stripped whole (`FOO=1 BAR=2 git push` -> `git push`)."""
    out = []
    for line in shell.split("\n"):
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
        except ValueError:                  # unbalanced quoting: not ours to rewrite
            out.append(line)
            continue
        targets, at_start = [], True
        for tok in toks:
            if at_start and _ASSIGN_NAME_RX.match(tok):
                targets.append(tok)         # stay at_start: assignments come in runs
                continue
            at_start = _segment_op(tok)
        stripped = line
        for val in targets:
            for m in _ASSIGN_TOKEN_RX.finditer(stripped):
                try:
                    if shlex.split(m.group(0))[0] != val:
                        continue
                except (ValueError, IndexError):
                    continue
                stripped = stripped[:m.start()] + stripped[m.end():]
                break
        out.append(stripped)
    return "\n".join(out)


_VAR_RX = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


_INTERP_RX = re.compile(r"^(?:python[0-9.]*|node)$")


def expand_path(token, here, env=None):
    """`token` as the shell would name the path — `~`, `$HOME`, `${VAR}` from
    `env` (default: this process's environment), relative to `here` — or ""
    when it cannot be named without running anything: a command
    substitution, a glob, an unset variable, another user's `~name`, or a
    relative path with no known `here`."""
    env = os.environ if env is None else env
    if not isinstance(token, str) or not token or "$(" in token \
            or any(c in token for c in "`*?[]\n\r\0"):
        return ""
    missing = []

    def var(m):
        v = env.get(m.group(1) or m.group(2))
        if v is None:
            missing.append(m.group(0))
            return ""
        return v
    t = _VAR_RX.sub(var, token)
    if missing or "$" in t or "{" in t or "}" in t:
        return ""
    if t == "~" or t.startswith("~/"):
        home = env.get("HOME") or ""
        if not home:
            return ""
        t = home + t[1:]
    elif t.startswith("~"):
        return ""
    if not os.path.isabs(t):
        if not here:
            return ""
        t = os.path.join(here, t)
    return os.path.normpath(t)


def _call_segments(cmd):
    """[(separator before it, segment)] of the command's shell text (heredoc
    bodies dropped), split where `blank_quoted` shows a separator, so one
    inside a quoted argument stays data."""
    text = shell_only(cmd or "")
    blank = blank_quoted(text)
    out, pos, sep = [], 0, ""
    for m in _SEPARATOR_RX.finditer(blank):
        out.append((sep, text[pos:m.start()]))
        sep, pos = m.group(0), m.end()
    out.append((sep, text[pos:]))
    return out


def _walk_call(cwd, cmd, env):
    """([segment facts], final dir) for each command of `cmd` that is not a
    directory change: {"dir", "toks", "sep", "text"}. `dir` follows `cd` /
    `pushd` / `popd` and a `git -C`; "" when it cannot be named."""
    here = os.path.normpath(cwd) if cwd else ""
    stack, out = [], []
    for sep, seg in _call_segments(cmd):
        text = strip_leading_assignments(seg.strip()).strip().lstrip("({ \t").strip()
        if not text:
            continue
        try:
            toks = shlex.split(text)
        except ValueError:
            if re.match(r"(?:cd|pushd|popd)\b", text):
                here = ""
            continue
        toks = [t for t in toks if t not in (")", "}", "))")]
        if not toks:
            continue
        if toks[0] in ("cd", "pushd"):
            args = [t for t in toks[1:] if not (t.startswith("-") and len(t) > 1)]
            if toks[0] == "pushd":
                stack.append(here)
            if not args:
                here = expand_path("~", "", env) if toks[0] == "cd" else ""
            elif args[0] == "-":
                here = ""
            else:
                here = expand_path(args[0], here, env)
            continue
        if toks[0] == "popd":
            here = stack.pop() if stack else ""
            continue
        d = here
        if os.path.basename(toks[0]) == "git":
            i = 1
            while i < len(toks) and toks[i].startswith("-"):
                t = toks[i]
                if t == "-C" and i + 1 < len(toks):
                    d = expand_path(toks[i + 1], d, env)
                    i += 2
                elif t.startswith("-C") and len(t) > 2:
                    d = expand_path(t[2:], d, env)
                    i += 1
                elif t == "-c" and i + 1 < len(toks):
                    i += 2
                else:
                    i += 1
        out.append({"dir": d, "toks": toks, "sep": sep, "text": text})
    return out, here


def call_dir(cwd, cmd, env=None):
    """The directory a Bash call acts in: its first command's, after any
    `cd`/`pushd` chain and `git -C`; with no command but a `cd`, where that
    leaves it. "" when it cannot be named (the caller keeps the cwd)."""
    env = os.environ if env is None else env
    segs, final = _walk_call(cwd, cmd, env)
    return segs[0]["dir"] if segs else final


def bash_dirs(cwd, cmd, env=None):
    """Every directory a Bash call's commands run in (known ones), in order."""
    env = os.environ if env is None else env
    segs, final = _walk_call(cwd, cmd, env)
    out = []
    for d in [s["dir"] for s in segs] + [final]:
        if d and d not in out:
            out.append(d)
    return out


def heredoc_bodies(cmd):
    """Each heredoc's body, in order — the lines `shell_only` drops."""
    out, skip, body = [], None, []
    for line in (cmd or "").split("\n"):
        if skip is not None:
            if line.strip() == skip:
                out.append("\n".join(body))
                skip = None
            else:
                body.append(line)
            continue
        m = _HD_OPEN.search(line)
        if m:
            skip, body = m.group(2), []
    if skip is not None:
        out.append("\n".join(body))
    return out


# `> f`, `>> f`, `1> f`, `&> f`, `>| f` — stdout into a file. `2> f` is a log,
# not an edit; `>&2` names no file.
_REDIR_OUT_RX = re.compile(r"(?<![0-9<>&])(?:1?>>?|&>>?|>\|)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|([^\s;&|<>()]+))")


# No backreferences (the TS port runs these through pyre): each quote style
# is its own alternative, and a path is group 1 or 2.
_QUOTED_PATH = r"(?:'([^'\n]+)'|\"([^\"\n]+)\")"


_PY_OPEN_W_RX = re.compile(r"\bopen\(\s*" + _QUOTED_PATH
                           + r"\s*,\s*(?:mode\s*=\s*)?[rRbBuUfF]?(?:'[^'\n]*[wax+][^'\n]*'|\"[^\"\n]*[wax+][^\"\n]*\")")


_PY_PATH_W_RX = re.compile(r"\bPath\(\s*" + _QUOTED_PATH + r"\s*\)\s*\.write_(?:text|bytes)\(")


_PY_ASSIGN_RX = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*(?:(?:pathlib\.)?Path\(\s*)?" + _QUOTED_PATH)


_JS_WRITE_RX = re.compile(r"\b(?:writeFileSync|appendFileSync|writeFile|appendFile)\(\s*" + _QUOTED_PATH)


_PY_LITERAL_RX = re.compile(r"\"\"\"([\s\S]*?)\"\"\"|'''([\s\S]*?)'''|\"((?:\\.|[^\"\\\n])*)\"|'((?:\\.|[^'\\\n])*)'")


_MODE_LITERAL_RX = re.compile(r"^(?:[rwabxt+]{1,3}|utf-?8)$", re.I)


# The text being REPLACED is not written: the first argument of `.replace(`,
# of a `rep(old, new)`-style helper, the first of an `(old, new)` pair, and a
# name bound to a literal and then passed as that first argument.
_REPLACER = r"(?:\.replace|\b_?(?:rep|replace|swap|sub)\w*)\("


_OLD_TAIL_RX = re.compile(r"(?:" + _REPLACER + r"|(?:^|[\[,=\s])\()\s*$")


_OLD_NAME_RX = re.compile(_REPLACER + r"\s*([A-Za-z_]\w*)\s*,")


def _code_writes(code):
    """(paths a Python / Node snippet opens for writing, the text it writes):
    the paths of `open(p, 'w')`, `Path(p).write_text(`, a name bound to a path
    and then written, `fs.writeFileSync(p`; the text is the snippet's string
    literals other than those paths, mode / encoding names, and the text a
    `.replace(old, new)` takes out (`_OLD_TAIL_RX`)."""
    paths = [m.group(1) or m.group(2) for rx in (_PY_OPEN_W_RX, _PY_PATH_W_RX, _JS_WRITE_RX)
             for m in rx.finditer(code)]
    for m in _PY_ASSIGN_RX.finditer(code):
        name = re.escape(m.group(1))
        if re.search(r"\b" + name + r"\s*\.write_(?:text|bytes)\(", code) or re.search(
                r"\bopen\(\s*" + name + r"\s*,\s*(?:mode\s*=\s*)?[rRbBuUfF]?['\"][^'\"\n]*[wax+]", code):
            paths.append(m.group(2) or m.group(3))
    if not paths:
        return [], ""
    lits = []
    olds = {m.group(1) for m in _OLD_NAME_RX.finditer(code)}
    old_bound = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(olds)) + r")\s*=\s*$") if olds else None
    for m in _PY_LITERAL_RX.finditer(code):
        before = code[max(0, m.start() - 80):m.start()]
        if _OLD_TAIL_RX.search(before) or (old_bound and old_bound.search(before)):
            continue
        if m.group(1) is not None or m.group(2) is not None:
            lit = m.group(1) if m.group(1) is not None else m.group(2)
        else:
            lit = (m.group(3) if m.group(3) is not None else m.group(4)).replace("\\n", "\n")
        if lit and lit not in paths and not _MODE_LITERAL_RX.match(lit):
            lits.append(lit)
    return paths, "\n".join(lits)


def _s_replacements(expr):
    """The replacement text of each `s<d>pat<d>repl<d>` in a sed / perl expression."""
    out = []
    for m in re.finditer(r"(?:^|[;\s}0-9$/])s([^\w\s\\])", expr or ""):
        delim, i, parts, cur = m.group(1), m.end(), [], []
        while i < len(expr) and len(parts) < 2:
            c = expr[i]
            if c == "\\" and i + 1 < len(expr):
                cur.append(expr[i + 1] if expr[i + 1] == delim else expr[i:i + 2])
                i += 2
                continue
            if c == delim:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(c)
            i += 1
        if len(parts) == 2:
            out.append(parts[1])
    return out


def _inplace_edit(toks):
    """(files, written text) for `sed -i …` / `perl -pi …`, else None."""
    name = os.path.basename(toks[0])
    args, exprs, pos, inplace, i = toks[1:], [], [], False, 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression") and i + 1 < len(args):
            exprs.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--expression="):
            exprs.append(a.split("=", 1)[1])
        elif a.startswith("--in-place"):
            inplace = True
        elif a in ("-f", "--file") and i + 1 < len(args):
            i += 2
            continue
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            flags = a[1:]
            if name == "perl":
                inplace = inplace or "i" in flags
                if flags.endswith("e") and i + 1 < len(args):
                    exprs.append(args[i + 1])
                    i += 2
                    continue
            elif "i" in flags:
                inplace = True
                if a == "-i" and i + 1 < len(args) and args[i + 1] == "":
                    i += 2                  # BSD sed: `-i ''`, the empty suffix
                    continue
        elif not a.startswith("--"):
            pos.append(a)
        i += 1
    if not inplace:
        return None
    if not exprs and pos:
        exprs.append(pos.pop(0))
    text = "\n".join(r for e in exprs for r in _s_replacements(e))
    return [p for p in pos if ">" not in p and "<" not in p], text


def bash_write_targets(cwd, cmd, env=None):
    """[(path, text)] for each file a Bash command visibly WRITES, read from
    the command alone so the pre lane can judge it before it runs.

    Written: a redirect (`> f`, `>> f`), `tee [-a] f`, `sed -i`, `perl -pi`,
    the destination of a two-argument `cp`/`mv`, and the files a Python or
    Node heredoc (`python3 - <<'EOF'`, `node <<EOF`) or `-c` snippet opens
    for writing. Paths resolve where that command runs (`cd`, `~`, `$VAR`).
    The text is what the command writes when the command line holds it — a
    heredoc fed to `cat`/`tee`, echoed text, a sed replacement, a snippet's
    string literals — else "" (a path-only rule can still match). Nothing
    else is a write: no write construct, no target."""
    env = os.environ if env is None else env
    segs, _ = _walk_call(cwd, cmd, env)
    bodies = heredoc_bodies(cmd)
    found, piped = [], ""
    for s in segs:
        toks, text, d = s["toks"], s["text"], s["dir"]
        body = bodies.pop(0) if _HD_OPEN.search(text) and bodies else None
        name = os.path.basename(toks[0])
        prev, piped = (piped if s["sep"] == "|" else ""), ""
        if name in ("echo", "printf"):
            said = []
            for t in toks[1:]:
                if ">" in t:
                    break
                if not (name == "echo" and re.match(r"^-[neE]+$", t)):
                    said.append(t)
            out_text = " ".join(said)
        elif name == "cat" and body is not None:
            out_text = body
        else:
            out_text = ""
        piped = out_text
        code = None
        is_interp = bool(_INTERP_RX.match(name)) or (
            name in ("uv", "poetry", "pipenv") and any(_INTERP_RX.match(os.path.basename(t)) for t in toks[1:]))
        if is_interp:
            if body is not None:
                code = body
            elif "-c" in toks and toks.index("-c") + 1 < len(toks):
                code = toks[toks.index("-c") + 1]
            elif "-e" in toks and name == "node" and toks.index("-e") + 1 < len(toks):
                code = toks[toks.index("-e") + 1]
        if code:
            paths, lits = _code_writes(code)
            found += [(d, p, lits) for p in paths]
        blank = blank_quoted(text)
        for m in _REDIR_OUT_RX.finditer(text):
            if blank[m.start()] != text[m.start()]:
                continue                    # inside a quoted argument
            p = next((g for g in m.groups() if g), "")
            found.append((d, p, out_text if name in ("echo", "printf", "cat") else ""))
        if name == "tee":
            for t in toks[1:]:
                if not t.startswith("-") and ">" not in t and "<" not in t:
                    found.append((d, t, body if body is not None else prev))
        elif name in ("sed", "perl"):
            got = _inplace_edit(toks)
            if got:
                found += [(d, p, got[1]) for p in got[0]]
        elif name in ("cp", "mv") and "-t" not in toks and "--target-directory" not in toks:
            pos = [t for t in toks[1:] if not t.startswith("-") and ">" not in t]
            if len(pos) == 2:
                found.append((d, pos[1], ""))
    out, seen = [], {}
    for d, p, written in found:
        path = expand_path(p, d, env)
        if not path or path.startswith("/dev/") or "/.git/" in path + "/":
            continue
        written = (written or "")[:BASH_EDIT_MAX_BYTES]
        if path in seen:
            i = seen[path]
            if written and written not in out[i][1]:
                out[i] = (path, (out[i][1] + "\n" + written).strip("\n"))
            continue
        seen[path] = len(out)
        out.append((path, written))
        if len(out) >= BASH_EDIT_MAX_FILES:
            break
    return out


def _segment_op(tok):
    return bool(tok) and all(ch in ";&|(" for ch in tok)


BASH_READ_MAX_FILES = 8            # read segments named per Bash call; past that it is a script, not a read


# ── files a Bash call READS into context ────────────────────────────────────
#
# A read rule (`event: read`) is about what enters the model's context: the
# Read tool, and `cat`/`head`/`tail`/`less`/`more`/`sed` on a path (most of
# the largest tool results are whole-file shell dumps).
#
# Read from the COMMAND, not the disk, because this lane must gate. A pipe or
# redirect is not a read into context, and every doubt resolves to "no read
# here" — under-count, never false-fire. `cd` is tracked segment by segment
# (`cd <repo> && cat spec.md`).

_READ_CMDS = ("cat", "head", "tail", "less", "more", "sed")
# stdout going somewhere other than the context: `> f`, `>> f`, `&> f`, and a
# heredoc opener (its stdin is data). `2>/dev/null` and `2>&1` are not that.
_REDIRECT_RX = re.compile(r"(?<![0-9&<])>(?!&)|&>|<<")
_SED_RANGE_RX = re.compile(r"^(\d+)(?:,(\+)?(\d+|\$))?p$")
_LINES_FLAG_RX = re.compile(r"^(?:-n|--lines=)(\d+)$|^-(\d+)$")


def _head_tail_lines(args):
    """(lines printed, indices of the flag VALUES) for `head`/`tail`: 10 by
    default, `-N`, `-n N`, `-nN`, `--lines=N`. (None, None) for a form this
    does not name (`-c` bytes, `-n +N`) — not a read this can measure."""
    n, i, used = 10, 0, set()
    while i < len(args):
        a = args[i]
        if a in ("-c", "--bytes") or a.startswith("--bytes=") or a.startswith("-c"):
            return None, None
        if a in ("-n", "--lines"):
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return None, None
            n, used, i = int(args[i + 1]), used | {i + 1}, i + 2
            continue
        m = _LINES_FLAG_RX.match(a)
        if m:
            n = int(m.group(1) or m.group(2))
        i += 1
    return n, used


def _sed_lines(args):
    """(lines printed, indices that are not files) for a `sed` that prints
    to stdout. `-n 'A,Bp'` is a range, `-n 'Ap'` one line, `-n '1,$p'` the
    whole file; `sed s/a/b/ f` with no -n prints every line. `-i` writes in
    place and prints nothing, and a script this cannot read is not a read —
    both answer (None, None)."""
    if any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in args):
        return None, None
    quiet, script, used, i = False, None, set(), 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--quiet", "--silent"):
            quiet = True
        elif a in ("-e", "--expression", "-f", "--file"):
            if i + 1 < len(args):
                used.add(i + 1)
                if a in ("-e", "--expression"):
                    script = args[i + 1]
            i += 2
            continue
        elif a.startswith("-"):
            pass
        elif script is None:
            script, used = a, used | {i}
        i += 1
    if script is None:
        return None, None
    if not quiet:
        return None, used               # prints the whole file, transformed
    m = _SED_RANGE_RX.match(script.replace(" ", ""))
    if not m:
        return None, None               # `/x/,/y/p` and friends: not a read we can measure
    a, plus, b = int(m.group(1)), m.group(2), m.group(3)
    if b is None:
        return 1, used
    if b == "$":
        return None, used
    return (int(b) + 1 if plus else max(int(b) - a + 1, 0)), used


def bash_reads(cwd, cmd):
    """(path, pulled) for each file a Bash call would print into the context;
    `pulled` is the line count the form asks for, None meaning every line.
    Relative paths resolve where the command runs, `cd` included. Anything
    this cannot name is not a read — the list is empty on every doubt."""
    out = []
    here = os.path.expanduser(cwd or "") or os.getcwd()
    for seg in re.split(r"&&|\|\||;|\n", shell_only(cmd or "")):
        seg = strip_leading_assignments(seg.strip()).strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        if not toks:
            continue
        if toks[0] == "cd":
            target = os.path.expanduser(toks[1]) if len(toks) > 1 else os.path.expanduser("~")
            here = target if os.path.isabs(target) else os.path.normpath(os.path.join(here, target))
            continue
        if "|" in seg or _REDIRECT_RX.search(seg):
            continue
        name = os.path.basename(toks[0])
        if name not in _READ_CMDS:
            continue
        args, pulled, used = toks[1:], None, set()
        if name in ("head", "tail"):
            pulled, used = _head_tail_lines(args)
            if used is None:
                continue
        elif name == "sed":
            pulled, used = _sed_lines(args)
            if used is None:
                continue
        # a token with `<` or `>` in it is a redirection operand (`2>/dev/null`),
        # never a file to read
        files = [a for i, a in enumerate(args)
                 if i not in used and a != "-" and not a.startswith("-") and "<" not in a and ">" not in a]
        for f in files:
            path = os.path.expanduser(f)
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(here, path))
            out.append((path, pulled))
            if len(out) >= BASH_READ_MAX_FILES:
                return out
    return out


def read_cd(cwd, cmd, repo_info):
    """Use a shell read's checkout when the command runs outside any checkout.

    Codex can send a projectless cwd and a command like `cat repo/file` from
    its parent. In that case the target's Rulebook must get the first chance
    to judge the read before the no-repository fast path in the hook.
    """
    here = call_dir(cwd, cmd)
    if repo_info(here)[0]:
        return here
    for path, _ in bash_reads(cwd, cmd):
        parent = os.path.dirname(path)
        if repo_info(parent)[0]:
            return parent
    return here
