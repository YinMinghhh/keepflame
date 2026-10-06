import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")

from keepflame.flames.joinquant.matching import MatchError, solve_challenge
from tests.auto_images import fixtures


@pytest.mark.parametrize("seed", [2, 41, 98])
def test_generated_image_match_has_correct_coordinates(seed):
    payload, piece, expected = fixtures(seed=seed)
    result = solve_challenge(payload, piece)
    assert (result.x, result.y) == expected
    assert result.confidence >= 0.99 and result.margin >= 0.04


def test_duplicate_matching_candidates_are_rejected():
    payload, piece, _ = fixtures(duplicate=True)
    with pytest.raises(MatchError, match="ambiguous_match"):
        solve_challenge(payload, piece)


def test_flat_template_is_not_guessed():
    payload, piece, _ = fixtures(flat=True)
    with pytest.raises(MatchError, match="flat_piece"):
        solve_challenge(payload, piece)


@pytest.mark.parametrize("offset", [[999,0], [False,0], [1,0], [None,0]])
def test_invalid_layout_is_rejected(offset):
    payload, piece, _ = fixtures()
    payload["data"]["point"][0] = offset
    with pytest.raises(MatchError, match="tile_offset"):
        solve_challenge(payload, piece)


def test_non_data_url_is_not_fetched():
    payload, _, _ = fixtures()
    with pytest.raises(MatchError, match="image_format"):
        solve_challenge(payload, "https://example.invalid/private-image.png")


@pytest.mark.parametrize("options", [{"masked":True}, {"jpeg":True}])
def test_generated_masked_or_compressed_images(options):
    payload, piece, expected = fixtures(**options)
    result = solve_challenge(payload, piece)
    assert (result.x,result.y) == expected


def test_canonical_numeric_string_offsets_are_supported():
    payload, piece, expected = fixtures()
    payload["data"]["point"] = [[str(x),str(y)] for x,y in payload["data"]["point"]]
    result = solve_challenge(payload, piece)
    assert (result.x,result.y) == expected
