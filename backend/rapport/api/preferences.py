"""Preference endpoints: the user's own weighting knobs.

v1: the label prior policy — how much MBTI / 星座 / 属相 may weigh before chat evidence takes
over. v2: the offline small-model switch for rules mode. Nothing here touches the objective
score; see `scoring.prior` and `emotion.model` for the boundaries.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from rapport.api.deps import get_state
from rapport.security import SessionDep
from rapport.state.emotion import MAX_MESSAGES_RANGE

router = APIRouter(dependencies=[SessionDep])


class PriorPolicyPatch(BaseModel):
    alpha0: float | None = Field(default=None, ge=0, le=1)  # total label budget at N_eff = 0
    scale: float | None = Field(default=None, ge=5, le=500)  # evidence units per e-fold of decay
    mbti: float | None = Field(default=None, ge=0, le=100)  # relative split, only ratios matter
    zodiac: float | None = Field(default=None, ge=0, le=100)
    chinese_zodiac: float | None = Field(default=None, ge=0, le=100)
    reset: bool = False


@router.get("/preferences/prior")
def get_prior_policy(request: Request):
    return get_state(request).prior_policy_view()


@router.patch("/preferences/prior")
def patch_prior_policy(body: PriorPolicyPatch, request: Request):
    st = get_state(request)
    st.set_prior_policy(body.model_dump(exclude_none=True))
    return st.prior_policy_view()


class EmotionPatch(BaseModel):
    enabled: bool | None = None
    engine: str | None = None
    max_messages: int | None = Field(default=None, ge=MAX_MESSAGES_RANGE[0], le=MAX_MESSAGES_RANGE[1])
    use_markers: bool | None = None
    use_changepoints: bool | None = None


@router.get("/preferences/emotion")
def get_emotion_config(request: Request):
    return get_state(request).emotion_config_view()


@router.patch("/preferences/emotion")
def patch_emotion_config(body: EmotionPatch, request: Request):
    st = get_state(request)
    st.set_emotion_config(body.model_dump(exclude_none=True))
    return st.emotion_config_view()
