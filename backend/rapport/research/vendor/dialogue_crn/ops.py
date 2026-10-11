"""Native torch equivalents of segmented operations used in upstream ReasonModule."""

import torch


def softmax(src, index, num_nodes):
    chunks = []
    for node in range(num_nodes):
        mask = index == node
        part = torch.zeros_like(src)
        if mask.any():
            part[mask] = src[mask].softmax(dim=0)
        chunks.append(part)
    return sum(chunks, torch.zeros_like(src))


def scatter_add(src, index, dim=0, dim_size=None):
    if dim != 0:
        raise ValueError("DialogueCRN adapter supports dimension zero only")
    result = src.new_zeros((dim_size, *src.shape[1:]))
    return result.index_add(0, index, src)
