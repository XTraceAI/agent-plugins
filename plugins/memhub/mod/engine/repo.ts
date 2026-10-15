// Which checkout a call works in: a port of rulebook_hook.py repo_info,
// _branch, _call_seed, repo_of_call, command_root and worktree_key, over
// EngineIO (stat / readText) instead of os.path.
//
// The repo NAME is the one thing not re-derived here: repo_identity resolves
// it from the remote URL, and `rulebook_mod_cli.py paths` answers it with the
// Python's own code (EngineIO.paths, asked once per checkout root).

import type { EngineIO } from './index'
import { dirname, pyMatch, sha1Hex } from './py'
import { pyStrip } from './rules'
import { findOverride, stripOverride } from './override'
import { basename, bashReads, bashWriteTargets, callDir, expanduser, isabs, join, normpath, type Env } from './shell'

export type RepoInfo = { repo: string; root: string; gitdir: string; branch: string }
export const NO_REPO: RepoInfo = { repo: '', root: '', gitdir: '', branch: '' }

const ARG = String.raw`(?:'([^']*)'|"([^"]*)"|([^\s;&|]+))`
const CD_PREFIX = String.raw`^\s*cd\s+` + ARG + String.raw`\s*(?:&&|;)`

/** `worktree_key(root)`: the 16-hex checkout id. */
export const worktreeKey = (root: string): string | null => (root ? sha1Hex(root).slice(0, 16) : null)

/** `state_path`'s file name for a session. */
export const sessionFileName = (session: string): string =>
  (String(session ?? '').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 80) || 'nosession') + '.json'

export class Repos {
  /** root → repo name, for the life of the process (a checkout keeps its remote). */
  private names = new Map<string, string>()

  constructor(private io: EngineIO) {}

  /** `_branch(head_path)` */
  async branchOf(headPath: string): Promise<string> {
    const raw = await this.io.readText(headPath)
    if (raw === undefined) return ''
    const h = pyStrip(raw)
    const m = /^ref:\s*refs\/heads\/(.+)$/.exec(h)
    if (m) return m[1]!
    if (h.startsWith('ref:')) return pyStrip(h.slice(h.indexOf(':') + 1))
    return /^[0-9a-f]{40,64}$/.test(h) ? 'detached@' + h : 'detached'
  }

  /** `_repo_name(root, gitdir)` through the Python, cached per root; the basename when it cannot answer. */
  async nameOf(root: string): Promise<string> {
    const known = this.names.get(root)
    if (known !== undefined) return known
    let name = ''
    try {
      const p = await this.io.paths(root)
      if (p && p.root === root) name = p.repo
    } catch {
      name = ''
    }
    if (!name) name = basename(root)
    else this.names.set(root, name)
    return name
  }

  /** Seed a name already known (the book's own checkout). */
  remember(root: string, name: string): void {
    if (root && name) this.names.set(root, name)
  }

  /** `repo_info(cwd)`: (name, worktree root, gitdir, branch), walking up to the first `.git`. */
  async repoInfo(cwd: string, withName = true): Promise<RepoInfo> {
    const nameOf = (d: string) => (withName ? this.nameOf(d) : Promise.resolve(basename(d)))
    let d = normpath(isabs(cwd || '') ? cwd : join('/', cwd || ''))
    for (;;) {
      const g = join(d, '.git')
      const st = await this.io.stat(g)
      if (st?.kind === 'dir') {
        return { repo: await nameOf(d), root: d, gitdir: g, branch: await this.branchOf(join(g, 'HEAD')) }
      }
      if (st?.kind === 'file') {
        let gitdir = ''
        const text = await this.io.readText(g)
        const i = text === undefined ? -1 : text.indexOf(':')
        if (text !== undefined && i >= 0) {
          gitdir = pyStrip(text.slice(i + 1))
          if (!isabs(gitdir)) gitdir = normpath(join(d, gitdir))
        }
        return { repo: await nameOf(d), root: d, gitdir, branch: await this.branchOf(join(gitdir, 'HEAD')) }
      }
      const parent = dirname(d)
      if (parent === d || !d) break
      d = parent
    }
    return NO_REPO
  }

  private async realpath(p: string): Promise<string | undefined> {
    return (await this.io.stat(p, true))?.realPath
  }

  /** `_call_seed(cwd, data)` for a Claude Code call: the directory the call acts in, or "". */
  callSeed(cwd: string, tool: string, inp: Readonly<Record<string, unknown>>, env: Env): string {
    for (const key of ['file_path', 'notebook_path']) {
      const fp = inp[key]
      if (typeof fp !== 'string' || !pyStrip(fp) || fp.includes('\0')) continue
      let d = dirname(fp.replace(/\\/g, '/'))
      if (!isabs(d)) d = join(cwd, d)
      return normpath(d)
    }
    const command = inp.command
    if (tool === 'Bash' && typeof command === 'string') return callDir(cwd, command, env)
    return ''
  }

  /** `command_root(cwd, command)`: the worktree a leading `cd <dir> &&|;` runs the command in, else "". */
  async commandRoot(cwd: string, command: string): Promise<string> {
    const m = pyMatch(CD_PREFIX, command || '')
    if (!m) return ''
    let path = [m[1], m[2], m[3]].find((g) => g) ?? ''
    if (!path) return ''
    path = expanduser(path, this.io.home)
    if (!isabs(path)) path = join(cwd || '', path)
    if ((await this.io.stat(path))?.kind !== 'dir') return ''
    return (await this.repoInfo(path)).root
  }

  /**
   * `repo_of_call(data)` for a Claude Code call (no apply_patch): the checkout
   * the call acts in — the file it edits or reads, wherever it is, or where
   * its Bash command runs — else the cwd's. A target that is the cwd's own
   * checkout spelled another way answers with the cwd's spelling (one
   * worktree, one ordering key).
   */
  async ofCall(cwd: string, tool: string, inp: Readonly<Record<string, unknown>>, env: Env = {}): Promise<RepoInfo> {
    const here = await this.repoInfo(cwd)
    const seed = this.callSeed(cwd, tool, inp, env)
    if (seed) {
      const info = await this.repoInfo(seed)
      if (info.repo) {
        if (here.repo && info.root !== here.root &&
            ((await this.realpath(info.root)) ?? info.root) === ((await this.realpath(here.root)) ?? here.root)) return here
        return info
      }
    }
    // Neither the cwd nor where the command runs is a checkout: the first
    // file the command writes or reads that is in one decides (`_bash_targets`).
    if (!here.repo) {
      for (const p of this.bashTargets(cwd, tool, inp, env)) {
        const info = await this.repoInfo(dirname(p))
        if (info.repo) return info
      }
    }
    return here
  }

  /** `_bash_targets(cwd, data)`: the files a Bash call writes, then reads (override stripped); [] otherwise. */
  bashTargets(cwd: string, tool: string, inp: Readonly<Record<string, unknown>>, env: Env): string[] {
    let cmd = inp.command
    if (tool !== 'Bash' || typeof cmd !== 'string' || !cmd) return []
    try {
      const found = findOverride(cmd)
      if (found) cmd = stripOverride(cmd, found)
      return [
        ...bashWriteTargets(cwd, cmd as string, env).map(([p]) => p),
        ...bashReads(cwd, cmd as string, this.io.home).map(([p]) => p),
      ]
    } catch {
      return []
    }
  }
}
