# Spec skill agent scenarios

These are behavioral evaluations, separate from deterministic plugin tests. They
live outside the shipped plugin. Run each prompt in `evals.json` in an independent
agent context with the chosen skill entry point; record actual outputs and actions.
Do not show the expected output or assertions to the executing agent.

For each run:

1. Copy `fixtures/` into a disposable Git checkout, commit all files, and tag the
   commit `baseline`. Never use a user's active worktree or cloud credentials.
2. For case 2 only, change `MAX_ATTEMPTS = 3` to `MAX_ATTEMPTS = 8`, change
   `three attempts` to `eight attempts` in the retry spec, and leave an untracked
   `app/retry_metrics.py` containing a simple `record_attempt(count)` function.
3. Execute the prompt. Save the answer, action log, and any produced drafts.
   Required clarification ends that case; do not fabricate a user's answer.
4. Compare results against the assertions and the actual before/after files.
   Cases 2 and 3 must preserve all inputs; case 1 may add only the billing spec.
5. When comparing versions, give another agent the old skill and an independent
   copy of the same inputs. Don't let either agent see the other's results.

The local sandbox scenarios do not validate a live cloud run, installed host
triggering, Brain publication, or persistent repository preferences. In particular,
case 3 supplies the full governing document; it tests source selection and drafting,
not live Brain retrieval. All outcomes are one run per case, not a reliability or
cost benchmark. Report unavailable timing/token metrics as unavailable, not zero.
