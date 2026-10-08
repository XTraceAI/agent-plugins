// The Rulebook engine the mod's lanes call: a TypeScript port of the parts of
// scripts/rulebook_hook.py that decide what fires. Python stays the engine on
// Codex, Cursor and wherever the mod does not load; the golden vectors
// (scripts/rule_vectors.py, generated into ./tests/vectors/ by
// scripts/test-mod.sh for each test run) hold the port to its answers.
//
// The engine decides; the mod acts (mod/act.ts): it denies, adds context,
// writes the disclosure row, asks, and feeds the companion. Nothing in
// ./engine touches `$` — I/O comes in through EngineIO, so every layer is a
// pure function a vector can replay.

/** A rule in the flat shape `to_hook_rule()` returns (rules.ts). */
export type HookRule = {
  id: string
  on: string
  mode?: string
  text?: string
  why?: string
  _label?: string | null
  [k: string]: unknown
}

/** One tool call as the lanes see it. */
export type CallEvent = {
  phase: 'pre' | 'post'
  tool: string
  input: Readonly<Record<string, unknown>>
  sessionId: string
  cwd: string
  /** Set when a subagent made the call (replaces the /subagents/ path test). */
  agentId?: string
  /** The call's tool_use id: pairs a Bash call's pre with its post (parallel calls). */
  toolUseId?: string
  /** Post phase only. */
  result?: { text?: string; isError?: boolean }
}

/** One rule that fired on this event. */
export type Fire = {
  ruleId: string
  label: string
  /** `disclosure_line(rule, blocked)`, byte-identical to Python. */
  line: string
  mode: 'advise' | 'gate'
  text: string
}

export type Verdict = {
  /** Set when a gate refuses the call: the reason the model reads. */
  deny?: string
  /** Text the model reads after the result (advisories, post-lane notes). */
  context: string[]
  fires: Fire[]
  /** Fire/event rows for the Python ledger (`rulebook_mod_cli.py log`). */
  ledger: Record<string, unknown>[]
}

/** One file-system entry as `$.fs.stat` answers it. */
export type FileStat = { kind: 'file' | 'dir' | 'other'; size: number; mtimeMs: number; realPath?: string }

/** One message in Messages API form (`$.session.messages({ as: "api" })`). */
export type TurnMessage = { role: 'user' | 'assistant'; content: readonly Record<string, unknown>[] | string }

/** The environment variables the Python hook reads, as the mod's process has them. */
export type EngineEnv = {
  /** MEMHUB_RULEBOOK_RECALL: "0" turns the anchor lane off. */
  recall?: string
  /** MEMHUB_RULEBOOK_JUDGE: "0" turns the judge off. */
  judge?: string
  /** MEMHUB_RULEBOOK_TIMEOUT_S: the network timeout override. */
  timeoutS?: string
  /** MEMHUB_RULEBOOK_BASE_BRANCH: the diff probes' explicit base. */
  baseBranch?: string
  /** MEMHUB_BRIEF_TOKEN_BUDGET: the session-start budget the posture rules take a third of. */
  briefBudget?: string
}

/** `rulebook_mod_cli.py paths --cwd <dir>`: what the Python hook resolves for a call made there. */
export type PathsInfo = { repo: string; root: string | null; base: string }

/**
 * One write to the state the Python hooks share (`scripts/rulebook_mod_state.py`,
 * which takes their lock): an ordering feed that writes, an arming, a discharge.
 */
export type StateOp =
  | { op: 'feed'; root: string; rule: HookRule; hook_phase: string; tool: string; cmd: string; file_path: string; ok: boolean | null; armed: string | null }
  | { op: 'arm'; session: string; event: string; prompt: string; rules: HookRule[]; repo: string; gitdir: string }
  | { op: 'drop'; session: string; rule_ids: string[] }
  | { op: 'specs'; root: string; spec_dir: string; paths: string[] }

/** The engine's only way out of pure code. */
export interface EngineIO {
  /** `git <argv>` (argv WITHOUT the leading `git`); rejects on a timeout, as Python's `subprocess.run` raises. */
  git(argv: readonly string[], cwd: string, timeoutMs?: number): Promise<{ code: number; stdout: string }>
  /** A file's text; undefined when missing, unreadable or past the 4 MiB `$.fs` cap. */
  readText(path: string): Promise<string | undefined>
  /** Best effort; never throws. */
  writeText(path: string, text: string): Promise<void>
  /** `os.stat` (`resolve`: also the real path); undefined when missing. */
  stat(path: string, resolve?: boolean): Promise<FileStat | undefined>
  /** Newline count of a file too big for `readText` (`read_facts`' loop); undefined on failure. */
  countLines(path: string): Promise<number | undefined>
  /** Milliseconds since the epoch. */
  now(): number
  /** Resolves after `ms` (the judge's own timeout race). */
  sleep(ms: number): Promise<void>
  /** The local UTC offset at `ms`, in minutes east (Python's `astimezone()`). */
  tzOffsetMinutes(ms: number): number
  /** $HOME, for `~` and the identity redaction. */
  home: string
  env(): Promise<EngineEnv>
  /** `rulebook_mod_cli.py paths --cwd <dir>`; undefined when it cannot answer. */
  paths(dir: string): Promise<PathsInfo | undefined>
  /** One write through `rulebook_mod_state.py`; undefined when the helper failed as a whole. */
  state(ops: readonly StateOp[], cwd: string): Promise<Record<string, unknown>[] | undefined>
  /**
   * POST /judge with `body`: the HTTP status and the unwrapped `data`; undefined
   * when the call could not be made or completed (no credential, network error,
   * timeout — the engine adds its own race too), which Python treats as
   * "nothing judged". A 404 is answered `{ status: 404 }`.
   */
  judge(body: Record<string, unknown>): Promise<{ status: number; data?: unknown } | undefined>
  /**
   * The conversation the judge reads (`rule_judge_turn.read_turn`'s input),
   * and an id that is stable for the length of one human turn; undefined when
   * there is none to read.
   */
  turn(): Promise<{ id: string; messages: readonly TurnMessage[] } | undefined>
}

export interface Engine {
  /** Replace the book (already shaped by `toHookRule`). */
  setBook(rules: readonly HookRule[], meta: { repo: string; fetchedAt: number }): void
  pre(e: CallEvent): Promise<Verdict>
  post(e: CallEvent): Promise<Verdict>
  prompt(text: string, sessionId: string, cwd: string): Promise<Verdict>
  /**
   * The session lane. `servedAlready`: the posture preamble is already in the
   * conversation (Python's SessionStart served it at startup, or the module
   * before a hot reload) — the engine then only arms, and records nothing.
   * `ledger`: the posture fire rows, for `rulebook_mod_cli.py log`.
   */
  session(
    sessionId: string,
    cwd: string,
    opts?: { servedAlready?: boolean },
  ): Promise<{ context: string[]; ledger?: Record<string, unknown>[] }>
}
