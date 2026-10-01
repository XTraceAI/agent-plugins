// FNV-1a 32-bit of each session id, computed by Python. Shared with
// tests/companion_contract_test.py, which reads the JSON array after the `=`
// and recomputes every hash itself — keep it plain JSON.
export const VECTORS: readonly { s: string; h: number }[] = [
  {
    "s": "",
    "h": 2166136261
  },
  {
    "s": "a",
    "h": 3826002220
  },
  {
    "s": "foobar",
    "h": 3214735720
  },
  {
    "s": "3611ce9d-5dfd-4c37-bc53-96da86d04828",
    "h": 879333932
  },
  {
    "s": "dcb61524-431d-489d-9814-0df5fb0d4718",
    "h": 1890717460
  },
  {
    "s": "séance-🦆",
    "h": 1478195025
  }
]
