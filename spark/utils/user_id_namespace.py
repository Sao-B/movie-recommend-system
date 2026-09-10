"""Helpers for keeping MovieLens and application user IDs separate."""


def validate_user_id_namespace(
    max_system_user_id: int,
    max_movielens_user_id: int,
) -> None:
    """Validate that MovieLens user IDs (negated) won't conflict with system user IDs.

    System user IDs are positive integers starting from 1.
    MovieLens user IDs are negated, so they become negative integers.

    This function checks that there's no theoretical overlap, though in practice
    MovieLens max user ID (~162k) is far below any realistic system user count.
    """
    if max_system_user_id < 0:
        raise ValueError(
            f"System user IDs must be positive, got max={max_system_user_id}"
        )

    # Since MovieLens IDs become negative and system IDs are positive,
    # they naturally don't conflict. This check is mostly informational.
    print(
        f"User ID namespace validated: "
        f"system=[1, {max_system_user_id}], "
        f"MovieLens=[{-max_movielens_user_id}, -1]"
    )
