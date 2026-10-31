"""Model registry — maps the model name aider sends to a ModelSpec."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    max_input_tokens: int = 128_000
    max_output_tokens: int = 8_192


REGISTRY: dict[str, ModelSpec] = {
    "deepseek-web": ModelSpec(id="deepseek-web"),
    "deepseek-reasoner-web": ModelSpec(id="deepseek-reasoner-web"),
}

DEFAULT_MODEL = "deepseek-web"


def resolve_model(name: str) -> ModelSpec:
    """Return the ModelSpec for ``name``, stripping any provider prefix.

    aider sends ``openai/<model>``.  Strip the prefix and look up the
    bare name.  Unknown models get a default spec — no up-front
    rejection, because aider's ``--model`` flag can be any string.
    """
    bare = (name or "").split("/", 1)[-1] or DEFAULT_MODEL
    return REGISTRY.get(bare, ModelSpec(id=bare))