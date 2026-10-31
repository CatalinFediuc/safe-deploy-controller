from itertools import product

import pytest

from deployctl.errors import InvariantError
from deployctl.models import EnvStatus
from deployctl.state import ALLOWED_TRANSITIONS, check_transition

STABLE = EnvStatus.STABLE
DEPLOYING = EnvStatus.DEPLOYING
VERIFYING = EnvStatus.VERIFYING
ROLLING_BACK = EnvStatus.ROLLING_BACK
NEEDS_ATTENTION = EnvStatus.NEEDS_ATTENTION

ALLOWED = {
    (STABLE, DEPLOYING),
    (STABLE, ROLLING_BACK),  # recover from drift
    (DEPLOYING, VERIFYING),
    (DEPLOYING, ROLLING_BACK),
    (VERIFYING, STABLE),
    (VERIFYING, ROLLING_BACK),
    (ROLLING_BACK, STABLE),
    (ROLLING_BACK, NEEDS_ATTENTION),
    (ROLLING_BACK, ROLLING_BACK),  # recover from a crash mid-rollback
    (NEEDS_ATTENTION, ROLLING_BACK),
}
DENIED = sorted(set(product(EnvStatus, EnvStatus)) - ALLOWED)


def test_every_status_has_a_row() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(EnvStatus)


@pytest.mark.parametrize(("current", "new"), sorted(ALLOWED))
def test_allowed_transition_passes(current: EnvStatus, new: EnvStatus) -> None:
    check_transition(current, new)


@pytest.mark.parametrize(("current", "new"), DENIED)
def test_illegal_transition_raises(current: EnvStatus, new: EnvStatus) -> None:
    with pytest.raises(InvariantError, match=f"{current} -> {new}"):
        check_transition(current, new)
