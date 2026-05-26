import re
import unicodedata


def slugify(value: str, max_length: int = 80) -> str:
    """Convert a string to a url-safe, filesystem-safe slug.

    Folds unicode (Allianz Österreich -> allianz-oesterreich), strips
    everything that isn't [a-z0-9-], collapses dashes, trims length.
    """
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    value = value.replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_length].rstrip("-") or "untitled"
