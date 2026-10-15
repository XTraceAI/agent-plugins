// The §4b forward test's claim, as the shell honours it: a call it covers is
// handed to the Python hook (lanes.ts `handForeign`), which reads the claim in
// full (`rulebook_hook._redirect_base`) and serves the test's private book.
//
// /memhub:create-rule step 4b proves a candidate rule fires before filing it:
// it copies the rulebook state to a private base, adds the candidate as a
// `candidate-…` row, writes `<real base>/pretest-redirect.json` naming that
// base and the directory prefix it covers (`<repo>/.claude/worktrees`), and
// runs the violating command in an Agent-tool fork isolated in a worktree
// there. The mod drops candidate rows (book.ts `hookRuleFor`, activeBase
// false) and its lane claim is keyed on the session id, which a fork shares
// with its parent — so with no hand-off the fork's calls were judged on the
// real book, nothing fired, and the skill refused to file (ENG-1250).
//
// This decides only whether to hand off; Python decides what the claim means.
// The uid, mode and `base` checks are Python's: a call handed over under a
// claim Python rejects is served by Python from the real base, which is the
// same answer the mod would have given. So every doubt here reads as "hand
// off" only where the claim is plainly live, and as "serve" otherwise.

import { REDIRECT_MAX_AGE_S, abspath, normpath, under } from './engine/shell'

export const REDIRECT_NAME = 'pretest-redirect.json'

/** What the check reads: the file's text, its stat, the clock. */
export type RedirectIo = {
  /** Resolves undefined when the file is missing or unreadable. */
  readText(path: string): Promise<string | undefined>
  /** `realPath` with `resolve`; undefined when the path does not exist. */
  stat(path: string, resolve?: boolean): Promise<{ mtimeMs: number; realPath?: string } | undefined>
  /** Epoch milliseconds. */
  now(): Promise<number>
}

/**
 * True when a live forward-test claim under `realBase` should steer this call
 * to the Python hook:
 *
 *  - the session's cwd is inside the claimed prefix (the harness's shape: a
 *    session started in the test worktree), checked on the spelling and, where
 *    both resolve, on the real paths, as Python checks them; or
 *  - the call is a sub-agent's (`agentId`): its own cwd is the isolated
 *    worktree, which the shell's `session.cwd()` does not report, so Python,
 *    which reads the call's cwd off the hook payload, is the one that can tell.
 *
 * Never when there is no claim file, when it is older than REDIRECT_MAX_AGE_S
 * (a forgotten one from an interrupted run), or when it names no prefix.
 */
export async function forwardTestCovers(
  io: RedirectIo, realBase: string | undefined, cwd: string, agentId: string | undefined,
): Promise<boolean> {
  if (!realBase) return false
  const path = `${realBase}/${REDIRECT_NAME}`
  try {
    const text = await io.readText(path)
    if (text === undefined) return false
    const st = await io.stat(path)
    if (!st || (await io.now()) - st.mtimeMs > REDIRECT_MAX_AGE_S * 1000) return false
    const data: unknown = JSON.parse(text)
    if (!data || typeof data !== 'object' || Array.isArray(data)) return false
    const prefix = (data as Record<string, unknown>).cwd_prefix
    if (typeof prefix !== 'string' || !prefix) return false
    if (cwd) {
      const here = normpath(abspath(cwd))
      const pre = normpath(abspath(prefix))
      if (under(here, pre)) {
        const [rh, rp] = await Promise.all([io.stat(here, true), io.stat(pre, true)])
        if (!rh?.realPath || !rp?.realPath || under(rh.realPath, rp.realPath)) return true
      }
    }
    return agentId !== undefined
  } catch {
    return false
  }
}
