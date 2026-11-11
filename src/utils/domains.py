"""Email address and domain helpers shared across modules."""

from email.utils import parseaddr

# Characters/sequences attackers swap in to make a domain look like another one.
_HOMOGLYPHS = (("rn", "m"), ("vv", "w"), ("0", "o"), ("1", "l"), ("i", "l"), ("_", "-"))


def parse_sender(header: str | None) -> tuple[str, str]:
    """'Acme Components <sales@acme.com>' -> ('Acme Components', 'acme.com')."""
    name, address = parseaddr(header or "")
    domain = address.rpartition("@")[2].lower().strip(".") if "@" in address else ""
    return name.strip(), domain


def same_organization(domain: str, base: str) -> bool:
    """True for the domain itself or a subdomain of it (mail.acme.com belongs to acme.com)."""
    domain, base = domain.lower(), base.lower()
    return bool(domain) and (domain == base or domain.endswith("." + base))


def related_domains(a: str, b: str) -> bool:
    """True if either domain is the other or a subdomain of it."""
    return same_organization(a, b) or same_organization(b, a)


def looks_like(domain: str, base: str) -> bool:
    """True if `domain` imitates `base` without being it, e.g. acme-cornponents.com,
    acme-components.co or acrne-components.com for acme-components.com."""
    domain, base = domain.lower(), base.lower()
    if not domain or same_organization(domain, base):
        return False
    label, _, tld = domain.rpartition(".")
    base_label, _, base_tld = base.rpartition(".")
    if label == base_label and tld != base_tld:  # same name, different TLD
        return True
    if domain.startswith(base + "."):  # acme-components.com.evil.io
        return True
    if _skeleton(domain) == _skeleton(base):  # homoglyph swap
        return True
    return len(base) >= 6 and edit_distance(domain, base) <= 2  # a typo or two away


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (insertions, deletions, substitutions)."""
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


def _skeleton(domain: str) -> str:
    for fake, real in _HOMOGLYPHS:
        domain = domain.replace(fake, real)
    return domain
