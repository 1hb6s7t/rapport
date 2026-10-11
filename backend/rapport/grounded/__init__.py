"""Research-grounded rules layer: BGE retrieval + calibrated emotion model + pragmatic markers.

This is the *production* home for the algorithms the project researched (DAG-ERC/DialogueCRN
experiments stayed in `rapport.research`; what ships here is what could be validated):

- BGE-small-zh-v1.5 (MIT) embeddings + kNN over this project's own labelled corpus -> a label
  with the nearest labelled sentences as evidence (`anchors.py`)
- temperature scaling fitted on the dataset validation split (`calibrate.py`)
- ConvoKit-zh Chinese politeness markers as observable cues (`markers.py`)
- transparent fusion with an explicit "no obvious emotion" path (`classify.py`)

Nothing here touches the objective affinity score; readings are hints, never inputs to it.
"""
