// Which animal a session shows. Pure: no `$`, so the director hands it what
// the store holds and it answers with a name. See
// docs/specs/companion-upgrades.md §3.1.
//
//   1. this session's own pick (a plain `/hippo` run in it)
//   2. the global pin          (`/hippo always`)
//   3. the pool, indexed by the session id's hash — every animal, minus the
//      ones left out with `/hippo never`
//
// So a session, and a resume of it, always shows the same animal, and a
// `/clear` (a new session id) may show another.

/** The global preferences: one pinned animal, and the ones left out of the pick. */
export type Prefs = { pin: string | null; never: readonly string[] }

/** Each session's own pick, oldest first: `[session id, lowercase name]`. */
export type Sessions = readonly (readonly [sessionId: string, animal: string])[]

/** How many sessions' picks are kept; the oldest go first. */
export const SESSIONS_KEPT = 200

/** FNV-1a, 32-bit, over the UTF-8 bytes of `s`. */
export function fnv1a32(s: string): number {
  let h = 0x811c9dc5
  for (const byte of utf8(s)) {
    h ^= byte
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}

/** UTF-8 by hand: the hooks module's environment promises no TextEncoder. */
function utf8(s: string): number[] {
  const out: number[] = []
  for (const ch of s) {
    const c = ch.codePointAt(0)!
    if (c < 0x80) out.push(c)
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63))
    else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
    else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
  }
  return out
}

const lower = (s: string) => s.toLowerCase()

/** The names the random pick draws from: all of them, minus `never`, never none. */
export function poolOf(names: readonly string[], never: readonly string[]): string[] {
  const out = new Set(never.map(lower))
  const pool = names.filter(n => !out.has(lower(n)))
  return pool.length > 0 ? pool : [...names]
}

/** The animal this session shows, as one of `names`. */
export function pick(names: readonly string[], sessionId: string, sessions: Sessions, prefs: Prefs): string {
  const known = (name: string | null | undefined) =>
    name ? names.find(n => lower(n) === lower(name)) : undefined
  const own = sessionId ? [...sessions].reverse().find(([id]) => id === sessionId) : undefined
  const pool = poolOf(names, prefs.never)
  return known(own?.[1]) ?? known(prefs.pin) ?? pool[fnv1a32(sessionId) % pool.length]!
}

/** `sessions` with this session's pick last, and no more than `cap` of them. */
export function remember(sessions: Sessions, sessionId: string, animal: string, cap = SESSIONS_KEPT): Sessions {
  const kept = sessions.filter(([id]) => id !== sessionId)
  return [...kept, [sessionId, lower(animal)] as const].slice(-cap)
}

/** A stored value read back as Sessions; anything malformed is dropped. */
export function sessionsOf(stored: unknown): Sessions {
  if (!Array.isArray(stored)) return []
  return stored.filter(
    (e): e is [string, string] => Array.isArray(e) && e.length === 2 && typeof e[0] === 'string' && typeof e[1] === 'string',
  )
}

/** Stored preferences read back; a malformed pin or list is treated as absent. */
export function prefsOf(pin: unknown, never: unknown): Prefs {
  return {
    pin: typeof pin === 'string' && pin ? lower(pin) : null,
    never: Array.isArray(never) ? never.filter((n): n is string => typeof n === 'string').map(lower) : [],
  }
}
