# DialogueCRN source provenance

Source: https://github.com/zerohd4869/DialogueCRN

Pinned commit: `2c2070d07ff190be25082804c13521aac8d25844`.

Paper: Hu et al., *DialogueCRN: Contextual Reasoning Networks for Emotion Recognition in Conversations*, ACL 2021, https://aclanthology.org/2021.acl-long.547/.

`model.py` is copied from `code/model.py`; MIT LICENSE is included. Modifications: native PyTorch segmented softmax/scatter replace optional compiled PyG/scatter imports; optional `return_features` exposes the cognitive representation before classification; constructor printing is removed. The original situation/party recurrent perception and iterative LSTM reasoning equations remain.

Rapport replaces original pretrained features with frozen Chinese BGE features and adds multilabel emotion/intent heads. Bidirectional recurrence is allowed only over the supplied window; causal experiments truncate the input at the target before invoking the original network. This does not make old post-hoc labels causal gold, nor does this recreate the published IEMOCAP/MELD/SEMAINE scores.
