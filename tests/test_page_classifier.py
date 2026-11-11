import numpy as np
import pymupdf

from page_classifier import BLANK, CONTENT, SEPARATOR, classify_page, measure_ink
from separator_sheet import LETTER, add_separator_page


def _page_from_array(doc, gray):
    import cv2

    ok, png = cv2.imencode(".png", gray)
    page = doc.new_page(width=LETTER.width, height=LETTER.height)
    page.insert_image(page.rect, stream=png.tobytes())
    return page


def test_clean_separator_page():
    with pymupdf.open() as doc:
        add_separator_page(doc)
        assert classify_page(doc[0], 0).kind == SEPARATOR


def test_gray_paper_blank_page_is_blank():
    # recycled paper with sensor noise: far below the old 245 cutoff
    rng = np.random.default_rng(1)
    gray = np.clip(rng.normal(200, 4, (1100, 850)), 0, 255).astype(np.uint8)

    with pymupdf.open() as doc:
        info = classify_page(_page_from_array(doc, gray), 0)

    assert info.kind == BLANK, info


def test_edge_shadow_and_punch_holes_are_ignored():
    gray = np.full((1100, 850), 235, np.uint8)
    gray[:, :25] = 30  # dark scanner edge
    gray[:40, :] = 30
    for y in (220, 550, 880):  # punch holes
        gray[y - 12 : y + 12, 28:52] = 20

    with pymupdf.open() as doc:
        info = classify_page(_page_from_array(doc, gray), 0)

    assert info.kind == BLANK, info


def test_all_black_page_is_kept_and_flagged():
    gray = np.full((1100, 850), 15, np.uint8)

    with pymupdf.open() as doc:
        info = classify_page(_page_from_array(doc, gray), 0)

    assert info.kind == CONTENT
    assert any("dark" in note for note in info.notes)


def test_single_short_line_is_content():
    with pymupdf.open() as doc:
        page = doc.new_page(width=LETTER.width, height=LETTER.height)
        page.insert_text((200, 400), "See reverse side.", fontsize=10)
        info = classify_page(page, 0)

    assert info.kind == CONTENT, info.ink_ratio


def test_measure_ink_relative_to_background():
    light = np.full((100, 100), 210, np.uint8)
    light[50, 20:80] = 10  # a dark line

    ratio, background = measure_ink(light)

    assert background == 210
    assert ratio > 0
