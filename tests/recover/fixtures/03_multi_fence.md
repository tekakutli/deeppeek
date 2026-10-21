# Fixture 03 — snippet + full file under one header

Tests: snippet exclusion via size threshold.  Under one
`## File:` header, two fences appear: a short snippet (classified
`partial` by the size heuristic, sits before the full print) and
a long full print (classified `full`).  Pass 5 picks the full;
the pre-full partial is not applied as a patch.

Expected: `foo.py` = the full print.

## File: `foo.py`

Here's a snippet from the middle of the file:

```python
def middle():
    pass
```

And the full file:

```python
"""foo — full file."""

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
