"""Drive layer: the proactive side of the tool (v0.6).

Why it exists: the product only ever answered when asked. The user proposed a "hormone" model —
slow state variables that make the tool think of the other person more when the relationship is
warm and less when it is not — so that it can occasionally start the conversation *with the user*.
`drive/engine.py` is that model; `drive/nudges.py` writes the message from real evidence;
`drive/actuators.py` is where the message goes (today: the local inbox only).

Defaults: off. When on: at most one nudge a day per relationship, quiet hours respected,
boundary pinned means nothing but self-care, every nudge explainable via `drive_explain`.
"""

from rapport.drive.params import DEFAULT, DriveParams, drive_digest

__all__ = ["DEFAULT", "DriveParams", "drive_digest"]
