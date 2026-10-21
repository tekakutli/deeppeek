# Fixture 06 — superset detection

Tests: dedup by verbatim superset.  `foo.py` is printed twice; the
longer print contains the shorter one verbatim at its start, and
the length ratio exceeds 1.3.  Pass 5's superset signal picks the
long print even though the modifier does not disambiguate.

Expected: `foo.py` = the long print (with EXTRA_01..EXTRA_20).

## File: `foo.py`

```python
"""foo — short version."""

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
```

## File: `foo.py`

```python
"""foo — short version."""

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

EXTRA_01 = 1
EXTRA_02 = 2
EXTRA_03 = 3
EXTRA_04 = 4
EXTRA_05 = 5
EXTRA_06 = 6
EXTRA_07 = 7
EXTRA_08 = 8
EXTRA_09 = 9
EXTRA_10 = 10
EXTRA_11 = 11
EXTRA_12 = 12
EXTRA_13 = 13
EXTRA_14 = 14
EXTRA_15 = 15
EXTRA_16 = 16
EXTRA_17 = 17
EXTRA_18 = 18
EXTRA_19 = 19
EXTRA_20 = 20
```
