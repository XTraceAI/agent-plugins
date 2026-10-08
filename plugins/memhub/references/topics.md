# Brain topics — what to pass as `topic` when you save

A brain's **topics** are its chapters: the subject area a reader would look
under (`billing`, `rulebook`, `search`). The brain's table of contents is built
from them. `save_artifact` takes one as `topic` (`save_artifact.py --topic`),
separate from `tags`: tags are free filters, the topic is the chapter.

## Choosing one — in this order

1. **An existing topic of the brain, whenever one fits — even loosely.**
   Reuse beats a near-duplicate.
2. **A new topic only when the document is clearly about a subject none of
   them covers**, and broad enough that other documents will land there too:
   one to three lowercase words joined by underscores. Never the KIND of
   document (`spec`, `pr_review`, `notes`, `report` are refused), never the
   repository, never an identifier (a PR number, a ticket, a file path).
3. **`"unsorted"`** only when neither applies. It is an answer, not a shrug:
   the artifact is listed under Unsorted until a tidy pass files it.

## Seeing the brain's topics

- `get_brain_overview(agent_brain_id)` names the brain's subjects.
- `list_tags(agent_brain_id)` lists its tags; the topics are among them.
- Simplest: save with your best choice. A brain that requires a topic refuses
  a NEW artifact without one, and the refusal lists every topic with its
  label and abstract — pick from it and re-run.

## What the server does with it

- A new version keeps its lineage's topic when you omit `topic`; pass one only
  to move it.
- A new topic you name is added to the brain on save. If it is spelled close
  to an existing one the reply says so (`topic.near`) — re-save with the
  existing one if that is what you meant.
- Outside a brain (personal memory), or in a brain without topics turned on,
  `topic` is kept as an ordinary tag.
- Automatic markdown capture (`md_capture_flush.py`) passes no topic: it is a
  script with nobody to judge, and the server's fallback picker files it.
