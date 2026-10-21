"""LaTeX delimiter normalisation: \\( … \\) -> $ … $ ; \\[ … \\] -> $$ … $$."""
import re


def convert_latex_delimiters(text: str) -> str:
    text = re.sub(r"\\\((.+?)\\\)", r"$\1$", text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.+?)\\\]", r"$$\1$$", text, flags=re.DOTALL)
    return text
