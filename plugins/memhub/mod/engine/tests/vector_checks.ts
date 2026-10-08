// The Python exception a port error stands for, for replaying `raises`
// vectors. The port's error classes carry `pyName` (PyValueError,
// PyReError, the ordering/rules PyTypeError and PyKeyError); anything else a
// port throws (a plain JS TypeError from a bug, say) has none, so it never
// matches a Python exception by accident.
export function pyKind(e: unknown): string {
  const n = (e as { pyName?: unknown } | null)?.pyName
  if (typeof n === 'string') return n
  return `untyped ${e instanceof Error ? e.name : typeof e} (${String(e).slice(0, 120)})`
}

/** Vector counts are a ratchet, not a "> some round number" floor: each
 *  suite records the count it was last generated with, and a run must carry
 *  at least `RATCHET` of it, so a layer that loses a few percent of its cases
 *  (a broken loop in a case file, a corpus that stopped loading) fails rather
 *  than passing on whatever is left. When cases are added, raise the
 *  recorded count in the suite to the new total. */
export const RATCHET = 0.95

/** The floor for a count recorded as `recorded`. */
export const floorOf = (recorded: number): number => Math.ceil(recorded * RATCHET)

/** `null` when `n` holds the ratchet, else why not. */
export function belowRatchet(what: string, n: number, recorded: number): string | null {
  return n >= floorOf(recorded) ? null
    : `${what}: ${n} vectors, under ${floorOf(recorded)} (${RATCHET * 100}% of the recorded ${recorded})`
}
