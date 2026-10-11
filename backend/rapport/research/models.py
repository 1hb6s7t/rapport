"""Chinese adapters. DAG/CRN reuse author code; GCN follows paper equations 1–6.

These adapted task heads are research candidates, not original benchmark reproductions.
"""

from __future__ import annotations

from types import SimpleNamespace

import torch
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from rapport.research.data import predecessor_edges


class DAGEncoder(nn.Module):
    def __init__(self, width, hidden, causal=True):
        super().__init__()
        from rapport.research.vendor.dag_erc.model import DAGERC_fushion

        args = SimpleNamespace(emb_dim=width, hidden_dim=hidden, gnn_layers=2, dropout=.1,
                               no_rel_attn=False, attn_type="rgcn", mlp_layers=1,
                               nodal_att_type=None if causal else "global")
        self.model = DAGERC_fushion(args, 1)
        self.model.out_mlp = nn.Identity()
        self.output_width = hidden * 3 + width

    def forward(self, x, speakers, lengths):
        b, n, _ = x.shape
        adjacency = x.new_zeros((b, n, n))
        for k in range(b):
            for i, j in predecessor_edges(speakers[k, :lengths[k]].tolist()):
                adjacency[k, i, j] = 1
        same = (speakers[:, :, None] == speakers[:, None, :]).long()
        return self.model(x, adjacency, same, nn.functional.one_hot(same, 2).float(), lengths.tolist())


class CRNEncoder(nn.Module):
    def __init__(self, width, hidden, causal=True):
        super().__init__()
        from rapport.research.vendor.dialogue_crn.model import DialogueCRN

        self.model = DialogueCRN(base_layer=1, input_size=width, hidden_size=hidden // 2,
                                 n_classes=1, dropout=0, reason_steps=[2, 2])
        self.model.cognition_net.return_features = True
        self.output_width = hidden * 4

    def forward(self, x, speakers, lengths):
        qmask = (nn.functional.one_hot(speakers.clamp_min(0), 2).float() * (speakers >= 0).unsqueeze(-1)).transpose(0, 1)
        self.model.cognition_net.cuda_flag = x.is_cuda
        encoded = self.model(x.transpose(0, 1), qmask, lengths.tolist())
        parts = list(torch.split(encoded, lengths.tolist()))
        return nn.utils.rnn.pad_sequence(parts, batch_first=True)


class GCNEncoder(nn.Module):
    """Dense DialogueGCN equations, no external PyG/scatter wheel required.

    2M² relation types encode source speaker, target speaker, past/future. Eq.2
    normalizes per relation; self edges use a separate transform. Window=10.
    This is an independent formula implementation, not byte-for-byte upstream code.
    """

    def __init__(self, width, hidden, causal=True):
        super().__init__()
        self.causal = causal
        self.rnn = nn.GRU(width, hidden if causal else hidden // 2, batch_first=True, bidirectional=not causal)
        self.edge = nn.Linear(hidden, hidden, bias=False)
        self.relations = nn.ModuleList([nn.Linear(hidden, hidden, bias=False) for _ in range(8)])
        self.self1 = nn.Linear(hidden, hidden, bias=False)
        self.neighbor2 = nn.Linear(hidden, hidden, bias=False)
        self.self2 = nn.Linear(hidden, hidden, bias=False)
        self.attention = nn.Linear(hidden * 2, hidden * 2, bias=False)
        self.output_width = hidden * 2

    def forward(self, x, speakers, lengths):
        packed = pack_padded_sequence(x, lengths.cpu(), batch_first=True, enforce_sorted=False)
        g, _ = self.rnn(packed)
        g, _ = pad_packed_sequence(g, batch_first=True, total_length=x.shape[1])
        b, n, _ = g.shape
        ids = torch.arange(n, device=x.device)
        valid = ids[None, :] < lengths[:, None]
        window = (ids[:, None] - ids[None, :]).abs() <= 10
        if self.causal:
            window &= ids[:, None] >= ids[None, :]
        allowed = window[None] & valid[:, None, :] & valid[:, :, None]
        scores = torch.bmm(self.edge(g), g.transpose(1, 2))
        alpha = scores.masked_fill(~allowed, -1e4).softmax(-1) * allowed
        h1 = self.self1(g) * alpha.diagonal(dim1=1, dim2=2).unsqueeze(-1)
        # Target i, source j; temporal bit denotes source occurring in target's future.
        rel_ids = speakers[:, :, None].clamp_min(0) * 4 + speakers[:, None, :].clamp_min(0) * 2 + (ids[None, :] > ids[:, None]).long()[None]
        no_self = ~torch.eye(n, dtype=torch.bool, device=x.device)[None]
        for relation, linear in enumerate(self.relations):
            mask = (rel_ids == relation) & allowed & no_self
            count = mask.sum(-1, keepdim=True).clamp_min(1)
            h1 = h1 + torch.bmm(alpha * mask / count, linear(g))
        h1 = h1.relu()
        h2 = (torch.bmm((allowed & no_self).float(), self.neighbor2(h1)) + self.self2(h1)).relu()
        h = torch.cat([g, h2], dim=-1)
        nodal = valid[:, None, :] & valid[:, :, None]
        if self.causal:
            nodal &= (ids[:, None] >= ids[None, :])[None]
        beta = torch.bmm(self.attention(h), h.transpose(1, 2)).masked_fill(~nodal, -1e4).softmax(-1) * nodal
        return torch.bmm(beta, h) * valid.unsqueeze(-1)


class UtteranceEncoder(nn.Module):
    def __init__(self, width, hidden, causal=True):
        super().__init__()
        self.layers = nn.Sequential(nn.Linear(width, hidden), nn.ReLU(), nn.Dropout(.1))
        self.output_width = hidden

    def forward(self, x, speakers, lengths):
        return self.layers(x)


ENCODERS = {"dag_erc": DAGEncoder, "dialogue_crn": CRNEncoder, "dialogue_gcn": GCNEncoder, "utterance_mlp": UtteranceEncoder}


class MultiTask(nn.Module):
    def __init__(self, method, architecture, width, hidden, label_counts, causal=True):
        super().__init__()
        if architecture not in {"shared", "separate"}:
            raise ValueError("Unsupported architecture")
        self.architecture = architecture
        tasks = list(label_counts)
        keys = ["shared"] if architecture == "shared" else tasks
        self.encoders = nn.ModuleDict({key: ENCODERS[method](width, hidden, causal) for key in keys})
        self.heads = nn.ModuleDict({task: nn.Linear(self.encoders[keys[0]].output_width, label_counts[task]) for task in tasks})

    def forward(self, x, speakers, lengths, targets):
        output = {}
        shared = self.encoders["shared"](x, speakers, lengths) if self.architecture == "shared" else None
        for task, head in self.heads.items():
            z = shared if shared is not None else self.encoders[task](x, speakers, lengths)
            output[task] = head(z[torch.arange(x.shape[0], device=x.device), targets])
        return output
