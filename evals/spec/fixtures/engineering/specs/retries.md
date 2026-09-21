---
spec: retries
owns:
  - app/retries.py
last_verified_at: null
---
# Retry policy
Transient errors receive at most three attempts. Permanent errors receive one.
Changing this limit requires an explicit product decision.
