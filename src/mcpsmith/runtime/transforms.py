"""Value transforms a plan can name on tool arguments (input) and output fields (output)."""

INPUT_NAMES = frozenset(
    {
        "date->unix",
        "date->unix_end",
        "jalali->unix",
        "jalali->unix_end",
        "jalali->date",
        "date->jalali",
        "toman->rial",
        "int->str",
    }
)
OUTPUT_NAMES = frozenset(
    {"unix->date", "unix->datetime", "date->jalali", "jalali->date", "rial->toman", "minutes->hh:mm"}
)
NAMES = INPUT_NAMES | OUTPUT_NAMES
