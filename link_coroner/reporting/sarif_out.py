"""SARIF 2.1.0 output (M6).

Emits a SARIF log that GitHub code scanning, Azure DevOps, and other
SARIF-aware dashboards can ingest. Each deceased / suspicious URL becomes a
``result`` with a stable ``ruleId`` matching the ``Cause`` taxonomy.

We deliberately emit a single run with a single tool driver
(``link-coroner``). Locations point at the URL itself via
``logicalLocations`` because we don't track the source file/line at this
layer yet (TODO: thread file/line metadata through ProbeResult — see #11).
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from .. import __version__
from ..diagnosis import Cause, cause_blurb, diagnose
from ..forensics.probe import ProbeResult, Verdict

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/"
    "Schemata/sarif-schema-2.1.0.json"
)

_LEVEL_FOR_VERDICT = {
    Verdict.ALIVE: "none",
    Verdict.DEAD: "error",
    Verdict.UNREACHABLE: "warning",
}


def _rules() -> list[dict]:
    return [
        {
            "id": cause.value,
            "name": cause.value,
            "shortDescription": {"text": cause.value.replace("_", " ").title()},
            "fullDescription": {"text": cause_blurb(cause)},
            "defaultConfiguration": {
                "level": "error" if cause is not Cause.ALIVE else "none"
            },
        }
        for cause in Cause
    ]


def render_sarif(results: Iterable[ProbeResult]) -> str:
    """Render an iterable of ``ProbeResult`` as a SARIF 2.1.0 JSON string."""
    results = list(results)
    sarif_results: list[dict] = []
    for r in results:
        if r.verdict is Verdict.ALIVE:
            continue
        cause = diagnose(r)
        sarif_results.append(
            {
                "ruleId": cause.value,
                "level": _LEVEL_FOR_VERDICT.get(r.verdict, "warning"),
                "message": {
                    "text": f"{cause.value}: {r.reason} ({r.url})",
                },
                "locations": [
                    {
                        "logicalLocations": [
                            {"name": r.url, "kind": "url"}
                        ],
                        "properties": {
                            "url": r.url,
                            "finalUrl": r.final_url,
                            "statusCode": r.status_code,
                            "verdict": r.verdict.value,
                        },
                    }
                ],
            }
        )

    doc = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "link-coroner",
                        "version": __version__,
                        "informationUri": "https://github.com/rwrife/link-coroner",
                        "rules": _rules(),
                    }
                },
                "results": sarif_results,
            }
        ],
    }
    return json.dumps(doc, indent=2) + "\n"
