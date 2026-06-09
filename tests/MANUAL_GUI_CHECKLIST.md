# Manual GUI checklist (browser-only behaviors)

The web walkthrough (`tt-prism serve <file>`, served by `flow.js` / `app.js`)
runs in a browser and cannot be exercised by the Python test suite (this
environment has no fastapi / node / browser). The items below must be verified
**by hand**. Each one is backed, where possible, by a Python *data-precondition*
test that checks the data the GUI consumes is well-formed — if those pass and the
behavior is still wrong, the bug is in the JS layer, not the data.

How to run the GUI for manual checks (needs the web extras + a browser):

```bash
pip install -e '.[web]'         # fastapi + uvicorn
tt-prism serve examples/deepseek/decoder_walkthrough.yaml
```

## Checklist

1. **Stepping advances frames** — Prev / Next / Play walk through every op in
   YAML order; within a compute op, Next steps its dataflow before advancing to
   the next op; Reset returns to frame 0.
   - Backed by: `test_contracts.py::test_flow_payload_*` (ops present in order,
     each compute op carries `steps`, init/movement carry none).

2. **Highlighting toggles classes** — the current stage / datum token / computing
   core gets a highlight class added, and it is removed when you move off it (no
   stale highlights left behind).
   - Backed by: `test_contracts.py::test_flow_payload_compute_steps_within_stages_and_canonical`
     (every highlighted stage a step references exists in the op's `stages`, so
     there is always a target element to toggle).

3. **Transfer arrows are drawn** — a data-movement op draws an arrow on the chip
   grid from each transfer's `from` core to its `to` core.
   - Backed by: `test_contracts.py::test_flow_payload_movement_op_has_transfers_to_real_cores_and_no_steps`
     (transfers reference real cores) and `chip_payload` cross-core interaction
     tests.

4. **Panels don't visually overlap** — the Tensix engine panel, chip grid, and op
   list lay out without overlapping at common window sizes.
   - Backed by (SVG Gantt analogue): `test_render_geometry.py` (lane rows and bar
     bands don't overlap; bars stay inside lanes and the canvas).

5. **Datum token placement** — each datum token sits in the column of the stage
   it currently occupies; tokens move between columns as steps advance.
   - Backed by: `test_contracts.py` stage/step containment checks +
     `test_flow.py::test_flow_step_data_identity`.
