# Fixture 05 — missing file reported

Tests: MISSING_FILES.txt and cross-validate.  `main.py` imports
from `core_helpers`, but `core_helpers.py` is never printed in
this transcript.  Pass 7 flags it as a missing module; Pass 9
records it in MISSING_FILES.txt and in the manifest's
`run.missing_files`.

Expected: `main.py` recovered unchanged; `core_helpers.py` appears
in MISSING_FILES.txt and in `manifest.run.missing_files`.

## File: `main.py`

```python
"""main — imports a file that isn't in the transcript."""

from core_helpers import do_save

# Fixture padding.  The classifier requires >= 30 lines to label a
# block `full`; the padding below pushes this file over the threshold.

PAD_01 = 1
PAD_02 = 2
PAD_03 = 3
PAD_04 = 4
PAD_05 = 5
PAD_06 = 6
PAD_07 = 7
PAD_08 = 8
PAD_09 = 9
PAD_10 = 10
PAD_11 = 11
PAD_12 = 12
PAD_13 = 13
PAD_14 = 14
PAD_15 = 15
PAD_16 = 16
PAD_17 = 17
PAD_18 = 18
PAD_19 = 19
PAD_20 = 20
PAD_21 = 21
PAD_22 = 22
PAD_23 = 23
PAD_24 = 24
PAD_25 = 25
PAD_26 = 26
PAD_27 = 27
PAD_28 = 28

VALUE = 1
```
