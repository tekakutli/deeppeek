# Fixture 10 — scoped `<details>` replace

Tests that 'Replace the NAME `<details>` with:' prose is understood
as a scoped replace: extract scope name from the block body, find
the matching `<details>` element in the pre-existing baseline, and
replace it with the new body.

Runs in `--apply-to` mode; the baseline lives in
`preexisting/10_scoped_details/sample.html`.

Expected: only the Inpainting `<details>` is replaced; the Image and
Other blocks survive unchanged.

## File: `sample.html`

Replace the Inpainting `<details>` with:

```html
<details class="section" open>
  <summary>Inpainting</summary>
  <div class="section-body">
    <div class="field">
      <label>New label</label>
      <input type="checkbox" id="inpaintNewFeature">
    </div>
  </div>
</details>
```
