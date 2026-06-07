# Reference material (not wired into the app yet)

Design references kept for features we plan to add. Nothing here is imported by
`tt_prism`; these are inspiration / target artifacts.

## `fast_untilize_diagram.html`

A self-contained interactive **dataflow / step-timeline** view of Blackhole
`fast_untilize` vs regular `pack_untilize` (open it in a browser). It shows, for
one example:

- the **steps** of the op and how **tiles flow** through the Tensix Engine
  stages (UNPACK → DEST → PACK …), advanced with a step timeline / progress
  control;
- per-stage **instruction work counts**, including **NOPs**;
- **metrics** (e.g. expected bandwidth) and implementation notes.

### Why it's here

This is the view style we want to grow into. Future direction (not yet built):

- Let the user **select one or more ops** from the main swim-lane view we're
  building, and **generate this flow graph in-app** for just those ops — i.e. a
  per-op "how does data move through Tensix" drill-down.
- Surface extra modeled quantities we don't track yet: **expected bandwidth**,
  **NOP counts**, instruction-work breakdowns per stage.

When we implement that, this file is the visual target to match.
