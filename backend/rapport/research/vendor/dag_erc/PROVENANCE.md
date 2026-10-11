# DAG-ERC source provenance

Source: https://github.com/shenwzh3/DAG-ERC

Pinned commit: `ba7639dd12cdfa77cbde762f22f94266b30073d2`.

Paper: Shen et al., *Directed Acyclic Graph Network for Conversational Emotion Recognition*, ACL 2021, https://aclanthology.org/2021.acl-long.123/.

`model.py`, `model_utils.py`, and `LICENSE` are copied from this revision. Apache-2.0 LICENSE accompanies the source. The only source changes are a package-relative import and file-level lint exclusions/notices. The original spelling `DAGERC_fushion` is retained. Rapport uses the authors' default `rgcn` gatherer, both content/position GRUs, and predecessor construction up to the previous same-speaker utterance.

Chinese adaptation changes outside these files: frozen Chinese BGE sentence features replace the original RoBERTa features; emotion and intent use multilabel task heads; the context is bounded and held-out by source groups. These are **adaptation experiments**, not reproduction of the original IEMOCAP/MELD paper scores. Author-provided precomputed benchmark features are not downloaded or redistributed because their underlying dataset authorization has not been supplied.
