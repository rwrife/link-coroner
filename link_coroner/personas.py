"""Persona modes for death-certificate narration.

Backlog item from PLAN.md / issue #7. A persona reskins the certificate
copy (titles, summary line, per-cause blurbs) without touching the
underlying diagnosis taxonomy. The default ``coroner`` persona preserves
the original voice so existing tests / users see no behavior change.

Add a persona by registering a :class:`Persona` in :data:`PERSONAS`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .diagnosis import Cause, cause_blurb


@dataclass(frozen=True)
class Persona:
    """A narrator voice for the autopsy report."""

    name: str
    description: str
    certificate_title: str = "CERTIFICATE OF DEATH"
    presumed_title: str = "PRESUMED DEAD"
    alive_message: str = "All URLs are breathing. No certificates required. 🎉"
    summary_title: str = "🪦 autopsy summary"
    # Per-cause overrides; missing causes fall back to the default blurb.
    blurbs: dict[Cause, str] = field(default_factory=dict)
    # Optional callable to post-process the default blurb if no override exists.
    fallback: Callable[[Cause, str], str] | None = None

    def blurb(self, cause: Cause) -> str:
        if cause in self.blurbs:
            return self.blurbs[cause]
        base = cause_blurb(cause)
        if self.fallback is not None:
            return self.fallback(cause, base)
        return base


_NOIR = Persona(
    name="noir-detective",
    description="A rain-soaked gumshoe narrates each corpse.",
    certificate_title="CASE FILE — STIFF FOUND",
    presumed_title="MISSING PERSONS REPORT",
    alive_message="Quiet night. Every link still has a pulse. I poured a drink anyway.",
    summary_title="🕵️  case ledger",
    blurbs={
        Cause.ALIVE: "Still breathing. For now.",
        Cause.NXDOMAIN: "No such name on the books. Whoever lived here was never here.",
        Cause.DNS_FAILURE: "The phonebook came back blank. Somebody tore the page out.",
        Cause.CONN_REFUSED: "Knocked twice. Door slammed. Saw a curtain twitch.",
        Cause.TLS_EXPIRED: "Their papers expired. Walked right past the bouncer anyway.",
        Cause.TLS_ERROR: "Forged credentials at the door. I don't trust 'em.",
        Cause.HTTP_4XX: "Sign on the door says 'go away.' Polite, almost.",
        Cause.HTTP_5XX: "Place is on fire inside. Sirens already coming.",
        Cause.TIMEOUT: "Waited in the rain. Nobody came down.",
        Cause.REDIRECT_LOOP: "They sent me in circles. Old trick. Bad one.",
        Cause.BAD_URL: "The address ain't even an address. Somebody's playing games.",
        Cause.SOFT_404: "Says 'open for business.' Inside? Empty shelves and a chalk outline.",
        Cause.PARKED: "Whole joint's been bought up. The body got sold with the deed.",
        Cause.UNKNOWN: "Can't make heads or tails of this one. Yet.",
    },
)


_VICTORIAN = Persona(
    name="victorian-doctor",
    description="A 19th-century physician pens each obituary in florid prose.",
    certificate_title="POST-MORTEM EXAMINATION",
    presumed_title="DECLARATION OF PRESUMED DEMISE",
    alive_message="I am pleased to report the patients are, on the whole, in robust health.",
    summary_title="📜 register of the deceased",
    blurbs={
        Cause.ALIVE: "The constitution appears sound; humours in balance.",
        Cause.NXDOMAIN: "No record of such a soul in the parish ledger.",
        Cause.DNS_FAILURE: "The telegraph office offered no reply to repeated inquiry.",
        Cause.CONN_REFUSED: "The door was barred against our most courteous knock.",
        Cause.TLS_EXPIRED: "The papers of credential have lapsed; trust withdrawn.",
        Cause.TLS_ERROR: "An irregularity in the patient's papers — forgery suspected.",
        Cause.HTTP_4XX: "The proprietor declined to receive us. Most uncivil.",
        Cause.HTTP_5XX: "Hemorrhage of the inner faculties; the staff in disarray.",
        Cause.TIMEOUT: "We waited the appointed hour. The patient did not present.",
        Cause.REDIRECT_LOOP: "Conducted in maddening circles by a most evasive footman.",
        Cause.BAD_URL: "The address provided was nonsense — a child's scribble.",
        Cause.SOFT_404: "Outwardly hale, yet upon examination — entirely hollow.",
        Cause.PARKED: "The estate has been auctioned. New owners; foul intent suspected.",
        Cause.UNKNOWN: "Cause obscure. Further dissection warranted.",
    },
)


_PHOTOGRAPHER = Persona(
    name="crime-scene-photographer",
    description="Terse, technical captions from behind the lens.",
    certificate_title="EXPOSURE LOG — SUBJECT DECEASED",
    presumed_title="EXPOSURE LOG — SUBJECT MISSING",
    alive_message="Roll developed. No fatalities in frame.",
    summary_title="📷  contact sheet",
    blurbs={
        Cause.ALIVE: "Subject upright. Pulse visible. Frame clean.",
        Cause.NXDOMAIN: "No subject at coordinates. Empty lot. Wide shot only.",
        Cause.DNS_FAILURE: "Address unreadable in viewfinder. Fog. Try again.",
        Cause.CONN_REFUSED: "Door closed. No entry. Captured exterior, 1/60s f/4.",
        Cause.TLS_EXPIRED: "Credentials past date. Documented for evidence.",
        Cause.TLS_ERROR: "Identification papers suspect. Close-up attached.",
        Cause.HTTP_4XX: "Subject refused entry. Notice posted on door — photographed.",
        Cause.HTTP_5XX: "Scene chaotic. Multiple failures. Wide and detail shots.",
        Cause.TIMEOUT: "Long exposure ran out. Subject never arrived in frame.",
        Cause.REDIRECT_LOOP: "Followed subject in circles. Same alley, three exposures.",
        Cause.BAD_URL: "Coordinates invalid. No location to photograph.",
        Cause.SOFT_404: "Front looks normal. Through the window: empty.",
        Cause.PARKED: "New 'For Sale' sign in frame. Previous tenant: gone.",
        Cause.UNKNOWN: "Image inconclusive. Recommend re-shoot.",
    },
)


_DEADPAN = Persona(
    name="deadpan-medical-examiner",
    description="A bored M.E. dictates findings between sips of cold coffee.",
    certificate_title="EXAMINER'S REPORT",
    presumed_title="EXAMINER'S REPORT (PENDING)",
    alive_message="Nothing on the table. Going back to my coffee.",
    summary_title="🧾 morgue log",
    blurbs={
        Cause.ALIVE: "Patient: fine. Next.",
        Cause.NXDOMAIN: "No body. No record. Wasting my time.",
        Cause.DNS_FAILURE: "DNS shrugged. So do I.",
        Cause.CONN_REFUSED: "Knocked. Got nothing. Moving on.",
        Cause.TLS_EXPIRED: "Cert expired. Real shocker.",
        Cause.TLS_ERROR: "TLS broke. I don't get paid for handshakes.",
        Cause.HTTP_4XX: "Server says no. Believable.",
        Cause.HTTP_5XX: "Server died screaming. I've seen worse.",
        Cause.TIMEOUT: "Waited. Didn't bother showing up.",
        Cause.REDIRECT_LOOP: "Round and round. I got dizzy. Marked it dead.",
        Cause.BAD_URL: "Not a URL. Whoever wrote this should be on the table instead.",
        Cause.SOFT_404: "Looks alive. Isn't. Classic.",
        Cause.PARKED: "Domain squatter. The vultures got here first.",
        Cause.UNKNOWN: "Inconclusive. Filing it anyway.",
    },
)


_DEFAULT = Persona(
    name="coroner",
    description="The default forensic-pathologist voice (formal but dry).",
)


PERSONAS: dict[str, Persona] = {
    p.name: p
    for p in (_DEFAULT, _NOIR, _VICTORIAN, _PHOTOGRAPHER, _DEADPAN)
}

DEFAULT_PERSONA = "coroner"


def get_persona(name: str | None) -> Persona:
    """Look up a persona by name (case-insensitive). Falls back to default."""
    if not name:
        return PERSONAS[DEFAULT_PERSONA]
    key = name.strip().lower()
    if key not in PERSONAS:
        available = ", ".join(sorted(PERSONAS))
        raise ValueError(f"Unknown persona '{name}'. Available: {available}")
    return PERSONAS[key]


def list_personas() -> list[Persona]:
    """All registered personas, default first."""
    default = PERSONAS[DEFAULT_PERSONA]
    rest = [p for n, p in sorted(PERSONAS.items()) if n != DEFAULT_PERSONA]
    return [default, *rest]
