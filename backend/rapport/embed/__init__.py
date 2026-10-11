"""Offline text encoders (BGE-small-zh via ONNX Runtime) used by the grounded rules layer."""

from rapport.embed.bge import BgeEncoder, bge_status, load_encoder

__all__ = ["BgeEncoder", "bge_status", "load_encoder"]
