"""Research provenance is separate from actual reproduction status."""

PAPERS = [
    {"id": "dialoguegcn", "name": "DialogueGCN", "url": "https://aclanthology.org/D19-1015/"},
    {"id": "dag-erc", "name": "DAG-ERC", "url": "https://aclanthology.org/2021.acl-long.123/"},
    {"id": "dialoguecrn", "name": "DialogueCRN", "url": "https://aclanthology.org/2021.acl-long.547/"},
    {"id": "cosmic", "name": "COSMIC", "url": "https://aclanthology.org/2020.findings-emnlp.224/"},
    {"id": "dialogxl", "name": "DialogXL", "url": "https://github.com/shenwzh3/DialogXL"},
    {"id": "convokit", "name": "ConvoKit", "url": "https://github.com/CornellNLP/ConvoKit"},
    {"id": "ruptures", "name": "ruptures", "url": "https://github.com/deepcharles/ruptures"},
]


def research_catalog():
    return [dict(x, replication_status="not_reproduced", license_review="required_before_packaging") for x in PAPERS]
