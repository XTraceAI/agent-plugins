// The golden-vector harness itself: generated files load and carry cases.
//
// The vectors are never committed: they hold real rule patterns and ~2 MB an
// install must not ship. `bash scripts/test-mod.sh` generates them into the
// gitignored ./vectors/ (scripts/rule_vectors.py, Python 3.14), runs
// `claude plugin test`, and removes them. Under a bare `claude plugin test`
// every vector suite fails to load ("cannot import ./vectors/..."); run the
// script instead.
import { test, expect } from 'claude-code/testing'
import { VECTORS } from './vectors/smoke'

test('generated vectors load as data', () => {
  expect(VECTORS.length).toBeGreaterThan(0)
  for (const v of VECTORS) expect(typeof v.fn).toBe('string')
})
