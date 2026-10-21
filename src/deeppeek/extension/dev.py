"""Print about:debugging instructions for extension iteration."""
import sys


def print_dev_instructions() -> int:
    print(
        "Extension dev loop:\n"
        "  1. open about:debugging#/runtime/this-firefox in Zen\n"
        "  2. click 'Load Temporary Add-on…'\n"
        "  3. select src/deeppeek/extension/assets/manifest.json\n"
        "  4. reload the DeepSeek tab and click the DEEPPEEK toolbar button\n"
        "\n"
        "Temporary installs survive no browser restart.\n"
        "Promote with `deeppeek extension install` when ready.",
        file=sys.stdout,
    )
    return 0
